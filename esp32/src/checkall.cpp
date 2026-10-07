// 全部品を同時に動かす確認（ラズパイ版 raspi/checkall.py の ESP32 版）。
// pio run -e checkall -t upload
//
// 配線:
//   RC522 : SS=GPIO4 RST=GPIO22 SCK=18 MOSI=23 MISO=19（LCD と SPI 共用、LCD CS=GPIO5）
//   マイク: SCK=GPIO32 WS=GPIO25 SD=GPIO33 L/R=GND
//   ボタン: 左=GPIO13 決定=GPIO14 右=GPIO27（GPIO → ボタン → GND、内部プルアップ）
//   LED   : 赤=GPIO21 青=GPIO16（GPIO → 抵抗 → LED → GND）
//
// LED は 赤→青→両方→消灯 を1秒ずつ繰り返す（目視）。ボタン・カードは押した/かざした時に出る。
// マイクは1秒ごとに RMS / ピーク（80Hz ハイパス後、dBFS）を出す。

#include <Arduino.h>
#include <SPI.h>
#include <MFRC522.h>
#include <driver/i2s.h>
#include <math.h>

static constexpr int PIN_LCD_CS = 5;
static constexpr int PIN_RC522_SS = 4, PIN_RC522_RST = 22;
static constexpr int PIN_MIC_SCK = 32, PIN_MIC_WS = 25, PIN_MIC_SD = 33;
static constexpr int PIN_LED_RED = 21, PIN_LED_BLUE = 16;
static const int BTN_PINS[3] = {13, 14, 27};
static const char* BTN_NAMES[3] = {"左", "決定", "右"};
static constexpr int RATE = 16000;

MFRC522 rfid(PIN_RC522_SS, PIN_RC522_RST);

static void mic_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_MIC_SCK;
  pins.ws_io_num = PIN_MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_SD;
  i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr);
  i2s_set_pin(I2S_NUM_0, &pins);
}

// 溜まっている分だけ読んで集計する（待たない）。チャンネル [0] だけ使う。
static void mic_poll() {
  static float a = 1.0f / (1.0f + 2.0f * PI * 80.0f / RATE), x1 = 0, y1 = 0;
  static double sum2 = 0;
  static float peak = 0;
  static uint32_t n = 0, zeros = 0, t0 = millis();
  static int32_t buf[256];
  size_t got = 0;
  while (i2s_read(I2S_NUM_0, buf, sizeof(buf), &got, 0) == ESP_OK && got > 0) {
    for (size_t i = 0; i < got / 4; i += 2) {
      int32_t v = buf[i] >> 8;
      if (v == 0) zeros++;
      y1 = a * (y1 + v - x1);
      x1 = v;
      sum2 += (double)y1 * y1;
      peak = max(peak, fabsf(y1));
      n++;
    }
  }
  if (millis() - t0 >= 1000 && n > 0) {
    const double fs = 8388608.0;
    Serial.printf("[MIC] rms=%6.1fdBFS peak=%6.1fdBFS zeros=%u/%u\n",
                  20 * log10(sqrt(sum2 / n) / fs + 1e-12), 20 * log10(peak / fs + 1e-12), zeros, n);
    sum2 = 0; peak = 0; n = 0; zeros = 0; t0 = millis();
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== checkall =====");

  pinMode(PIN_LCD_CS, OUTPUT);
  digitalWrite(PIN_LCD_CS, HIGH);
  SPI.begin(18, 19, 23);
  rfid.PCD_Init();
  Serial.printf("[RC522] VersionReg=0x%02X\n", rfid.PCD_ReadRegister(MFRC522::VersionReg));

  mic_begin();

  for (int p : BTN_PINS) pinMode(p, INPUT_PULLUP);
  delay(5);
  Serial.printf("[BTN] 起動時: 左=%d 決定=%d 右=%d（1=離している）\n",
                digitalRead(BTN_PINS[0]), digitalRead(BTN_PINS[1]), digitalRead(BTN_PINS[2]));

  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_BLUE, OUTPUT);
}

void loop() {
  // LED: 赤 → 青 → 両方 → 消灯
  static int led_step = -1;
  int step = (millis() / 1000) % 4;
  if (step != led_step) {
    led_step = step;
    static const char* names[4] = {"赤", "青", "両方", "消灯"};
    digitalWrite(PIN_LED_RED, step == 0 || step == 2);
    digitalWrite(PIN_LED_BLUE, step == 1 || step == 2);
    Serial.printf("[LED] %s\n", names[step]);
  }

  // ボタン: 20ms 以上同じ値が続いたら確定
  static int stable[3] = {1, 1, 1}, last_raw[3] = {1, 1, 1};
  static uint32_t changed_at[3] = {0, 0, 0};
  for (int i = 0; i < 3; i++) {
    int raw = digitalRead(BTN_PINS[i]);
    if (raw != last_raw[i]) { last_raw[i] = raw; changed_at[i] = millis(); }
    if (raw != stable[i] && millis() - changed_at[i] > 20) {
      stable[i] = raw;
      Serial.printf("[BTN] %s(GPIO%d) %s\n", BTN_NAMES[i], BTN_PINS[i], raw ? "離した" : "押した");
    }
  }

  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    Serial.print("[RC522] UID: ");
    for (byte i = 0; i < rfid.uid.size; i++) Serial.printf("%02X", rfid.uid.uidByte[i]);
    Serial.println();
    rfid.PICC_HaltA();
  }

  mic_poll();
}
