# Host、Chromium 与网页预览

## 1. Host 职责

Host 同时管理业务状态、HTML UI、视频/测试图、编码、UDP、ESP 状态和控制网页。它们必须通过 snapshot、worker 和缓存隔离，浏览器打开或最大化不应改变 ESP 发送节奏。

## 2. Web 与 Pillow

- Hermes、Lights、Link、Codex：持久隐藏 Chromium screenshot。竖屏为 240x320，横屏为 320x240；方向切换只更新 Chromium device metrics，不重启浏览器。
- 测试图和视频：Pillow/OpenCV 直接生成像素帧。
- Web renderer 只保留 1 个 Chromium 页面。单页实测仍可达到 45.9 FPS，覆盖 Host 45 FPS 调度上限，不再为超出产品上限的吞吐维持 3 个独立浏览器。
- 页面通过可见状态签名按需截图：Hermes、Lights、Link 和 Codex 都由 display worker 统一调度，避免 renderer 使用相同周期二次限速而隔帧。Link 的诊断文字以 10 FPS 从 Chromium 采样，最近栅格按 worker 帧率复用发送。状态不变且动效结束后降到 1 FPS 保活截图。当前 ESP32 固件依赖显示数据回传在线状态，因此不能完全停止静态帧。测试图、视频和桌面副屏不受该门控影响。
- 前端文本节点只在值变化时更新；脚本类型未改变时复用原有中英文字体 span，仅替换文本内容，避免连续数字更新反复重建 DOM。
- 不要在 renderer 与 display worker 中配置相同帧周期。Windows 线程可能在 deadline 前几十微秒醒来，renderer 二次判断会稳定跳过下一帧；Link 的 Indexed 实测因此从约 29.5 FPS 降到约 15.9 FPS。移除重复门控并复用 Link 栅格后恢复到 29.4-29.8 FPS。
- Codex 曾单独保留 0.09 秒门控，导致 550 ms 切换动画从旧方案约 31 FPS 降到 11.19 FPS，200 ms 连续切换从 30.55 FPS 降到 26.23 FPS。Codex 静止后本来就会进入状态签名门控和 1 FPS 保活，不需要额外限制动画帧率。
- 单 Chromium 不等于截图、编码必须串行。场景切换首帧同步建立正确目标页，过渡期间由同一页面异步预取下一张截图，使 Chromium 合成与当前帧的 Indexed 编码/UDP 发送重叠；尾帧再同步校准。这样保持动画时钟单调且不增加浏览器实例。
- Indexed 编码器对同一个不可变图像对象复用 palette、索引打包和 LZ4 blob，只为新帧重建协议包头。该缓存用于 Link 的中间复用帧；新截图、场景切换、格式或保护色变化会自然失效或重建编码器。
- Chromium 启动失败时产品页可回退 Pillow；测试源不依赖 Chromium。
- `DisplayGeometry` 是 Host 尺寸的唯一来源。Web、Pillow、视频、桌面副屏、Indexed 编码和 Preview 均读取同一方向，禁止在各模块重新硬编码 240x320。
- 方向改变时必须丢弃异步截图流水线中的旧尺寸帧，再同步生成新方向首帧。否则旧的 240x320 帧可能在 320x240 mode packet 之后发送，造成短暂错帧或长度校验失败。

本机 A/B（同一 240x320 Lights 页面）：

| 配置 | 直接截图吞吐 | Chrome 增量进程 | Chrome 增量工作集 |
|---|---:|---:|---:|
| 3 个独立页面 | 121.3 FPS | 27 | 1316.7 MB |
| 1 个页面 | 45.9 FPS | 9（加入精简启动参数后为 8） | 439.6 MB（精简后约 403.2 MB） |

结论：三页流水线约多占 0.9 GB，但超过 45 FPS 的吞吐不会被显示 worker 使用，因此默认采用单页。

## 3. 状态锁

正确流程：

```text
短锁复制 DemoState -> 锁外截图/编码/pacing/UDP
                    -> 短锁回写 frame ID 和 FPS
```

禁止在锁内执行 Chrome screenshot、JPEG/PNG、调色板、LZ4、sleep 和多包发送。否则状态 API、displayed ACK 和预览会反向阻塞物理链路。

## 4. Windows 调度

当前机器的短 `Event.wait()` 曾出现 15.6 ms 量化，在完整 Host 中甚至把约 14 ms 请求拖到 26-28 ms。Indexed 改用绝对 deadline 和最多 10 ms 的 `time.sleep()` 分片；MJPEG 只补足剩余帧间隔。

结论：等待原语必须在目标 Windows 环境实测，不能把请求 timeout 当作实际 sleep 精度。

## 5. Chromium 生命周期

强制停止 Host 曾遗留数百个隐藏 Chrome 进程和 profile，持续争抢 CPU/GPU，使代码回退后性能仍无法恢复。

当前每个隐藏 Chrome 加入 Windows Kill-on-close Job Object。Host 正常或被强制终止时，项目进程树都应结束。检查：

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe' OR Name = 'msedge.exe'" |
  Where-Object { $_.CommandLine -like '*espagent-display-*' }
```

Host 停止数秒后结果应为空。只处理带项目 profile 的进程，不结束用户正常浏览器。

## 6. Preview

```text
Indexed /api/preview.png
MJPEG   /api/display/preview.jpg
```

- 帧来自 Host 缓存，不是 ESP 抓屏。
- `X-Frame-Id` 用于去重；重复请求同一帧不计 FPS。
- MJPEG 尽量按 `displayed_frame_id` 选择已刷到 LCD 的缓存帧。
- 请求和图片解码耗时从下一次等待中扣除。
- Preview 限速关闭时自动跟随 Host/LCD，仍不会创造超过源的新帧。

Preview FPS 应不高于 Host FPS，并接近 LCD FPS。Host/LCD 正常而 Preview 低时，只调查浏览器轮询、图片解码和前端计数，不修改 ESP 发送 pacing。

Preview 图片尺寸必须等于当前方向。可用 Pillow 检查：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/api/preview.png -OutFile preview.png
python -c "from PIL import Image; print(Image.open('preview.png').size)"
```

## 7. 最大化浏览器掉帧的结论

窗口状态不是根因。历史下降由全局状态锁、控制页 JSON/预览竞争、孤立 Chrome 和 Windows 短等待共同放大。修复后判断浏览器影响必须比较同一场景下的 Host FPS、LCD FPS、lag 和错误增量，而不是只看窗口大小。
