# ADR-003-render-plan-v3: 不可变 RenderPlan v3.0 与 FFmpeg-Only 预览/导出一致性

- **状态**: Approved & Frozen (v3.0)
- **日期**: 2026-11-21
- **关联协议**: `02_schemas/render-plan.v3.schema.json`、`01_database/007_v3_protocol_upgrade.sql`

## 决策内容
1. **FFmpeg-Only V1 渲染锁定**：`renderer` 锁定为 `"ffmpeg"`，Preview 与 Final Render 共用同一 `RenderPlan v3.0` 结构，保留 Adapter 接口以备未来扩展。
2. **RenderPlan 不可变性强制锁（Risk #3）**：
   - `render-plan.v3.schema.json` 强制要求 `plan_id`、`plan_checksum`（SHA-256）、`immutability_lock`（`locked: true`、`locked_at`、`source_timeline_version`、`source_timeline_checksum`）及 `determinism_flags.immutable_once_queued = true`。
   - 数据库层通过 `007_v3_protocol_upgrade.sql` 中的 `enforce_render_plan_immutability()` `BEFORE UPDATE OR DELETE` 触发器强制拦截任何对已锁定/已入队 `render_plans` 记录的修改或删除；修改时间线必须编译生成新的 `RenderPlan`。
3. **零静默降级（Fail-Fast）**：
   - `allow_silent_semantic_fallback: false`、`allow_arbitrary_filtergraph: false`、`require_exact_duration_check: true` 均为常量强约束。
   - 任何尚未实现的滤镜、转场、特效或叠加层必须在渲染前返回 `422 Unprocessable Entity`，绝不允许静默丢弃。
4. **统一时间计算与真实 ffprobe QA（Risk #4）**：
   - `video_ops` 与 `audio_ops` 均携带 `source_timebase`、`playback_rate` 与 `duration_ticks`，并由统一时间计算测试集（`tests/test_time_semantics_v3.py`）与真实 `ffprobe` 校验音画时长一致性。
