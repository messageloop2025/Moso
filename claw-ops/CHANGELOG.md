# Changelog · @edgeops/claw-ops

本插件遵循 [语义化版本](https://semver.org/lang/zh-CN/)。版本号与 `package.json` / `openclaw.plugin.json` 保持一致。

> 运行期工具集以服务端 `GET /api/integration/claw-ops/manifest` 的 `extended_tools` 为权威源；
> `openclaw.plugin.json` 的 `contracts.tools` 仅为离线静态校验用的 baseline 快照，可能滞后于服务端扩展，无需逐项同步。

## [1.1.0]

### 新增
- **manifest 动态扩展**：Gateway 启动或 `edgeops_gateway_ping` 时拉取 `extended_tools` 并对未注册项 `registerTool`（增量），执行统一走 `POST /api/integration/claw-ops/invoke`。Moso 后台在 `claw_ops_registry.py` 新增扩展工具后，重启 Gateway 即可出现在模型工具列表，无需改插件并发版。
- **统一调用入口** `edgeops_invoke`：扩展工具（P1/P2）经服务端校验后执行；核心工具仍由插件直连对应 REST。
- **离线兜底** `manifest-fallback.ts`：manifest 拉取失败或离线时回退到内置的 20 个扩展工具快照。
- **服务端管理系统提示词**：`system_prompt.prepend_markdown` 由服务端下发，后台可更新，免插件发版。
- `capabilities_version` 协商：客户端缓存版本未变时返回 `unchanged: true`（仍含完整 manifest）。

### 工具规模
- 核心 **22** + 扩展 **20** + `edgeops_invoke` = baseline **43**。

## [1.0.0]

### 新增
- 首个 OpenClaw 插件版本：内置核心工具（探活 / 主机资产 / 检索 / 探活统计 / 最佳实践 / 集成运维对话 `edgeops_ops_chat`）。
- **SSH 交互通道**：`edgeops_ssh_channel_*`（创建 / 列表 / 详情 / 发送 / 按行读 / 按字符读 / 有新输出 / 关闭 / 导出 spill / 批量关）+ `edgeops_read_chat_data` 读 spill。
- `before_tool_call` 拦截本机 exec 对 Moso HTTP 的直连，强制走 `edgeops_*` 工具。
- `appendOpenClawUiHints` / `blockLocalMosoExec` 配置项。
