# 渲染引擎、一致性质检与人物检测抽象契约（HITL v2.0）

## 1. V1 渲染架构决策：FFmpeg-Only + 统一 RenderPlan

- **V1 唯一正式渲染实现**：`FFmpegRendererAdapter`（暂不启用 `remotion/`，保留 `RendererAdapter` 抽象接口供后续版本按需扩展）。
- **单一编译路径**：`Immutable TimelineVersion Snapshot -> RenderPlan Compiler -> render_plans (checksum) -> FFmpegRendererAdapter -> Temporary Artifact -> QA -> Ready Artifact`。
- **严禁任意滤镜透传**：禁止 Agent、API 或前端传入原始 FFmpeg 命令或任意 `filtergraph` 字符串；所有滤镜图必须由受控编译器从 `RenderPlan` 确定性生成。

## 2. Preview 与 Final Render 语义一致性硬规则

| 维度 | Preview Render | Final Render | 一致性要求 |
|---|---|---|---|
| 输入来源 | 不可变 `TimelineVersion` 编译的 `RenderPlan`（`render_mode=preview`） | 同一 `TimelineVersion` 编译的 `RenderPlan`（`render_mode=final`） | 必须共用同一个 RenderPlan Compiler |
| 时间基准与剪辑点 | 有理数 `timeline_timebase` 整数 tick | 相同整数 tick | 帧边界与剪辑点误差 ≤ 1 帧 |
| 构图/裁剪/字幕/音频布局 | 完全一致的 `video_ops`、`subtitle_ops`、`overlay_ops`、`audio_ops` | 完全一致 | **禁止因 Preview 降级跳过字幕、构图或音频混音** |
| 允许差异项 | 仅允许降低输出分辨率（如 540×960）、提高 CRF、或限定 `render_range_ticks` 局部区间 | 全分辨率（如 1080×1920）与正式码率/封装 | 除画质档位与局部区间外，零语义差异 |
| 静默降级策略 | **禁止**（`allow_silent_semantic_fallback = false`） | **禁止**（`allow_silent_semantic_fallback = false`） | 硬件编码器切换（NVENC → libx264）须显式写入 `render_jobs.encoder_used` 且不得改变帧率/时长/构图语义 |

## 3. VFR 与非整数帧率（29.97fps / 25fps）精度保障

- Media Probe 阶段检测 `is_vfr` 并对 VFR 素材生成不可变 PTS 映射表（`pts_map_storage_key`）。
- 编译器将源时间 `[source_in, source_out)` 与轴时间 `[timeline_start, timeline_start + duration)` 统一以精确有理数表达，禁用 `float` 秒拼接 `concat` 导致的累积漂移。
- 渲染完成后强制校验输出文件真实时长与 `expected_duration_ticks` 的偏差（容差 ≤ 1 帧周期）。

## 4. `PersonDetectionProvider` 抽象与合规替代（替换 Ultralytics YOLOv8）

- V1 移除 `ultralytics` 与 `yolov8n.pt` 依赖，通过 `PersonDetectionProvider` 端口隔离人物与人脸检测能力：
  - **人脸/姿态/关键点辅助**：[MediaPipe](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE)（Apache-2.0）。
  - **通用人物检测主力**：基于 **ONNX Runtime** 接入许可证经核验允许商业 SaaS 使用的检测模型及权重（**代码许可与模型权重许可必须分别登记核验**）。
- **明确边界**：MediaPipe 不得被默认视为完整通用人物检测器；所有构图管线必须通过 Golden Media 的**多人同框、遮挡、快速移动、远景人物、低光场景**实测验收。

## 5. 技术 QA 与内容 QA 门禁

- **技术 QA（任一失败产生 `blocker`，强制 `qa_reports.status = failed` 阻止审批）**：
  - 容器可解码性与文件完整性（`checksum_sha256` 校验）。
  - 分辨率、帧率、时长与 `RenderPlan.output_profile` 一致。
  - 音轨存在、无异常全段静音、音画同步误差在容差内。
  - 异常黑帧 / 冻结帧检测、峰值响度（True Peak）与字幕安全区检查。
- **内容 QA（严重问题标为 `blocker`，可疑问题标为 `warning` 需 Reviewer 显式确认）**：
  - 断句完整性、字幕与语音一致性、上下文缺失检测。
  - 人物头部/主体裁切检测、Hook 与正文一致性、CTA 完整性与品牌/敏感风险标记。
