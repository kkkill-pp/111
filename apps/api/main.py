"""AI Video Studio FastAPI Application — Sprint 1 Vertical Slice (HITL v2.2)."""
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
import time
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from services.agent_runtime.harness import AgentHarness
from services.agent_runtime.mcp_server import AIVSMCPServer
from services.timeline.otio_exporter import export_timeline_to_otio
from services.application.review_service import ReviewService
from services.application.timeline_service import TimelineService
from services.infrastructure.contracts import canonical_sha256, validate_against_schema
from services.infrastructure.errors import AppError
from services.infrastructure.observability import (
    MetricsRegistry,
    SlidingWindowRateLimiter,
    StructuredLogger,
    resolve_trace_id,
)
from services.infrastructure.pg_store import create_tenant_store
from services.infrastructure.store import TenantContext, iso_now, require_role
from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256
from services.media_engine.capability_registry import DEFAULT_CAPABILITY_REGISTRY
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler
from services.media_engine.qa_engine import QAEngine
from services.workers.render_job_manager import RenderJobManager
from services.plugins import PluginRegistry
from services.plugins.plugin_loader import load_plugins_from_directory
from services.plugins.builtin import (
    AnchoredReviewPlugin,
    HtmlGsapMotionPlugin,
    MediaGatewayPlugin,
    MotionOverlayRendererPlugin,
    MotionTemplatePlugin,
)


# ---- Top-level Pydantic Request Schemas ----
class CreateWorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    hitl_policy: Optional[dict[str, Any]] = None


class CreateProjectBody(BaseModel):
    workspace_id: str
    name: str = Field(min_length=1, max_length=160)
    description: Optional[str] = None


class CreateUploadBody(BaseModel):
    filename: str
    size_bytes: int = Field(ge=1)
    kind: str


class CompleteUploadBody(BaseModel):
    checksum_sha256: str = Field(min_length=64, max_length=64)


class CreateBriefBody(BaseModel):
    title: str
    brief: dict[str, Any]


class ProposeStoryboardBody(BaseModel):
    brief_version_id: str
    asset_id: str


class ReviewBody(BaseModel):
    subject_version: str
    subject_checksum: str
    decision: str
    qa_report_id: Optional[str] = None
    comment: Optional[str] = None
    selected_items: list[str] = Field(default_factory=list)
    rejected_items: list[str] = Field(default_factory=list)


class CreateTimelineBody(BaseModel):
    storyboard_id: Optional[str] = None
    storyboard_version: Optional[int] = None
    profile: dict[str, Any]


class AgentProposeBody(BaseModel):
    base_version: int = Field(ge=1)
    user_intent: str = Field(min_length=1, max_length=2000)
    permission_level: str = "L1"


class ApplyProposalBody(BaseModel):
    base_version: int = Field(ge=1)
    proposal_id: Optional[str] = None
    approval_token: Optional[str] = None
    operations: Optional[list[dict[str, Any]]] = None


class ValidateOpsBody(BaseModel):
    base_version: int = Field(ge=1)
    operations: list[dict[str, Any]]


class UndoBody(BaseModel):
    base_version: int = Field(ge=2)
    target_version: Optional[int] = None


class CreatePreviewBody(BaseModel):
    timeline_version: int = Field(ge=1)


class DeliveryBody(BaseModel):
    human_review_id: str
    destination_type: str = "signed_download"


class MotionTemplateCompileBody(BaseModel):
    motion_md: str = Field(min_length=1)
    brand_kit: Optional[dict[str, Any]] = None
    subtitle_track_id: Optional[str] = None
    target_clip_id: Optional[str] = None


class AnchoredReviewCommentBody(BaseModel):
    timeline_version: int = Field(ge=1)
    at_tick: int = Field(ge=0)
    comment: str = Field(min_length=1, max_length=2000)
    suggested_action: Optional[str] = None


class MotionOverlayBannerBody(BaseModel):
    project_id: str
    kind: str = "lower_third"
    title: str = Field(min_length=1, max_length=200)
    subtitle: Optional[str] = None
    brand_kit: Optional[dict[str, Any]] = None
    width: int = Field(default=480, ge=64, le=1920)
    height: int = Field(default=110, ge=32, le=1080)


class MediaGatewayProviderBody(BaseModel):
    provider: str
    api_key: str = Field(min_length=6)
    base_url: Optional[str] = None


class MediaGatewayGenerateBody(BaseModel):
    project_id: str
    modality: str = "image"
    prompt: str = Field(min_length=1, max_length=1000)
    provider: str = "local_deterministic"
    model_id: Optional[str] = None
    duration_sec: float = Field(default=2.0, ge=0.5, le=30.0)


class TenantPluginPolicyBody(BaseModel):
    enabled: Optional[bool] = None
    config: Optional[dict[str, Any]] = None


class HtmlGsapSceneBody(BaseModel):
    preset: str = "kinetic_title"
    headline: str = Field(min_length=1, max_length=200)
    subheadline: Optional[str] = None
    brand_kit: Optional[dict[str, Any]] = None
    width: int = Field(default=1080, ge=160, le=3840)
    height: int = Field(default=1920, ge=160, le=3840)
    fps: int = Field(default=30, ge=15, le=60)
    duration_sec: float = Field(default=3.0, ge=0.5, le=30.0)
    render_mode: str = "html_bundle"


def create_app(
    media_root: Optional[str] = None,
    database_url: Optional[str] = None,
    rate_limit_max: Optional[int] = None,
    rate_limit_window_sec: float = 60.0,
    max_upload_bytes: Optional[int] = None,
    plugins_dir: Optional[str] = None,
) -> FastAPI:
    root_dir = media_root or tempfile.mkdtemp(prefix="aivs_media_")
    store, storage_backend = create_tenant_store(media_root=root_dir, database_url=database_url)
    timeline_svc = TimelineService(store)
    review_svc = ReviewService(store)
    agent_harness = AgentHarness(timeline_svc)
    plugin_registry = PluginRegistry()
    plugin_registry.register(MotionTemplatePlugin())
    plugin_registry.register(AnchoredReviewPlugin())
    plugin_registry.register(MotionOverlayRendererPlugin())
    plugin_registry.register(MediaGatewayPlugin())
    plugin_registry.register(HtmlGsapMotionPlugin())
    effective_plugins_dir = plugins_dir if plugins_dir is not None else os.environ.get("AIVS_PLUGINS_DIR")
    if effective_plugins_dir:
        load_plugins_from_directory(effective_plugins_dir, plugin_registry)
    mcp_server = AIVSMCPServer(timeline_svc, agent_harness, store, plugin_registry=plugin_registry)
    media_adapter = OpenShortsAdapter()
    plan_compiler = RenderPlanCompiler()
    ffmpeg_renderer = FFmpegRendererAdapter()
    qa_engine = QAEngine(media_adapter)
    render_job_mgr = RenderJobManager(max_workers=2)


    app = FastAPI(title="AI Video Studio API", version="3.0.0")
    cors_env = os.environ.get("AIVS_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    metrics = MetricsRegistry()
    structured_logger = StructuredLogger()
    effective_rate_max = (
        rate_limit_max
        if rate_limit_max is not None
        else int(os.environ.get("AIVS_RATE_LIMIT_MAX", "300"))
    )
    effective_rate_window = float(
        os.environ.get("AIVS_RATE_LIMIT_WINDOW_SEC", str(rate_limit_window_sec))
    )
    rate_limiter = SlidingWindowRateLimiter(
        max_requests=effective_rate_max,
        window_seconds=effective_rate_window,
    )
    effective_max_upload = (
        max_upload_bytes
        if max_upload_bytes is not None
        else int(os.environ.get("AIVS_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
    )

    app.state.store = store
    app.state.storage_backend = storage_backend
    app.state.agent_harness = agent_harness
    app.state.metrics = metrics
    app.state.structured_logger = structured_logger
    app.state.rate_limiter = rate_limiter
    app.state.render_job_mgr = render_job_mgr
    app.state.plugin_registry = plugin_registry

    @app.middleware("http")
    async def observability_and_hardening_middleware(request: Request, call_next):
        start_ts = time.perf_counter()
        trace_id = resolve_trace_id(request.headers.get("X-Trace-Id"))
        tenant_id = request.headers.get("X-Tenant-Id") or ""
        path = request.url.path
        method = request.method

        def _attach_headers(resp, remaining: Optional[int] = None):
            resp.headers["X-Trace-Id"] = trace_id
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Referrer-Policy"] = "no-referrer"
            if path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"
            if remaining is not None:
                resp.headers["X-RateLimit-Remaining"] = str(remaining)
            return resp

        # 1) Upload / payload size guard
        content_length_hdr = request.headers.get("content-length")
        if content_length_hdr:
            try:
                content_length = int(content_length_hdr)
            except ValueError:
                content_length = 0
            if content_length > effective_max_upload:
                metrics.inc_upload_size_rejected()
                duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 3)
                metrics.observe_http_request(method, path, 413, duration_ms)
                structured_logger.log(
                    "WARNING",
                    "http.payload_too_large",
                    trace_id=trace_id,
                    tenant_id=tenant_id or None,
                    method=method,
                    path=path,
                    status_code=413,
                    content_length=content_length,
                    max_upload_bytes=effective_max_upload,
                    duration_ms=duration_ms,
                )
                return _attach_headers(
                    JSONResponse(
                        status_code=413,
                        content={
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": f"Request payload ({content_length} bytes) exceeds limit ({effective_max_upload} bytes)",
                            "details": {"max_upload_bytes": effective_max_upload},
                            "trace_id": trace_id,
                        },
                    )
                )

        # 2) Per-tenant sliding-window rate limiter (exempt /healthz and /metrics)
        remaining: Optional[int] = None
        if path not in ("/healthz", "/metrics"):
            rate_key = tenant_id or (request.client.host if request.client else "anonymous")
            allowed, remaining, retry_after = rate_limiter.check(rate_key)
            if not allowed:
                metrics.inc_rate_limit_rejected(rate_key)
                duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 3)
                metrics.observe_http_request(method, path, 429, duration_ms)
                structured_logger.log(
                    "WARNING",
                    "http.rate_limited",
                    trace_id=trace_id,
                    tenant_id=tenant_id or None,
                    method=method,
                    path=path,
                    status_code=429,
                    retry_after=retry_after,
                    duration_ms=duration_ms,
                )
                resp = JSONResponse(
                    status_code=429,
                    content={
                        "code": "RATE_LIMITED",
                        "message": "Too many requests for tenant window",
                        "details": {"retry_after_seconds": retry_after},
                        "trace_id": trace_id,
                    },
                )
                resp.headers["Retry-After"] = str(retry_after)
                return _attach_headers(resp, remaining=0)

        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 3)
        metrics.observe_http_request(method, path, response.status_code, duration_ms)
        structured_logger.log(
            "INFO",
            "http.request",
            trace_id=trace_id,
            tenant_id=tenant_id or None,
            method=method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return _attach_headers(response, remaining=remaining)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "3.0.0", "storage_backend": storage_backend}

    @app.get("/metrics")
    def prometheus_metrics():
        return PlainTextResponse(
            content=metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/v1/capabilities")
    def list_capabilities():
        return {
            "version": "3.0.0",
            "renderer": "ffmpeg",
            "storage_backend": storage_backend,
            "effects": DEFAULT_CAPABILITY_REGISTRY.list_effects(),
            "plugins": plugin_registry.list_plugins(),
            "llm_provider": {"provider": agent_harness._llm_provider.provider_name, "model_id": agent_harness._llm_provider.model_id},
        }


    def get_ctx(
        x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
        x_user_id: str = Header(..., alias="X-User-Id"),
        x_user_role: str = Header(..., alias="X-User-Role"),
        x_trace_id: Optional[str] = Header(None, alias="X-Trace-Id"),
    ) -> TenantContext:
        return TenantContext(
            tenant_id=x_tenant_id,
            user_id=x_user_id,
            role=x_user_role,
            trace_id=x_trace_id or str(uuid4()),
        )

    def get_idem_key(idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200)) -> str:
        return idempotency_key

    @app.post("/api/v1/mcp/rpc", status_code=200)
    async def mcp_rpc_endpoint(request: Request, ctx: TenantContext = Depends(get_ctx)):
        body = await request.json()
        return mcp_server.handle_rpc(ctx, body)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id") or str(uuid4())
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details, "trace_id": trace_id},
        )

    @app.post("/api/v1/workspaces", status_code=201)
    def create_workspace(body: CreateWorkspaceBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin"])
        cached = store.get_idempotent(ctx.tenant_id, "create_workspace", idem)
        if cached:
            return cached
        ws_id = str(uuid4())
        rec = {
            "id": ws_id,
            "tenant_id": ctx.tenant_id,
            "name": body.name,
            "status": "active",
            "hitl_policy": body.hitl_policy or {"default_mode": "copilot", "allow_auto_apply_low_risk": False},
        }
        store.put("workspaces", rec)
        store.add_audit(ctx, "user", ctx.user_id, "workspace.created", "workspace", ws_id)
        resp = {k: v for k, v in rec.items() if k != "tenant_id"}
        store.save_idempotent(ctx.tenant_id, "create_workspace", idem, resp)
        return resp

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: CreateProjectBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        cached = store.get_idempotent(ctx.tenant_id, "create_project", idem)
        if cached:
            return cached
        store.get(ctx, "workspaces", body.workspace_id)
        proj_id = str(uuid4())
        rec = {"id": proj_id, "tenant_id": ctx.tenant_id, "workspace_id": body.workspace_id, "name": body.name, "description": body.description, "status": "active"}
        store.put("projects", rec)
        resp = {k: v for k, v in rec.items() if k != "tenant_id"}
        store.save_idempotent(ctx.tenant_id, "create_project", idem, resp)
        return resp

    @app.get("/api/v1/projects/{project_id}")
    def get_project(project_id: str, ctx: TenantContext = Depends(get_ctx)):
        rec = store.get(ctx, "projects", project_id)
        return {k: v for k, v in rec.items() if k != "tenant_id"}

    @app.post("/api/v1/projects/{project_id}/upload-sessions", status_code=201)
    def create_upload_session(project_id: str, body: CreateUploadBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        store.get(ctx, "projects", project_id)
        sess_id = str(uuid4())
        storage_dir = Path(store.media_root) / ctx.tenant_id / project_id
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_key = str(storage_dir / f"{sess_id}_{body.filename}")
        rec = {
            "id": sess_id,
            "tenant_id": ctx.tenant_id,
            "project_id": project_id,
            "created_by": ctx.user_id,
            "idempotency_key": idem,
            "filename": body.filename,
            "kind": body.kind,
            "size_bytes": body.size_bytes,
            "storage_key": storage_key,
            "upload_url": f"file://{storage_key}",
            "multipart_upload_id": None,
            "expires_at": iso_now(),
        }
        store.put("upload_sessions", rec)
        return {k: rec[k] for k in ("id", "storage_key", "upload_url", "multipart_upload_id", "expires_at")}

    @app.put("/api/v1/upload-sessions/{session_id}/content", status_code=200)
    async def upload_session_content(session_id: str, request: Request, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        sess = store.get(ctx, "upload_sessions", session_id)
        data = await request.body()
        if not data:
            raise AppError(422, "VALIDATION_ERROR", "Empty upload body")
        Path(sess["storage_key"]).parent.mkdir(parents=True, exist_ok=True)
        Path(sess["storage_key"]).write_bytes(data)
        return {"session_id": session_id, "size_bytes": len(data), "checksum_sha256": file_sha256(sess["storage_key"])}

    @app.get("/api/v1/assets/{asset_id}/media")
    def stream_asset_media(asset_id: str, ctx: TenantContext = Depends(get_ctx)):
        asset = store.get(ctx, "assets", asset_id)
        if not os.path.exists(asset["storage_key"]):
            raise AppError(404, "NOT_FOUND", "Asset media file not found")
        return FileResponse(asset["storage_key"], media_type="video/mp4", filename=asset.get("original_filename", "asset.mp4"))

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str, ctx: TenantContext = Depends(get_ctx)):
        art = store.get(ctx, "artifacts", artifact_id)
        if not os.path.exists(art["storage_key"]):
            raise AppError(404, "NOT_FOUND", "Artifact file not found")
        return FileResponse(art["storage_key"], media_type="video/mp4", filename=f"export_{artifact_id[:8]}.mp4")

    @app.post("/api/v1/upload-sessions/{session_id}/complete", status_code=202)
    def complete_upload(session_id: str, body: CompleteUploadBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        sess = store.get(ctx, "upload_sessions", session_id)
        if not os.path.exists(sess["storage_key"]):
            raise AppError(422, "VALIDATION_ERROR", "Uploaded object not found in storage")
        actual_hash = file_sha256(sess["storage_key"])
        if actual_hash != body.checksum_sha256:
            raise AppError(422, "VALIDATION_ERROR", "Uploaded file SHA-256 checksum mismatch")

        probe = media_adapter.probe_asset(sess["storage_key"])
        asset_id = str(uuid4())
        asset_rec = {
            "id": asset_id,
            "tenant_id": ctx.tenant_id,
            "project_id": sess["project_id"],
            "upload_session_id": session_id,
            "kind": sess["kind"],
            "status": "ready",
            "original_filename": sess["filename"],
            "storage_key": sess["storage_key"],
            "checksum_sha256": actual_hash,
            "size_bytes": os.path.getsize(sess["storage_key"]),
            "duration_num": probe.duration_num,
            "duration_den": probe.duration_den,
            "is_vfr": probe.is_vfr,
            "probe_summary": {
                "width": probe.width,
                "height": probe.height,
                "frame_rate": {"num": probe.frame_rate_num, "den": probe.frame_rate_den},
                "container_format": probe.container_format,
            },
            "created_at": iso_now(),
        }
        validate_against_schema("asset.schema.json", asset_rec)
        asset_rec["created_by"] = ctx.user_id
        store.put("assets", asset_rec)
        store.put("media_probes", {"id": str(uuid4()), "tenant_id": ctx.tenant_id, "asset_id": asset_id, "probe": probe.__dict__})
        return {"asset_id": asset_id, "task_id": str(uuid4()), "status": "queued", "trace_id": ctx.trace_id}

    @app.post("/api/v1/assets/{asset_id}/analyze", status_code=202)
    def analyze_asset(asset_id: str, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        asset = store.get(ctx, "assets", asset_id)
        probe = media_adapter.probe_asset(asset["storage_key"])
        proxy_out = media_adapter.generate_proxy_and_thumbnail(asset["storage_key"], str(Path(asset["storage_key"]).parent / f"derived_{asset_id}"))
        analysis = media_adapter.analyze_media(probe, asset_id, media_path=asset["storage_key"])
        run_id = str(uuid4())
        store.put("analysis_runs", {
            "id": run_id,
            "tenant_id": ctx.tenant_id,
            "asset_id": asset_id,
            "run_type": "full_pipeline",
            "status": "succeeded",
            "pipeline_version": "2.2",
            "derived": proxy_out,
            "analysis": analysis,
        })
        asset["status"] = "analyzed"
        store.put("assets", asset)
        return {"task_id": run_id, "status": "queued", "trace_id": ctx.trace_id}

    @app.get("/api/v1/assets/{asset_id}/analysis")
    def get_asset_analysis(asset_id: str, ctx: TenantContext = Depends(get_ctx)):
        store.get(ctx, "assets", asset_id)
        runs = store.query(ctx, "analysis_runs", asset_id=asset_id)
        latest = runs[-1]["analysis"] if runs else {"scenes": [], "semantic_segments": [], "highlight_candidates": []}
        return {
            "asset_id": asset_id,
            "runs": [{"id": r["id"], "run_type": r["run_type"], "status": r["status"], "pipeline_version": r["pipeline_version"]} for r in runs],
            "scenes_count": len(latest["scenes"]),
            "segments_count": len(latest["semantic_segments"]),
            "candidates_count": len(latest["highlight_candidates"]),
            "derived": runs[-1]["derived"] if runs else {},
        }

    @app.post("/api/v1/projects/{project_id}/briefs", status_code=201)
    def create_brief(project_id: str, body: CreateBriefBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        store.get(ctx, "projects", project_id)
        brief_doc = {"schema_version": "2.0", **body.brief}
        validate_against_schema("creative-brief.schema.json", brief_doc)
        brief_id = str(uuid4())
        bv_id = str(uuid4())
        chk = canonical_sha256(brief_doc)
        store.put("creative_brief_versions", {"id": bv_id, "tenant_id": ctx.tenant_id, "brief_id": brief_id, "project_id": project_id, "version": 1, "brief": brief_doc, "brief_checksum": chk})
        return {"brief_id": brief_id, "brief_version_id": bv_id, "version": 1, "brief_checksum": chk, "brief": brief_doc}

    @app.post("/api/v1/projects/{project_id}/storyboards", status_code=202)
    def propose_storyboard(project_id: str, body: ProposeStoryboardBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        store.get(ctx, "projects", project_id)
        store.get(ctx, "creative_brief_versions", body.brief_version_id)
        asset = store.get(ctx, "assets", body.asset_id)
        runs = store.query(ctx, "analysis_runs", asset_id=body.asset_id)
        run_ids = [r["id"] for r in runs] or [str(uuid4())]

        sb_id = str(uuid4())
        dim = {"score": 85.0, "confidence": 0.9, "evidence": [f"asset:{body.asset_id}"], "rationale": "Grounded in probed source clip"}
        proposal_doc = {
            "schema_version": "2.0",
            "storyboard_id": sb_id,
            "version": 1,
            "brief_version_id": body.brief_version_id,
            "source_analysis_run_ids": run_ids,
            "ordered_segments": [{
                "segment_id": "seg-1",
                "asset_id": body.asset_id,
                "source_in_tick": 0,
                "source_out_tick": asset["duration_num"],
                "role_in_arc": "hook",
                "rationale": "Opening high-clarity segment",
            }],
            "hook": "Verified opening hook",
            "narrative_arc": "Hook -> Core message",
            "estimated_duration_ticks": asset["duration_num"],
            "score_breakdown": {
                k: dim for k in [
                    "hook_strength", "information_value", "emotional_intensity",
                    "story_completeness", "visual_quality", "audio_quality",
                    "context_dependency", "brand_safety", "duration_fit", "audience_relevance",
                ]
            },
            "evidence_refs": [f"asset:{body.asset_id}"],
            "confidence": 0.88,
            "model_lineage": {"provider": "local-stub", "model_id": "sprint1-deterministic", "prompt_version": "v1"},
        }
        validate_against_schema("storyboard.schema.json", proposal_doc)
        chk = canonical_sha256(proposal_doc)
        store.put("storyboards", {"id": sb_id, "tenant_id": ctx.tenant_id, "project_id": project_id, "brief_version_id": body.brief_version_id, "current_version": 1, "status": "proposed"})
        store.put("storyboard_versions", {"id": str(uuid4()), "tenant_id": ctx.tenant_id, "storyboard_id": sb_id, "version": 1, "proposal_json": proposal_doc, "proposal_checksum": chk})
        return {"task_id": str(uuid4()), "storyboard_id": sb_id, "version": 1, "proposal_checksum": chk, "status": "queued", "trace_id": ctx.trace_id}

    @app.post("/api/v1/storyboards/{storyboard_id}/reviews", status_code=201)
    def review_storyboard(storyboard_id: str, body: ReviewBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return review_svc.submit_review(ctx, "storyboard", storyboard_id, body.subject_version, body.subject_checksum, body.decision, idem, body.comment, body.selected_items, body.rejected_items)

    @app.post("/api/v1/projects/{project_id}/timelines", status_code=201)
    def create_timeline(project_id: str, body: CreateTimelineBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return timeline_svc.create_timeline(ctx, project_id, body.profile, idem, body.storyboard_id, body.storyboard_version)

    @app.get("/api/v1/timelines/{timeline_id}")
    def get_timeline(timeline_id: str, version: Optional[int] = None, ctx: TenantContext = Depends(get_ctx)):
        return timeline_svc.get_timeline(ctx, timeline_id, version=version)

    @app.get("/api/v1/timelines/{timeline_id}/export/otio")
    def export_otio(timeline_id: str, version: Optional[int] = None, ctx: TenantContext = Depends(get_ctx)):
        tl = store.get(ctx, "timelines", timeline_id)
        ver = version or tl["current_version"]
        rows = store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=ver)
        if not rows:
            raise AppError(404, "NOT_FOUND", f"Timeline version {ver} not found")
        return export_timeline_to_otio(rows[0]["snapshot_json"], name=tl.get("name", "AI Video Studio Timeline"))

    @app.get("/api/v1/timelines/{timeline_id}/versions")
    def list_timeline_versions(timeline_id: str, ctx: TenantContext = Depends(get_ctx)):
        return timeline_svc.list_versions(ctx, timeline_id)

    @app.post("/api/v1/timelines/{timeline_id}/agent-proposals", status_code=200)
    def agent_propose(timeline_id: str, body: AgentProposeBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return agent_harness.propose_edit_operations(ctx, timeline_id, body.base_version, body.user_intent, idem, permission_level=body.permission_level)

    @app.post("/api/v1/proposals/{proposal_id}/reviews", status_code=201)
    def review_proposal(proposal_id: str, body: ReviewBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return review_svc.submit_review(ctx, "edit_proposal", proposal_id, body.subject_version, body.subject_checksum, body.decision, idem, body.comment, body.selected_items, body.rejected_items)

    @app.post("/api/v1/timelines/{timeline_id}/validate", status_code=200)
    def validate_timeline_ops(timeline_id: str, body: ValidateOpsBody, ctx: TenantContext = Depends(get_ctx)):
        return timeline_svc.validate_operations(ctx, timeline_id, body.base_version, body.operations)

    @app.post("/api/v1/timelines/{timeline_id}/operations", status_code=200)
    def apply_operations(timeline_id: str, body: ApplyProposalBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        if body.proposal_id and body.approval_token:
            return timeline_svc.apply_approved_proposal(ctx, timeline_id, body.base_version, body.proposal_id, body.approval_token, idem)
        if body.operations:
            return timeline_svc.apply_human_operations(ctx, timeline_id, body.base_version, body.operations, idem)
        raise AppError(422, "VALIDATION_ERROR", "Provide either (proposal_id + approval_token) or direct human operations[]")

    @app.post("/api/v1/timelines/{timeline_id}/undo", status_code=200)
    def undo_timeline(timeline_id: str, body: UndoBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return timeline_svc.undo(ctx, timeline_id, body.base_version, idem, body.target_version)

    @app.post("/api/v1/timelines/{timeline_id}/redo", status_code=200)
    def redo_timeline(timeline_id: str, body: UndoBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return timeline_svc.redo(ctx, timeline_id, body.base_version, idem, body.target_version)

    @app.post("/api/v1/timelines/{timeline_id}/renders", status_code=202)
    def create_final_render(timeline_id: str, body: CreatePreviewBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        tl = store.get(ctx, "timelines", timeline_id)
        vrows = store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=body.timeline_version)
        if not vrows:
            raise AppError(404, "NOT_FOUND", "Timeline version not found")
        snap = vrows[0]["snapshot_json"]
        assets = {a["id"]: a for a in store.query(ctx, "assets", project_id=tl["project_id"])}
        plan, plan_checksum = plan_compiler.compile(snap, assets, render_mode="final")
        plan_id = str(uuid4())
        store.put("render_plans", {"id": plan_id, "tenant_id": ctx.tenant_id, "project_id": tl["project_id"], "timeline_id": timeline_id, "timeline_version": body.timeline_version, "plan_json": plan, "plan_checksum": plan_checksum})
        out_path = str(Path(store.media_root) / ctx.tenant_id / tl["project_id"] / f"final_{plan_id}.mp4")
        rendered = ffmpeg_renderer.render(plan, out_path)
        art_id = str(uuid4())
        art_rec = {
            "id": art_id, "tenant_id": ctx.tenant_id, "project_id": tl["project_id"],
            "render_job_id": None, "timeline_id": timeline_id, "timeline_version": body.timeline_version,
            "artifact_type": "final_video", "storage_key": rendered["output_path"],
            "checksum_sha256": rendered["checksum_sha256"], "size_bytes": rendered["size_bytes"],
            "status": "ready", "created_by_lease_token": 1,
            "metadata": {"render_plan_id": plan_id, "encoder_used": rendered["encoder_used"]},
            "created_at": iso_now(),
        }
        validate_against_schema("artifact.schema.json", art_rec)
        store.put("artifacts", art_rec)
        return {"task_id": str(uuid4()), "render_plan_id": plan_id, "artifact_id": art_id, "artifact_checksum": rendered["checksum_sha256"], "status": "queued", "trace_id": ctx.trace_id}

    @app.post("/api/v1/timelines/{timeline_id}/previews", status_code=202)
    def create_preview(timeline_id: str, body: CreatePreviewBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        tl = store.get(ctx, "timelines", timeline_id)
        vrows = store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=body.timeline_version)
        if not vrows:
            raise AppError(404, "NOT_FOUND", "Timeline version not found")
        snap = vrows[0]["snapshot_json"]
        assets = {a["id"]: a for a in store.query(ctx, "assets", project_id=tl["project_id"])}
        plan, plan_checksum = plan_compiler.compile(snap, assets, render_mode="preview")

        plan_id = str(uuid4())
        store.put("render_plans", {"id": plan_id, "tenant_id": ctx.tenant_id, "project_id": tl["project_id"], "timeline_id": timeline_id, "timeline_version": body.timeline_version, "plan_json": plan, "plan_checksum": plan_checksum})

        out_path = str(Path(store.media_root) / ctx.tenant_id / tl["project_id"] / f"preview_{plan_id}.mp4")
        rendered = ffmpeg_renderer.render(plan, out_path)

        art_id = str(uuid4())
        art_rec = {
            "id": art_id,
            "tenant_id": ctx.tenant_id,
            "project_id": tl["project_id"],
            "render_job_id": None,
            "timeline_id": timeline_id,
            "timeline_version": body.timeline_version,
            "artifact_type": "preview_video",
            "storage_key": rendered["output_path"],
            "checksum_sha256": rendered["checksum_sha256"],
            "size_bytes": rendered["size_bytes"],
            "status": "ready",
            "created_by_lease_token": 1,
            "metadata": {"render_plan_id": plan_id, "encoder_used": rendered["encoder_used"]},
            "created_at": iso_now(),
        }
        validate_against_schema("artifact.schema.json", art_rec)
        store.put("artifacts", art_rec)
        return {"task_id": str(uuid4()), "render_plan_id": plan_id, "artifact_id": art_id, "artifact_checksum": rendered["checksum_sha256"], "status": "queued", "trace_id": ctx.trace_id}

    @app.post("/api/v1/artifacts/{artifact_id}/visual-inspect", status_code=200)
    def visual_inspect_artifact(artifact_id: str, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor", "reviewer"])
        art = store.get(ctx, "artifacts", artifact_id)
        snap_dir = str(Path(art["storage_key"]).parent / f"vis_{artifact_id[:8]}")
        return agent_harness.inspect_rendered_frames(ctx, art["storage_key"], save_dir=snap_dir)

    @app.post("/api/v1/artifacts/{artifact_id}/qa", status_code=202)
    def run_qa(artifact_id: str, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor", "reviewer"])
        art = store.get(ctx, "artifacts", artifact_id)
        art_meta = art.get("metadata") or art.get("metadata_json") or {}
        plan_rec = store.get(ctx, "render_plans", art_meta["render_plan_id"])
        report = qa_engine.run_qa(artifact_id, art["storage_key"], art["checksum_sha256"], plan_rec["plan_json"])
        store.put("qa_reports", {"id": report["qa_report_id"], "tenant_id": ctx.tenant_id, "project_id": art["project_id"], **report})
        return {"task_id": str(uuid4()), "qa_report_id": report["qa_report_id"], "qa_status": report["status"], "status": "queued", "trace_id": ctx.trace_id}

    @app.get("/api/v1/artifacts/{artifact_id}/qa")
    def get_qa(artifact_id: str, ctx: TenantContext = Depends(get_ctx)):
        store.get(ctx, "artifacts", artifact_id)
        rows = store.query(ctx, "qa_reports", artifact_id=artifact_id)
        if not rows:
            raise AppError(404, "NOT_FOUND", "QA report not found")
        r = rows[-1]
        return {k: r[k] for k in ("qa_report_id", "artifact_id", "status", "blockers", "warnings", "completed_at")}

    @app.post("/api/v1/artifacts/{artifact_id}/reviews", status_code=201)
    def review_artifact(artifact_id: str, body: ReviewBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return review_svc.submit_review(ctx, "render_artifact", artifact_id, body.subject_version, body.subject_checksum, body.decision, idem, body.comment, body.selected_items, body.rejected_items, body.qa_report_id)

    @app.post("/api/v1/artifacts/{artifact_id}/deliveries", status_code=202)
    def create_delivery(artifact_id: str, body: DeliveryBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin"])
        art = store.get(ctx, "artifacts", artifact_id)
        rev = store.get(ctx, "human_reviews", body.human_review_id)
        if rev["status"] not in ("approved", "approved_with_warnings") or rev["subject_checksum"] != art["checksum_sha256"]:
            raise AppError(409, "APPROVAL_REQUIRED", "Current valid human approval matching artifact checksum is required")
        deliv_id = str(uuid4())
        deliv_doc = {
            "id": deliv_id,
            "tenant_id": ctx.tenant_id,
            "project_id": art["project_id"],
            "artifact_id": artifact_id,
            "artifact_checksum": art["checksum_sha256"],
            "qa_report_id": rev["qa_report_id"],
            "human_review_id": rev["id"],
            "destination_type": body.destination_type,
            "status": "ready",
            "idempotency_key": idem,
            "delivered_at": None,
            "created_at": iso_now(),
        }
        validate_against_schema("delivery.schema.json", deliv_doc)
        store.put("deliveries", deliv_doc)
        return {"task_id": str(uuid4()), "delivery_id": deliv_id, "status": "queued", "trace_id": ctx.trace_id}

    @app.post("/api/v1/timelines/{timeline_id}/render-jobs", status_code=202)
    def create_async_render_job(
        timeline_id: str,
        body: CreatePreviewBody,
        ctx: TenantContext = Depends(get_ctx),
        idem: str = Depends(get_idem_key),
    ):
        require_role(ctx, ["owner", "admin", "editor"])
        tl = store.get(ctx, "timelines", timeline_id)
        vrows = store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=body.timeline_version)
        if not vrows:
            raise AppError(404, "NOT_FOUND", f"Timeline version {body.timeline_version} not found")
        snap = vrows[0]["snapshot_json"]
        assets_map = {a["id"]: a for a in store.query(ctx, "assets", project_id=tl["project_id"])}

        def _execute_render() -> dict[str, Any]:
            plan, plan_checksum = plan_compiler.compile(snap, assets_map, render_mode="final")
            plan_id = str(uuid4())
            store.put(
                "render_plans",
                {
                    "id": plan_id,
                    "tenant_id": ctx.tenant_id,
                    "project_id": tl["project_id"],
                    "timeline_id": timeline_id,
                    "timeline_version": body.timeline_version,
                    "plan_checksum": plan_checksum,
                    "plan_json": plan,
                },
            )
            out_path = str(Path(store.media_root) / ctx.tenant_id / tl["project_id"] / f"async_render_{plan_id}.mp4")
            rendered = ffmpeg_renderer.render(plan, out_path)
            art_id = str(uuid4())
            art_rec = {
                "id": art_id,
                "tenant_id": ctx.tenant_id,
                "project_id": tl["project_id"],
                "render_job_id": None,
                "timeline_id": timeline_id,
                "timeline_version": body.timeline_version,
                "artifact_type": "final_video",
                "storage_key": rendered["output_path"],
                "checksum_sha256": rendered["checksum_sha256"],
                "size_bytes": rendered["size_bytes"],
                "status": "ready",
                "created_by_lease_token": 1,
                "metadata": {
                    "render_plan_id": plan_id,
                    "encoder_used": rendered["encoder_used"],
                },
                "created_at": iso_now(),
            }
            validate_against_schema("artifact.schema.json", art_rec)
            store.put("artifacts", art_rec)
            report = qa_engine.run_qa(art_id, art_rec["storage_key"], art_rec["checksum_sha256"], plan)
            store.put("qa_reports", {"id": report["qa_report_id"], "tenant_id": ctx.tenant_id, "project_id": tl["project_id"], **report})
            return {"render_plan_id": plan_id, "artifact_id": art_id, "qa_report_id": report["qa_report_id"]}

        job = render_job_mgr.submit_job(ctx, timeline_id, body.timeline_version, "final", _execute_render)
        return {**job, "trace_id": ctx.trace_id}

    @app.get("/api/v1/render-jobs/{job_id}")
    def get_async_render_job(job_id: str, ctx: TenantContext = Depends(get_ctx)):
        return render_job_mgr.get_job(ctx, job_id)

    @app.post("/api/v1/render-jobs/{job_id}/cancel")
    def cancel_async_render_job(job_id: str, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor"])
        return render_job_mgr.cancel_job(ctx, job_id)

    @app.get("/api/v1/plugins")
    def list_plugins(category: Optional[str] = None):
        return {"plugins": plugin_registry.list_plugins(category=category)}

    @app.post("/api/v1/plugins/{plugin_id}/enable")
    def enable_plugin(plugin_id: str, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin"])
        return plugin_registry.enable(plugin_id)

    @app.post("/api/v1/plugins/{plugin_id}/disable")
    def disable_plugin(plugin_id: str, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin"])
        return plugin_registry.disable(plugin_id)

    @app.get("/api/v1/tenant/plugins")
    def list_tenant_plugins(category: Optional[str] = None, ctx: TenantContext = Depends(get_ctx)):
        return {"plugins": plugin_registry.list_plugins_for_tenant(ctx.tenant_id, category=category)}

    @app.put("/api/v1/tenant/plugins/{plugin_id}")
    def update_tenant_plugin_policy(
        plugin_id: str,
        body: TenantPluginPolicyBody,
        ctx: TenantContext = Depends(get_ctx),
    ):
        require_role(ctx, ["owner", "admin"])
        res = plugin_registry.set_tenant_policy(ctx.tenant_id, plugin_id, enabled=body.enabled, config=body.config)
        store.add_audit(ctx, "user", ctx.user_id, "plugin.tenant_policy_updated", "plugin", plugin_id)
        return res

    @app.post("/api/v1/plugins/motion-template/compile")
    def compile_motion_template(body: MotionTemplateCompileBody, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        plugin, tcfg = plugin_registry.require_enabled_for_tenant(MotionTemplatePlugin.PLUGIN_ID, ctx.tenant_id)
        merged_brand_kit = {**(tcfg.get("brand_kit") or {}), **(body.brand_kit or {})}
        return plugin.compile_to_operations(  # type: ignore
            body.motion_md,
            brand_kit=merged_brand_kit or None,
            subtitle_track_id=body.subtitle_track_id,
            target_clip_id=body.target_clip_id,
        )

    @app.post("/api/v1/timelines/{timeline_id}/review-comments", status_code=201)
    def add_anchored_review_comment(
        timeline_id: str,
        body: AnchoredReviewCommentBody,
        ctx: TenantContext = Depends(get_ctx),
    ):
        require_role(ctx, ["owner", "admin", "editor", "reviewer"])
        store.get(ctx, "timelines", timeline_id)
        plugin, _ = plugin_registry.require_enabled_for_tenant(AnchoredReviewPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.add_comment(ctx, timeline_id, body.timeline_version, body.at_tick, body.comment, body.suggested_action)

    @app.get("/api/v1/timelines/{timeline_id}/review-comments")
    def list_anchored_review_comments(timeline_id: str, ctx: TenantContext = Depends(get_ctx)):
        store.get(ctx, "timelines", timeline_id)
        plugin, _ = plugin_registry.require_enabled_for_tenant(AnchoredReviewPlugin.PLUGIN_ID, ctx.tenant_id)
        return {"comments": plugin.list_comments(ctx, timeline_id)}

    @app.post("/api/v1/review-comments/{comment_id}/resolve")
    def resolve_anchored_review_comment(comment_id: str, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor", "reviewer"])
        plugin, _ = plugin_registry.require_enabled_for_tenant(AnchoredReviewPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.resolve_comment(ctx, comment_id)

    @app.post("/api/v1/plugins/motion-overlay/render-banner", status_code=201)
    def render_motion_overlay_banner(body: MotionOverlayBannerBody, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        store.get(ctx, "projects", body.project_id)
        plugin, _tcfg = plugin_registry.require_enabled_for_tenant(MotionOverlayRendererPlugin.PLUGIN_ID, ctx.tenant_id)
        asset_id = str(uuid4())
        out_png = str(Path(store.media_root) / ctx.tenant_id / body.project_id / f"overlay_{asset_id}.png")
        rendered = plugin.render_banner_png(
            kind=body.kind,
            title=body.title,
            output_path=out_png,
            subtitle=body.subtitle,
            brand_kit=body.brand_kit,
            width=body.width,
            height=body.height,
        )
        asset_rec = {
            "id": asset_id,
            "tenant_id": ctx.tenant_id,
            "project_id": body.project_id,
            "kind": "image",
            "filename": f"{body.kind}_{asset_id[:8]}.png",
            "storage_key": rendered["output_path"],
            "checksum_sha256": file_sha256(rendered["output_path"]),
            "size_bytes": rendered["size_bytes"],
            "status": "ready",
            "created_at": iso_now(),
        }
        store.put("assets", asset_rec)
        return {"asset_id": asset_id, **rendered}

    @app.get("/api/v1/plugins/media-gateway/models")
    def list_media_gateway_models(ctx: TenantContext = Depends(get_ctx)):
        plugin, _ = plugin_registry.require_enabled_for_tenant(MediaGatewayPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.list_catalog(ctx)

    @app.post("/api/v1/plugins/media-gateway/providers")
    def configure_media_gateway_provider(body: MediaGatewayProviderBody, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin"])
        plugin, _ = plugin_registry.require_enabled_for_tenant(MediaGatewayPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.configure_provider(ctx, body.provider, body.api_key, body.base_url)

    @app.post("/api/v1/plugins/media-gateway/generate", status_code=201)
    def generate_media_candidate(body: MediaGatewayGenerateBody, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        store.get(ctx, "projects", body.project_id)
        plugin, _ = plugin_registry.require_enabled_for_tenant(MediaGatewayPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.generate_candidate(
            ctx,
            project_id=body.project_id,
            modality=body.modality,
            prompt=body.prompt,
            output_dir=store.media_root,
            provider=body.provider,
            model_id=body.model_id,
            duration_sec=body.duration_sec,
        )

    @app.post("/api/v1/plugins/media-gateway/candidates/{candidate_id}/adopt")
    def adopt_media_candidate(candidate_id: str, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        plugin, _ = plugin_registry.require_enabled_for_tenant(MediaGatewayPlugin.PLUGIN_ID, ctx.tenant_id)
        adopted = plugin.adopt_candidate(ctx, candidate_id)
        asset_id = adopted["adopted_asset_id"]
        asset_rec = {
            "id": asset_id,
            "tenant_id": ctx.tenant_id,
            "project_id": adopted["project_id"],
            "kind": adopted["asset_kind"],
            "filename": adopted["filename"],
            "storage_key": adopted["storage_key"],
            "checksum_sha256": adopted["checksum_sha256"],
            "size_bytes": adopted["size_bytes"],
            "status": "ready",
            "created_at": iso_now(),
        }
        store.put("assets", asset_rec)
        return {
            "candidate_id": candidate_id,
            "asset_id": asset_id,
            "kind": adopted["asset_kind"],
            "filename": adopted["filename"],
            "checksum_sha256": adopted["checksum_sha256"],
        }

    @app.get("/api/v1/plugins/html-gsap/runtime")
    def html_gsap_runtime(ctx: TenantContext = Depends(get_ctx)):
        plugin, _ = plugin_registry.require_enabled_for_tenant(HtmlGsapMotionPlugin.PLUGIN_ID, ctx.tenant_id)
        return plugin.runtime_info()  # type: ignore

    @app.post("/api/v1/plugins/html-gsap/build-scene", status_code=201)
    def html_gsap_build_scene(body: HtmlGsapSceneBody, ctx: TenantContext = Depends(get_ctx)):
        require_role(ctx, ["owner", "admin", "editor"])
        plugin, tcfg = plugin_registry.require_enabled_for_tenant(HtmlGsapMotionPlugin.PLUGIN_ID, ctx.tenant_id)
        merged_kit = {**(tcfg.get("brand_kit") or {}), **(body.brand_kit or {})}
        out_dir = str(Path(store.media_root) / ctx.tenant_id / "gsap_scenes")
        return plugin.build_scene(  # type: ignore
            preset=body.preset,
            headline=body.headline,
            output_dir=out_dir,
            subheadline=body.subheadline,
            brand_kit=merged_kit or None,
            width=body.width,
            height=body.height,
            fps=body.fps,
            duration_sec=body.duration_sec,
            render_mode=body.render_mode,
        )

    return app


app = create_app()
