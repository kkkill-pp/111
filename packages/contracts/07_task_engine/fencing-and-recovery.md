# 任务引擎：角色隔离调度、Fencing Token 完整防脑裂与 GPU 容量预留（HITL v2.1）

## 1. 跨租户调度与 `FORCE ROW LEVEL SECURITY` 的职责拆分（P0-2 修复）

- **`app_runtime`（API 与 Worker 执行角色）**：`NOSUPERUSER NOBYPASSRLS`，严格受 39 张租户表的 `FORCE ROW LEVEL SECURITY` 约束；不设置 `SET LOCAL app.tenant_id` 时对任何租户表返回 0 行（fail-closed）。
- **`task_scheduler_role`（独立受限调度角色）**：仅作为 `claim_next_task()` 与 `reclaim_expired_leases()` 两个 `SECURITY DEFINER` 函数的 Owner，普通 Worker 无法切换至该角色，也无权直接查询其他租户表。
- **执行期租户重验**：Worker 通过 `claim_next_task()` 仅获得单条已领取任务的 `(task_id, tenant_id, lease_token, lease_expires_at, payload_json)` 后，必须在独立事务中以 `app_runtime` 身份执行 `SET LOCAL app.tenant_id = '<tenant_id>'` 并重新核验资源归属。

## 2. 六维 Fencing Guard（彻底阻断“过期未重领”旧 Worker，P0-1 修复）

所有执行期写入函数（`heartbeat_task_with_fencing`、`complete_task_with_fencing`、`fail_task_with_fencing`、`publish_artifact_with_fencing`、`record_usage_with_fencing`）必须在数据库事务内同时校验全部 6 项条件：
1. `tenant_id = p_tenant_id`
2. `id = p_task_id`
3. `lease_owner = p_worker_id`
4. `lease_token = p_lease_token`
5. `status IN ('claimed', 'running')`
6. **`lease_expires_at > clock_timestamp()`**（即使 Lease Scanner 尚未运行、新 Worker 尚未接管，已过期租约仍立即被拒绝！）

## 3. 完整原子生命周期与 Outbox 投递规则（P1-5 & P1-6 修复）

- **Outbox 投递状态**：`tasks.status` 默认值为 `queued`；Outbox Dispatcher 创建可执行任务时必须写入 `queued`（依赖未满足时写入 `blocked`），避免任务滞留 `pending`。
- **完整 6 个原子数据库函数**：`claim_next_task`（合法 PostgreSQL 顺序 `ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED`）、`heartbeat_task_with_fencing`、`complete_task_with_fencing`、`fail_task_with_fencing`（指数退避 + jitter）、`confirm_cancel_with_fencing`、`reclaim_expired_leases`。
- **GPU / NVENC / 并发原子预留与释放**：`claim_next_task` 锁定 `workers` 行校验 `active_tasks < max_concurrency`、`gpu_vram_free_mb >= required_gpu_memory_mb` 与 `has_nvenc`，领取时原子扣减剩余显存与并发槽位，在完成/失败/取消/过期回收时原子归还。
