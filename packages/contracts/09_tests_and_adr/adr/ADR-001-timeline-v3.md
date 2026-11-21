# ADR-001-timeline-v3: Canonical Timeline v3.0 向后兼容扩展与显式时间语义

- **状态**: Approved & Frozen (v3.0)
- **日期**: 2026-11-21
- **关联协议**: `02_schemas/timeline.v3.schema.json`、`01_database/007_v3_protocol_upgrade.sql`

## 背景与决策
从 v2.2 升级至 v3.0 时，为支撑剪映级多轨编辑能力并避免新旧字段并存导致的渲染/预览歧义，做出以下冻结决策：
1. **保留四类标准轨道**：`video`、`audio`、`subtitle`、`overlay`；`AudioItem` 作为 `audio` 轨道中的标准片段类型，不另造第二套时间轴。
2. **向后兼容扩展**：Clip/AudioItem/OverlayItem 增加 `keyframes`、`effects`（默认 `[]`），顶层增加 `markers`（默认 `[]`）。
3. **统一倍速字段**：全链路统一使用 `playback_rate`（`0.25–4.0`），`timeline.v3.schema.json` 通过 `additionalProperties: false` 严格禁止旧字段 `speed`；历史 v2.2 数据通过 `CR-001` 原地迁移函数 `migrate_timeline_snapshot_v2_to_v3()` 一次性转换为 `playback_rate`。
4. **显式时间语义（防范 Risk #4 预览/导出漂移）**：
   - `time_semantics` 强制声明：`unit = "ticks"`、`timeline_timebase`（有理数 `num/den`，如 `1/30000`）、`frame_rate`（如 `30000/1001`）、`rounding_policy = "half_to_even"`、`coordinate_model = "half_open_source_and_project_ticks"`。
   - 素材源时间 `[source_range.in, source_range.out)` 采用 `source_range.timebase`；项目时间 `[timeline_start, timeline_start + duration)` 采用 `time_semantics.timeline_timebase`。
   - 统一换算公式：`duration_ticks = round_half_to_even(((source_out - source_in) * (source_tb / timeline_tb)) / playback_rate)`，Web 编辑器预览与 FFmpeg RenderPlan 编译器共享同一套有理数时间算法（`services/timeline/time_math.py`）。
5. **并发编辑策略（Risk #1）**：V1 采用 `version` 乐观锁 + `SELECT ... FOR UPDATE` 事务串行化，不引入 CRDT；并发修改同一版本时返回 `409 Conflict` 并附带最新版本号。
