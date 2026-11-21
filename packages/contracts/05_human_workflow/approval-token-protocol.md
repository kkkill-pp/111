# 一次性版本绑定 ApprovalToken 协议（HITL v2.2）

## 1. 设计目标

防止 Agent 伪造审批、重放旧审批、跨用户使用审批，或将针对 Timeline v3 的审批误用于 Timeline v4。

## 2. 签发条件

仅当人类审阅者通过 `POST /proposals/{proposal_id}/reviews` 提交 `decision IN ('approve', 'approve_with_warnings')` 且通过 RBAC、租户、版本和 checksum 校验后，由 Approval Service 在数据库事务内签发：

1. 生成 256-bit 加密随机明文 Token（仅通过 API 响应返回一次，服务端永不落盘明文）。
2. 计算 `token_hash = sha256(raw_token)` 存入 `approval_tokens` 表。
3. Token 必须绑定：
   - `tenant_id`
   - `human_review_id`
   - `subject_type = 'edit_proposal'`
   - `subject_id = proposal_id`
   - `bound_version = base_version::text`
   - `bound_checksum = proposal_checksum`
   - `issued_to_user_id = reviewer_id`
   - `expires_at`
   - `consumed_at IS NULL`

## 3. 原子核销与应用（必须同一事务）

Timeline Service 在一个 PostgreSQL 事务内执行以下步骤：

1. 锁定 `timelines` 行，并确认 `current_version = expected_base_version`。
2. 调用数据库函数：

```sql
SELECT consume_approval_token_for_timeline(
  p_tenant_id,
  p_timeline_id,
  p_proposal_id,
  p_expected_base_version,
  p_expected_checksum,
  p_token_hash,
  p_authenticated_user_id
);
```

3. 数据库函数会锁定并核验 Token、HumanReview、EditProposal 三行，确认：
   - Token 未消费且未过期；
   - Token 的 `issued_to_user_id` 等于当前认证用户；
   - Review 状态为 `approved` 或 `approved_with_warnings`；
   - Review 的 subject、版本和 checksum 与 Proposal 一致；
   - Proposal 状态为 `approved`，Timeline、base_version 和 proposal_checksum 全部匹配；
   - Timeline 当前版本仍等于批准时的 base_version。
4. 函数返回 `human_review_id` 才允许继续；返回 `NULL` 必须回滚整个事务。
5. 在同一事务内应用已批准的 EditOperations，写入新的不可变 `timeline_versions`，更新 `timelines.current_version` 和 `edit_proposals.status = 'applied'`，写入 `audit_events` 后提交。

**禁止**把 Token 核销和 Timeline 应用拆成两个独立事务。否则可能出现 Token 已消费但 Timeline 未应用的半完成状态。

## 4. 失败语义

- Token 不存在、用户不匹配、Review 未批准、Proposal 不匹配、Token 已消费或过期：返回 `409 APPROVAL_INVALID`。
- Timeline 版本已经前进：返回 `409 VERSION_CONFLICT`。
- 以上失败必须回滚，不得消费 Token，不得产生 Timeline 新版本。

## 5. 自动失效

Timeline 产生任何新版本后，绑定旧 base_version 的审批无法再核销。后台可清理已过期 Token，但清理不是安全边界；核销函数必须每次重新校验版本、状态和有效期。
