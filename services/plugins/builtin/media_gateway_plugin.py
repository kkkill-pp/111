"""Built-in Plugin 4: BYOK Multi-Modal Media Gateway (Phase 5 Stage 2).

Inspired by OpenMotion's BYOK Model Gateway (Apache-2.0).
Features:
  - Tenant-scoped BYOK provider credential registry (keys masked on all reads, never written to timeline JSON)
  - Strict fail-loud guard when a remote provider is requested without a configured key (422 API_KEY_REQUIRED)
  - Built-in offline/deterministic local generator (PIL PNG + FFmpeg audio) for zero-cost local & CI verification
  - HITL Candidate -> Explicit Human Adoption flow before entering project assets
"""
from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext, iso_now
from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest

MODEL_CATALOG: list[dict[str, Any]] = [
    {"provider": "local_deterministic", "model_id": "local-art-v1", "modality": "image", "requires_key": False},
    {"provider": "local_deterministic", "model_id": "local-bgm-v1", "modality": "audio_bgm", "requires_key": False},
    {"provider": "openai_compatible", "model_id": "gpt-image-1", "modality": "image", "requires_key": True},
    {"provider": "fal_ai", "model_id": "flux-schnell", "modality": "image", "requires_key": True},
    {"provider": "fal_ai", "model_id": "kokoro-tts", "modality": "tts_voice", "requires_key": True},
    {"provider": "atlas_cloud", "model_id": "suno-v4", "modality": "audio_bgm", "requires_key": True},
]


def mask_secret(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 6:
        return "***"
    return f"{k[:3]}***{k[-4:]}"


class MediaGatewayPlugin(AIVSPlugin):
    PLUGIN_ID = "media_gateway_plugin"

    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id=self.PLUGIN_ID,
            name="BYOK Multi-Modal Media Gateway",
            version="1.0.0",
            category="media_gen",
            license="Apache-2.0",
            description="Tenant-scoped BYOK gateway for image, BGM, and TTS generation with explicit HITL candidate adoption.",
            capabilities=["media_gen.image", "media_gen.audio_bgm", "media_gen.tts_voice", "media_gen.byok_providers"],
            enabled=True,
        )
        self._lock = threading.Lock()
        # tenant_id -> provider_name -> {"api_key": ..., "base_url": ...}
        self._credentials: dict[str, dict[str, dict[str, str]]] = {}
        self._candidates: dict[str, dict[str, Any]] = {}

    def configure_provider(
        self, ctx: TenantContext, provider: str, api_key: str, base_url: Optional[str] = None
    ) -> dict[str, Any]:
        if provider not in {"openai_compatible", "fal_ai", "atlas_cloud"}:
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported BYOK provider '{provider}'")
        if not api_key or len(api_key.strip()) < 6:
            raise AppError(422, "VALIDATION_ERROR", "api_key must be at least 6 characters")
        with self._lock:
            self._credentials.setdefault(ctx.tenant_id, {})[provider] = {
                "api_key": api_key.strip(),
                "base_url": (base_url or "").strip(),
            }
        return {
            "provider": provider,
            "configured": True,
            "api_key_masked": mask_secret(api_key),
            "base_url": (base_url or "").strip() or None,
        }

    def list_catalog(self, ctx: TenantContext) -> dict[str, Any]:
        with self._lock:
            tenant_creds = dict(self._credentials.get(ctx.tenant_id, {}))
        providers_status = [
            {
                "provider": p,
                "configured": True,
                "api_key_masked": mask_secret(cfg["api_key"]),
            }
            for p, cfg in sorted(tenant_creds.items())
        ]
        return {"models": list(MODEL_CATALOG), "configured_providers": providers_status}

    def generate_candidate(
        self,
        ctx: TenantContext,
        project_id: str,
        modality: str,
        prompt: str,
        output_dir: str,
        provider: str = "local_deterministic",
        model_id: Optional[str] = None,
        duration_sec: float = 2.0,
    ) -> dict[str, Any]:
        if modality not in {"image", "audio_bgm", "tts_voice"}:
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported modality '{modality}'")
        if not prompt or not prompt.strip():
            raise AppError(422, "VALIDATION_ERROR", "prompt cannot be empty")

        if provider != "local_deterministic":
            with self._lock:
                cred = self._credentials.get(ctx.tenant_id, {}).get(provider)
            if not cred or not cred.get("api_key"):
                raise AppError(
                    422,
                    "API_KEY_REQUIRED",
                    f"Provider '{provider}' requires a configured BYOK API key for this tenant",
                )
            raise AppError(
                501,
                "REMOTE_PROVIDER_UNAVAILABLE",
                f"Remote HTTP dispatch for '{provider}' requires outbound network access",
            )

        cand_id = str(uuid4())
        out_root = Path(output_dir) / ctx.tenant_id / project_id / "candidates"
        out_root.mkdir(parents=True, exist_ok=True)

        if modality == "image":
            out_file = out_root / f"gen_{cand_id}.png"
            digest = hashlib.sha256(prompt.encode("utf-8")).digest()
            bg = (digest[0] % 180 + 30, digest[1] % 180 + 30, digest[2] % 180 + 30)
            img = Image.new("RGB", (640, 360), bg)
            draw = ImageDraw.Draw(img)
            draw.rectangle([20, 20, 620, 340], outline=(255, 255, 255), width=3)
            draw.text((40, 160), f"AI GEN: {prompt[:48]}", fill=(255, 255, 255), font=ImageFont.load_default())
            img.save(str(out_file), format="PNG")
            asset_kind = "image"
            filename = f"gen_image_{cand_id[:8]}.png"
        else:
            out_file = out_root / f"gen_{cand_id}.wav"
            freq = 330 + (hashlib.sha256(prompt.encode("utf-8")).digest()[0] % 220)
            dur = max(0.5, min(30.0, float(duration_sec)))
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={dur}",
                    "-c:a", "pcm_s16le", str(out_file),
                ],
                check=True,
                timeout=30,
            )
            asset_kind = "audio"
            filename = f"gen_{modality}_{cand_id[:8]}.wav"

        rec = {
            "candidate_id": cand_id,
            "tenant_id": ctx.tenant_id,
            "project_id": project_id,
            "provider": provider,
            "model_id": model_id or ("local-art-v1" if modality == "image" else "local-bgm-v1"),
            "modality": modality,
            "asset_kind": asset_kind,
            "filename": filename,
            "prompt": prompt.strip(),
            "storage_key": str(out_file),
            "checksum_sha256": file_sha256(str(out_file)),
            "size_bytes": out_file.stat().st_size,
            "status": "candidate",
            "adopted_asset_id": None,
            "created_at": iso_now(),
        }
        with self._lock:
            self._candidates[cand_id] = rec
        return self._public_view(rec)

    def adopt_candidate(self, ctx: TenantContext, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._candidates.get(candidate_id)
            if not rec or rec["tenant_id"] != ctx.tenant_id:
                raise AppError(404, "NOT_FOUND", f"Media candidate {candidate_id} not found")
            if rec["status"] == "adopted":
                return dict(rec)
            asset_id = str(uuid4())
            rec["status"] = "adopted"
            rec["adopted_asset_id"] = asset_id
            return dict(rec)

    @staticmethod
    def _public_view(rec: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in rec.items() if k not in ("tenant_id", "storage_key")}
