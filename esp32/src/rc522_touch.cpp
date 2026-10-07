// RC522 のタッチ反応確認。カードを重ねた／離した瞬間を出す。
// pio run -e rc522_touch -t upload
//
// 配線は rc522_test.cpp と同じ。受信ゲインは最大（48dB）。
//
// 出力:
//   [TOUCH] かざした UID=... 前回離してから x.x秒
//   [TOUCH] 離した   UID=... かざしていた x.x秒 読取 n回 / 失敗 m回
// 一度読めたあと LOST_MS のあいだ読めなければ「離した」とみなす（磁界の揺らぎで
// 1〜2回読み損ねても離れた扱いにしないため。ラズパイ版と同じ考え方）。

#include <Arduino.h>
#include <SPI.h>
#include <MFRC522.h>

static constexpr int PIN_SS = 4;
static constexpr int PIN_RST = 22;
static constexpr int PIN_LCD_CS = 5;
static constexpr uint32_t POLL_MS = 30;
static constexpr uint32_t LOST_MS = 500;

MFRC522 rfid(PIN_SS, PIN_RST);

// 初期化して VersionReg が読めるまで繰り返す。電源投入直後は 0x00 が返ることがある。
// WUPA が TIMEOUT した直後もしばらく 0x00 で読めるので、判定は PCD_Init の直後に限る。
static bool rc522_begin() {
  for (int i = 0; i < 20; i++) {
    rfid.PCD_Init();
    uint8_t v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
    if (v != 0x00 && v != 0xFF) {
      rfid.PCD_SetAntennaGain(MFRC522::RxGain_max);
      if (i > 0) Serial.printf("[RC522] %d回目の初期化で応答しました\n", i + 1);
      return true;
    }
    delay(100);
  }
  return false;
}

static String uid_str() {
  String s;
  for (byte i = 0; i < rfid.uid.size; i++) {
    char b[3];
    sprintf(b, "%02X", rfid.uid.uidByte[i]);
    s += b;
  }
  return s;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== RC522 touch =====");
  pinMode(PIN_LCD_CS, OUTPUT);
  digitalWrite(PIN_LCD_CS, HIGH);
  SPI.begin(18, 19, 23);
  if (!rc522_begin()) Serial.println("[RC522] 応答しません。配線を確認してください");
  Serial.printf("VersionReg=0x%02X AntennaGain=0x%02X\n",
                rfid.PCD_ReadRegister(MFRC522::VersionReg), rfid.PCD_GetAntennaGain());
  Serial.println("カードを重ねたり離したりしてください");
}

void loop() {
  static bool present = false;
  static String cur_uid;
  static uint32_t since = 0, last_seen = 0, last_left = 0, reads = 0, fails = 0;

  // WUPA は HALT 中のカードも起こすので、かざしっぱなしでも毎回読める
  byte atqa[2];
  byte len = sizeof(atqa);
  uint32_t now = millis();
  if (rfid.PICC_WakeupA(atqa, &len) == MFRC522::STATUS_OK) {
    if (rfid.PICC_ReadCardSerial()) {
      String uid = uid_str();
      if (!present) {
        present = true;
        cur_uid = uid;
        since = now;
        reads = 0;
        fails = 0;
        if (last_left) Serial.printf("[TOUCH] かざした UID=%s 前回離してから %.1f秒\n", uid.c_str(), (now - last_left) / 1000.0);
        else Serial.printf("[TOUCH] かざした UID=%s\n", uid.c_str());
      } else if (uid != cur_uid) {
        Serial.printf("[TOUCH] 別のカード UID=%s（前 %s）\n", uid.c_str(), cur_uid.c_str());
        cur_uid = uid;
      }
      reads++;
      last_seen = now;
    } else if (present) {
      fails++;
    }
    rfid.PICC_HaltA();
  } else if (present) {
    fails++;
  }

  if (present && now - last_seen > LOST_MS) {
    present = false;
    last_left = last_seen;
    Serial.printf("[TOUCH] 離した   UID=%s かざしていた %.1f秒 読取 %lu回 / 失敗 %lu回\n",
                  cur_uid.c_str(), (last_seen - since) / 1000.0, reads, fails);
  }
  // カードが無い状態が続いたら、RC522 が生きているか確かめて必要なら初期化し直す
  static uint32_t last_check = 0;
  if (!present && now - last_seen > 5000 && now - last_check > 5000) {
    last_check = now;
    if (!rc522_begin()) Serial.println("[RC522] 応答しません。配線を確認してください");
  }
  delay(POLL_MS);
}
