# ESPAgent 项目文档

本目录描述当前主线实现。早期 UDP tile、WebRTC、单 MJPEG 固件、VoxCPM2 和无 PSRAM 显示板实验，仅在能解释现行设计时保留为历史结论，不得写成产品能力。

## 当前状态

| 领域 | 当前实现 |
|---|---|
| 产品形态 | Windows WebView2 桌面应用；提供独立安装包和源码开发入口 |
| 桌面页面 | 总览、模块、助手、家居、显示、固件、设置、诊断 |
| 模块运行时 | 内置模块 Manifest、Registry、统一状态/动作 API、动态导航和 ESP32 场景激活 |
| 内置模块 | Codex 额度、桌面副屏、WS2812B 灯条 |
| 设备 UI | Windows Host 使用 HTML/CSS/JavaScript 和持久 Chromium 渲染 240x320 或 320x240 画面 |
| 显示传输 | 默认 Indexed I2 + LZ4；可运行时热切换 MJPEG；旧 RGB565 tile 仅保留兼容 |
| ESP32 显示 | 带 PSRAM 的 ESP32-S3、ST7789、80 MHz SPI DMA、双局部 DMA 缓冲、Gamma 默认 1.8 |
| 固件管理 | 应用内置 esptool、pyserial、发行镜像和 SHA-256 清单；用户无需安装 ESP-IDF |
| Hermes | demo adapter 或外部 WSL Hermes 常驻 JSON-RPC gateway；支持流式回复、新会话、打断和 STT |
| 语音 | Windows 原生麦克风采集，Hermes STT 转写；Fish Audio 将回复播放到 Windows 扬声器 |
| 家居 | Home Assistant 单灯具控制；WLED 灯条模块支持颜色、亮度、特效、烧录和 USB 配网 |
| 未实现 | ESP32 音频/I2S、VoxCPM2、第三方模块安装/签名、触摸/按键、可用的 ESP32 WebRTC 显示链路 |

显示默认值的唯一详细说明见[显示链路文档](./display/README.md)。硬件和引脚边界见[显示硬件支持策略](./hardware-support-policy.md)。

## 分发边界

Windows 安装包只包含应用本体、运行库、内置模块、显示资源、测试视频和发行固件，不包含 Hermes、WSL、Home Assistant、Fish Audio 服务、AI 模型、API Key 或用户凭据。

安装版的可变数据位于：

```text
配置  %LOCALAPPDATA%\ESPAgent\.env.local
模块  %LOCALAPPDATA%\ESPAgent\modules.json
日志  %LOCALAPPDATA%\ESPAgent\logs\espagent_app.log
```

安装版首次运行时，如果用户配置不存在，会尝试迁移常见源码目录中的旧 `.env.local`，但不会覆盖已经存在的用户配置。

## 文档职责

| 需要了解的内容 | 文档 |
|---|---|
| 系统边界、数据流、已实现与未实现 | [项目架构与实现状态](./project-architecture-and-status.md) |
| 安装版、页面、快捷键、配置和日志 | [本地桌面应用](./desktop-app-guide.md) |
| 最短演示流程、源码运行和故障入口 | [构建、运行与演示手册](./demo-runbook.md) |
| 模块 Manifest、Runtime、UI 和测试 | [模块开发指南](./module-development-guide.md) |
| 固件烧录、USB 配网、GPIO 和发行清单 | [内置固件烧录](./firmware-flashing-guide.md) |
| ESP32-S3 显示硬件准入范围 | [显示硬件支持策略](./hardware-support-policy.md) |
| 设备 UI 的功能和操作 | [设备 Web UI 功能与操作](./display-ui-features-and-operations.md) |
| Host 状态和 HTTP 接入 | [功能接入 API](./display-ui-integration-api.md) |
| 设备 UI 视觉和排版经验 | [小屏 UI 设计经验](./display-ui-design-experience.md) |
| Indexed、MJPEG、LCD 和性能测试 | [显示链路专题](./display/README.md) |
| 麦克风、Hermes STT 和 Fish Audio | [Hermes 与 Fish Audio 语音](./audio/fish-audio-hermes-streaming-guide.md) |
| WLED 灯条使用与固件 | [WS2812B 灯条模块](./ws2812b-module-guide.md) |

## 维护规则

- 默认值以 `apps/demo_host/espagent_demo/state.py`、`server.py` 和 `firmware/esp32_display/main/` 为准。
- 协议字段以 `apps/demo_host/espagent_demo/protocol.py` 和 `firmware/esp32_display/main/protocol.h` 为准。
- 发行固件以各自 `firmware/releases/*/manifest.json` 为唯一烧录参数来源。
- 性能数字必须注明场景、模式、参数、固件和测试窗口；历史采样不是理论上限。
- 新能力只有在代码、自动测试和必要的实机流程完成后才能标记为“已实现”。
- 专题参数只在所属文档完整维护，其他文档链接引用，不复制整表。
- 凭据只写入被 Git 忽略的用户配置，不得进入文档、前端或提交记录。
