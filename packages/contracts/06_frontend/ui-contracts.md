# 前端页面与人机协同交互契约（HITL v2.0）

## 1. 页面路由与 RBAC 权限守卫

| 页面 | 路由 | 允许角色 | 核心人机协同职责 |
|---|---|---|---|
| Workspace Dashboard | `/workspaces/:wsId` | 全部角色 | 查看项目列表、待审阅队列角标、用量与预算水位 |
| Project List | `/workspaces/:wsId/projects` | 全部角色 | 新建/归档项目、过滤状态 |
| Project Detail | `/projects/:projectId` | 全部角色 | 贯穿全流程的状态总览（素材 → 分析 → 故事板 → 时间轴 → 渲染 → 审批） |
| Asset Upload | `/projects/:projectId/assets/upload` | owner, admin, editor | 预签名 Multipart 直传、断点续传、SHA-256 校验与 Probe 进度 |
| Analysis Review | `/projects/:projectId/assets/:assetId/analysis` | 全部角色 | 分栏审阅 Transcript 词级时间戳、Scene 镜头、SemanticSegment 与 HighlightCandidate 10 维证据分 |
| Creative Brief | `/projects/:projectId/brief` | owner, admin, editor（其余只读） | 编辑与版本化 Creative Brief，展示历史版本 Diff |
| Storyboard Compare | `/projects/:projectId/storyboards` | 全部角色（审阅仅 owner/admin/reviewer） | 并排对比 1–5 个 AI 故事板方案；调用 `POST /storyboards/{id}/reviews` 执行接受、修改后接受或拒绝 |
| Timeline Editor | `/projects/:projectId/timelines/:timelineId` | owner, admin, editor（其余只读） | 多轨非线性编辑、AI Assistant 提案 Diff、人工接管、Undo/Redo 与版本历史 |
| Render Queue | `/projects/:projectId/renders` | 全部角色 | 区分 Preview 与 Final Render 任务进度、查看 RenderPlan 摘要与预算确认 |
| QA Report | `/projects/:projectId/artifacts/:artifactId/qa` | 全部角色 | 展示技术 QA 与内容 QA 报告；Blocker 红灯禁止审批，Warning 黄灯需逐项确认 |
| Approval Center | `/workspaces/:wsId/reviews` | owner, admin, reviewer | 统一待审队列（Storyboard / EditProposal / Artifact），支持一键跳至上下文并提交 `/reviews` |
| Workspace Settings | `/workspaces/:wsId/settings` | owner, admin | 成员 RBAC 管理、HITL 模式（`copilot`/`supervised_autopilot`/`batch_operator`）、预算硬顶 |
| Usage & Audit | `/workspaces/:wsId/usage` | owner, admin | 用量台账、成本拆分与不可变审计日志查询 |

## 2. Timeline 编辑器核心交互契约

### 2.1 状态同步与 409 冲突处理
- **服务端权威原则**：前端严禁将本地 JSON 全量覆盖提交到后端，所有变更必须封装为 `EditOperationInput[]` 并携带 `base_version` 与 `Idempotency-Key`。
- **本地暂存层**：支持浏览器草稿自动保存，但在顶栏清晰标注 `本地未提交修改`。
- **409 版本冲突**：当收到 `409 VERSION_CONFLICT` 时，编辑器立即锁定提交按钮，拉取最新 `timeline_version` 并弹出双栏 Diff 对比视图，由用户选择“基于最新版本重新计算提案”或“放弃本地冲突修改”。

### 2.2 AI Assistant 提案、Diff、逐项选择与人工接管
1. 用户输入自然语言指令（如“删掉开头 5 秒并简化字幕”）或由 Agent 发起提案。
2. 面板展示结构化 `EditProposal` 卡片，必须呈现：**操作清单、证据锚点（可点击跳转到源视频时间码）、风险徽章（low/medium/high/critical）、Impact Summary（时长变化/受影响片段数/是否可逆）与轨道级可视 Diff**。
3. **逐项控制**：每条操作带独立勾选框与“手动微调”入口；用户可勾选 5 项中的 3 项提交 `POST /proposals/{id}/reviews`（`selected_items` / `rejected_items`）。
4. **人工即时接管**：用户随时可直接拖拽轨道 Clip、修改字幕或手动调整构图框（写入 `crop_override_json`），系统立即将尚未应用的旧 AI 提案标为 `superseded`。
5. **一键撤销/重做**：调用 `POST /timelines/{id}/undo` 或 `/redo`，在版本历史抽屉中展示完整版本号、操作人（区分用户与 Agent 归因）与时间戳。

## 3. 精细化状态反馈规范（禁止笼统“处理中”）

前端状态徽章必须精确映射到以下 10 种状态之一：
`本地未提交修改` | `正在校验操作` | `正在保存版本` | `版本冲突 (409)` | `等待人工审阅` | `Preview 生成中` | `Final Render 渲染中` | `QA 未通过（阻断）` | `QA 有警告（待确认）` | `已审批可交付` / `已交付`。
