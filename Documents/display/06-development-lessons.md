# 显示链路开发原则

## 1. 定义完成点

区分 render、send、receive、LCD complete。当前 `displayed_frame_id` 只表示 LCD pipeline成功完成；Preview和性能判断不能把 Host send当作实屏显示。

## 2. FPS 与帧号一起看

最小观测集：Host/LCD FPS、frame/displayed ID、lag、queue和错误增量。固定不变的 LCD FPS 可能只是旧采样，必须同时确认 displayed ID在前进。

## 3. 昂贵操作不持有共享锁

截图、编码、量化、LZ4、sleep、UDP和外部服务都在锁外。锁内只复制 snapshot或一次提交完整业务状态。

## 4. 使用绝对时间调度

`do_frame(); sleep(interval)` 会把工作耗时叠加到周期。使用 deadline或“补足剩余时间”，并实测 Windows等待精度。

## 5. 进程生命周期属于性能

Chrome、ffmpeg和辅助服务必须覆盖正常退出、强制终止和多次重启。回退代码前检查残留进程、端口、旧固件和运行时配置。

## 6. 先 profile再优化

本项目真实热点曾是 Python逐像素 bit packing、小数组NumPy调用和错误等待原语，而不是理论上最复杂的算法。优化后必须做输出等价、协议和实机测试。

## 7. 协议双端同步

payload、pixel format、长度、字节序、bpp、mode和epoch任一端单改都会产生随机 reject或旧画面。I8 说明“扩大缓存”不足以处理超过 16 位长度，必须同步升级所有长度解释和边界测试。

## 8. 扩展模式不拖慢默认路径

I1/I2保持内部 RAM fast buffer；I4/I8才使用大 extended buffer。新能力所需的内存和访问成本由该能力承担。

## 9. NACK不是默认越多越好

实时显示优先最新完整帧。补旧帧会增加突发、延迟和区域时序差。MJPEG默认NACK Off，Indexed主线不使用NACK；只有明确丢包诊断才开启。

## 10. Preview不控制物理链路

Preview读取缓存、按frame ID去重，不触发新设备渲染、不持有发送锁，限速也不修改ESP发送FPS。

## 11. 模式切换使用epoch

mode枚举不足以隔离迟到包和已排队任务。Host控制、ESP收包、入队、worker拿LCD锁和result消费都要检查epoch。

## 12. Panel IO与DMA必须统一所有权

画面DMA之外的Gamma、MADCTL或其他panel command也会访问同一SPI IO。运行时命令必须去重并与LCD mutex串行；不能因为命令很短就从接收任务直接并发发送。

## 13. 颜色诊断与产品调校分离

原始诊断图绕过Tone和锐化。量化容量、通道污染、面板Gamma和产品审美必须用不同测试图判断，不能用一个超容量全色图同时证明所有问题。

## 14. 优化顺序

1. 建立计数器和可重复A/B。
2. 修复帧完整性、mode/epoch和LCD所有权。
3. 解耦UDP接收与decode/LCD。
4. 缩短状态锁。
5. 优化Host热点和调度。
6. 优化block decode、字节序和局部DMA。
7. 调整Q、Chroma、bpp和保护色。
8. 最后才增加缓存、并发、优先级或核心绑定。

## 15. 不再重复的方案

- 用tile重复发送掩盖完整性问题。
- 无限减小packet gap或无限增加队列。
- 为锐利小字直接改MJPEG 4:4:4。
- 用请求次数计算Preview FPS。
- 未检查残留进程就反复回退Git。
- 修改固件后只重启Host而不烧录。
- 只覆盖调色板槽而不重新映射原像素。
- 让Tone/Sharpness参与原始色彩诊断。
- 在LCD DMA进行时从其他任务发送panel command。
