# WS2812B 灯条模块

## 1. 支持范围

`builtin.ws2812b` 通过 WLED HTTP JSON API 控制灯条，支持电源、颜色、亮度、灯珠数量和十个预设特效。支持的控制器固件为：

| 开发板 | 固件 | 要求 |
|---|---|---|
| ESP-01S | WLED 16.0.0 | ESP8266、1 MB Flash，灯带数据 GPIO0 |
| ESP32-S3 SuperMini | WLED 16.0.0 | 4 MB QSPI Flash，灯带数据 GPIO 按实际接线设置 |

SuperMini 在这里只承担 WLED 灯条控制，不运行 ESPAgent 显示链路，因此不需要 PSRAM。它仍不受主显示固件支持，不能用于 ST7789 画面接收。

## 2. 首次烧录

1. 打开“模块 → 灯条”。
2. 在“控制器网络”中选择控制器对应的 USB 串口。
3. 在“控制器固件”中选择 `ESP-01S` 或 `ESP32-S3 SuperMini`。
4. 点击“烧录 WLED”，等待进度完成和设备复位。
5. 输入 2.4 GHz Wi-Fi 名称和密码，点击“写入并连接”。
6. SuperMini 首次上线后，在 WLED 设置中把灯带数据 GPIO 设为实际接线引脚，并设置灯珠数量。

烧录器和固件均内置在桌面应用中，不依赖用户安装 ESP-IDF。串口选择会同时用于烧录和 USB 配网；电脑连接多个 Espressif 设备时应明确选择灯条控制器，避免误烧主显示设备。

## 3. SuperMini 固件布局

SuperMini 使用完整的空白芯片初装布局，而不是只写入 WLED 应用镜像：

```text
Chip: ESP32-S3
Flash: 4 MB QSPI
0x000000  bootloader.bin
0x008000  partition-table.bin
0x00E000  boot_app0.bin
0x010000  WLED_16.0.0_ESP32-S3_4M_qspi.bin
```

发布包位于 `firmware/releases/esp32s3_supermini_wled/`。Host 在烧录前按 `manifest.json` 校验每个镜像的大小和 SHA-256，并先用 `chip_id` 确认目标芯片。

## 4. GPIO 隔离

ESP-01S 固件上线后，Host 会把 WLED 默认 GPIO2 灯带总线迁移到 GPIO0，禁用占用 GPIO0 的默认按钮配置，并释放 GPIO2，避免板载蓝灯常亮。

ESP32-S3 SuperMini 不执行上述迁移，也不会自动重写灯带、按钮或 Relay GPIO。两类设备只共享 WLED 网络控制和 USB Improv 配网协议，不共享板级引脚修复逻辑。

## 5. 故障检查

- 未发现串口：确认 USB 数据线和驱动，并点击“重新扫描”。
- 多设备同时连接：在“控制器串口”中手动选择目标端口。
- 烧录后无法配网：等待设备复位完成，再重新扫描串口并写入 Wi-Fi。
- 已联网但灯条不亮：先确认 WLED 中的数据 GPIO、灯珠数量、供电和共地。
- SuperMini 烧录后不能作为屏幕使用：这是预期行为；灯条包不包含 ESPAgent 显示固件。
