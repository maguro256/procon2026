from __future__ import annotations

"""
ai_stub.py - タスク割り当てAI (WariAthena) と app.py をつなぐ層

AI.py で検証した Thompson Sampling（作業者ごとのベイズ線形回帰）を
そのまま使う。app.py が呼ぶのは assign_task() / rank_tasks() /
update_model() の3つだけなので、AI.py 側のクラスを差し替えても
このファイルの外には影響しない。

文脈ベクトル x（先頭8次元は AI.py の worker.get_context() と同じ）:
    x[0]    勤続年数 / 30
    x[1]    難易度スケール（AI.py の Simulator.task_difficulty と同じ値）
    x[2]    x[0] * x[1]
    x[3:8]  難易度の one-hot（TASK_COUNT = 5）
    x[8:14] 必要権限の multi-hot（permissions.PERMISSIONS の順。溶接・クレーン等）
    x[14:]  機材の one-hot（学習時に実績のある機材だけ。実績の無い機材は全部 0）

権限と機材を足したのは、難易度だけだと「溶接の得意な人」「旋盤の得意な人」が
区別できないため。どちらも**タスクの種類を表す固定の軸**なので、タスクを登録しても
次元は増えない。機材は台数が増えると次元が増えるが、学習は毎回ログを再生して
組み立て直すので問題ない。

報酬 r は AI.py の RewardConverter が計算する:
    「全従業員の同難易度タスクの1個あたり平均所要時間 / 今回の1個あたり所要時間」に
    速い/普通/遅いの係数を掛けたもの。所要時間は数量(quantity)で割ってから渡す。
    割らないと、30個のタスクを引いた人が「遅い」と学習されてしまう。

学習した mu / sigma はDBに保存しない。呼ばれるたびに work_logs を
古い順に再生して組み立て直す。ベイズ更新 A ← A + xxᵀ, b ← b + rx は
足し算なので順に再生すれば同じ事後分布になり、app.py を再起動しても
学習内容が失われない。再生結果はログが増えるまでキャッシュする。
"""

import threading

import permissions as perms

try:
    from AI import RewardConverter, workerBelief, TASK_COUNT
    _AI_READY = True
    _AI_ERROR = None
except Exception as exc:  # numpy 未インストールなど。管理画面自体は動かし続ける
    _AI_READY = False
    _AI_ERROR = exc
    TASK_COUNT = 5
    print(f"[ai] AI.py を読み込めませんでした。ダミー割り当てで動きます: {exc}")

# DBの difficulty(1-5) を AI.py の Simulator が使う難易度スケールへ対応させる
DIFFICULTY_SCALE = [0.15, 0.3, 0.45, 0.6, 0.85]
MAX_YEARS = 30.0

# work_logs.felt_difficulty (D-6) → RewardConverter.to_reward() の feedback 引数。
# 本人が答えていれば時間からの推定より優先する。tasks.difficulty や文脈ベクトルには
# 触らない方針は変えない（TODO.md D-6 参照）。答えていないログは None のままにして
# AI.py 側の時間ベース推定に任せる。
FELT_TO_FEEDBACK = {"easy": 0, "normal": 1, "hard": 2}

# 必要権限の軸。permissions.PERMISSIONS に権限を足すと次元も1つ増える（再生で作り直すので可）
PERMISSION_CODES = list(perms.PERMISSIONS)

_lock = threading.Lock()
_cache_key = None
# 学習結果。beliefs: worker_id -> workerBelief / eq_index: 機材id -> one-hot の添字
_model: dict = {"beliefs": {}, "eq_index": {}, "dim": 3 + TASK_COUNT + len(PERMISSION_CODES)}


# ---------------------------------------------------------------- 文脈ベクトル

def _task_type(difficulty) -> int:
    """難易度 1-5 を one-hot の添字 0-4 にする。AI.py では両者が1対1に対応している"""
    try:
        d = int(difficulty)
    except (TypeError, ValueError):
        d = 3
    return max(0, min(TASK_COUNT - 1, d - 1))


def _context(years_of_service, difficulty, required_permissions=None,
             equipment_id=None, eq_index=None) -> list:
    """
    文脈ベクトルを作る。先頭8次元は AI.py の worker.get_context() と同じ形で、
    その後ろに必要権限の multi-hot と機材の one-hot が続く（先頭の説明を参照）。
    """
    try:
        years = float(years_of_service or 0.0)
    except (TypeError, ValueError):
        years = 0.0
    year_norm = max(0.0, min(years, MAX_YEARS)) / MAX_YEARS
    t = _task_type(difficulty)
    scale = DIFFICULTY_SCALE[t]
    onehot = [0] * TASK_COUNT
    onehot[t] = 1

    required = set(perms.parse(required_permissions))
    perm_hot = [1 if code in required else 0 for code in PERMISSION_CODES]

    eq_index = eq_index or {}
    eq_hot = [0] * len(eq_index)
    i = eq_index.get(_as_id(equipment_id))
    if i is not None:
        eq_hot[i] = 1
    return [year_norm, scale, year_norm * scale] + onehot + perm_hot + eq_hot


def _as_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unit_seconds(log) -> float:
    """1個あたりの所要時間。数量が不明・不正なら1個として扱う"""
    try:
        quantity = max(int(log.get("quantity") or 1), 1)
    except (TypeError, ValueError):
        quantity = 1
    return float(log["duration_sec"]) / quantity


# ---------------------------------------------------------------- 学習

def _fit(work_logs: list) -> dict:
    """work_logs を古い順に再生して worker_id ごとの事後分布を作る"""
    converter = RewardConverter()
    # duration_sec が 0 や NULL のログは報酬が計算できない（0除算）ので捨てる
    usable = [l for l in work_logs
              if l.get("worker_id") and (l.get("duration_sec") or 0) > 0]
    usable.sort(key=lambda l: (l.get("completed_at") or "", l.get("id") or 0))

    # 機材の軸は実績のある機材だけで作る。実績の無い機材の列は学習しようがない
    eq_ids = sorted({i for i in (_as_id(l.get("equipment_id")) for l in usable) if i is not None})
    eq_index = {eq: n for n, eq in enumerate(eq_ids)}
    dim = 3 + TASK_COUNT + len(PERMISSION_CODES) + len(eq_index)

    beliefs: dict = {}
    for log in usable:
        wid = int(log["worker_id"])
        t = _task_type(log.get("difficulty"))
        x = _context(log.get("years_of_service"), log.get("difficulty"),
                     log.get("required_permissions"), log.get("equipment_id"), eq_index)
        feedback = FELT_TO_FEEDBACK.get(log.get("felt_difficulty"))  # 未回答なら None
        reward = converter.to_reward(feedback, _unit_seconds(log), t)
        belief = beliefs.get(wid)
        if belief is None:
            belief = beliefs[wid] = workerBelief(dim)
        belief.update(x, reward)
    # 難易度ごとの1個あたり平均所要時間。負荷分散の見積もりに使う（実績が無ければ None）
    unit_seconds = [float(converter.task_avgtime_list[i]) if converter.tasked_count[i] else None
                    for i in range(TASK_COUNT)]
    return {"beliefs": beliefs, "eq_index": eq_index, "dim": dim, "unit_seconds": unit_seconds}


def _get_model(work_logs: list) -> dict:
    """ログが増えていなければ前回の再生結果を使い回す"""
    global _cache_key, _model
    key = (len(work_logs), max((l.get("id") or 0) for l in work_logs) if work_logs else 0)
    with _lock:
        if key != _cache_key:
            _model = _fit(work_logs)
            _cache_key = key
        return _model


def _belief_for(model: dict, worker_id: int):
    """実績のない作業者は事前分布のまま。分散が大きいので Thompson Sampling が自然に試す"""
    beliefs = model["beliefs"]
    b = beliefs.get(worker_id)
    if b is None:
        b = beliefs[worker_id] = workerBelief(model["dim"])
    return b


def _task_context(model: dict, years_of_service, task: dict, equipment_id=None) -> list:
    """予測用の文脈。機材はタスクに指定があればそれ、無ければ呼び出し側が渡した機材"""
    return _context(years_of_service, task.get("difficulty"), task.get("required_permissions"),
                    task.get("equipment_id") or equipment_id, model["eq_index"])


# ---------------------------------------------------------------- app.py が呼ぶ口

def is_ready() -> bool:
    """AI本体が使える状態か。管理画面の文言を出し分けるのに使う"""
    return _AI_READY


def _fallback_worker(workers: list, loads=None):
    """AIが使えないときの保険。手持ちの一番少ない人、同じなら勤続年数の長い人"""
    loads = loads or {}
    return min(workers, key=lambda w: (loads.get(w["id"], 0.0),
                                       -(w.get("years_of_service") or 0)))["id"]


# ---------------------------------------------------------------- 負荷分散
# 予測報酬だけで選ぶと、何でも一番速い人（ベテラン）に全部集まる。そこで
# 手持ちの仕事の量で点数を割る:
#
#   点数 = 予測報酬 ÷ (1 + 手持ちの残り時間 ÷ このタスクの見積もり時間)
#
# 手持ちがこのタスク1本ぶんあれば点数は半分、2本ぶんなら1/3。
# 「引き算」にしないのは、報酬が速さに比例しないため（体感係数 1.5/1.0/0.5 が
# 掛かるので、2倍速い人の報酬は6倍になりうる）。割り算なら報酬の尺度に依らず、
# 差が大きい仕事（難しい作業のベテランなど）ほど手持ちが多くても任され続ける。
# 大きなタスクほど手持ちの影響は小さい（着手が少し遅れても全体では誤差になる）。
#
# 実績が1件も無いときの1個あたりの見積もり（秒）
DEFAULT_UNIT_SECONDS = 600.0


def _unit_estimates(model: dict) -> list:
    """難易度ごとの1個あたり平均所要時間。実績の無い難易度は、ある難易度の平均で埋める"""
    known = [s for s in model.get("unit_seconds", []) if s]
    fill = sum(known) / len(known) if known else DEFAULT_UNIT_SECONDS
    return [s or fill for s in model.get("unit_seconds", [None] * TASK_COUNT)]


def estimate_seconds(task: dict, work_logs: list) -> float:
    """そのタスクに標準的にかかる時間の見積もり（難易度ごとの平均 × 数量）"""
    units = _unit_estimates(_get_model(work_logs)) if _AI_READY else [DEFAULT_UNIT_SECONDS] * TASK_COUNT
    try:
        quantity = max(int(task.get("quantity") or 1), 1)
    except (TypeError, ValueError):
        quantity = 1
    return units[_task_type(task.get("difficulty"))] * quantity


def workloads(open_tasks: list, work_logs: list, now=None) -> dict:
    """
    作業者ごとの手持ちの残り時間（秒）。open_tasks は割り当て済み・作業中のタスク。
    作業中のものは着手からの経過時間を引く（見積もりを超えていたら0）。
    """
    from datetime import datetime

    now = now or datetime.now()
    loads: dict = {}
    for t in open_tasks:
        wid = t.get("assigned_worker_id")
        if not wid:
            continue
        remaining = estimate_seconds(t, work_logs)
        if t.get("status") == "in_progress" and t.get("started_at"):
            try:
                started = datetime.strptime(t["started_at"], "%Y-%m-%d %H:%M:%S")
                remaining = max(0.0, remaining - (now - started).total_seconds())
            except ValueError:
                pass
        loads[wid] = loads.get(wid, 0.0) + remaining
    return loads


def assign_task(task: dict, workers: list, work_logs: list, open_tasks=None):
    """
    タスクに最適な作業者の worker_id を返す。候補がいなければ None。

    作業者ごとの事後分布から theta を1本ずつ引き（Thompson Sampling）、
    予測報酬が最大の人を選ぶ。AI.py のメインループと同じ手順。
    open_tasks（割り当て済み・作業中のタスク）を渡すと、手持ちの多い人ほど
    点数を下げて負荷を分散する（上の「負荷分散」を参照）。
    """
    if not workers:
        return None
    loads = workloads(open_tasks or [], work_logs)
    if not _AI_READY:
        return _fallback_worker(workers, loads)
    try:
        model = _get_model(work_logs)
        this_task = max(estimate_seconds(task, work_logs), 1.0)
        best_id, best_score = None, float("-inf")
        for w in workers:
            x = _task_context(model, w.get("years_of_service"), task)
            belief = _belief_for(model, w["id"])
            score = float(belief.predict(x, belief.sample_theta()))
            busy = 1.0 + loads.get(w["id"], 0.0) / this_task
            # 実績の少ない人は事前分布が広く、負の点数も引く。負を割ると
            # 手持ちが多いほど良く見えてしまうので、そのときは掛ける
            score = score / busy if score >= 0 else score * busy
            if score > best_score:
                best_score, best_id = score, w["id"]
        return best_id
    except Exception as exc:
        print(f"[ai] 割り当てに失敗したのでダミーに切り替えます: {exc}")
        return _fallback_worker(workers, loads)


def rank_tasks(worker: dict, tasks: list, work_logs: list, equipment_id=None) -> list:
    """
    作業者1人に対して、候補タスクを「その人が速く終わらせられそうな順」に並べ替える。
    equipment_id は機材指定の無いタスクをどの機材でやる想定で評価するか（タッチされた機材）。

    決定1回につき theta は1本だけ引く（Thompson Sampling の作法）。
    タスクごとに引き直すと同じ人の中で基準がぶれる。
    """
    if not tasks or not _AI_READY:
        return list(tasks)
    try:
        model = _get_model(work_logs)
        belief = _belief_for(model, worker["id"])
        theta = belief.sample_theta()
        years = worker.get("years_of_service")
        scored = [(float(belief.predict(_task_context(model, years, t, equipment_id), theta)), i, t)
                  for i, t in enumerate(tasks)]
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [t for _, _, t in scored]
    except Exception as exc:
        print(f"[ai] 並べ替えに失敗したので元の順序で返します: {exc}")
        return list(tasks)


def update_model(task: dict, worker_id: int, duration_sec: int, work_logs: list) -> None:
    """
    タスク完了時の学習フック。work_logs への追記は app.py 側で済んでいるので、
    ここではキャッシュを捨てて次回の呼び出しで再学習させるだけでよい。
    """
    invalidate_cache()


def invalidate_cache() -> None:
    """
    次回の assign_task/rank_tasks で work_logs を再生し直させる。

    felt_difficulty (D-6) は既存の work_logs 行を後から UPDATE するだけなので、
    _get_model の cache_key（件数と最大id）が変わらず、そのままだと
    モジュール側から届いたフィードバックが学習に反映されない。
    api_work_log_feedback からもこれを呼んでキャッシュを捨てる。
    """
    global _cache_key
    with _lock:
        _cache_key = None
