"""Timeline Service — SOLE write entry point for Canonical Timelines (HITL v2.2)."""
from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from services.infrastructure.contracts import canonical_sha256, validate_against_schema
from services.infrastructure.errors import AppError
from services.infrastructure.store import InMemoryTenantStore, TenantContext, iso_now, require_role


class TimelineService:
    def __init__(self, store: InMemoryTenantStore) -> None:
        self.store = store

    def create_timeline(
        self,
        ctx: TenantContext,
        project_id: str,
        profile: dict[str, Any],
        idempotency_key: str,
        storyboard_id: str | None = None,
        storyboard_version: int | None = None,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"create_timeline:{project_id}", idempotency_key)
        if cached:
            return cached

        self.store.get(ctx, "projects", project_id)
        clips: list[dict[str, Any]] = []
        origin_cmd = "init"

        if storyboard_id:
            sb = self.store.get(ctx, "storyboards", storyboard_id)
            if sb["status"] != "accepted":
                raise AppError(409, "APPROVAL_REQUIRED", "Storyboard must be human-accepted before creating production timeline")
            ver = storyboard_version or sb["current_version"]
            sb_vers = self.store.query(ctx, "storyboard_versions", storyboard_id=storyboard_id, version=ver)
            if not sb_vers:
                raise AppError(404, "NOT_FOUND", "Storyboard version not found")
            proposal = sb_vers[0]["proposal_json"]
            cursor_tick = 0
            for seg in proposal["ordered_segments"]:
                dur = seg["source_out_tick"] - seg["source_in_tick"]
                clips.append({
                    "id": str(uuid4()),
                    "asset_id": seg["asset_id"],
                    "source_range": {
                        "in": seg["source_in_tick"],
                        "out": seg["source_out_tick"],
                        "timebase": profile["timeline_timebase"],
                        "vfr_pts_map_ref": None,
                    },
                    "timeline_start": cursor_tick,
                    "duration": dur,
                    "speed": 1.0,
                    "transform": {"mode": "fit", "scale": 1.0, "position_x": 0.5, "position_y": 0.5},
                    "audio": {"enabled": True, "volume": 1.0, "fade_in_ticks": 0, "fade_out_ticks": 0},
                    "transitions": [],
                })
                cursor_tick += dur
            origin_cmd = "storyboard_compile"

        timeline_id = str(uuid4())
        snapshot = {
            "schema_version": "2.0",
            "timeline_id": timeline_id,
            "project_id": project_id,
            "version": 1,
            "parent_version": None,
            "profile": profile,
            "tracks": [
                {"id": str(uuid4()), "type": "video", "order": 0, "muted": False, "clips": clips},
                {"id": str(uuid4()), "type": "subtitle", "order": 1, "items": []},
            ],
        }
        validate_against_schema("timeline.schema.json", snapshot)
        checksum = canonical_sha256(snapshot)

        self.store.put("timelines", {
            "id": timeline_id,
            "tenant_id": ctx.tenant_id,
            "project_id": project_id,
            "source_storyboard_id": storyboard_id,
            "source_storyboard_version": storyboard_version,
            "current_version": 1,
            "profile_json": profile,
            "status": "active",
            "created_by": ctx.user_id,
            "created_at": iso_now(),
        })
        self.store.put("timeline_versions", {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "version": 1,
            "parent_version": None,
            "origin_command": origin_cmd,
            "restored_from_version": None,
            "snapshot_json": snapshot,
            "snapshot_checksum": checksum,
            "created_by_type": "user",
            "created_by_id": ctx.user_id,
            "created_at": iso_now(),
        })
        self.store.add_audit(ctx, "user", ctx.user_id, "timeline.created", "timeline", timeline_id, {"version": 1})

        resp = {
            "timeline_id": timeline_id,
            "project_id": project_id,
            "version": 1,
            "snapshot_checksum": checksum,
            "profile": profile,
            "tracks": snapshot["tracks"],
        }
        self.store.save_idempotent(ctx.tenant_id, f"create_timeline:{project_id}", idempotency_key, resp)
        return resp

    def get_timeline(self, ctx: TenantContext, timeline_id: str, version: int | None = None) -> dict[str, Any]:
        tl = self.store.get(ctx, "timelines", timeline_id)
        ver = version or tl["current_version"]
        rows = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=ver)
        if not rows:
            raise AppError(404, "NOT_FOUND", f"Timeline version {ver} not found")
        vrow = rows[0]
        snap = vrow["snapshot_json"]
        return {
            "timeline_id": timeline_id,
            "project_id": tl["project_id"],
            "version": ver,
            "snapshot_checksum": vrow["snapshot_checksum"],
            "profile": snap["profile"],
            "tracks": snap["tracks"],
        }

    def list_versions(self, ctx: TenantContext, timeline_id: str) -> dict[str, Any]:
        self.store.get(ctx, "timelines", timeline_id)
        rows = sorted(
            self.store.query(ctx, "timeline_versions", timeline_id=timeline_id),
            key=lambda r: r["version"],
        )
        return {
            "timeline_id": timeline_id,
            "items": [
                {
                    "version": r["version"],
                    "parent_version": r["parent_version"],
                    "origin_command": r["origin_command"],
                    "snapshot_checksum": r["snapshot_checksum"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "next_cursor": None,
        }

    def _simulate_operations(self, snapshot: dict[str, Any], operations: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        next_snap = copy.deepcopy(snapshot)
        next_snap["parent_version"] = snapshot["version"]
        next_snap["version"] = snapshot["version"] + 1
        changes = []
        clips_affected = 0
        subtitles_affected = 0
        before_dur = self._total_duration(snapshot)

        for op in operations:
            validate_against_schema("edit-operation.schema.json", op, sub_ref="#/$defs/operation_input")
            otype = op["operation_type"]
            payload = op["payload"]
            if otype == "TRIM_CLIP":
                clip = self._find_clip(next_snap, payload["clip_id"])
                if payload["new_source_out"] <= payload["new_source_in"]:
                    raise AppError(422, "VALIDATION_ERROR", "TRIM_CLIP requires new_source_out > new_source_in")
                old_in, old_out = clip["source_range"]["in"], clip["source_range"]["out"]
                clip["source_range"]["in"] = payload["new_source_in"]
                clip["source_range"]["out"] = payload["new_source_out"]
                clip["duration"] = payload["new_source_out"] - payload["new_source_in"]
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "from": [old_in, old_out], "to": [payload["new_source_in"], payload["new_source_out"]]})
            elif otype == "ADD_SUBTITLE":
                sub_track = next((t for t in next_snap["tracks"] if t["id"] == payload["track_id"] and t["type"] == "subtitle"), None)
                if not sub_track:
                    raise AppError(422, "VALIDATION_ERROR", "Subtitle track not found")
                item = {
                    "id": str(uuid4()),
                    "timeline_start": payload["timeline_start"],
                    "duration": payload["duration"],
                    "text": payload["text"],
                    "style_preset": "default",
                    "safe_zone": "bottom",
                }
                sub_track["items"].append(item)
                subtitles_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "subtitle_id": item["id"], "text": item["text"]})
            elif otype == "REMOVE_CLIP":
                removed = False
                for tr in next_snap["tracks"]:
                    if tr["type"] in ("video", "audio"):
                        before_len = len(tr["clips"])
                        tr["clips"] = [c for c in tr["clips"] if c["id"] != payload["clip_id"]]
                        if len(tr["clips"]) < before_len:
                            removed = True
                            clips_affected += 1
                            changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": payload["clip_id"]})
                if not removed:
                    raise AppError(422, "VALIDATION_ERROR", f"Clip {payload['clip_id']} not found")
            elif otype == "UPDATE_VOLUME":
                clip = self._find_clip(next_snap, payload["clip_id"])
                audio = dict(clip.get("audio") or {"enabled": True, "volume": 1.0})
                old_vol = audio.get("volume", 1.0)
                audio["volume"] = float(payload["volume"])
                if "enabled" in payload:
                    audio["enabled"] = bool(payload["enabled"])
                clip["audio"] = audio
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "from_volume": old_vol, "to_volume": audio["volume"], "enabled": audio.get("enabled", True)})
            elif otype == "UPDATE_SPEED":
                clip = self._find_clip(next_snap, payload["clip_id"])
                old_speed = float(clip.get("speed", 1.0))
                new_speed = float(payload["speed"])
                clip["speed"] = new_speed
                src_span = clip["source_range"]["out"] - clip["source_range"]["in"]
                clip["duration"] = max(1, int(round(src_span / new_speed)))
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "from_speed": old_speed, "to_speed": new_speed})
            elif otype == "ADD_CLIP":
                tr = next((t for t in next_snap["tracks"] if t["id"] == payload["track_id"] and t["type"] in ("video", "audio")), None)
                if not tr:
                    raise AppError(422, "VALIDATION_ERROR", f"Track {payload['track_id']} not found")
                if payload["source_out"] <= payload["source_in"]:
                    raise AppError(422, "VALIDATION_ERROR", "ADD_CLIP requires source_out > source_in")
                tb = next_snap["profile"]["timeline_timebase"]
                new_clip = {
                    "id": str(uuid4()),
                    "asset_id": payload["asset_id"],
                    "source_range": {"in": payload["source_in"], "out": payload["source_out"], "timebase": tb},
                    "timeline_start": payload["timeline_start"],
                    "duration": payload["source_out"] - payload["source_in"],
                    "speed": 1.0,
                    "transform": {"mode": "fit"},
                    "audio": {"enabled": True, "volume": 1.0},
                    "transitions": [],
                }
                tr["clips"].append(new_clip)
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": new_clip["id"], "track_id": tr["id"]})
            elif otype == "MOVE_CLIP":
                clip = self._find_clip(next_snap, payload["clip_id"])
                old_start = clip["timeline_start"]
                clip["timeline_start"] = int(payload["target_timeline_start"])
                if payload.get("target_track_id"):
                    target_tr = next((t for t in next_snap["tracks"] if t["id"] == payload["target_track_id"] and t["type"] in ("video", "audio")), None)
                    if not target_tr:
                        raise AppError(422, "VALIDATION_ERROR", f"Target track {payload['target_track_id']} not found")
                    for tr in next_snap["tracks"]:
                        if tr["type"] in ("video", "audio"):
                            tr["clips"] = [c for c in tr["clips"] if c["id"] != clip["id"]]
                    target_tr["clips"].append(clip)
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "from_start": old_start, "to_start": clip["timeline_start"]})
            elif otype == "SPLIT_CLIP":
                split_tick = int(payload["split_at_timeline_tick"])
                found_track = None
                found_idx = -1
                for tr in next_snap["tracks"]:
                    if tr["type"] in ("video", "audio"):
                        for idx, c in enumerate(tr["clips"]):
                            if c["id"] == payload["clip_id"]:
                                found_track, found_idx = tr, idx
                                break
                if not found_track:
                    raise AppError(422, "VALIDATION_ERROR", f"Clip {payload['clip_id']} not found")
                orig = found_track["clips"][found_idx]
                c_start, c_dur = orig["timeline_start"], orig["duration"]
                if split_tick <= c_start or split_tick >= c_start + c_dur:
                    raise AppError(422, "VALIDATION_ERROR", "split_at_timeline_tick must lie strictly inside clip bounds")
                left_dur = split_tick - c_start
                right_dur = c_dur - left_dur
                src_in, src_out = orig["source_range"]["in"], orig["source_range"]["out"]
                src_split = src_in + int(round((src_out - src_in) * (left_dur / c_dur)))
                right_clip = copy.deepcopy(orig)
                right_clip["id"] = str(uuid4())
                orig["duration"] = left_dur
                orig["source_range"]["out"] = src_split
                right_clip["timeline_start"] = split_tick
                right_clip["duration"] = right_dur
                right_clip["source_range"]["in"] = src_split
                found_track["clips"].insert(found_idx + 1, right_clip)
                clips_affected += 2
                changes.append({"operation_id": op["operation_id"], "type": otype, "left_clip_id": orig["id"], "right_clip_id": right_clip["id"], "split_at": split_tick})
            elif otype == "UPDATE_SUBTITLE":
                sub_item = None
                for tr in next_snap["tracks"]:
                    if tr["type"] == "subtitle":
                        for it in tr["items"]:
                            if it["id"] == payload["subtitle_id"]:
                                sub_item = it
                                break
                if not sub_item:
                    raise AppError(422, "VALIDATION_ERROR", f"Subtitle {payload['subtitle_id']} not found")
                for k in ("text", "timeline_start", "duration"):
                    if k in payload:
                        sub_item[k] = payload[k]
                subtitles_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "subtitle_id": sub_item["id"]})
            elif otype == "REMOVE_SUBTITLE":
                removed = False
                for tr in next_snap["tracks"]:
                    if tr["type"] == "subtitle":
                        before_len = len(tr["items"])
                        tr["items"] = [it for it in tr["items"] if it["id"] != payload["subtitle_id"]]
                        if len(tr["items"]) < before_len:
                            removed = True
                            subtitles_affected += 1
                            changes.append({"operation_id": op["operation_id"], "type": otype, "subtitle_id": payload["subtitle_id"]})
                if not removed:
                    raise AppError(422, "VALIDATION_ERROR", f"Subtitle {payload['subtitle_id']} not found")
            elif otype == "UPDATE_TRANSFORM":
                clip = self._find_clip(next_snap, payload["clip_id"])
                tr_cfg = dict(clip.get("transform") or {"mode": "fit"})
                tr_cfg.update(payload["transform"])
                clip["transform"] = tr_cfg
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "transform": tr_cfg})
            elif otype == "REORDER_CLIP":
                tr = next((t for t in next_snap["tracks"] if t["id"] == payload["track_id"] and t["type"] in ("video", "audio")), None)
                if not tr:
                    raise AppError(422, "VALIDATION_ERROR", f"Track {payload['track_id']} not found")
                by_id = {c["id"]: c for c in tr["clips"]}
                ordered = [by_id[cid] for cid in payload["ordered_clip_ids"] if cid in by_id]
                if len(ordered) != len(payload["ordered_clip_ids"]):
                    raise AppError(422, "VALIDATION_ERROR", "REORDER_CLIP contains unknown clip_id")
                cursor = 0
                for c in ordered:
                    c["timeline_start"] = cursor
                    cursor += c["duration"]
                tr["clips"] = ordered
                clips_affected += len(ordered)
                changes.append({"operation_id": op["operation_id"], "type": otype, "track_id": tr["id"], "ordered_clip_ids": payload["ordered_clip_ids"]})
            elif otype == "ADD_TRANSITION":
                clip = self._find_clip(next_snap, payload["clip_id"])
                tr_item = {
                    "id": str(uuid4()),
                    "type": payload["type"],
                    "duration_ticks": int(payload["duration_ticks"]),
                    "position": payload["position"],
                }
                clip.setdefault("transitions", []).append(tr_item)
                clips_affected += 1
                changes.append({"operation_id": op["operation_id"], "type": otype, "clip_id": clip["id"], "transition_id": tr_item["id"]})
            elif otype == "REMOVE_TRANSITION":
                removed = False
                for tr in next_snap["tracks"]:
                    if tr["type"] in ("video", "audio"):
                        for c in tr["clips"]:
                            before = len(c.get("transitions") or [])
                            c["transitions"] = [t for t in (c.get("transitions") or []) if t["id"] != payload["transition_id"]]
                            if len(c["transitions"]) < before:
                                removed = True
                                clips_affected += 1
                                changes.append({"operation_id": op["operation_id"], "type": otype, "transition_id": payload["transition_id"]})
                if not removed:
                    raise AppError(422, "VALIDATION_ERROR", f"Transition {payload['transition_id']} not found")
            elif otype == "ADD_OVERLAY":
                ov_track = next((t for t in next_snap["tracks"] if t["id"] == payload["track_id"] and t["type"] == "overlay"), None)
                if not ov_track:
                    raise AppError(422, "VALIDATION_ERROR", f"Overlay track {payload['track_id']} not found")
                ov_item = {
                    "id": str(uuid4()),
                    "kind": payload["kind"],
                    "timeline_start": int(payload["timeline_start"]),
                    "duration": int(payload["duration"]),
                    "asset_id": payload.get("asset_id"),
                    "text": payload.get("text"),
                    "position_x": float(payload.get("position_x", 0.5)),
                    "position_y": float(payload.get("position_y", 0.5)),
                    "scale": float(payload.get("scale", 1.0)),
                    "opacity": float(payload.get("opacity", 1.0)),
                }
                ov_track["items"].append(ov_item)
                changes.append({"operation_id": op["operation_id"], "type": otype, "overlay_id": ov_item["id"], "kind": ov_item["kind"]})
            elif otype == "REMOVE_OVERLAY":
                removed = False
                for tr in next_snap["tracks"]:
                    if tr["type"] == "overlay":
                        before = len(tr["items"])
                        tr["items"] = [it for it in tr["items"] if it["id"] != payload["overlay_id"]]
                        if len(tr["items"]) < before:
                            removed = True
                            changes.append({"operation_id": op["operation_id"], "type": otype, "overlay_id": payload["overlay_id"]})
                if not removed:
                    raise AppError(422, "VALIDATION_ERROR", f"Overlay {payload['overlay_id']} not found")
            else:
                raise AppError(
                    422,
                    "VALIDATION_ERROR",
                    f"Operation '{otype}' is not implemented in Phase 1 TimelineService (silent no-op forbidden)",
                )

        validate_against_schema("timeline.schema.json", next_snap)
        after_dur = self._total_duration(next_snap)
        diff = {"base_version": snapshot["version"], "candidate_version": next_snap["version"], "changes": changes}
        impact = {
            "duration_delta_ticks": after_dur - before_dur,
            "clips_affected": clips_affected,
            "subtitles_affected": subtitles_affected,
            "reversible": True,
            "warnings": [],
        }
        return next_snap, diff, impact

    @staticmethod
    def _find_clip(snapshot: dict[str, Any], clip_id: str) -> dict[str, Any]:
        for tr in snapshot["tracks"]:
            if tr["type"] in ("video", "audio"):
                for c in tr["clips"]:
                    if c["id"] == clip_id:
                        return c
        raise AppError(422, "VALIDATION_ERROR", f"Clip {clip_id} not found")

    @staticmethod
    def _total_duration(snapshot: dict[str, Any]) -> int:
        end_tick = 0
        for tr in snapshot["tracks"]:
            if tr["type"] in ("video", "audio"):
                for c in tr["clips"]:
                    end_tick = max(end_tick, c["timeline_start"] + c["duration"])
        return end_tick

    def validate_operations(
        self, ctx: TenantContext, timeline_id: str, base_version: int, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Expected base_version {tl['current_version']}, got {base_version}")
        snap = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]["snapshot_json"]
        _, diff, _ = self._simulate_operations(snap, operations)
        return {"valid": True, "errors": [], "warnings": [], "diff": diff}

    def create_proposal(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        operations: list[dict[str, Any]],
        actor_type: str,
        actor_id: str,
        rationale: str,
        evidence_refs: list[str],
        risk_level: str,
        idempotency_key: str,
        user_intent: str | None = None,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"proposal:{timeline_id}", idempotency_key)
        if cached:
            return cached

        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Timeline is at v{tl['current_version']}, cannot propose against v{base_version}")

        snap = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]["snapshot_json"]
        _, diff, impact = self._simulate_operations(snap, operations)

        proposal_id = str(uuid4())
        checksum = canonical_sha256({"timeline_id": timeline_id, "base_version": base_version, "operations": operations})
        proposal_doc = {
            "proposal_id": proposal_id,
            "timeline_id": timeline_id,
            "base_version": base_version,
            "result_version": None,
            "status": "awaiting_review",
            "actor": {"type": actor_type, "id": actor_id},
            "originating_user_id": ctx.user_id,
            "originating_request_id": str(uuid4()),
            "user_intent": user_intent,
            "rationale": rationale,
            "risk_level": risk_level,
            "evidence_refs": evidence_refs,
            "operations": operations,
            "diff": diff,
            "impact_summary": impact,
            "proposal_checksum": checksum,
        }
        validate_against_schema("edit-proposal.schema.json", proposal_doc)

        rec = {
            **proposal_doc,
            "id": proposal_id,
            "tenant_id": ctx.tenant_id,
            "project_id": tl["project_id"],
            "selected_operation_ids": [op["operation_id"] for op in operations],
            "created_at": iso_now(),
        }
        self.store.put("edit_proposals", rec)
        self.store.add_audit(ctx, actor_type, actor_id, "proposal.created", "edit_proposal", proposal_id, {"base_version": base_version, "risk_level": risk_level})

        resp = {
            "outcome": "proposal_created",
            "base_version": base_version,
            "new_version": None,
            "proposal_id": proposal_id,
            "proposal_checksum": checksum,
            "diff": diff,
            "impact_summary": impact,
            "risk_level": risk_level,
        }
        self.store.save_idempotent(ctx.tenant_id, f"proposal:{timeline_id}", idempotency_key, resp)
        return resp

    def apply_approved_proposal(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        proposal_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"apply:{timeline_id}", idempotency_key)
        if cached:
            return cached

        prop = self.store.get(ctx, "edit_proposals", proposal_id)
        if prop["timeline_id"] != timeline_id:
            raise AppError(404, "NOT_FOUND", "Proposal does not belong to this timeline")

        # Atomic version check + single-use ApprovalToken consumption
        review_id = self.store.consume_approval_token(
            ctx,
            timeline_id=timeline_id,
            proposal_id=proposal_id,
            expected_base_version=base_version,
            expected_checksum=prop["proposal_checksum"],
            raw_token=approval_token,
        )

        selected_ids = set(prop.get("selected_operation_ids") or [])
        ops_to_apply = [op for op in prop["operations"] if op["operation_id"] in selected_ids]
        if not ops_to_apply:
            raise AppError(422, "VALIDATION_ERROR", "No operations selected in approved proposal")

        tl = self.store.get(ctx, "timelines", timeline_id)
        snap = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]["snapshot_json"]
        next_snap, diff, _ = self._simulate_operations(snap, ops_to_apply)
        new_version = next_snap["version"]
        new_checksum = canonical_sha256(next_snap)

        self.store.put("timeline_versions", {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "version": new_version,
            "parent_version": base_version,
            "origin_command": "edit",
            "restored_from_version": None,
            "snapshot_json": next_snap,
            "snapshot_checksum": new_checksum,
            "created_by_type": prop["actor"]["type"],
            "created_by_id": prop["actor"]["id"],
            "created_at": iso_now(),
        })
        tl["current_version"] = new_version
        self.store.put("timelines", tl)

        prop["status"] = "applied"
        prop["result_version"] = new_version
        self.store.put("edit_proposals", prop)

        # Supersede any other awaiting_review proposals targeting old base_version
        for other in self.store.query(ctx, "edit_proposals", timeline_id=timeline_id, base_version=base_version):
            if other["id"] != proposal_id and other["status"] in ("awaiting_review", "approved"):
                other["status"] = "superseded"
                self.store.put("edit_proposals", other)

        self.store.add_audit(
            ctx,
            prop["actor"]["type"],
            prop["actor"]["id"],
            "timeline.version.created",
            "timeline",
            timeline_id,
            {"base_version": base_version, "new_version": new_version, "proposal_id": proposal_id, "review_id": review_id},
        )

        resp = {
            "outcome": "applied",
            "base_version": base_version,
            "new_version": new_version,
            "proposal_id": proposal_id,
            "proposal_checksum": prop["proposal_checksum"],
            "diff": diff,
        }
        self.store.save_idempotent(ctx.tenant_id, f"apply:{timeline_id}", idempotency_key, resp)
        return resp

    def undo(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        idempotency_key: str,
        target_version: int | None = None,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"undo:{timeline_id}", idempotency_key)
        if cached:
            return cached

        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Timeline is at v{tl['current_version']}, cannot undo from v{base_version}")
        restore_ver = target_version or (base_version - 1)
        if restore_ver < 1 or restore_ver >= base_version:
            raise AppError(422, "VALIDATION_ERROR", "Invalid undo target_version")

        target_row = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=restore_ver)[0]
        restored_snap = copy.deepcopy(target_row["snapshot_json"])
        new_version = base_version + 1
        restored_snap["parent_version"] = base_version
        restored_snap["version"] = new_version
        validate_against_schema("timeline.schema.json", restored_snap)
        new_checksum = canonical_sha256(restored_snap)

        self.store.put("timeline_versions", {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "version": new_version,
            "parent_version": base_version,
            "origin_command": "undo",
            "restored_from_version": restore_ver,
            "snapshot_json": restored_snap,
            "snapshot_checksum": new_checksum,
            "created_by_type": "user",
            "created_by_id": ctx.user_id,
            "created_at": iso_now(),
        })
        tl["current_version"] = new_version
        self.store.put("timelines", tl)
        self.store.add_audit(ctx, "user", ctx.user_id, "timeline.undo", "timeline", timeline_id, {"base_version": base_version, "new_version": new_version, "restored_from_version": restore_ver})

        resp = {
            "timeline_id": timeline_id,
            "base_version": base_version,
            "new_version": new_version,
            "origin_command": "undo",
            "snapshot_checksum": new_checksum,
        }
        self.store.save_idempotent(ctx.tenant_id, f"undo:{timeline_id}", idempotency_key, resp)
        return resp

    def validate_operations(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Timeline is at v{tl['current_version']}, expected v{base_version}")
        cur_row = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]
        _, diff, impact = self._simulate_operations(cur_row["snapshot_json"], operations)
        return {"valid": True, "base_version": base_version, "diff": diff, "impact_summary": impact}

    def apply_human_operations(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        operations: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"human_apply:{timeline_id}", idempotency_key)
        if cached:
            return cached
        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Optimistic lock conflict: timeline is at v{tl['current_version']}, expected v{base_version}")
        cur_row = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]
        next_snap, diff, impact = self._simulate_operations(cur_row["snapshot_json"], operations)
        new_version = base_version + 1
        new_checksum = canonical_sha256(next_snap)
        self.store.put("timeline_versions", {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "version": new_version,
            "parent_version": base_version,
            "origin_command": "edit",
            "restored_from_version": None,
            "snapshot_json": next_snap,
            "snapshot_checksum": new_checksum,
            "created_by_type": "user",
            "created_by_id": ctx.user_id,
            "created_at": iso_now(),
        })
        tl["current_version"] = new_version
        self.store.put("timelines", tl)
        self.store.add_audit(ctx, "user", ctx.user_id, "timeline.version.created", "timeline", timeline_id, {"base_version": base_version, "new_version": new_version, "origin_command": "edit"})
        resp = {"outcome": "applied", "base_version": base_version, "new_version": new_version, "snapshot_checksum": new_checksum, "diff": diff, "impact_summary": impact}
        self.store.save_idempotent(ctx.tenant_id, f"human_apply:{timeline_id}", idempotency_key, resp)
        return resp

    def redo(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        idempotency_key: str,
        target_version: int | None = None,
    ) -> dict[str, Any]:
        require_role(ctx, ["owner", "admin", "editor"])
        cached = self.store.get_idempotent(ctx.tenant_id, f"redo:{timeline_id}", idempotency_key)
        if cached:
            return cached
        tl = self.store.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != base_version:
            raise AppError(409, "VERSION_CONFLICT", f"Timeline is at v{tl['current_version']}, cannot redo from v{base_version}")
        cur_row = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=base_version)[0]
        if target_version is None:
            if cur_row.get("origin_command") != "undo" or not cur_row.get("parent_version"):
                raise AppError(422, "VALIDATION_ERROR", "Nothing to redo from current version")
            restore_ver = cur_row["parent_version"]
        else:
            restore_ver = target_version
        if restore_ver < 1 or restore_ver >= base_version:
            raise AppError(422, "VALIDATION_ERROR", "Invalid redo target_version")
        target_row = self.store.query(ctx, "timeline_versions", timeline_id=timeline_id, version=restore_ver)[0]
        restored_snap = copy.deepcopy(target_row["snapshot_json"])
        new_version = base_version + 1
        restored_snap["parent_version"] = base_version
        restored_snap["version"] = new_version
        validate_against_schema("timeline.schema.json", restored_snap)
        new_checksum = canonical_sha256(restored_snap)
        self.store.put("timeline_versions", {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "version": new_version,
            "parent_version": base_version,
            "origin_command": "redo",
            "restored_from_version": restore_ver,
            "snapshot_json": restored_snap,
            "snapshot_checksum": new_checksum,
            "created_by_type": "user",
            "created_by_id": ctx.user_id,
            "created_at": iso_now(),
        })
        tl["current_version"] = new_version
        self.store.put("timelines", tl)
        self.store.add_audit(ctx, "user", ctx.user_id, "timeline.version.redo", "timeline", timeline_id, {"base_version": base_version, "new_version": new_version, "restored_from_version": restore_ver})
        resp = {"timeline_id": timeline_id, "base_version": base_version, "new_version": new_version, "origin_command": "redo", "restored_from_version": restore_ver, "snapshot_checksum": new_checksum}
        self.store.save_idempotent(ctx.tenant_id, f"redo:{timeline_id}", idempotency_key, resp)
        return resp
