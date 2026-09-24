"""
WariAthena の文脈ベクトル（必要権限・機材）と、数量で割った報酬の確認。
DB も app.py も使わず、ai_stub に work_logs 相当の dict を直接渡す。

    python Test/test_ai_features.py
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import ai_stub

np.random.seed(0)


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'NG  '} {label} {detail}")
    return ok


def log(i, worker, sec, perms="", equipment=None, quantity=1, difficulty=3):
    return {"id": i, "worker_id": worker, "duration_sec": sec, "quantity": quantity,
            "difficulty": difficulty, "required_permissions": perms,
            "equipment_id": equipment, "years_of_service": 5,
            "completed_at": f"2026-09-01 00:00:{i:02d}"}


def wins(task, workers, logs, n=300):
    """assign_task を n 回引いて、誰が何回選ばれたか"""
    ai_stub.invalidate_cache()
    return Counter(ai_stub.assign_task(task, workers, logs) for _ in range(n))


results = []
W = [{"id": 1, "years_of_service": 5}, {"id": 2, "years_of_service": 5}]

# 1. 難易度は同じでも、溶接は1番・検査は2番が速い → 作業の種類で選び分ける
logs = []
for k in range(20):
    logs.append(log(len(logs), 1, 300, perms="welding"))
    logs.append(log(len(logs), 2, 600, perms="welding"))
    logs.append(log(len(logs), 1, 600, perms="inspection"))
    logs.append(log(len(logs), 2, 300, perms="inspection"))
w = wins({"difficulty": 3, "required_permissions": "welding"}, W, logs)
results.append(check("溶接タスクは溶接の速い1番へ", w[1] > 250, dict(w)))
w = wins({"difficulty": 3, "required_permissions": "inspection"}, W, logs)
results.append(check("検査タスクは検査の速い2番へ", w[2] > 250, dict(w)))

# 2. 機材でも同じ。旋盤(10)は1番、プレス(20)は2番
logs = []
for k in range(20):
    logs.append(log(len(logs), 1, 300, equipment=10))
    logs.append(log(len(logs), 2, 600, equipment=10))
    logs.append(log(len(logs), 1, 600, equipment=20))
    logs.append(log(len(logs), 2, 300, equipment=20))
w = wins({"difficulty": 3, "equipment_id": 10}, W, logs)
results.append(check("旋盤のタスクは1番へ", w[1] > 250, dict(w)))
w = wins({"difficulty": 3, "equipment_id": 20}, W, logs)
results.append(check("プレスのタスクは2番へ", w[2] > 250, dict(w)))

# rank_tasks: 1番にとって、溶接の方が検査より上に来る（機材指定の無いタスクでも）
ai_stub.invalidate_cache()
logs_perm = [log(i, 1, 300 if i % 2 else 600, perms="welding" if i % 2 else "inspection")
             for i in range(40)]
tasks = [{"id": "insp", "difficulty": 3, "required_permissions": "inspection"},
         {"id": "weld", "difficulty": 3, "required_permissions": "welding"}]
first = Counter(ai_stub.rank_tasks(W[0], tasks, logs_perm)[0]["id"] for _ in range(200))
results.append(check("1番の候補の並びは溶接が先", first["weld"] > 180, dict(first)))

# 3. 1個あたりの速さが同じなら、30個のタスクをやった人が「遅い」とされない
#    1番は1個を100秒、2番は30個を3000秒（1個100秒）。以前は2番が30倍遅い扱いだった
logs = []
for k in range(20):
    logs.append(log(len(logs), 1, 100, quantity=1))
    logs.append(log(len(logs), 2, 3000, quantity=30))
ai_stub.invalidate_cache()
model = ai_stub._get_model(logs)
x = ai_stub._task_context(model, 5, {"difficulty": 3})
m1 = ai_stub._belief_for(model, 1).predict(x, ai_stub._belief_for(model, 1).mu)
m2 = ai_stub._belief_for(model, 2).predict(x, ai_stub._belief_for(model, 2).mu)
results.append(check("数量が違っても1個あたりが同じなら同程度の評価",
                     abs(m1 - m2) < 0.2, f"(1番 {m1:.2f} / 2番 {m2:.2f})"))

# 4. 数量が空・0・壊れていても落ちない
logs = [log(0, 1, 100, quantity=None), log(1, 1, 100, quantity=0), log(2, 1, 100, quantity="x")]
ai_stub.invalidate_cache()
results.append(check("数量が不正でも学習できる",
                     ai_stub.assign_task({"difficulty": 3}, W, logs) in (1, 2)))

# 5. 負荷分散。1番が常に速い（難易度4で 300秒 vs 600秒）
logs = []
for k in range(30):
    logs.append(log(len(logs), 1, 300, difficulty=4))
    logs.append(log(len(logs), 2, 600, difficulty=4))
task = {"difficulty": 4, "quantity": 1}
w = wins(task, W, logs)
results.append(check("手持ちが無ければ速い1番へ", w[1] > 250, dict(w)))

# 1番だけ手持ちが山ほどある（難易度4を20個 ≒ 数時間）→ 2番へ回る
backlog = [{"assigned_worker_id": 1, "status": "assigned", "difficulty": 4, "quantity": 20}]
ai_stub.invalidate_cache()
w = Counter(ai_stub.assign_task(task, W, logs, open_tasks=backlog) for _ in range(300))
results.append(check("手持ちが多いと2番へ回る", w[2] > 250, dict(w)))

# 次々に割り当てると、2人ともに振り分けられる（全部1番に集まらない）
open_tasks = []
for _ in range(20):
    wid = ai_stub.assign_task(task, W, logs, open_tasks=open_tasks)
    open_tasks.append({"assigned_worker_id": wid, "status": "assigned", "difficulty": 4, "quantity": 1})
w = Counter(t["assigned_worker_id"] for t in open_tasks)
results.append(check("20件を連続で割り当てると分散する", w[1] > w[2] > 0, dict(w)))

# 作業中のタスクは経過時間ぶん手持ちが減る
from datetime import datetime, timedelta
ai_stub.invalidate_cache()
est = ai_stub.estimate_seconds({"difficulty": 4, "quantity": 1}, logs)
started = (datetime.now() - timedelta(seconds=est / 2)).strftime("%Y-%m-%d %H:%M:%S")
load = ai_stub.workloads([{"assigned_worker_id": 1, "status": "in_progress", "difficulty": 4,
                           "quantity": 1, "started_at": started}], logs)[1]
results.append(check("作業中は経過時間を引く", abs(load - est / 2) < 5,
                     f"(見積 {est:.0f}秒 / 残り {load:.0f}秒)"))

print("\nすべて期待通り" if all(results) else "\n失敗あり")
sys.exit(0 if all(results) else 1)
