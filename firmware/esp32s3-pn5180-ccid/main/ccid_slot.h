// ccid_slot.h — CCID メッセージ処理（framework 非依存の中核, 契約 §5/§6/§7）。
//
// USB plumbing(ccid_device.c)から bulk-OUT を受け取り、bulk-IN レスポンスを組み立てる純関数的 API。
// ここを単体で正しくしておけば、USB 層が版依存で揺れても契約準拠は保てる。
#pragma once

#include <stddef.h>
#include <stdint.h>

// CCID bulk-OUT メッセージ 1 件を処理し、bulk-IN レスポンスを out に書く。
// 返り値 = レスポンス長（10byte ヘッダ + データ）。0 は「応答不要」。
// in/out は別バッファ前提。out_max は最低 (10 + ATR/UID 長) を確保すること。
size_t ccid_process_message(const uint8_t *in, size_t in_len,
                            uint8_t *out, size_t out_max);

// ── カード挿抜通知（RDR_to_PC_NotifySlotChange 0x50, interrupt-IN）──
// poll の後に呼ぶ。前回 commit した present 状態と比べて変化した slot があれば、out に
// 0x50 + bmSlotICCState（slot ごと 2bit: bit0=present, bit1=changed）を組み立てて長さを返す
// （変化なしなら 0）。送信に成功したら ccid_slot_notify_committed() で「通知済み」を確定する。
// 送信失敗時は呼ばない → 次の poll で同じ差分を再送する。
size_t ccid_slot_build_notify(uint8_t *out, size_t out_max);
void ccid_slot_notify_committed(void);

// USB reset / 再列挙時に呼ぶ: 通知済み状態と powered をクリアし、挿抜を改めて通知させる。
void ccid_slot_reset_notify(void);
