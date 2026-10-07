// RC522 (MFRC522) 動作確認。LCD と同じ VSPI に CS 違いで相乗りする。
// pio run -e rc522_test -t upload
//
// 配線:
//   3.3V=3V3 GND=GND SDA(SS)=GPIO4 SCK=GPIO18 MOSI=GPIO23 MISO=GPIO19 RST=GPIO22 IRQ=未接続
//
// 判定:
//   VersionReg が 0x91/0x92（互換チップなら 0x88 など）で毎回同じ → SPI 配線 OK
//   0x00 / 0xFF / 毎回違う → MISO・SS・電源を確認

#include <Arduino.h>
#include <SPI.h>
#include <MFRC522.h>

static constexpr int PIN_SCK = 18;
static constexpr int PIN_MISO = 19;
static constexpr int PIN_MOSI = 23;
static constexpr int PIN_SS = 4;
static constexpr int PIN_RST = 22;
static constexpr int PIN_LCD_CS = 5;  // LCD を非選択にしておくため

MFRC522 rfid(PIN_SS, PIN_RST);

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n===== RC522 test =====");

  pinMode(PIN_LCD_CS, OUTPUT);
  digitalWrite(PIN_LCD_CS, HIGH);
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI);
  rfid.PCD_Init();
  delay(10);

  Serial.print("VersionReg x5:");
  for (int i = 0; i < 5; i++) {
    Serial.printf(" 0x%02X", rfid.PCD_ReadRegister(MFRC522::VersionReg));
  }
  Serial.println();

  // 内蔵セルフテスト（終わると再初期化が必要）
  bool self_ok = rfid.PCD_PerformSelfTest();
  Serial.printf("SelfTest: %s\n", self_ok ? "OK" : "NG");
  rfid.PCD_Init();

  Serial.printf("TxControlReg=0x%02X (下位2bitが11ならアンテナON)\n",
                rfid.PCD_ReadRegister(MFRC522::TxControlReg));
  Serial.println("カードをかざしてください");
}

void loop() {
  // 0.5秒ごとに WUPA（停止中のカードも起こす呼びかけ）を送り、応答の状態が変わったら出す。
  // TIMEOUT=何も返ってこない / COLLISION・CRC_WRONG など=返事はあるが崩れている
  // 注意: 0x82 の互換チップは、WUPA が TIMEOUT になった直後しばらくレジスタが全部 0x00 で
  // 読める（次の WUPA は普通に通る）。ここで VersionReg を見て「死んだ」と判定しないこと。
  static uint32_t last_probe = 0;
  static int last_status = -1;
  if (millis() - last_probe > 500) {
    last_probe = millis();
    byte atqa[2];
    byte len = sizeof(atqa);
    MFRC522::StatusCode st = rfid.PICC_WakeupA(atqa, &len);
    if (st != last_status) {
      last_status = st;
      Serial.printf("[WUPA] %s", MFRC522::GetStatusCodeName(st));
      if (st == MFRC522::STATUS_OK) Serial.printf(" ATQA=%02X%02X", atqa[1], atqa[0]);
      Serial.println();
    }
    if (st == MFRC522::STATUS_OK && rfid.PICC_ReadCardSerial()) goto print_uid;
    return;
  }
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;
print_uid:
  Serial.print("UID: ");
  for (byte i = 0; i < rfid.uid.size; i++) Serial.printf("%02X", rfid.uid.uidByte[i]);
  Serial.printf("  (%d bytes, %s)\n", rfid.uid.size,
                MFRC522::PICC_GetTypeName(rfid.PICC_GetType(rfid.uid.sak)));
  rfid.PICC_HaltA();
}
