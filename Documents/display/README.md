# ESPAgent 显示链路文档

本目录是当前显示实现的唯一详细资料。默认路径为 Indexed I2 + LZ4，MJPEG 作为视频和高色内容路径；早期 tile、WebRTC 和单模式方案只在解释兼容代码或失败经验时出现。

## 当前默认

```text
Transport              indexed
Indexed format         I2
Indexed keyframe       60 frames
Protected colors       disabled
MJPEG fallback         Q90, 4:2:0
NACK                    Off
Preview limit          Off (stored value 30 FPS)
Tone                    Off / 100% neutral
Backlight               100%
Panel Gamma             1.8
ESP logging             Off
```

## 文档

1. [双模式架构](./00-merged-display-architecture.md)
2. [MJPEG 实现与调优](./01-mjpeg-runtime-and-optimization.md)
3. [Indexed LZ4 实现与调优](./02-indexed-runtime-and-optimization.md)
4. [Host、Chromium 与网页预览](./03-host-browser-render-and-preview.md)
5. [ESP32 接收、LCD DMA 与 ACK](./04-esp32-receive-decode-lcd-ack.md)
6. [性能 A/B 与故障定位](./05-performance-ab-test-guide.md)
7. [开发原则](./06-development-lessons.md)
8. [横竖屏运行时切换](./07-landscape-orientation.md)

## 模式选择

| 内容 | 推荐 |
|---|---|
| 双色文字、线条、像素 UI | I1/I2 + Binary |
| 5 至 16 个受控颜色 | I4 |
| 较多离散颜色、协议和兼容测试 | I8 |
| 视频、照片、阴影、连续渐变 | MJPEG |

应使用能表达重要颜色的最低 Indexed bpp。I8 的 76800 字节 raw indices 和高 chunk 数会降低余量，并不天然比 I2 更清晰或更快。

## 关键事实

- 两种模式使用同一 Host worker、UDP 端口、transport epoch、LCD mutex 和 displayed ACK。
- Indexed 是完整帧，不是脏矩形 tile；稳定窗口不使用 NACK。
- MJPEG 是 JPEG 编码格式，网络分片、完整性和 ACK 由自定义 UDP 协议负责。
- 网页预览来自 Host 缓存，不是 ESP32 抓屏。
- LCD FPS 只在实际刷屏完成后更新；必须与 frame ID、lag、队列和错误增量一起判断。
- Gamma 命令属于 LCD panel IO，运行时变更必须与 RGB DMA 串行。
- 显示方向可在设置页切换：竖屏为 240x320，横屏为 320x240；两种方向像素总数均为 76800，不改变链路的理论整帧带宽。

## 关键代码

| 模块 | 文件 |
|---|---|
| Host 调度 | `apps/demo_host/espagent_demo/display_worker.py` |
| UDP 发送和 pacing | `apps/demo_host/espagent_demo/udp_link.py` |
| Indexed 编码 | `apps/demo_host/espagent_demo/indexed_rect.py` |
| Web/Pillow 渲染 | `web_renderer.py`, `renderer.py` |
| 协议 | `protocol.py`, `firmware/esp32_display/main/protocol.h` |
| ESP 接收和 worker | `firmware/esp32_display/main/udp_display.c` |
| ST7789 | `firmware/esp32_display/main/lcd_display.c` |
