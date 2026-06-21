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
