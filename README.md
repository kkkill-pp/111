# AI Video Studio — Sprint 0 门禁脚手架 + Sprint 1 最小垂直切片

本仓库严格以 `packages/contracts/` 中的 **《工程契约包 HITL v2.2 开发启动版》** 为唯一基线构建（`packages/contracts/` 与 `migrations/` 零擅自改动）。

> **OpenAPI 覆盖范围说明**：`HITL v2.2` 契约共定义 40 个端点操作；Sprint 1 最小垂直切片当前实现核心闭环的 **21/40（52%）** 端点（其余 19 个列表/管理/Final Render/Redo/Task Cancel 端点按路线图归属 Sprint 2 实现）。

## 运行说明

### 1. Sprint 0 阻断门禁（需本地 Docker 或 PostgreSQL 15+ 与 `psql`）
```bash
# 方式 A：自动拉起一次性 PostgreSQL 15 容器并执行全量门禁
bash scripts/run_sprint0_local.sh

# 方式 B：使用 docker-compose 内的独立空库（端口 5433，库名 aivs_sprint0_empty）
docker compose -f infra/compose/docker-compose.yml up -d postgres-sprint0
DATABASE_URL="postgres://postgres:postgres_dev_password@localhost:5433/aivs_sprint0_empty" \
  bash scripts/run_sprint0_local.sh
```
完整原始终端日志将自动保存至 `logs/sprint0_pg15_<timestamp>.log`。GitHub Actions 工作流见 `.github/workflows/sprint0-db-gate.yml`（推荐直接推至 GitHub 运行并归档 `sprint0-pg15-and-test-logs` Artifact）。

### 2. 跨契约一致性校验（61 项）与 Sprint 1 垂直切片自动化测试（真实 FFmpeg/ffprobe）
```bash
pip install -r requirements.txt
python3 packages/contracts/09_tests_and_adr/verify_contracts.py
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

### 3. 本地启动 FastAPI 服务
```bash
uvicorn apps.api.main:app --reload --port 8000
```

## Sprint 4 — Global Platform Plugin Registry

Sprint 4 adds a platform-global plugin catalog, immutable release records, append-only governance events, license-review gating, and token-protected platform administration APIs. Tenant plugin enablement/configuration remains tenant-scoped and is not elevated by this registry.

See [`docs/SPRINT4_PLATFORM_REGISTRY.md`](docs/SPRINT4_PLATFORM_REGISTRY.md) for migration 010, dedicated database role setup, API headers, and verification boundaries.
