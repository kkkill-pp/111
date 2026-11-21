# 全实体统一状态机与人机门禁（HITL v2.0）

所有枚举值在 PostgreSQL、JSON Schema、OpenAPI 3.1 与应用服务中统一使用**小写 `snake_case`**，严禁混用动词/过去分词或大小写分歧。

## 1. Human Review 状态机（统一复数 `/reviews` 资源）

- **状态集合 (`status`)**：`pending -> approved | approved_with_warnings | rejected | changes_requested | expired | superseded`
- **人工提交动作 (`decision`)**：`approve | approve_with_warnings | reject | request_changes`
- **映射规则**：
  - 发起审阅时必须先创建 `status = pending`、`decision = NULL`、`reviewer_id = NULL` 的记录。
  - 无响应或超时到达 `expires_at` 时转为 `expired`，**绝不视为同意**。
  - 若被审对象（`subject_version` 或 `subject_checksum`）发生变化，所有关联的 `pending` 或历史 `approved` 记录立即转为 `superseded`。
  - 针对 `render_artifact`：当关联 `qa_reports.status = failed` 时，系统在服务层拒绝 `approve` / `approve_with_warnings`，返回 `409 QA_BLOCKER_PRESENT`。

## 2. Storyboard 状态机

- `generating -> proposed -> accepted | rejected | revision_requested`
- 异常/失效态：`superseded | stale`
- **门禁**：仅当通过 `POST /storyboards/{id}/reviews` 获得 `status = approved` 后，Storyboard 转为 `accepted`，并允许编译创建生产 Timeline v1。若关联的 `brief_version` 或 `analysis_run_ids` 更新，旧 Storyboard 标记为 `stale`。

## 3. Edit Proposal 状态机（批次级 AI 剪辑提案）

- `draft -> validating -> awaiting_review -> approved -> applied`
- 异常态：`rejected | expired | superseded | invalid`
- **门禁**：
  - `Proposal` 不等于 `TimelineVersion`。
  - 若 `timelines.current_version != edit_proposals.base_version`，所有针对旧版本的 `awaiting_review` / `approved` 提案自动转为 `superseded`，客户端提交时返回 `409 VERSION_CONFLICT`。
  - 人工可在 `POST /proposals/{id}/reviews` 中通过 `selected_items` / `rejected_items` 实现逐项接受或拒绝，仅被选中的操作进入最终原子应用。

## 4. Render Job 状态机

- `queued -> claimed -> running -> qa_pending -> qa_passed | qa_warning | qa_failed`
- 异常态：`retry_wait | failed | cancel_requested | cancelled`
- **门禁**：只引用不可变 `render_plans`（绑定不可变 `timeline_version`）；严禁将未通过完整校验或已被取消的临时文件升级为 `artifacts.status = ready`。

## 5. Delivery 状态机

- `blocked -> ready -> running -> delivered`
- 异常态：`failed | cancelled | revoked`
- **进入 `ready` 的硬性前置条件**：
  1. `artifacts.status = ready`
  2. `qa_reports.status IN ('passed', 'warning')`（若为 `warning`，需对应的 `human_reviews.status = approved_with_warnings`）
  3. 存在有效的 `human_reviews` 记录（`status IN ('approved', 'approved_with_warnings')` 且 `subject_checksum == artifacts.checksum_sha256`）
  4. 目标渠道已授权且在租户配额内。

## 6. Asset 与 AnalysisRun 状态机

- **Asset**：`uploading -> uploaded -> probing -> ready -> analyzing -> analyzed`（异常：`failed | quarantined | deleted`）
- **AnalysisRun**：`pending -> running -> succeeded | partial | failed | cancelled`

## 7. 十二条不可违反的全局不变量

1. 未经人工 `approved` 的 Storyboard，不得创建生产 Timeline。
2. 未通过 Timeline Service 校验的 EditOperation，不得写入新版本。
3. 中/高风险 EditProposal 未经人工审阅并核销一次性 `ApprovalToken`，不得应用。
4. 无响应或超时（`expired`）永远不等价于同意。
5. `base_version` 变更后，所有基于旧版本的 Proposal 与 ApprovalToken 自动失效（409）。
6. 撤销（Undo）只能追加新反向版本，严禁删除或覆写历史 `timeline_versions`。
7. Preview 与 Final Render 必须共享同一 `RenderPlan` 语义，禁止静默语义降级。
8. `qa_reports.status = failed` 强制阻止 Artifact 审批与 Delivery。
9. `artifacts.checksum_sha256` 或 `timeline_version` 变更后，原审批自动失效。
10. 过期 Worker（`lease_token` 不匹配）严禁提交任务结果、发布 Artifact 或记录计费。
11. Agent 不得自审（`reviewer_id` 必须为有权限的真实人类用户且不可为 Agent）。
12. 全部跨表查询与写入必须在事务内设置 `SET LOCAL app.tenant_id` 并受 PostgreSQL `FORCE ROW LEVEL SECURITY` 保护。
