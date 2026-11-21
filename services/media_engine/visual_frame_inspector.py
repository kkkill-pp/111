"""Visual Frame Inspector (Phase 2 Precursor — inspired by Cutlass crates/agent/src/picture.rs).

Decodes real rendered video frames via OpenCV to:
  1. Detect black/blank video frames (mean luma < 5.0 & std < 2.0)
  2. Measure bottom subtitle-band pixel activity (contrast/variance)
  3. Export PNG frame snapshots for Agent / Human visual inspection
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass
class FrameInspectionSummary:
    frames_sampled: int
    black_frames: int
    avg_luma: float
    avg_subtitle_band_std: float
    snapshot_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frames_sampled": self.frames_sampled,
            "black_frames": self.black_frames,
            "avg_luma": round(self.avg_luma, 2),
            "avg_subtitle_band_std": round(self.avg_subtitle_band_std, 2),
            "snapshot_paths": self.snapshot_paths,
        }


class VisualFrameInspector:
    """Real pixel-level frame sampler & visual QA inspector."""

    @staticmethod
    def inspect_video_frames(
        video_path: str,
        sample_fractions: tuple[float, ...] = (0.25, 0.5, 0.75),
        save_dir: str | None = None,
    ) -> FrameInspectionSummary:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open video for visual inspection: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            total_frames = 30

        out_dir = Path(save_dir) if save_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        lumas: list[float] = []
        sub_stds: list[float] = []
        black_frames = 0
        snapshots: list[str] = []

        for idx, frac in enumerate(sample_fractions):
            target_frame = max(0, min(total_frames - 1, int(total_frames * frac)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean_luma = float(np.mean(gray))
            std_luma = float(np.std(gray))
            lumas.append(mean_luma)
            if mean_luma < 5.0 and std_luma < 2.0:
                black_frames += 1

            h = gray.shape[0]
            sub_band = gray[int(h * 0.65):, :]
            sub_stds.append(float(np.std(sub_band)))

            if out_dir:
                snap_path = out_dir / f"frame_{idx}_{target_frame}.png"
                cv2.imwrite(str(snap_path), frame)
                snapshots.append(str(snap_path))

        cap.release()
        if not lumas:
            raise RuntimeError(f"Could not decode any frames from {video_path}")

        return FrameInspectionSummary(
            frames_sampled=len(lumas),
            black_frames=black_frames,
            avg_luma=float(np.mean(lumas)),
            avg_subtitle_band_std=float(np.mean(sub_stds)) if sub_stds else 0.0,
            snapshot_paths=snapshots,
        )
