"""
permissions.py - 役職と保有権限のモデル（TODO.md の D-2）

`概要` の評価軸のうち「人物側: 役職・保有権限」「タスク側: 権限」を担当する。

**これは学習の特徴量ではなく、候補集合を絞るハード制約**として使う。
無資格者に危険作業を割り当てて失敗から学ぶ、という設計は成立しないため、
WariAthena(ai_stub) を呼ぶ「前」にここで候補を落とす。勤続年数でも代用できない
（20年でも無資格は不可、1年でも有資格なら可）。

DBの持ち方:
    workers.role         役職コード1つ。ROLE_GRANTS の既定権限が自動で付く
    workers.permissions  個別に付与した権限コード。カンマ区切り
    tasks.required_permissions
                         そのタスクに必要な権限コード。カンマ区切りで
                         **すべて**必要。空なら誰でも着手できる
    tasks.required_workers
                         2以上なら複数人タスク。必要権限は**メンバーの誰か1人が
                         持っていればよい**（権限ごとに判定する）。そのため1人ずつの
                         判定（allows）では落とさず、人数がそろう時点で
                         team_missing() でまとめて見る

有効な権限 = 役職由来 ∪ 個別付与。役職を割当に効かせるのはこの経路で、
作業者側の権限・役職は文脈ベクトルに足していない（役職を特徴量にする案は
TODO.md の D-2 に残してある）。

一方、**タスク側の必要権限**は ai_stub が文脈ベクトルに入れている。こちらは
制約ではなく「どういう種類の作業か」を表す特徴量で、溶接の得意な人・検査の
得意な人を学習するために使う（候補を絞る役割は引き続きここが担う）。
"""

# 権限コード → 表示名。現場の法定資格を想定した最小セット。
# 増やすときはここに1行足すだけでよく、DBのスキーマ変更は要らない。
PERMISSIONS = {
    "forklift":   "フォークリフト運転",
    "crane":      "クレーン・玉掛け",
    "press":      "プレス機械作業主任者",
    "welding":    "アーク溶接",
    "electric":   "低圧電気取扱",
    "inspection": "完成検査",
}

# 役職コード → 表示名。上から順に権限が広い想定で並べている
ROLES = {
    "member":     "一般作業者",
    "leader":     "班長",
    "supervisor": "作業主任者",
    "manager":    "管理者",
}
DEFAULT_ROLE = "member"

# 役職に自動で付く権限。個別付与と合わせて「有効な権限」になる。
# 管理者は全部持つ（デモで権限切れを起こしても詰まないようにする意味もある）
ROLE_GRANTS = {
    "member":     (),
    "leader":     ("inspection",),
    "supervisor": ("inspection", "press"),
    "manager":    tuple(PERMISSIONS),
}


def _get(obj, key, default=""):
    """sqlite3.Row と dict のどちらでも同じように読む。無い列は default"""
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def parse(text) -> list:
    """カンマ区切りの権限コード列を list にする。重複と空白は落とす"""
    if not text:
        return []
    if isinstance(text, (list, tuple, set)):
        items = list(text)
    else:
        items = str(text).replace("、", ",").split(",")
    out = []
    for item in items:
        code = str(item).strip()
        if code and code not in out:
            out.append(code)
    return out


def dump(codes) -> str:
    """フォームから来た権限コード列をDBに入れる形（カンマ区切り）にする"""
    return ",".join(parse(codes))


def label(code) -> str:
    """権限コードの表示名。未知のコードはそのまま返す（DBの値を消さない）"""
    return PERMISSIONS.get(code, code)


def labels(codes) -> list:
    return [label(c) for c in parse(codes)]


def role_label(code) -> str:
    return ROLES.get(code or DEFAULT_ROLE, code or DEFAULT_ROLE)


def granted_by_role(role) -> list:
    return list(ROLE_GRANTS.get(role or DEFAULT_ROLE, ()))


def held(worker) -> list:
    """その作業者が実際に持っている権限（役職由来 ∪ 個別付与）"""
    return parse(granted_by_role(_get(worker, "role")) + parse(_get(worker, "permissions")))


def required(task) -> list:
    """そのタスクに必要な権限"""
    return parse(_get(task, "required_permissions"))


def missing(worker, task) -> list:
    """不足している権限。空リストなら着手できる"""
    have = set(held(worker))
    return [c for c in required(task) if c not in have]


def allows(worker, task) -> bool:
    return not missing(worker, task)


def team_size(task) -> int:
    """タスクに必要な人数。1なら1人で行うタスク"""
    try:
        return max(1, int(_get(task, "required_workers", 1) or 1))
    except (TypeError, ValueError):
        return 1


def team_missing(workers, task) -> list:
    """メンバーの誰も持っていない必要権限。空リストならこの顔ぶれで着手できる"""
    have = set()
    for w in workers:
        have.update(held(w))
    return [c for c in required(task) if c not in have]


def eligible_workers(workers, task) -> list:
    """
    タスクの担当者（複数人タスクならリーダー）になれる作業者だけを残す。順序は入力のまま。

    複数人タスクは権限を持たない人もリーダーになれる（持っている人が加われば足りる）。
    ただし、作業者全体を見ても誰も持っていない権限があれば、誰も候補にしない
    """
    if team_size(task) > 1:
        return list(workers) if not team_missing(workers, task) else []
    return [w for w in workers if allows(w, task)]


def eligible_tasks(worker, tasks) -> list:
    """その作業者が着手できる（複数人タスクなら呼びかけられる）タスクだけを残す。順序は入力のまま"""
    return [t for t in tasks if team_size(t) > 1 or allows(worker, t)]
