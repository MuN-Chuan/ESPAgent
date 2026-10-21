# ESPAgent 设备 Web UI 功能与操作

## 1. UI 边界

ESP32 不运行网页。Windows Host 用持久 Chromium 按当前方向渲染 240x320 或 320x240 的 HTML/CSS/JavaScript 页面，截图后再编码为 Indexed 或 MJPEG。浏览器中的 `/screen/` 是同一 UI 的检查入口，最终画质和帧率仍以编码后预览及 ESP 实屏为准。

本文件描述传送到 ESP32 的设备 UI，不是 Windows 桌面应用。桌面入口和页面组织见 [本地桌面应用](./desktop-app-guide.md)。

产品页：

- `Hermes`：回复、活动状态、工具进度、语音状态。
- `Lights`：灯具开关、亮度、色温和同步状态。
- `Link`：Host/LCD FPS、帧号、队列和错误计数。

测试源：Color Bars、256 Colors、Channel Ramps、Color Gamut、Grid、Checker、双色字体和视频。

## 2. 启动与入口

安装版直接启动 ESPAgent。源码诊断时在项目根目录运行 `.\start_demo_host.cmd --console`。

```text
桌面 UI http://127.0.0.1:8787/app/
开发页  http://127.0.0.1:8787/debug/
小屏 UI http://127.0.0.1:8787/screen/
API     http://127.0.0.1:8787/docs
```

## 3. 默认 UI 配置

```json
{
  "theme": "first",
  "font_chinese": "auto",
  "font_latin": "auto",
  "font_numeric": "auto",
  "scale_percent": 100,
  "motion_enabled": true,
  "font_hinting": "auto",
  "grayscale_antialiasing": false
}
```

`First` 是当前主线的浅底、文字排版优先 Paper 主题。`Second` Editorial 主题已冻结并从桌面应用、旧诊断网页的选择入口隐藏，仅保留资源、状态兼容和回归测试；新功能只要求更新 `First`，功能代码仍不应依赖主题 DOM。

## 4. 字体与缩放

字体角色：

```text
Chinese: auto / modern / ui / editorial / humanist / pixel
Latin:   auto / modern / ui / editorial / industrial / pixel
Numeric: auto / modern / editorial / industrial / mono / pixel
```

冻结的 Second 主题不允许中文 `humanist`。兼容字段 `font` 可一次设置三类角色，显式角色字段优先。

Hinting：

- `auto`：平台和字体默认。
- `crisp`：小字、像素字体和清晰网格优先。
- `smooth`：较大曲线字形优先。

Gray AA 关闭时文字边缘严格归一到主题色；开启时只保留背景色到文字色之间的中性灰阶，不保留 RGB 子像素彩边。小字号或 I1/I2 优先关闭，较大字号可实机比较。

缩放支持 `90/95/100/105/110`。它会改变逻辑画布并重新排版，不是裁切后的 CSS transform。

## 5. 动效

Motion 只用于场景切换、Hermes 回复变化和灯具状态变化。快速切页会取消旧动画。关闭 Motion 或系统启用 `prefers-reduced-motion` 后，页面应稳定静止。

避免透明交叉淡化、整页 GPU 位移和无限装饰动画：它们会在截图、量化和传输后形成重影、黑块或持续网络负载。

## 6. 显示控制

### 6.1 Transport

- `Indexed LZ4`：文字、线条和有限颜色 UI；默认 I2。
- `MJPEG`：视频、照片、阴影和连续渐变。
- `UDP Tile`：遗留兼容/实验，不作为产品默认。

Indexed 面板显示 I1/I2/I4/I8、保护色和关键帧参数；MJPEG 面板显示 JPEG Q、色度子采样和 NACK。当前模式无效的参数会隐藏。

### 6.2 Tone、背光和 Gamma

- Tone 默认 Off，四个百分比为 100%，binary threshold 为 0。
- 256 Colors、Channel Ramps、Color Gamut 始终绕过 Tone。
- Backlight 控制 GPIO 6 的 PWM，只改变亮度，不修复色阶或 Gamma。
- Panel Gamma 默认 1.8。运行时切换已与 LCD DMA 串行，不应在快速切页时重复发送同值命令。

### 6.3 Preview FPS

Preview 限速默认关闭。关闭表示自动跟随 Host/LCD 源帧率，不表示无限生成新帧。预览读取 Host 缓存，不从 ESP32 抓屏，不占用 ESP32 图像回传带宽。

## 7. 颜色测试

| 测试 | 目的 |
|---|---|
| 256 Colors | I8 应保留 256 个 RGB565 对齐离散色 |
| Channel Ramps | 检查灰/R/G/B 通道污染 |
| Color Gamut | 超过 256 色的量化、暗部和色带压力测试 |
| Color Bars | 低色容量和保护色测试 |
| Duotone | 字号、背景/文字配色和二值边缘测试 |

Color Gamut 超过 I8 容量，出现有限色阶不等于协议串色；跨通道污染、固定规则错误块或静态输入下调色板抖动才按 bug 调查。

## 8. UI API

读取：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/state
```

局部更新：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8787/api/display/ui `
  -ContentType application/json `
  -Body '{"theme":"first","font_hinting":"crisp","grayscale_antialiasing":false}'
```

浏览器查询参数只影响该 `/screen/` 页面，例如：

```text
/screen/?theme=first&font_zh=pixel&font_en=industrial&font_num=mono&scale=110&motion=0
```

## 9. 扩展主题或字体

主题目录：

```text
apps/demo_host/display_ui/themes/<theme-id>/
  theme.js
  theme.css
  assets/
```

新增主题必须注册前端主题、后端白名单和每个字体角色的允许集合。新增字体必须保留许可证和来源，注册 `@font-face`、角色变量、前后端选项，并实测中英数混排、长文本、90%/100%/110% 和 Motion Off。

## 10. 验收

- 竖屏输出严格为 240x320，横屏输出严格为 320x240。
- Hermes 长回复不覆盖底栏，Lights 和 Link 数值不溢出。
- 中英数角色实际使用预期字体。
- Indexed 预览与 ESP 色值、字形一致。
- Motion Off 的静态输入不产生无意义变化帧。
- 快速切页不闪旧页面，LCD 帧号持续增长。
- `rejected_packets` 和 `draw_failed_packets` 在稳定窗口不增长。
