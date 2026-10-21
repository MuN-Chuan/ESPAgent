# ESPAgent 本地桌面应用

## 1. 产品入口

ESPAgent 的产品入口是 Windows 桌面应用。桌面壳使用 `pywebview` 调用系统 WebView2，FastAPI Host 提供状态、模块、固件、语音和显示传输能力。`/debug/` 网页只保留为开发诊断入口。

### 安装版

运行 `ESPAgent-Setup-x64.exe` 后，应用默认安装到：

```text
%LOCALAPPDATA%\Programs\ESPAgent
```

安装版已包含 Python 运行时和应用依赖。最终用户不需要 Python、ESP-IDF 或源码目录，可以直接从开始菜单、快捷方式或安装目录中的 `ESPAgent.exe` 启动。

应用直接启动且没有控制台时，会自动把日志写入 `%LOCALAPPDATA%\ESPAgent\logs\espagent_app.log`。日志目录无法创建时会降级到空设备，不会因 Uvicorn 访问不存在的 `stderr` 而退出。

### 源码开发

项目根目录的启动脚本用于开发和诊断：

```powershell
.\start_espagent_app.cmd --console
```

脚本使用 `apps/demo_host/.venv` 和源码资源，默认把日志输出到当前终端；需要文件日志时传入 `--log-file logs\espagent_app.log`。独立启动 Host 的方法见[构建、运行与演示手册](./demo-runbook.md)。

安装版和源码版不要共用“是否需要系统 Python”的说明：前者不需要，后者需要可创建虚拟环境的 Python。

## 2. 配置与迁移

安装版配置文件：

```text
%LOCALAPPDATA%\ESPAgent\.env.local
```

也可以把 `.env.local` 放在 `ESPAgent.exe` 同目录。冻结版把用户数据目录配置作为基础，再用安装目录文件覆盖其中的同名项；安装目录文件没有写出的 Home Assistant、Fish Audio 等配置仍从用户数据目录继承。该方式不要求设置系统环境变量，移除安装目录文件后恢复只使用用户数据目录配置。

源码版配置文件：

```text
apps\demo_host\.env.local
```

安装版首次运行且用户配置不存在时，会依次检查：

1. `ESPAGENT_LEGACY_ENV_FILE` 指定的文件。
2. `Documents\Codex\ESPAgent\apps\demo_host\.env.local`。
3. OneDrive Documents 下的同名路径。
4. 当前工作目录下的 `apps\demo_host\.env.local`。

迁移只发生一次，不覆盖已存在的用户配置。安装包不包含 Hermes、WSL、Home Assistant、Fish Audio 服务、模型、Token 或 API Key；这些外部服务必须由用户单独提供并在 `.env.local` 中连接。

## 3. 启动与生命周期

桌面应用默认创建自己的 `127.0.0.1:8787` Host，并清理旧桌面实例占用的端口，避免新窗口复用即将退出的旧 Host。只有开发调试明确需要连接已启动 Host 时才使用 `--reuse-host`。

WebView2 仅对 `127.0.0.1` 和 `localhost` 设置进程级代理绕过，不修改系统代理或 v2rayN。窗口先显示本地加载状态，再进入产品界面；失败时应查看应用日志，而不是只依据空白窗口判断。

关闭由应用创建的窗口会停止其 Host、显示 worker、模块运行时和全局快捷键。复用的外部 Host 不由窗口关闭。

## 4. 页面结构

| 页面 | 主要任务 |
|---|---|
| 总览 | ESP32、Hermes、Home Assistant、Host/LCD FPS、当前活动和屏幕预览 |
| 模块 | 模块启停、状态和入口 |
| 助手 | Hermes 文本、流式回复、新会话、打断、麦克风和 TTS 状态 |
| 家居 | 灯具开关、亮度、色温和同步状态 |
| 显示 | Indexed/MJPEG 热切换、模式参数、背光、Gamma、方向和预览限制 |
| 固件 | ESP32-S3 自动识别、烧录、USB 配网和屏幕 GPIO |
| 设置 | 主题、字体、缩放、显示调校、快捷键、软件版本和语音日志位置 |
| 诊断 | 测试图、视频、链路计数、ESP 日志和原始状态 |

画面预览只在需要的页面请求。隐藏页面和模块 iframe 应停止高频预览与轮询，避免桌面窗口影响 ESP32 发送和 LCD FPS。

模块清单来自 `/api/modules`。模块页运行在独立 iframe 中，带设备场景的模块激活后同步切换 ESP32 页面。接口见[模块开发指南](./module-development-guide.md)。

## 5. 屏幕控制与文字输入

默认使用 `Alt+\`` 开启或关闭 ESP32 屏幕控制。ESP32 离线时，控制和语音快捷键不注册。

| 默认按键 | 动作 |
|---|---|
| `←` / `→` | 非 Hermes 输入态时循环切换功能页 |
| `Alt+↑` / `Alt+↓` | 滚动当前设备页 |
| `Ctrl+Alt+V` 按住 | 切到 Hermes、录音；释放后转写到输入框 |
| `Alt+\`` | 再次按下退出控制 |

“设置”页可以录入任意多键组合。录入会持续到再次点击停止，最多保存 12 个不同按键。页面切换和滚动前缀可设为无前缀、自定义或关闭；退出快捷键可复用控制组合、自定义或关闭。

Hermes 输入态使用 Windows 输入法。已提交文本、预编辑拼音、候选词、选择项和插入光标同步到 ESP32；左右键有候选时循环选词，无候选时移动文本光标。`Enter` 发送后隐藏输入区，用户消息显示为右对齐黑底白字矩形。

## 6. 原生语音

Windows Host 使用 `sounddevice` 从系统默认麦克风采集 16 kHz 单声道 WAV，停止后交给 Hermes 网关的异步 STT worker pool。转写和预热被登记为长任务，不占用 JSON-RPC 主读取线程，因此长转写、识别失败或超时不会阻塞文字对话和 `voice.toggle`。faster-whisper 模型在持久网关进程中只加载一次，应用启动后后台预热；WSL 不打开麦克风，也不要求 WSLg 或 PulseAudio。

只有真实 Hermes adapter 报告 STT 能力时，语音按钮和快捷键才可用。demo adapter 在 `/api/state` 中保持 `voice_supported=false`，语音接口返回不支持并禁用麦克风，不模拟识别。默认识别语言为 `zh`，默认转写超时为 600 秒；可分别用 `HERMES_STT_LANGUAGE` 和 `HERMES_STT_TIMEOUT_SECONDS` 覆盖。

安装版将分阶段语音诊断写入 `%LOCALAPPDATA%\ESPAgent\logs\espagent_app.log`。`voice_record_*` 表示 Windows 录音，`voice_stt_*` 表示 Host 总耗时，`hermes_stt_*` 表示 RPC 排队与请求耗时，`hermes_gateway_transcribe_*` 表示 WSL 内 provider 的实际推理耗时。日志只记录时长、WAV 大小、provider、字符数和错误类型，不记录音频或转写文本。

完整链路和故障定位见[Hermes 语音输入与 Fish Audio TTS](./audio/fish-audio-hermes-streaming-guide.md)。

## 7. 桌面副屏

`builtin.desktop-mirror` 使用 DXGI Desktop Duplication 捕获显示器，保持原比例缩放到当前 240x320 或 320x240 画布，空余区域填充黑边。采集上限 `0` 表示采集线程不主动限帧，由发送链路消费最新帧。

- MJPEG 适合视频和大面积连续变化。
- Indexed LZ4 适合文字、窗口和低色桌面，彩色桌面建议 I8。

场景传输配置只在副屏激活期间覆盖全局设置，离开后恢复。

## 8. 本地入口

```text
产品界面  http://127.0.0.1:8787/app/
开发诊断  http://127.0.0.1:8787/debug/
设备 UI   http://127.0.0.1:8787/screen/
API 文档  http://127.0.0.1:8787/docs
```

本地 API 当前没有鉴权，只应绑定回环地址或运行在受信任网络中。

## 9. 打包边界

打包配置位于 `packaging/`。构建命令：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

产物：

```text
dist\ESPAgent\ESPAgent.exe
dist\installer\ESPAgent-Setup-x64.exe
```

安装包包含桌面 UI、设备 UI、内置模块、测试资源和发行固件，不包含外部 Hermes 服务及其模型或用户凭据。更详细的构建依赖见[打包说明](../packaging/README.md)。

## 10. 验收

- 安装机无 Python 和 ESP-IDF 时可以直接启动应用并打开产品页。
- 直接运行安装目录中的 `ESPAgent.exe` 与快捷方式行为一致。
- 日志写入 `%LOCALAPPDATA%\ESPAgent`；配置默认写入该目录，也可由 `ESPAgent.exe` 同目录的 `.env.local` 按字段覆盖。
- 1024x700 和 1280x820 下导航、控件和预览不重叠。
- 页面切换不重启 Host 或 ESP32；离开预览页后资源占用下降。
- Indexed/MJPEG、横竖屏和模块场景切换无需重启固件。
- 固件页可完成识别、烧录、USB 配网和 GPIO 设置，不要求手动烧录参数。
