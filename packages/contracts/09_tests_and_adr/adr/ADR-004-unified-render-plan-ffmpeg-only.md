# ADR-004：V1 锁定 FFmpeg-Only 渲染并强制 Preview/Final 共享统一 `RenderPlan`

**状态：** Accepted  
**日期：** 2026-11-21

## 背景
1. OpenShorts 仓库包含 `remotion/`，但 [Remotion Company License](https://www.remotion.dev/docs/license/pricing) 仅对个人、非营利或 ≤3 人营利组织免费，≥4 人公司商用须购买付费授权；同时维护 FFmpeg 与 Remotion 双渲染管线极易导致 Preview 与 Final 剪辑语义漂移。
2. OpenShorts `reframe_v2.py` 存在 `gpu_render -> per-scene FFmpeg -> v1 OpenCV` 静默回退与浮点秒拼接，无法满足确定性产物要求。

## 决策
1. **V1 主渲染引擎锁定为 FFmpeg-Only**，暂不启用 `remotion/`；动态图形与字幕模板在 V1 通过受控 FFmpeg 滤镜与 libass 实现。
2. 保留 `RendererAdapter` 抽象接口，未来如采购商业授权或有复杂动效需求时可按需接入 Remotion 或其他渲染器。
3. Preview 与 Final Render 必须由同一不可变 `TimelineVersion` 经同一个 `RenderPlan Compiler` 编译为 `render-plan.schema.json`（`allow_silent_semantic_fallback: false`），仅允许在分辨率/码率/局部渲染区间上存在差异，剪辑点、构图、字幕与音频混音语义完全一致。
