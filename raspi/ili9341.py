"""
ili9341.py - 2.8インチ SPI TFT (ILI9341 / 240x320) の最小ドライバ

luma.lcd や adafruit-circuitpython-rgb-display を使わず、標準で入っている
spidev + PIL + numpy + gpiozero だけで書いている。Debian 13 (trixie) の pip は
externally-managed で追加インストールが面倒なため、依存を増やさない方針。

実機の配線（この既定値。環境変数で変えられる）:

    LCD側   ラズパイ側                       環境変数
    ------  -------------------------------  ----------------------
    VCC     3.3V          (1番ピン)
    GND     GND           (6番ピン)
    CS      GPIO8  / CE0  (24番ピン)         GEMMBA_LCD_SPI_DEV=0
    RESET   GPIO25        (22番ピン)         GEMMBA_LCD_RST=25
    DC/RS   GPIO24        (18番ピン)         GEMMBA_LCD_DC=24
    SDI     GPIO10 / MOSI (19番ピン)
    SCK     GPIO11 / SCLK (23番ピン)
    LED     3.3V          (17番ピン)         GEMMBA_LCD_BL=none
    SDO     GPIO9  / MISO (21番ピン) ※表示だけなら未接続でよい

    LED を 3.3V へ直結しているのでバックライトは常時点灯。GPIO からは触らない。
    ネット上の作例は DC=25 / RESET=24 が多いので、参考にするときは注意すること。

事前に SPI を有効化しておくこと（/dev/spidev0.0 が出来る）:

    sudo raspi-config nonint do_spi 0

タッチパネル(XPT2046)はこのドライバでは扱わない。B-2 で別途。
"""
import os
import time

import numpy as np
import spidev
from PIL import Image

# コマンド
_SWRESET = 0x01
_SLPOUT = 0x11
_DISPON = 0x29
_CASET = 0x2A
_PASET = 0x2B
_RAMWR = 0x2C
_MADCTL = 0x36
_COLMOD = 0x3A

# MADCTL のビット
_MY, _MX, _MV, _BGR = 0x80, 0x40, 0x20, 0x08

# 回転角 -> MADCTL。90/270 では幅と高さが入れ替わる
_ROTATION = {
    0:   _MX | _BGR,
    90:  _MV | _BGR,
    180: _MY | _BGR,
    270: _MY | _MX | _MV | _BGR,
}

# 電源・ガンマまわり。データシートの推奨値で、ほぼ全てのモジュールで通る
_INIT = [
    (0xEF, b"\x03\x80\x02"),
    (0xCF, b"\x00\xC1\x30"),
    (0xED, b"\x64\x03\x12\x81"),
    (0xE8, b"\x85\x00\x78"),
    (0xCB, b"\x39\x2C\x00\x34\x02"),
    (0xF7, b"\x20"),
    (0xEA, b"\x00\x00"),
    (0xC0, b"\x23"),          # 電源制御1
    (0xC1, b"\x10"),          # 電源制御2
    (0xC5, b"\x3E\x28"),      # VCOM制御1
    (0xC7, b"\x86"),          # VCOM制御2
    (_COLMOD, b"\x55"),       # 16bit/pixel (RGB565)
    (0xB1, b"\x00\x18"),      # フレームレート
    (0xB6, b"\x08\x82\x27"),  # 表示機能制御
    (0xF2, b"\x00"),          # 3Gamma無効
    (0x26, b"\x01"),          # ガンマカーブ選択
    (0xE0, b"\x0F\x31\x2B\x0C\x0E\x08\x4E\xF1\x37\x07\x10\x03\x0E\x09\x00"),
    (0xE1, b"\x00\x0E\x14\x03\x11\x07\x31\xC1\x48\x08\x0F\x0C\x31\x36\x0F"),
]


class DisplayUnavailable(RuntimeError):
    """SPI未有効やGPIOを掴めない等、ディスプレイが使えないとき"""


class ILI9341:
    def __init__(self, spi_bus=0, spi_device=0, dc=24, rst=25, backlight=None,
                 speed_hz=8_000_000, rotation=90):
        if rotation not in _ROTATION:
            raise ValueError(f"rotation は {sorted(_ROTATION)} のいずれか")
        self.rotation = rotation
        self.width, self.height = (320, 240) if rotation in (90, 270) else (240, 320)

        dev = f"/dev/spidev{spi_bus}.{spi_device}"
        if not os.path.exists(dev):
            raise DisplayUnavailable(
                f"{dev} がありません。SPIが無効です。"
                "`sudo raspi-config nonint do_spi 0` で有効化してください。")

        # gpiozero は import 時にピンファクトリを探すので、ここまで遅らせる
        try:
            from gpiozero import DigitalOutputDevice
        except ImportError as e:  # pragma: no cover
            raise DisplayUnavailable(f"gpiozero がありません: {e}")

        try:
            self._dc = DigitalOutputDevice(dc)
            self._rst = DigitalOutputDevice(rst) if rst is not None else None
            self._bl = DigitalOutputDevice(backlight) if backlight is not None else None
        except Exception as e:
            raise DisplayUnavailable(f"GPIOを掴めません ({e})")

        try:
            self._spi = spidev.SpiDev()
            self._spi.open(spi_bus, spi_device)
            self._spi.max_speed_hz = speed_hz
            self._spi.mode = 0
        except OSError as e:
            raise DisplayUnavailable(f"{dev} を開けません ({e})")

        self._reset()
        self._init_panel()
        if self._bl:
            self._bl.on()

    # ------------------------------------------------------------ 低レベル

    def _write(self, is_data, buf):
        self._dc.value = 1 if is_data else 0
        # spidev の1回の転送はカーネルの bufsiz(既定4096)までだが、
        # writebytes2 が自動で分割してくれる
        self._spi.writebytes2(buf)

    def _cmd(self, cmd, data=b""):
        self._write(False, bytes([cmd]))
        if data:
            self._write(True, data)

    def _reset(self):
        if self._rst:
            self._rst.on()
            time.sleep(0.005)
            self._rst.off()
            time.sleep(0.02)
            self._rst.on()
            time.sleep(0.15)
        else:
            self._cmd(_SWRESET)
            time.sleep(0.15)

    def _init_panel(self):
        for cmd, data in _INIT:
            self._cmd(cmd, data)
        self._cmd(_MADCTL, bytes([_ROTATION[self.rotation]]))
        self._cmd(_SLPOUT)
        time.sleep(0.12)
        self._cmd(_DISPON)
        time.sleep(0.02)

    def _set_window(self, x0, y0, x1, y1):
        self._cmd(_CASET, bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        self._cmd(_PASET, bytes([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))
        self._cmd(_RAMWR)

    # ------------------------------------------------------------ 描画

    @staticmethod
    def _to_rgb565(image):
        """PIL Image -> RGB565 ビッグエンディアンのバイト列"""
        arr = np.asarray(image.convert("RGB"), dtype=np.uint16)
        rgb = ((arr[:, :, 0] >> 3) << 11) | ((arr[:, :, 1] >> 2) << 5) | (arr[:, :, 2] >> 3)
        return rgb.astype(">u2").tobytes()

    def display(self, image):
        """画面全体を書き換える。image は self.width x self.height"""
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        self._set_window(0, 0, self.width - 1, self.height - 1)
        self._write(True, self._to_rgb565(image))

    def fill(self, color=(0, 0, 0)):
        self.display(Image.new("RGB", (self.width, self.height), color))

    def backlight(self, on):
        if self._bl:
            self._bl.value = 1 if on else 0

    def close(self):
        try:
            self._spi.close()
        except Exception:
            pass
        for pin in (self._dc, self._rst, self._bl):
            if pin is not None:
                try:
                    pin.close()
                except Exception:
                    pass


def open_display():
    """環境変数を見て1枚開く。使えないときは DisplayUnavailable を投げる"""
    def _pin(name, default):
        raw = os.environ.get(name, str(default)).strip().lower()
        return None if raw in ("", "none", "off", "-1") else int(raw)

    return ILI9341(
        spi_bus=int(os.environ.get("GEMMBA_LCD_SPI_BUS", "0")),
        spi_device=int(os.environ.get("GEMMBA_LCD_SPI_DEV", "0")),
        dc=_pin("GEMMBA_LCD_DC", 24),
        rst=_pin("GEMMBA_LCD_RST", 25),
        backlight=_pin("GEMMBA_LCD_BL", "none"),  # LEDは3.3V直結なのでGPIO制御しない
        # 8MHz。ILI9341自体は32MHzでも動くが、この実機のジャンパ配線では
        # 32MHzだと初期化コマンドが通らず画面が白いままになった。全面書き換えは
        # 8MHzでも0.15秒程度で、この用途には十分速い。
        speed_hz=int(os.environ.get("GEMMBA_LCD_SPEED", "8000000")),
        # 270 = 90 の上下反転。筐体への取り付け向きに合わせて 2026-10-07 に 90 から変えた
        rotation=int(os.environ.get("GEMMBA_LCD_ROTATION", "270")),
    )
