# AI Video Studio — 工程契约包 HITL v2.2

本版本基于 HITL v2.0 的评审意见执行了定向工程修复（保留全部合理的人机协同、不可变时间轴与 FFmpeg-only 设计，修复全部 4 项 P0 与 4 项 P1 问题），并内置自动化跨契约一致性校验脚本与 PostgreSQL 15+ 三项故障注入验证脚本。

## v2.1 关键修复一览

1. **P0-1 Fencing Token 过期拦截**：`complete_task_with_fencing`、`heartbeat_task_with_fencing`、`fail_task_with_fencing`、`publish_artifact_with_fencing`、`record_usage_with_fencing` 全部强制校验 `lease_expires_at > now()` 与 `lease_owner`。
2. **P0-2 调度器与 RLS 角色隔离**：新增 `000_roles_and_extensions.sql` 严格分离 `app_runtime`（`NOBYPASSRLS` + `FORCE RLS`）与 `task_scheduler_role`（仅拥有 `SECURITY DEFINER` 调度函数）。
3. **P0-3 PostgreSQL 锁子句顺序修复**：统一修正为 `ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED`。
4. **P0-4 EditOperation 枚举三端统一**：明确 `UNDO`/`REDO` 为独立版本命令（`/timelines/{id}/undo|redo`），SQL、JSON Schema 与 OpenAPI 严格统一为 18 项 `EditOperation`，并由 `09_tests_and_adr/verify_contracts.py` 机检防漂移。
5. **P1-5 完整任务恢复原子函数**：补齐心跳续租、指数退避失败重试、合作式取消确认、过期租约回收全部 6 个原子函数，明确 Outbox 创建任务状态为 `queued`。
6. **P1-6 GPU/NVENC/并发容量调度**：`claim_next_task` 原子校验并预留/释放 `active_tasks`、`gpu_vram_free_mb` 与 `has_nvenc`。
7. **P1-7 补齐 4 个独立业务 Schema**：新增 `task.schema.json`、`asset.schema.json`、`artifact.schema.json`、`delivery.schema.json`（共 13 个 Draft 2020-12 Schema）。
8. **P1-8 收紧 OpenAPI 3.1 响应契约**：消除关键端点泛化 `type: object`，补齐分页（`limit`/`cursor`/`next_cursor`）与 `x-required-roles` RBAC 元数据。

## 本地验证命令

```bash
# 1. 运行跨契约一致性校验（无需数据库）
python3 09_tests_and_adr/verify_contracts.py

# 2. 在空 PostgreSQL 15+ 数据库上执行全部迁移 + RLS 校验 + 3 项故障注入测试
DATABASE_URL="postgres://postgres:postgres@localhost:5432/aivs_test" ./01_database/verify_all_pg15.sh
```


## 工程开发入口

请先阅读 [`START_HERE_FOR_ENGINEERING.md`](START_HERE_FOR_ENGINEERING.md)。本版本允许分阶段启动开发，但数据库 Sprint 0 必须先在真实 PostgreSQL 15+ 空库通过迁移、RLS 和故障注入门禁，才能合并依赖业务表的功能实现。

## v2.2 额外修复

- 所有执行期租约有效性判断使用 `clock_timestamp()`。
- 产物发布和用量记账先锁定任务行，避免与租约回收并发竞态。
- 取消确认增加过期租约校验。
- 审批 Token 增加用户、已批准 Review、Proposal 状态/版本/checksum 绑定。
- 数据库验证脚本增加 PostgreSQL 版本与空库预检。

验证边界：契约静态检查已通过；PostgreSQL 迁移与故障注入尚待工程团队在目标环境实测。
