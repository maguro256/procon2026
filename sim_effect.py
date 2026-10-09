"""
システム導入効果のシミュレーション（離散イベント＋作業者の移動）

工場レイアウト
- 機材を7m間隔で並べた床（1列最大10台）、管理者席、登録用PC
- 作業者は通路を歩く（直角に曲がる距離 / 歩行速度 1m/秒）

従来運用
- タスク完了 → 管理者席まで歩いて指示待ち 3分 → 機材まで歩く
- 機材に着いたら確率 p で前の作業者探し 10分（他の機材を歩き回る）
- 突発タスクは今の作業が終わってから PC まで歩いて入力 1分
- 割当は2通り: 「順番どおり」と「熟練管理者」（上位4割だと思う人に難易度4以上を回す。
  管理者の見立てには誤差がある）

システム導入
- タスク完了 → その場の機材でタッチ確認 10秒 → 割り当てられた機材まで歩く
- 作業者探しは「引き継ぎ確認」として一部残る（従来の p × 残存率）
- 突発タスクはその場で音声登録 20秒。失敗率 15% で言い直し、2回失敗したら PC へ
- 操作ミス・タッチ忘れ・システム不調が 5% あり、そのときは従来どおり管理者席へ
- AI割当は 5% の確率で作業者に断られ、2番目の候補を渡す（再タッチ 10秒）

能力モデル
- 時間倍率 = c + k(1-能力)(難易度/5)。能力差の大きさ spread で k を変える
  （spread=0 で全員同じ速さ、1 で熟練者0.7倍〜新人は難易度5で1.9倍）

同じ乱数（タスク列・ばらつき・作業者探しの発生）を全シナリオで共有して比較する。
"""
import argparse
import heapq
import random
import statistics as st

N_WORKERS = 5
N_EQUIP = 10
BASE_MIN = {1: 15, 2: 25, 3: 40, 4: 60, 5: 90}  # 標準的な人がかかる時間（分）
DIFF_WEIGHTS = [0.20, 0.25, 0.25, 0.18, 0.12]

FLOOR = (40.0, 21.0)
MACHINES = []
OFFICE = (3.0, 2.0)   # 管理者席（朝はここに集合）
PC = (9.0, 2.0)       # 登録用PC
SPEED = 60.0          # m/分

PC_INPUT = 1.0        # PC での入力時間（分）
WASTE_KEYS = ["walk", "dispatch", "search", "register", "equip_wait"]

SPREAD = 1.0


def configure(n_workers, n_equip):
    """作業者数・機材数と工場レイアウトを決める。機材は1列に最大10台、7m間隔で並べる。"""
    global N_WORKERS, N_EQUIP, MACHINES, FLOOR
    N_WORKERS, N_EQUIP = n_workers, n_equip
    cols = 5 if n_equip <= 10 else 10
    rows = (n_equip + cols - 1) // cols
    MACHINES = [(6.0 + 7.0 * (k % cols), 9.0 + 8.0 * (k // cols)) for k in range(n_equip)]
    FLOOR = (6.0 + 7.0 * (cols - 1) + 6.0, 9.0 + 8.0 * (rows - 1) + 4.0)


def pick_top(perceived):
    """熟練管理者が「上手だ」と見ている上位4割（5人なら2人）"""
    n = max(1, round(len(perceived) * 0.4))
    return sorted(range(len(perceived)), key=lambda i: -perceived[i])[:n]


configure(N_WORKERS, N_EQUIP)


def true_mult(ability, d, spread=None):
    k = 1.2 * (SPREAD if spread is None else spread)
    c = 1.06 - k * 0.3  # 能力0.5の人が難易度3をやる倍率は spread によらず 1.06
    return c + k * (1 - ability) * (d / 5)


def dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def make_day(rng, n_planned, sudden_mean):
    tasks = [new_task(rng, 0.0, False) for _ in range(n_planned)]
    for _ in range(poisson(rng, sudden_mean)):
        tasks.append(new_task(rng, rng.uniform(30, 420), True))
    for n, t in enumerate(tasks):
        t["id"] = n
    return tasks


def new_task(rng, release, sudden):
    return {
        "d": rng.choices([1, 2, 3, 4, 5], DIFF_WEIGHTS)[0],
        "equip": rng.randrange(N_EQUIP),
        "noise": rng.lognormvariate(0, 0.15),
        "search_u": rng.random(),
        "search_pts": rng.sample(range(N_EQUIP), 2),
        "release": release,
        "sudden": sudden,
        "finder": rng.randrange(N_WORKERS),
        "voice_u": (rng.random(), rng.random()),
    }


def poisson(rng, lam):
    n, t = 0, rng.expovariate(1)
    while t < lam:
        n += 1
        t += rng.expovariate(1)
    return n


class Estimator:
    """AI側の推定: 作業者×難易度ごとの時間倍率を実績から更新（事前分布つき平均）。"""

    def __init__(self, abilities, rng):
        self.sum, self.cnt = {}, {}
        prior_w = 2.0
        for i, a in enumerate(abilities):
            guess = min(1.0, max(0.0, a + rng.gauss(0, 0.3)))  # 経験年数などからのあいまいな見積もり
            for d in BASE_MIN:
                self.sum[i, d] = true_mult(guess, d) * prior_w
                self.cnt[i, d] = prior_w

    def mult(self, i, d):
        return self.sum[i, d] / self.cnt[i, d]

    def observe(self, i, d, ratio):
        self.sum[i, d] += ratio
        self.cnt[i, d] += 1


def scenarios(p_search, resid=0.3, op_fail=0.05, voice_fail=0.15, reject=0.05):
    old = dict(system=False, dispatch=3.0, search=10.0, p_search=p_search, reject=0.0)
    new = dict(system=True, dispatch=10 / 60, search=10.0, p_search=p_search * resid,
               voice=20 / 60, voice_fail=voice_fail, op_fail=op_fail, reject=0.0)
    return {
        "従来・順番割当": dict(old, policy="fifo"),
        "従来・熟練管理者": dict(old, policy="manager"),
        "システム・順番割当": dict(new, policy="fifo"),
        "システム+AI割当": dict(new, policy="ai", reject=reject),
        "参考:理想の割当": dict(new, policy="oracle", reject=reject),
    }


def rank_tasks(policy, i, t, pos, queue, equip_free, mult_fn, top2):
    """作業者 i（時刻 t, 位置 pos）に渡す候補をよい順に並べてキュー上の添字で返す。"""
    def arrive_wait(task):
        arr = t + dist(pos, MACHINES[task["equip"]]) / SPEED
        return arr - t, max(0.0, equip_free[task["equip"]] - arr)

    idx = range(len(queue))
    if policy == "fifo":
        return sorted(idx, key=lambda k: (arrive_wait(queue[k])[1] > 0, k))
    if policy == "manager":
        if i in top2:  # 上手だと思う人には難しいものから
            key = lambda k: (arrive_wait(queue[k])[1] > 0, queue[k]["d"] < 4, k)
        else:          # それ以外には難易度3以下を順番に
            key = lambda k: (queue[k]["d"] >= 4, arrive_wait(queue[k])[1] > 0, k)
        return sorted(idx, key=key)
    avg = {d: sum(mult_fn(j, d) for j in range(N_WORKERS)) / N_WORKERS for d in BASE_MIN}

    def score(k):
        task = queue[k]
        d = task["d"]
        mine = mult_fn(i, d)
        walk, wait = arrive_wait(task)
        return mine / avg[d] + (walk + wait) / max(BASE_MIN[d] * mine, 1.0) + 0.002 * k
    return sorted(idx, key=score)


class DaySim:
    def __init__(self, tasks, abilities, sc, est=None, top2=(), rng_key=0, record=False):
        self.tasks, self.ab, self.sc, self.est = tasks, abilities, sc, est
        self.top2 = set(top2)
        self.record = record
        self.rng = [random.Random(f"{rng_key}-{i}") for i in range(N_WORKERS)]
        pol = sc["policy"]
        if pol == "oracle":
            self.mult_fn = lambda i, d: true_mult(abilities[i], d)
        elif pol == "ai":
            self.mult_fn = est.mult
        else:
            self.mult_fn = None
        self.pos = [OFFICE] * N_WORKERS
        self.segs = [[] for _ in range(N_WORKERS)]  # (t0, t1, (x0,y0), (x1,y1), state, info)
        self.stats = [{k: 0.0 for k in WASTE_KEYS + ["work", "walk_m"]} for _ in range(N_WORKERS)]
        self.equip_free = [0.0] * N_EQUIP
        self.equip_segs = [[] for _ in range(N_EQUIP)]
        self.queue = [t for t in tasks if not t["sudden"]]
        self.busy_until = [0.0] * N_WORKERS
        self.idle = [False] * N_WORKERS
        self.need_dispatch = [False] * N_WORKERS
        self.pending_reg = [[] for _ in range(N_WORKERS)]
        self.cur_work = [None] * N_WORKERS
        self.ver = [0] * N_WORKERS
        self.events, self.seq = [], 0
        self.done = 0
        self.end_time = 0.0

    # --- 記録用 ---
    def push(self, time, kind, payload):
        heapq.heappush(self.events, (time, self.seq, kind, payload))
        self.seq += 1

    def stay(self, i, t0, dur, state, info=None):
        if dur <= 0:
            return t0
        self.segs[i].append([t0, t0 + dur, self.pos[i], self.pos[i], state, info])
        if state in self.stats[i]:
            self.stats[i][state] += dur
        return t0 + dur

    def walk(self, i, t0, dest, state="walk"):
        m = dist(self.pos[i], dest)
        if m <= 0:
            return t0
        dur = m / SPEED
        # 通路を直角に歩く（横→縦）
        mid = (dest[0], self.pos[i][1])
        d1 = abs(mid[0] - self.pos[i][0]) / SPEED
        if d1 > 0:
            self.segs[i].append([t0, t0 + d1, self.pos[i], mid, state, None])
        if dur - d1 > 0:
            self.segs[i].append([t0 + d1, t0 + dur, mid, dest, state, None])
        self.pos[i] = dest
        self.stats[i]["walk_m"] += m
        self.stats[i]["walk" if state == "walk" else state] += dur
        return t0 + dur

    # --- 本体 ---
    def run(self):
        for i in range(N_WORKERS):
            self.push(0.0, "free", (i, 0))
        for t in self.tasks:
            if t["sudden"]:
                self.push(t["release"], "sudden", t)
        while self.events:
            now, _, kind, payload = heapq.heappop(self.events)
            if kind == "sudden":
                self.on_sudden(now, payload)
            elif kind == "release":
                self.queue.append(payload)
                for j in range(N_WORKERS):
                    if self.idle[j]:
                        self.idle[j] = False
                        self.ver[j] += 1
                        self.push(max(now, self.busy_until[j]), "free", (j, self.ver[j]))
            else:
                i, v = payload
                if v == self.ver[i]:
                    self.on_free(i, now)
        assert self.done == len(self.tasks), (self.done, len(self.tasks))
        return self

    def pc_register(self, i, t, task):
        t = self.walk(i, t, PC, "register")
        t = self.stay(i, t, PC_INPUT, "register", task["id"])
        self.push(t, "release", task)
        return t

    def on_sudden(self, now, task):
        i = task["finder"]
        sc = self.sc
        busy = not self.idle[i] and self.busy_until[i] > now
        if not sc["system"]:
            if busy:
                self.pending_reg[i].append(task)   # 今の作業を終えてから PC へ
                return
            self.idle[i] = False
            t = self.pc_register(i, max(now, self.busy_until[i]), task)
            self.busy_until[i] = t
            self.ver[i] += 1
            self.push(t, "free", (i, self.ver[i]))
            return
        # システム: その場で音声登録
        u1, u2 = task["voice_u"]
        tries = 1 if u1 >= sc["voice_fail"] else 2
        if tries == 2 and u2 < sc["voice_fail"]:
            if busy:
                self.pending_reg[i].append(task)   # 2回失敗 → 作業が終わったら PC へ
                return
            self.idle[i] = False
            t = self.stay(i, max(now, self.busy_until[i]), 2 * sc["voice"], "register", task["id"])
            t = self.pc_register(i, t, task)
            self.busy_until[i] = t
            self.ver[i] += 1
            self.push(t, "free", (i, self.ver[i]))
            return
        cost = tries * sc["voice"]
        self.stats[i]["register"] += cost
        if busy and self.cur_work[i] is not None:
            # 作業を一時中断して登録 → 作業の終わりがずれる
            seg, eq = self.cur_work[i]
            seg[1] += cost  # 移動中・機材待ち中に見つけた場合も作業時間に上乗せして近似
            if self.equip_free[eq] < seg[1]:
                self.equip_free[eq] = seg[1]
                self.equip_segs[eq][-1][1] = seg[1]
            self.busy_until[i] += cost
            self.push(now + cost, "release", task)
            self.ver[i] += 1
            self.push(self.busy_until[i], "free", (i, self.ver[i]))
            return
        self.idle[i] = False
        t0 = max(now, self.busy_until[i])
        self.segs[i].append([t0, t0 + cost, self.pos[i], self.pos[i], "register", task["id"]])
        self.busy_until[i] = t0 + cost
        self.push(t0 + cost, "release", task)
        self.ver[i] += 1
        self.push(self.busy_until[i], "free", (i, self.ver[i]))

    def on_free(self, i, now):
        sc = self.sc
        t = now
        self.cur_work[i] = None
        # 溜まっている突発タスクの登録（PC）
        for task in self.pending_reg[i]:
            t = self.pc_register(i, t, task)
        self.pending_reg[i].clear()

        if self.need_dispatch[i]:
            if not sc["system"] or self.rng[i].random() < sc["op_fail"]:
                t = self.walk(i, t, OFFICE)
                t = self.stay(i, t, 3.0, "dispatch")
            else:
                t = self.stay(i, t, sc["dispatch"], "dispatch")
            self.need_dispatch[i] = False

        if not self.queue:
            self.idle[i] = True
            self.busy_until[i] = t
            return

        order = rank_tasks(sc["policy"], i, t, self.pos[i], self.queue, self.equip_free,
                           self.mult_fn, self.top2)
        k = order[0]
        if sc["reject"] and len(order) > 1 and self.rng[i].random() < sc["reject"]:
            t = self.stay(i, t, 10 / 60, "dispatch")
            k = order[1]
        task = self.queue.pop(k)
        eq = task["equip"]
        t = self.walk(i, t, MACHINES[eq])

        if task["search_u"] < sc["p_search"]:
            t = self.search(i, t, eq, task)

        start = max(t, self.equip_free[eq])
        t = self.stay(i, t, start - t, "equip_wait")
        dur = BASE_MIN[task["d"]] * true_mult(self.ab[i], task["d"]) * task["noise"]
        seg = [start, start + dur, self.pos[i], self.pos[i], "work", task["id"]]
        self.segs[i].append(seg)
        self.stats[i]["work"] += dur
        self.equip_free[eq] = start + dur
        self.equip_segs[eq].append([start, start + dur, i])
        self.cur_work[i] = (seg, eq)
        if self.est is not None:
            self.est.observe(i, task["d"], dur / BASE_MIN[task["d"]])
        self.done += 1
        self.need_dispatch[i] = True
        self.busy_until[i] = start + dur
        self.end_time = max(self.end_time, start + dur)
        self.ver[i] += 1
        self.push(start + dur, "free", (i, self.ver[i]))

    def search(self, i, t, eq, task):
        """前の作業者を探して他の機材を回り、元の機材に戻る（合計 search 分）。"""
        home = MACHINES[eq]
        pts = [MACHINES[p] for p in task["search_pts"]]
        route = [home] + pts + [home]
        walk_t = sum(dist(a, b) for a, b in zip(route, route[1:])) / SPEED
        linger = max(0.0, self.sc["search"] - walk_t) / len(pts)
        for p in pts:
            t = self.walk(i, t, p, "search")
            t = self.stay(i, t, linger, "search")
        return self.walk(i, t, home, "search")

    # --- 集計 ---
    def summary(self):
        tot = {k: sum(s[k] for s in self.stats) for k in self.stats[0]}
        tot["waste"] = sum(tot[k] for k in WASTE_KEYS)
        tot["makespan"] = self.end_time
        return tot


def simulate(reps, days, p_search, n_planned, sudden_mean, seed, spread=1.0, **kw):
    global SPREAD
    SPREAD = spread
    scs = scenarios(p_search, **kw)
    results = {name: [[] for _ in range(days)] for name in scs}
    for r in range(reps):
        rng = random.Random(seed + r)
        abilities = sorted((rng.uniform(0.05, 1.0) for _ in range(N_WORKERS)), reverse=True)
        perceived = [a + rng.gauss(0, 0.15) for a in abilities]  # 管理者の見立て
        top2 = pick_top(perceived)
        ests = {n: Estimator(abilities, random.Random(seed * 7 + r)) for n, sc in scs.items()
                if sc["policy"] == "ai"}
        for day in range(days):
            tasks = make_day(rng, n_planned, sudden_mean)
            for name, sc in scs.items():
                sim = DaySim(tasks, abilities, sc, ests.get(name), top2, rng_key=(seed, r, day)).run()
                results[name][day].append(sim.summary())
    return results


def mean(xs):
    return sum(xs) / len(xs)


def ci95(xs):
    return 1.96 * st.stdev(xs) / len(xs) ** 0.5 if len(xs) > 1 else 0.0


def flat(results, name, key, day_range=None):
    days = range(len(results[name])) if day_range is None else day_range
    return [x[key] for d in days for x in results[name][d]]


def red(results, name, base, key, day_range=None):
    return (1 - mean(flat(results, name, key, day_range)) / mean(flat(results, base, key, day_range))) * 100


def report(results, args):
    names = list(results)
    fifo, mgr = names[0], names[1]
    print(f"\n=== 全{args.days}日の平均（作業者探し p={args.p_search:.0%}、能力差 spread={args.spread}） ===")
    print(f"{'シナリオ':<18}{'全タスク完了時刻':>12}{'対 熟練管理者':>12}{'対 順番割当':>10}"
          f"{'ムダ時間':>10}{'対 熟練管理者':>12}")
    for n in names:
        ms = flat(results, n, "makespan")
        print(f"{n:<16}{mean(ms):>9.1f}分±{ci95(ms):<4.1f}{red(results, n, mgr, 'makespan'):>11.1f}%"
              f"{red(results, n, fifo, 'makespan'):>11.1f}%"
              f"{mean(flat(results, n, 'waste')):>9.0f}人分{red(results, n, mgr, 'waste'):>11.1f}%")

    print(f"\n--- 1日あたりの内訳（{N_WORKERS}人合計） ---")
    print(f"{'シナリオ':<18}{'実作業':>7}{'歩行':>7}{'指示待ち':>7}{'人探し':>7}{'突発登録':>7}"
          f"{'機材待ち':>7}{'歩行距離':>9}")
    for n in names:
        row = "".join(f"{mean(flat(results, n, k)):>8.1f}" for k in ["work"] + WASTE_KEYS)
        print(f"{n:<16}{row}{mean(flat(results, n, 'walk_m')) / 1000:>8.2f}km")

    print("\n--- AIの学習の進み具合（全タスク完了時刻・対 熟練管理者の削減率） ---")
    print(f"{'期間':<10}" + "".join(f"{n:>18}" for n in names[2:]))
    for a, b in [(0, 1), (1, 5), (5, 10), (10, 20), (20, args.days)]:
        if a >= args.days:
            continue
        b = min(b, args.days)
        label = f"{a + 1}日目" if b - a == 1 else f"{a + 1}〜{b}日目"
        print(f"{label:<10}" + "".join(f"{red(results, n, mgr, 'makespan', range(a, b)):>17.1f}%"
                                     for n in names[2:]))


def sweep(args, label, key, values):
    print(f"\n=== 感度分析: {label}（全タスク完了時刻の削減率, 全日平均） ===")
    print(f"{key:<10}{'熟練管理者/順番':>16}{'システム/熟練管理者':>20}{'AI/熟練管理者':>16}{'AI/システム順番':>16}")
    for v in values:
        kw = dict(reps=args.sweep_reps, days=args.days, p_search=args.p_search, n_planned=args.tasks,
                  sudden_mean=args.sudden, seed=args.seed, spread=args.spread)
        kw[key] = v
        r = simulate(**kw)
        n = list(r)
        print(f"{v:<10}{red(r, n[1], n[0], 'makespan'):>15.1f}%{red(r, n[2], n[1], 'makespan'):>19.1f}%"
              f"{red(r, n[3], n[1], 'makespan'):>15.1f}%{red(r, n[3], n[2], 'makespan'):>15.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=200, help="工場（作業者の組合せ）を何通り試すか")
    ap.add_argument("--days", type=int, default=30, help="1工場あたり何日回すか（AIはこの間学習を続ける）")
    ap.add_argument("--p-search", type=float, default=0.3, help="従来運用で作業者探しが起きる確率")
    ap.add_argument("--spread", type=float, default=1.0, help="能力差の大きさ（0=全員同じ）")
    ap.add_argument("--workers", type=int, default=15)
    ap.add_argument("--equip", type=int, default=30)
    ap.add_argument("--tasks", type=int, default=None, help="1日の計画タスク数（既定: 作業者1人あたり8件）")
    ap.add_argument("--sudden", type=float, default=None, help="1日の突発タスク数の平均（既定: 1人あたり1.6件）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--sweep", action="store_true", help="能力差・作業者探しの確率で感度分析")
    ap.add_argument("--sweep-reps", type=int, default=60)
    args = ap.parse_args()
    configure(args.workers, args.equip)
    if args.tasks is None:
        args.tasks = 8 * args.workers
    if args.sudden is None:
        args.sudden = 1.6 * args.workers

    print(f"作業者{N_WORKERS}人・機材{N_EQUIP}台・計画タスク{args.tasks}件/日・突発 平均{args.sudden}件/日"
          f"・{args.reps}工場×{args.days}日")
    report(simulate(args.reps, args.days, args.p_search, args.tasks, args.sudden, args.seed, args.spread), args)
    if args.sweep:
        sweep(args, "能力差の大きさ spread", "spread", [0.0, 0.5, 1.0, 1.5, 2.0])
        sweep(args, "作業者探しの発生確率 p", "p_search", [0.0, 0.1, 0.3, 0.5])


if __name__ == "__main__":
    main()
