# AI Video Studio — 工程开发启动令（HITL v2.2）

**状态：允许启动分阶段开发；不等于数据库迁移已在目标环境实测通过，也不等于生产上线批准。**

本文件是工程团队的执行入口。以本包为唯一 V1 契约基线。除非通过 ADR 变更，不得自行改变核心状态机、权限边界、Timeline 写入规则、RenderPlan 语义或许可证决策。

## 1. 开发授权范围

工程团队现在可以开始：
- 仓库、CI、环境、数据库连接与服务骨架；
- 数据库迁移和 RLS / 角色隔离实现；
- API、Schema Validator、Timeline Service、Task Engine 的基础实现；
- 前端页面骨架和 API client；
- OpenShorts Adapter、FFmpeg Renderer Adapter 的隔离层；
- 测试夹具、Golden Media 管线和可观测性基础设施。

**但必须先完成 Sprint 0 数据库门禁，才能合并任何依赖业务表的功能代码到主分支。** 前端可并行搭骨架，但不得自行定义与契约不同的接口或状态。

## 2. V1 不可擅自改变的架构决策

1. OpenShorts 仅通过 `OpenShortsAdapter` 复用获准的媒体处理能力；禁止复用 `cloud/`、原生 `mcp_server.py` 和未经许可核验的依赖。
2. YOLOv8 / Ultralytics 不进入 V1 默认运行链。人物检测通过 `PersonDetectionProvider` 抽象；MediaPipe 用于其适合的人脸、姿态、关键点等任务，通用人物检测模型须单独核验代码和权重许可。
3. V1 正式渲染器只有 FFmpeg。Preview 与 Final Render 必须消费相同语义的 RenderPlan。
4. PostgreSQL 是业务状态唯一权威来源。Redis 只作缓存、短期协调，不得成为业务状态事实来源。
5. Timeline Service 是 Timeline 唯一写入口。Agent、Worker、前端不得绕过服务直接改写 Timeline 表。
6. Timeline Version 是不可变快照。修改必须产生新版本，不得覆盖历史快照。
7. Agent 只能按权限矩阵调用工具。Proposal 不等于执行；高风险动作必须经过明确审批；无响应不等于同意。
8. 所有跨租户业务关系必须保留 `tenant_id` 复合约束和 RLS；Worker 执行时必须重新验证租户上下文。
9. 所有任务执行期副作用必须绑定有效的 `lease_owner + lease_token + lease_expires_at`。过期租约不得完成、发布产物、记账或确认取消。
10. 供应商、模型、提示词、成本和策略必须配置化，不得散落硬编码在业务流程中。

## 3. 强制开发顺序与验收门禁

### Sprint 0 — 数据库与安全基础（阻断门禁）

必须按以下顺序完成：
1. 创建全新、一次性 PostgreSQL 15+ 测试数据库。
2. 运行 `01_database/verify_all_pg15.sh`，脚本会检查 PostgreSQL 版本和空库条件，然后执行全部迁移、RLS 检查和故障注入。
3. 将完整终端日志作为 CI Artifact 保存；不得只提交“通过”截图。
4. 验证数据库角色属性：`app_runtime` 不得是 superuser / BYPASSRLS；`task_scheduler_role` 只能通过受限 SECURITY DEFINER 调度函数执行跨租户调度。
5. 在真实 `app_runtime` 非超级用户连接下验证租户 A 看不到租户 B 数据，且跨租户写入被复合外键拒绝。
6. 运行 FI-1、FI-2、FI-3 故障注入；任何失败都阻断后续依赖业务表的开发合并。

**注意：** `verify_all_pg15.sh` 是空库验证脚本，不是升级脚本，不得对已有数据库运行。正式环境迁移必须使用团队选定的迁移工具记录版本、校验和、锁和回滚策略。

### Sprint 1 — 最小垂直切片

必须按真实用户路径跑通：
1. 创建 Workspace / Project；
2. 上传自有素材，完成校验、对象存储写入和媒体探测；
3. 生成代理文件、缩略图、音频转写、场景与关键帧分析；
4. 生成 Storyboard；
5. Agent 生成 EditProposal，不直接修改 Timeline；
6. 人工查看 Diff，逐项接受 / 拒绝；
7. 审批绑定具体版本和 checksum；
8. Timeline Service 应用提案并创建不可变新版本；
9. 使用统一 RenderPlan 生成 Preview；
10. QA 生成结构化报告。

交付物必须包含可复现的演示素材、数据库记录、任务日志、Timeline 版本链、审批审计事件和 Preview 文件。

### Sprint 2 — 可靠性与交付

- 任务幂等、重试、取消、租约过期恢复；
- CPU / GPU Worker 资源预留、释放与失联恢复；
- Final Render 与 Preview 语义一致；
- QA 阻断规则、交付审批、下载 / 分享权限；
- 账单记录幂等，禁止过期 Worker 记账；
- Golden Media 测试覆盖 VFR、29.97fps、旋转元数据、无音轨、多音轨、超长素材、损坏文件、低分辨率、字幕与字体等。

## 4. Worker / Scheduler 身份安全要求

`claim_next_task()` 的 `p_worker_id` 不能被视为身份认证本身。调用前必须由内部 Worker Gateway 验证 Worker 的 mTLS 身份或等价强身份凭据，并将已验证的 worker_id 传入函数。浏览器、普通用户 API、Agent 工具不得直接调用调度函数。

Worker 获得任务后，以 `app_runtime` 身份建立独立事务，设置 `SET LOCAL app.tenant_id`，再读取任务及其依赖资源。不得将 `task_scheduler_role` 凭据发给 Worker 进程。

## 5. 审批与 Timeline 原子性

审批 Token 必须绑定：
- tenant；
- subject type / subject id；
- Timeline base version；
- proposal checksum；
- 已批准的 HumanReview；
- 被授权用户；
- 过期时间；
- 单次消费状态。

Token 消费和 Proposal 应用必须在同一个数据库事务中完成；若服务层分成多个 SQL 调用，必须由同一个事务包裹。不得出现 Token 已消费但 Timeline 未应用、或 Timeline 已应用但审批未消费的半完成状态。

## 6. API 与 Schema 规则

- `03_openapi/openapi.yaml` 是 HTTP API 的唯一接口来源。
- `02_schemas/*.schema.json` 是跨服务领域对象的唯一 JSON Schema 来源。
- 禁止在前端、Worker、Agent 中复制一套独立枚举；需要代码生成或 CI 对比。
- 所有变更写操作必须实现 Idempotency-Key；重复请求返回同一逻辑结果。
- Timeline 写操作必须带 `expected_version`；版本冲突返回明确 409，不得静默覆盖。
- 异步操作返回任务资源，不得假装同步完成。
- 错误响应必须携带稳定的 `code`、可读 `message`、`trace_id`。

## 7. Definition of Done（每个模块）

模块只有同时满足以下条件才算完成：
- 实现与 OpenAPI / JSON Schema 一致；
- 单元测试和集成测试通过；
- 权限与租户隔离测试通过；
- 幂等、重试和异常路径测试通过；
- 关键状态变更有审计事件；
- 日志可用 trace_id 关联；
- 没有未经批准的许可证依赖；
- 有运行说明、环境变量说明和回滚说明；
- CI 结果可复现。

## 8. 不得宣称已经完成的事项

本契约包静态校验已在生成环境执行；但当前交付生成环境没有 PostgreSQL Server / psql / Docker，因此数据库迁移和故障注入**尚未实机运行**。工程团队必须在 Sprint 0 运行数据库验证脚本，并提交原始日志。静态检查通过不能替代数据库实测。

本包是 V1 工程开发基线，不是完整生产运维手册；容量压测、备份恢复演练、灾备、渗透测试、隐私合规与正式许可证法务意见仍需在上线前完成。
