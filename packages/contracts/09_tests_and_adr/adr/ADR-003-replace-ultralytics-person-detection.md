# ADR-003：V1 移除 Ultralytics 依赖并抽象商用合规 `PersonDetectionProvider`

**状态：** Accepted  
**日期：** 2026-11-21

## 背景
第一阶段源码审计发现 OpenShorts 的 `Dockerfile` 与 `reframe_v2.py` 依赖 [Ultralytics YOLOv8](https://www.ultralytics.com/license)（AGPL-3.0 / 付费 Enterprise License）。

关于 AGPL-3.0 的合规边界需准确界定：AGPL 对修改后的程序通过网络提供服务存在特定源码提供义务，其具体传染与影响范围取决于程序边界、修改方式、组件组合及分发形态，不能简单一概而论为“整个 SaaS 网站必须开源”。然而，在 V1 阶段保留 AGPL-3.0 依赖会为商业化引入不必要的法律不确定性与额外法务成本。

## 决策
1. **V1 移除 `ultralytics` 与 `yolov8n.pt` 依赖**，不将购买 Ultralytics Enterprise 授权作为 V1 默认方案；商业化上线前由专业法务对全量 SBOM 进行最终复核。
2. 抽象统一 `PersonDetectionProvider` 端口，允许未来按需平滑替换底层模型：
   - **[MediaPipe](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE)（Apache-2.0）**：负责面部检测、姿态/关键点与人物追踪辅助。
   - **ONNX Runtime + 商用合规人物检测模型**：提供通用人物检测推理，所选模型的**代码许可证与权重许可证必须分别核验并登记台账**，确保明确允许商业 SaaS 使用。
3. **强制验收约束**：MediaPipe 不得被默认视为完整通用人物检测器；新检测栈必须通过 Golden Media（特别是 `GM-02`、`GM-11`、`GM-15`）在**多人同框、遮挡、快速移动、远景人物和低光场景**下的实测验收。
4. CI 增加依赖检查（`LIC-001`），阻止未经审批重新引入 `ultralytics`。
