# 毛竹 前端资源说明

**所有 Web 资源均使用本地文件，不依赖任何 CDN 或外链。**

- **HTML**：`index.html` 入口，通过 `/static/` 引用本地资源。
- **样式**：`css/style.css`、`css/xterm.min.css` 已放在本目录，由服务端挂载到 `/static/css/`。
- **脚本**：`js/xterm.min.js`、`js/addon-fit.min.js`（@xterm/addon-fit 本地副本，用于终端填满容器）、`js/utils.js`、`js/api.js`、`js/router.js`、`js/app.js` 已放在本目录，由服务端挂载到 `/static/js/`。

新增或更新前端依赖时，请将对应文件下载到 `css/` 或 `js/`，并在 `index.html` 中以 `/static/...` 引用，勿使用外部 URL。

## 文案（中 / 英）

用户可见字符串在 `web/locales/zh-CN/` 与 `web/locales/en/` 下按模块分 JSON。**同一功能的中英文须使用完全相同的键路径**（嵌套对象递归对齐；例如 `hostTree.addGroup`、`modals.addGroupTitle`）。新增菜单或弹窗时请**两个目录下同名文件同时**增加或修改键，避免仅一侧有键导致切换英文界面时出现 `t('…')` 回退或空白。

### 模块文件（两侧各 16 个，文件名一一对应）

`ai.json`、`api.json`、`auth.json`、`batch.json`、`common.json`、`feedback.json`、`files.json`、`host.json`、`intro.json`、`layout.json`、`meta.json`、`misc.json`、`nav.json`、`pages.json`、`settings.json`、`toasts.json`

### 校验键是否对齐

在项目根目录执行：

```bash
python scripts/check_locale_parity.py
```

脚本会递归比较上述成对 JSON 的**全部键路径**；退出码 0 表示一致。部分文件带 UTF-8 BOM，脚本已处理。合并或发布前建议在分支上执行一遍，避免漏补键。
