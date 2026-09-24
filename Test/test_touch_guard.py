"""
連打ガードの確認。_handle_touch を差し替えて、MQTT もラズパイも無しで
_dispatch_touch の受付/無視だけを見る。

    python Test/test_touch_guard.py
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


handled = []
_release = threading.Event()


def fake_handle_touch(device_id, module_id, tag_id):
    """1回の操作の代わり。_release が立つまで「操作中」でいる"""
    handled.append(tag_id)
    _release.wait(10)


app._handle_touch = fake_handle_touch


def wait_until(cond, timeout=5):
    limit = time.time() + timeout
    while time.time() < limit:
        if cond():
            return True
        time.sleep(0.02)
    return False


def check(label, ok):
    print(f"{'OK  ' if ok else 'NG  '} {label}")
    return ok


results = []

# 1. 連打しても最初の1回しか通らない
for i in range(5):
    app._dispatch_touch("pi-test", "MOD-TEST", f"TAG-{i}")
results.append(check("連打5回のうち処理されたのは1回",
                     wait_until(lambda: len(handled) == 1) and len(handled) == 1))
time.sleep(0.3)
results.append(check("待っても2回目は流れてこない", len(handled) == 1))

# 2. 操作が終われば次のタッチを受け付ける
_release.set()
results.append(check("操作の終了でガードが外れる",
                     wait_until(lambda: "pi-test" not in app._touch_busy)))
app._dispatch_touch("pi-test", "MOD-TEST", "TAG-next")
results.append(check("終了後のタッチは処理される",
                     wait_until(lambda: handled[-1:] == ["TAG-next"])))

# 3. 別のモジュールは巻き添えにならない
_release.clear()
handled.clear()
app._dispatch_touch("pi-a", "MOD-A", "TAG-A")
app._dispatch_touch("pi-b", "MOD-B", "TAG-B")
results.append(check("モジュールごとに独立して受け付ける",
                     wait_until(lambda: sorted(handled) == ["TAG-A", "TAG-B"])))

_release.set()

# 4. 操作が例外で落ちても、その機材が二度と反応しなくならない
handled.clear()


def boom(device_id, module_id, tag_id):
    handled.append(tag_id)
    raise RuntimeError("操作中の想定外エラー")


app._handle_touch = boom
app._dispatch_touch("pi-err", "MOD-ERR", "TAG-err")
results.append(check("例外でもガードが外れる",
                     wait_until(lambda: "pi-err" not in app._touch_busy)))
app._handle_touch = fake_handle_touch
app._dispatch_touch("pi-err", "MOD-ERR", "TAG-after-err")
results.append(check("例外の次のタッチは処理される",
                     wait_until(lambda: handled[-1:] == ["TAG-after-err"])))

print("\n" + ("全て通りました" if all(results) else "失敗があります"))
sys.exit(0 if all(results) else 1)
