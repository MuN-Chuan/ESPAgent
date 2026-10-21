---
name: espagent-light-strip
description: "Control every ESPAgent WS2812B light-strip function through the local ESPAgent API."
version: 1.0.0
author: ESPAgent
platforms: [linux]
metadata:
  hermes:
    tags: [ESPAgent, Smart-Home, WLED, WS2812B, Lights]
prerequisites:
  commands: [bash, curl.exe]
---

# ESPAgent Light Strip

Use this skill whenever the user asks about the ESPAgent WS2812B strip, WLED controller,
strip power, brightness, color, effects, LED count, controller address, Wi-Fi provisioning,
or firmware flashing.

All commands must go through ESPAgent. Do not call WLED directly because ESPAgent owns the
optimistic state, command queue, desktop UI updates, and error reporting.

## Command

```bash
SKILL="$HOME/.hermes/skills/smart-home/espagent-light-strip"
bash "$SKILL/scripts/espagent-light" state
bash "$SKILL/scripts/espagent-light" action ACTION '{"field":"value"}'
```

The helper uses Windows `curl.exe` so WSL always reaches the desktop Host at
`${ESPAGENT_API_URL:-http://127.0.0.1:8787}`.

Always read `state` before changing an effect, LED count, endpoint, firmware target,
Wi-Fi, or firmware. A successful action returns the updated optimistic module state.
If `online` is false, report the device error instead of claiming the light changed.

## Lighting Controls

```bash
# Power
bash "$SKILL/scripts/espagent-light" action set_power '{"power":true}'
bash "$SKILL/scripts/espagent-light" action set_power '{"power":false}'

# Brightness, 0 through 100
bash "$SKILL/scripts/espagent-light" action set_brightness '{"brightness":40}'

# RGB color
bash "$SKILL/scripts/espagent-light" action set_color '{"color":"#FF6600"}'

# One atomic power/brightness/color update
bash "$SKILL/scripts/espagent-light" action set_state \
  '{"power":true,"brightness":35,"color":"#FFB347"}'

# Effect. Use a supported `effect_presets[].id` from state; `solid` disables animation.
bash "$SKILL/scripts/espagent-light" action set_effect '{"preset_id":"solid"}'
```

For natural-language colors, convert to a conventional `#RRGGBB` value. Preserve values
the user did not mention. Prefer `set_state` when two or more of power, brightness, and
color change together.

## Configuration And Maintenance

These actions change persistent configuration or hardware. State the exact change and
obtain explicit user confirmation immediately before invoking them:

```bash
# Physical LED count, 1 through 1200
bash "$SKILL/scripts/espagent-light" action set_led_count '{"led_count":60}'

# WLED endpoint
bash "$SKILL/scripts/espagent-light" action configure \
  '{"endpoint":"http://192.168.1.50"}'

# Firmware target. Choose an id from `firmware_targets`.
bash "$SKILL/scripts/espagent-light" action set_firmware_target \
  '{"target":"esp01s"}'

# USB Wi-Fi provisioning. Never repeat or expose the password in the final reply.
bash "$SKILL/scripts/espagent-light" action provision_wifi \
  '{"port":"COM5","ssid":"Network","password":"secret"}'

# Firmware flash. The device and serial port must match the current state.
bash "$SKILL/scripts/espagent-light" action flash_firmware '{"port":"COM5"}'
```

Use `refresh` to update serial ports and device state:

```bash
bash "$SKILL/scripts/espagent-light" action refresh '{}'
```

Never guess a firmware target, serial port, effect id, or LED count. Never flash or
provision Wi-Fi from an ambiguous request. Do not include Wi-Fi passwords in summaries.
