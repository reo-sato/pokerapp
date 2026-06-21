// ccid_device.h — TinyUSB のカスタムクラスとして CCID を登録する層。
#pragma once

#include <stdint.h>
#include "tusb.h"
#include "device/usbd_pvt.h"

// TinyUSB が app driver を集める弱シンボル。これを定義して CCID ドライバを返す。
usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count);

// main.c から呼び、本翻訳単位(ccid_device.o)を強制リンクする。
// これが無いと ESP-IDF が ccid_device.o を archive から抽出せず、上の強定義がリンクされず
// TinyUSB の weak スタブ(0 drivers)が使われてしまう（CCID 未登録 = Windows Code 10）。
void ccid_force_link(void);
