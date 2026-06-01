/**
 * OpenClaw 等下游通常只能渲染有限 Markdown，与 Moso 网页内高级图表能力不一致。
 * 本段作为 edgeops_ops_chat（Moso 集成运维）消息的隐式后缀，让下游回复形态适配 ClawOps。
 */
export const OPENCLAW_DOWNSTREAM_FORMAT_BLOCK = `
---
[下游：OpenClaw · ClawOps / Moso 运维回复]
请按**受限富文本**输出最终结论（面向无法在 Moso 网页内渲染高级可视化的客户端）：
- **允许**：普通 Markdown——分级标题、有序/无序列表、**粗体**、\`行内代码\`、围栏代码块、简单 GFM 表格（列数尽量少）。
- **禁止**：Mermaid、ECharts、需要 JS/HTML 的图表或仪表板、内联 SVG/HTML 组件、假设存在「可点击/可折叠网页控件」的交互块。
- 若需说明拓扑或流程：用**短文字分点**或**极简 ASCII**（缩进列表），不要依赖图表渲染。
- 数据量较大时：摘要 + 表格或列表，避免整屏无法滚动阅读的巨型代码块。
`.trim();
