# WS2812B 灯条 API 与 Hermes Skill

## 目标与边界

灯条桌面 UI、HTTP API 和 Hermes 共用 `builtin.ws2812b` 模块运行时。所有命令都经过运行时的节流队列、乐观状态和错误发布，不允许 Hermes 绕过 Host 直接请求 WLED。

专用 API 默认位于 `http://127.0.0.1:8787`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/light-strip` | 返回 API 版本、动作说明、风险等级和完整模块状态 |
| `POST` | `/api/light-strip/actions/{action}` | 调用一个灯条动作，请求体为 `{"payload": {...}}` |

通用 `/api/modules/builtin.ws2812b/...` 接口继续保留，桌面模块 UI 无需迁移。专用接口是给 Hermes、自动化和外部本地调用方使用的稳定入口。

## 动作

日常灯光控制：

- `set_power`：`power` 布尔值。
- `set_brightness`：`brightness` 为 `0..100`。
- `set_color`：`color` 为 `#RRGGBB`。
- `set_state`：一次提交开关、亮度、颜色中的一项或多项。
- `set_effect`：`preset_id` 必须来自状态中的 `effect_presets[].id`，`solid` 表示纯色。
- `refresh`：刷新设备、串口和特效信息。

持久配置和管理：

- `set_led_count`：设置 `1..1200` 颗灯珠。
- `configure`：修改 WLED HTTP 地址。
- `set_firmware_target`：选择状态中声明的固件目标。
- `provision_wifi`：通过 USB 串口配网。
- `flash_firmware`：烧录当前固件目标。

后五项会改变硬件或持久配置。Hermes Skill 要求在执行前复述具体参数并取得明确确认；Wi-Fi 密码不得出现在最终回复中。

## Hermes Skill

Skill 源码位于：

`apps/demo_host/hermes_skills/espagent-light-strip/`

源码运行时，真实 Hermes 网关启动前，`WslHermesAdapter` 会同步该目录到：

`~/.hermes/skills/smart-home/espagent-light-strip/`

本机当前也可手动验证：

```bash
SKILL="$HOME/.hermes/skills/smart-home/espagent-light-strip"
bash "$SKILL/scripts/espagent-light" state
bash "$SKILL/scripts/espagent-light" action set_state \
  '{"power":true,"brightness":40,"color":"#FF6600"}'
```

助手脚本在 WSL 内调用 Windows `curl.exe`，因此请求进入 Windows 的 `127.0.0.1:8787`，不依赖 WSL NAT、TUN 或局域网网关地址。

“仅应用本体”的 Windows 安装包不包含 Hermes 服务和 Skill 部署目录。安装版仍提供灯条 HTTP API，但需要在外部 Hermes 环境中单独安装或同步该 Skill；不能把“应用已安装”视为“Hermes Skill 已部署”。

## 验证

1. 启动桌面应用并确认灯条模块已启用。
2. 请求 Hermes“读取灯条状态”，确认它先调用 `state`。
3. 请求“打开灯条，亮度 30%，颜色设为暖橙色”，确认桌面 UI 与实际灯条同步。
4. 请求一个状态中支持的特效，确认 `active_effect_preset` 更新。
5. 请求修改灯珠数或烧录固件，确认 Hermes 先要求明确确认而不直接执行。
6. 停止 WLED 或使用错误端点，确认 Hermes 报告 `online=false` 或 API 错误，不宣称操作成功。
