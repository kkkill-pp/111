# HITL v2.2 修订记录

本版本在 v2.1 基础上做安全性和开发执行门禁修订，不推翻既有模块结构和业务设计。

## 本次修复
1. Worker 租约有效性判断统一改用 `clock_timestamp()`，避免 PostgreSQL `now()` 的事务开始时间语义造成长事务误判。
2. `publish_artifact_with_fencing()` 对任务行执行 `FOR UPDATE`，与租约回收串行化。
3. `record_usage_with_fencing()` 对任务行执行 `FOR UPDATE`，与租约回收串行化。
4. `confirm_cancel_with_fencing()` 增加实时租约有效期检查，过期 Worker 不能确认取消或释放资源。
5. `claim_next_task()` 限制租约秒数为 5–3600，避免非法租约参数。
6. 审批 Token 核销增加用户绑定、已批准 Review 校验、Proposal 状态/版本/checksum 校验和 Token 行锁。
7. FI-2 改为先用合法租约领取任务，再显式将租约置为过期；新增过期 Worker 不能确认取消的断言。
8. PostgreSQL 验证脚本增加 PG15+ 版本和空数据库预检，防止误用于已有数据库。
9. 新增 `START_HERE_FOR_ENGINEERING.md`，明确开发授权范围、顺序、门禁、身份安全和 Definition of Done。

## 验证结果
- `09_tests_and_adr/verify_contracts.py`：47 项静态/结构/契约检查通过。
- PostgreSQL 迁移、RLS 实测和故障注入：本交付环境没有 PostgreSQL Server、psql 或 Docker，未运行；必须由工程团队在 Sprint 0 的真实 PG15+ 空库执行。

## 仍然需要工程实现与验证
- Worker Gateway 的 mTLS / Worker 身份验证。
- Outbox Dispatcher 的生产级投递循环、去重和死信运维。
- Timeline Proposal 应用与 Approval Token 消费的同事务编排。
- 真实 PostgreSQL 并发故障测试与迁移工具集成。
- 生产环境容量、安全、备份恢复、灾备和法务审查。
