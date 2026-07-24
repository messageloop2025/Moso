# 物理引擎（本地 vendor）

- `cannon.min.js` — Cannon.js 0.6.2 UMD，全局 `CANNON`（聊天 three-scene 物理、报告默认 snippet）
- `cannon-es.js` — cannon-es 0.20.0 ESM（`type=module` 报告可选）

包名在 manifest 中为 `cannon-es`（与现代生态称呼一致）；浏览器全局路径优先用 `cannon.min.js`。
