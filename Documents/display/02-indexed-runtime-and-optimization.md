# Indexed LZ4 实现与调优

## 1. 定义

当前 Indexed 是每帧独立的完整索引图，不是脏矩形 tile：

```text
current geometry RGB -> RGB565 对齐 -> 调色板量化
-> I1/I2/I4/I8 indices -> bit pack -> LZ4
-> 1400 B UDP chunks -> ESP complete frame
-> LZ4 decode -> 16-line RGB565_BE blocks -> LCD DMA
```

完整帧避免大面积变化时 tile 分区先后可见。稳定主线不使用 Indexed NACK；不完整旧帧直接放弃。

## 2. 格式

| 格式 | 调色板 | Raw indices | 典型用途 |
|---|---:|---:|---|
| I1 | 2 | 9600 B | 严格双色 |
| I2 | 4 | 19200 B | 默认文字 UI |
| I4 | 16 | 38400 B | 受控多色 UI |
| I8 | 256 | 76800 B | 离散高色和诊断 |

压缩帧上限为 80 KiB。I8 超过 16 位 raw length：`raw_len=0` 表示固定 76800 B，`x/y` 合成 32 位 frame length，`w` 表示 chunk count，`h=0`。I1/I2/I4 保留原帧头布局。

## 3. 量化

### I1/I2

采用频率优先、相似颜色吸附和相邻边界保护。距离为：

```text
2*dr^2 + 4*dg^2 + db^2
```

代表色先对齐 RGB565，并优先保留真实高频源颜色，避免平均出画面中不存在的新色。

### I4/I8

采用 RGB565 对齐后的 Pillow Maximum Coverage，关闭 dithering。不要复用 I1/I2 的强近色吸附阈值；它会让 I8 灰阶和色彩容量塌缩。Fast Octree 曾在连续色相边界产生规则矩形色块。

## 4. Binary、Gray AA 和保护色

Binary 只保留深浅端点，适合小字。Gray AA 保留中性覆盖率，适合较大曲线字形，但会占用更多颜色并可能让小字发灰。

保护色使用固定 `Color N` 槽：

```text
I1 2 slots, I2 4, I4 16, I8 256
```

数组位置稳定，`null` 表示禁用。仅当目标 RGB565 色在当前帧出现时才占调色板。替换普通槽后必须重新映射原槽像素，不能只覆盖调色板值。背景、文字和品牌色等语义由主题层映射，不进入底层编码器。

## 5. Host 性能实现

- NumPy 向量化 bit packing，替代 76800 次 Python 循环。
- 三通道高频距离使用整数标量，避免大量微型 NumPy 数组。
- I4/I8 高色量化使用原生 Maximum Coverage。
- worker 使用 30 FPS 绝对 deadline 和最多 10 ms `time.sleep()` 分片。
- ESP32 在线时，Host 根据逐帧 `displayed ACK` 将未显示帧限制为最多 3 帧；网络或 LCD 暂停时先等待 ACK，不继续把完整帧灌入 ESP 队列。
- 截图、量化、LZ4 和 UDP 发送均在状态锁外。

## 6. ESP32 内存路径

```text
compressed frame buffers  4 x 80 KiB
complete queue            2
result queue              4
I1/I2 fast indices        19200 B internal RAM
I4/I8 extended indices    76800 B, PSRAM preferred
block buffers             2 x 16 lines, DMA capable
```

四个压缩缓冲分别覆盖当前接收帧、worker 帧和最多两个完整排队帧。旧的三个缓冲无法同时兑现深度 2 的完整队列，短时网络突发会产生 `no_buffer`。新增缓冲占用 80 KiB PSRAM。

扩展格式的成本只由 I4/I8 承担，默认 I2 保持内部 RAM 快路径。worker 先完整 LZ4 解压 indices，再逐块展开 RGB565_BE；前一块 DMA 时可展开后一块。

## 7. 选择与故障

- 使用能完整容纳重要颜色的最低 bpp。
- I8 高熵帧可接近 raw 大小并产生数十个 chunk，视频和连续渐变应改用 MJPEG。
- I4 在复合全色测试中无法同时保留完整色域和 16 级灰阶，这是容量限制。
- `256 Colors` 与 `Channel Ramps` 在 I8 下应无跨通道污染；`Color Gamut` 超过 256 色，色带合并属于量化结果。
- 静态输入下规则错误块、跨通道污染或调色板抖动才按实现错误调查。
- `dropped/no_buffer` 增长且局域网 RTT 同时出现数十到数百毫秒尖峰时，应先检查 PC 网卡、AP 和路由；增加 ESP 队列只能换取更高延迟，不能修复链路停顿。

PC 端 UI 直出最终 indices 尚未实现；当前保留 HTML/CSS 截图生态。
