# HITL v2.2 静态验证报告

- JSON Schema：13 份全部通过 Draft 2020-12 Schema 自身校验。
- YAML：全部可解析。
- OpenAPI：34 个路径；所有本地 `$ref` 可解析。
- API：所有写操作均声明 Idempotency-Key；所有端点声明 RBAC 元数据。
- 跨契约枚举：EditOperation、Task.status、HumanReview.status、Asset.status 三端一致。
- 数据库结构静态检查：39 张租户业务表均纳入 FORCE RLS；未发现指向租户业务表的单列外键。
- Fencing：领取、心跳、完成、失败、取消确认、产物发布、用量记账的实时过期检查与锁定规则通过静态断言。
- Approval Token：用户绑定、Review 批准状态、Proposal 绑定、版本/checksum、行锁和单次消费规则通过静态断言。

**结果：47 项静态契约检查通过，0 项失败。**

## 未执行项目

本次生成环境没有 PostgreSQL Server、`psql` 或 Docker。因此：
- PostgreSQL 迁移尚未在真实数据库执行；
- RLS 运行时行为尚未在非超级用户会话验证；
- 故障注入 SQL 尚未运行。

工程团队必须在 PostgreSQL 15+ 的一次性空数据库中执行 `01_database/verify_all_pg15.sh`，保存完整日志并将结果纳入 CI。未通过 Sprint 0 门禁前，不得合并依赖业务表的功能实现。
