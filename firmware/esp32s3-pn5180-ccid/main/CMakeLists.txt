idf_component_register(
    SRCS
        "main.c"
        "usb_descriptors.c"
        "ccid_device.c"
        "ccid_slot.c"
        "pn5180_reader.c"
    INCLUDE_DIRS "."
    # esp_tinyusb（USB スタック）と jef-sure/pn5180（NFC reader）は idf_component.yml で取得。
    # esp_timer は pn5180_reader.c の poll 周期計測（esp_timer_get_time）。main が REQUIRES を明示すると
    # 「main は全コンポーネントを自動要求」の既定が外れるため、使うものは明示する。
    REQUIRES esp_tinyusb esp_timer
)

# usbd_app_driver_get_cb は TinyUSB が weak 宣言のみ + 同一 TU から呼ぶため、main の strong 定義が
# link 順で無視され「未定義 → 0」に解決されることがある（→ CCID クラス未登録 = Windows Code 10）。
# -u（--undefined）で我々の定義を強制リンクし、weak 参照を我々の関数へ束縛させる。
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--undefined=usbd_app_driver_get_cb")
