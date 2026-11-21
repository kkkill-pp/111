# V1 自动化验收用例清单（HITL v2.0）

| ID | 类别 | 场景 | 操作 | 预期结果 |
|---|---|---|---|---|
| HITL-001 | 人机协同 | Storyboard 人工选择 | AI 生成 3 个方案，用户通过 `POST /storyboards/{id}/reviews` 拒绝 2 个、修改并批准 1 个 | 仅被批准版本可编译为生产 Timeline v1 |
| HITL-002 | 人机协同 | Agent 修改可解释 | 提交自然语言“删掉冗余部分” | 返回 `EditProposal`（操作清单、证据锚点、影响摘要、风险等级、Diff），不静默改写 Timeline |
| HITL-003 | 人机协同 | 高风险操作门禁 | Agent 提议移除核心论点 | 必须经 `POST /proposals/{id}/reviews` 人工批准并签发一次性 `ApprovalToken`，未审批禁止应用 |
| HITL-004 | 人机协同 | 无响应≠同意 | 创建 `human_reviews(status=pending)` 后不操作至超时 | 状态保持 `pending` 或超时转 `expired`，绝不自动应用或发布 |
| HITL-005 | 人机协同 | 版本冲突与 Token 失效 | 审阅期间 Timeline 更新至 v+1 | 旧 Proposal 与 `ApprovalToken` 失效，返回 `409 VERSION_CONFLICT` |
| HITL-006 | 人机协同 | 不可变撤销 | 调用 `POST /timelines/{id}/undo` | 追加新的反向版本（v+2），历史 `timeline_versions` 完整保留 |
| HITL-007 | 人机协同 | 逐项选择 | Proposal 含 5 项操作，用户在 `/reviews` 中勾选 3 项 | 仅应用被选中的 3 项操作，其余标为 `rejected` |
| HITL-008 | 人机协同 | 发布审批门禁 | QA 通过但无当前有效 Artifact 审批 | `POST /artifacts/{id}/deliveries` 返回 `409 APPROVAL_REQUIRED`，Delivery 保持 `blocked` |
| HITL-009 | 人机协同 | 审批绑定 Checksum | 审批后重新渲染产生新 `checksum_sha256` | 原审批自动失效（`superseded`），新产物须重新审阅 |
| HITL-010 | 人机协同 | 预算门禁 | Final Render 预估成本超过项目/工作空间硬预算 | 未确认前返回 `409 BUDGET_LIMIT_EXCEEDED`，不自动扣费 |
| SEC-001 | 租户隔离 | 跨租户 API 读取 | Tenant A 请求 Tenant B 的 Asset/Timeline | 返回 `404 NOT_FOUND` 或 `403 FORBIDDEN`，不泄露资源存在性 |
| SEC-002 | 租户隔离 | DB 复合外键与强制 RLS | 在 DB 层尝试将 Tenant A 的 Asset 插入 Tenant B 的 Project，或不设 `app.tenant_id` 查询 | 复合外键直接报错拒绝；未设租户上下文返回 0 行（fail-closed） |
| TASK-001 | 任务系统 | Worker 崩溃恢复 | Worker 在渲染中途被 kill | Lease 过期后自动按退避重试，半成品不升为 `ready` |
| TASK-002 | 任务系统 | 幂等去重 | 相同 `Idempotency-Key` 并发重放 3 次 | 仅产生 1 个 Task、1 个业务效果与 1 条 `usage_ledger` |
| TASK-003 | 任务系统 | Fencing Token 防脑裂 | 旧 Worker（`lease_token=1`）卡顿恢复后尝试提交结果，此时新 Worker 已领取（`lease_token=2`） | `complete_task_with_fencing` 返回 `false`，拒绝写入、拒绝发布 Artifact、拒绝重复计费 |
| TASK-004 | 任务系统 | 合作式取消 | 对运行中任务调用 `POST /tasks/{id}/cancel` | Worker 下次心跳收到 `cancel_requested`，终止 FFmpeg、清理临时对象并标为 `cancelled` |
| AGENT-001 | Agent 治理 | 步数/预算熔断 | Agent 陷入循环工具调用超过 `max_steps` 或 `max_cost_usd` | Orchestrator 强制中止并标为 `budget_exceeded`，写入审计日志 |
| AGENT-002 | Agent 治理 | 权限继承上限 | `viewer` 角色用户触发 Agent 尝试调用 `apply_edit_operations` | Tool Registry 直接拒绝（`decision=denied`），不发生提权 |
| TIME-001 | 时间精度 | VFR 与 29.97fps 裁剪 | 手机 VFR 与 29.97fps 素材执行剪切与拼接 | 有理数时间映射准确，音画同步偏差在容差内 |
| RENDER-001 | 渲染一致 | Preview vs Final | 同一 `TimelineVersion` 编译生成 Preview 与 Final | 剪辑点、字幕、构图与音频语义一致，无静默降级 |
| QA-001 | 质检门禁 | 技术 QA 失败 | 产物损坏、音轨缺失或时长超差 | `qa_reports.status=failed`，调用 `POST /artifacts/{id}/reviews` 批准时返回 `409 QA_BLOCKER_PRESENT` |
| QA-002 | 质检门禁 | 内容 QA 警告 | 字幕疑似改变原意 | 生成 `warning`，仅允许 Reviewer 以 `approve_with_warnings` 显式确认 |
| PER-001 | 人物检测 | 合规检测栈压测 | 在 Golden Media 多人/遮挡/快移/远景/低光样本上运行 `PersonDetectionProvider` | 构图追踪稳定，满足业务精度基线且零 `ultralytics` 依赖 |
| LIC-001 | 合规门禁 | CI 许可红线拦截 | PR 中引入 `cloud/`、`requirements-billing.txt` 或 `ultralytics` | CI 自动拦截并构建失败 |
