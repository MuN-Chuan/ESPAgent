# ESP-01S WLED firmware

This package contains the official `WLED_16.0.0_ESP01.bin` release image.

- Source: https://github.com/wled/WLED/releases/tag/v16.0.0
- Target: ESP8266-based ESP-01 / ESP-01S with 1 MiB flash
- License: EUPL-1.2
- Flash mode: DOUT, 40 MHz, 1 MiB

The desktop application verifies the image size and SHA-256 before invoking its
bundled `esptool`. No ESP-IDF installation or manual flash arguments are needed.

The desktop application reads the WLED bus configuration after the device joins
the network. The official ESP-01 image initially assigns the LED bus to GPIO2,
which is also connected to the active-low onboard blue LED. ESPAgent migrates a
standard single-wire bus from GPIO2 to GPIO0, disables WLED's default GPIO0
button assignment, and leaves GPIO2 unassigned so the onboard LED stays off.

Connect the WS2812B data input to GPIO0. GPIO1 and GPIO3 remain available to the
UART so USB Improv Wi-Fi provisioning continues to work.
