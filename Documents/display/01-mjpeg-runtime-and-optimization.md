# MJPEG 实现与调优

## 1. 定义

本项目的 MJPEG 是“每帧独立 Baseline JPEG + 自定义 UDP chunk”。JPEG 本身不提供分片、可靠性、帧完整性或显示 ACK。

```text
current geometry RGB -> Pillow JPEG -> 1400 B payload chunks
-> ESP complete-frame assembly -> esp_new_jpeg block decode
-> RGB565_BE local blocks -> LCD DMA -> displayed ACK
```

## 2. Host

- 单帧上限 65535 B，长度字段为 16 位。
- 每 chunk 携带 frame/chunk ID、尺寸、总长度、payload 长度和 CRC32。
- 发送上限 30 FPS；编码耗时从剩余 pacing 时间中扣除。
- 缓存近期 JPEG 帧，用于 NACK 和按 displayed frame 对齐网页预览。
- Q 或色度参数变化时清理旧 chunk 缓存并请求关键帧。

## 3. ESP32

```text
JPEG frame buffers       3
complete queue depth     2
result queue depth       4
chunk payload            1400 B
block max lines          16
block buffers            2
LCD transactions inflight 1
decoder                  task-level persistent handle
output                    JPEG_PIXEL_FORMAT_RGB565_BE
```

UDP 接收任务只组帧。完整帧交给 JPEG worker 后立即复用空闲接收缓存。decoder 不按帧反复 open/close，也不分配 153600 字节完整 RGB565 解码帧。

两个 block buffer 允许 decoder 在上一块 DMA 进行时写下一块；当前只允许一个 LCD transaction in flight，以保持 block 生命周期和完成信号简单可靠。

## 4. 参数

### Quality

| 内容 | 建议 Q |
|---|---:|
| 产品 UI | 85-90 |
| 视频 | 70-85 |
| 弱 Wi-Fi、帧率优先 | 55-70 |
| 静态画质比较 | 90-95 |

默认 Q90。Q 越高不一定越好：JPEG 和 chunk 数增加后，网络暴露面、解码时间和丢帧概率都会上升。

### Chroma

```text
4:2:0 -> Pillow subsampling 2 (默认)
4:2:2 -> 1
4:4:4 -> 0
```

彩色细线可比较 4:2:2；4:4:4 主要用于静态诊断。黑白小字模糊优先处理字体、Binary/Gray AA 和模式选择，而不是直接改 4:4:4。

## 5. NACK

默认 Off。关闭时不完整旧帧必须尽快丢弃，让新帧继续；偶发丢包表现为跳帧而不是卡死。

开启时 ESP 请求缺失 JPEG chunk，Host 从近期缓存补发。仅在诊断稳定丢包或关键低频帧时启用；持续动画补旧帧会增加突发和延迟。

## 6. Preview

`GET /api/display/preview.jpg` 返回 Host 缓存的 JPEG，并尽量按 `displayed_frame_id` 选择实屏已完成帧。`X-Frame-Id` 用于浏览器去重。它不是 ESP 抓屏，不占用 ESP 图像回传带宽。

## 7. 故障顺序

1. mode/epoch 是否同步，displayed frame 是否增长。
2. `jpeg_incomplete`、`jpeg_dropped` 的窗口增量。
3. JPEG 是否超过 65535 B，两端 payload 是否都是 1400 B。
4. `decode_avg_us`、`lcd_avg_us`、`frame_avg_us`。
5. Q、Chroma、chunk 数和 packet gap。
6. decoder handle、RGB565_BE block 和接收帧缓存是否仍按当前实现。

`reject jpeg chunk offset/raw/payload` 通常是两端 payload/offset 常量不一致，不应靠扩大缓冲掩盖。
