idf_component_register(
    SRCS
        "main.c"
        "usb_descriptors.c"
        "ccid_device.c"
        "ccid_slot.c"
        "pn5180_reader.c"
    INCLUDE_DIRS "."
    # esp_tinyusb（USB スタック）と jef-sure/pn5180（NFC reader）は idf_component.yml で取得。
    REQUIRES esp_tinyusb
)
