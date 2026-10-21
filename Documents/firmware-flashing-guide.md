# ESPAgent 内置固件烧录

## 1. 用户流程

桌面应用的“固件”页提供 ESP32-S3 一键烧录。用户只需：

1. 使用 USB 连接 ESP32-S3。
2. 等待页面显示自动识别的 COM 端口。
3. 点击“烧录固件”。
4. 等待进度到 100%，设备自动复位并使用 NVS 中已保存的配置重新连接 Wi-Fi。

用户不需要安装 ESP-IDF，不需要运行 `idf` 或 `idf.py`，也不需要输入芯片、串口、波特率、烧录地址和镜像路径。Windows 仍需能识别开发板使用的 USB 串口芯片；当前实机是 CH340。烧录不会擦除 NVS，发行镜像也不包含编译期 Wi-Fi 密码；全新设备和更换路由器均可继续在同一页面通过 USB 配网。

固件页只提供带外部 PSRAM 的主线 ESP32-S3 固件，不提供开发板选择。ESP32-S3 SuperMini 等无 PSRAM 显示板不建议使用，也不受桌面应用支持；Flash 容量不能替代 PSRAM。带 8 MB Octal PSRAM 的 ESP32-S3 Nano/N16R8 类开发板可使用同一主线固件，当前发行固件同时支持 UART0 USB 转串口和芯片原生 USB Serial/JTAG。原因和硬件准入规则见[显示硬件支持策略](./hardware-support-policy.md)。

以上限制只针对主显示固件。灯条模块另有 ESP-01S 和 4 MB QSPI ESP32-S3 SuperMini 的 WLED 固件选择，入口位于“模块 → 灯条 → 控制器固件”，操作和地址布局见[WS2812B 灯条模块](./ws2812b-module-guide.md)。

## 2. USB 网络设置

固件页的“网络设置”不要求用户选择串口：

1. 连接运行当前发行固件的 ESP32-S3。
2. 输入 2.4 GHz Wi-Fi 名称和密码；开放网络的密码留空。
3. 点击“写入并连接”。
4. 页面依次显示设备识别、写入、连接和取得 IP 的状态。

Host 会依次向自动发现的候选串口发送 `ESPAGENT_WIFI_HELLO 1`，只有返回 ESPAgent 协议标识的设备才会收到配置。固件同时监听 UART0 和原生 USB Serial/JTAG，回应也写入两个通道；CH340/CP210x 开发板与只有原生 Type-C USB 的 ESP32-S3 Nano 因而可以共用协议。原生 USB 通道使用 ESP-IDF 的中断驱动和独立 RX/TX 环形缓冲，不依赖辅助控制台 VFS 的无驱动轮询读取。SSID 和密码以 UTF-8 字节的十六进制形式组成单行命令，避免空格和中文破坏协议边界；密码不进入 API 状态、桌面日志或固件日志。该通道是本机 USB 串口协议，不提供加密或远程配置能力，仍应限制电脑上的本地进程访问串口。

固件把新配置写入 Wi-Fi 驱动的 Flash/NVS 存储，立即断开旧网络并重连。USB 配网任务常驻：没有凭据时固件等待 USB 配置而不会退出；已有凭据时显示链路正常启动，之后仍可热切换网络。网络连接成功前 UDP 显示服务尚未启动，连接成功后按原流程启动；运行期间更换网络不需要重启固件。

USB 配网和固件烧录共享串口任务互斥，同一时间只能执行一个操作。桌面页面的状态轮询只枚举串口，不会持续打开 COM 端口。

## 3. 局域网自动发现

Host 每秒向局域网广播一个 `ESPAGENT_DISCOVER 1` 探测包。固件只返回状态，不返回画面；Host 从状态包来源地址取得 ESP32 的实际 IP，并把后续 MJPEG/Indexed 数据目标切换为该单播地址。

因此 `ESP32_TARGET_IP` 只作为启动回退值，不再要求用户提前查询 DHCP 地址。自动发现流量约为每秒一个短报文，不会占用画面链路带宽。网络仍须允许同一局域网内的 UDP 广播和客户端互访；开启 AP 隔离时无法自动发现，也无法显示。

## 4. USB 屏幕引脚设置

固件页提供“屏幕引脚”设置。默认值为：

| 信号 | 默认 GPIO |
|---|---:|
| SCL / SCLK | 12 |
| SDA / MOSI | 11 |
| CS | 10 |
| DC | 8 |
| RST | 7 |
| BL | 6 |

推荐先点击“读取设备”，确认当前 NVS 中的六个值，再修改并点击“写入并重启”。固件验证六个 GPIO 都能输出、互不重复且未占用八线 PSRAM/Flash 或 UART0 USB 恢复通道，随后一次性提交到 NVS，返回成功后自动重启。LCD 和背光只在启动时读取配置，因此不在运行中拆除 SPI/DMA 链路。

“恢复默认”只把 `12/11/10/8/7/6`填回表单，仍需点击“写入并重启”才会修改设备。GPIO 26 至 37、43 和 44 被产品固件保留，GPIO 46 不能作为输出；其他引脚仍须确认没有被开发板上的外设、启动电阻或板载器件占用。写错但仍通过校验时，屏幕可能不显示，可保持 USB 连接后重新写入默认值。烧录不会擦除该配置。

屏幕引脚读取、写入、USB 配网和固件烧录共享同一个串口互斥，同一时间只执行一项操作。协议命令为 `ESPAGENT_DISPLAY_GPIO_GET 1` 和 `ESPAGENT_DISPLAY_GPIO_SET 1 <scl> <sda> <cs> <dc> <rst> <bl>`。

## 5. 烧录运行方式

桌面应用自己的 Python 环境包含 `esptool` 和 `pyserial`。Host 启动后：

1. 校验内置 `manifest.json`。
2. 校验三个镜像的文件大小和 SHA-256。
3. 使用 `pyserial`扫描 USB 串口并优先选择 Espressif、CH340、CP210x 和 FTDI 设备。
4. 以 115200 波特率执行 `chip_id`，确认候选端口确实是 ESP32-S3。
5. 根据内置清单以 460800 波特率写入镜像。
6. 由 esptool 校验写入内容并通过 RTS 复位设备。

烧录任务在独立线程执行，同一时间只允许一个任务。页面轮询状态不会持有串口或影响显示传输任务；烧录期间按钮被禁用。

## 6. 内置固件包

发行包位置：

```text
firmware/releases/esp32_display/
├── manifest.json
├── bootloader.bin
├── partition-table.bin
└── espagent_display.bin
```

当前地址和参数：

```text
Chip: ESP32-S3
Baud: 460800
Flash mode: DIO
Flash frequency: 80 MHz
Flash size: 2 MB
0x000000  bootloader.bin
0x008000  partition-table.bin
0x010000  espagent_display.bin
```

`manifest.json`是唯一烧录参数来源。Host 不读取开发工程的 `build/`、`flasher_args.json`或 `sdkconfig`，因此最终用户机器没有 ESP-IDF 也能烧录。清单没有 NVS 镜像，写入过程不会执行 `erase_flash`，已有网络配置会被保留。

## 7. 开发者更新固件包

只有重新编译固件时需要 ESP-IDF：

```powershell
Set-Location firmware\esp32_display
idf
idf.py build
```

构建完成后，将 `build/flasher_args.json`列出的镜像复制到 `firmware/releases/esp32_display/`，同步修改版本号、文件大小和 SHA-256。提交前必须通过以下检查：

- 清单目标是 `esp32s3`。
- 三个地址与 `flasher_args.json`一致。
- 编译时 `CONFIG_ESPAGENT_WIFI_SSID`和 `CONFIG_ESPAGENT_WIFI_PASSWORD`均为空，固件通过 `esp_wifi_get_config`读取 NVS。
- `manifest.json` 的 `provisioning.usb` 为 `uart0+usb_serial_jtag`，配网波特率为 115200，`display_gpio` 为 `nvs`。
- 干净构建配置包含 `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y`，原生 USB 可看到应用日志并执行配网。
- Host 从错误的 `ESP32_TARGET_IP` 启动时，能通过 `ESPAGENT_DISCOVER 1` 自动切换到状态包来源 IP。
- 实机发送只读 `ESPAGENT_WIFI_HELLO 1` 后能返回当前连接状态、SSID 和 IP；不要在自动测试中写入真实密码。
- 实机发送只读 `ESPAGENT_DISPLAY_GPIO_GET 1` 后能返回当前六个屏幕 GPIO；写入测试优先使用默认值并确认重启后 LCD 恢复。
- 修改任意镜像字节后，旧清单必须校验失败。
- 清除 `IDF_PATH`、`IDF_PYTHON_ENV_PATH`后，固件页仍显示烧录器和设备就绪。
- 使用桌面应用完成一次实机烧录并确认 ESP32 重新上线。

不要把 `firmware/esp32_display/build/`作为产品烧录源；该目录被 Git 忽略且只属于本机开发构建。

## 8. 关键代码

| 内容 | 位置 |
|---|---|
| 固件包解析、哈希校验、端口扫描和烧录任务 | `apps/demo_host/espagent_demo/firmware_flash.py` |
| HTTP 状态与启动接口 | `apps/demo_host/espagent_demo/server.py` |
| 桌面固件页面 | `apps/demo_host/desktop_ui/` |
| 应用烧录依赖 | `apps/demo_host/pyproject.toml` |
| 固件发行包 | `firmware/releases/esp32_display/` |
| 固件 USB 配网协议 | `firmware/esp32_display/main/usb_wifi_provisioning.c` |
| 固件 Wi-Fi 状态机与 NVS 更新 | `firmware/esp32_display/main/wifi_connect.c` |
| 屏幕 GPIO 默认值、校验与 NVS | `firmware/esp32_display/main/display_gpio_config.c` |

固件相关 API：

- `GET /api/firmware/status`：设备、烧录、USB 配网和屏幕 GPIO 状态。
- `POST /api/firmware/flash`：无请求参数的一键烧录。
- `POST /api/firmware/wifi`：只接受 `ssid`和 `password`；不接受串口、芯片或波特率。
- `POST /api/firmware/display-gpio/read`：通过 USB 读取设备当前引脚。
- `POST /api/firmware/display-gpio`：接受六个 GPIO，保存后重启设备。

接口不提供客户端指定路径、地址、端口或芯片参数的能力，避免误烧、路径注入和选择错误设备。
