# ESPAgent 显示硬件支持策略

## 1. 当前支持基线

ESPAgent 的桌面显示功能当前只支持**带外部 PSRAM 的 ESP32-S3 显示板**。主线固件按以下能力设计：

- ESP32-S3，启用外部 Octal PSRAM（当前配置为 80 MHz）。
- ST7789 IPS，240x320 或 320x240。
- 80 MHz SPI DMA 和双局部 DMA 缓冲。
- 完整 MJPEG 与 Indexed I1/I2/I4/I8 传输能力。
- 独立 UDP 接收、完整帧队列、JPEG 接收缓冲和显示工作任务。
- UART0 USB 转串口或芯片原生 USB Serial/JTAG；两种通道均可查看应用日志和执行 USB 配网。

桌面应用的“固件”页只提供经过构建、校验和回归测试的主线 ESP32-S3 固件，不提供开发板型号切换。

带 8 MB Octal PSRAM 的 ESP32-S3 Nano/N16R8 类开发板满足内存基线，可以使用同一主线固件。当前发行固件支持 UART0 USB 转串口和芯片原生 USB Serial/JTAG。屏幕 GPIO 仍须按实际接线设置，开发板名称本身不代表引脚兼容。

## 2. 不建议和不支持的设备

ESP32-S3 SuperMini 等**没有外部 PSRAM**的开发板不建议用作 ESPAgent 显示终端，桌面应用的主显示固件页不提供对应固件或显示运行时兼容。这里的关键条件是是否具有可用的外部 PSRAM，而不是开发板名称或 Flash 容量。

无 PSRAM 设备存在以下限制：

- 无法稳定保留主线需要的 JPEG 接收缓冲、待解码帧和显示缓冲。
- 必须关闭 Indexed 或完整 framebuffer 等功能，形成与主线不同的协议和任务模型。
- Wi-Fi/LwIP、JPEG 解码和 LCD DMA 会竞争有限的内部 SRAM，容易出现分配失败、看门狗复位、无日志重启或持续零帧。
- 为其降低队列、缓冲和任务配置会改变主线 ESP32-S3 的性能与稳定性，不适合作为共享默认实现。

`2 MB Flash` 只表示程序存储容量，不表示设备具有 2 MB 可用运行内存，也不能替代 PSRAM。

## 3. 已取消的 SuperMini 显示支持

以下实验性能力已从主线移除：

- 桌面固件页中的 ESP32-S3 SuperMini 选择项。
- SuperMini 预编译固件、专用 `sdkconfig` 和发布压缩包。
- Host 根据 `mjpeg_only` 状态自动降级传输模式。
- 为无 PSRAM 构建加入的 LCD、UDP、看门狗和专用双串口分支。

原版 ESP32-S3 显示固件已恢复为单一主线配置。当前局域网发现与 UART0/原生 USB 双通道属于主线通用能力，不是 SuperMini 降级分支。

## 4. 灯条控制器例外

`builtin.ws2812b` 灯条模块支持 ESP-01S 和 **4 MB QSPI ESP32-S3 SuperMini** 作为独立 WLED 控制器。该角色不接收屏幕画面、不运行 JPEG/Indexed 解码和 LCD DMA，也不需要 PSRAM，因此不与上述显示硬件限制冲突。

两类灯条固件位于独立发布包，不会改变或替换 `esp32_display` 主线：

- `firmware/releases/esp01s_wled/`
- `firmware/releases/esp32s3_supermini_wled/`

SuperMini 灯条包只适用于兼容 4 MB QSPI Flash 模式的设备。灯带数据 GPIO 由用户在 WLED 中按接线设置，ESPAgent 不会对 ESP32-S3 应用 ESP-01S 专用的 GPIO2→GPIO0 迁移。操作见[WS2812B 灯条模块](./ws2812b-module-guide.md)。

## 5. 网络要求

ESPAgent 的画面链路使用局域网 UDP。电脑和 ESP32 必须位于可互通的同一局域网，且路由器或手机热点不能启用客户端隔离。代理软件中的 TUN `ping` 响应不能证明 UDP 画面链路可达。

判定顺序：

1. ESP32 串口确认已经取得 IP。
2. 电脑和 ESP32 的地址属于同一实际局域网，或网络中存在明确的双向 UDP 路由。
3. Host 正在监听状态端口 `39001`，并每秒向显示端口 `39000` 广播 `ESPAGENT_DISCOVER 1`。
4. Host 从状态回复来源地址自动选择 ESP32 单播目标。
5. ESP32 的接收、通过和 LCD FPS 计数开始增加。

## 6. 新硬件接入门槛

新显示板进入桌面应用支持列表前，必须具备独立硬件配置并完成：

- 固件连续构建和完整镜像校验。
- MJPEG 与 Indexed 两种模式的切换、长时间播放和快速页面切换测试。
- 内存余量、看门狗、UDP 丢包、解码失败和 LCD FPS 记录。
- USB 烧录、USB 配网、断电重启和 NVS 保留测试。
- 不修改或降低现有 ESP32-S3 主线默认性能。

未通过上述验证的开发板只能保留在独立实验分支，不能加入桌面应用的产品固件列表。
