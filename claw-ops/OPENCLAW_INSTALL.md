# ClawOps（claw-ops）— OpenClaw 安装与配置

> 技术总览（Token、REST、MCP、claw-skills）：Moso 主仓库 [docs/外部集成与ClawOps.md](../docs/外部集成与ClawOps.md)  
> 用户向帮助：[web/aihelp/external-integration.md](../web/aihelp/external-integration.md)

本文档适用于 **Windows、Linux、macOS**。ClawOps 在 **OpenClaw Gateway** 内执行：**直连 Moso REST API**，**不依赖**打开 Moso 网页、也**不依赖**网页里的 AI 聊天 / Web 终端 UI；运维在 **Moso 服务端** 完成。

---

## 1. 前置条件

| 项 | 说明 |
|----|------|
| Node.js | **≥ 22**（与 `package.json` 的 `engines` 一致） |
| OpenClaw CLI | 已安装并在终端可用，例如 `openclaw --version` |
| Moso | 可访问的根 URL；用户 **JWT** 或 **个人 API Token**（`eop_`…） |

**OpenClaw 配置目录（全平台）：**

| 系统 | 主配置路径 |
|------|------------|
| Linux / macOS | `$HOME/.openclaw/openclaw.json` |
| Windows（cmd） | `%USERPROFILE%\.openclaw\openclaw.json` |
| Windows（PowerShell） | `$env:USERPROFILE\.openclaw\openclaw.json` |

可选：`exec-approvals` 等与网关/exec 策略相关文件也在 **`.openclaw`** 下（名称以你本机 OpenClaw 版本为准，常见如 `exec-approvals.json`）。

---

## 2. 从源码打包（构建）

在克隆后的 **`claw-ops` 目录**执行（三系统命令相同）：

```bash
cd claw-ops
npm ci
npm run build
npm run check
```

- `npm run build`：生成 `dist/`（TypeScript → JavaScript）。
- 开发/调试通常也可让 OpenClaw **直接加载源码目录**（见下文 `--link`）；正式发布前建议至少执行一次 `build` 以保持与 `package.json` 约定一致。

**生成可分发的 `.tgz`（推荐用脚本，等价 `npm run pack` = `build` + `npm pack`）：**

| 系统 | 命令 |
|------|------|
| Windows | 在资源管理器中双击 `pack.bat`，或在 **`claw-ops`** 目录执行：`pack.bat` |
| Linux / macOS | `chmod +x pack.sh`（首次），在 **`claw-ops`** 目录执行：`./pack.sh` |

或直接：

```bash
npm run pack
```

会在 **`claw-ops` 当前目录**生成形如 `edgeops-claw-ops-x.y.z.tgz` 的文件（**`x.y.z`** 为占位，**与 `package.json` 的 `version` 一致**）。复制到目标机器后的安装命令见 **§3.1**。

---

## 3. 安装插件（命令）

近期 **ClawHub** 访问量大时容易出现 **429 限流**；访问 **`clawhub.ai` 较慢或不稳定**时，可优先使用 ClawHub **中国官方镜像** [https://mirror-cn.clawhub.com](https://mirror-cn.clawhub.com)（配置见 **§3.6**）。**离线 `.tgz` / 本机目录**等**不依赖 ClawHub**；若仅 **npm 官方 registry 网络差**，可再配合 **§3.7 npm 镜像**。与 ClawHub 安装（§3.6）对照使用。

### 3.0 安装方式一览

| 方式 | 命令思路 | 适用 |
|------|----------|------|
| **离线包 `.tgz`** | `npm run pack` → `openclaw plugins install /路径/edgeops-claw-ops-*.tgz` | **最推荐**：内网、CI、ClawHub 限流、或尚未发 npm/ClawHub。 |
| **npm registry** | `openclaw plugins install @edgeops/claw-ops@<版本或 tag>` | 包已在 **registry.npmjs.org**（或你们配置的 npm 镜像）**成功发布**后。 |
| **npm + 镜像** | 设置 `npm_config_registry` 等后执行同上 install | 连官方 registry **超时/失败**时；见 **§3.7**。 |
| 本机源码目录 | `openclaw plugins install /path/to/claw-ops` | 本机有克隆仓库；见 §3.3。 |
| `--link` | `openclaw plugins install --link /path/to/claw-ops` | 开发调试；见 §3.4。 |
| **ClawHub** | `openclaw plugins install clawhub:…` | 无 429、且包已在 ClawHub 上架；国内可配镜像见 §3.6。 |

安装后均需 **重启 Gateway**；并配置 **`plugins.entries.claw-ops`** 等（§4）。

### 3.1 离线包安装（`.tgz`，推荐）

**产物**：在 `claw-ops` 目录执行 **`npm run pack`**（等同于 `build` + `npm pack`，参见 §2），得到 **`edgeops-claw-ops-x.y.z.tgz`**（**`x.y.z`** 与 `package.json` 的 **`version`** 相同）。

**安装**（路径改成你的绝对路径；三系统相同）：

```bash
openclaw plugins install ./edgeops-claw-ops-x.y.z.tgz
```

```powershell
openclaw plugins install "D:\dist\edgeops-claw-ops-x.y.z.tgz"
```

- **不靠 ClawHub、不靠公共 npm**，只要把文件拷到目标机即可。
- 若已存在旧版目录，OpenClaw 可能提示先删除 `~/.openclaw/extensions/claw-ops`（Windows：`%USERPROFILE%\.openclaw\extensions\claw-ops`）再装。

### 3.2 从 npm registry 安装

**前提**：`@edgeops/claw-ops` 已在所使用的 **npm registry** 上存在（执行维护者 **`npm publish`** 并完成作用域 **`@edgeops`** 授权）。若从未发布，`Package not found on npm` 为预期现象，请改用 **§3.1** 或 §3.3。

**命令**（不要用 `npm:` 前缀，不要写版本范围如 `^x.y.z`，只接受 **精确版本号** 或 **dist-tag**，例如 `latest` / `beta`；下例 **`x.y.z`** 须换成与发布版本一致的 semver）：

```bash
openclaw plugins install @edgeops/claw-ops@x.y.z
openclaw plugins install @edgeops/claw-ops@latest
```

**与 ClawHub 的关系**：对 **作用域包** `@edgeops/...`，OpenClaw 会**先尝试 ClawHub**。遇 **429 限流**时往往**不会**自动落到 npm，此时请改 **§3.1 离线包** 或 **§3.3 本地路径**，或待限流恢复后再试。

### 3.3 本机路径安装（复制到 OpenClaw 托管目录）

以下假定路径指向 **包含 `openclaw.plugin.json` 的插件根目录**。

**Linux / macOS**

```bash
openclaw plugins install /absolute/path/to/claw-ops
```

**Windows（PowerShell，示例）**

```powershell
openclaw plugins install "D:\path\to\claw-ops"
```

### 3.4 开发：`--link`（不复制，改代码即生效）

```bash
openclaw plugins install --link /absolute/path/to/claw-ops
```

Windows 同样传入绝对路径（注意引号）。

### 3.5 验证

```bash
openclaw plugins list
openclaw plugins inspect claw-ops
openclaw plugins doctor
```

### 3.6 从 ClawHub 安装（可选）

插件发布到 [ClawHub](https://clawhub.ai/) 后，可用：

#### 国内镜像（中国大陆访问优化）

ClawHub 提供**中国官方镜像站**：[https://mirror-cn.clawhub.com](https://mirror-cn.clawhub.com)（[ClawHub 中国官方镜像站](https://mirror-cn.clawhub.com)）。在向 `clawhub.ai` 拉取插件慢、超时或易遇限流时，可在执行安装命令**前**将 Registry 指向该镜像。

与 [ClawHub CLI](https://github.com/openclaw/clawhub/blob/main/docs/cli.md) 一致，常用环境变量为：

| 变量 | 含义 |
|------|------|
| `CLAWHUB_SITE` | 站点基址（浏览器登录等；默认 `https://clawhub.ai`） |
| `CLAWHUB_REGISTRY` | Registry **API** 基址（未设置时通常与默认站点一致或由发现逻辑决定） |

**bash / macOS / Linux**（当前终端一次性生效）：

```bash
export CLAWHUB_SITE=https://mirror-cn.clawhub.com
export CLAWHUB_REGISTRY=https://mirror-cn.clawhub.com
openclaw plugins install clawhub:<slug>
```

**Windows PowerShell**：

```powershell
$env:CLAWHUB_SITE = "https://mirror-cn.clawhub.com"
$env:CLAWHUB_REGISTRY = "https://mirror-cn.clawhub.com"
openclaw plugins install clawhub:<slug>
```

也可在 OpenClaw 的 `~/.openclaw/openclaw.json` 中 **`env`** 块写入上述键值（仅对缺失变量生效；优先级见 [OpenClaw 环境变量说明](https://docs.openclaw.ac.cn/help/environment)）。镜像与 **§3.7 的 npm registry 镜像**是两条线：前者解决 **ClawHub** 访问，后者解决 **npm 包**拉取。

#### 安装命令

```bash
# 将 <slug> 换成 ClawHub 上该包的实际标识（以 clawhub.ai 展示为准）
openclaw plugins install clawhub:<slug>
```

示例（占位，真实 slug 以 ClawHub 页面为准）：

```bash
openclaw plugins install clawhub:edgeops-claw-ops
```

也可使用 **`clawhub:@scope/name`** 等形式，以 ClawHub 对包名的解析为准。

若出现 **`429 Rate limit exceeded`**（URL 中 `%40`=`@`、`%2F`=`/` 为正常编码）：属 **ClawHub API 限流**。请 **间隔一段时间再试**、减少连击、或换网络；**优先改 §3.1 离线 `.tgz` 或 §3.3 路径安装**，无需依赖 ClawHub。

更新已安装的 ClawHub 跟踪插件：

```bash
openclaw plugins update --all
# 或仅更新某一插件（名称以 openclaw plugins list 为准）
openclaw plugins update claw-ops
```

### 3.7 安装失败或网络慢：通过 npm 镜像（registry）安装

当 **`openclaw plugins install @edgeops/claw-ops@…`** 在拉取 **npm registry** 时报错（超时、连接重置、`ETIMEDOUT`、`ENOTFOUND` 等），可与其它 Node 工具一样，**先为 npm 指定镜像再安装**，思路类似：

```text
npx -y @tencent-weixin/openclaw-weixin-cli@latest install --registry=https://registry.npmmirror.com
```

上式仅为 **「带 `--registry` / 镜像站」的类比**；本插件仍使用 **`openclaw plugins install`**。OpenClaw 在安装 **npm 来源**的包时会调用本机 **npm**，通常会尊重环境变量 **`npm_config_registry`** 以及用户 **`~/.npmrc`**（含 `registry=`）。

**当前终端一次性指定镜像（推荐先试）**

bash / macOS / Linux：

```bash
export npm_config_registry=https://registry.npmmirror.com
openclaw plugins install @edgeops/claw-ops@x.y.z
```

单条命令（不设变量）：

```bash
npm_config_registry=https://registry.npmmirror.com openclaw plugins install @edgeops/claw-ops@x.y.z
```

Windows PowerShell：

```powershell
$env:npm_config_registry = "https://registry.npmmirror.com"
openclaw plugins install @edgeops/claw-ops@x.y.z
```

**较长期**：为本用户设置默认 registry（影响其它 `npm install`，按需使用）：

```bash
npm config set registry https://registry.npmmirror.com
```

恢复使用官方默认（视本机 npm 版本，原配置可能是 `https://registry.npmjs.org/`）：

```bash
npm config delete registry
```

**须注意**

- **ClawHub 429、DNS 到 clawhub.ai 失败** 等，改 **npm** 镜像**通常无效**；可先试 **§3.6 国内 ClawHub 镜像**（`CLAWHUB_SITE` / `CLAWHUB_REGISTRY`），或改用 **§3.1 离线 `.tgz`** / **§3.3 本地路径**，或稍后重试。  
- 镜像站与 **registry.npmjs.org** 同步可能有延迟；若镜像上暂无 `@edgeops/claw-ops`，可换其它可信镜像或临时切回官方。  
- 示例域名 **https://registry.npmmirror.com** 为常用国内镜像（原 cnpm）；请只使用你信任的 registry。

安装后仍需配置 **`plugins.entries.claw-ops`**（尤其是 **`config.accessToken`**），并视情况配置 **`tools.alsoAllow`**（见 §5.1）。最小闭环示例见 **§3.8**；完整逐项命令见 **§4**。

### 3.8 安装后：命令行绑定 `accessToken`（环境变量引用，推荐）

将 **Moso JWT 或 `eop_` Token** 放进环境变量，再用 OpenClaw **引用**写入配置，避免令牌以明文落盘进 `openclaw.json`。

**bash / macOS / Linux**（先导出变量，再执行 `config set`）：

```bash
export EDGEOPS_TOKEN='eop_你的令牌'

openclaw config set plugins.entries.claw-ops.config.accessToken \
  --ref-provider default --ref-source env --ref-id EDGEOPS_TOKEN
```

**Windows PowerShell**（先赋给进程环境变量）：

```powershell
$env:EDGEOPS_TOKEN = "eop_你的令牌"

openclaw config set plugins.entries.claw-ops.config.accessToken --ref-provider default --ref-source env --ref-id EDGEOPS_TOKEN
```

- `--ref-id` 与上面的变量名须一致（示例为 **`EDGEOPS_TOKEN`**，可按团队规范改名）。  
- 另需 **`enabled`**、**`hooks.allowPromptInjection`**、**`config.baseUrl`** 等时，见 **§4.1 / §4.2**；**`plugins.allow` / `tools.alsoAllow`** 见 **§4.3**、**§5.1**。  
- 配置变更后**重启 Gateway**（**§5.3**）。

---

## 4. Moso URL、KEY 与插件开关（全命令行示例）

**配置是前置条件**：须在 `openclaw.json` 中完成 `plugins.entries.claw-ops`（**必填** `config.accessToken`，**可改** `config.baseUrl`）、以及在 `tools.profile: coding` 场景下的 `tools.alsoAllow`（含 `claw-ops`），否则 `edgeops_*` 无法正常工作。`plugins.allow` 在 OpenClaw 里为**可选**白名单：官方说明为「**若已设置（非空），则仅列表内插件可加载**」——此时**必须**包含 `"claw-ops"`；若未设置或为空，非捆绑插件仍**可能**被自动发现加载（网关会告警并建议显式列出可信 ID），故生产/多插件环境仍**强烈建议**配置 `plugins.allow` 并纳入 `claw-ops`。`appendOpenClawUiHints`、`blockLocalMosoExec` 仅写在 **`plugins.entries.claw-ops.config`**，不要写在 `hooks` 里。包内合并示例：**`openclaw.claw-ops.example.json`**。

**默认 Moso 根 URL**：插件未配置 `baseUrl` 时，会回退到内置默认 **`https://ops.pinglan.cc`**（与 `client.ts` 中常量一致）。**自建实例**请显式设为你的域名；下列示例以公网默认为例。

配置均通过 **`openclaw config set`** 写入 **`openclaw.json`**。可先查看配置文件路径：

```bash
openclaw config file
```

批量写入时可用 **`--dry-run`** 先校验（见 §4.4）。

### 4.1 逐项写入（bash / macOS / Linux）

**Token 环境变量引用**与 **§3.8** 中命令相同；本节补充 `enabled`、`baseUrl`、`hooks` 等其余项。

```bash
# 启用插件 + 注入路由提示（推荐）
openclaw config set plugins.entries.claw-ops.enabled true --strict-json
openclaw config set plugins.entries.claw-ops.hooks.allowPromptInjection true --strict-json

# Moso 根地址（默认公网；自建请改 URL）
openclaw config set plugins.entries.claw-ops.config.baseUrl "https://ops.pinglan.cc" --strict-json

# Token：方式 A — 直接从环境变量读取（推荐，避免明文进配置文件）
# 先在 shell 中 export EDGEOPS_TOKEN='eop_xxx' 再执行：
openclaw config set plugins.entries.claw-ops.config.accessToken \
  --ref-provider default --ref-source env --ref-id EDGEOPS_TOKEN

# Token：方式 B — 明文写入（仅用于本机快速试验；勿截图、勿提交 git）
openclaw config set plugins.entries.claw-ops.config.accessToken '"eop_你的令牌"' --strict-json
```

### 4.2 逐项写入（Windows PowerShell）

**Token 环境变量引用**见 **§3.8**；下为本节其余项。

```powershell
openclaw config set plugins.entries.claw-ops.enabled true --strict-json
openclaw config set plugins.entries.claw-ops.hooks.allowPromptInjection true --strict-json
openclaw config set plugins.entries.claw-ops.config.baseUrl https://ops.pinglan.cc

# Token：环境变量（推荐。先执行：$env:EDGEOPS_TOKEN = "eop_..."）
openclaw config set plugins.entries.claw-ops.config.accessToken --ref-provider default --ref-source env --ref-id EDGEOPS_TOKEN

# 明文 token 在 PowerShell 引号易踩坑，建议用 §4.4 批量文件，或 bash 运行 §4.1
```

若需一次性写入多项含字符串的配置，在 Windows 上优先 **§4.4 批量 JSON**。

### 4.3 `plugins.allow`（插件 ID 白名单，可选）

与 OpenClaw 运行时配置说明一致：`plugins.allow` 为**可选**；**仅当该数组存在且非空时**，才表示「只允许这些插件参与加载」——列表中若无 `claw-ops`，本插件将**不会**被加载。未配置或为空时，行为更宽松（非捆绑插件可能被自动发现，官方日志会提示设置显式白名单）。

仅当配置里**还没有**其它插件，或你愿意**覆盖**整个列表时：

```bash
openclaw config set plugins.allow '["claw-ops"]' --strict-json
```

若已有 `qqbot`、`google` 等，请先读出再合并，**不要把原列表覆盖掉**：

```bash
openclaw config get plugins.allow
```

将输出中的数组**加上** `"claw-ops"` 后一次性设置，例如：

```bash
openclaw config set plugins.allow '["claw-ops","qqbot","google"]' --strict-json
```

（顺序无所谓，以你本机实际插件名为准。）

### 4.4 批量写入（任选平台，推荐）

新建文件 `claw-ops-openclaw.batch.json`（内容按需改掉 token / URL）：

```json
[
  { "path": "plugins.entries.claw-ops.enabled", "value": true },
  { "path": "plugins.entries.claw-ops.hooks.allowPromptInjection", "value": true },
  { "path": "plugins.entries.claw-ops.config.baseUrl", "value": "https://ops.pinglan.cc" },
  { "path": "plugins.entries.claw-ops.config.accessToken", "value": "eop_REPLACE_ME" }
]
```

执行（**勿**把含真实 token 的文件提交到 git）：

```bash
openclaw config set --batch-file ./claw-ops-openclaw.batch.json --dry-run
openclaw config set --batch-file ./claw-ops-openclaw.batch.json
```

批量模式**不能**与 `--ref-provider` 混用；若要用环境变量引用 token，请对 `accessToken` 单独执行 **§4.1** 中的 `ref` 命令，或把 batch 里该项删掉后单独 `config set ... --ref-source env ...`。

### 4.5 与手工编辑 JSON 等价的结构（对照）

```json
"plugins": {
  "allow": ["claw-ops"],
  "entries": {
    "claw-ops": {
      "enabled": true,
      "hooks": {
        "allowPromptInjection": true
      },
      "config": {
        "baseUrl": "https://ops.pinglan.cc",
        "accessToken": "eop_xxxxxxxx"
      }
    }
  }
}
```

| 字段 | 含义 |
|------|------|
| `baseUrl` | Moso **根地址**，无末尾 `/`。未设置时插件默认 **`https://ops.pinglan.cc`**；自建必填你的 URL。 |
| `accessToken` | Moso **Bearer**：JWT 或 `eop_` API Token。**勿**提交到仓库；优先 **env + SecretRef**（§4.1 方式 A）。 |
| `hooks.allowPromptInjection` | 建议 `true`：注入须用 `edgeops_*`、勿本机 curl Moso 等说明。 |

若 `plugins.installs.claw-ops` 中记录了 `sourcePath`，只影响**扩展来源**；**URL / TOKEN 以 `plugins.entries.claw-ops.config` 为准**。

**校验：**

```bash
openclaw config validate
```

### 4.6 排查：`plugin not found: claw-ops (stale config entry ignored)`

这与 **`hooks.allowPromptInjection` 路径写错无关**。OpenClaw 官方 [Configuration reference](https://docs.openclaw.ai/gateway/configuration-reference) 写明：`plugins.entries.<插件id>.hooks.allowPromptInjection` 用于控制是否允许 `before_prompt_build` 等提示注入类钩子；本插件在 `openclaw.plugin.json` 中的 **`id` 为 `claw-ops`**，故配置键应为 `plugins.entries.claw-ops.hooks.allowPromptInjection`。[Plugin manifest](https://docs.openclaw.ai/plugins/manifest) 亦说明：`manifest.id` 即写入 `plugins.entries` 时使用的键名。

该警告表示：当前校验所见的**已安装/可发现插件列表**里**还没有** `claw-ops`（常见原因：尚未在本机安装、或已删除 `~/.openclaw/extensions/claw-ops` 但 `openclaw.json` 仍保留 `plugins.entries.claw-ops` / `plugins.allow`）。此时未知 id 会被标成「陈旧配置」并告警；`openclaw config set` 仍可能**已写入** `openclaw.json`，但在插件未正确安装前，相关条目**不会对运行时生效**。

**处理：** 在本机完成 **`openclaw plugins install`**（项目目录、`.tgz` 或发布 spec），执行 **`openclaw plugins list`** 确认出现 **`claw-ops`**，再 `config set` 或 `openclaw config validate`，警告应消失；修改后**重启 Gateway**。需要严格 JSON 布尔值时与 §4.1 一致：`… true --strict-json`。

---

## 5. 权限与策略（OpenClaw 侧，命令行 + 说明）

插件**不会**替你把网关的 exec 安全策略全部关掉；若模型仍尝试 `exec` + HTTP 调 Moso，可能触发审批。

### 5.1 工具白名单（`tools.profile: coding` 等）——命令行

若使用精简工具 **profile**（如 `coding`），须把插件放进 **`tools.alsoAllow`**（或等价配置），否则模型**看不到** `edgeops_*`。

仅当列表为空或你可覆盖整个数组时：

```bash
openclaw config set tools.alsoAllow '["claw-ops"]' --strict-json
```

若已有其它项，先 `openclaw config get tools.alsoAllow`，合并后再 `config set`。**勿**在同一作用域同时配置冲突的 `tools.allow` 与 `tools.alsoAllow`（OpenClaw 会校验）。

按 agent 细粒度控制时，路径可能是 `agents.list[].tools.alsoAllow`，需编辑 `openclaw.json` 或使用支持你本机 OpenClaw 版本的 dot path（可用 `openclaw config schema` 查阅）。

### 5.2 Exec 审批（可选）

- 网关若采用 **allowlist + on-miss**，模型误用 shell 访问 Moso 时会出现审批弹窗。
- **处理思路**：优先走 `edgeops_*`；在可信环境下再在 **`.openclaw`** 下调整 exec 审批相关配置（文件名以安装版本为准，如 `exec-approvals.json`）。
- 该文件多数情况下需**手工编辑**或通过 Control UI 配置；是否提供 `openclaw config set` 路径依版本而定，可将 `openclaw config schema` 的输出保存后搜索 `exec`。

### 5.3 重启 Gateway

修改 `openclaw.json` 或插件文件后：

```bash
openclaw gateway stop
# 稍等端口释放（必要时 2～5 秒）
openclaw gateway start
```

或你环境中等价的一键重启命令。

---

## 6. Moso 侧权限说明（简要）

- `eop_` Token 或 JWT 能访问哪些主机、哪些 API，由 **Moso 用户权限**决定。
- ClawOps 使用的集成接口为服务端 **`POST /api/integration/ops-chat/complete`** 等；需保证该令牌有权访问对应 Moso 实例。

---

## 7. 常见问题

| 现象 | 建议 |
|------|------|
| 模型只会 `curl` / `Invoke-RestMethod` | 确认 `allowPromptInjection`、`tools.alsoAllow` 含 `claw-ops`，并重启 Gateway。 |
| 工具报未配置 token | 填写 `plugins.entries.claw-ops.config.accessToken`。 |
| `SSL` / 自建证书 | Moso 使用 https 时，需系统/Node 信任该 CA（依部署而定）。 |
| 连接拒绝 | 检查 `baseUrl`、防火墙、Moso 是否监听 `8010`/443 等。 |

---

## 8. 文档与版本

- 插件元数据：`openclaw.plugin.json`
- OpenClaw CLI 插件说明：<https://docs.openclaw.ai/cli/plugins>

若本仓库 `package.json` 中的 `openclaw.compat` / `openclaw.build` 与网关版本不匹配，发布到 ClawHub 前请按 [Plugin Setup](https://docs.openclaw.ai/plugins/sdk-setup) 更新（`build.openclawVersion`、`build.pluginSdkVersion` 需与用于 `npm run build` 的 OpenClaw 版本一致）。
