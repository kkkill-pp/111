# ADR-002-edit-operation-v3: 21 项标准 EditOperation 与独立 Undo/Redo 版本命令

- **状态**: Approved & Frozen (v3.0)
- **日期**: 2026-11-21
- **关联协议**: `02_schemas/edit-operation.v3.schema.json`、`03_openapi/openapi.yaml`

## 决策内容
1. **21 项标准编辑操作**：在 v2.2 的 18 项基础上将 `UPDATE_SPEED` 规范升级为 `UPDATE_PLAYBACK_RATE`（通过兼容迁移映射历史操作），并新增 3 项 v3.0 能力：`ADD_KEYFRAME`、`APPLY_EFFECT`、`ADD_MARKER`，共计 21 项；每项均在 Draft 2020-12 Schema 中定义了严格的 `if/then` + `additionalProperties: false` payload 约束。
2. **UNDO / REDO 独立版本命令**：
   - `UNDO` 与 `REDO` 严格禁止进入 `EditOperation.operation_type` 枚举（CI 反向测试强制拦截）。
   - 使用独立端点 `POST /timelines/{timeline_id}/undo` 与 `POST /timelines/{timeline_id}/redo`。
   - 每次 Undo/Redo 必须生成新的不可变版本（`origin_command = 'undo' | 'redo'`，`restored_from_version` 可追溯），严禁物理删除历史版本。
   - 调用时必须携带 `expected_version` 进行乐观锁校验，防止覆盖其他协作者的新提交（版本不匹配返回 `409 Conflict`）。
3. **Undo/Redo 操作边界（Risk #2）**：
   - 每条操作与版本必须显式标记 `undo_scope`：`user_edit`（人类可见且可逆编辑）、`proposal_batch`（AI 提案整批回滚）、`non_undoable_system`（系统/渲染/外部交付类不可逆操作）。
   - 普通用户 `Ctrl+Z`（`undo_scope = user_edit`）不会无差别跨边界撤销 AI 已审批提案或外部渲染任务；撤销 AI 提案需显式指定 `proposal_batch` 边界并生成审计记录。
