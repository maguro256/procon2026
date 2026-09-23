"""
checkall.py - 繋いである機器をまとめて検査する

    python3 checkall.py [秒数]        # 既定20秒

試作機を1台ずつ組み上げていくときに使う。**まだ繋いでいない機器は自動で
「未接続」と判定して飛ばす**ので、途中段階でもそのまま実行できる。

1つの窓の中で LCD 表示・録音・NFCスキャン・LED点灯・ボタン監視を**同時に**走らせる。
順番に試すより速いだけでなく、**機器どうしの干渉も一緒に見られる**（LCD と RC522 は
SPI を共有し、マイクは GPIO18/19/20 を占有するため、単体で動いても同時だと
落ちることがある）。

目視でしか判定できないもの（LCDに何が映ったか・LEDの色）は自動で○×を付けない。
画面とLEDを見ておくこと。判定できるものは最後に表で出す。

    python3 checkall.py --no-lcd      # LCDを開かない（他を試すとき）
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIC_DEVICE = os.environ.get("GEMMBA_MIC", "plughw:CARD=sndrpigooglevoi,DEV=0")
REC_PATH = "/tmp/checkall_rec.wav"

# 判定のしきい値。TODO.md の E-2 の実測に合わせてある
MIC_NOISE_FLOOR_MAX = -55.0   # これより雑音が大きければ何かおかしい
MIC_ACOUSTIC_MIN_DB = 12.0    # 音に反応していると言える最小の振れ幅

results = []      # (機器名, 判定, 詳細)
lock = threading.Lock()


def report(name, verdict, detail):
    with lock:
        results.append((name, verdict, detail))


# ---------------------------------------------------------------- LCD
def check_lcd(sec):
    """
    確認画面を出して保持する。**SPIは書きっぱなしで応答を読まないので、
    ここで分かるのは「開けたか」までで、映ったかどうかは目視でしか分からない。**
    """
    try:
        import raspi
    except Exception as e:
        report("LCD", "NG", "raspi.py を import できません: %s" % e)
        return
    lcd = raspi.init_display()
    if lcd is None:
        report("LCD", "NG", "開けません（上のメッセージ参照）")
        return
    try:
        lcd.display(raspi._compose_selftest((lcd.width, lcd.height), lcd.rotation))
    except Exception as e:
        report("LCD", "NG", "描画で失敗: %s" % e)
        return
    report("LCD", "要目視", "%dx%d rotation=%d で描画した。4隅・色帯・日本語を見ること"
           % (lcd.width, lcd.height, lcd.rotation))
    time.sleep(sec)   # プロセスが終わると消えるので保持する


# ---------------------------------------------------------------- マイク
def check_mic(sec):
    try:
        import numpy as np
    except ImportError as e:
        report("マイク", "NG", "numpy がありません: %s" % e)
        return

    dur = max(3, min(int(sec), 30))
    try:
        rc = subprocess.call(["arecord", "-D", MIC_DEVICE, "-c", "2", "-r", "16000",
                              "-f", "S32_LE", "-d", str(dur), "-q", REC_PATH])
    except OSError as e:
        report("マイク", "未接続?", "arecord を起動できません: %s" % e)
        return
    if rc != 0:
        report("マイク", "NG", "arecord が失敗しました（カードが見えていない可能性）")
        return

    import wave
    w = wave.open(REC_PATH)
    sr, ch = w.getframerate(), w.getnchannels()
    raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i4").reshape(-1, ch)

    # 鳴っている側を選ぶ（L/R を GND に落としてあるので普通は ch0）
    live = max(range(ch), key=lambda c: int(np.count_nonzero(raw[:, c])))
    col = raw[:, live]
    if not np.count_nonzero(col):
        report("マイク", "NG", "全サンプルが0。SD(38番)かVDD/GNDの配線を疑う")
        return

    x = col / 2.0 ** 31
    X = np.fft.rfft(x)
    X[np.fft.rfftfreq(len(x), 1.0 / sr) < 80] = 0      # 起動直後のDCドリフトを落とす
    y = np.fft.irfft(X, len(x))[int(0.5 * sr):]
    blocks = np.array([np.sqrt(np.mean(b ** 2))
                       for b in np.array_split(y, max(1, len(y) // (sr // 10)))])
    db = lambda v: 20 * np.log10(max(float(v), 1e-12))
    floor, loud, peak = db(np.percentile(blocks, 10)), db(np.percentile(blocks, 95)), \
        db(np.max(np.abs(y)))
    detail = "ch%d / 雑音 %.1f / 大きい所 %.1f / 差 %.1f dB / ピーク %.1f dBFS" % (
        live, floor, loud, floor and (loud - floor), peak)

    if floor > MIC_NOISE_FLOOR_MAX:
        report("マイク", "NG", "雑音が大きすぎます。" + detail)
    elif (loud - floor) < MIC_ACOUSTIC_MIN_DB:
        # **「話しかけていない」と「音が届いていない」をソフトで区別できない。**
        # 前者で NG を出すと狼少年になるので、断定せず切り分け方だけ示す。
        # 音穴の詰まりで「電気的には正常なのに音だけ入らない」を実際に踏んでいる
        report("マイク", "音の入力なし",
               "配線は正常（データが流れ、雑音フロアも正常値）。窓の間に音が"
               "入らなかっただけなら問題ない。話しかけてもこれなら音穴の詰まり・"
               "距離・声量を疑う。" + detail)
    else:
        report("マイク", "OK", detail)


# ---------------------------------------------------------------- RC522
def check_rc522(sec):
    try:
        import rc522
    except Exception as e:
        report("RC522", "NG", "rc522.py を import できません: %s" % e)
        return
    try:
        r = rc522.open_reader()
    except Exception as e:
        report("RC522", "未接続?", str(e))
        return

    try:
        # バージョンだけでなく書き戻しまで見る。**0xC0 を正常と誤診した反省**
        versions = [r._read(0x37) for _ in range(5)]
        back = sum(1 for v in (0x3D, 0xA5, 0x00, 0xFF, 0x5A)
                   if (r._write(0x2C, v), r._read(0x2C))[1] == v)
        antenna = r._read(0x14)
        head = "VersionReg=0x%02X(x5:%s) 書戻し %d/5 TxControl=0x%02X" % (
            versions[0], "同一" if len(set(versions)) == 1 else "ばらつく", back, antenna)
        if back < 5 or len(set(versions)) != 1:
            report("RC522", "NG", "レジスタが安定しません。" + head)
            return
        if not (antenna & 0x03):
            report("RC522", "NG", "アンテナがOFFです。" + head)
            return

        # 窓の残り時間はタグを探す。**取得率ではなくパターンで見る**
        # （カードを動かしている時間が分母に入るので、割合は当てにならない）
        marks, seen = [], {}
        end = time.time() + max(3, sec - 2)
        while time.time() < end:
            uid = r.read_uid()
            marks.append("#" if uid else ".")
            if uid:
                seen[uid] = seen.get(uid, 0) + 1
            time.sleep(0.1)
        s = "".join(marks)
        if not seen:
            report("RC522", "OK(タグ未検出)", head + " / タグはかざされませんでした")
        else:
            # 連続して載っている区間での交互パターン（#.#.#）が出ていれば結合は良好
            best = 0
            for i in range(len(s) - 6):
                if s[i:i + 7] in ("#.#.#.#", ".#.#.#."):
                    best += 1
            uids = " ".join("%s(%d回)" % (u, n) for u, n in seen.items())
            report("RC522", "OK", "%s / %s / 交互パターン %s" % (
                head, uids, "あり=結合良好" if best else "なし=位置を追い込む余地あり"))
    finally:
        try:
            r.close()
        except Exception:
            pass


# ---------------------------------------------------------------- LED
def check_leds(sec):
    try:
        import raspi
    except Exception as e:
        report("LED", "NG", "raspi.py を import できません: %s" % e)
        return
    if not raspi.init_leds():
        report("LED", "未接続?", "GPIOを開けません")
        return
    report("LED", "要目視", "赤(GPIO%d)→青(GPIO%d)を交互に点けます。色が逆でないか見ること"
           % (raspi.LED_PINS["red"], raspi.LED_PINS["blue"]))
    end = time.time() + sec
    try:
        while time.time() < end:
            for name in ("red", "blue"):
                for other, led in raspi._leds.items():
                    led.value = (other == name)
                time.sleep(1.5)
                if time.time() >= end:
                    break
    finally:
        for led in raspi._leds.values():
            led.off()


# ---------------------------------------------------------------- ボタン
def check_buttons(sec):
    try:
        import raspi
    except Exception as e:
        report("ボタン", "NG", "raspi.py を import できません: %s" % e)
        return
    if not raspi.init_buttons():
        report("ボタン", "未接続?", "GPIOを開けません")
        return
    # キューを空にしてから数える
    while not raspi._button_events.empty():
        raspi._button_events.get_nowait()

    pressed = {}
    end = time.time() + sec
    while time.time() < end:
        try:
            name = raspi._button_events.get(timeout=0.2)
        except Exception:
            continue
        pressed[name] = pressed.get(name, 0) + 1

    labels = {"left": "左", "ok": "決定", "right": "右"}
    if not pressed:
        report("ボタン", "押されず", "GPIO%s は開けました。窓の間に押されませんでした"
               % list(raspi.BUTTON_PINS.values()))
    else:
        got = " ".join("%s=%d回" % (labels.get(k, k), v) for k, v in pressed.items())
        missing = [labels[k] for k in raspi.BUTTON_PINS if k not in pressed]
        report("ボタン", "OK" if not missing else "一部のみ",
               got + ("" if not missing else " / 押されていない: " + "・".join(missing)))


# ---------------------------------------------------------------- 本体
def main(argv):
    sec = 20
    for a in argv:
        if a.isdigit():
            sec = int(a)
    checks = [("マイク", check_mic), ("RC522", check_rc522),
              ("LED", check_leds), ("ボタン", check_buttons)]
    if "--no-lcd" not in argv:
        checks.insert(0, ("LCD", check_lcd))

    print("=== %d秒間、繋いである機器を同時に検査します ===" % sec)
    print("この間に: LCDの画面を見る / マイクに話しかける / タグをかざす / "
          "LEDの色を見る / ボタンを3つとも押す")
    print()

    threads = [threading.Thread(target=fn, args=(sec,), daemon=True) for _, fn in checks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(sec + 30)

    print()
    print("=" * 72)
    print("%-8s %-12s %s" % ("機器", "判定", "詳細"))
    print("-" * 72)
    order = {name: i for i, (name, _) in enumerate(checks)}
    for name, verdict, detail in sorted(results, key=lambda r: order.get(r[0], 99)):
        print("%-8s %-12s %s" % (name, verdict, detail))
    print("=" * 72)
    print("※ LCD と LED は目視でしか判定できません（SPIもGPIOも書きっぱなしで"
          "応答を読まないため、「開けた」は「映った・光った」の証明になりません）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
