"""RC522 が「壊れている」のか「誰も答えない」のかを分ける診断。

9/16 に記録した良好時の値と比べる:
    VersionReg=0x82 / TxControlReg=0x83(アンテナON) / REQA の ErrorReg=0x00
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc522 as R

try:
    r = R.open_reader()
except R.ReaderUnavailable as e:
    print(f"[NG] リーダーを開けません: {e}")
    sys.exit(1)

def rd(a):
    return r._read(a)

print("=== 1. レジスタの素性 ===")
print(f"  VersionReg    = 0x{r.version:02X}   (期待 0x82 = FM17522系クローン)")
print(f"  CommandReg    = 0x{rd(R._CommandReg):02X}   (期待 0x20)")
tx = rd(R._TxControlReg)
print(f"  TxControlReg  = 0x{tx:02X}   (bit0/1 がアンテナ。0x83 ならON)")
if not (tx & 0x03):
    print("  → アンテナがOFFです。明示的にONにします。")
    r.antenna_on()
    tx = rd(R._TxControlReg)
    print(f"  TxControlReg  = 0x{tx:02X} (antenna_on 後)")

print()
print("=== 2. 書いて読み返せるか(SPIの往復) ===")
TReloadL = 0x2C
orig = rd(TReloadL)
ok = 0
for v in (0x00, 0x55, 0xAA, 0xFF, 0x3C):
    r._write(TReloadL, v)
    back = rd(TReloadL)
    mark = "OK" if back == v else "NG"
    if back == v: ok += 1
    print(f"  書き 0x{v:02X} → 読み 0x{back:02X}  {mark}")
r._write(TReloadL, orig)
print(f"  → {ok}/5 一致")

print()
print("=== 3. REQA を40回(約20秒)。カードを当てたままにしてください ===")
det = 0
errs = {}
pattern = []
for i in range(40):
    try:
        found = r.request()
    except Exception as e:
        found = False
    err = rd(R._ErrorReg)
    errs[err] = errs.get(err, 0) + 1
    if found:
        det += 1
        pattern.append("#")
    else:
        pattern.append(".")
    time.sleep(0.5)

print("  " + "".join(pattern))
print(f"  検出 {det}/40")
print("  ErrorReg の分布: " + ", ".join(f"0x{k:02X}×{v}" for k, v in sorted(errs.items())))

print()
print("=== 4. UID の読み出し ===")
uid = None
for _ in range(10):
    uid = r.read_uid()
    if uid:
        break
    time.sleep(0.3)
print(f"  UID = {uid if uid else '読めず'}")

r.close()

print()
print("=== 読み方 ===")
print("  検出0 かつ ErrorReg が全部 0x00  → 壊れてはいない。「誰も答えなかった」")
print("      = カードが届いていない(位置/距離/画面越し)か、カードがFeliCaで読めない")
print("  ErrorReg に 0x00 以外が混じる     → 結合はしているが通信が崩れている")
print("  書き戻しが 5/5 未満              → SPIの配線が怪しい")
