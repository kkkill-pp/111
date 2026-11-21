"""AI Video Studio FastAPI Application — Sprint 1 Vertical Slice (HITL v2.2)."""
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.agent_runtime.harness import AgentHarness
from services.application.review_service import ReviewService
from services.application.timeline_service import TimelineService
from services.infrastructure.contracts import canonical_sha256, validate_against_schema
from services.infrastructure.errors import AppError
from services.infrastructure.pg_store import create_tenant_store
from services.infrastructure.store import TenantContext, iso_now, require_role
from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler
from services.media_engine.qa_engine import QAEngine


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


def create_app(media_root: Optional[str] = None, database_url: Optional[str] = None) -> FastAPI:
    root_dir = media_root or tempfile.mkdtemp(prefix="aivs_media_")
    store, storage_backend = create_tenant_store(media_root=root_dir, database_url=database_url)
    timeline_svc = TimelineService(store)
    review_svc = ReviewService(store)
    agent_harness = AgentHarness(timeline_svc)
    media_adapter = OpenShortsAdapter()
    plan_compiler = RenderPlanCompiler()
    ffmpeg_renderer = FFmpegRendererAdapter()
    qa_engine = QAEngine(media_adapter)

    app = FastAPI(title="AI Video Studio API", version="3.0.0")
    app.state.store = store
    app.state.storage_backend = storage_backend
    app.state.agent_harness = agent_harness

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": "3.0.0", "storage_backend": storage_backend}

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

    @app.get("/api/v1/timelines/{timeline_id}/versions")
    def list_timeline_versions(timeline_id: str, ctx: TenantContext = Depends(get_ctx)):
        return timeline_svc.list_versions(ctx, timeline_id)

    @app.post("/api/v1/timelines/{timeline_id}/agent-proposals", status_code=200)
    def agent_propose(timeline_id: str, body: AgentProposeBody, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        return agent_harness.propose_edit_operations(ctx, timeline_id, body.base_version, body.user_intent, idem)

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

    @app.post("/api/v1/artifacts/{artifact_id}/qa", status_code=202)
    def run_qa(artifact_id: str, ctx: TenantContext = Depends(get_ctx), idem: str = Depends(get_idem_key)):
        require_role(ctx, ["owner", "admin", "editor", "reviewer"])
        art = store.get(ctx, "artifacts", artifact_id)
        plan_rec = store.get(ctx, "render_plans", art["metadata"]["render_plan_id"])
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

    return app


app = create_app()
