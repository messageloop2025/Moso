# Windows 主机接入步骤手册

毛竹 通过 **SSH** 管理主机。Windows 默认通常没有启好 SSH 服务，所以要先在 Windows 上安装并配置 **OpenSSH Server**，再把它接入 毛竹。

你可以把这份文档理解为：如何让一台 Windows 机器变成可被 毛竹 管理的 SSH 主机。

---

## 1. 接入前先知道什么

要让 Windows 主机被 毛竹 正常管理，至少需要满足：

- Windows 上已安装 OpenSSH Server
- SSH 服务已启动
- 防火墙已放行端口
- 你有可登录的 Windows 账户
- 毛竹 能从网络上访问这台机器

### 接入成功后能做什么

- 执行命令
- 打开控制台
- 上传文件
- 让 AI 协助排障或运维

---

## 2. 推荐接入顺序

建议按下面顺序完成：

1. 在 Windows 上安装 OpenSSH Server
2. 启动 `sshd` 服务并设为自动启动
3. 放行 TCP 22 端口
4. 在本机上先测试 SSH 是否可用
5. 在 毛竹 中创建凭证和主机
6. 执行一条简单命令验证

---

## 3. 方式一：通过图形界面安装 OpenSSH（推荐）

适合大多数 Windows 10 / 11 / Windows Server 用户。

### 步骤

1. 打开 **设置**
2. 进入 **应用**
3. 打开 **可选功能**
4. 点击 **添加功能**
5. 搜索并安装 **OpenSSH Server**
6. 安装完成后，打开 `services.msc`
7. 找到 **OpenSSH SSH Server**
8. 将启动类型设为 **自动**
9. 启动该服务

### 然后要做的事

1. 打开 Windows 防火墙高级设置
2. 新建入站规则
3. 放行 TCP 22

---

## 4. 方式二：通过 PowerShell 安装 OpenSSH

适合喜欢命令行或需要批量处理的场景。

### 以管理员身份打开 PowerShell

先查看系统中的 OpenSSH 可选功能：

```powershell
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH*'
```

再安装 OpenSSH Server：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

安装后执行：

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

### 推荐检查

安装后确认：

- 服务是否已启动
- 启动类型是否为自动
- 防火墙规则是否生效

---

## 5. 方式三：旧版 Windows 或可选功能不可用时

如果系统里没有 OpenSSH 可选功能，或安装失败，可以使用微软官方 OpenSSH 安装包。

### 推荐步骤

1. 下载微软官方 OpenSSH 安装包
2. 解压
3. 以管理员权限运行 `install-sshd.ps1`
4. 检查服务是否已创建并启动
5. 放行 22 端口

参考文档：

[OpenSSH 安装](https://docs.microsoft.com/zh-cn/windows-server/administration/openssh/openssh_install_firstuse)

---

## 6. 安装后如何验证 SSH 是否正常

不要一装完就直接进 毛竹，建议先在 Windows 本机或同网机器上测试。

### 推荐检查项

- `sshd` 服务是否在运行
- 22 端口是否监听
- 防火墙是否放行
- 账户密码是否正确

### 可做的验证

1. 先检查服务
2. 再检查端口
3. 再尝试 SSH 登录

如果本地都无法 SSH 登录，毛竹 也通常无法连通。

---

## 7. 在 毛竹 中添加 Windows 主机

Windows SSH 端准备好之后，就可以回到 毛竹。

### 推荐步骤

1. 先在 毛竹 创建一条凭证
2. 凭证中填写 Windows 本地账户或域账户
3. 进入主机管理
4. 新增主机
5. 主机填写 Windows 的 IP 或主机名
6. 端口填 `22`
7. 绑定刚才的凭证
8. 保存
9. 检测主机类型并执行简单命令验证

相关文档：

- [credentials.md](credentials.md)
- [hosts.md](hosts.md)

---

## 8. 登录账户怎么选

### 一般可用的账户

- Windows 本地账户
- 域账户

### 注意事项

- SSH 登录后执行的权限，取决于该账户本身的权限
- 如果要做管理员级操作，账户本身要具备相应权限
- 不建议直接用权限不明的账号做自动化操作

### 推荐做法

- 为运维准备专用账户
- 测试环境和生产环境分开
- 高权限和低权限账户分开

---

## 9. 连接成功后建议做什么

接入成功后，建议立刻做这些动作：

1. 检测主机类型
2. 执行简单命令确认输出正常
3. 打开控制台看是否能持续交互
4. 如需长期管理，补充主机知识

### 推荐测试命令

```powershell
whoami
hostname
pwd
```

---

## 10. 常见问题

### 10.1 服务装了，但连不上

优先检查：

- `sshd` 是否已启动
- 防火墙是否放行 22
- 网络是否可达
- 端口是否被改过

### 10.2 用户名密码正确，但还是认证失败

优先检查：

- 用的是本地账户还是域账户
- 账户是否被禁用
- 密码是否已变更
- 是否允许该账户通过 SSH 登录

### 10.3 能连上，但命令不正常

优先检查：

- 当前 shell 环境
- 账户权限
- 是否使用了不适合 Windows 的 Linux 命令

### 10.4 毛竹 能连接，但 AI 操作效果不好

建议：

1. 先检测主机类型
2. 明确告诉 AI 当前是 Windows 主机
3. 尽量用 Windows 风格命令或明确要求 PowerShell

---

## 11. 推荐的使用习惯

- 接入前先在 Windows 上验证 SSH
- 使用清晰命名的凭证和主机名
- 生产与测试环境分开
- 高权限账户与普通账户分开
- 连接成功后先做简单验证，再交给 AI 执行复杂任务
