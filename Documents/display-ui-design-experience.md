# ESPAgent 小屏 UI 设计经验

## 1. 以最终实屏为准

当前产品页链路：

```text
DemoState -> HTML/CSS/JS -> Chromium 240x320 or 320x240 screenshot
          -> Binary/Gray AA -> Tone
          -> Indexed LZ4 or MJPEG -> RGB565 -> ST7789
```

检查优先级：ESP 实屏、编码后 Host 预览、无损 PNG、原始 `/screen/`。浏览器页面适合检查 DOM 和排版，不是最终像素标准。

## 2. 页面层级

- 一个页面只保留一个主视觉：Hermes 回复、灯具状态或 Link FPS。
- 使用连续画布、分隔线、留白和稳定对齐，不使用卡片网格。
- 左右安全边距建议至少 12 px；像素字体额外检查墨迹是否超出 advance width。
- 文本适配必须同时检查 `scrollWidth` 和 `scrollHeight`。
- 英文、中文和数字使用独立字体角色；单一 fallback stack 无法阻止第一个全字符字体接管全部脚本。

## 3. 字体

推荐：

- 8 至 14 px 或像素字体：`crisp`、Gray AA Off。
- 16 px 以上轮廓字体：比较 `auto/smooth` 与 Gray AA On。
- 禁止伪粗体和负 letter-spacing；像素字体尽量使用设计字号的整数倍。
- 长回复使用离散字号序列重排，不按 viewport 宽度连续缩放字体。

Gray AA 只保留背景到文字之间的中性色，不保留 ClearType 彩边。它能改善曲线，但会增加颜色数和压缩负担；小字可能变粗或发灰。

## 4. 配色

浅底深字在 ST7789、RGB565 和 MJPEG 下最稳定。当前推荐：

```text
Paper:      #F8FAFC / #111827
WhiteBlack: #FFFFFF / #000000
Editorial:  #F5EBDD / #2A1E17
Solarized:  #FDF6E3 / #073642
```

原则：

- 背景保持高亮、低通道跨度；正文使用中性深色。
- 高饱和色用于大状态块、图标或进度，不用于小字号正文。
- 深底亮字更容易暴露面板黑位、JPEG ringing、RGB565 暗部台阶和拍屏色差。
- MJPEG 4:4:4 只能消除色度降采样，不能消除 JPEG 量化、RGB565 或面板子像素问题。
- 需要像素级文字时优先 Indexed Binary，不要仅提高 JPEG Q。

安全浅色起点，不是硬标准：

```text
background channels: 235..255
background span: <= 25
text max channel: <= 90
text channel span: <= 60
contrast: 7:1..18:1
```

## 5. 颜色数量和保护色

- I1：严格双色文字和线条。
- I2：背景、文字及最多两个稳定点缀色。
- I4：受控 16 色 UI。
- I8：较多离散颜色和兼容测试；连续渐变仍优先 MJPEG。
- 保护色是 `Color N` 编码槽，不是“背景/文字”等业务语义；主题层负责映射。
- 动态调色板跨帧变化可能让低频边缘抖动。固定重要颜色、关闭抖动并减少无意义中间色比增加 bpp 更有效。

## 6. 动效

动效必须适合逐帧截图：

- 只在场景、回复或设备状态变化时运行有限时长动画。
- 不使用透明交叉淡化；二值化后会同时留下两页文字。
- 不对整页合成层做大位移；无头 Chromium 截图曾出现未绘制黑块。
- 使用固定画布上的裁切揭示，并在新切换时取消旧 animation 和 class。
- 支持 Motion Off 和 `prefers-reduced-motion`。

## 7. 缩放

不要直接缩放固定页面。当前逻辑画布根据方向和 90% 至 110% 缩放重新排版，再映射到 240x320 或 320x240 物理画布。放大后能显示的文本减少是正常结果，不应通过溢出或裁切补偿。

## 8. 已验证失败模式

| 现象 | 根因 | 处理 |
|---|---|---|
| 小字彩边 | 子像素 AA、JPEG、RGB565 叠加 | Binary 或中性 Gray AA，Indexed 优先 |
| 文字重影 | 透明交叉淡化 | 裁切揭示 |
| 动画黑块 | 整层 GPU transform | 固定画布、局部 clip |
| 右侧半字 | 墨迹超过字宽 | 同时检查宽高并增加安全边距 |
| 字体选择无效 | fallback 首字体覆盖全部脚本 | 中英数分段 |
| 色彩图规则细线 | Tone sharpen 产生过冲后被量化 | 原始诊断图绕过 Tone |
| 深底亮字脏边 | 暗部台阶、ringing、面板黑位 | 浅底深字或增大字号/字重 |
| 快速切页闪旧页 | 多页面动画时钟和旧 worker 结果 | 单页过渡、取消旧动画、transport epoch |

## 9. 验收清单

- 240x320 和 320x240 均保持严格尺寸，90/100/110% 无裁切。
- 长中文、长英文和数字混排不覆盖相邻内容。
- 小字在 Binary 下边缘稳定，Gray AA 下无彩边和帧间抖色。
- I1/I2 重要颜色已映射保护槽，I4/I8 不出现协议性串色。
- Tone Off 时诊断图像素不被修改。
- Motion Off 静态输入不产生变化帧。
- MJPEG Q90/4:2:0 和 Indexed I2 都完成实屏检查。
- 快速切页后 `displayed_frame_id` 持续增长，LCD worker 不冻结。
