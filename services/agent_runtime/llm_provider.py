"""Pluggable LLM Provider Adapter Layer (AI Video Studio v3.0 Principle 3).

Supports swappable providers behind a single strict structured-output interface:
  - RuleBasedDeterministicProvider (default offline/CI deterministic planner)
  - OpenAICompatibleProvider (OpenAI / DeepSeek / Qwen / Ollama / LM Studio via stdlib urllib)

All provider outputs MUST pass JSON Schema validation in AgentHarness before reaching TimelineService.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class PlannedProposal:
    provider_name: str
    model_id: str
    rationale: str
    risk_level: str
    operations: list[dict[str, Any]]
    evidence_refs: list[str]


class LLMProvider(Protocol):
    provider_name: str
    model_id: str

    def plan_edit_operations(
        self,
        timeline_snapshot: dict[str, Any],
        user_intent: str,
    ) -> PlannedProposal: ...


class RuleBasedDeterministicProvider:
    """Zero-network deterministic intent planner for offline dev, tests, and local fallback."""

    provider_name = "deterministic_local"
    model_id = "rule-planner-v3.0"

    def plan_edit_operations(
        self,
        timeline_snapshot: dict[str, Any],
        user_intent: str,
    ) -> PlannedProposal:
        video_track = next((t for t in timeline_snapshot["tracks"] if t["type"] == "video"), None)
        sub_track = next((t for t in timeline_snapshot["tracks"] if t["type"] == "subtitle"), None)
        clips = video_track["clips"] if video_track else []
        if not clips:
            return PlannedProposal(self.provider_name, self.model_id, "No video clips", "low", [], [])

        first_clip = clips[0]
        src_in = first_clip["source_range"]["in"]
        src_out = first_clip["source_range"]["out"]
        intent_low = user_intent.lower()
        ops: list[dict[str, Any]] = []
        evidence: list[str] = [f"provider:{self.provider_name}", f"clip:{first_clip['id']}"]

        # 1. Explicit speed intent (e.g. "1.5x", "2x", "倍速")
        m_speed = re.search(r"(\d+(?:\.\d+)?)\s*x", intent_low)
        if m_speed and ("speed" in intent_low or "倍速" in user_intent or "加速" in user_intent or "慢放" in user_intent):
            rate = max(0.25, min(4.0, float(m_speed.group(1))))
            ops.append({
                "operation_id": str(uuid4()),
                "operation_type": "UPDATE_SPEED",
                "payload": {"clip_id": first_clip["id"], "speed": rate},
                "reason": f"Adjust playback speed to {rate}x per intent",
                "evidence_refs": [f"clip:{first_clip['id']}"],
                "risk_level": "medium",
            })

        # 2. Explicit mute / volume intent
        if "mute" in intent_low or "静音" in user_intent:
            ops.append({
                "operation_id": str(uuid4()),
                "operation_type": "UPDATE_VOLUME",
                "payload": {"clip_id": first_clip["id"], "volume": 0.0, "enabled": False},
                "reason": "Mute primary clip audio per intent",
                "evidence_refs": [f"clip:{first_clip['id']}"],
                "risk_level": "low",
            })

        # 3. Default trim + caption behavior (preserves v2.2/v3.0 vertical-slice intent contract)
        if not ops or "trim" in intent_low or "裁" in user_intent or "收紧" in user_intent:
            trim_delta = min(30000, max(1, (src_out - src_in) // 4))
            ops.append({
                "operation_id": str(uuid4()),
                "operation_type": "TRIM_CLIP",
                "payload": {
                    "clip_id": first_clip["id"],
                    "new_source_in": src_in + trim_delta,
                    "new_source_out": src_out,
                },
                "reason": f"Trim opening pause per intent: {user_intent}",
                "evidence_refs": [f"clip:{first_clip['id']}", f"tick:{src_in}-{src_in + trim_delta}"],
                "risk_level": "medium",
            })
            if sub_track:
                m_sub = re.search(r"(?:subtitle|caption|字幕)[:：]\s*(.+)$", user_intent, re.IGNORECASE)
                sub_text = m_sub.group(1).strip()[:120] if m_sub else "AI Video Studio HITL Preview"
                ops.append({
                    "operation_id": str(uuid4()),
                    "operation_type": "ADD_SUBTITLE",
                    "payload": {
                        "track_id": sub_track["id"],
                        "timeline_start": 0,
                        "duration": max(1, (src_out - src_in) - trim_delta),
                        "text": sub_text,
                    },
                    "reason": "Add opening caption aligned to trimmed clip",
                    "evidence_refs": [f"track:{sub_track['id']}"],
                    "risk_level": "low",
                })
                evidence.append(f"track:{sub_track['id']}")

        return PlannedProposal(
            provider_name=self.provider_name,
            model_id=self.model_id,
            rationale=f"[{self.provider_name}/{self.model_id}] Proposed {len(ops)} reversible edits for intent: '{user_intent}'",
            risk_level="medium",
            operations=ops,
            evidence_refs=evidence,
        )


class OpenAICompatibleProvider:
    """Swappable OpenAI-compatible HTTP JSON provider (OpenAI / DeepSeek / Qwen / Ollama / LM Studio)."""

    provider_name = "openai_compatible"

    def __init__(self, base_url: str, model_id: str, api_key: str = "", timeout_sec: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    def _post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def plan_edit_operations(
        self,
        timeline_snapshot: dict[str, Any],
        user_intent: str,
    ) -> PlannedProposal:
        sys_prompt = (
            "You are AI Video Studio's controlled editing planner. "
            "Output ONLY valid JSON: {\"rationale\": str, \"risk_level\": \"low\"|\"medium\"|\"high\", \"operations\": [...]}. "
            "Never emit UNDO or REDO."
        )
        req_body = {
            "model": self.model_id,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps({"intent": user_intent, "timeline_summary": {"version": timeline_snapshot.get("version"), "tracks": timeline_snapshot.get("tracks", [])}})},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        raw = self._post_json(f"{self.base_url}/chat/completions", req_body, headers)
        content = raw["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        ops = parsed.get("operations") or []
        for op in ops:
            op.setdefault("operation_id", str(uuid4()))
        return PlannedProposal(
            provider_name=self.provider_name,
            model_id=self.model_id,
            rationale=str(parsed.get("rationale") or f"[{self.model_id}] Planned edits for: {user_intent}"),
            risk_level=str(parsed.get("risk_level") or "medium"),
            operations=ops,
            evidence_refs=[f"provider:{self.provider_name}", f"model:{self.model_id}"],
        )


def create_llm_provider() -> LLMProvider:
    base_url = os.environ.get("AIVS_LLM_BASE_URL", "").strip()
    model_id = os.environ.get("AIVS_LLM_MODEL", "").strip()
    api_key = os.environ.get("AIVS_LLM_API_KEY", "").strip()
    if base_url and model_id:
        return OpenAICompatibleProvider(base_url=base_url, model_id=model_id, api_key=api_key)
    return RuleBasedDeterministicProvider()
