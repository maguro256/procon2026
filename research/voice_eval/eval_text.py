# -*- coding: utf-8 -*-
"""文字起こしが完全に正しいとき、Gemma がタスクの各欄をどれだけ正しく取り出せるか。

    python eval_text.py [基準日 YYYY-MM-DD]   （既定 2026-11-16）

結果: results/text_<基準日>.csv と、欄ごとの正解率の表示。
音声の評価で正解率が下がったとき、どこまでが文字起こしのせいかを切り分ける上限になる。
"""
import csv, os, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "voice"))
import intent  # noqa: E402
from score import load_prompts, judge, summarize, FIELDS  # noqa: E402


def main():
    ref = datetime.strptime(sys.argv[1] if len(sys.argv) > 1 else "2026-11-16", "%Y-%m-%d")
    ok, msg = intent.available()
    if not ok:
        sys.exit("Gemma が使えません: " + msg)
    rows, out = load_prompts(), []
    t0 = time.time()
    for r in rows:
        res = intent.analyze(r["text"], today=ref)
        j = judge(r, res, ref.date())
        out.append({"id": r["id"], "set": r["set"], "text": r["text"],
                    "title": res["title"], "priority": res["priority"],
                    "quantity": res["quantity"], "deadline": res["deadline"] or "",
                    "permissions": " ".join(res["required_permissions"]),
                    **{"ok_" + k: int(v) for k, v in j.items()}})
        print(r["id"], "OK" if j["all"] else "NG " + ",".join(f for f in FIELDS if not j[f]),
              res["title"], flush=True)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", "text_{}.csv".format(ref.date()))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    print("\n{:.0f}秒  {}".format(time.time() - t0, path))
    for s in ("core", "synth", None):
        sub = [{k[3:]: v for k, v in o.items() if k.startswith("ok_")} for o in out
               if s is None or o["set"] == s]
        print(s or "全体", len(sub), "文", {k: "{:.0%}".format(v) for k, v in summarize(sub).items()})


if __name__ == "__main__":
    main()
