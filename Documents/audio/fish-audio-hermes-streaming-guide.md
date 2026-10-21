# Hermes 语音输入与 Fish Audio TTS

## 1. 当前边界

当前语音分为两条独立链路：

1. Windows Host 用 `sounddevice` 采集系统默认麦克风，生成 16 kHz 单声道 WAV，再通过 Hermes 网关的异步 worker pool 调用 STT。
2. Hermes 回复事件由 Host 过滤和分句，再通过 Fish Audio 实时 WebSocket 生成 44.1 kHz PCM，播放到 Windows 默认扬声器。

WSL Hermes 不直接打开麦克风。ESP32 音频传输、I2S 播放和 VoxCPM2 尚未实现。

```mermaid
flowchart LR
    MIC["Windows microphone"] --> REC["Native 16 kHz WAV capture"]
    REC --> STT["Hermes STT RPC"]
    STT --> TXT["Editable transcript"]
    TXT --> H["Hermes reply stream"]
    H --> FILTER["Answer event filter"]
    FILTER --> FA["Fish Audio realtime TTS"]
    FA --> SPK["Windows default speaker"]
```

## 2. 分发与配置

安装包包含录音和连接代码，但不包含 Hermes、WSL、STT 模型、Fish Audio 服务或凭据。安装版配置写入 `%LOCALAPPDATA%\ESPAgent\.env.local`，源码版写入 `apps\demo_host\.env.local`。

```dotenv
HERMES_MODE=real
HERMES_WSL_DISTRO=ESPHermes
HERMES_COMMAND=/path/to/hermes/venv/bin/hermes -z
HERMES_STT_LANGUAGE=zh
HERMES_STT_TIMEOUT_SECONDS=600
HERMES_VOICE_SILENCE_SECONDS=2.0

FISHAUDIO_ENABLED=true
FISHAUDIO_API_KEY=...
FISHAUDIO_VOICE_ID=...
FISHAUDIO_REALTIME_URL=wss://realtime.fishaudio.org/v1/tts/live
FISHAUDIO_SAMPLE_RATE=44100
FISHAUDIO_SPEED=1.0
FISHAUDIO_VOLUME=1.0
FISHAUDIO_LANGUAGE=zh
FISHAUDIO_CHUNK_LENGTH=200
FISHAUDIO_LATENCY=balanced
```

修改后重启应用。Key 和 Voice ID 只能保存在被忽略的用户配置中。

## 3. 麦克风与 STT

启动时 Host 刷新语音能力：

- `voice_supported`：当前 adapter 是否实现产品语音链路。
- `voice_audio_available`：Windows 录音设备是否可用。
- `voice_stt_available`：Hermes STT provider 是否可用。
- `voice_available`：录音和 STT 是否同时可用。

demo adapter 在 `/api/state` 中设置 `voice_supported=false`，桌面端禁用麦克风并提示需要外部 Hermes；此时 voice API 返回 HTTP 501。真实模式开始录音时只打开 Windows 设备；停止后关闭 WAV，再由网关异步线程调用 Hermes STT。使用原生录音时不会同时调用 Hermes `voice.record`，也不会在每次开始录音时重复调用 `voice.toggle`。

默认语言为 `zh`。`HERMES_STT_LANGUAGE` 设为空时沿用 Hermes 自动检测。STT 长任务默认允许 600 秒；超时或失败只更新语音错误，不阻塞持久 Hermes 网关。应用启动后异步预热本地 faster-whisper，模型在网关进程内缓存，后续录音不重复加载。全局按住说话和 ESP32 Hermes 控制使用 draft 目标，转写先回到输入框供修改；通用 `/api/voice/record/start|stop` 保留 submit 目标，会把最终非空转写送入正常 Hermes 回合。

## 4. TTS 事件过滤

只朗读实际回答：

- `message.delta.text`
- `message.complete.text` 中尚未通过 delta 收到的尾部

不朗读 thinking、reasoning、tool、status、session、error、代码块、URL 和 Markdown 控制标记。每轮必须先收到本轮 `message.start`；旧回合迟到事件不能重新开启 TTS。

强句界包括中英文句号、问号、感叹号、分号和换行。过长内容可在逗号、冒号或空格软切分。控制器累计 delta 并对 complete 做尾部去重，避免整段重播。

## 5. Fish Audio 与 PCM

```text
wss://realtime.fishaudio.org/v1/tts/live
subprotocol: realtime.tts.msgpack.v1
Authorization: Bearer <server-side key>
```

应用层使用 MessagePack。正常顺序为 authenticated、start、ready、text/flush、audio、usage、stop/finish。

PCM 为 44.1 kHz、mono、signed 16-bit little-endian。WebSocket 块若包含奇数字节，播放器保留末尾字节并与下一块拼接，避免采样错位。正常结束排空设备缓冲；打断立即 abort。

## 6. 打断

新问题、“打断”、新会话或 Hermes 错误会：

1. 递增 `audio_stream_id`，使旧回调失效。
2. 关闭 Fish Audio 连接并停止 PCM。
3. 调用 Hermes `session.interrupt`。
4. 清理流式、工具和临时回复状态。

旧 stream ID 的文本和音频必须无条件丢弃。Fish Audio 或麦克风故障不得终止文字回复、Home Assistant 或显示 worker。

## 7. API 与状态

```text
POST /api/hermes/stream
POST /api/hermes/interrupt
POST /api/hermes/session/new

GET  /api/voice/status
POST /api/voice/enable
POST /api/voice/record/start
POST /api/voice/record/stop
POST /api/voice/silence
```

真实语音 controller 存在时，`GET /api/voice/status` 返回 supported、available、audio/STT 能力、状态和诊断详情；demo 模式返回 HTTP 501。`GET /api/state` 的 Hermes 状态始终包括：

```text
audio_status, audio_stream_id, audio_queue_depth
audio_units, audio_bytes, audio_error
voice_supported, voice_enabled, voice_available
voice_audio_available, voice_stt_available
voice_status, voice_transcript, voice_error, voice_details
voice_silence_seconds
```

录音或转写失败时，HTTP detail 与 `voice_error` 应保留设备或 provider 的具体原因，不能只返回 `RuntimeError`。

## 8. 故障定位

安装版诊断日志位于 `%LOCALAPPDATA%\ESPAgent\logs\espagent_app.log`。对比以下连续事件即可定位用户端慢点：

| 日志事件 | 含义 |
|---|---|
| `voice_record_start/stop` | Windows 录音时长和 WAV 大小 |
| `voice_stt_start/done/failed` | 从 Host 转写线程开始到结果返回的总耗时 |
| `hermes_stt_request_start/done` | 等待 STT 串行锁的 `queue_ms` 和 JSON-RPC 往返耗时 |
| `hermes_gateway_prewarm_*` | WSL 内本地模型预热耗时 |
| `hermes_gateway_transcribe_*` | WSL 内 STT provider 的实际推理耗时 |

如果 `queue_ms` 很大，说明预热或上一条转写仍占用 STT；如果 RPC 耗时很大但网关推理耗时较小，应检查 WSL 文件访问和网关通信；如果 `hermes_gateway_transcribe_done elapsed_ms` 本身很大，应检查用户端 CPU、模型、provider 和录音长度。日志不保存音频内容和转写文本。

| 现象 | 检查 |
|---|---|
| 提示 adapter 不支持语音 | 确认 `HERMES_MODE=real`，重启应用并检查 `/api/voice/status` |
| 麦克风按钮禁用 | `voice_supported`、Windows 麦克风权限、默认输入设备、`voice_stt_available` |
| 录音报 `RuntimeError` | 查看 `voice_error` 和 `voice_details`，确认输入设备支持 16 kHz 单声道或允许库转换 |
| 录音正常但无文本 | Hermes STT provider、模型、语言和 WAV 可读性 |
| 首次转写较慢 | 启动后等待后台 faster-whisper 预热完成；同一应用会话内后续转写复用模型 |
| 转写较久后报超时 | 检查 STT provider 日志和模型资源；必要时调整 `HERMES_STT_TIMEOUT_SECONDS`，文字 Hermes 应仍可正常响应 |
| 有文本无声音 | Fish Audio health、Key、Voice ID 和 Windows 默认输出 |
| TTS 重复 | Hermes 事件序列、delta/complete 去重、是否存在多个 Host |
| 打断后仍有旧声 | `audio_stream_id`、PCM abort 和其他播放器进程 |

## 9. 代码与测试

```text
native_audio.py     Windows 录音设备和 WAV 采集
voice_input.py      能力、录音生命周期、STT 和转写草稿
hermes_adapter.py   WSL JSON-RPC、STT、会话和打断
fish_audio.py       事件过滤、分句、WebSocket、PCM 和取消
server.py           API、依赖装配和生命周期
state.py            audio/voice 状态
```

测试至少覆盖能力刷新、demo 禁用、原生录音、STT 错误透传、转写草稿、分句、complete 去重、奇数字节 PCM、旧流失效和打断。

## 10. ESP32 音频后续边界

未来增加 ESP32 播放时只扩展 PCM sink，保留现有 Hermes 事件过滤。必须新增独立 `audio_stream_id`、包序号/时间戳、重采样、抖动缓冲、I2S DMA 和取消控制包；不能复用显示帧队列或图像 NACK 策略。
