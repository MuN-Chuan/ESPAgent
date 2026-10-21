# ESPAgent 功能接入 API

## 1. 接入原则

Hermes、Home Assistant、语音和 ESP32 都先更新 Host `DemoState`，主题只负责渲染状态。外部服务不得直接修改主题 DOM，也不得持有状态锁执行网络、截图、编码、TTS 或等待操作。

```text
Adapters / UDP status / voice events
                 -> DemoState snapshot
                 -> Web/Pillow renderer
                 -> Indexed or MJPEG
                 -> ESP32 LCD
```

API 当前没有鉴权，只适合本机或受信任局域网。

## 2. 服务入口

| 方法与路径 | 用途 |
|---|---|
| `GET /api/state` | 完整状态快照 |
| `GET /api/health` | Hermes、HA、Fish Audio、ESP32 健康状态 |
| `GET /api/preview.png` | 当前无损 Host 预览，带 `X-Frame-Id` |
| `GET /api/display/preview.jpg` | 当前 JPEG 预览 |
| `GET /api/display/mjpeg?frames=N` | multipart MJPEG 流 |
| `GET /api/display/frame.rgb565` | 当前方向的 RGB565；240x320 与 320x240 均为 153600 字节 |

## 3. 状态结构

核心字段：

```json
{
  "active_scene": "hermes",
  "test_pattern": "colorbars",
  "hermes": {
    "online": true,
    "status": "idle",
    "last_reply": "等待 Hermes 回复",
    "streaming": false,
    "audio_status": "disabled",
    "voice_supported": false,
    "voice_status": "disabled",
    "voice_silence_seconds": 2.0
  },
  "light": {
    "entity_id": "light.demo_light",
    "online": false,
    "on": false,
    "brightness": 0,
    "color_temp_kelvin": null
  },
  "link": {
    "transport_mode": "indexed",
    "esp_transport_mode": null,
    "transport_epoch": 0,
    "esp_transport_epoch": 0,
    "frame_id": 0,
    "displayed_frame_id": 0,
    "display_fps": 0.0,
    "panel_gamma": "1.8",
    "indexed_bpp": 2
  }
}
```

状态详情应从 `/api/state` 或 FastAPI schema 获取，不在业务调用方复制完整 dataclass。

## 4. 场景与测试源

```http
POST /api/scene/{hermes|lights|link|test|video}
POST /api/video/start
POST /api/test-pattern/{colorbars|palette256|channels|colors|grid|checker|duotone}
```

双色测试可选 body：

```json
{
  "background": "#F8FAFC",
  "text_color": "#111827",
  "text": "Aa 汉字 123",
  "preset": "paper"
}
```

## 5. Hermes 与语音

| 接口 | 说明 |
|---|---|
| `POST /api/hermes` | 等待最终回答，body 为 `{"prompt":"..."}` |
| `POST /api/hermes/stream` | SSE 流式事件与最终 `done` |
| `POST /api/hermes/session/new` | 打断旧回合并创建新会话 |
| `POST /api/hermes/interrupt` | 同时打断 Hermes 与当前 TTS |
| `GET /api/voice/status` | 刷新麦克风、录音和 STT 能力 |
| `POST /api/voice/enable` | body `{"enabled":true}` |
| `POST /api/voice/record/start` | 开始录音 |
| `POST /api/voice/record/stop` | 停止并进入转写 |
| `POST /api/voice/silence` | body `{"seconds":2.0}`，范围 0.5 至 10 |

Windows 主线中，Host 使用 `sounddevice` 直接采集系统默认麦克风并写入 16 kHz 单声道临时 WAV；`WslHermesAdapter` 通过 `espagent.voice.transcribe` RPC 把 WAV 交给 Hermes STT。WSL 不再直接打开麦克风。非 Windows 环境保留 Hermes `voice.record` 回退。Fish Audio 是 Hermes 回复的 TTS 输出，不是麦克风识别服务。打断必须同时使旧 Hermes 事件和旧 `audio_stream_id` 失效。

`GET /api/voice/status` 的 `supported` 表示 adapter 是否实现产品语音链路，`available` 表示录音和 STT 是否同时可用；`details` 应明确报告 Windows 输入设备和 STT provider。demo 模式没有 voice controller，`/api/state` 中 `voice_supported=false`，语音接口返回 HTTP 501。录音或转写失败时 HTTP `detail` 与 `hermes.voice_error` 保留具体错误，不得只返回 `RuntimeError` 类型名。默认 `HERMES_STT_LANGUAGE=zh`，空值表示沿用 Hermes 自动语言检测。

## 6. 灯具

```http
POST /api/light
Content-Type: application/json

{"on":true,"brightness":40}
```

`brightness` 范围为 0 至 100。Host 调用 `HomeAssistantLightAdapter` 后使用完整设备状态替换 `LightState`，避免开关、亮度和色温跨帧不一致。

## 7. UI 与显示参数

```http
POST /api/display/ui
POST /api/display/scroll
POST /api/display/control
POST /api/display/tone
POST /api/display/transport
POST /api/display/unified-flush
```

`POST /api/display/scroll` 接收 `{"delta":36}` 形式的相对滚动命令，范围为 `-120..120` 且不能为 `0`。Host 将命令写入 `display_ui.scroll_delta` 并递增 `display_ui.scroll_revision`；设备主题只在 revision 变化时消费一次命令。当前 Hermes 页把命令映射到回复正文，其他主题可按相同机制声明自己的滚动区域。

`POST /api/display/control` 接收 `enabled` 和 `global_hotkeys_available` 两个可选布尔字段。前者是桌面与 Windows 全局运行时共享的控制模式，后者表示当前是否由桌面进程持有全局热键；普通浏览器控制页不得伪造全局可用状态。

`/api/display/ui` 支持主题、三类字体、90 至 110 缩放、Motion、Hinting 和 Gray AA。

`/api/display/transport` 当前主要字段：

```json
{
  "transport_mode": "indexed",
  "nack_enabled": false,
  "esp_log_enabled": false,
  "backlight_percent": 100,
  "panel_gamma": "1.8",
  "mjpeg_quality": 90,
  "mjpeg_subsampling": "420",
  "indexed_bpp": 2,
  "indexed_protected_colors": [null, null, null, null],
  "indexed_keyframe_interval": 60,
  "preview_fps_limited": false,
  "preview_fps_limit": 30
}
```

保护色数组长度必须匹配格式容量：I1=2、I2=4、I4=16、I8=256。`null` 表示该固定槽位未启用。

WebRTC 信令端点仍有遗留代码，但当前没有可用的 ESP32 WebRTC 显示实现，不应作为产品接入接口。

## 8. Adapter 边界

- `HermesAdapter`：health、ask、new_session、interrupt 和 voice capabilities。
- `HomeAssistantLightAdapter`：health、set_light、get_state、close。
- `HermesSpeechController`：Hermes 事件过滤、分句、Fish Audio 和 PCM 播放。
- `StatusReceiver`：接收 ESP32 状态、transport ACK、NACK 和 displayed ACK。

新增功能页应新增结构化 State 和窄 Adapter，再由两个主题消费相同字段。状态更新后调用现有刷新编排；不要直接调用 streamer 的私有发送逻辑。

## 9. 刷新与并发

功能更新顺序：

1. 在短临界区内更新完整业务状态。
2. 必要时切换 `active_scene`。
3. 应用场景传输 profile。
4. 请求关键帧并唤醒 display worker。

Display worker 使用不可变 snapshot 在锁外完成 Chromium 截图、量化、JPEG/LZ4 和 UDP 发送。高频 Hermes delta 应合并后发布，避免每个 token 触发完整画面。

## 10. 验收

- 输入有 Pydantic/range/enum 校验。
- demo、离线、加载、成功、错误和打断状态均可渲染。
- First 是当前开发主题；冻结的 Second 继续读取相同状态字段，仅用于兼容验证。
- 状态变化无需手动刷新即可到达 ESP32。
- 外部服务失败不终止 display worker。
- 新接口包含自动测试和实机显示检查。
