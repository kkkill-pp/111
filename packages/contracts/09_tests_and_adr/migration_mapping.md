# v2.2 → v3.0 协议、API 与数据库兼容迁移映射表

> 本文件作为 `CR-001` 的配套映射清单，明确规定 Schema 字段、Operation 枚举、Proposal 状态机、OpenAPI 路径及数据库迁移/回滚/抽样核对规则。**严禁直接重建数据库或直接删除旧 API 路径。**

## 1. Timeline Schema 字段映射（v2.0 → v3.0）

| v2.2 字段/结构 | v3.0 目标字段/结构 | 迁移规则 | 兼容期政策 |
|---|---|---|---|
| `schema_version: "2.0"` | `schema_version: "3.0"` | `007_v3_protocol_upgrade.sql` 原地转换 | 迁移完成后新写入一律使用 `3.0` |
| `clip.speed` | `clip.playback_rate` | 重命名字段并删除 `speed`（新旧禁止并存） | API 入口兼容期（Phase 1 结束前）自动适配并记录弃用告警；落盘一律 `playback_rate` |
| 无 `time_semantics` | 必填 `time_semantics` | 从 `profile.timeline_timebase` 与 `profile.frame_rate` 补全，`unit="ticks"`, `rounding_policy="half_to_even"` | v3.0 强制校验 |
| `audio` 轨道 `clips`（复用 video clip） | `audio` 轨道 `clips`（`AudioItem`） | 补齐默认 `role="dialogue"`, `volume`, `pan=0.0`, `keyframes=[]`, `effects=[]` | 统一单时间轴模型 |
| 无 `keyframes` / `effects` / `markers` | 默认空数组 `[]` | 原地补齐默认空数组 | 完全向后兼容 |

## 2. EditOperation 映射（18 项 → 21 项）

| v2.2 操作类型 | v3.0 目标操作类型 | 说明 |
|---|---|---|
| `UPDATE_SPEED`（`payload.speed`） | `UPDATE_PLAYBACK_RATE`（`payload.playback_rate`） | SQL 原地更新历史记录；API 兼容期自动映射并输出 `Deprecation` 响应头 |
| 其余 17 项标准操作 | 保持原名不变 | 均保留并补充严格 payload 约束 |
| （新增） | `ADD_KEYFRAME` | 支持关键帧属性动画 |
| （新增） | `APPLY_EFFECT` | 支持视频/音频特效参数 |
| （新增） | `ADD_MARKER` | 支持时间线标记点 |
| `UNDO` / `REDO` | **禁止进入 EditOperation** | 独立端点 `POST /timelines/{timeline_id}/undo`、`/redo`，带 `expected_version` 与 `undo_scope` |

## 3. Proposal 状态机映射（v2.2 → v3.0 `snake_case`）

| v2.2 状态 | v3.0 目标状态 | 备注 |
|---|---|---|
| `draft` | `draft` | 保持不变 |
| `validating` | `submitted` | 已提交待校验归入 `submitted` |
| `awaiting_review` | `reviewing` | 统一为审阅中 `reviewing` |
| `approved` | `approved` | 保持不变 |
| `applied` | `applied` | 强校验已核销 `approval_token_binding` |
| `rejected` | `rejected` | 保持不变 |
| `expired` | `expired` | 保持不变 |
| `superseded` / `invalid` | `cancelled` | 归并为 `cancelled`，原始原因保留在 `validation_json.legacy_status` |
| （新增） | `revision_requested` | 人工要求修订 |
| （新增） | `conflicted` | 并发乐观锁冲突导致基准版本失效 |

## 4. OpenAPI 3.1 路径兼容与弃用策略

| 能力域 | v3.0 规范路径 | v2.2 旧路径处理 | 弃用与下线窗口 |
|---|---|---|---|
| Storyboard 审阅 | `POST /storyboards/{storyboard_id}/reviews` | 保持不变（统一复数） | 永久保留 |
| Proposal 审阅 | `POST /proposals/{proposal_id}/reviews` | 保持不变（统一复数） | 永久保留 |
| Artifact 审阅 | `POST /artifacts/{artifact_id}/reviews` | 保持不变（统一复数） | 永久保留 |
| Undo / Redo | `POST /timelines/{timeline_id}/undo` & `/redo` | 保持不变，新增 `expected_version` 与 `undo_scope` | 旧请求缺省 `undo_scope` 时默认按 `user_edit` 兼容至 v3.1 |
| 历史单数审阅别名（如有外部客户端调用） | 统一重定向至复数 `/reviews` | 禁止新增单数 `/review` 或 `/approval` 端点 | 已在 v2.1 阶段清理完毕 |

## 5. 数据库原地迁移、回滚与抽样核对流程（Risk #5）
1. **迁移前备份**：`007_v3_protocol_upgrade.sql` 自动将变更前的 `timeline_versions.snapshot_json`、`edit_proposals.status`、`edit_operations.operation_type` 备份至 `migration_backup.v22_*`（`v22_timeline_versions` / `v22_edit_proposals` / `v22_edit_operations`） 临时审计表（保留 30 天，不删除旧库）。
2. **原地执行**：按序执行 `007_v3_protocol_upgrade.sql`（幂等事务块）。
3. **迁移后抽样核对（必须 0 异常）**：
   - 核对 `timeline_versions` 中包含旧字段 `"speed"` 的记录数 = `0`。
   - 核对 `edit_operations` 中 `operation_type = 'UPDATE_SPEED'` 记录数 = `0`。
   - 核对 `edit_proposals` 中不在 v3.0 10 个合法状态内的记录数 = `0`。
   - 随机抽取 5%（至少 20 条）`timeline_versions` 记录通过 `timeline.v3.schema.json` 校验，通过率必须 `100%`。
4. **回滚策略**：若抽样核对失败，立即执行 `007_v3_protocol_rollback.sql` 从 `migration_backup.v22_*`（`v22_timeline_versions` / `v22_edit_proposals` / `v22_edit_operations`） 原地恢复字段与约束，无需重建数据库。
