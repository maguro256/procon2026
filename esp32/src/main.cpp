// ILI9341 表示テスト（ESP32 版）
//
// ラズパイで色が出た Python のテスト（spidev + gpiozero）を1行ずつ移したもの。
// 送り方も Python と同じにしている:
//   - spidev の writebytes2 は1回ごとに CS を上げ下げするので、コマンド1バイトと
//     データを別々の CS 区間で送る
//   - SPI 8MHz / モード0、MISO は使わない（読み返しもしない）
//
// 配線:
//   VCC=3V3 GND=GND LED=3V3
//   CS=GPIO5 RESET=GPIO17 DC=GPIO26 SDI(MOSI)=GPIO23 SCK=GPIO18（SDO は使わない）
//   RC522 が同じバスに CS=GPIO4 で相乗りしているので、GPIO4 は High にしておく

#ifndef LCD_PROBE

#include <Arduino.h>
#include <SPI.h>

static constexpr int PIN_SCK = 18;
static constexpr int PIN_MOSI = 23;
static constexpr int PIN_CS = 5;
static constexpr int PIN_DC = 26;
static constexpr int PIN_RST = 17;  // -1 にすると RESET 線を使わずコマンドだけでリセットする
static constexpr int PIN_RC522_SS = 4;  // 同じ SPI バスの RC522 を非選択にしておく

static constexpr int W = 320, H = 240;

static const SPISettings SPI_CFG(8000000, MSBFIRST, SPI_MODE0);

// spidev の writebytes2() 相当: CS を下げて送り、上げる
static void writebytes(const uint8_t* buf, size_t n) {
  SPI.beginTransaction(SPI_CFG);
  digitalWrite(PIN_CS, LOW);
  SPI.writeBytes(buf, n);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

static void cmd(uint8_t c, std::initializer_list<uint8_t> data = {}) {
  digitalWrite(PIN_DC, LOW);
  writebytes(&c, 1);
  if (data.size()) {
    digitalWrite(PIN_DC, HIGH);
    writebytes(data.begin(), data.size());
  }
}

static void fill_rect(int x, int y, int w, int h, uint16_t color) {
  cmd(0x2A, {(uint8_t)(x >> 8), (uint8_t)(x & 0xFF), (uint8_t)((x + w - 1) >> 8),
             (uint8_t)((x + w - 1) & 0xFF)});
  cmd(0x2B, {(uint8_t)(y >> 8), (uint8_t)(y & 0xFF), (uint8_t)((y + h - 1) >> 8),
             (uint8_t)((y + h - 1) & 0xFF)});
  cmd(0x2C);
  digitalWrite(PIN_DC, HIGH);
  // Python は全画素を1回の writebytes2 で送る（CS は最後まで下げたまま）
  static uint8_t line[2 * W];
  for (int i = 0; i < w; i++) {
    line[2 * i] = color >> 8;
    line[2 * i + 1] = color & 0xFF;
  }
  SPI.beginTransaction(SPI_CFG);
  digitalWrite(PIN_CS, LOW);
  for (int r = 0; r < h; r++) SPI.writeBytes(line, 2 * w);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== ILI9341 表示テスト（Python 版の移植） =====");

  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_RC522_SS, OUTPUT);
  digitalWrite(PIN_RC522_SS, HIGH);
  pinMode(PIN_DC, OUTPUT);
  SPI.begin(PIN_SCK, -1, PIN_MOSI, -1);

  if (PIN_RST >= 0) {
    pinMode(PIN_RST, OUTPUT);
    digitalWrite(PIN_RST, HIGH); delay(5);
    digitalWrite(PIN_RST, LOW);  delay(20);
    digitalWrite(PIN_RST, HIGH);
  }
  delay(150);
  cmd(0x01); delay(150);  // ソフトウェアリセット

  cmd(0xEF, {0x03, 0x80, 0x02});
  cmd(0xCF, {0x00, 0xC1, 0x30});
  cmd(0xED, {0x64, 0x03, 0x12, 0x81});
  cmd(0xE8, {0x85, 0x00, 0x78});
  cmd(0xCB, {0x39, 0x2C, 0x00, 0x34, 0x02});
  cmd(0xF7, {0x20});
  cmd(0xEA, {0x00, 0x00});
  cmd(0xC0, {0x23});
  cmd(0xC1, {0x10});
  cmd(0xC5, {0x3E, 0x28});
  cmd(0xC7, {0x86});
  cmd(0x3A, {0x55});
  cmd(0xB1, {0x00, 0x18});
  cmd(0xB6, {0x08, 0x82, 0x27});
  cmd(0xF2, {0x00});
  cmd(0x26, {0x01});
  cmd(0xE0, {0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00});
  cmd(0xE1, {0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F});
  cmd(0x36, {0x28});
  cmd(0x11); delay(120);
  cmd(0x29); delay(20);
  Serial.println("[INIT] 初期化コマンドを送った");
}

void loop() {
  static const struct { uint16_t color; const char* name; } COLORS[] = {
      {0xF800, "赤"}, {0x07E0, "緑"}, {0x001F, "青"}, {0xFFFF, "白"}, {0x0000, "黒"},
  };
  static const uint16_t BARS[] = {0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000};

  for (auto& c : COLORS) {
    fill_rect(0, 0, W, H, c.color);
    Serial.printf("[TEST] 全面 %s\n", c.name);
    delay(2000);
  }
  for (int i = 0; i < 8; i++) fill_rect(i * W / 8, 0, W / 8, H, BARS[i]);
  fill_rect(0, 0, 24, 24, 0xF800);
  Serial.println("[TEST] カラーバー");
  delay(5000);
}

#endif  // LCD_PROBE
