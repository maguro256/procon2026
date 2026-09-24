from __future__ import annotations

"""
ai_stub.py - タスク割り当てAI (WariAthena) と app.py をつなぐ層

AI.py で検証した Thompson Sampling（作業者ごとのベイズ線形回帰）を
そのまま使う。app.py が呼ぶのは assign_task() / rank_tasks() /
update_model() の3つだけなので、AI.py 側のクラスを差し替えても
このファイルの外には影響しない。

文脈ベクトル x は AI.py の worker.get_context() と同じ8次元:
    x[0]    勤続年数 / 30
    x[1]    難易度スケール（AI.py の Simulator.task_difficulty と同じ値）
    x[2]    x[0] * x[1]
    x[3:8]  難易度の one-hot（TASK_COUNT = 5）

報酬 r は AI.py の RewardConverter が計算する:
    「全従業員の同難易度タスクの平均所要時間 / 今回の実所要時間」に
    速い/普通/遅いの係数を掛けたもの。

学習した mu / sigma はDBに保存しない。呼ばれるたびに work_logs を
古い順に再生して組み立て直す。ベイズ更新 A ← A + xxᵀ, b ← b + rx は
足し算なので順に再生すれば同じ事後分布になり、app.py を再起動しても
学習内容が失われない。再生結果はログが増えるまでキャッシュする。
"""

import threading

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

_lock = threading.Lock()
_cache_key = None
_beliefs: dict = {}


# ---------------------------------------------------------------- 文脈ベクトル

def _task_type(difficulty) -> int:
    """難易度 1-5 を one-hot の添字 0-4 にする。AI.py では両者が1対1に対応している"""
    try:
        d = int(difficulty)
    except (TypeError, ValueError):
        d = 3
    return max(0, min(TASK_COUNT - 1, d - 1))


def _context(years_of_service, difficulty) -> list:
    """AI.py の worker.get_context() と同じ形の文脈ベクトルを作る"""
    try:
        years = float(years_of_service or 0.0)
    except (TypeError, ValueError):
        years = 0.0
    year_norm = max(0.0, min(years, MAX_YEARS)) / MAX_YEARS
    t = _task_type(difficulty)
    scale = DIFFICULTY_SCALE[t]
    onehot = [0] * TASK_COUNT
    onehot[t] = 1
    return [year_norm, scale, year_norm * scale] + onehot


# ---------------------------------------------------------------- 学習

def _fit(work_logs: list) -> dict:
    """work_logs を古い順に再生して worker_id ごとの事後分布を作る"""
    converter = RewardConverter()
    beliefs: dict = {}
    # duration_sec が 0 や NULL のログは報酬が計算できない（0除算）ので捨てる
    usable = [l for l in work_logs
              if l.get("worker_id") and (l.get("duration_sec") or 0) > 0]
    usable.sort(key=lambda l: (l.get("completed_at") or "", l.get("id") or 0))
    for log in usable:
        wid = int(log["worker_id"])
        t = _task_type(log.get("difficulty"))
        x = _context(log.get("years_of_service"), log.get("difficulty"))
        feedback = FELT_TO_FEEDBACK.get(log.get("felt_difficulty"))  # 未回答なら None
        reward = converter.to_reward(feedback, float(log["duration_sec"]), t)
        belief = beliefs.get(wid)
        if belief is None:
            belief = beliefs[wid] = workerBelief()
        belief.update(x, reward)
    return beliefs


def _get_beliefs(work_logs: list) -> dict:
    """ログが増えていなければ前回の再生結果を使い回す"""
    global _cache_key, _beliefs
    key = (len(work_logs), max((l.get("id") or 0) for l in work_logs) if work_logs else 0)
    with _lock:
        if key != _cache_key:
            _beliefs = _fit(work_logs)
            _cache_key = key
        return _beliefs


def _belief_for(beliefs: dict, worker_id: int):
    """実績のない作業者は事前分布のまま。分散が大きいので Thompson Sampling が自然に試す"""
    b = beliefs.get(worker_id)
    if b is None:
        b = beliefs[worker_id] = workerBelief()
    return b


# ---------------------------------------------------------------- app.py が呼ぶ口

def is_ready() -> bool:
    """AI本体が使える状態か。管理画面の文言を出し分けるのに使う"""
    return _AI_READY


def _fallback_worker(workers: list):
    """AIが使えないときの保険。勤続年数が最長の作業者を返すだけ"""
    return max(workers, key=lambda w: w.get("years_of_service") or 0)["id"]


def assign_task(task: dict, workers: list, work_logs: list):
    """
    タスクに最適な作業者の worker_id を返す。候補がいなければ None。

    作業者ごとの事後分布から theta を1本ずつ引き（Thompson Sampling）、
    予測報酬が最大の人を選ぶ。AI.py のメインループと同じ手順。
    """
    if not workers:
        return None
    if not _AI_READY:
        return _fallback_worker(workers)
    try:
        beliefs = _get_beliefs(work_logs)
        difficulty = task.get("difficulty", 3)
        best_id, best_score = None, float("-inf")
        for w in workers:
            x = _context(w.get("years_of_service"), difficulty)
            belief = _belief_for(beliefs, w["id"])
            score = float(belief.predict(x, belief.sample_theta()))
            if score > best_score:
                best_score, best_id = score, w["id"]
        return best_id
    except Exception as exc:
        print(f"[ai] 割り当てに失敗したのでダミーに切り替えます: {exc}")
        return _fallback_worker(workers)


def rank_tasks(worker: dict, tasks: list, work_logs: list) -> list:
    """
    作業者1人に対して、候補タスクを「その人が速く終わらせられそうな順」に並べ替える。

    決定1回につき theta は1本だけ引く（Thompson Sampling の作法）。
    タスクごとに引き直すと同じ人の中で基準がぶれる。
    """
    if not tasks or not _AI_READY:
        return list(tasks)
    try:
        beliefs = _get_beliefs(work_logs)
        belief = _belief_for(beliefs, worker["id"])
        theta = belief.sample_theta()
        years = worker.get("years_of_service")
        scored = [(float(belief.predict(_context(years, t.get("difficulty")), theta)), i, t)
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
    _get_beliefs の cache_key（件数と最大id）が変わらず、そのままだと
    モジュール側から届いたフィードバックが学習に反映されない。
    api_work_log_feedback からもこれを呼んでキャッシュを捨てる。
    """
    global _cache_key
    with _lock:
        _cache_key = None
