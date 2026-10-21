# WLED for ESP32-S3 SuperMini

This package is the prebuilt WLED 16.0.0 initial-install image set used by the
ESPAgent WS2812B module.

Supported hardware baseline:

- ESP32-S3 SuperMini with 4 MB QSPI flash configuration.
- No external PSRAM is required for the ESPAgent light-strip role.
- The strip data pin is configured in WLED after first boot; ESPAgent does not
  apply the ESP-01S GPIO2-to-GPIO0 migration to ESP32-S3 devices.

Flash layout:

| Address | Image |
|---|---|
| `0x0000` | ESP32-S3 QSPI bootloader |
| `0x8000` | 4 MB WLED partition table |
| `0xE000` | OTA boot metadata |
| `0x10000` | Official WLED ESP32-S3 4 MB QSPI application |

The WLED application image and checksum come from the official
[WLED 16.0.0 release](https://github.com/wled/WLED/releases/tag/v16.0.0).
The initial-install bootloader, partition table, and OTA metadata layout follow
the open-source
[WLED advanced installer](https://github.com/wled-install/wled-install.github.io).

Do not use this package for ESP32-S3 boards with incompatible flash/PSRAM modes
or for ESPAgent display firmware.
