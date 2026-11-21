# ADR-004-agent-proposal-v3: 统一 L0–L3 权限分级、snake_case 状态机与原子 ApprovalToken

- **状态**: Approved & Frozen (v3.0)
- **日期**: 2026-11-21
- **关联协议**: `02_schemas/agent-proposal.v3.schema.json`、`01_database/007_v3_protocol_upgrade.sql`

## 决策内容
1. **统一 Agent 权限模型（L0–L3）**：
   - `L0`：只读（严禁生成或提交任何修改提案，Schema 直接将 `agent_permission_level` 限制为 `["L1", "L2", "L3"]`）。
   - `L1`：生成提案（仅生成待审 Proposal，不得直接应用）。
   - `L2`：限定范围执行（仅在人工显式授权边界与单次 Token 下应用）。
   - `L3`：受控自动化（仅限租户级策略批准的低风险自动化流程，仍受审计与预算门禁约束）。
2. **统一 `snake_case` Proposal 生命周期状态机**：
   - 主路径：`draft → submitted → reviewing → approved → applied`
   - 异常状态：`rejected`、`revision_requested`、`conflicted`、`expired`、`cancelled`
3. **原子单次 ApprovalToken 绑定（三项补充强约束）**：
   - **五维强绑定**：Token 必须同时绑定 `tenant_id`、`project_id`、`proposal_id`、`timeline_version` 以及 `proposal_checksum` + `timeline_checksum`。
   - **强制过期时间**：`expires_at` 必填，数据库函数使用 `clock_timestamp()` 实时判定过期。
   - **原子事务单次核销**：`consume_approval_token_for_timeline_v3()` 在同一数据库事务中 `FOR UPDATE` 锁住 `timelines`、`approval_tokens`、`human_reviews`、`edit_proposals` 四张表，确保核销与提案应用原子完成，重复使用或版本漂移立即失败。
   - Schema 层通过 `allOf` 条件规则强制要求：当 `status == "applied"` 时，必须存在已核销（`consumed_at` 非空）的 `approval_token_binding` 与 `result_version`。
