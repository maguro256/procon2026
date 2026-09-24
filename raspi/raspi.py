import paho.mqtt.client as mqtt
import json
import os
import queue
import socket
import sys
import threading
import time
import uuid

# カードリーダーは RC522 (MFRC522) に一本化した。ドライバは raspi/rc522.py。
# import は touch_loop() の中で行う。リーダーの無いPCでも sim.py が動くようにするため。
#
# 注意: RC522 は ISO14443A(MIFARE) 専用で **FeliCa は読めない**。
# FeliCa の社員証を使う必要が出たら、USBリーダー(nfcpy)を併用する構成に戻すこと。

# 機材との対応付けに使う名前。管理画面の「モジュールID」と一致させること。
# モジュールの同一性はこのIDで決まるので、IPが変わっても影響しない。
DEVICE_ID = os.environ.get("GEMMBA_DEVICE_ID", "pi01")

# 起動ごとに変わる識別子。同じ device_id でも「再起動した」ことが分かる
SESSION_ID = uuid.uuid4().hex[:8]

# ブローカーはUDPブロードキャストで自動探索する。環境変数を指定した場合はそちらを優先。
#   GEMMBA_BROKER_HOST=192.168.0.114 python raspi.py
BROKER_HOST_ENV = os.environ.get("GEMMBA_BROKER_HOST")
BROKER_PORT_ENV = int(os.environ.get("GEMMBA_BROKER_PORT", "1883"))
DISCOVERY_PORT = int(os.environ.get("GEMMBA_DISCOVERY_PORT", "50505"))
DISCOVERY_REQUEST = b"GEMMBA_DISCOVER_V1"
DISCOVERY_TIMEOUT = 3.0

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".broker_cache.json")
STATUS_TOPIC = f"pi/{DEVICE_ID}/status"
DATA_TOPIC = f"pi/{DEVICE_ID}/data"
CMD_TOPIC = f"pi/{DEVICE_ID}/cmd"      # サーバーからの指示（下り）
REPLY_TOPIC = f"pi/{DEVICE_ID}/reply"  # 指示への応答
KEEPALIVE = 30          # この2倍ほど無応答だとブローカーがLWTを配信する
HEARTBEAT_SEC = 30
RETRY_SEC = 5
# 同じ失敗が延々と続くときに、この回数に1回だけ出す。PCが落ちている間ずっと
# 再探索を繰り返すので、間引かないとログがSDカードを埋める（実測: 9日で9.5MB）
LOG_REPEAT_EVERY = 60

# この秒数だけカードが見えなければ「離れた」とみなす。RC522 は磁界の揺らぎで
# 一時的に読めないことがあるので、途切れてすぐ離脱と判断しない
TOUCH_RELEASE_SEC = float(os.environ.get("GEMMBA_TOUCH_RELEASE", "1.0"))
TOUCH_POLL_SEC = float(os.environ.get("GEMMBA_TOUCH_POLL", "0.1"))

# 選択の入力元。既定はキーボードで、物理ボタン(B-2)が付いたら buttons にする。
#   buttons … 左/右/決定 の3ボタン。開けなければ console に落ちる
#   console … キーボード（既定）。番号、2択なら y / n
#   yes / no / timeout … 自動応答。試験用
INPUT_MODE = os.environ.get("GEMMBA_INPUT", "console")


# ------------------------------------------------------------ ログ

# 再接続まわりは「いつ落ちて、いつ戻ったか」が分からないと後から追えないので
# 時刻を付ける。表示のコンソール代替出力は素の print のままにしてある。
_repeat = {"key": None, "count": 0}


def log(msg, key=None):
    """
    時刻付きで1行出す。key を渡すと、同じ key が続く間は LOG_REPEAT_EVERY 回に
    1回へ間引く（PC不在時の再試行ログが無限に伸びるのを防ぐ）。
    """
    stamp = time.strftime("%m-%d %H:%M:%S")
    if key is None:
        _repeat["key"], _repeat["count"] = None, 0
        print(f"[{stamp}] {msg}")
        return
    if key != _repeat["key"]:
        _repeat["key"], _repeat["count"] = key, 1
        print(f"[{stamp}] {msg}")
        return
    _repeat["count"] += 1
    if _repeat["count"] % LOG_REPEAT_EVERY == 0:
        print(f"[{stamp}] {msg}（同じ状態が {_repeat['count']} 回続いています）")


# ------------------------------------------------------------ ブローカー探索

def discover_broker():
    """LANへブロードキャストし、app.py から自分のIPを教えてもらう"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(DISCOVERY_TIMEOUT)
    try:
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.time() + DISCOVERY_TIMEOUT
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if info.get("host"):
                return info["host"], int(info.get("port", 1883))
    except OSError as e:
        log(f"[discovery] ブロードキャストに失敗: {e}", key=f"bcast:{e}")
    finally:
        sock.close()
    return None


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            info = json.load(f)
        return info["host"], int(info.get("port", 1883))
    except (OSError, ValueError, KeyError):
        return None


def save_cache(host, port):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": port}, f)
    except OSError as e:
        log(f"[discovery] キャッシュ保存に失敗: {e}")


def resolve_broker():
    """環境変数 → 自動探索 → 前回成功したIP の順で候補を決める"""
    if BROKER_HOST_ENV:
        return BROKER_HOST_ENV, BROKER_PORT_ENV
    found = discover_broker()
    if found:
        return found
    cached = load_cache()
    if cached:
        log(f"[discovery] 応答なし。前回のブローカー {cached[0]}:{cached[1]} を試します。",
            key=f"cache:{cached[0]}:{cached[1]}")
        return cached
    return None


def local_ip_toward(host):
    """ブローカーから見た自分のIP。管理画面に表示するだけで、識別には使わない"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


# ------------------------------------------------------------ MQTT

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
# 電源断やネット切断で予告なく落ちた場合、ブローカーが代理で offline を配信する
client.will_set(
    STATUS_TOPIC,
    json.dumps({"device_id": DEVICE_ID, "online": False}),
    qos=1, retain=True,
)

_broker = {"host": None, "port": None}


def publish_online():
    payload = json.dumps({
        "device_id": DEVICE_ID,
        "online": True,
        # プロセスごとに変わる。サーバー側が「再起動したモジュール」を見分けて
        # 画面を送り直すために使う（ハートビートでは変わらない）
        "session": SESSION_ID,
        "ip": local_ip_toward(_broker["host"]) if _broker["host"] else None,
    })
    # retain=True にすると、app.py が後から起動しても現在の状態を受け取れる
    client.publish(STATUS_TOPIC, payload, qos=1, retain=True)


def publish_reply(body):
    """サーバーの問い合わせに答える。request_id を付けて返さないと紐付かない"""
    client.publish(REPLY_TOPIC, json.dumps(dict(body, device_id=DEVICE_ID), ensure_ascii=False), qos=1)


def on_connect(client, userdata, flags, reason_code, properties):
    log(f"[MQTT] connected to {_broker['host']}:{_broker['port']} as {DEVICE_ID}")
    publish_online()
    # 下り。自分宛てだけを購読する
    client.subscribe(CMD_TOPIC, qos=1)
    # 機材に紐付いていればサーバーが直後に本来の表示を送ってくるので、それまでの暫定表示
    set_led("idle")
    render_display(["社員証をタッチしてください"])


def on_disconnect(client, userdata, flags, reason_code, properties):
    log(f"[MQTT] disconnected (reason={reason_code})", key=f"disc:{reason_code}")


def on_message(client, userdata, msg):
    """
    サーバーからの指示。NFC待ちは main スレッドでブロックしているので、
    下りの処理はこのコールバック（paho のネットワークスレッド）側で完結させる。
    """
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except ValueError:
        print(f"[cmd] 壊れたペイロード: {msg.payload!r}")
        return

    cmd = payload.get("cmd")
    if cmd == "display":
        render_display(payload.get("lines") or [], payload.get("badge"))
    elif cmd == "led":
        set_led(payload.get("state", "idle"))
    elif cmd == "confirm":
        # 答えを待つ間このスレッドを止めると後続の指示を取りこぼすので、別スレッドへ
        threading.Thread(target=_handle_confirm, args=(payload,), daemon=True).start()
    elif cmd == "choice":
        threading.Thread(target=_handle_choice, args=(payload,), daemon=True).start()
    elif cmd == "record":
        threading.Thread(target=_handle_record, args=(payload,), daemon=True).start()
    elif cmd == "ping":
        publish_reply({"request_id": payload.get("request_id"), "answer": "pong"})
    else:
        print(f"[cmd] 未知のコマンド: {cmd}")


# paho の自動再接続は既定で最大120秒まで待ち幅を伸ばす。supervisor() が
# loop_stop() でその待ちに合流するため、待ち幅が長いと再探索の開始が遅れる。
client.reconnect_delay_set(min_delay=1, max_delay=RETRY_SEC)

client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


# ---------------------------------------------- 表示・LED・ボタン（B-1/B-2/B-3）
# 2.8インチ SPI TFT (ILI9341) に描く。ディスプレイが無い・SPIが無効・PC上で
# sim.py を動かしている場合は自動でコンソール出力に落ちるので、呼び出し側
# （on_message）は環境を気にしなくてよい。
#
# ステータスLED(B-3)は独立した部品がまだ無いので、画面上部の色帯で代用している。

LED_LABELS = {
    "idle": "空き", "working": "作業中", "free": "フリー利用中",
    "guide": "移動してください", "recording": "タスク登録中",
    "error": "使用不可", "offline": "オフライン",
}

LED_COLORS = {
    "idle":    (0x2E, 0xA0, 0x43),
    "working": (0xE0, 0x8A, 0x1E),
    "free":    (0x2F, 0x6D, 0xCC),
    "guide":   (0x7E, 0x3F, 0xB8),   # C-3 の誘導中。他のどの状態とも見間違えない紫
    # 音声でのタスク登録中(E-2)。機材を使う状態ではないので、空き(緑)・作業中(橙)・
    # フリー利用(青)のどれとも重ならない色にしてある
    "recording": (0xC2, 0x37, 0x9A),
    "error":   (0xC8, 0x32, 0x32),
    "offline": (0x5A, 0x5A, 0x5A),
}

BG_COLOR = (0x12, 0x12, 0x14)
CARD_COLOR = (0x1D, 0x1D, 0x22)   # 選択肢チップの下地
LINE_COLOR = (0x32, 0x32, 0x3C)   # 区切り線・チップの枠
FG_COLOR = (0xF2, 0xF2, 0xF4)
SUB_COLOR = (0x9E, 0x9E, 0xA8)
DIM_COLOR = (0x6A, 0x6A, 0x74)

# 画面の骨組み。320x240（rotation=90）を基準にした固定値で、240x320 でも
# 破綻しないよう本文の高さだけが伸び縮みする。
BAND_H = 40      # 上部の状態帯
BAR_H = 34       # 下部のボタン列
PAD = 12

# 物理ボタン(B-2)と画面下部のラベルの対応。**左右を入れ替えないこと。**
# 現場で画面の「◀」と実際に押すボタンがずれると必ず迷う。
BTN_LABELS = ("◀ 前へ", "● 決定", "次へ ▶")

# 文字の大きさ。選択肢チップは、この順に試して**収まる一番大きいもの**を使う。
# 「はい/いいえ」なら big、「タスク実行」のような長い選択肢は自動で小さくなる。
ROLE_PX = {"big": 28, "title": 25, "body": 20, "chip": 17, "small": 14}
CHIP_ROLES = ("big", "body", "chip", "small")

# フォントは1つでは足りない。Raspberry Pi OS 標準の DroidSansFallbackFull は
# 日本語を持つ代わりに ASCII のグリフが無く、'A' や '0' が豆腐(□)になる。逆に
# DejaVuSans は英数字だけ。そこで候補を並べ、文字ごとに持っている方で描く。
# apt で Noto CJK を入れれば1つで済むが、root が要るので依存させない。
FONT_PATHS = [p for p in (
    os.environ.get("GEMMBA_FONT"),                                 # 明示指定を最優先
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",             # 英数字・記号
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",   # 日本語
) if p]
DISPLAY_MODE = os.environ.get("GEMMBA_DISPLAY", "auto")  # auto | off

# 今どういう画面を出しているか。描画関数はここだけを見て絵を作る。
#   choice = None                                   … 通常表示（下部は接続先）
#   choice = {"text":…, "options":[…], "selected":n} … 選択中（下部はボタン列）
_screen = {"lines": [], "state": "idle", "choice": None, "badge": None}
_last_pushed = None  # 直前に描いた内容。同じなら描き直さない
_lcd = None
_fonts = {}          # role -> [ImageFont, ...] 前から順に、その字を持つ方を使う
_ascent = {}         # role -> ベースライン位置（フォントを混ぜても行が揃うように）
_notdef_cache = {}   # font -> 豆腐(.notdef)のビットマップ
_glyph_cache = {}    # (role, 文字) -> 実際に使うフォント
# MQTTの受信スレッドと confirm のスレッドが同時に描きに来るので直列化する
_display_lock = threading.Lock()


def _load_fonts():
    """フォントを1度だけ読む。ハード無しでも呼べる（--preview 用）"""
    from PIL import ImageFont

    if _fonts:
        return
    # big は選択肢チップ用。title は本文1行目（タスク名など）で、離れた場所から
    # 読めるように一番大きくしてある。chip は選択肢が長いときの中間段。
    for role, size in ROLE_PX.items():
        loaded = []
        for path in FONT_PATHS:
            try:
                loaded.append(ImageFont.truetype(path, size))
            except OSError:
                pass
        if not loaded:
            loaded = [ImageFont.load_default()]
            print(f"[LCD] フォントを読めません（{FONT_PATHS}）。文字化けします。")
        _fonts[role] = loaded
        # 異なるフォントを混ぜて描くので、ベースラインを揃えないと上下にガタつく
        _ascent[role] = max(f.getmetrics()[0] for f in loaded)


def _has_glyph(font, ch):
    """その文字の絵を持っているか。持っていなければ .notdef（豆腐）と同じ絵になる"""
    from PIL import Image, ImageDraw

    def bitmap(c):
        img = Image.new("L", (48, 48), 0)
        ImageDraw.Draw(img).text((4, 4), c, font=font, fill=255)
        return img.tobytes()

    notdef = _notdef_cache.get(font)
    if notdef is None:
        notdef = _notdef_cache[font] = bitmap(chr(0xE000))  # 私用領域＝まず入っていない
    return bitmap(ch) != notdef


def _font_for(role, ch):
    """文字ごとに、その字を持っているフォントを選ぶ。結果は覚えておく"""
    key = (role, ch)
    font = _glyph_cache.get(key)
    if font is None:
        fonts = _fonts[role]
        font = next((f for f in fonts if _has_glyph(f, ch)), fonts[0])
        _glyph_cache[key] = font
    return font


def _text_width(text, role):
    return sum(_font_for(role, ch).getlength(ch) for ch in text)


def _draw_text(d, x, y, text, role, fill):
    """フォントを混ぜて1行描く。y は行の上端で、内部でベースラインに揃える"""
    baseline = y + _ascent[role]
    for ch in text:
        font = _font_for(role, ch)
        d.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch)
    return x


def init_display():
    """LCDを開く。開けなければ None のままでコンソール出力になる（例外は出さない）"""
    global _lcd
    if DISPLAY_MODE == "off":
        print("[LCD] GEMMBA_DISPLAY=off のため使いません")
        return None
    try:
        import ili9341
    except ImportError as e:
        print(f"[LCD] 使えません（{e}）。コンソールに出力します。")
        return None
    try:
        lcd = ili9341.open_display()
    except Exception as e:
        print(f"[LCD] 使えません（{e}）。コンソールに出力します。")
        return None

    _load_fonts()
    _lcd = lcd
    print(f"[LCD] {lcd.width}x{lcd.height} rotation={lcd.rotation} で初期化しました")
    return lcd


def _rounded(d, box, radius, **kw):
    """角丸。古い Pillow には rounded_rectangle が無いので、無ければ角ばらせる"""
    try:
        d.rounded_rectangle(box, radius=radius, **kw)
    except AttributeError:
        d.rectangle(box, **kw)


def _draw_band(d, W, state, badge=None):
    """
    上部の状態帯。状態の色をそのまま面で見せるので、離れていても状態が分かる。

    右肩は既定では機材の識別子。badge（「1/3」など）が来たらそちらを出す。
    候補の件数を本文の頭に入れると、その分タスク名が押し出されて切れるため。
    """
    color = LED_COLORS.get(state, LED_COLORS["offline"])
    d.rectangle([0, 0, W, BAND_H], fill=color)
    _draw_text(d, PAD, 9, LED_LABELS.get(state, state), "body", (255, 255, 255))
    if badge:
        _draw_text(d, W - _text_width(str(badge), "body") - PAD, 9, str(badge), "body",
                   (255, 255, 255))
    else:
        _draw_text(d, W - _text_width(DEVICE_ID, "small") - PAD, 14, DEVICE_ID, "small",
                   (0xF0, 0xF0, 0xF0))


def _draw_button_bar(d, W, H, labels=BTN_LABELS):
    """
    下部のボタン列。**物理ボタンの左右と画面上の左右を一致させるための帯。**
    押すものが無い画面では呼ばない（押せると誤解させないため）。
    """
    top = H - BAR_H
    d.rectangle([0, top, W, H], fill=CARD_COLOR)
    d.line([0, top, W, top], fill=LINE_COLOR)
    left, ok, right = labels
    y = top + (BAR_H - 20) // 2
    _draw_text(d, PAD, y, left, "small", SUB_COLOR)
    _draw_text(d, (W - _text_width(ok, "small")) // 2, y, ok, "small", FG_COLOR)
    _draw_text(d, W - _text_width(right, "small") - PAD, y, right, "small", SUB_COLOR)


def _draw_footer(d, W, H):
    """ボタン列を出さない画面の下部。接続先を小さく置いておく（現場の切り分け用）"""
    foot = f"broker {_broker['host']}" if _broker["host"] else "ブローカー未接続"
    _draw_text(d, PAD, H - 22, foot, "small", DIM_COLOR)


def _draw_body(d, W, top, bottom, lines, center=False):
    """
    本文。1行目だけ大きく、残りは補足として小さく積む。

    center=True で上下中央に置く。行数が1〜4行と振れるので、上詰めにすると
    短い画面（「お疲れさまでした」等）だけ下半分がぽっかり空いて据わりが悪い。
    """
    steps = [34 if i == 0 else 28 for i in range(len(lines))]
    total = 0
    shown = 0
    for step in steps:
        if top + total + step > bottom:
            break
        total += step
        shown += 1
    y = top + max(0, (bottom - top - total) // 2) if center else top
    for i, line in enumerate(lines[:shown]):
        role = "title" if i == 0 else "body"
        _draw_text(d, PAD, y, _fit(str(line), role, W - PAD * 2), role,
                   FG_COLOR if i == 0 else SUB_COLOR)
        y += steps[i]
    return y


def _draw_choices(d, W, top, bottom, choice, state):
    """
    選択肢を横並びのチップで描く。選択中は状態色で塗り、他は枠だけにする。
    2択（はい/いいえ）も3択（簡単/普通/難しい）も同じ見た目になる。
    """
    options = choice["options"]
    selected = choice["selected"]
    accent = LED_COLORS.get(state, LED_COLORS["offline"])

    if choice.get("text"):
        _draw_text(d, PAD, top, _fit(choice["text"], "body", W - PAD * 2), "body", FG_COLOR)
        top += 30

    gap = 6
    inner = 8                      # チップ内の左右の余白
    avail = W - PAD * 2 - gap * (len(options) - 1)
    chip_w = avail // len(options)
    chip_h = min(52, max(40, bottom - top - 6))
    y = top + max(0, (bottom - top - chip_h) // 2)

    # 全部のチップで同じ大きさにする。1つだけ小さいと不揃いに見えるので、
    # **一番長い選択肢が収まる大きさ**に全体を合わせる。
    role = CHIP_ROLES[-1]
    for candidate in CHIP_ROLES:
        if all(_text_width(str(o), candidate) <= chip_w - inner for o in options):
            role = candidate
            break

    for i, label in enumerate(options):
        x = PAD + i * (chip_w + gap)
        box = [x, y, x + chip_w, y + chip_h]
        if i == selected:
            _rounded(d, box, 8, fill=accent)
            fg = (0x10, 0x10, 0x12)
        else:
            _rounded(d, box, 8, fill=CARD_COLOR, outline=LINE_COLOR, width=1)
            fg = SUB_COLOR
        text = _fit(str(label), role, chip_w - inner)
        tx = x + (chip_w - _text_width(text, role)) // 2
        ty = y + (chip_h - ROLE_PX[role]) // 2 - 2
        _draw_text(d, tx, ty, text, role, fg)


def _compose(lines, state, size=None, choice=None, badge=None):
    """
    1画面ぶんの絵を作る。上が状態の色帯、真ん中が本文、下はボタン列（選択中のみ）。

    choice を渡すと選択画面になり、下部が物理ボタンに対応したラベル列になる。
    """
    from PIL import Image, ImageDraw

    W, H = size or (_lcd.width, _lcd.height)
    img = Image.new("RGB", (W, H), BG_COLOR)
    d = ImageDraw.Draw(img)

    _draw_band(d, W, state, badge)
    top = BAND_H + 14

    if choice:
        bottom = H - BAR_H - 8
        # 選択中は本文を2行までに抑える。チップの高さを確保する方を優先する
        y = _draw_body(d, W, top, bottom - 46, list(lines)[:2])
        _draw_choices(d, W, y + 6, bottom, choice, state)
        _draw_button_bar(d, W, H)
    else:
        _draw_body(d, W, top, H - 28, list(lines)[:4], center=True)
        _draw_footer(d, W, H)
    return img


def _fit(text, role, max_width):
    """画面幅に収まらない行は末尾を … で詰める。機材名やタスク名は長くなりうる"""
    if _text_width(text, role) <= max_width:
        return text
    while text and _text_width(text + "…", role) > max_width:
        text = text[:-1]
    return text + "…"


def _push():
    """内容が変わっていなければ描かない。SPIの全面書き換えは200ms近くかかる"""
    global _last_pushed
    if _lcd is None:
        return
    key = repr(_screen)
    if key == _last_pushed:
        return
    try:
        with _display_lock:
            _lcd.display(_compose(_screen["lines"], _screen["state"],
                                  choice=_screen["choice"], badge=_screen["badge"]))
        _last_pushed = key
    except Exception as e:
        print(f"[LCD] 描画に失敗: {e}")


def _print_screen():
    """LCDが無い環境用の代替出力。選択中はどれを選んでいるかも出す"""
    width = 34
    print("┌" + "─" * width)
    for line in _screen["lines"]:
        print("│ " + str(line))
    choice = _screen["choice"]
    if choice:
        if choice.get("text"):
            print("│ " + choice["text"])
        marks = ["[" + str(o) + "]" if i == choice["selected"] else " " + str(o) + " "
                 for i, o in enumerate(choice["options"])]
        print("│ " + "  ".join(marks))
    print("└" + "─" * width)


def render_display(lines, badge=None):
    _screen["lines"] = [str(x) for x in lines]
    _screen["choice"] = None      # 新しい表示が来たら選択画面は畳む
    _screen["badge"] = badge
    _print_screen()
    _push()


def set_led(state):
    _screen["state"] = state
    print(f"[LED] {LED_LABELS.get(state, state)}")
    _apply_leds(state)     # 実物のLED（B-3）。無ければ何もしない
    _push()


def _compose_selftest(size, rotation):
    """動作確認画面の絵。ハード無しでも作れるよう lcd に依存させない"""
    from PIL import Image, ImageDraw

    W, H = size
    img = Image.new("RGB", (W, H), BG_COLOR)
    d = ImageDraw.Draw(img)

    bars = [("R", (255, 0, 0)), ("G", (0, 255, 0)), ("B", (0, 0, 255)),
            ("C", (0, 255, 255)), ("M", (255, 0, 255)), ("Y", (255, 255, 0)),
            ("W", (255, 255, 255)), ("K", (0, 0, 0))]
    bw = W / len(bars)
    top, bh = 30, 60
    for i, (label, color) in enumerate(bars):
        x0 = int(i * bw)
        x1 = int((i + 1) * bw) - 1
        d.rectangle([x0, top, x1, top + bh], fill=color)
        # ラベルは帯の下に置く（帯の上に書くと白/黒帯で読めない）
        _draw_text(d, x0 + 6, top + bh + 4, label, "small", SUB_COLOR)

    _draw_text(d, 32, 4, "ILI9341 動作確認", "body", FG_COLOR)  # 左上のかぎ括弧を避ける

    y = top + bh + 28
    for text, color in (
        (f"{W}x{H}  rotation={rotation}", FG_COLOR),
        (f"device_id: {DEVICE_ID}", SUB_COLOR),
        ("日本語表示テスト: 旋盤 稼働中", (0x6C, 0xD0, 0x8A)),
    ):
        _draw_text(d, 10, y, text, "body", color)
        y += 28

    # 4隅のかぎ括弧。切れていたら表示領域か回転がずれている
    m, L = 2, 22
    for cx, cy, dx, dy in ((m, m, 1, 1), (W - m - 1, m, -1, 1),
                           (m, H - m - 1, 1, -1), (W - m - 1, H - m - 1, -1, -1)):
        d.line([cx, cy, cx + dx * L, cy], fill=(255, 255, 255), width=2)
        d.line([cx, cy, cx, cy + dy * L], fill=(255, 255, 255), width=2)
    return img


def selftest(hold_sec=180):
    """
    ディスプレイの動作確認画面。配線・向き・色順(RGB/BGR)・日本語フォントを
    一度に確認する。

        python3 raspi.py --selftest [秒数]

    見るべき点:
      - 4隅のかぎ括弧が全部見えるか  → 表示領域と回転が合っているか
      - 色帯のラベルと色が一致するか → RGB/BGRの順序（赤と青が逆なら要調整）
      - 日本語が化けていないか       → フォント

    描いたら hold_sec 秒そのまま保持する。プロセスが終わると gpiozero が
    GPIO を解放し、バックライトを GPIO で制御している配線では消えてしまうため。
    """
    lcd = init_display()
    if lcd is None:
        print("[selftest] ディスプレイを開けませんでした。上のメッセージを確認してください。")
        return 1

    lcd.display(_compose_selftest((lcd.width, lcd.height), lcd.rotation))
    print("[selftest] 表示しました。画面を確認してください。")
    print("  ・4隅のかぎ括弧が全部見える → 表示領域と回転はOK")
    print("  ・R/G/B の帯が赤/緑/青の順  → 色順はOK（赤と青が逆なら GEMMBA_LCD_ROTATION や配線を確認）")
    print("  ・日本語が読める            → フォントOK")
    print(f"[selftest] {hold_sec}秒間このまま表示します（Ctrl-C で終了）")
    try:
        time.sleep(hold_sec)
    except KeyboardInterrupt:
        pass
    print("[selftest] 終了します。")
    return 0


TOUCH_CAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".touch_calibration.json")


def load_calibration():
    """生値→画面座標の変換係数。無ければ None（未キャリブレーション）"""
    try:
        with open(TOUCH_CAL_PATH, encoding="utf-8") as f:
            return json.load(f)["coeffs"]
    except (OSError, ValueError, KeyError):
        return None


def raw_to_screen(coeffs, rx, ry):
    """
    アフィン変換で生値を画面座標にする。

        px = a1*rx + b1*ry + c1
        py = a2*rx + b2*ry + c2

    x/y の入れ替わりや上下左右の反転も係数側に吸収されるので、
    パネルの向きを気にしなくてよい。
    """
    (a1, b1, c1), (a2, b2, c2) = coeffs
    return int(a1 * rx + b1 * ry + c1), int(a2 * rx + b2 * ry + c2)


def _wait_for_tap(touch, timeout=30):
    """1回のタップの生値を返す。押した瞬間ではなく、値が落ち着いてから拾う"""
    deadline = time.time() + timeout
    while time.time() < deadline:                      # 押されるまで待つ
        if touch.read_touch():
            break
        time.sleep(0.02)
    else:
        return None

    samples = []
    while time.time() < deadline:                      # 押されている間ためる
        point = touch.read_touch()
        if point is None:
            break
        samples.append(point)
        time.sleep(0.02)
    if len(samples) < 3:
        return None
    # 押し始めと離し際は値が暴れるので中央付近だけ使う
    core = samples[len(samples) // 4: max(len(samples) * 3 // 4, len(samples) // 4 + 1)]
    xs = sorted(p[0] for p in core)
    ys = sorted(p[1] for p in core)
    while touch.read_touch():                          # 離すまで待つ
        time.sleep(0.02)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def touch_calibrate():
    """
    画面の5点を順にタップしてもらい、生値→画面座標の変換係数を最小二乗で求める。

        python3 raspi.py --calibrate

    結果は raspi/.touch_calibration.json に保存され、以後自動で読まれる。
    """
    import numpy as np
    from PIL import Image, ImageDraw

    try:
        import xpt2046
    except ImportError as e:
        print(f"[calibrate] xpt2046.py がありません: {e}")
        return 1

    lcd = init_display()
    if lcd is None:
        print("[calibrate] ディスプレイを開けませんでした。")
        return 1
    try:
        touch = xpt2046.open_touch()
    except Exception as e:
        print(f"[calibrate] タッチパネルを開けません: {e}")
        return 1

    W, H = lcd.width, lcd.height
    m = 30
    targets = [(m, m), (W - m, m), (W - m, H - m), (m, H - m), (W // 2, H // 2)]
    measured = []

    for i, (px, py) in enumerate(targets, 1):
        img = Image.new("RGB", (W, H), BG_COLOR)
        d = ImageDraw.Draw(img)
        d.line([px - 12, py, px + 12, py], fill=(255, 255, 255), width=2)
        d.line([px, py - 12, px, py + 12], fill=(255, 255, 255), width=2)
        d.ellipse([px - 5, py - 5, px + 5, py + 5], outline=(0xE0, 0x8A, 0x1E), width=2)
        _draw_text(d, 12, 10, f"キャリブレーション {i}/{len(targets)}", "body", FG_COLOR)
        _draw_text(d, 12, H - 30, "十字の中心を押してください", "small", SUB_COLOR)
        lcd.display(img)
        print(f"[calibrate] {i}/{len(targets)}  画面の十字を押してください…", flush=True)

        raw = _wait_for_tap(touch)
        if raw is None:
            print("[calibrate] 反応がありませんでした。中止します。")
            touch.close()
            return 1
        print(f"           生値 x={raw[0]} y={raw[1]}")
        measured.append(raw)
        time.sleep(0.3)

    # [rx, ry, 1] から [px, py] への最小二乗フィット
    A = np.array([[rx, ry, 1.0] for rx, ry in measured])
    coeffs = []
    for axis in (0, 1):
        b = np.array([t[axis] for t in targets], dtype=float)
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        coeffs.append([float(v) for v in sol])

    errors = [max(abs(raw_to_screen(coeffs, rx, ry)[a] - targets[i][a]) for a in (0, 1))
              for i, (rx, ry) in enumerate(measured)]
    worst = max(errors)

    with open(TOUCH_CAL_PATH, "w", encoding="utf-8") as f:
        json.dump({"coeffs": coeffs, "size": [W, H]}, f, ensure_ascii=False, indent=2)

    print(f"[calibrate] 保存しました: {TOUCH_CAL_PATH}")
    print(f"[calibrate] 最大誤差 {worst}px" +
          ("（十分です）" if worst <= 12 else "（大きいのでやり直しを勧めます）"))

    img = Image.new("RGB", (W, H), BG_COLOR)
    d = ImageDraw.Draw(img)
    _draw_text(d, 12, 40, "キャリブレーション完了", "title", FG_COLOR)
    _draw_text(d, 12, 90, f"最大誤差 {worst}px", "body", SUB_COLOR)
    lcd.display(img)
    touch.close()
    return 0


def buttontest(duration=30.0):
    """
    ボタン(B-2)の配線確認。押すたびに、どのボタンとして認識されたかを出す。

        python3 raspi.py --btntest [秒数]

    「左」を押して 左 と出れば向きが合っている。左右が逆に出るなら、配線を
    入れ替えるか `GEMMBA_BTN_LEFT` と `GEMMBA_BTN_RIGHT` を入れ替える。
    """
    if not init_buttons():
        print("ボタンを開けませんでした。配線と gpiozero を確認してください。")
        return 1
    labels = {"left": "左（前へ）", "ok": "決定", "right": "右（次へ）"}
    for name in ("left", "ok", "right"):
        print(f"  {labels[name]:<10} GPIO{BUTTON_PINS[name]}")

    # 押していないのに押下状態なら、まず配線を疑う。タクトスイッチは4本足のうち
    # 2本ずつが内部で繋がっているので、同じ組を選ぶと常に導通したままになる。
    stuck = [labels[n] for n in ("left", "ok", "right") if _buttons[n].is_pressed]
    if stuck:
        print("！ 押していないのに押下状態: " + " / ".join(stuck))
        print("  タクトスイッチの4本足のうち、**内部で最初から繋がっている組**を")
        print("  選んでいる可能性があります。隣り合う足ではなく対角の足を使ってください。")

    print(f"\n{duration:.0f}秒間、押されたボタンを表示します（Ctrl-C で終了）。")
    while not _button_events.empty():
        _button_events.get_nowait()
    deadline = time.time() + duration
    seen = set()
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                name = _button_events.get(timeout=remaining)
            except queue.Empty:
                break
            seen.add(name)
            print(f"  押された → {labels[name]}  (GPIO{BUTTON_PINS[name]})")
    except KeyboardInterrupt:
        pass

    missing = [labels[n] for n in ("left", "ok", "right") if n not in seen]
    if missing:
        print("\n一度も反応しなかった: " + " / ".join(missing))
        return 1
    print("\n3つとも反応しました")
    return 0


def ledtest(hold_sec=2.0):
    """
    ステータスLED(B-3)の配線確認。

        python3 raspi.py --ledtest [秒数]

    まず赤だけ・青だけを点けて**色と極性**を確かめ、そのあと実際の状態遷移を
    なぞる。光らない場合は、足の向き（長い足がアノード＝GPIO側）と抵抗を疑う。
    """
    if not init_leds():
        print("LEDを開けませんでした。配線と gpiozero を確認してください。")
        return 1
    try:
        for name in ("red", "blue"):
            print(f"[{name}] だけ点灯 … GPIO{LED_PINS[name]}")
            for other, led in _leds.items():
                led.value = (other == name)
            time.sleep(hold_sec)
        print("--- 状態をなぞります ---")
        for state in ("idle", "working", "free", "guide", "error", "offline"):
            color = "赤" if state in LED_RED_STATES else "青"
            print(f"{LED_LABELS[state]:<12} → {color}")
            _apply_leds(state)
            time.sleep(hold_sec)
    finally:
        for led in _leds.values():
            led.off()
    print("完了（消灯しました）")
    return 0


def _choice(text, options, selected):
    return {"text": text, "options": options, "selected": selected}


# モジュールが出しうる画面の一覧。**app.py が送る内容と対で維持すること。**
# 画面デザインの確認はここを見れば全部そろうようにしてある（--preview）。
#   (キー, 画面名, いつ出るか, lines, state, choice, badge)
PREVIEW_SCREENS = [
    # ---- 起動・待機 -------------------------------------------------------
    ("boot", "起動中", "電源投入直後。ブローカーを探している間",
     [f"Gemmba {DEVICE_ID}", "ブローカーを探しています…"], "offline", None, None),
    ("idle", "待機", "空いている機材の通常表示。ここから全部が始まる",
     ["旋盤 #2", "社員証をタッチしてください"], "idle", None, None),
    ("restored", "使用中（復元）", "モジュール再起動後、DBの状態から復元したとき",
     ["山田 花子 さん 使用中", "製品A ロット12 組立"], "working", None, None),
    ("unavailable", "停止・メンテ中", "機材が stopped / maintenance のとき",
     ["旋盤 #2", "メンテ中"], "error", None, None),

    # ---- メニュー --------------------------------------------------------
    ("menu_idle", "メニュー（空き）", "タッチすると必ずここに来る。空きなら「タスク実行」が選ばれた状態",
     ["田中 太郎 さん", "旋盤 #2"], "idle",
     _choice("どうしますか？", ["タスク実行", "タスク登録", "作業終了"], 0), None),
    ("menu_working", "メニュー（作業中）", "作業中は「作業終了」が選ばれた状態で出る。決定1回で終わる",
     ["田中 太郎 さん", "旋盤 #2"], "working",
     _choice("どうしますか？", ["タスク実行", "タスク登録", "作業終了"], 2), None),
    ("menu_busy", "すでに作業中", "作業中に「タスク実行」を選んだとき。メニューへ戻る",
     ["すでに作業中です", "終わるときは「作業終了」"], "working", None, None),
    ("menu_notask", "作業中のタスクが無い", "空きのときに「作業終了」を選んだとき。待機画面へ戻る",
     ["作業中のタスクがありません", "「タスク実行」から始めてください"], "idle", None, None),

    # ---- 音声でのタスク登録（E-2）-----------------------------------------
    ("rec_wait", "録音待ち", "「タスク登録」を選んだあと。決定を押している間だけ録音する",
     ["田中 太郎 さん", "決定ボタンを押している間", "話してください"], "recording", None, None),
    ("rec_done", "登録できた", "文字起こしの結果をそのままタスク名にして登録する",
     ["タスクを登録しました", "旋盤の切粉清掃を至急お願いします"], "idle", None, None),
    ("rec_fail", "録音できなかった", "押す時間が短すぎた・マイクが不調・PCへ送れなかった",
     ["録音できませんでした", "もう一度お試しください"], "error", None, None),

    # ---- 誘導（C-3）------------------------------------------------------
    ("guide_ask", "移動しますか？", "他機材に優先タスクがあるとき。ロックより前に尋ねる",
     ["プレス機 #1 に至急のタスク", "製品C 外形加工"], "idle",
     _choice("プレス機 #1 へ移動しますか？", ["はい", "いいえ"], 0), None),
    ("guide_go", "移動の案内", "「はい」のあと、移動元に15秒出る",
     ["プレス機 #1 へ移動してください", "製品C 外形加工", "移動先で社員証をタッチ"],
     "guide", None, None),
    ("guide_arrive", "到着の予告", "移動先の機材に180秒出る。歩く時間ぶん長め",
     ["山田 花子 さんが向かっています", "製品C 外形加工", "社員証をタッチしてください"],
     "guide", None, None),

    # ---- 着手（C-1 / C-2）------------------------------------------------
    ("task_ask", "タスクの提示", "候補をおすすめ順（割り当て済み→優先度→AI）に最大3件、1件ずつ。右上が何件目か",
     ["製品A ロット12 組立", "30個 / 期限 2026-09-30"], "idle",
     _choice("このタスクに着手しますか？", ["はい", "いいえ"], 0), "1/3"),
    ("task_ask_long", "タスクの提示（長い名前）", "タスク名が長いときの省略のされ方",
     ["レーザー加工機 #1 の長いタスク名テスト", "20個 / 期限 2026-09-20"], "idle",
     _choice("このタスクに着手しますか？", ["はい", "いいえ"], 0), "3/3"),
    ("free_ask", "フリー利用の確認", "候補が無い／全部スキップしたとき",
     ["すべてスキップしました"], "idle",
     _choice("フリー利用で使いますか？", ["はい", "いいえ"], 0), None),
    ("working", "作業中", "着手してロックした状態。終了タッチまでこれ",
     ["山田 花子 さん", "製品A ロット12 組立", "30個 / 期限 2026-09-30",
      "終了時にもう一度タッチ"], "working", None, None),
    ("free", "フリー利用中", "タスク無しでロックした状態",
     ["山田 花子 さん", "フリー利用中", "終了時にもう一度タッチ"], "free", None, None),
    ("cancelled", "キャンセル", "フリー利用も断ったとき。3秒で待機へ戻る",
     ["キャンセルしました"], "idle", None, None),
    ("timeout", "無応答で中止", "25秒答えなかったとき。ロックせずに戻す（C-4）",
     ["応答がありませんでした", "もう一度タッチしてください"], "idle", None, None),

    # ---- 完了（D-6）------------------------------------------------------
    ("felt", "難易度フィードバック", "完了・機材解放の直後。20秒で時間切れ",
     ["製品A ロット12 組立", "お疲れさまでした"], "idle",
     _choice("この作業の難易度は？", ["簡単", "普通", "難しい"], 1), None),
    ("done", "完了", "フィードバックのあと。待機へ戻る",
     ["お疲れさまでした", "社員証をタッチしてください"], "idle", None, None),

    # ---- エラー -----------------------------------------------------------
    ("unknown_tag", "未登録ICカード", "登録されていないカードがタッチされたとき",
     ["未登録のICカードです", "管理画面から登録してください"], "error", None, None),
    ("unknown_module", "未登録モジュール", "機材に紐付いていないモジュールでタッチしたとき",
     ["未登録のモジュールです", "機材管理画面で紐付けてください"], "error", None, None),
    ("busy", "他の人が使用中", "別の作業者がロックしている機材にタッチしたとき",
     ["他の人が使用中です", "鈴木 一郎 さんは使用できません"], "working", None, None),
    ("no_perm", "権限不足", "承認の間に権限や必要権限が変わって着手できなかったとき",
     ["このタスクには権限が必要です", "アーク溶接"], "error", None, None),
]


# 画面の並び順と区分。現場フローの順にしてあるので、上から読めば遷移が追える。
PREVIEW_GROUPS = [
    ("起動・待機", ["boot", "idle", "restored", "unavailable"]),
    ("メニュー", ["menu_idle", "menu_working", "menu_busy", "menu_notask"]),
    ("タスク登録・音声 (E-2)", ["rec_wait", "rec_done", "rec_fail"]),
    ("誘導 (C-3)", ["guide_ask", "guide_go", "guide_arrive"]),
    ("着手 (C-1 / C-2)", ["task_ask", "task_ask_long", "free_ask",
                          "working", "free", "cancelled", "timeout"]),
    ("完了 (D-6)", ["felt", "done"]),
    ("エラー", ["unknown_tag", "unknown_module", "busy", "no_perm"]),
    ("診断", ["selftest"]),
]


def _group_of(key):
    return next((name for name, keys in PREVIEW_GROUPS if key in keys), "その他")


def preview(path="screens.png"):
    """
    ハード無しで全画面をPNGに書き出す。SPIが通る前や開発PCで、はみ出し・
    文字化け・配置を確認するため。

        python3 raspi.py --preview /tmp/screens.png   # 1枚にまとめた一覧
        python3 raspi.py --preview /tmp/screens/      # 1画面ずつ + index.json

    ディレクトリを指定すると1画面ずつ書き出し、名前と「いつ出るか」を
    index.json に添える。PC側でデザインを並べて見るときはこちら。
    """
    import json as _json
    from PIL import Image

    rot = int(os.environ.get("GEMMBA_LCD_ROTATION", "90"))
    size = (320, 240) if rot in (90, 270) else (240, 320)
    _load_fonts()

    rendered = [(key, name, when, state,
                 _compose(lines, state, size, choice, badge))
                for key, name, when, lines, state, choice, badge in PREVIEW_SCREENS]
    rendered.append(("selftest", "動作確認画面", "--selftest で出る診断用。配線・色順・日本語の確認",
                     "idle", _compose_selftest(size, rot)))

    if path.lower().endswith(".png"):
        cols = 3
        rows = (len(rendered) + cols - 1) // cols
        sheet = Image.new("RGB", (size[0] * cols + 12 * (cols + 1),
                                  size[1] * rows + 12 * (rows + 1)), (60, 60, 66))
        for i, (_, _, _, _, img) in enumerate(rendered):
            sheet.paste(img, (12 + (i % cols) * (size[0] + 12),
                              12 + (i // cols) * (size[1] + 12)))
        sheet.save(path)
        print(f"[preview] {path} に {len(rendered)} 画面を書き出しました ({size[0]}x{size[1]})")
        return 0

    os.makedirs(path, exist_ok=True)
    index = []
    for i, (key, name, when, state, img) in enumerate(rendered, 1):
        filename = f"{i:02d}-{key}.png"
        img.save(os.path.join(path, filename))
        index.append({"file": filename, "key": key, "name": name,
                      "when": when, "state": state,
                      "state_label": LED_LABELS.get(state, state),
                      "group": _group_of(key)})
    with open(os.path.join(path, "index.json"), "w", encoding="utf-8") as fp:
        _json.dump({"size": list(size), "rotation": rot, "screens": index},
                   fp, ensure_ascii=False, indent=2)
    print(f"[preview] {path} に {len(rendered)} 画面と index.json を書き出しました "
          f"({size[0]}x{size[1]})")
    return 0


# ------------------------------------------------------------ 物理ボタン（B-2）
# 左 / 決定 / 右 の3つ。抵抗は要らない（内部プルアップを使う）。押すと Low。
#
#   左   GPIO5  (29番ピン)  … 選択を左へ
#   決定 GPIO6  (31番ピン)  … 確定
#   右   GPIO13 (33番ピン)  … 選択を右へ
#   GND は 30/34/39番ピンが近い
#
# SPI(7〜11)・LCD(24/25)・I2Sマイク(18/19/20) を避けた空きピンで、並び順が
# そのまま画面下部の 左/決定/右 に対応するよう昇順に割り当ててある。
BUTTON_PINS = {
    "left":  int(os.environ.get("GEMMBA_BTN_LEFT", "5")),
    "ok":    int(os.environ.get("GEMMBA_BTN_OK", "6")),
    "right": int(os.environ.get("GEMMBA_BTN_RIGHT", "13")),
}
_button_events = queue.Queue()
_buttons = {}


def _on_press(name):
    return lambda: _button_events.put(name)


def init_buttons():
    """
    ボタンを開く。開けなければ False を返し、呼び出し側はキーボード入力に落ちる。
    どちらが先に押されたかを待つ必要があるので、押下をキューに積む方式にしてある。
    """
    if _buttons:
        return True
    try:
        from gpiozero import Button
    except ImportError as e:
        print(f"[BTN] gpiozero がありません（{e}）。キーボード入力に落とします。")
        return False
    try:
        for name, pin in BUTTON_PINS.items():
            # bounce_time でチャタリング除去まで済む
            button = Button(pin, pull_up=True, bounce_time=0.05)
            button.when_pressed = _on_press(name)
            _buttons[name] = button
    except Exception as e:
        print(f"[BTN] 開けません（{e}）。キーボード入力に落とします。")
        for button in _buttons.values():
            button.close()
        _buttons.clear()
        return False
    print(f"[BTN] 左=GPIO{BUTTON_PINS['left']} 決定=GPIO{BUTTON_PINS['ok']} "
          f"右=GPIO{BUTTON_PINS['right']}")
    return True


# ------------------------------------------------------------ ステータスLED（B-3）
# 単色LED 2個。**作業中は赤、それ以外は青。** 電流制限抵抗が各1本要る（220〜1kΩ）。
#
#   赤 GPIO17（11番ピン）→ 抵抗 → LEDのアノード(長い足)、カソード(短い足) → GND(9番)
#   青 GPIO27（13番ピン）→ 抵抗 → LEDのアノード(長い足)、カソード(短い足) → GND(9番)
#
# 9/11/13番が隣り合っているので、GND・赤・青を並べて挿せる。
#
# **GPIO16 は使えない。** I2Sマイクの `dtoverlay=googlevoicehat-soundcard`（E-2）が
# アンプの sdmode として掴んでいて、開こうとすると 'GPIO busy' になる。同じ理由で
# GPIO18/19/20 もマイクが使っている。
#
# 画面上部の色帯（LED_COLORS）は6状態を色で出し分けるが、こちらは2色しかないので
# 「その機材が今ふさがっているか」だけを離れた場所から見せる役割に割り切っている。
LED_PINS = {
    "red": int(os.environ.get("GEMMBA_LED_RED", "17")),
    "blue": int(os.environ.get("GEMMBA_LED_BLUE", "27")),
}
# 赤を点ける状態。フリー利用も「ふさがっている」に含めるなら "free" を足す
LED_RED_STATES = {"working"}
_leds = {}


def init_leds():
    """ステータスLEDを開く。無ければ何もしない（画面の色帯だけで動き続ける）"""
    if _leds:
        return True
    if os.environ.get("GEMMBA_LED", "auto") == "off":
        print("[LED] GEMMBA_LED=off のため使いません")
        return False
    try:
        from gpiozero import LED
    except ImportError as e:
        print(f"[LED] gpiozero がありません（{e}）。画面の色帯だけで動きます。")
        return False
    try:
        for name, pin in LED_PINS.items():
            _leds[name] = LED(pin)
    except Exception as e:
        print(f"[LED] 開けません（{e}）。画面の色帯だけで動きます。")
        for led in _leds.values():
            led.close()
        _leds.clear()
        return False
    print(f"[LED] 赤=GPIO{LED_PINS['red']} 青=GPIO{LED_PINS['blue']}")
    _apply_leds(_screen["state"])
    return True


def _apply_leds(state):
    """
    常にどちらか片方だけ点ける。両方消えた状態を作らないのは、消灯と
    「モジュールが死んでいる」が現場で見分けられなくなるため。
    """
    if not _leds:
        return
    red = state in LED_RED_STATES
    try:
        _leds["red"].value = red
        _leds["blue"].value = not red
    except Exception as e:
        print(f"[LED] 点灯に失敗: {e}")


# ------------------------------------------------------------ 音声入力（E-2）
# **決定ボタンを押している間だけ**録音して、PCへ送る。専用の録音ボタンは設けない
# （ボタンを増やすより、メニューで「タスク登録」を選んでから決定を押す方が迷わない）。
#
# 録音形式は rec.sh と同じ S32_LE / ステレオ / 16kHz。左チャンネルにだけ声が出る。
# 変換はしない。PC側の voice/stt.py がこの形をそのまま読める。
MIC_DEVICE = os.environ.get("GEMMBA_MIC", "plughw:CARD=sndrpigooglevoi,DEV=0")
REC_MAX_SEC = float(os.environ.get("GEMMBA_REC_MAX", "30"))
REC_MIN_SEC = 0.6     # これより短い押下は押し間違いとみなして捨てる
REC_PATH = "/tmp/gemmba_rec.wav"


def record_while_held(max_sec=REC_MAX_SEC, wait_sec=30):
    """
    決定ボタンが押されるのを待ち、**押している間だけ**録音する。
    戻り値: 録音したファイルのパス / None（押されなかった・短すぎた・失敗）
    """
    import subprocess

    if not init_buttons():
        print("[REC] ボタンが無いので録音できません")
        return None
    ok = _buttons["ok"]

    if not ok.wait_for_press(timeout=wait_sec):
        print("[REC] 決定ボタンが押されませんでした")
        return None

    cmd = ["arecord", "-D", MIC_DEVICE, "-c", "2", "-r", "16000",
           "-f", "S32_LE", "-d", str(int(max_sec)), "-q", REC_PATH]
    started = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        print(f"[REC] arecord を起動できません: {e}")
        return None
    print("[REC] 録音中…")

    # 離すまで待つ。押しっぱなしでも max_sec で arecord 自身が止まる
    ok.wait_for_release(timeout=max_sec)
    held = time.time() - started
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"[REC] 録音終了 {held:.1f}秒")

    if held < REC_MIN_SEC:
        print("[REC] 短すぎるので捨てます")
        return None
    if not os.path.exists(REC_PATH) or os.path.getsize(REC_PATH) < 1000:
        print("[REC] 録音できていません（マイクの配線を確認してください）")
        return None
    return REC_PATH


def upload_recording(path, url, timeout=60):
    """録音をPCへ送り、応答のJSONを返す。失敗したら None"""
    import requests

    try:
        with open(path, "rb") as fp:
            res = requests.post(url, data=fp.read(),
                                headers={"Content-Type": "audio/wav"}, timeout=timeout)
    except Exception as e:
        print(f"[REC] 送信に失敗: {e}")
        return None
    if not res.ok:
        print(f"[REC] サーバーがエラーを返しました: {res.status_code} {res.text[:120]}")
        return None
    try:
        return res.json()
    except ValueError:
        print("[REC] 応答がJSONではありません")
        return None


def _handle_record(payload):
    """
    「録音して送れ」という指示。決定ボタンを押している間だけ録音して送り、
    結果を応答で返す。画面は送り主（app.py）が出すので、ここでは触らない。
    """
    request_id = payload.get("request_id")
    url = payload.get("url")
    if not url:
        publish_reply({"request_id": request_id, "answer": False, "error": "no url"})
        return
    path = record_while_held(float(payload.get("max_sec", REC_MAX_SEC)),
                             float(payload.get("wait_sec", 30)))
    if path is None:
        publish_reply({"request_id": request_id, "answer": False, "error": "no audio"})
        return
    body = upload_recording(path, url, timeout=float(payload.get("upload_timeout", 90)))
    if body is None:
        publish_reply({"request_id": request_id, "answer": False, "error": "upload failed"})
        return
    publish_reply({"request_id": request_id, "answer": bool(body.get("ok")),
                   "text": body.get("text"), "task_id": body.get("task_id"),
                   "error": body.get("error")})


# console モードの入力。行を1本のスレッドで読んでキューに積む。都度 input() する
# 実装だと、時間切れになった問い合わせのスレッドが stdin を掴んだまま残る。
_stdin_lines = queue.Queue()


def _stdin_reader():
    for line in sys.stdin:
        _stdin_lines.put(line.strip())


def _console_choice(options, timeout):
    """キーボードで選ぶ。番号のほか、2択のときは y / n も受ける（従来の操作のまま）"""
    while not _stdin_lines.empty():  # 問い合わせ前に打たれた行は捨てる
        _stdin_lines.get_nowait()
    hint = " / ".join(f"{i + 1}={o}" for i, o in enumerate(options))
    print(f"[入力] {hint}")
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            line = _stdin_lines.get(timeout=remaining).strip().lower()
        except queue.Empty:
            return None
        if len(options) == 2 and line in ("y", "yes"):
            return 0
        if len(options) == 2 and line in ("n", "no"):
            return 1
        if line.isdigit() and 1 <= int(line) <= len(options):
            return int(line) - 1
        print(f"[入力] {hint}")


def _move_selection(step):
    """選択を動かして描き直す。端では止める（押し続けて一周すると現場で迷う）"""
    choice = _screen["choice"]
    if not choice:
        return
    n = len(choice["options"])
    choice["selected"] = min(max(choice["selected"] + step, 0), n - 1)
    _print_screen()
    _push()


def _buttons_choice(options, timeout):
    """物理ボタンで選ぶ。左右でカーソルを動かし、決定で確定する"""
    while not _button_events.empty():   # 問い合わせ前の押下は捨てる
        _button_events.get_nowait()
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            name = _button_events.get(timeout=remaining)
        except queue.Empty:
            return None
        if name == "ok":
            return _screen["choice"]["selected"]
        _move_selection(-1 if name == "left" else 1)


def ask_choice(text, lines, options, timeout, default=0, badge=None):
    """
    選択肢から1つ選んでもらう。物理ボタン(左/右/決定)で操作する（B-2）。
    戻り値: 選んだ添字 / None=時間切れ

    Yes/No も難易度フィードバックもこれ1つで賄う。入力元を差し替えるときは
    ここだけを見ればよく、on_message も app.py も変更は要らない。
    """
    options = [str(o) for o in options]
    if not options:
        return None
    _screen["lines"] = [str(x) for x in lines]
    _screen["badge"] = badge
    _screen["choice"] = {
        "text": text,
        "options": options,
        "selected": min(max(default, 0), len(options) - 1),
    }
    _print_screen()
    _push()
    try:
        if INPUT_MODE == "yes":
            return 0                      # 2択なら「はい」。自動応答の試験用
        if INPUT_MODE == "no":
            return len(options) - 1       # 2択なら「いいえ」
        if INPUT_MODE == "timeout":
            time.sleep(timeout)
            return None
        if INPUT_MODE == "buttons" and init_buttons():
            return _buttons_choice(options, timeout)
        return _console_choice(options, timeout)
    finally:
        # 答えた後・時間切れの後にチップを残さない。次の display が来るまでの間、
        # 押せないものが押せるように見えてしまう。
        _screen["choice"] = None
        _push()


def ask_yes_no(text, lines, timeout, badge=None):
    """
    Yes/No を取得する。中身は2択の ask_choice。
    戻り値: True=はい / False=いいえ / None=時間切れ
    """
    index = ask_choice(text, lines, ["はい", "いいえ"], timeout, badge=badge)
    return None if index is None else index == 0


def _handle_confirm(payload):
    answer = ask_yes_no(
        payload.get("text", ""),
        payload.get("lines") or [],
        float(payload.get("timeout", 30)),
        badge=payload.get("badge"),
    )
    if answer is None:
        print("[cmd] 応答なしで時間切れ")
    publish_reply({"request_id": payload.get("request_id"), "answer": answer})


def _handle_choice(payload):
    """3択以上を尋ねる指示。答えは選んだ添字で返す（ラベルは送り主が決めている）"""
    options = payload.get("options") or []
    if not options:
        print("[cmd] choice に options がありません")
        publish_reply({"request_id": payload.get("request_id"), "answer": None})
        return
    index = ask_choice(
        payload.get("text", ""),
        payload.get("lines") or [],
        options,
        float(payload.get("timeout", 30)),
        default=int(payload.get("default", 0)),
        badge=payload.get("badge"),
    )
    if index is None:
        print("[cmd] 応答なしで時間切れ")
    publish_reply({"request_id": payload.get("request_id"), "answer": index})


def connect_forever():
    """繋がるまで探索と接続を繰り返す。PCより先に起動しても落ちない"""
    while True:
        target = resolve_broker()
        if target:
            host, port = target
            try:
                client.connect(host, port, KEEPALIVE)
                _broker["host"], _broker["port"] = host, port
                save_cache(host, port)
                return
            except OSError as e:
                log(f"[MQTT] {host}:{port} へ接続できません ({e})",
                    key=f"conn:{host}:{port}:{e}")
        else:
            log("[discovery] ブローカーが見つかりません（PC側の app.py は起動していますか？）",
                key="nofind")
        time.sleep(RETRY_SEC)


def supervisor():
    """
    切断されたら再探索して繋ぎ直す。paho の自動再接続は同じIPを見続けるため、
    PC側のIPが変わったケースはここで拾う必要がある。
    """
    while True:
        time.sleep(RETRY_SEC)
        if client.is_connected():
            continue
        log("[MQTT] 切断を検知。ブローカーを再探索します。", key="research")
        client.loop_stop()
        connect_forever()
        client.loop_start()


def heartbeat():
    """定期的に生存を通知する。誰も機材に触らなくてもオンラインだと分かる"""
    while True:
        time.sleep(HEARTBEAT_SEC)
        if client.is_connected():
            publish_online()


# ------------------------------------------------------------ NFC

def touch_loop(on_tag):
    """
    カードが1回タッチされるごとに on_tag(tag_id) を呼び続ける。RC522 を使う。

    「1回のタッチ」を成立させるには、カードが離れたことを判定する必要がある。
    これが無いと、置いたままのカードを何度も読み直して連続発火する。
    離脱は「一定時間カードが見えないこと」で判断する。在席フラグの類は
    カードの種類によって当てにならないため（旧USBリーダーで実証済み）。
    """
    import rc522

    # 1回のタッチ = 1回の呼び出しにするため、「カードが見えている間」を状態として持つ。
    # read_uid() は磁界の揺らぎで一時的に None を返すことがあるので、
    # 途切れてすぐ離脱とはみなさず TOUCH_RELEASE_SEC ぶんの猶予を置く。
    holding, last_seen = False, 0.0

    while True:
        reader = None
        try:
            reader = rc522.open_reader()
            print(f"[NFC] RC522 を初期化しました (VersionReg=0x{reader.version:02X})。"
                  "カードを待っています。")
            while True:
                uid = reader.read_uid()
                now = time.monotonic()
                if uid:
                    last_seen = now
                    if not holding:
                        holding = True
                        print(f"Tag ID: {uid}")
                        try:
                            on_tag(uid)
                        except Exception as e:   # 送信失敗でリーダーは止めない
                            print(f"[NFC] 送信に失敗: {e}")
                elif holding and now - last_seen >= TOUCH_RELEASE_SEC:
                    holding = False
                    print("[NFC] カードが離れました")
                time.sleep(TOUCH_POLL_SEC)
        except Exception as e:
            print(f"[NFC] リーダーのエラー: {e}  5秒後に開き直します")
            holding = False
            if reader is not None:
                reader.close()
            time.sleep(5)


def send_to_host_tag_id(tag_id):
    payload = json.dumps({
        "device_id": DEVICE_ID,
        "tag_id": tag_id,
        "timestamp": time.time(),
    })
    info = client.publish(DATA_TOPIC, payload, qos=1)
    # NFCのコールバックから呼ばれるので、無期限に待つとリーダーが止まる
    try:
        info.wait_for_publish(timeout=5)
    except (RuntimeError, ValueError) as e:
        print(f"[MQTT] タッチの送信を確認できませんでした: {e}")


def main():
    init_display()
    init_leds()
    set_led("offline")
    render_display([f"Gemmba {DEVICE_ID}", "ブローカーを探しています…"])
    connect_forever()
    client.loop_start()
    threading.Thread(target=supervisor, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    if INPUT_MODE == "buttons":
        init_buttons()   # 起動時に開いておく。失敗してもここでは落とさない
    if INPUT_MODE == "console":
        threading.Thread(target=_stdin_reader, daemon=True).start()

    touch_loop(send_to_host_tag_id)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        sys.exit(selftest(int(arg) if arg.isdigit() else 180))
    if "--btntest" in sys.argv:
        i = sys.argv.index("--btntest")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        sys.exit(buttontest(float(arg) if arg.replace(".", "", 1).isdigit() else 30.0))
    if "--ledtest" in sys.argv:
        i = sys.argv.index("--ledtest")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        sys.exit(ledtest(float(arg) if arg.replace(".", "", 1).isdigit() else 2.0))
    if "--calibrate" in sys.argv:
        sys.exit(touch_calibrate())
    if "--preview" in sys.argv:
        i = sys.argv.index("--preview")
        sys.exit(preview(sys.argv[i + 1] if len(sys.argv) > i + 1 else "screens.png"))
    main()
