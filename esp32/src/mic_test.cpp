// I2S MEMS マイク（INMP441 系）動作確認。左右両方を受けて、どちらに音が出ているかも見る。
// pio run -e mic_test -t upload
//
// 配線:
//   VDD=3V3 GND=GND SCK=GPIO32 WS=GPIO25 SD=GPIO33 L/R=GND
//
// 0.5秒ごとに左右それぞれの RMS / ピーク（dBFS）を出す。ラズパイ版と同じく 80Hz の
// ハイパスを通した値で、録り始めの DC ドリフトを除く。
// 目安（ラズパイ版の実測）: 無音の雑音 約-70dBFS、発話ピーク 約-26dBFS。
// 全サンプル 0 なら SD・電源・L/R を確認。

#include <Arduino.h>
#include <driver/i2s.h>
#include <math.h>

static constexpr int PIN_SCK = 32;
static constexpr int PIN_WS = 25;
static constexpr int PIN_SD = 33;
static constexpr int RATE = 16000;

// 1次ハイパス（80Hz）
struct HighPass {
  float a = 1.0f / (1.0f + 2.0f * PI * 80.0f / RATE), x1 = 0, y1 = 0;
  float step(float x) { y1 = a * (y1 + x - x1); x1 = x; return y1; }
};

struct Stat {
  double sum = 0, sum2 = 0;
  int32_t mn = INT32_MAX, mx = INT32_MIN;
  uint32_t n = 0, zeros = 0;
  void add(int32_t v) {
    sum += v; sum2 += (double)v * v; n++;
    if (v == 0) zeros++;
    mn = min(mn, v); mx = max(mx, v);
  }
  void print(const char* name) const {
    const double fs = 8388608.0;  // 2^23
    double mean = sum / n;
    double var = sum2 / n - mean * mean;
    double rms = sqrt(var > 0 ? var : 0);
    double peak = max(fabs(mx - mean), fabs(mn - mean));
    Serial.printf("%s rms=%6.1fdBFS peak=%6.1fdBFS dc=%8.0f zeros=%u/%u  ", name,
                  20 * log10(rms / fs + 1e-12), 20 * log10(peak / fs + 1e-12), mean, zeros, n);
  }
};

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== mic test =====");
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
  pins.bck_io_num = PIN_SCK;
  pins.ws_io_num = PIN_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_SD;
  esp_err_t e1 = i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr);
  esp_err_t e2 = i2s_set_pin(I2S_NUM_0, &pins);
  Serial.printf("i2s_driver_install=%d i2s_set_pin=%d\n", e1, e2);
}

void loop() {
  static int32_t buf[512];
  static HighPass hp[2];
  Stat ch[2];
  uint32_t t0 = millis();
  int32_t first = 0;
  bool got_first = false;
  while (millis() - t0 < 500) {
    size_t n = 0;
    i2s_read(I2S_NUM_0, buf, sizeof(buf), &n, portMAX_DELAY);
    for (size_t i = 0; i < n / 4; i++) {
      int32_t v = buf[i] >> 8;  // 上位24bitが有効
      if (!got_first) { first = buf[i]; got_first = true; }
      ch[i & 1].add((int32_t)hp[i & 1].step((float)v));
    }
  }
  ch[0].print("[0]");
  ch[1].print("[1]");
  Serial.printf("raw=0x%08X\n", (uint32_t)first);
}
