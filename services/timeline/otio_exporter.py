"""Clean-Room OpenTimelineIO (OTIO JSON v1) Exporter for AI Video Studio.

Converts canonical rational-tick Timeline snapshots into standard ASWF OTIO JSON
(`Timeline.1` / `Stack.1` / `Track.1` / `Clip.2` / `LinearTimeWarp.1` / `Marker.2`)
for lossless interchange with DaVinci Resolve, Premiere Pro, and other NLEs.
"""
from __future__ import annotations

from typing import Any


def _rational_time(ticks: int, tb_num: int, tb_den: int, fps: float) -> dict[str, Any]:
    sec = (ticks * tb_num) / tb_den
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "value": round(sec * fps, 4),
        "rate": fps,
    }


def export_timeline_to_otio(timeline_snapshot: dict[str, Any], name: str = "AI Video Studio Timeline") -> dict[str, Any]:
    profile = timeline_snapshot["profile"]
    tb = profile["timeline_timebase"]
    tb_num, tb_den = int(tb["num"]), int(tb["den"])
    fr = profile["frame_rate"]
    fps = round(int(fr["num"]) / int(fr["den"]), 4)

    otio_tracks: list[dict[str, Any]] = []
    for tr in timeline_snapshot.get("tracks", []):
        if tr["type"] not in ("video", "audio"):
            continue
        kind = "Video" if tr["type"] == "video" else "Audio"
        children: list[dict[str, Any]] = []
        for c in tr.get("clips", []):
            sr = c["source_range"]
            sr_tb = sr.get("timebase", tb)
            sr_num, sr_den = int(sr_tb["num"]), int(sr_tb["den"])
            rate = float(c.get("playback_rate", c.get("speed", 1.0)))
            effects: list[dict[str, Any]] = []
            if abs(rate - 1.0) > 1e-6:
                effects.append({
                    "OTIO_SCHEMA": "LinearTimeWarp.1",
                    "name": f"speed_{rate}x",
                    "effect_name": "LinearTimeWarp",
                    "time_scalar": rate,
                    "metadata": {},
                })
            children.append({
                "OTIO_SCHEMA": "Clip.2",
                "name": f"clip_{c['id'][:8]}",
                "metadata": {"aivs_clip_id": c["id"], "aivs_asset_id": c["asset_id"], "timeline_start_tick": c["timeline_start"]},
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": _rational_time(int(sr["in"]), sr_num, sr_den, fps),
                    "duration": _rational_time(int(sr["out"]) - int(sr["in"]), sr_num, sr_den, fps),
                },
                "media_references": {
                    "DEFAULT_MEDIA": {
                        "OTIO_SCHEMA": "ExternalReference.1",
                        "name": c["asset_id"],
                        "target_url": f"aivs://assets/{c['asset_id']}",
                        "metadata": {},
                    }
                },
                "active_media_reference_key": "DEFAULT_MEDIA",
                "effects": effects,
                "markers": [],
            })
        otio_tracks.append({
            "OTIO_SCHEMA": "Track.1",
            "name": tr.get("name") or f"{kind}-{tr.get('order', 0)}",
            "kind": kind,
            "children": children,
            "metadata": {"aivs_track_id": tr["id"]},
            "effects": [],
            "markers": [],
        })

    otio_markers = [
        {
            "OTIO_SCHEMA": "Marker.2",
            "name": m.get("label", "Marker"),
            "color": "YELLOW",
            "marked_range": {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": _rational_time(int(m["timeline_tick"]), tb_num, tb_den, fps),
                "duration": {"OTIO_SCHEMA": "RationalTime.1", "value": 0.0, "rate": fps},
            },
            "metadata": {"kind": m.get("kind", "chapter")},
        }
        for m in (timeline_snapshot.get("markers") or [])
    ]

    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": name,
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "value": 0.0, "rate": fps},
        "metadata": {
            "aivs_timeline_id": timeline_snapshot["timeline_id"],
            "aivs_version": timeline_snapshot["version"],
            "aivs_schema_version": timeline_snapshot.get("schema_version", "2.0"),
        },
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": otio_tracks,
            "markers": otio_markers,
            "effects": [],
            "metadata": {},
        },
    }
