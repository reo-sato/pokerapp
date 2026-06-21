# ESP-IDF プロジェクト定義（ESP32-S3 + PN5180 ×N を USB CCID smart card reader として公開）
# ビルド: idf.py set-target esp32s3 && idf.py build && idf.py -p <PORT> flash monitor
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32s3-pn5180-ccid)
