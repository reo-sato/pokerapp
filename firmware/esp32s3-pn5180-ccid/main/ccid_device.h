// ccid_device.h — TinyUSB のカスタムクラスとして CCID を登録する層。
#pragma once

#include <stdint.h>
#include "tusb.h"
#include "device/usbd_pvt.h"

// TinyUSB が app driver を集める弱シンボル。これを定義して CCID ドライバを返す。
usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count);
