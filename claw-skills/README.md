# claw-skills · Moso 外部智能体技能包



供 [Hermes Agent](https://hermes-agent.nousresearch.com/) 等**无 OpenClaw 插件**的智能体使用 Moso。



## 安装到 Hermes



将整个 `claw-skills/devops/` 目录复制到 Hermes 技能目录，例如：



```bash

cp -r claw-skills/devops/* ~/.hermes/skills/devops/

```



## 配置 Token 鉴权（与 claw-ops 相同）



与 OpenClaw `claw-ops` 的 `config.accessToken` / `config.baseUrl` 等价，**任选一种**方式配置（勿在聊天或 curl 参数里写 Token）。



### 方式 A · Hermes 技能环境变量（推荐）



各 SKILL 的 `required_environment_variables` 会在 Hermes 中提示填写：



| 变量 | 说明 |

|------|------|

| `EDGEOPS_ACCESS_TOKEN` | **必填**。JWT 或 `eop_…` API Token |

| `EDGEOPS_BASE_URL` | 可选，默认 **`https://ops.pinglan.cc`** |



### 方式 B · 配置文件（与 claw-ops 示例 JSON 同形）



```bash

cp claw-skills/edgeops.config.example.json ~/.config/edgeops/config.json

# 编辑 accessToken、baseUrl

```



或使用 `.env` 格式：



```bash

cp claw-skills/edgeops.env.example ~/.config/edgeops/.env

```



配置文件查找顺序（先命中先用）：



1. 环境变量 `EDGEOPS_CONFIG` 指向的文件

2. `~/.config/edgeops/config.json` 或 `edgeops.config.json`

3. `~/.hermes/edgeops.json`（或 `HERMES_HOME` 下同名）

4. `claw-skills/edgeops.config.json` / `edgeops.env`（复制 example 后填写）

5. 当前目录 `edgeops.config.json` / `edgeops.env`



加载到 shell（REST / curl 技能用）：



```bash

source claw-skills/scripts/load-edgeops-env.sh

# 或一次性：eval "$(claw-skills/scripts/load-edgeops-env.sh --export)"

```



```powershell

. .\claw-skills\scripts\load-edgeops-env.ps1

```



JSON 字段与 claw-ops 一致：`accessToken`、`baseUrl`（也接受 `access_token`、`base_url`）。



### 方式 C · MCP 客户端 HTTP 头



连接 `services.edgeops_mcp` 时在 MCP 配置写：



```http

Authorization: Bearer eop_…

X-EdgeOps-Access-Token: eop_…   # 可选，与 Authorization 二选一

```



stdio MCP 用 `env.EDGEOPS_ACCESS_TOKEN`；本地开发可加 `EDGEOPS_API_BASE_URL=http://127.0.0.1:8010`。



**说明**：`edgeops.config.json` / `edgeops.env` 已加入 `.gitignore`，勿提交真实 Token。



## 技能索引



| 技能 | 适用场景 |

|------|----------|

| [edgeops](./devops/edgeops/SKILL.md) | 总览、路由与鉴权 |

| [edgeops-ops-chat](./devops/edgeops-ops-chat/SKILL.md) | **最简单**：一条 REST 完成运维 |

| [edgeops-hosts](./devops/edgeops-hosts/SKILL.md) | 主机检索 REST |

| [edgeops-ssh-channel](./devops/edgeops-ssh-channel/SKILL.md) | 交互式 SSH REST |

| [edgeops-mcp](./devops/edgeops-mcp/SKILL.md) | Moso **内置 MCP**（**47** 工具；编排 ops、ssh_execute） |



## 三种集成方式



1. **MCP（47 工具，推荐 Cursor）** — Moso 内置，默认 `http://127.0.0.1:8010/mcp/`；含编排 ops、直连 ssh_execute（见 [edgeops-mcp](./devops/edgeops-mcp/SKILL.md)）

2. **ops-chat（一条 API）** — Moso 服务端代跑 ai_skills

3. **REST 细粒度** — hosts / ssh-channel



OpenClaw 用户请用 [claw-ops](../claw-ops/README.md)（独立 GitHub 仓库，Node 插件）。

完整技术说明：[docs/外部集成与ClawOps.md](../docs/外部集成与ClawOps.md) · 用户帮助：[web/aihelp/external-integration.md](../web/aihelp/external-integration.md)


