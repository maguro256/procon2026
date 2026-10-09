# -*- coding: utf-8 -*-
"""
旧UI（審査用・classic）と新UI（デモ用・new）の切り替えの確認。

  1. Cookie が無ければ旧UIで描かれる（審査で初めて開いた画面に新UIを出さない）
  2. /ui/new・/ui/classic で切り替わり、元の画面へ戻る。外部URLへは飛ばない
  3. どちらのUIでも全画面がエラーなく描ける（サーバー側の変更で片方だけ壊すのを防ぐ）
  4. 旧UIには切り替えの存在を出さない

    python Test\\test_ui_switch.py
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())
import db

tmp = Path(tempfile.mkdtemp()) / "t.db"
db.DB_PATH = tmp
db.init_db(seed=True)

import app

ok = True


def check(label, got, want):
    global ok
    mark = "OK " if got == want else "NG "
    if got != want:
        ok = False
    print(f"{mark}{label}: {got!r}" + ("" if got == want else f"  期待 {want!r}"))


PAGES = ["/", "/tasks", "/workers", "/equipment", "/test"]

# 1. 既定は旧UI
c = app.app.test_client()
html = c.get("/").get_data(as_text=True)
check("既定は旧UIのCSS", "classic/style.css" in html, True)
check("既定で新UIのCSSは読まない", "new/style.css" in html, False)

# 2. 切り替え
res = c.get("/ui/new?next=/tasks")
check("/ui/new は元の画面へ戻る", (res.status_code, res.headers["Location"].endswith("/tasks")), (302, True))
check("Cookie に new が入る", "gemmba_ui=new" in res.headers.get("Set-Cookie", ""), True)
html = c.get("/tasks").get_data(as_text=True)
check("切り替え後は新UIのCSS", "new/style.css" in html, True)
check("新UIには旧UIへ戻す口がある", "旧UIに戻す" in html, True)

for bad in ["https://example.com/", "//example.com/", "/\\example.com"]:
    res = c.get("/ui/classic", query_string={"next": bad})
    check(f"外部URL {bad!r} へは飛ばない", res.headers["Location"].endswith("/"), True)
check("未知のUIは 404", c.get("/ui/fancy").status_code, 404)
check("未知のUIでは Cookie を書かない", "gemmba_ui" in c.get("/ui/fancy").headers.get("Set-Cookie", ""), False)

# Cookie を不正な値にされても旧UIで描く
c2 = app.app.test_client()
c2.set_cookie("gemmba_ui", "../../etc")
html = c2.get("/").get_data(as_text=True)
check("不正な Cookie は旧UI扱い", "classic/style.css" in html, True)

# 3・4. 両方のUIで全画面が描ける。旧UIには切り替えの痕跡を出さない
for ui in ("classic", "new"):
    cl = app.app.test_client()
    cl.get(f"/ui/{ui}")
    for page in PAGES:
        res = cl.get(page)
        body = res.get_data(as_text=True)
        check(f"[{ui}] {page} が描ける", res.status_code, 200)
        check(f"[{ui}] {page} は {ui} のCSS", f"{ui}/style.css" in body, True)
        if ui == "classic":
            check(f"[classic] {page} に切り替えの表示が無い", "旧UIに戻す" in body or "ui-switch" in body, False)

# フォーム送信のあとの画面も、そのブラウザのUIのまま
cl = app.app.test_client()
cl.get("/ui/new")
res = cl.post("/tasks/add", data={"title": "UI切替の確認"}, follow_redirects=True)
check("送信後も新UIのまま", "new/style.css" in res.get_data(as_text=True), True)

print("\nすべてOK" if ok else "\nNG があります")
sys.exit(0 if ok else 1)
