// ILI9341 にコマンドが届いているかを、レジスタを読み返して自動判定する
// （pio run -e lcd_probe -t upload）。目視に頼らずに送り方・速度の上限を調べる。
//
// 要: ILI9341 の SDO(MISO) → GPIO19
//
// 1. レジスタ: 調べたい送り方・速度で SLPOUT / COLMOD=0x55 / MADCTL=0xA8 / DISPON を送り、
//    低速のビットバンギングで 0x0A/0x0B/0x0C を読んで期待値 9C/A8/05 と比べる
// 2. 画素: 同じ送り方で塗り、画面メモリ(RAMRD)を低速で読み返して色が入ったかを見る
// 3. RESET 線: ハードリセットとソフトリセット(0x01)で 0x0A が 08 に戻るかを比べる
// 読み出しは常に低速なので、書き込みの成否だけを見ていることになる。
// リセットは RESET 線が効かない個体に備えてソフトリセットで行う。

#if defined(LCD_PROBE) && !defined(ADAFRUIT_TEST)

#include <Arduino.h>
#include <SPI.h>

static constexpr int PIN_SCK = 18;
static constexpr int PIN_MOSI = 23;
static constexpr int PIN_MISO = 19;
static constexpr int PIN_CS = 5;
static constexpr int PIN_DC = 26;
static constexpr int PIN_RST = 17;

enum Mode { BITBANG, HWSPI };
static Mode mode;
static uint32_t bb_half_us;  // ビットバンギングの半周期
static SPISettings hw_settings;

static void pins_gpio() {
  pinMode(PIN_SCK, OUTPUT);
  pinMode(PIN_MOSI, OUTPUT);
  pinMode(PIN_MISO, INPUT_PULLUP);  // 未接続なら 0xFF が読める
  pinMode(PIN_CS, OUTPUT);
  pinMode(PIN_DC, OUTPUT);
  digitalWrite(PIN_RST, HIGH);
  pinMode(PIN_RST, OUTPUT);
  digitalWrite(PIN_SCK, LOW);
  digitalWrite(PIN_CS, HIGH);
}

static uint8_t bb_xfer(uint8_t out) {
  uint8_t in = 0;
  for (int i = 7; i >= 0; i--) {
    digitalWrite(PIN_MOSI, (out >> i) & 1);
    if (bb_half_us) delayMicroseconds(bb_half_us);
    digitalWrite(PIN_SCK, HIGH);
    in = (in << 1) | digitalRead(PIN_MISO);
    if (bb_half_us) delayMicroseconds(bb_half_us);
    digitalWrite(PIN_SCK, LOW);
  }
  return in;
}

static void xfer(uint8_t b) {
  if (mode == HWSPI)
    SPI.transfer(b);
  else
    bb_xfer(b);
}

static void begin_write() {
  if (mode == HWSPI) {
    SPI.begin(PIN_SCK, -1, PIN_MOSI, -1);
    SPI.beginTransaction(hw_settings);
  }
}

static void end_write() {
  if (mode == HWSPI) {
    SPI.endTransaction();
    SPI.end();
  }
  pins_gpio();
}

static void w_cmd(uint8_t c, std::initializer_list<uint8_t> d = {}) {
  digitalWrite(PIN_DC, LOW);
  digitalWrite(PIN_CS, LOW);
  xfer(c);
  if (d.size()) {
    digitalWrite(PIN_DC, HIGH);
    for (uint8_t b : d) xfer(b);
  }
  digitalWrite(PIN_CS, HIGH);
}

// 常に低速ビットバンギングで、コマンド c のあと n バイト読む（skip バイトは捨てる）
static void slow_read_n(uint8_t c, uint8_t* out, int n, int skip = 0) {
  Mode saved_mode = mode;
  uint32_t saved = bb_half_us;
  mode = BITBANG;
  bb_half_us = 5;
  digitalWrite(PIN_DC, LOW);
  digitalWrite(PIN_CS, LOW);
  bb_xfer(c);
  digitalWrite(PIN_DC, HIGH);
  for (int i = 0; i < skip; i++) bb_xfer(0x00);
  for (int i = 0; i < n; i++) out[i] = bb_xfer(0x00);
  digitalWrite(PIN_CS, HIGH);
  bb_half_us = saved;
  mode = saved_mode;
}

static uint8_t slow_read(uint8_t c) {
  uint8_t v;
  slow_read_n(c, &v, 1);
  return v;
}

static void soft_reset() {
  Mode saved_mode = mode;
  uint32_t saved = bb_half_us;
  mode = BITBANG;
  bb_half_us = 5;
  pins_gpio();
  w_cmd(0x01);
  delay(150);
  bb_half_us = saved;
  mode = saved_mode;
}

static void hw_reset() {
  pins_gpio();
  digitalWrite(PIN_RST, LOW);
  delay(20);
  digitalWrite(PIN_RST, HIGH);
  delay(150);
}

static bool init_and_check(const char* label) {
  soft_reset();
  uint8_t pm0 = slow_read(0x0A);

  begin_write();
  // 電源・VCOM・ガンマ。raspi/ili9341.py の _INIT と同じ（ラズパイでこのモジュールが映った値）
  w_cmd(0xEF, {0x03, 0x80, 0x02});
  w_cmd(0xCF, {0x00, 0xC1, 0x30});
  w_cmd(0xED, {0x64, 0x03, 0x12, 0x81});
  w_cmd(0xE8, {0x85, 0x00, 0x78});
  w_cmd(0xCB, {0x39, 0x2C, 0x00, 0x34, 0x02});
  w_cmd(0xF7, {0x20});
  w_cmd(0xEA, {0x00, 0x00});
  w_cmd(0xC0, {0x23});
  w_cmd(0xC1, {0x10});
  w_cmd(0xC5, {0x3E, 0x28});
  w_cmd(0xC7, {0x86});
  w_cmd(0xB1, {0x00, 0x18});
  w_cmd(0xB6, {0x08, 0x82, 0x27});
  w_cmd(0xF2, {0x00});
  w_cmd(0x26, {0x01});
  w_cmd(0xE0, {0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00});
  w_cmd(0xE1, {0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F});
  w_cmd(0x11);  // SLPOUT
  delay(130);
  w_cmd(0x3A, {0x55});
  w_cmd(0x36, {0xA8});  // 横長 320x240
  w_cmd(0x29);          // DISPON
  end_write();
  delay(10);

  uint8_t pm = slow_read(0x0A), mad = slow_read(0x0B), pix = slow_read(0x0C);
  bool ok = pm == 0x9C && mad == 0xA8 && pix == 0x05;
  Serial.printf("  %-16s リセット後 0A=%02X | 初期化後 0A=%02X 0B=%02X 0C=%02X  %s\n", label, pm0,
                pm, mad, pix, ok ? "OK" : "NG");
  return ok;
}

static void set_window(int x0, int y0, int x1, int y1) {
  w_cmd(0x2A, {(uint8_t)(x0 >> 8), (uint8_t)x0, (uint8_t)(x1 >> 8), (uint8_t)x1});
  w_cmd(0x2B, {(uint8_t)(y0 >> 8), (uint8_t)y0, (uint8_t)(y1 >> 8), (uint8_t)y1});
}

static void fill(int w, int h, uint16_t color) {
  begin_write();
  set_window(0, 0, w - 1, h - 1);
  digitalWrite(PIN_DC, LOW);
  digitalWrite(PIN_CS, LOW);
  xfer(0x2C);
  digitalWrite(PIN_DC, HIGH);
  for (int i = 0; i < w * h; i++) {
    xfer(color >> 8);
    xfer(color & 0xFF);
  }
  digitalWrite(PIN_CS, HIGH);
  end_write();
}

// 左上から2画素を読み返す。1バイト目はダミー、以降は1画素3バイト（各色の上位6ビット）
static void read_pixels(uint8_t* rgb6) {
  Mode saved_mode = mode;
  mode = BITBANG;
  bb_half_us = 5;
  set_window(0, 0, 319, 239);
  mode = saved_mode;
  slow_read_n(0x2E, rgb6, 6, 1);
}

struct Step {
  const char* label;
  Mode mode;
  uint32_t hz;  // HW SPI のときだけ
  uint16_t color;
  const char* color_name;
};

static const Step STEPS[] = {
    {"bitbang ~100kHz", BITBANG, 0, 0xF800, "赤"},
    {"HW SPI 1MHz", HWSPI, 1000000, 0x07E0, "緑"},
    {"HW SPI 4MHz", HWSPI, 4000000, 0x001F, "青"},
    {"HW SPI 8MHz", HWSPI, 8000000, 0xFFE0, "黄"},
    {"HW SPI 16MHz", HWSPI, 16000000, 0xF81F, "紫"},
    {"HW SPI 40MHz", HWSPI, 40000000, 0x07FF, "水色"},
};

void setup() {
  Serial.begin(115200);
  // 同じ SPI バスの RC522（CS=GPIO4）を非選択にして MISO を手放させる
  pinMode(4, OUTPUT);
  digitalWrite(4, HIGH);
  delay(300);
}

// GPIO17 の状態ごとに、通信が通るかを比べる
static void rst_state_test() {
  Serial.println("\n=== GPIO17 の状態ごとの比較（期待 0A=9C 0B=A8 0C=05） ===");
  mode = BITBANG;
  bb_half_us = 5;
  struct { const char* name; int st; } STATES[] = {{"浮かせる", -1}, {"High", 1}, {"Low", 0}, {"浮かせる", -1}};
  for (auto& s : STATES) {
    // pins_gpio() は PIN_RST を High にするので、その後に上書きする
    pins_gpio();
    if (s.st < 0) pinMode(PIN_RST, INPUT);
    else { pinMode(PIN_RST, OUTPUT); digitalWrite(PIN_RST, s.st); }
    delay(200);
    w_cmd(0x01);  // SWRESET
    delay(150);
    uint8_t pm0 = slow_read(0x0A);
    w_cmd(0x11);
    delay(130);
    w_cmd(0x3A, {0x55});
    w_cmd(0x36, {0xA8});
    w_cmd(0x29);
    delay(10);
    uint8_t pm = slow_read(0x0A), mad = slow_read(0x0B), pix = slow_read(0x0C);
    Serial.printf("  GPIO17=%-6s リセット後 0A=%02X | 初期化後 0A=%02X 0B=%02X 0C=%02X  %s\n", s.name,
                  pm0, pm, mad, pix, (pm == 0x9C && mad == 0xA8 && pix == 0x05) ? "OK" : "NG");
  }
}

void loop() {
  rst_state_test();
  delay(3000);
  return;
  Serial.println("\n=== ILI9341 読み返しテスト ===");
  Serial.println("[画素] 塗ったあと画面メモリを読む。期待値は塗った色（各色の上位6ビット）");
  for (const Step& st : STEPS) {
    mode = st.mode;
    bb_half_us = 5;
    if (st.mode == HWSPI) hw_settings = SPISettings(st.hz, MSBFIRST, SPI_MODE0);
    bool reg_ok = init_and_check(st.label);

    // ビットバンギングは遅いので左上 40x40 だけ塗る
    int w = st.mode == BITBANG ? 40 : 320, h = st.mode == BITBANG ? 40 : 240;
    fill(w, h, st.color);
    uint8_t px[6];
    read_pixels(px);
    // 期待値: RGB565 を各色 6 ビット（上詰め）に直したもの
    uint8_t er = ((st.color >> 11) & 0x1F) << 3, eg = ((st.color >> 5) & 0x3F) << 2,
            eb = (st.color & 0x1F) << 3;
    auto near = [](uint8_t a, uint8_t b) { return (a & 0xF0) == (b & 0xF0); };
    // MADCTL の BGR ビットで R と B が入れ替わって読める場合も許す
    bool px_ok = near(px[1], eg) && ((near(px[0], er) && near(px[2], eb)) ||
                                     (near(px[0], eb) && near(px[2], er)));
    uint8_t pm_after = slow_read(0x0A);
    Serial.printf("      %s で塗った → 読み返し %02X %02X %02X / %02X %02X %02X（期待 %02X %02X %02X）"
                  " 0A=%02X  画素%s\n",
                  st.color_name, px[0], px[1], px[2], px[3], px[4], px[5], er, eg, eb, pm_after,
                  px_ok ? "OK" : "NG");
    (void)reg_ok;
    delay(2000);  // 目視用
  }

  // RESET が実際に繋がっている GPIO を探す: 1本ずつ Low にして、0A が 08 に戻ったら当たり。
  // 基板の印刷と GPIO 番号がずれている個体があったため（2026-10-04）
  Serial.println("[RESET探し] 各GPIOを20ms Lowにして手放す → 0A=08 になったピンが RESET");
  static const int CANDIDATES[] = {2, 4, 12, 13, 14, 15, 16, 17, 21, 22, 25, 27, 32, 33};
  mode = BITBANG;
  int found = -1;
  for (int p : CANDIDATES) {
    init_and_check("(準備)");
    pinMode(p, OUTPUT);
    digitalWrite(p, LOW);
    delay(20);
    pinMode(p, INPUT);
    delay(150);
    uint8_t pm = slow_read(0x0A);
    Serial.printf("  GPIO%-2d → 0A=%02X%s\n", p, pm, pm == 0x08 ? "  ← RESET はここ" : "");
    if (pm == 0x08 && found < 0) found = p;
  }
  Serial.printf("[RESET探し] 結果: %s%d\n", found >= 0 ? "GPIO" : "見つからない ", found);

  Serial.println("[RESET線]");
  mode = BITBANG;
  init_and_check("(準備)");
  hw_reset();
  uint8_t after_hw = slow_read(0x0A);
  init_and_check("(準備)");
  soft_reset();
  uint8_t after_sw = slow_read(0x0A);
  Serial.printf("  RESET線(GPIO17)でリセット → 0A=%02X %s\n", after_hw,
                after_hw == 0x08 ? "OK" : "NG: RESETが効いていない");
  Serial.printf("  コマンドでリセット       → 0A=%02X %s\n", after_sw,
                after_sw == 0x08 ? "OK" : "NG");
  delay(5000);
}

#endif
