"""Commercial-SaaS-safe PersonDetectionProvider (ADR-003).

Replaces AGPL-3.0 Ultralytics YOLOv8 with a pluggable provider port:
- MediaPipe (Apache-2.0): face/pose/keypoint tracking helper (when installed).
- ONNX Runtime (MIT): general person detection inference engine with separately
  verified commercial-SaaS-safe model & weight licenses.
- OpenCV Haar/HOG fallback (Apache-2.0): lightweight CPU detector for sandbox
  unit/CI testing without downloading external weights.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import cv2
import numpy as np


@dataclass(frozen=True)
class DetectionBox:
    x: int
    y: int
    w: int
    h: int
    confidence: float
    kind: str  # "face" | "person"


class PersonDetectionProvider(Protocol):
    provider_name: str
    code_license: str
    weights_license: str

    def detect(self, frame_bgr: np.ndarray) -> list[DetectionBox]: ...


class OpenCVDefaultPersonProvider:
    """Zero-AGPL CPU baseline provider (OpenCV Apache-2.0) for CI & sandbox smoke tests.

    NOTE: Production reframing MUST pair MediaPipe (Apache-2.0) with a verified
    commercial-SaaS ONNX general person detector and pass Golden Media GM-15
    (multi-person, occlusion, fast motion, distant subjects, low light).
    """

    provider_name = "opencv-hog-person-baseline"
    code_license = "Apache-2.0"
    weights_license = "Apache-2.0 (OpenCV built-in default people detector)"

    def __init__(self) -> None:
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, frame_bgr: np.ndarray) -> list[DetectionBox]:
        if frame_bgr is None or frame_bgr.size == 0:
            return []
        rects, weights = self._hog.detectMultiScale(frame_bgr, winStride=(8, 8), padding=(4, 4), scale=1.05)
        boxes: list[DetectionBox] = []
        for (x, y, w, h), weight in zip(rects, weights):
            boxes.append(DetectionBox(x=int(x), y=int(y), w=int(w), h=int(h), confidence=float(weight), kind="person"))
        return boxes


class ONNXPersonDetectionProvider:
    """Pluggable ONNX Runtime provider (requires explicit commercial-safe model + weight license)."""

    def __init__(self, model_path: str, code_license: str, weights_license: str) -> None:
        import onnxruntime as ort  # verified installed in sandbox & production

        self.provider_name = f"onnxruntime:{model_path}"
        self.code_license = code_license
        self.weights_license = weights_license
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    def detect(self, frame_bgr: np.ndarray) -> list[DetectionBox]:
        raise NotImplementedError("Bind model-specific pre/post-processing once commercial ONNX weights are selected")
