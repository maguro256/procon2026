# -*- coding: utf-8 -*-
"""
ポスターのグラフ用データを作る。eval_assign.py と同じシミュレーションを回し、
ラウンドごとの適性比・完了時間（試行平均、5ラウンドの移動平均）を CSV に書く。

使い方（リポジトリ直下で）: python resume/poster_data.py 150 20
出力: resume/poster_curve.csv, resume/poster_summary.csv
"""
import os, sys
import numpy as np
import eval_assign as ev

HERE = os.path.dirname(os.path.abspath(__file__))
STRATEGIES = ["random", "roundrobin", "veteran", "ts_noload", "ts_load", "oracle"]
WINDOW = 5


def smooth(a):
    out = np.convolve(a, np.ones(WINDOW) / WINDOW, mode="valid")
    return np.concatenate([np.full(WINDOW - 1, np.nan), out])


def main():
    curves, rows = {}, []
    for s in STRATEGIES:
        ratio = np.zeros((ev.SEEDS, ev.ROUNDS))
        span = np.zeros((ev.SEEDS, ev.ROUNDS))
        tops = []
        for seed in range(ev.SEEDS):
            ratios, spans, counts = ev.run(s, seed)
            for r, x in ratios:
                ratio[seed, r] += x / ev.PER_ROUND
            for r, x in spans:
                span[seed, r] = x / 60
            tops.append(max(counts.values()) / sum(counts.values()))
        late = ev.ROUNDS // 3
        curves[s] = (smooth(ratio.mean(0)), smooth(span.mean(0)))
        # 試行ごとの学習後平均 → その平均と標準偏差
        r_late, s_late = ratio[:, late:].mean(1), span[:, late:].mean(1)
        rows.append((s, r_late.mean(), r_late.std(), s_late.mean(), s_late.std(), np.mean(tops)))
        print(f"{s:10s} ratio {r_late.mean():.2f}±{r_late.std():.2f}  "
              f"span {s_late.mean():.1f}±{s_late.std():.1f}  top {np.mean(tops):.2f}", flush=True)

    with open(os.path.join(HERE, "poster_curve.csv"), "w", encoding="utf-8") as f:
        f.write("round," + ",".join(f"{s}_ratio,{s}_span" for s in STRATEGIES) + "\n")
        for r in range(ev.ROUNDS):
            vals = []
            for s in STRATEGIES:
                vals += [curves[s][0][r], curves[s][1][r]]
            f.write(f"{r + 1}," + ",".join("nan" if np.isnan(v) else f"{v:.4f}" for v in vals) + "\n")
    with open(os.path.join(HERE, "poster_summary.csv"), "w", encoding="utf-8") as f:
        f.write("strategy,ratio,ratio_sd,span,span_sd,top\n")
        for row in rows:
            f.write("%s,%.4f,%.4f,%.3f,%.3f,%.4f\n" % row)


if __name__ == "__main__":
    main()
