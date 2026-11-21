# ADR-005：数据库级多租户强隔离与 Worker Fencing Token 防脑裂

**状态：** Accepted  
**日期：** 2026-11-21

## 背景
在工程契约包 v1.0 审计中发现两项 P0 级底层架构隐患：
1. 租户隔离仅停留在应用层查询与 SQL 末尾注释：跨表外键仅引用单列 `id` 而非 `(tenant_id, id)`，`workspace_members` 缺少 `tenant_id`，且未启用 PostgreSQL RLS。
2. 任务表仅依赖 `lease_owner` 字符串标识租约，当旧 Worker 发生 GC 停顿或网络分区导致租约过期并被新 Worker 接管后，旧 Worker 恢复运行仍可能覆写任务状态、发布错误 Artifact 或触发重复计费（经典分布式脑裂问题）。

## 决策
1. **数据库级多租户纵深防御**：
   - 所有租户资源表（含 `workspace_members`）均强制包含 `tenant_id`。
   - 所有租户内跨表外键统一使用 `FOREIGN KEY (tenant_id, <ref_id>) REFERENCES <table>(tenant_id, id)` 复合约束。
   - 通过 `006_governance_usage_rls.sql` 对全部 39 张租户表实际启用 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，采用 fail-closed 策略（未设置 `SET LOCAL app.tenant_id` 时返回 0 行）。
   - Worker 领取任务后必须重设事务内租户上下文并重新核验资源归属。
2. **Worker Fencing Token 防脑裂**：
   - `tasks.lease_token` 在每次原子领取（`claim_next_task`）时严格单调递增（`lease_token = lease_token + 1`）。
   - 所有心跳续租、状态推进、任务完成（`complete_task_with_fencing`）、Artifact 升级 `ready` 与 `usage_ledger` 写入均强制校验 `lease_token` 匹配；不匹配时数据库拒绝提交，旧 Worker 必须立即终止并清理临时文件。
