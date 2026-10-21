# ESPAgent 构建、运行与演示手册

## 1. 最短产品流程

1. 安装 `dist\installer\ESPAgent-Setup-x64.exe`。
2. 启动 ESPAgent，进入“固件”页。
3. 连接带外部 PSRAM 的 ESP32-S3，点击“烧录固件”。
4. 在同一页面通过 USB 写入 Wi-Fi，必要时写入屏幕 GPIO。
5. 回到“总览”，确认 ESP32 online、`displayed_frame_id` 和 LCD FPS 持续增长。
6. 在“显示”或“诊断”页验证 Indexed、MJPEG、横竖屏、测试图和视频。

最终用户不需要 Python、ESP-IDF、固件地址或命令行。安装版配置和日志位置见[本地桌面应用](./desktop-app-guide.md)。

## 2. 外部服务配置

安装版编辑：

```text
%LOCALAPPDATA%\ESPAgent\.env.local
```

源码版编辑：

```text
apps\demo_host\.env.local
```

最小离线演示：

```dotenv
HERMES_MODE=demo
ESP32_DISPLAY_PORT=39000
ESP32_STATUS_PORT=39001
```

真实服务按需增加：

```dotenv
HOME_ASSISTANT_URL=http://127.0.0.1:8123
HOME_ASSISTANT_TOKEN=...
HOME_ASSISTANT_LIGHT_ENTITY=light.demo_light

HERMES_MODE=real
HERMES_WSL_DISTRO=ESPHermes
HERMES_COMMAND=/path/to/hermes/venv/bin/hermes -z
HERMES_STT_LANGUAGE=zh
HERMES_STT_TIMEOUT_SECONDS=600

FISHAUDIO_ENABLED=true
FISHAUDIO_API_KEY=...
FISHAUDIO_VOICE_ID=...
```

安装包不部署这些外部服务。Token、Key 和密码不得进入文档、前端或 Git。

## 3. 源码运行

启动桌面应用并保留终端：

```powershell
.\start_espagent_app.cmd --console
```

仅启动 Host：

```powershell
.\start_demo_host.cmd --console
```

开发入口：

```text
产品界面  http://127.0.0.1:8787/app/
开发诊断  http://127.0.0.1:8787/debug/
设备 UI   http://127.0.0.1:8787/screen/
API 文档  http://127.0.0.1:8787/docs
```

源码日志位于 `logs/`。修改 Python、主题、CSS 或 JavaScript 后应重启 Host，持久 Chromium 不保证热加载全部资源。

## 4. 固件开发

以下步骤只用于修改固件源码，不是最终用户烧录流程。先在 `firmware\esp32_display` 中运行 `idf` 激活 ESP-IDF：

```powershell
Set-Location firmware\esp32_display
idf
idf.py build
idf.py flash
```

项目根目录没有固件 `CMakeLists.txt`，不能在根目录运行 `idf.py build`。发布镜像必须复制到 `firmware\releases\esp32_display\` 并同步更新 `manifest.json` 中的版本、大小和 SHA-256。

## 5. 推荐演示

1. 总览：确认服务状态和 ESP32 实时画面。
2. 助手：验证 demo 或真实 Hermes 的文本、流式回复、新会话和打断。
3. 语音：真实 Hermes STT 可用时，按住语音快捷键说话，释放后修改转写并发送。
4. 家居：验证 Home Assistant 灯具的开关、亮度和色温。
5. 模块：验证 Codex 额度、桌面副屏和 WLED 灯条。
6. 显示：热切换 Indexed/MJPEG；用视频验证 MJPEG，用色板与通道图验证 I8。
7. 固件：演示端口识别、USB 配网和 GPIO 读取，不在演示中重复擦写固件。

## 6. 显示验收

在稳定 20 秒窗口观察：

- `frame_id` 和 `displayed_frame_id` 持续增长。
- lag 通常为 0 至 2。
- `rejected`、`draw_failed` 和当前模式的 incomplete/no-buffer 不持续增长。
- 快速切页后 worker stage 与队列恢复为空。
- 打开桌面窗口不应长期压低 LCD FPS；离开预览页后预览请求停止。

完整判定方法见[性能测试与故障定位](./display/05-performance-ab-test-guide.md)。

## 7. 常见故障

| 现象 | 处理 |
|---|---|
| 安装版直接启动报 Uvicorn formatter/`isatty` | 更新到包含无控制台日志修复的安装包；检查 `%LOCALAPPDATA%\ESPAgent\logs\espagent_app.log` |
| 窗口白屏或 localhost 拒绝连接 | 检查应用日志、8787 端口和 WebView2 Runtime；确认没有旧 Host 残留 |
| ESP32 offline | 用固件页重新 USB 配网；检查 Wi-Fi、防火墙、39000/39001 和状态来源 IP |
| 预览更新、实屏不更新 | 检查 transport/epoch、`displayed_frame_id`、固件版本和 ESP 计数 |
| LCD FPS 冻结 | 检查 worker stage、队列、DMA 错误和快速切页后的 Gamma 命令 |
| LCD 偏白或暗部不足 | 保持 Tone Off，比较 Gamma 1.8/2.2 与背光，不用锐化修正诊断图 |
| 麦克风按钮禁用 | 需要 `HERMES_MODE=real` 且 Hermes STT 可用；demo 模式不支持语音 |
| 录音失败或识别为空 | 检查 Windows 默认输入设备和麦克风权限，再检查 `/api/voice/status` 的设备与 STT 详情 |
| 转写超时后 Hermes 无响应 | 新版把 STT 登记为异步长任务；确认安装包版本，旧版同步 `espagent.voice.transcribe` 会阻塞网关 |
| Hermes 有文字但无 TTS | 检查 Fish Audio 配置、`/api/health` 和 Windows 默认扬声器 |
| 浏览器可访问 HA 但应用离线 | 检查 Token 和实体 ID；局域网地址会自动绕过系统代理，公网地址保留系统代理 |
| WLED 频繁离线 | 使用固定局域网地址，检查供电、Wi-Fi 和轮询错误；不要把状态刷新当作高频帧循环 |

## 8. 验证命令

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/health | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8787/api/state | ConvertTo-Json -Depth 8
```

源码完整回归：

```powershell
Set-Location apps\demo_host
.\.venv\Scripts\python.exe -m pytest -q tests
```

Windows 安装包构建：

```powershell
Set-Location ..\..
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```
