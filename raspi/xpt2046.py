"""
xpt2046.py - 抵抗膜タッチパネル(XPT2046)の読み取り

ILI9341 と同じSPIバスに CE1 で相乗りする。表示は spidev0.0、タッチは spidev0.1。
CS が別なので互いに干渉しない。

配線（ディスプレイの4本に追加する分）:

    LCD側   ラズパイ側                        備考
    ------  ------------------------------   ---------------------------
    T_CS    GPIO7  / CE1  (26番ピン)          これで spidev0.1 になる
    T_CLK   GPIO11 / SCLK (23番ピン)          表示と共用（同じ線でよい）
    T_DIN   GPIO10 / MOSI (19番ピン)          表示と共用（同じ線でよい）
    T_DO    GPIO9  / MISO (21番ピン)          ★表示では未接続。タッチには必須
    T_IRQ   未接続でよい                      ポーリングで読むため

**T_CLK は表示より遅くする必要がある。** XPT2046 は 2MHz 程度が上限で、
表示の 8MHz を流用すると値が化ける。spidev0.1 側で別に設定している。

生値の確認:

    python3 xpt2046.py        # 触っている間、生の値を出し続ける
"""
import os
import time

import spidev

# 制御バイト。S=1, 12bit, differential(SER/DFR=0), PD=00(常時オン)
_CMD_X = 0xD0   # X位置
_CMD_Y = 0x90   # Y位置
_CMD_Z1 = 0xB0  # 圧力測定用
_CMD_Z2 = 0xC0

# 触れていないときの z は 0 付近。実機で xpt2046.py を流して決めること。
TOUCH_THRESHOLD = int(os.environ.get("GEMMBA_TOUCH_THRESHOLD", "120"))


class TouchUnavailable(RuntimeError):
    """SPIが無効、または spidev0.1 を開けないとき"""


class XPT2046:
    def __init__(self, spi_bus=0, spi_device=1, speed_hz=1_000_000,
                 threshold=TOUCH_THRESHOLD):
        dev = f"/dev/spidev{spi_bus}.{spi_device}"
        if not os.path.exists(dev):
            raise TouchUnavailable(f"{dev} がありません。SPIが有効か確認してください。")
        try:
            self._spi = spidev.SpiDev()
            self._spi.open(spi_bus, spi_device)
            self._spi.max_speed_hz = speed_hz   # 表示(8MHz)より遅くすること
            self._spi.mode = 0
        except OSError as e:
            raise TouchUnavailable(f"{dev} を開けません ({e})")
        self.threshold = threshold

    def _read(self, cmd, samples=5):
        """1チャンネルを複数回読んで中央値を返す。抵抗膜は値が暴れる"""
        vals = []
        for _ in range(samples):
            r = self._spi.xfer2([cmd, 0x00, 0x00])
            vals.append(((r[1] << 8) | r[2]) >> 3)   # 12bit
        vals.sort()
        return vals[len(vals) // 2]

    def read_raw(self):
        """(x, y, z1, z2) の生値。診断のため z1/z2 は分けて返す"""
        return (self._read(_CMD_X), self._read(_CMD_Y),
                self._read(_CMD_Z1), self._read(_CMD_Z2))

    @staticmethod
    def pressure(z1, z2):
        """押し込みの強さ。触れていなければ 0 付近"""
        return z1 + (4095 - z2) if (z1 or z2) else 0

    def read_touch(self):
        """触れていれば (x, y) の生値、触れていなければ None"""
        x, y, z1, z2 = self.read_raw()
        if self.pressure(z1, z2) < self.threshold:
            return None
        # 全ビット0（配線不良）や、端で飽和した値を触れたと誤認しないようにする
        if not (100 < x < 4000 and 100 < y < 4000):
            return None
        return x, y

    def close(self):
        try:
            self._spi.close()
        except Exception:
            pass


def open_touch():
    return XPT2046(
        spi_bus=int(os.environ.get("GEMMBA_TOUCH_SPI_BUS", "0")),
        spi_device=int(os.environ.get("GEMMBA_TOUCH_SPI_DEV", "1")),
        speed_hz=int(os.environ.get("GEMMBA_TOUCH_SPEED", "1000000")),
    )


def probe():
    """
    配線確認用。生の値を出し続ける。

      - 触れていないとき z がほぼ 0 で落ち着いていれば配線OK
      - 触れると z が跳ね上がり、x/y が指の位置で変わる
      - ずっと同じ値が出る / z が常に大きい → T_DO(MISO) の未接続を疑う
    """
    try:
        touch = open_touch()
    except TouchUnavailable as e:
        print(f"[touch] 開けません: {e}")
        return 1

    print("[touch] 画面を触ってください（Ctrl-C で終了）")
    print("        触れていないとき z がほぼ0、触れると跳ね上がれば配線OK")
    all_zero = True
    try:
        while True:
            x, y, z1, z2 = touch.read_raw()
            z = touch.pressure(z1, z2)
            if x or y or z1 or z2:
                all_zero = False
            touched = z >= touch.threshold and 100 < x < 4000 and 100 < y < 4000
            print(f"  x={x:4d}  y={y:4d}  z1={z1:4d}  z2={z2:4d}  z={z:5d}"
                  + ("  ← 触れている" if touched else ""))
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        touch.close()
    if all_zero:
        print("\n[touch] すべて0でした。SPIから1ビットも返ってきていません。")
        print("        ・T_DO を 21番ピン(MISO) に挿しているか")
        print("        ・T_CS を 26番ピン(CE1) に挿しているか（24番のCE0ではない）")
        print("        ・パネルのVCC/GNDが来ているか")
    else:
        print("\n[touch] 終了します。")
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())
