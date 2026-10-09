// 接続先の Wi-Fi。これを secrets.h にコピーして書き換える（secrets.h は git に入れない）。
// WIFI_SSID を優先し、WIFI_SSID2 を書けばつながらないときに交互に試す（省略可）。
#pragma once

#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-password"

// #define WIFI_SSID2 "your-second-ssid"
// #define WIFI_PASSWORD2 "your-second-password"
