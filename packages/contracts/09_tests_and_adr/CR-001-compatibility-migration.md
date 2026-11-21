# CR-001: v2.2 兼容性修复与原地平滑迁移至 v3.0 协议基线

- **变更编号**: CR-001
- **状态**: Approved (由项目负责人正式批准)
- **影响范围**: `01_database/verify_rls.sql`、`01_database/007_v3_protocol_upgrade.sql`、`01_database/007_v3_protocol_rollback.sql`、`02_schemas/*.v3.schema.json`、`03_openapi/openapi.yaml`

## 一、原始问题与根因
1. **RLS 空表/超级用户假通过缺陷（原独立核验 P1-C）**：
   - 原 `01_database/verify_rls.sql` 的 Test 2 在未切换 `app_runtime`（`NOBYPASSRLS`）且 `projects` 表为空的情况下执行 `RESET app.tenant_id; SELECT count(*) FROM projects;`，在 `postgres` 超级用户连接下会因空表而“假通过”。
2. **v2.2 → v3.0 协议原地升级（Risk #5 禁止重建数据库）**：
   - 需在不重建数据库、不丢弃现有 39 张租户表与六维 Fencing Token 守卫的前提下，将 `timeline`、`edit_operations`、`edit_proposals`、`render_plans`、`approval_tokens` 平滑升级至 v3.0。

## 二、批准修订方案
1. **修复 `verify_rls.sql`**：
   - 先以表所有者身份写入 2 个独立租户（Tenant A / Tenant B）及各自 1 条 `projects` 记录。
   - 显式 `SET LOCAL ROLE app_runtime;`（`NOBYPASSRLS`），执行 `RESET app.tenant_id;` 后断言 `SELECT count(*) FROM projects` 必须为 `0`（真实 fail-closed）。
   - 分别 `SET LOCAL app.tenant_id` 为 Tenant A 与 Tenant B，断言各自仅见 `1` 条且不可见对方数据。
   - 全程包裹在 `BEGIN; ... ROLLBACK;` 中，不污染目标库。
2. **原地增量迁移 `007_v3_protocol_upgrade.sql`**：
   - 为 `edit_operations` 迁移历史 `'UPDATE_SPEED'` → `'UPDATE_PLAYBACK_RATE'`，放宽并重建 21 项 CHECK 约束，新增 `undo_scope`、`agent_permission_level` 列。
   - 为 `edit_proposals` 迁移历史状态（`validating -> submitted`、`awaiting_review -> reviewing`、`superseded/invalid -> cancelled`），重建 10 项 `snake_case` CHECK 约束，新增 `agent_permission_level`、`base_timeline_checksum` 列。
   - 为 `timeline_versions` 升级 `origin_command`（新增 `'proposal_apply'`）与 `undo_scope` 列，并提供 `migrate_timeline_snapshot_v2_to_v3(jsonb)` 原地将 `speed -> playback_rate` 并补齐 `keyframes: []`、`effects: []`、`markers: []`、`time_semantics`。
   - 为 `render_plans` 新增 `immutable_locked_at` 与 `BEFORE UPDATE OR DELETE` 触发器 `enforce_render_plan_immutability()`，强制锁定后不可变。
   - 为 `approval_tokens` 新增 `project_id`、`timeline_checksum` 列与 `consume_approval_token_for_timeline_v3()` 原子五维校验函数。
3. **回滚与抽样核对**：
   - 配套交付 `007_v3_protocol_rollback.sql`（含迁移前快照备份表恢复逻辑与迁移后抽样核对 SQL），确保升级失败可安全回退且无数据丢失。
