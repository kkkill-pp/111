# ADR-002：通过 Adapter 隔离复用 OpenShorts 媒体能力并剔除受限目录

**状态：** Accepted（基于第一阶段源码审计确认）  
**日期：** 2026-11-21

## 背景
第一阶段审计核验了 [mutonby/openshorts](https://github.com/mutonby/openshorts)（`main@b3cd947`）：根目录核心代码采用 [MIT 许可](https://github.com/mutonby/openshorts/blob/main/LICENSE)，而 [cloud/LICENSE](https://github.com/mutonby/openshorts/blob/main/cloud/LICENSE) 为 Source-Available 商业许可，明确禁止向第三方提供托管/SaaS/付费或竞争服务；同时其根目录平铺 50+ 文件，存在 `reframe_v2.py` 反向 `import main` 的强耦合及进程内信号量队列。

## 决策
1. 业务层只依赖内部 `MediaEngine` 端口，OpenShorts 仅作为受控 `OpenShortsAdapter` 的底层实现来源。
2. Fork 上游基线时锁定完整 40 位 Commit SHA，并**物理移除 `cloud/` 目录与 `requirements-billing.txt`**，在 CI 中配置路径红线检查。
3. 严禁暴露 OpenShorts 原生 `mcp_server.py`（包含无审批门禁的 `publish_clip`）与单机内存队列。
4. Adapter 统一输入 `AssetRef + Options`，输出有理数时间基的规范 DTO + `ArtifactRef` + 耗时/成本指标，全部运行在独立非 root Worker 容器内。
