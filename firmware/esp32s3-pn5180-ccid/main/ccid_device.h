// ccid_device.h — TinyUSB のカスタムクラスとして CCID を登録する層。
#pragma once

#include <stddef.h>
#include <stdint.h>
#include "tusb.h"
#include "device/usbd_pvt.h"

// カード挿抜通知（RDR_to_PC_NotifySlotChange）を interrupt-IN で host へ送る。
// msg = ccid_slot_build_notify() が組み立てた 0x50 + bmSlotICCState。
// 未 mount / EP 未 open / 前回の通知がまだ host に読まれていない、なら false
// （呼び側は「通知済み」を確定せず、次の poll で再試行する）。任意タスクから呼べる
// （usbd_edpt_claim が OS mutex で排他）。
bool ccid_notify_slot_change(const uint8_t *msg, size_t len);

// TinyUSB が app driver を集める弱シンボル。これを定義して CCID ドライバを返す。
usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count);

// main.c から呼び、本翻訳単位(ccid_device.o)を強制リンクする。
// これが無いと ESP-IDF が ccid_device.o を archive から抽出せず、上の強定義がリンクされず
// TinyUSB の weak スタブ(0 drivers)が使われてしまう（CCID 未登録 = Windows Code 10）。
void ccid_force_link(void);
