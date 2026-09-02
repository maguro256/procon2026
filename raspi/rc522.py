"""
rc522.py - MFRC522 (RC522) でカードのUIDを読む最小ドライバ

ILI9341 と同じSPIバスに CE1 で相乗りする。表示は spidev0.0、RC522 は spidev0.1。

配線:

    RC522   ラズパイ（物理ピン）          備考
    ------  ---------------------------  ------------------------------
    3.3V    3.3V (1番/17番から分岐)      **5Vに繋ぐと壊れる**
    GND     GND  (9番など)
    SDA(SS) GPIO7  / CE1  (26番)         → /dev/spidev0.1
    SCK     GPIO11 / SCLK (23番)         表示と共用
    MOSI    GPIO10 / MOSI (19番)         表示と共用
    MISO    GPIO9  / MISO (21番)         表示は未使用なので直挿し
    RST     GPIO22        (15番)
    IRQ     未接続                        ポーリングで読む

**RC522 は ISO14443A(MIFARE) 専用で FeliCa は読めない。** 社員証にFeliCaを使う場合は
USBリーダー(nfcpy)側が必要。

    python3 rc522.py     # カードをかざすとUIDを表示し続ける
"""
import os
import time

import spidev

# レジスタ
_CommandReg = 0x01
_ComIEnReg = 0x02
_ComIrqReg = 0x04
_DivIrqReg = 0x05
_ErrorReg = 0x06
_FIFODataReg = 0x09
_FIFOLevelReg = 0x0A
_ControlReg = 0x0C
_BitFramingReg = 0x0D
_ModeReg = 0x11
_TxControlReg = 0x14
_TxASKReg = 0x15
_CRCResultRegH = 0x21
_CRCResultRegL = 0x22
_TModeReg = 0x2A
_TPrescalerReg = 0x2B
_TReloadRegH = 0x2C
_TReloadRegL = 0x2D
_VersionReg = 0x37

# PCD(リーダー)へのコマンド
_PCD_IDLE = 0x00
_PCD_CALCCRC = 0x03
_PCD_TRANSCEIVE = 0x0C
_PCD_RESETPHASE = 0x0F

# PICC(カード)へのコマンド
_PICC_REQIDL = 0x26      # REQA
_PICC_ANTICOLL = 0x93    # カスケードレベル1
_PICC_ANTICOLL2 = 0x95   # カスケードレベル2
_PICC_SElECT_CT = 0x88   # カスケードタグ。UIDが7バイト以上のときに先頭に付く


class ReaderUnavailable(RuntimeError):
    """SPIが開けない、またはRC522が応答しないとき"""


class RC522:
    def __init__(self, spi_bus=0, spi_device=1, rst_pin=22, speed_hz=1_000_000):
        dev = f"/dev/spidev{spi_bus}.{spi_device}"
        if not os.path.exists(dev):
            raise ReaderUnavailable(f"{dev} がありません。SPIが有効か確認してください。")

        self._rst = None
        if rst_pin is not None:
            try:
                from gpiozero import DigitalOutputDevice
                self._rst = DigitalOutputDevice(rst_pin, initial_value=True)
                time.sleep(0.05)
            except Exception as e:
                raise ReaderUnavailable(f"RSTピン(GPIO{rst_pin})を掴めません ({e})")

        try:
            self._spi = spidev.SpiDev()
            self._spi.open(spi_bus, spi_device)
            self._spi.max_speed_hz = speed_hz
            self._spi.mode = 0
        except OSError as e:
            raise ReaderUnavailable(f"{dev} を開けません ({e})")

        self.version = self._read(_VersionReg)
        if self.version in (0x00, 0xFF):
            raise ReaderUnavailable(
                "RC522が応答しません（VersionReg=0x%02X）。"
                "SDA/MISO/SCK/MOSI/3.3V/RST の配線を確認してください。" % self.version)
        self._init_chip()

    # ------------------------------------------------------------ 低レベル

    def _read(self, addr):
        return self._spi.xfer2([((addr << 1) & 0x7E) | 0x80, 0x00])[1]

    def _write(self, addr, val):
        self._spi.xfer2([(addr << 1) & 0x7E, val])

    def _set_bits(self, addr, mask):
        self._write(addr, self._read(addr) | mask)

    def _clear_bits(self, addr, mask):
        self._write(addr, self._read(addr) & (~mask & 0xFF))

    def _init_chip(self):
        self._write(_CommandReg, _PCD_RESETPHASE)
        time.sleep(0.05)
        # タイマ設定。応答待ちのタイムアウトを約25msにする
        self._write(_TModeReg, 0x8D)
        self._write(_TPrescalerReg, 0x3E)
        self._write(_TReloadRegL, 30)
        self._write(_TReloadRegH, 0)
        self._write(_TxASKReg, 0x40)     # 100% ASK変調
        self._write(_ModeReg, 0x3D)      # CRCプリセット 0x6363
        self.antenna_on()

    def antenna_on(self):
        if not (self._read(_TxControlReg) & 0x03):
            self._set_bits(_TxControlReg, 0x03)

    def antenna_off(self):
        self._clear_bits(_TxControlReg, 0x03)

    # ------------------------------------------------------------ 通信

    def _transceive(self, send_data):
        """カードへ送って応答を受ける。戻り値: (成功したか, 受信データ, ビット数)"""
        self._write(_ComIEnReg, 0x77 | 0x80)
        self._clear_bits(_ComIrqReg, 0x80)
        self._set_bits(_FIFOLevelReg, 0x80)          # FIFOを空にする
        self._write(_CommandReg, _PCD_IDLE)

        for b in send_data:
            self._write(_FIFODataReg, b)
        self._write(_CommandReg, _PCD_TRANSCEIVE)
        self._set_bits(_BitFramingReg, 0x80)         # 送信開始

        # 応答かタイムアウトまで待つ。25msのタイマが切れると 0x01 が立つ
        for _ in range(2000):
            irq = self._read(_ComIrqReg)
            if irq & 0x30:      # RxIRq / IdleIRq
                break
            if irq & 0x01:      # TimerIRq
                return False, [], 0
        else:
            return False, [], 0

        self._clear_bits(_BitFramingReg, 0x80)

        if self._read(_ErrorReg) & 0x1B:             # Buffer/Parity/Protocol/Collision
            return False, [], 0

        n = self._read(_FIFOLevelReg)
        last_bits = self._read(_ControlReg) & 0x07
        bits = (n - 1) * 8 + last_bits if last_bits else n * 8
        n = max(1, min(n, 16))
        data = [self._read(_FIFODataReg) for _ in range(n)]
        return True, data, bits

    def request(self):
        """カードが場にいるか尋ねる（REQA）。いれば True"""
        self._write(_BitFramingReg, 0x07)            # 最終バイトは7ビットだけ送る
        ok, data, bits = self._transceive([_PICC_REQIDL])
        return ok and bits == 0x10

    def _anticoll(self, cmd):
        """1カスケードレベルぶんの衝突回避。戻り値: 5バイト（UID4+BCC）または None"""
        self._write(_BitFramingReg, 0x00)
        ok, data, _ = self._transceive([cmd, 0x20])
        if not ok or len(data) != 5:
            return None
        check = 0
        for b in data[:4]:
            check ^= b
        return data if check == data[4] else None

    def read_uid(self):
        """
        かざされているカードのUIDを16進文字列で返す。無ければ None。
        4バイトUIDと7バイトUID（カスケード）の両方に対応する。
        """
        if not self.request():
            return None
        first = self._anticoll(_PICC_ANTICOLL)
        if first is None:
            return None
        if first[0] != _PICC_SElECT_CT:
            return bytes(first[:4]).hex()

        # 先頭がカスケードタグ = UIDは7バイト。残りを次のレベルで取る
        second = self._anticoll(_PICC_ANTICOLL2)
        if second is None:
            return None
        return bytes(first[1:4] + second[:4]).hex()

    def close(self):
        try:
            self.antenna_off()
            self._spi.close()
        except Exception:
            pass
        if self._rst is not None:
            try:
                self._rst.close()
            except Exception:
                pass


def open_reader():
    def _pin(name, default):
        raw = os.environ.get(name, str(default)).strip().lower()
        return None if raw in ("", "none", "off", "-1") else int(raw)

    return RC522(
        spi_bus=int(os.environ.get("GEMMBA_RC522_SPI_BUS", "0")),
        spi_device=int(os.environ.get("GEMMBA_RC522_SPI_DEV", "1")),
        rst_pin=_pin("GEMMBA_RC522_RST", 22),
        speed_hz=int(os.environ.get("GEMMBA_RC522_SPEED", "1000000")),
    )


def probe():
    """カードをかざすとUIDを表示し続ける。配線と読み取りの確認用"""
    try:
        reader = open_reader()
    except ReaderUnavailable as e:
        print(f"[RC522] {e}")
        return 1

    print(f"[RC522] 初期化しました (VersionReg=0x{reader.version:02X})")
    print("[RC522] カードをかざしてください（Ctrl-C で終了）")
    last, last_at = None, 0.0
    try:
        while True:
            uid = reader.read_uid()
            now = time.monotonic()
            if uid and (uid != last or now - last_at > 1.0):
                print(f"  UID: {uid}  ({len(uid)//2}バイト)")
                last, last_at = uid, now
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[RC522] 終了します。")
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())
