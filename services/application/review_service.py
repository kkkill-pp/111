"""Unified Human Review Service (/storyboards|proposals|artifacts/{id}/reviews)."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from services.infrastructure.contracts import validate_against_schema
from services.infrastructure.errors import AppError
from services.infrastructure.store import InMemoryTenantStore, TenantContext, iso_now, require_role

DECISION_TO_STATUS = {
    "approve": "approved",
    "approve_with_warnings": "approved_with_warnings",
    "reject": "rejected",
    "request_changes": "changes_requested",
}


class ReviewService:
    def __init__(self, store: InMemoryTenantStore) -> None:
        self.store = store

    def submit_review(
        self,
        ctx: TenantContext,
        subject_type: str,
        subject_id: str,
        subject_version: str,
        subject_checksum: str,
        decision: str,
        idempotency_key: str,
        comment: str | None = None,
        selected_items: list[str] | None = None,
        rejected_items: list[str] | None = None,
        qa_report_id: str | None = None,
    ) -> dict[str, Any]:
        allowed = ["owner", "admin", "reviewer"] if subject_type != "edit_proposal" else ["owner", "admin", "reviewer", "editor"]
        require_role(ctx, allowed)

        scope = f"review:{subject_type}:{subject_id}"
        cached = self.store.get_idempotent(ctx.tenant_id, scope, idempotency_key)
        if cached:
            return cached

        if decision not in DECISION_TO_STATUS:
            raise AppError(422, "VALIDATION_ERROR", f"Invalid decision: {decision}")

        project_id = ""
        approval_token: str | None = None

        if subject_type == "storyboard":
            sb = self.store.get(ctx, "storyboards", subject_id)
            project_id = sb["project_id"]
            sb_vers = self.store.query(ctx, "storyboard_versions", storyboard_id=subject_id, version=int(subject_version))
            if not sb_vers or sb_vers[0]["proposal_checksum"] != subject_checksum:
                raise AppError(409, "STALE_SUBJECT_CHECKSUM", "Storyboard version or checksum mismatch")
            sb["status"] = "accepted" if decision in ("approve", "approve_with_warnings") else ("rejected" if decision == "reject" else "revision_requested")
            self.store.put("storyboards", sb)

        elif subject_type == "edit_proposal":
            prop = self.store.get(ctx, "edit_proposals", subject_id)
            project_id = prop["project_id"]
            tl = self.store.get(ctx, "timelines", prop["timeline_id"])
            if str(prop["base_version"]) != str(subject_version) or prop["proposal_checksum"] != subject_checksum:
                raise AppError(409, "STALE_SUBJECT_CHECKSUM", "Proposal version or checksum mismatch")
            if tl["current_version"] != prop["base_version"]:
                prop["status"] = "superseded"
                self.store.put("edit_proposals", prop)
                raise AppError(409, "VERSION_CONFLICT", "Timeline has advanced; proposal is superseded")

            all_op_ids = [op["operation_id"] for op in prop["operations"]]
            sel = selected_items if selected_items else all_op_ids
            if not set(sel).issubset(set(all_op_ids)):
                raise AppError(422, "VALIDATION_ERROR", "selected_items contains unknown operation_id")
            prop["selected_operation_ids"] = sel
            prop["status"] = "approved" if decision in ("approve", "approve_with_warnings") else "rejected"
            self.store.put("edit_proposals", prop)

        elif subject_type == "render_artifact":
            art = self.store.get(ctx, "artifacts", subject_id)
            project_id = art["project_id"]
            if art["checksum_sha256"] != subject_checksum:
                raise AppError(409, "STALE_SUBJECT_CHECKSUM", "Artifact checksum changed; refresh before deciding")
            qa_rows = self.store.query(ctx, "qa_reports", artifact_id=subject_id)
            if not qa_rows:
                raise AppError(409, "QA_BLOCKER_PRESENT", "QA report required before approving artifact")
            latest_qa = qa_rows[-1]
            qa_report_id = qa_report_id or latest_qa["id"]
            if latest_qa["status"] == "failed" and decision in ("approve", "approve_with_warnings"):
                raise AppError(409, "QA_BLOCKER_PRESENT", "Cannot approve artifact when QA status is failed")
        else:
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported subject_type: {subject_type}")

        review_id = str(uuid4())
        now = iso_now()
        review_doc = {
            "review_id": review_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "subject_version": str(subject_version),
            "subject_checksum": subject_checksum,
            "qa_report_id": qa_report_id,
            "status": DECISION_TO_STATUS[decision],
            "decision": decision,
            "reviewer_id": ctx.user_id,
            "review_reason": None,
            "comment": comment,
            "selected_items": selected_items or [],
            "rejected_items": rejected_items or [],
            "expires_at": None,
            "decided_at": now,
            "created_at": now,
        }
        validate_against_schema("human-review.schema.json", review_doc)

        stored = {**review_doc, "id": review_id, "tenant_id": ctx.tenant_id, "project_id": project_id}
        self.store.put("human_reviews", stored)

        if subject_type == "edit_proposal" and decision in ("approve", "approve_with_warnings"):
            approval_token = self.store.issue_approval_token(
                ctx=ctx,
                human_review_id=review_id,
                subject_type="edit_proposal",
                subject_id=subject_id,
                bound_version=str(subject_version),
                bound_checksum=subject_checksum,
            )

        self.store.add_audit(
            ctx,
            "user",
            ctx.user_id,
            f"review.{DECISION_TO_STATUS[decision]}",
            subject_type,
            subject_id,
            {"review_id": review_id, "subject_version": subject_version, "subject_checksum": subject_checksum},
        )

        api_review = {k: v for k, v in stored.items() if k not in ("review_id", "tenant_id", "project_id")}
        resp = {"review": api_review, "approval_token": approval_token} if subject_type == "edit_proposal" else api_review
        self.store.save_idempotent(ctx.tenant_id, scope, idempotency_key, resp)
        return resp
