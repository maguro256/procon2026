// Gemmba モジュール本体（ESP32 版）。raspi/raspi.py の移植。
// pio run -e gemmba -t upload
//
// サーバー（app.py）とのやり取りはラズパイ版と同じなので、サーバー側の変更は要らない。
//   - ブローカーは UDP ブロードキャスト（GEMMBA_DISCOVER_V1 / 50505）で探す。見つからなければ
//     前回つながったブローカー（NVS に保存）を試す
//   - MQTT のトピック: pi/<id>/status（LWT・retain）, data（タッチ）, cmd（下り）, reply（応答）
//   - 下りの cmd: display / led / confirm / choice / record / restart / ping
//   - 録音は決定ボタンを押している間だけ録り、録りながら /api/voice へ送る（TODO.md H 参照）
//
// ラズパイ版との違い:
//   - MQTT の上り publish は QoS0（PubSubClient の制約）。LAN 内の TCP なので実用上は落ちない
//   - 1本のループで全部を回す（スレッドを使わない）。confirm / choice は状態として持ち、
//     待っている間も MQTT・タッチ・ボタンは動き続ける
//   - 録音は 16kHz / 16bit / モノラルの WAV で送る（stt.py はどちらの形式も読める）
//   - MQTT が切れている間は画面を「オフライン」にする
//
// 配線（esp32/ の各テストと同じ）:
//   LCD   : CS=5 RESET=17 DC=26 MOSI=23 SCK=18（SDO はつながない）
//   RC522 : SS=4 RST=22 MOSI=23 MISO=19 SCK=18
//   マイク: SCK=32 WS=25 SD=33 L/R=GND
//   ボタン: 左=13 決定=14 右=27（GPIO → ボタン → GND）
//   LED   : 赤=21 青=16（GPIO → 抵抗 → LED → GND）

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <MFRC522.h>
#include <LovyanGFX.hpp>
#include <driver/i2s.h>
#include <time.h>
#include <vector>

#include "secrets.h"

// ------------------------------------------------------------ 設定

// 機材との対応付けに使う名前。管理画面の「モジュールID」と一致させる。
// 指定しなければ MAC アドレスの下位3バイトから作る（モジュールごとに必ず違う）。
//   build_flags = -DGEMMBA_DEVICE_ID=\"esp01\"
#ifndef GEMMBA_DEVICE_ID
#define GEMMBA_DEVICE_ID ""
#endif

static constexpr uint16_t DISCOVERY_PORT = 50505;
static const char* DISCOVERY_REQUEST = "GEMMBA_DISCOVER_V1";
static constexpr uint32_t DISCOVERY_TIMEOUT_MS = 3000;
static constexpr uint16_t KEEPALIVE_SEC = 30;
static constexpr uint32_t HEARTBEAT_MS = 30000;
static constexpr uint32_t RETRY_MS = 5000;

// この時間カードが見えなければ「離れた」とみなす（磁界の揺らぎで一時的に読めないため）
static constexpr uint32_t TOUCH_RELEASE_MS = 1000;
static constexpr uint32_t TOUCH_POLL_MS = 100;

static constexpr uint32_t BUTTON_DEBOUNCE_MS = 50;  // 2台目のボタンは 20ms では揺れが残った

static constexpr float REC_MAX_SEC = 30;
static constexpr uint32_t REC_MIN_MS = 600;  // これより短い押下は押し間違いとみなして捨てる
static constexpr int SAMPLE_RATE = 16000;

// ピン
static constexpr int PIN_SCK = 18, PIN_MOSI = 23, PIN_MISO = 19;
static constexpr int PIN_LCD_CS = 5, PIN_LCD_DC = 26, PIN_LCD_RST = 17;
static constexpr int PIN_RC522_SS = 4, PIN_RC522_RST = 22;
static constexpr int PIN_MIC_SCK = 32, PIN_MIC_WS = 25, PIN_MIC_SD = 33;
static constexpr int PIN_LED_RED = 21, PIN_LED_BLUE = 16;
enum Btn { BTN_LEFT, BTN_OK, BTN_RIGHT, BTN_COUNT };
static const int BTN_PINS[BTN_COUNT] = {13, 14, 27};

// ------------------------------------------------------------ 状態

static String device_id;
static String session_id;
static String status_topic, data_topic, cmd_topic, reply_topic;

static WiFiClient mqtt_net;
static PubSubClient mqtt(mqtt_net);
static Preferences prefs;
static String broker_host;
static uint16_t broker_port = 0;

static MFRC522 rfid(PIN_RC522_SS, PIN_RC522_RST);

// ------------------------------------------------------------ ログ

static void logf(const char* fmt, ...) {
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.printf("[%8.1f] %s\n", millis() / 1000.0, buf);
}

// ------------------------------------------------------------ 画面（ILI9341）

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9341 panel_;
  lgfx::Bus_SPI bus_;

 public:
  LGFX() {
    auto b = bus_.config();
    b.spi_host = VSPI_HOST;
    b.spi_mode = 0;
    b.freq_write = 20000000;
    b.freq_read = 8000000;
    b.spi_3wire = false;
    b.use_lock = true;
    b.dma_channel = 0;  // RC522（Arduino の SPI）と同じバスを共有するので DMA は使わない
    b.pin_sclk = PIN_SCK;
    b.pin_mosi = PIN_MOSI;
    b.pin_miso = PIN_MISO;
    b.pin_dc = PIN_LCD_DC;
    bus_.config(b);
    panel_.setBus(&bus_);

    auto p = panel_.config();
    p.pin_cs = PIN_LCD_CS;
    p.pin_rst = PIN_LCD_RST;
    p.pin_busy = -1;
    p.panel_width = 240;
    p.panel_height = 320;
    p.readable = false;
    p.invert = false;
    p.rgb_order = false;
    p.bus_shared = true;  // 描くたびにバスを手放す（RC522 と交互に使う）
    panel_.config(p);
    setPanel(&panel_);
  }
};

static LGFX lcd;
static constexpr int W = 320, H = 240;
static constexpr int BAND_H = 40, BAR_H = 34, PAD = 12;

static uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return lgfx::color565(r, g, b); }

static const uint16_t BG_COLOR = rgb(0x12, 0x12, 0x14);
static const uint16_t CARD_COLOR = rgb(0x1D, 0x1D, 0x22);
static const uint16_t LINE_COLOR = rgb(0x32, 0x32, 0x3C);
static const uint16_t FG_COLOR = rgb(0xF2, 0xF2, 0xF4);
static const uint16_t SUB_COLOR = rgb(0x9E, 0x9E, 0xA8);
static const uint16_t DIM_COLOR = rgb(0x6A, 0x6A, 0x74);
static const uint16_t WHITE = rgb(0xFF, 0xFF, 0xFF);

struct StateDef {
  const char* key;
  const char* label;
  uint16_t color;
};
static const StateDef STATES[] = {
    {"idle", "空き", rgb(0x2E, 0xA0, 0x43)},
    {"working", "作業中", rgb(0xE0, 0x8A, 0x1E)},
    {"free", "フリー利用中", rgb(0x2F, 0x6D, 0xCC)},
    {"guide", "移動してください", rgb(0x7E, 0x3F, 0xB8)},
    {"gathering", "集合待ち", rgb(0x14, 0x91, 0x9B)},  // 複数人タスクの人がそろうのを待っている
    {"recording", "タスク登録中", rgb(0xC2, 0x37, 0x9A)},
    {"error", "使用不可", rgb(0xC8, 0x32, 0x32)},
    {"offline", "オフライン", rgb(0x5A, 0x5A, 0x5A)},
};

static const StateDef& state_def(const String& key) {
  for (auto& s : STATES)
    if (key == s.key) return s;
  return STATES[sizeof(STATES) / sizeof(STATES[0]) - 1];  // 未知は offline の色
}

// 文字の大きさ。ラズパイ版の big 28 / title 25 / body 20 / chip 17 / small 14 を、
// フラッシュに載る3サイズ（24 / 20 / 16）へ寄せた
enum Role { ROLE_BIG, ROLE_TITLE, ROLE_BODY, ROLE_CHIP, ROLE_SMALL };
static const lgfx::IFont* role_font(Role r) {
  switch (r) {
    case ROLE_BIG:
    case ROLE_TITLE: return &fonts::lgfxJapanGothicP_24;
    case ROLE_BODY: return &fonts::lgfxJapanGothicP_20;
    default: return &fonts::lgfxJapanGothicP_16;
  }
}
static const Role CHIP_ROLES[] = {ROLE_BIG, ROLE_BODY, ROLE_CHIP};

struct Choice {
  bool active = false;
  String text;
  std::vector<String> options;
  int selected = 0;
};

struct Screen {
  std::vector<String> lines;
  String state = "offline";
  String badge;
  Choice choice;
};
static Screen screen;
static bool screen_dirty = true;

static int text_width(const String& s, Role r) {
  lcd.setFont(role_font(r));
  return lcd.textWidth(s);
}

static int font_height(Role r) {
  lcd.setFont(role_font(r));
  return lcd.fontHeight();
}

// UTF-8 の1文字ぶん戻った位置
static int utf8_prev(const String& s, int end) {
  int i = end - 1;
  while (i > 0 && (s[i] & 0xC0) == 0x80) i--;
  return i < 0 ? 0 : i;
}

// 幅に収まらない行は末尾を … で詰める（機材名・タスク名は長くなりうる）
static String fit(const String& text, Role r, int max_w) {
  if (text_width(text, r) <= max_w) return text;
  String t = text;
  while (t.length() && text_width(t + "…", r) > max_w) t = t.substring(0, utf8_prev(t, t.length()));
  return t + "…";
}

static void draw_text(int x, int y, const String& s, Role r, uint16_t color) {
  lcd.setFont(role_font(r));
  lcd.setTextColor(color);
  lcd.setTextDatum(lgfx::top_left);
  lcd.drawString(s, x, y);
}

static void draw_band() {
  const StateDef& st = state_def(screen.state);
  lcd.fillRect(0, 0, W, BAND_H, st.color);
  int y = (BAND_H - font_height(ROLE_BODY)) / 2;
  draw_text(PAD, y, st.label, ROLE_BODY, WHITE);
  if (screen.badge.length()) {
    draw_text(W - text_width(screen.badge, ROLE_BODY) - PAD, y, screen.badge, ROLE_BODY, WHITE);
  } else {
    int ys = (BAND_H - font_height(ROLE_SMALL)) / 2;
    draw_text(W - text_width(device_id, ROLE_SMALL) - PAD, ys, device_id, ROLE_SMALL, rgb(0xF0, 0xF0, 0xF0));
  }
}

// 本文。1行目だけ大きく、残りは補足として小さく積む。center=true で上下中央
static int draw_body(int top, int bottom, const std::vector<String>& lines, size_t max_lines, bool center) {
  int total = 0;
  size_t shown = 0;
  for (size_t i = 0; i < lines.size() && i < max_lines; i++) {
    int step = i == 0 ? 34 : 28;
    if (top + total + step > bottom) break;
    total += step;
    shown++;
  }
  int y = center ? top + max(0, (bottom - top - total) / 2) : top;
  for (size_t i = 0; i < shown; i++) {
    Role r = i == 0 ? ROLE_TITLE : ROLE_BODY;
    draw_text(PAD, y, fit(lines[i], r, W - PAD * 2), r, i == 0 ? FG_COLOR : SUB_COLOR);
    y += i == 0 ? 34 : 28;
  }
  return y;
}

// 選択肢を横並びのチップで描く。選択中は状態色で塗り、他は枠だけ
static void draw_choices(int top, int bottom) {
  const Choice& c = screen.choice;
  uint16_t accent = state_def(screen.state).color;
  if (c.text.length()) {
    draw_text(PAD, top, fit(c.text, ROLE_BODY, W - PAD * 2), ROLE_BODY, FG_COLOR);
    top += 30;
  }
  int n = c.options.size();
  if (n == 0) return;
  const int gap = 6, inner = 8;
  int chip_w = (W - PAD * 2 - gap * (n - 1)) / n;
  int chip_h = min(52, max(40, bottom - top - 6));
  int y = top + max(0, (bottom - top - chip_h) / 2);

  // 全チップを同じ大きさの字にする。一番長い選択肢が収まる大きさに合わせる
  Role role = CHIP_ROLES[2];
  for (Role cand : CHIP_ROLES) {
    bool ok = true;
    for (auto& o : c.options)
      if (text_width(o, cand) > chip_w - inner) ok = false;
    if (ok) {
      role = cand;
      break;
    }
  }
  for (int i = 0; i < n; i++) {
    int x = PAD + i * (chip_w + gap);
    uint16_t fg;
    if (i == c.selected) {
      lcd.fillRoundRect(x, y, chip_w, chip_h, 8, accent);
      fg = rgb(0x10, 0x10, 0x12);
    } else {
      lcd.fillRoundRect(x, y, chip_w, chip_h, 8, CARD_COLOR);
      lcd.drawRoundRect(x, y, chip_w, chip_h, 8, LINE_COLOR);
      fg = SUB_COLOR;
    }
    String t = fit(c.options[i], role, chip_w - inner);
    int tx = x + (chip_w - text_width(t, role)) / 2;
    int ty = y + (chip_h - font_height(role)) / 2;
    draw_text(tx, ty, t, role, fg);
  }
}

// 下部のボタン列。物理ボタンの左右と画面上の左右を一致させる（入れ替えないこと）
static void draw_button_bar() {
  int top = H - BAR_H;
  lcd.fillRect(0, top, W, BAR_H, CARD_COLOR);
  lcd.drawFastHLine(0, top, W, LINE_COLOR);
  int fh = font_height(ROLE_SMALL);
  int y = top + (BAR_H - fh) / 2;
  int cy = top + BAR_H / 2;
  // ◀ ▶ はフォントに無いので三角形で描く
  lcd.fillTriangle(PAD, cy, PAD + 9, cy - 6, PAD + 9, cy + 6, SUB_COLOR);
  draw_text(PAD + 14, y, "前へ", ROLE_SMALL, SUB_COLOR);
  String ok = "● 決定";
  draw_text((W - text_width(ok, ROLE_SMALL)) / 2, y, ok, ROLE_SMALL, FG_COLOR);
  String next = "次へ";
  int nx = W - PAD - 14 - text_width(next, ROLE_SMALL);
  draw_text(nx, y, next, ROLE_SMALL, SUB_COLOR);
  lcd.fillTriangle(W - PAD, cy, W - PAD - 9, cy - 6, W - PAD - 9, cy + 6, SUB_COLOR);
}

// 接続先は secrets.h の WIFI_SSID を優先し、WIFI_SSID2 があればつながらないときに交互に試す
// （ラズパイ版の NetworkManager の優先度と同じ考え方）
struct WifiAp {
  const char* ssid;
  const char* pass;
};
static const WifiAp WIFI_APS[] = {
    {WIFI_SSID, WIFI_PASSWORD},
#ifdef WIFI_SSID2
    {WIFI_SSID2, WIFI_PASSWORD2},
#endif
};
static constexpr int WIFI_AP_COUNT = sizeof(WIFI_APS) / sizeof(WIFI_APS[0]);
static int wifi_ap = 0;

// ボタン列を出さない画面の下部。接続先を小さく置く（現場の切り分け用）
static void draw_footer() {
  String foot;
  if (WiFi.status() != WL_CONNECTED) foot = String("Wi-Fi 未接続（") + WIFI_APS[wifi_ap].ssid + "）";
  else if (mqtt.connected()) foot = "broker " + broker_host;
  else foot = "ブローカー未接続  " + WiFi.localIP().toString();
  draw_text(PAD, H - 22, foot, ROLE_SMALL, DIM_COLOR);
}

static void render() {
  lcd.startWrite();
  lcd.fillScreen(BG_COLOR);
  draw_band();
  int top = BAND_H + 14;
  if (screen.choice.active) {
    int bottom = H - BAR_H - 8;
    int y = draw_body(top, bottom - 46, screen.lines, 2, false);
    draw_choices(y + 6, bottom);
    draw_button_bar();
  } else {
    draw_body(top, H - 28, screen.lines, 4, true);
    draw_footer();
  }
  lcd.endWrite();
  screen_dirty = false;
}

static void print_screen() {
  Serial.println("┌──────────────────────────────────");
  for (auto& l : screen.lines) Serial.println("│ " + l);
  if (screen.choice.active) {
    if (screen.choice.text.length()) Serial.println("│ " + screen.choice.text);
    String marks = "│ ";
    for (size_t i = 0; i < screen.choice.options.size(); i++) {
      bool sel = (int)i == screen.choice.selected;
      marks += String(sel ? "[" : " ") + screen.choice.options[i] + (sel ? "] " : "  ");
    }
    Serial.println(marks);
  }
  Serial.println("└──────────────────────────────────");
}

// ------------------------------------------------------------ LED（B-3）
// 作業中は赤、それ以外は青。両方消えた状態を作らない（死んでいるのと見分けられないため）

static void apply_leds(const String& state) {
  bool red = state == "working";
  digitalWrite(PIN_LED_RED, red);
  digitalWrite(PIN_LED_BLUE, !red);
}

static void render_display(const std::vector<String>& lines, const String& badge = "") {
  screen.lines = lines;
  screen.badge = badge;
  screen.choice.active = false;  // 新しい表示が来たら選択画面は畳む
  print_screen();
  screen_dirty = true;
}

static void set_led(const String& state) {
  screen.state = state;
  logf("[LED] %s", state_def(state).label);
  apply_leds(state);
  screen_dirty = true;
}

// ------------------------------------------------------------ ボタン（B-2）

struct Button {
  int stable = HIGH, last_raw = HIGH;
  uint32_t changed_at = 0;
};
static Button buttons[BTN_COUNT];
static std::vector<Btn> button_events;

static void buttons_begin() {
  for (int i = 0; i < BTN_COUNT; i++) pinMode(BTN_PINS[i], INPUT_PULLUP);
}

static void buttons_scan() {
  uint32_t now = millis();
  for (int i = 0; i < BTN_COUNT; i++) {
    Button& b = buttons[i];
    int raw = digitalRead(BTN_PINS[i]);
    if (raw != b.last_raw) {
      b.last_raw = raw;
      b.changed_at = now;
    }
    if (raw != b.stable && now - b.changed_at >= BUTTON_DEBOUNCE_MS) {
      b.stable = raw;
      if (raw == LOW) button_events.push_back((Btn)i);
    }
  }
}

static bool ok_pressed() { return buttons[BTN_OK].stable == LOW; }

// ------------------------------------------------------------ MQTT の上り

static void publish_json(const String& topic, JsonDocument& doc, bool retain = false) {
  String out;
  serializeJson(doc, out);
  if (!mqtt.publish(topic.c_str(), out.c_str(), retain)) logf("[MQTT] publish 失敗: %s", topic.c_str());
}

static void publish_online() {
  JsonDocument doc;
  doc["device_id"] = device_id;
  doc["online"] = true;
  doc["session"] = session_id;  // 起動ごとに変わる。サーバーが再起動を見分けて画面を送り直す
  doc["ip"] = WiFi.localIP().toString();
  publish_json(status_topic, doc, true);
}

static void publish_reply(JsonDocument& body) {
  body["device_id"] = device_id;
  publish_json(reply_topic, body);
}

// ------------------------------------------------------------ 選択（confirm / choice）

struct Ask {
  bool active = false;
  bool yes_no = false;  // confirm は true/false、choice は添字で答える
  String request_id;
  uint32_t deadline = 0;
};
static Ask ask;

static void ask_reply(bool timed_out) {
  JsonDocument r;
  r["request_id"] = ask.request_id;
  if (timed_out) {
    r["answer"] = nullptr;
    logf("[cmd] 応答なしで時間切れ");
  } else if (ask.yes_no) {
    r["answer"] = screen.choice.selected == 0;
  } else {
    r["answer"] = screen.choice.selected;
  }
  publish_reply(r);
  ask.active = false;
  // 答えた後・時間切れの後にチップを残さない（押せないものが押せるように見えるため）
  screen.choice.active = false;
  screen_dirty = true;
}

static std::vector<String> json_lines(JsonVariantConst v) {
  std::vector<String> out;
  for (JsonVariantConst x : v.as<JsonArrayConst>()) {
    if (x.is<const char*>()) out.push_back(x.as<const char*>());
    else {
      String s;
      serializeJson(x, s);
      out.push_back(s);
    }
  }
  return out;
}

static String json_str(JsonVariantConst v) {
  if (v.isNull()) return "";
  if (v.is<const char*>()) return v.as<const char*>();
  String s;
  serializeJson(v, s);
  return s;
}

static void start_ask(JsonDocument& p, bool yes_no) {
  if (ask.active) ask_reply(true);  // 前の問い合わせは時間切れ扱いで閉じる
  std::vector<String> options;
  int def = 0;
  if (yes_no) {
    options = {"はい", "いいえ"};
  } else {
    options = json_lines(p["options"]);
    def = p["default"] | 0;
  }
  if (options.empty()) {
    logf("[cmd] choice に options がありません");
    JsonDocument r;
    r["request_id"] = json_str(p["request_id"]);
    r["answer"] = nullptr;
    publish_reply(r);
    return;
  }
  screen.lines = json_lines(p["lines"]);
  screen.badge = json_str(p["badge"]);
  screen.choice.active = true;
  screen.choice.text = json_str(p["text"]);
  screen.choice.options = options;
  screen.choice.selected = constrain(def, 0, (int)options.size() - 1);
  print_screen();
  screen_dirty = true;

  ask.active = true;
  ask.yes_no = yes_no;
  ask.request_id = json_str(p["request_id"]);
  ask.deadline = millis() + (uint32_t)((p["timeout"] | 30.0f) * 1000);
  button_events.clear();  // 問い合わせ前の押下は捨てる
}

static void ask_service() {
  if (!ask.active) return;
  for (Btn b : button_events) {
    if (b == BTN_OK) {
      ask_reply(false);
      break;
    }
    // 端では止める（押し続けて一周すると現場で迷う）
    int n = screen.choice.options.size();
    screen.choice.selected = constrain(screen.choice.selected + (b == BTN_LEFT ? -1 : 1), 0, n - 1);
    screen.choice.active = true;
    print_screen();
    screen_dirty = true;
  }
  button_events.clear();
  if (ask.active && (int32_t)(millis() - ask.deadline) >= 0) ask_reply(true);
}

// ------------------------------------------------------------ 音声入力（E-2）

static void mic_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;  // 声は [0] 側に出る（mic_test で確認済み）
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 512;  // 合計 4096 フレーム = 256ms。HTTP の接続待ちの間も取りこぼさない
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_MIC_SCK;
  pins.ws_io_num = PIN_MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_SD;
  esp_err_t e1 = i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr);
  esp_err_t e2 = i2s_set_pin(I2S_NUM_0, &pins);
  if (e1 != ESP_OK || e2 != ESP_OK) logf("[MIC] I2S を開けません (%d, %d)", e1, e2);
}

// 80Hz のハイパス。録り始めの DC ドリフトと、末尾に詰める無音との段差を消す
struct HighPass {
  float a = 1.0f / (1.0f + 2.0f * PI * 80.0f / SAMPLE_RATE), x1 = 0, y1 = 0;
  float step(float x) {
    y1 = a * (y1 + x - x1);
    x1 = x;
    return y1;
  }
};

// I2S から溜まっている分を読み、16bit モノラルにして out に足す。読めたサンプル数を返す
static size_t mic_read(std::vector<int16_t>& out, HighPass& hp, uint32_t wait_ms) {
  static int32_t raw[512];
  size_t got = 0;
  // タイムアウトでも途中まで読めた分は got に入るので、戻り値は見ない
  i2s_read(I2S_NUM_0, raw, sizeof(raw), &got, pdMS_TO_TICKS(wait_ms));
  size_t n = 0;
  for (size_t i = 0; i < got / 4; i += 2) {  // [0] 側だけ
    float v = hp.step((float)(raw[i] >> 8));  // 上位 24bit が有効
    int32_t s = (int32_t)(v / 256.0f);        // 24bit → 16bit
    out.push_back((int16_t)constrain(s, -32768, 32767));
    n++;
  }
  return n;
}

static void mic_flush() {
  static int32_t raw[512];
  size_t got = 0;
  while (i2s_read(I2S_NUM_0, raw, sizeof(raw), &got, 0) == ESP_OK && got > 0) {
  }
}

static void wav_header(uint8_t* h, uint32_t data_bytes) {
  auto le32 = [](uint8_t* p, uint32_t v) {
    for (int i = 0; i < 4; i++) p[i] = v >> (8 * i);
  };
  auto le16 = [](uint8_t* p, uint16_t v) {
    p[0] = v;
    p[1] = v >> 8;
  };
  memcpy(h, "RIFF", 4);
  le32(h + 4, 36 + data_bytes);
  memcpy(h + 8, "WAVEfmt ", 8);
  le32(h + 16, 16);
  le16(h + 20, 1);  // PCM
  le16(h + 22, 1);  // モノラル
  le32(h + 24, SAMPLE_RATE);
  le32(h + 28, SAMPLE_RATE * 2);
  le16(h + 32, 2);
  le16(h + 34, 16);
  memcpy(h + 36, "data", 4);
  le32(h + 40, data_bytes);
}

static void service_while_blocked();

static bool parse_url(const String& url, String& host, uint16_t& port, String& path) {
  if (!url.startsWith("http://")) return false;
  String rest = url.substring(7);
  int slash = rest.indexOf('/');
  String hostport = slash < 0 ? rest : rest.substring(0, slash);
  path = slash < 0 ? "/" : rest.substring(slash);
  int colon = hostport.indexOf(':');
  host = colon < 0 ? hostport : hostport.substring(0, colon);
  port = colon < 0 ? 80 : hostport.substring(colon + 1).toInt();
  return host.length() > 0;
}

// 決定ボタンが押されるのを待つ。待っている間も MQTT とボタンは回す
static bool wait_ok(bool pressed, uint32_t timeout_ms) {
  uint32_t t0 = millis();
  while (ok_pressed() != pressed) {
    if (millis() - t0 > timeout_ms) return false;
    service_while_blocked();
    delay(5);
  }
  return true;
}

// 録音して /api/voice へ送る。録りながら送るので RAM は数十KB で済む（TODO.md H）。
// サーバーは Content-Length を固定で受けるので、離したあとは無音を詰めて規定長まで送る
// （後ろの無音は stt.py の VAD が切る）。戻り値: サーバーの JSON / 失敗時は error を詰める
static void record_and_upload(const String& url, float max_sec, float wait_sec, float upload_timeout,
                              JsonDocument& result) {
  String host, path;
  uint16_t port;
  if (!parse_url(url, host, port, path)) {
    result["error"] = "bad url";
    return;
  }
  uint32_t wait_ms = wait_sec * 1000;
  // メニューの「決定」を押した指がまだ載っていることがある。いったん離されるのを待つ
  if (ok_pressed()) {
    logf("[REC] 決定がまだ押されています。離すのを待ちます");
    if (!wait_ok(false, wait_ms)) {
      result["error"] = "no audio";
      return;
    }
  }
  if (!wait_ok(true, wait_ms)) {
    logf("[REC] 決定ボタンが押されませんでした");
    result["error"] = "no audio";
    return;
  }

  // 押し間違いを捨てるため、最初の REC_MIN_MS ぶんは手元に溜めてから送り始める
  mic_flush();
  HighPass hp;
  std::vector<int16_t> pre;
  pre.reserve(SAMPLE_RATE * REC_MIN_MS / 1000 + 512);
  uint32_t started = millis();
  logf("[REC] 録音中…");
  while (millis() - started < REC_MIN_MS) {
    mic_read(pre, hp, 20);
    buttons_scan();
    if (!ok_pressed()) {
      logf("[REC] 短すぎるので捨てます (%lums)", millis() - started);
      result["error"] = "no audio";
      return;
    }
  }

  const uint32_t data_bytes = (uint32_t)(max_sec * SAMPLE_RATE) * 2;
  WiFiClient http;
  if (!http.connect(host.c_str(), port, 5000)) {
    logf("[REC] %s:%u へ接続できません", host.c_str(), port);
    result["error"] = "upload failed";
    return;
  }
  http.setNoDelay(false);
  http.printf("POST %s HTTP/1.1\r\nHost: %s:%u\r\nContent-Type: audio/wav\r\nContent-Length: %lu\r\n"
              "Connection: close\r\n\r\n",
              path.c_str(), host.c_str(), port, (unsigned long)(44 + data_bytes));
  uint8_t header[44];
  wav_header(header, data_bytes);
  http.write(header, sizeof(header));

  uint32_t sent = 0;
  auto send = [&](const int16_t* p, size_t n) {
    size_t bytes = min((uint32_t)(n * 2), data_bytes - sent);
    if (bytes) sent += http.write((const uint8_t*)p, bytes);
  };
  send(pre.data(), pre.size());
  pre.clear();
  pre.shrink_to_fit();

  std::vector<int16_t> chunk;
  chunk.reserve(512);
  uint32_t last_mqtt = millis();
  while (sent < data_bytes && ok_pressed() && http.connected()) {
    chunk.clear();
    mic_read(chunk, hp, 20);
    send(chunk.data(), chunk.size());
    buttons_scan();
    // 録音は最長30秒続くので、キープアライブが切れないよう時々 MQTT を回す
    if (millis() - last_mqtt > 1000 && mqtt.connected()) {
      last_mqtt = millis();
      mqtt.loop();
    }
  }
  logf("[REC] 録音終了 %.1f秒", (millis() - started) / 1000.0);

  // 規定長まで無音を詰める
  static const int16_t zeros[512] = {};
  while (sent < data_bytes && http.connected()) {
    uint32_t before = sent;
    send(zeros, 512);
    if (sent == before) break;
  }
  if (sent < data_bytes) {
    logf("[REC] 送信が途中で切れました (%lu / %lu)", sent, data_bytes);
    result["error"] = "upload failed";
    http.stop();
    return;
  }

  // 応答を待つ。文字起こしに時間がかかるので、その間も MQTT を回して切断されないようにする
  uint32_t t0 = millis();
  String resp;
  while (millis() - t0 < upload_timeout * 1000) {
    while (http.available()) resp += (char)http.read();
    if (!http.connected() && !http.available()) break;
    service_while_blocked();
    delay(20);
  }
  http.stop();
  int status = resp.substring(resp.indexOf(' ') + 1).toInt();
  int body_at = resp.indexOf("\r\n\r\n");
  String body = body_at < 0 ? "" : resp.substring(body_at + 4);
  if (status != 200) {
    logf("[REC] サーバーがエラーを返しました: %d %s", status, body.substring(0, 120).c_str());
    result["error"] = "upload failed";
    return;
  }
  JsonDocument res;
  if (deserializeJson(res, body)) {
    logf("[REC] 応答がJSONではありません");
    result["error"] = "upload failed";
    return;
  }
  result["answer"] = (bool)(res["ok"] | false);
  result["text"] = res["text"];
  result["task_id"] = res["task_id"];
  result["error"] = res["error"];
}

// ------------------------------------------------------------ 下りの指示

static JsonDocument pending_record;  // record は長くブロックするので loop() 側で処理する
static bool record_pending = false;
static bool restart_pending = false;
static String restart_request_id;

static void on_message(char* topic, byte* payload, unsigned int len) {
  JsonDocument p;
  if (deserializeJson(p, payload, len)) {
    logf("[cmd] 壊れたペイロード");
    return;
  }
  String cmd = json_str(p["cmd"]);
  if (cmd == "display") {
    render_display(json_lines(p["lines"]), json_str(p["badge"]));
  } else if (cmd == "led") {
    set_led(p["state"] | "idle");
  } else if (cmd == "confirm") {
    start_ask(p, true);
  } else if (cmd == "choice") {
    start_ask(p, false);
  } else if (cmd == "record") {
    if (record_pending) {
      JsonDocument r;
      r["request_id"] = json_str(p["request_id"]);
      r["answer"] = false;
      r["error"] = "busy";
      publish_reply(r);
      return;
    }
    pending_record = p;
    record_pending = true;
  } else if (cmd == "restart") {
    restart_request_id = json_str(p["request_id"]);
    restart_pending = true;
  } else if (cmd == "ping") {
    JsonDocument r;
    r["request_id"] = json_str(p["request_id"]);
    r["answer"] = "pong";
    publish_reply(r);
  } else {
    logf("[cmd] 未知のコマンド: %s", cmd.c_str());
  }
}

static void handle_record() {
  if (screen_dirty) render();  // record の直前に届いた「録音待ち」の画面を先に出す
  JsonDocument p = pending_record;
  JsonDocument r;
  r["request_id"] = json_str(p["request_id"]);
  r["answer"] = false;
  String url = json_str(p["url"]);
  if (!url.length()) {
    r["error"] = "no url";
  } else {
    record_and_upload(url, p["max_sec"] | REC_MAX_SEC, p["wait_sec"] | 30.0f, p["upload_timeout"] | 90.0f, r);
  }
  publish_reply(r);
  record_pending = false;
}

static void handle_restart() {
  JsonDocument r;
  r["request_id"] = restart_request_id;
  r["answer"] = true;
  publish_reply(r);
  logf("[restart] 管理画面から再起動を指示されました");
  render_display({"再起動しています", "しばらくお待ちください"});
  set_led("offline");
  render();
  // 正常な切断では LWT が出ないので、自分で offline を出してから切る
  JsonDocument off;
  off["device_id"] = device_id;
  off["online"] = false;
  publish_json(status_topic, off, true);
  for (int i = 0; i < 20; i++) {
    mqtt.loop();
    delay(50);
  }
  mqtt.disconnect();
  delay(200);
  ESP.restart();
}

// ------------------------------------------------------------ Wi-Fi・ブローカー

static void wifi_connect(int ap) {
  wifi_ap = ap;
  WiFi.begin(WIFI_APS[ap].ssid, WIFI_APS[ap].pass);
  logf("[WiFi] %s に接続します", WIFI_APS[ap].ssid);
}

static void wifi_begin() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setHostname(device_id.c_str());
  wifi_connect(0);
}

// 切れていたら繋ぎ直す。setAutoReconnect が拾わない場合（起動時に AP が無かった等）の保険
static void wifi_service() {
  static bool was_connected = false;
  static uint32_t last_try = 0;
  bool now = WiFi.status() == WL_CONNECTED;
  if (now != was_connected) {
    was_connected = now;
    if (now) logf("[WiFi] %s に接続しました IP=%s RSSI=%d", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
    else logf("[WiFi] 切断されました");
    screen_dirty = true;  // 下部の接続表示を更新
  }
  if (!now && millis() - last_try > 10000) {
    last_try = millis();
    WiFi.disconnect();
    wifi_connect((wifi_ap + 1) % WIFI_AP_COUNT);
  }
}

// LAN へブロードキャストし、app.py から自分のIPを教えてもらう
static bool discover_broker(String& host, uint16_t& port) {
  WiFiUDP udp;
  if (!udp.begin(0)) return false;
  udp.beginPacket(IPAddress(255, 255, 255, 255), DISCOVERY_PORT);
  udp.write((const uint8_t*)DISCOVERY_REQUEST, strlen(DISCOVERY_REQUEST));
  udp.endPacket();
  uint32_t t0 = millis();
  bool found = false;
  while (millis() - t0 < DISCOVERY_TIMEOUT_MS && !found) {
    if (udp.parsePacket() > 0) {
      char buf[256];
      int n = udp.read(buf, sizeof(buf) - 1);
      buf[max(n, 0)] = 0;
      JsonDocument info;
      if (!deserializeJson(info, buf) && info["host"].is<const char*>()) {
        host = info["host"].as<const char*>();
        port = info["port"] | 1883;
        found = true;
      }
    }
    buttons_scan();
    delay(10);
  }
  udp.stop();
  return found;
}

static bool connect_broker() {
  String host;
  uint16_t port = 0;
  if (discover_broker(host, port)) {
    logf("[discovery] ブローカー %s:%u", host.c_str(), port);
  } else {
    host = prefs.isKey("host") ? prefs.getString("host", "") : "";  // 無いキーを読むとエラーログが出る
    port = prefs.getUShort("port", 1883);
    if (!host.length()) {
      logf("[discovery] ブローカーが見つかりません（PC側の app.py は起動していますか？）");
      return false;
    }
    logf("[discovery] 応答なし。前回のブローカー %s:%u を試します", host.c_str(), port);
  }
  broker_host = host;  // setServer は文字列を保持しないので、こちらで持ち続ける
  mqtt.setServer(broker_host.c_str(), port);
  JsonDocument will;
  will["device_id"] = device_id;
  will["online"] = false;
  String will_s;
  serializeJson(will, will_s);
  // 電源断やネット切断で予告なく落ちた場合、ブローカーが代理で offline を配信する
  if (!mqtt.connect(device_id.c_str(), status_topic.c_str(), 1, true, will_s.c_str())) {
    logf("[MQTT] %s:%u へ接続できません (state=%d)", host.c_str(), port, mqtt.state());
    return false;
  }
  broker_port = port;
  if (!prefs.isKey("host") || prefs.getString("host", "") != host || prefs.getUShort("port", 0) != port) {
    prefs.putString("host", host);
    prefs.putUShort("port", port);
  }
  logf("[MQTT] connected to %s:%u as %s", host.c_str(), port, device_id.c_str());
  // 購読を先に済ませてから online を出す。逆だと、サーバーが online を受けて即座に
  // 送ってくる本来の表示（機材名など）が購読前に届いて捨てられ、暫定表示のまま残る
  mqtt.subscribe(cmd_topic.c_str(), 1);
  // 機材に紐付いていればサーバーが直後に本来の表示を送ってくるので、それまでの暫定表示
  set_led("idle");
  render_display({"社員証をタッチしてください"});
  publish_online();
  return true;
}

static void mqtt_service() {
  static bool was_connected = false;
  static uint32_t last_try = 0, last_beat = 0;
  if (mqtt.connected()) {
    mqtt.loop();
    if (millis() - last_beat > HEARTBEAT_MS) {
      last_beat = millis();
      publish_online();  // 誰も機材に触らなくてもオンラインだと分かるように
    }
    was_connected = true;
    return;
  }
  if (was_connected) {
    was_connected = false;
    logf("[MQTT] 切断を検知。ブローカーを再探索します");
    if (ask.active) {
      ask.active = false;
      screen.choice.active = false;
    }
    set_led("offline");
    render_display({"Gemmba " + device_id, "サーバーに再接続しています…"});
  }
  if (WiFi.status() != WL_CONNECTED) return;
  if (last_try && millis() - last_try < RETRY_MS) return;
  last_try = millis();
  if (connect_broker()) {
    was_connected = true;
    last_beat = millis();
  }
}

// ------------------------------------------------------------ NFC（RC522）

// 初期化して VersionReg が読めるまで繰り返す。電源投入直後は 0x00 が返ることがある。
// 0x82 の互換チップは WUPA がタイムアウトした直後もしばらく 0x00 を返すので、
// 生死の判定は PCD_Init の直後に限る（rc522_touch で確認済み）
static bool rc522_begin() {
  for (int i = 0; i < 10; i++) {
    rfid.PCD_Init();
    uint8_t v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
    if (v != 0x00 && v != 0xFF) {
      rfid.PCD_SetAntennaGain(MFRC522::RxGain_max);
      return true;
    }
    delay(100);
  }
  return false;
}

static void send_tag(const String& uid) {
  JsonDocument doc;
  doc["device_id"] = device_id;
  doc["tag_id"] = uid;
  time_t t = time(nullptr);
  doc["timestamp"] = t > 1600000000 ? (double)t : millis() / 1000.0;
  publish_json(data_topic, doc);
}

// カードが1回タッチされるごとに1回だけ送る。離脱は「一定時間カードが見えないこと」で判断する
static void touch_service() {
  static bool ready = false, holding = false, reported = false;
  static uint32_t last_poll = 0, last_seen = 0, last_check = 0;
  uint32_t now = millis();
  if (now - last_poll < TOUCH_POLL_MS) return;
  last_poll = now;

  // 未初期化・カードが無い間は 5 秒ごとに RC522 の生死を確かめる（電源投入直後の取りこぼし対策）
  if (!ready || (!holding && now - last_check > 5000)) {
    last_check = now;
    bool ok = rc522_begin();
    // 状態が変わったときと、起動して最初の判定のときだけ出す
    if (ok != ready || !reported) logf(ok ? "[NFC] RC522 を初期化しました" : "[NFC] RC522 が応答しません。配線を確認してください");
    reported = true;
    ready = ok;
    if (!ready) return;
  }

  byte atqa[2];
  byte len = sizeof(atqa);
  // WUPA は HALT 中のカードも起こすので、かざしっぱなしでも毎回読める
  if (rfid.PICC_WakeupA(atqa, &len) == MFRC522::STATUS_OK && rfid.PICC_ReadCardSerial()) {
    String uid;
    for (byte i = 0; i < rfid.uid.size; i++) {
      char b[3];
      sprintf(b, "%02x", rfid.uid.uidByte[i]);  // ラズパイ版（bytes.hex()）と同じ小文字
      uid += b;
    }
    rfid.PICC_HaltA();
    last_seen = now;
    if (!holding) {
      holding = true;
      logf("Tag ID: %s", uid.c_str());
      if (mqtt.connected()) send_tag(uid);
      else logf("[NFC] ブローカー未接続のため送れません");
    }
  } else if (holding && now - last_seen >= TOUCH_RELEASE_MS) {
    holding = false;
    logf("[NFC] カードが離れました");
  }
}

// ------------------------------------------------------------ メイン

// 録音の前後など長く待つ処理の中から呼ぶ。MQTT・ボタン・画面を回す（NFC は止める）。
// サーバーは録音待ちや「文字にしています」の画面をこの間に送ってくる
static void service_while_blocked() {
  if (mqtt.connected()) mqtt.loop();
  buttons_scan();
  if (screen_dirty) render();
}

static String make_device_id() {
  if (strlen(GEMMBA_DEVICE_ID)) return GEMMBA_DEVICE_ID;
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[16];
  snprintf(buf, sizeof(buf), "esp-%02x%02x%02x", mac[3], mac[4], mac[5]);
  return buf;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_BLUE, OUTPUT);
  pinMode(PIN_RC522_SS, OUTPUT);
  digitalWrite(PIN_RC522_SS, HIGH);  // LCD の初期化中に RC522 が反応しないように
  buttons_begin();

  WiFi.mode(WIFI_STA);
  device_id = make_device_id();
  char sid[9];
  snprintf(sid, sizeof(sid), "%08lx", (unsigned long)esp_random());
  session_id = sid;
  status_topic = "pi/" + device_id + "/status";
  data_topic = "pi/" + device_id + "/data";
  cmd_topic = "pi/" + device_id + "/cmd";
  reply_topic = "pi/" + device_id + "/reply";
  Serial.printf("\n===== Gemmba %s (session %s) =====\n", device_id.c_str(), session_id.c_str());

  lcd.init();
  // 横長 320x240。esp-0e5310 と esp-af4828 は筐体への取り付け向きが逆なので上下反転（3 = 1 の180度回転）
  lcd.setRotation(device_id == "esp-0e5310" || device_id == "esp-af4828" ? 3 : 1);
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI);
  set_led("offline");
  render_display({"Gemmba " + device_id, "Wi-Fi に接続しています…"});
  render();

  prefs.begin("gemmba", false);
  mqtt.setBufferSize(4096);
  mqtt.setKeepAlive(KEEPALIVE_SEC);
  mqtt.setSocketTimeout(5);
  mqtt.setCallback(on_message);
  mic_begin();
  wifi_begin();
  configTime(9 * 3600, 0, "ntp.nict.jp", "pool.ntp.org");
}

void loop() {
  static bool announced = false;
  wifi_service();
  if (WiFi.status() == WL_CONNECTED && !announced && !mqtt.connected()) {
    announced = true;
    render_display({"Gemmba " + device_id, "ブローカーを探しています…"});
  }
  mqtt_service();
  buttons_scan();
  ask_service();
  if (record_pending) handle_record();
  if (restart_pending) handle_restart();
  touch_service();
  if (screen_dirty) render();
  delay(5);
}
