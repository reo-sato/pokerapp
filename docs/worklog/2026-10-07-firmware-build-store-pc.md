# Worklog: 店舗 PC でファームウェアをビルドする（ESP-IDF の導入と部品の版の固定）

## Date

2026-10-07

## Scope / Task

読み取り装置のファームウェア（v1.11 = 使えるリーダーの一覧・再起動の命令, 10/06）を店舗 PC から書き込む。店舗 PC には
ESP-IDF が無かったので入れ方をまとめ、最初のビルドで見つかった失敗を直す。

## 経緯

- オーナー:「先にファームウェアの更新やらせて」「だからesp-idfをインストールしなきゃ」。
- 版は開発 PC で実機に書き込んだ **ESP-IDF v5.3.5**（`docs/worklog/2026-06-22-esp32s3-ccid-firmware-bring-up.md`）。
  公式のオフライン版インストーラ `esp-idf-tools-setup-offline-5.3.5.exe`（GitHub の espressif/idf-installer の
  リリース offline-5.3.5, 1.15 GB）を案内した（dl.espressif.com はこちらの環境から届かないので、GitHub のリリースで
  ファイルの存在を確かめた）。
- オーナーが開いたのは「ESP-IDF 5.3 CMD」（コマンド プロンプト）だった。PowerShell 用の `;` 区切り・`Where-Object` は
  動かない。CMD の `idf.py` は DOSKEY の別名で行の先頭でしか効かないので、`&&` でつなぐときは
  `python "%IDF_PATH%\tools\idf.py"` と書く形にした。
- **最初のビルドの失敗**: 部品の版の指定が `esp_tinyusb: "^1.4.0"` だけで、tinyusb は esp_tinyusb の依存として最新の
  **0.21.0~2** が入った。tinyusb 0.21 は `usbd_edpt_xfer` に `is_isr` の引数を足した（上流の変更）ので、
  `ccid_device.c` の 5 か所が「too few arguments」でビルドできなかった。開発 PC（6 月）のときは 0.19.0 が最新だった。

## Changed Files

- `firmware/esp32s3-pn5180-ccid/main/idf_component.yml` — 実機で確かめた組み合わせに固定:
  `esp_tinyusb: "~1.7.6"`・`espressif/tinyusb: "~0.19.0"`（`jef-sure/pn5180: "^0.1.0"` はそのまま）。版を上げるときは
  実機で CCID の列挙と読み取りを確かめてから。
- `firmware/esp32s3-pn5180-ccid/README.md` — 必要環境を v5.3.5 に、店舗 PC の書き換えの手順に ESP-IDF の入れ方・
  「ESP-IDF 5.3 CMD」の 1 行（前のビルドの `managed_components` と `dependencies.lock` を消してから取り直す）・
  CMD から UART 側の COM 番号を選ぶ 1 行・git の「dubious ownership」の警告は無害、を追加。
- `CHANGELOG.md`・本作業ログ。

## Expected / Implemented Behavior

- 更新したあと、CMD の 1 行で部品を取り直してビルドすると tinyusb 0.19 系が入り、`ccid_device.c` がビルドできる。
- コードは変えていない（tinyusb 0.21 に合わせる直しは実機で確かめられないので入れない）。

## Test Results

- 版の指定を ESP の部品管理（`idf-component-manager` 2.5.2, PyPI）で確かめた: マニフェストとして読める
  （`espressif/esp_tinyusb ~1.7.6`・`espressif/tinyusb ~0.19.0`・`idf >=5.1`・`jef-sure/pn5180 ^0.1.0`）。
  `~0.19.0` は 0.19.0・0.19.0~2・0.19.0~3 に当たり、0.21.0~2 には当たらない。`~1.7.6` は 1.7.6~2 に当たる。
- ESP-IDF のビルドそのものはこちらの環境ではできない（ESP の配布元に届かない）。店舗 PC のビルドで確かめる。

## Remaining Gaps

- 店舗 PC でのビルド・書き込み・`rfid_relay.py status` で「使えるリーダーの一覧」が出ることの確認。
- 部品の版の記録（`dependencies.lock`）はリポジトリに入れていない（`.gitignore`）。版の固定はマニフェストで行う。

## Related Commits

- 本作業ログと同じコミット。
