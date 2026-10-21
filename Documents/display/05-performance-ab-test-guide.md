# 显示性能 A/B 与故障定位

## 1. 指标

每次测试至少记录：

```text
scene, transport, format/Q/chroma, browser state, ESP log state
Host fps, LCD fps, Preview fps
frame_id, displayed_frame_id, lag
queue depth, dropped, incomplete, no_buffer
decode_avg_us, lcd_avg_us, frame_avg_us
commit/worktree and flashed firmware
```

错误使用 `delta=end-start`。累计值不为零不证明当前窗口仍在丢帧。

## 2. 标准流程

1. 重启 Host，确认没有残留项目 Chromium。
2. 烧录与当前代码一致的固件，关闭 ESP 日志。
3. 固定场景和参数，预热 5 秒。
4. 每秒采样一次，持续 20 秒。
5. 改变一个变量，重复同样窗口。
6. 比较 FPS、lag、帧大小/chunk 数和错误增量。

## 3. 当前性能基线

健康局域网下，Link 页、浏览器预览开启、NACK Off 的已验证范围：

| 模式 | Host FPS | LCD FPS | Preview FPS | lag | 稳定错误增量 |
|---|---:|---:|---:|---:|---:|
| Indexed I2 | 约 29.6 | 约 29.6 | 约 28.6 | 0-2 | 0 |
| MJPEG Q90/420 | 约 29.6 | 约 29.6 | 约 28.0 | 约 1 | 0 |

一次阶段采样：

```text
Indexed decode/expand ~15.7 ms, LCD ~18.9 ms, frame ~21.4 ms
MJPEG decode ~22.7 ms, LCD ~27.3 ms, frame ~29.1 ms
```

阶段可能重叠，不能直接相加推导 FPS。

### 3.1 2026-07-17 网络抖动诊断

症状是 Host 仍尝试约 30 FPS，但 LCD 在约 18-29 FPS 间变化，同时 Indexed `dropped/no_buffer` 增长。确认不是 FPS 计数器假波动：ESP 实际完整帧队列确实发生替换。

修复包括：

```text
Indexed frame buffers  3 -> 4
Host max in-flight     3 displayed-unacked frames
ESP result/ACK poll    20 ms -> 2 ms
legacy tile idle flush remains 20 ms
```

桌面预览开启后的 30 秒最终窗口：

```text
Host avg 24.00 FPS, range 19.42-26.47
LCD avg 24.28 FPS, range 18.90-28.91
lag max 3, queue max 1
dropped delta 0, no_buffer delta 0
```

此窗口验证了背压和缓冲修复：Host 与 LCD 对齐且不再丢帧；它不是新的硬件性能基线。同步对照 ping 显示网关 `2-314 ms, avg 54 ms`，ESP32 `5-284 ms, avg 56 ms`，且 `192.168.1.0/24` 已走本地接口而非 sing-tun。两者抖动接近，瓶颈位于 PC 虚拟网卡/WLAN/AP 路径。网络恢复正常后仍以本节前面的约 29.6 FPS 为目标。

## 4. Indexed 格式 A/B

| 格式/场景 | Host | LCD | 压缩/chunks | 结论 |
|---|---:|---:|---:|---|
| I2 / 中性 UI | 29.70 | 29.68 | 少量 | 默认基线 |
| I4 / 中性 UI | 29.65 | 29.69 | 少量 | 受控 16 色可维持 30 FPS |
| I8 / 中性 UI | 29.33 | 29.14 | 内容相关 | 接近 30 FPS，余量较小 |
| I4 / Color Bars | 29.62 | 29.62 | 817 B / 1 | 高效 |
| I8 / Color Bars | 25.31 | 24.72 | 18447 B / 14 | packet gap累积成为瓶颈 |

73894 B、53 chunks 的高熵 I8 压力帧可完整显示，`rejected=0`、`draw_failed=0`。这验证长度和缓冲正确，不代表该内容适合实时 I8。

## 5. 色彩判定

- `256 Colors` + I8：应保留 256 个 RGB565 对齐色。
- `Channel Ramps` + I8：R/G/B 条不得污染其他通道；灰阶允许 RGB565 位深造成的微小通道差。
- `Color Gamut`：源颜色超过 256，I8 量化和色带合并正常。
- 当前帧不存在的保护色不得占调色板。
- Tone/Sharpness 会在纯色色块边缘产生过冲；三个原始诊断图必须显示 `Tone: Bypassed`。

## 6. 快速切页/Gamma 压力测试

旧故障：持续切换 Hermes/Lights/Link 后，Host Preview 正常，但 ESP displayed frame和 LCD FPS冻结；Indexed worker永久停在 stage 4，queue depth增至 2，no-buffer持续增长。

根因：每个场景关键帧重新发送 Gamma 控制，UDP task 的 `esp_lcd_panel_io_tx_param(GAMSET)` 与 RGB DMA并发访问 panel IO。

修复：缓存当前 Gamma；同值不发送；实际变更在 `s_display_mutex` 内执行。

实机复测：

```text
连续场景切换 400 次
displayed frame持续增长
worker stage恢复 0
queue depth恢复 0
dropped/no_buffer/draw_failed delta = 0

运行中 Gamma 1.8/2.2 连续切换 20 次
LCD恢复约 29.65 FPS
worker/queue/error正常
```

## 7. 快速故障表

| 现象 | 优先检查 |
|---|---|
| Host/LCD 同时低 | Host截图/编码、Windows等待、残留进程 |
| Host高、LCD低且队列涨 | ESP decode/LCD、packet burst、帧大小 |
| Host/LCD 同步波动且网关 ping 也抖 | PC 网卡、虚拟交换机、WLAN/AP；不要先增加 ESP 队列 |
| incomplete增长 | 丢包、payload不一致、socket buffer |
| Preview低、Host/LCD正常 | 前端轮询、frame ID去重、图片解码 |
| lag持续扩大 | ACK处理、全局锁、ESP队列、epoch |
| 切模式闪旧页 | stale worker、旧epoch、多页面动画时钟 |
| worker stage 4冻结 | LCD submit/panel IO并发、DMA完成信号 |
| 规则性色线 | Tone sharpen、量化器，不先归因于LCD |
| 小字发灰/抖动 | Gray AA、动态调色板、保护色、bpp |

## 8. 自动检查

Host：

```powershell
# 从项目根目录执行
Set-Location apps\demo_host
.\.venv\Scripts\python.exe -m pytest -q
```

Firmware：

```powershell
# 从项目根目录执行，并先激活本机 ESP-IDF 环境
Set-Location firmware\esp32_display
idf.py build
```

自动测试不替代 20 秒实机窗口、模式双向切换和快速切页压力测试。
