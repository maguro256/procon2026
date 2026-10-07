// Adafruit_ILI9341 の公式サンプル graphicstest を、ピン番号だけ今の配線に合わせたもの。
// 自作の初期化コードを一切使わずに表示を確かめるため。
#ifdef ADAFRUIT_TEST

#include <Arduino.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

#define TFT_CS 5
#define TFT_DC 26
#define TFT_RST 17
// SCK=18 MOSI=23 MISO=19 は ESP32 の標準 SPI ピンなので指定不要

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== Adafruit_ILI9341 graphicstest =====");
  tft.begin();
  Serial.printf("Display Power Mode: 0x%02X\n", tft.readcommand8(ILI9341_RDMODE));
  Serial.printf("MADCTL Mode:        0x%02X\n", tft.readcommand8(ILI9341_RDMADCTL));
  Serial.printf("Pixel Format:       0x%02X\n", tft.readcommand8(ILI9341_RDPIXFMT));
  tft.setRotation(1);
}

void loop() {
  static const struct { uint16_t c; const char* name; } COLORS[] = {
      {ILI9341_RED, "RED"}, {ILI9341_GREEN, "GREEN"}, {ILI9341_BLUE, "BLUE"},
      {ILI9341_WHITE, "WHITE"}, {ILI9341_BLACK, "BLACK"},
  };
  for (auto& c : COLORS) {
    tft.fillScreen(c.c);
    Serial.printf("[TEST] fillScreen %s\n", c.name);
    delay(2000);
  }
  tft.fillScreen(ILI9341_BLACK);
  tft.setCursor(0, 0);
  tft.setTextColor(ILI9341_WHITE);  tft.setTextSize(3);
  tft.println("Hello World!");
  tft.setTextColor(ILI9341_YELLOW); tft.setTextSize(2);
  tft.println(1234.56);
  tft.setTextColor(ILI9341_RED);    tft.setTextSize(3);
  tft.println(0xDEADBEEF, HEX);
  Serial.println("[TEST] text");
  delay(5000);
}

#endif
