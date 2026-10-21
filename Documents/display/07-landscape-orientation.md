# 横竖屏运行时切换

## 1. 使用方式

桌面应用进入“设置 -> 外观 -> 屏幕方向”，选择：

| 选项 | 逻辑尺寸 |
|---|---:|
| 竖屏 | 240x320 |
| 横屏 | 320x240 |

切换不需要重启 Host 或 ESP32。设置写入 `display_ui.orientation`，显示 worker 在下一帧生成新尺寸并向 ESP32 发送带新 epoch 的 display mode packet。

## 2. UI 策略

横屏不是把竖屏页面整体缩小，而是重新排版：

- Hermes：回复区扩宽，输入法候选区放在右侧。
- Lights：状态和亮度放左侧，亮度/色温分段放右侧。
- Link：FPS 放左侧，诊断指标放右侧。
- Codex：额度读数放左侧，周期、模型和配置放右侧。
- 视频、测试图和桌面副屏：按 320x240 输出，不拉伸桌面副屏内容；`contain` 模式保留源比例并留边。

主题通过 `#display[data-orientation="landscape"]` 覆盖布局。页面逻辑不复制，横竖屏仍使用同一 DOM 和状态更新代码。

## 3. Host 数据流

```text
DisplayUiState.orientation
  -> DisplayGeometry(240x320 / 320x240)
  -> Chromium metrics / Pillow / video / desktop capture
  -> Indexed or MJPEG encoder
  -> DisplayMode(orientation + new epoch)
  -> frame packets
```

方向切换会：

1. 清空 Web renderer 中旧尺寸的异步截图。
2. 重置 Indexed 编码缓存和 MJPEG pacing 时间。
3. 清空 Host 重传帧缓存并强制关键帧。
4. 递增 transport epoch。
5. 让 Preview frame 与发送给 ESP32 的 frame 使用同一尺寸。

I1/I2/I4 packet 在现有 header 字段中携带宽高。I8 的 raw 长度固定为 76800，宽高由已确认的 display mode 决定。为兼容旧固件，竖屏 display mode 保留 6 字节 payload；横屏追加第 7 字节 orientation。

## 4. ESP32 数据流

固件收到新 mode/epoch 后，在 `s_display_mutex` 内：

1. 丢弃两种 transport 中尚未显示的完整帧。
2. 调用 `esp_lcd_panel_swap_xy()` 和 `esp_lcd_panel_mirror()`。
3. 更新运行时 `DISPLAY_WIDTH/DISPLAY_HEIGHT`。
4. 清屏，避免旧方向残影。
5. 按新尺寸校验、解码和提交 LCD block。

LCD framebuffer 固定为 76800 像素；通用 DMA、JPEG block 和 Indexed block buffer 按最大行宽 320 分配。横屏没有增加整帧 PSRAM 占用，只增加局部内部 RAM buffer 的行宽余量。

## 5. 验证基线

2026-07-19，ESP32-S3 实机、80 MHz SPI、动态视频源：

| 方向/模式 | Host TX FPS | LCD FPS | Reject |
|---|---:|---:|---:|
| 横屏 / MJPEG | 29.22 | 28.91 | 0 |
| 竖屏 / MJPEG | 29.58 | 30.03 | 0 |
| 竖屏 / Indexed I2 | 29.66 | 29.64 | 0 |

另一次横屏 Indexed 稳态采样的 LCD FPS 为 29.66，Preview 尺寸为 320x240，Host/ESP epoch 一致。切换测试必须同时检查：

- `transport_epoch == esp_transport_epoch`
- `frame_id - displayed_frame_id` 不持续增长
- `rejected_packets` 和 `draw_failed_packets` 增量为 0
- Indexed/JPEG queue 回到 0
- `/api/preview.png` 尺寸与方向一致

物理安装方向不同只应调整 ST7789 mirror 组合，不应在 Host 旋转像素或交换协议宽高。
