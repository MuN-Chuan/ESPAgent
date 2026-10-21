# 第三方资源说明

ESPAgent 自有代码目前未指定项目级开源许可证。本文件仅记录已知第三方资源及其来源，不替代各项目的许可证。

| 资源 | 使用方式 | 来源与许可 |
| --- | --- | --- |
| Ark Pixel Font | 设备 UI 像素字体 | [TakWolf/ark-pixel-font](https://github.com/TakWolf/ark-pixel-font)，SIL Open Font License 1.1；原文保留在 `apps/demo_host/display_ui/fonts/ark-pixel/OFL.txt` |
| esp_new_jpeg | ESP-IDF 组件依赖，未将下载后的组件目录复制进仓库 | [Espressif 组件](https://components.espressif.com/components/espressif/esp_new_jpeg/versions/0.5.0)，许可随组件获取 |
| WLED | 灯条模块对接与发行固件 | [wled/WLED](https://github.com/wled/WLED)，EUPL-1.2 或后续版本；许可原文见 `firmware/releases/LICENSE-WLED.txt`，来源见 `firmware/releases/*wled/` |
| WLED installer layout | SuperMini 初次烧录布局 | [wled-install/wled-install.github.io](https://github.com/wled-install/wled-install.github.io)，原项目许可适用 |
| ChatGPT 标识 | Codex 额度功能页中的服务标识 | 标识及相关商标归其权利人所有；本项目并非 OpenAI 官方产品 |
| Python / Windows 依赖 | 通过包管理器或系统环境获取 | 版本范围见 `requirements.txt`、`apps/demo_host/pyproject.toml` 与 `packaging/requirements-build.txt`，各依赖保留自己的许可 |

`docs/media/` 为用户提供的设备实拍展示素材，经压缩、无声转码并清除原始元数据。视频中的游戏、角色与其他第三方内容及商标归各自权利人所有；本仓库不额外授予这些内容的使用权。原始测试影片和个人录音不包含在公开源码中。
