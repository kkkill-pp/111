#!/usr/bin/env python3
"""Phase 0 Real Media Performance Benchmark (FFmpeg-only V1 Baseline).

Measures real wall-clock time, RTF (real-time factor), and ffprobe accuracy for:
1. Synthetic 1080p@30fps 10s H.264/AAC source generation
2. 720p proxy transcode (editor preview pipeline)
3. Filmstrip thumbnail extraction (10 frames)
4. 2.0x playback_rate + libass ASS subtitle burn-in export (RenderPlan v3.0 path)
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path


def run_timed(cmd: list[str]) -> float:
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return round(time.perf_counter() - t0, 4)


def main() -> None:
    out_log = Path("logs/media_benchmark_v3.json")
    out_log.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src_1080p = tmp_dir / "src_1080p_10s.mp4"
        proxy_720p = tmp_dir / "proxy_720p.mp4"
        thumb_pat = tmp_dir / "thumb_%02d.jpg"
        ass_path = tmp_dir / "sub.ass"
        final_out = tmp_dir / "final_2x_sub.mp4"

        ass_path.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,54,&H00FFFFFF,&H00000000,&H64000000,-1,0,1,3,1,2,40,40,60,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.50,0:00:04.50,Default,,0,0,0,,AI Video Studio v3.0 Benchmark\n",
            encoding="utf-8",
        )

        t_gen = run_timed([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=10:size=1920x1080:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10:sample_rate=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src_1080p),
        ])

        t_proxy = run_timed([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src_1080p),
            "-vf", "scale=1280:720",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", str(proxy_720p),
        ])

        t_thumbs = run_timed([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src_1080p),
            "-vf", "fps=1,scale=320:-1", "-q:v", "4", str(thumb_pat),
        ])
        thumb_count = len(list(tmp_dir.glob("thumb_*.jpg")))

        t_export = run_timed([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src_1080p),
            "-filter_complex", f"[0:v]setpts=PTS/2.0,ass={ass_path}[v];[0:a]atempo=2.0[a]",
            "-map", "[v]", "-map", "[a]",
            "-t", "5.000000",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(final_out),
        ])

        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,width,height",
            "-of", "json", str(final_out),
        ], text=True))

        result = {
            "source_spec": "1920x1080@30fps H.264/AAC 10.0s",
            "metrics": {
                "generate_1080p_10s_sec": t_gen,
                "transcode_720p_proxy_sec": t_proxy,
                "proxy_realtime_factor": round(10.0 / t_proxy, 2) if t_proxy > 0 else 0,
                "extract_10_thumbnails_sec": t_thumbs,
                "thumbnails_generated": thumb_count,
                "export_1080p_2x_ass_burnin_sec": t_export,
                "export_realtime_factor": round(5.0 / t_export, 2) if t_export > 0 else 0,
                "exported_duration_sec": round(float(probe["format"]["duration"]), 4),
                "exported_duration_error_ms": round(abs(float(probe["format"]["duration"]) - 5.0) * 1000, 2),
                "exported_size_bytes": int(probe["format"]["size"]),
            },
        }
        out_log.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
