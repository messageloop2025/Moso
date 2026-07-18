# 手工验收清单：聊天模式 + Skills Command/Hook

## 聊天模式

1. 全局 AI / 主机 AI / 本机 AI：输入区出现模式选择（普通/问答/严格），切换后刷新会话仍保持。
2. **问答**：让 AI「在某主机执行 `uptime`」→ 不真正 send；出现可复制「待执行命令」卡。
3. **严格**：同上写操作 → 弹窗三按钮；拒绝则工具失败；允许则执行；一直允许后再发相同命令跳过弹窗。
4. 切出严格再切回：一直允许缓存仍有效。
5. **普通**：现有 auto_approve / ask_user_choice 行为无回归。

## Skills

1. Skills 页：斜杠命令、Hook 开关、matcher 可编辑；列表显示 `/name`、Hook/仅斜杠徽标。
2. 聊天输入 `/某技能` → system 注入该 Skill 全文（显式唤起）。
3. 启用 Hook + matcher=`ssh_execute` + 无 hooks.json → 调用 ssh_execute 时弹确认（ask）。
4. `hooks.json` 中 `decision: deny` → 工具直接失败且有审计/错误信息。

## 单测

```text
.venv/Scripts/python.exe -c "import tests.test_chat_mode_gate as t; ..."
```

或安装 pytest 后：`pytest tests/test_chat_mode_gate.py -q`
