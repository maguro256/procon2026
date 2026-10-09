# -*- coding: utf-8 -*-
"""
WariAthena (ai_stub.assign_task) と比較手法の割当シミュレーション。
隠れた適性を持つ作業者6名に、1ラウンド6件ずつタスクを割り当てる。
ラウンド内で割り当てたタスクは「手持ち」として open_tasks に入り、
ラウンドの終わりに全員が作業を終えて work_logs に追記される。

使い方（リポジトリ直下で）: python resume/eval_assign.py 150 20
"""
import os, sys, random
from collections import Counter
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_stub

BASE = {1: 60, 2: 120, 3: 240, 4: 420, 5: 600}
KINDS = ["", "welding", "inspection", "press"]
YEARS = [1, 3, 8, 15, 25, 30]
ROUNDS, PER_ROUND = int(sys.argv[1]) if len(sys.argv) > 1 else 150, 6
SEEDS = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def make_world(rng):
    # 時間倍率（小さいほど速い）。ベテランほど全体に速いが、得意分野は人による
    apt = {}
    for w, y in enumerate(YEARS, 1):
        general = 1.3 - 0.5 * (y / 30)
        for k in KINDS:
            apt[(w, k)] = general * rng.uniform(0.7, 1.3)
        apt[(w, KINDS[1 + (w % 3)])] *= 0.6  # 1人1つ得意な作業
    return apt


def run(strategy, seed):
    rng = random.Random(seed); np.random.seed(seed)
    apt = make_world(rng)
    workers = [{"id": i, "name": f"w{i}", "years_of_service": y} for i, y in enumerate(YEARS, 1)]
    logs, lid = [], 0
    ratios, spans, counts = [], [], Counter()
    rr = 0
    for r in range(ROUNDS):
        tasks = [{"id": 10000 + r * 10 + j, "difficulty": rng.randint(1, 5), "quantity": rng.randint(1, 5),
                  "required_permissions": rng.choice(KINDS), "equipment_id": None} for j in range(PER_ROUND)]
        open_tasks, load, done = [], Counter(), []
        ai_stub.invalidate_cache()
        for t in tasks:
            if strategy == "random":
                wid = rng.choice(workers)["id"]
            elif strategy == "roundrobin":
                wid = workers[rr % len(workers)]["id"]; rr += 1
            elif strategy == "veteran":
                wid = max(workers, key=lambda w: w["years_of_service"])["id"]
            elif strategy == "ts_noload":
                wid = ai_stub.assign_task(t, workers, logs, open_tasks=None)
            elif strategy == "ts_load":
                wid = ai_stub.assign_task(t, workers, logs, open_tasks=open_tasks)
            elif strategy == "oracle":  # 真の適性を知り、終了時刻が最も早い人へ
                wid = min(workers, key=lambda w: load[w["id"]] + BASE[t["difficulty"]] * t["quantity"] * apt[(w["id"], t["required_permissions"])])["id"]
            unit = BASE[t["difficulty"]] * apt[(wid, t["required_permissions"])] * rng.lognormvariate(0, 0.15)
            dur = unit * t["quantity"]
            best = min(apt[(w["id"], t["required_permissions"])] for w in workers)
            ratios.append((r, apt[(wid, t["required_permissions"])] / best))
            load[wid] += dur
            counts[wid] += 1
            open_tasks.append(dict(t, status="assigned", assigned_worker_id=wid, started_at=None))
            lid += 1
            done.append(dict(t, id=lid, task_id=t["id"], worker_id=wid, duration_sec=int(dur),
                             years_of_service=next(w["years_of_service"] for w in workers if w["id"] == wid),
                             completed_at=f"{r:05d}-{lid:05d}", felt_difficulty=None))
        logs.extend(done)  # ラウンドの終わりに全員が作業を終える
        spans.append((r, max(load.values())))
    return ratios, spans, counts


def summary(strategy):
    late_ratio, all_ratio, late_span, top = [], [], [], []
    for s in range(SEEDS):
        ratios, spans, counts = run(strategy, s)
        all_ratio += [x for _, x in ratios]
        late_ratio += [x for r, x in ratios if r >= ROUNDS // 3]
        late_span += [x for r, x in spans if r >= ROUNDS // 3]
        top.append(max(counts.values()) / sum(counts.values()))
    return (np.mean(all_ratio), np.mean(late_ratio), np.mean(late_span) / 60, np.mean(top))


if __name__ == "__main__":
    names = sys.argv[3].split(",") if len(sys.argv) > 3 else ["random", "roundrobin", "veteran", "ts_noload", "ts_load", "oracle"]
    print("strategy  ratio_all  ratio_late  makespan_late[min]  top_share")
    for n in names:
        a, b, c, d = summary(n)
        print(f"{n:10s} {a:.3f} {b:.3f} {c:.1f} {d:.3f}", flush=True)
