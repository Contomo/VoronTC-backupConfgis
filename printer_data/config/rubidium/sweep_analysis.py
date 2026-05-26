#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from rubidium.analysis.analyzer import AnalysisConfig, analyze_session_json
from rubidium.analysis.image_processing import CropConfig, LaserExtractConfig


# /home/klippy/printer_data/config/rubidium/scan/recording_2025-12-22_21-21_111815
# /home/klippy/klippy-env/bin/python /home/klippy/rubidium/sweep_analysis.py --session /home/klippy/printer_data/config/rubidium/scan/recording_2025-12-22_21-21_111815/rubidium_scan_session.json --out /home/klippy/printer_data/config/rubidium/analysis

@dataclass(frozen=True)
class Candidate:
    name: str
    laser: LaserExtractConfig
    pipeline_steps: Optional[List[str]] = None


def _safe_name(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s).strip("_") or "run"


def _write_json(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def build_candidates() -> List[Candidate]:
    base_steps = ["crop", "brightness", "clahe", "blur", "threshold", "hsv_gate", "morph", "masked_gray", "centroid"]

    c: List[Candidate] = []

    # 1) Baseline (your defaults)
    c.append(Candidate(
        name="baseline_default",
        laser=LaserExtractConfig(),
        pipeline_steps=base_steps,
    ))

    # 2) Percentile tighter, no CLAHE, slightly gentler morph, stronger weighting
    c.append(Candidate(
        name="percentile_tight",
        laser=LaserExtractConfig(
            bright_percentile=99.5,
            use_clahe=False,
            morph_ksize=2,
            weight_power=6.0,
            median_ksize=5,
            blur_ksize=5,
            min_row_energy=1.0,
        ),
        pipeline_steps=base_steps,
    ))

    # 3) Percentile looser + mild CLAHE for low-light / low-contrast
    c.append(Candidate(
        name="percentile_looser_lowlight",
        laser=LaserExtractConfig(
            bright_percentile=99.2,
            use_clahe=True,
            clahe_clip=2.0,
            blur_ksize=5,
            morph_ksize=2,
            min_row_energy=0.5,
            weight_power=4.0,
            median_ksize=5,
        ),
        pipeline_steps=base_steps,
    ))

    # 4) Smooth centroid (more median + heavier intensity weighting)
    c.append(Candidate(
        name="smooth_centroid",
        laser=LaserExtractConfig(
            bright_percentile=-1.0,
            use_clahe=True,
            blur_ksize=5,
            morph_ksize=3,
            min_row_energy=1.0,
            weight_power=8.0,
            median_ksize=9,
        ),
        pipeline_steps=base_steps,
    ))

    # 5) No morph test (disable morph by setting k<=1; your StepMorph runs only if k>1)
    c.append(Candidate(
        name="no_morph_test",
        laser=LaserExtractConfig(
            bright_percentile=-1.0,
            use_clahe=True,
            blur_ksize=5,
            morph_ksize=1,
            min_row_energy=1.0,
            weight_power=4.0,
            median_ksize=5,
        ),
        pipeline_steps=base_steps,
    ))

    # 6) HSV gate (low-red)
    c.append(Candidate(
        name="hsv_low_red_gate",
        laser=LaserExtractConfig(
            hsv_lower=(0, 80, 140),
            hsv_upper=(10, 255, 255),
            bright_percentile=99.2,
            use_clahe=False,
            blur_ksize=5,
            morph_ksize=2,
            min_row_energy=0.5,
            weight_power=6.0,
            median_ksize=5,
        ),
        pipeline_steps=base_steps,
    ))

    # 7) HSV gate (high-red)
    c.append(Candidate(
        name="hsv_high_red_gate",
        laser=LaserExtractConfig(
            hsv_lower=(170, 80, 140),
            hsv_upper=(179, 255, 255),
            bright_percentile=99.2,
            use_clahe=False,
            blur_ksize=5,
            morph_ksize=2,
            min_row_energy=0.5,
            weight_power=6.0,
            median_ksize=5,
        ),
        pipeline_steps=base_steps,
    ))

    # 8) Pipeline A/B: drop CLAHE step entirely (same laser defaults)
    steps_no_clahe = [s for s in base_steps if s != "clahe"]
    c.append(Candidate(
        name="pipeline_no_clahe_step",
        laser=LaserExtractConfig(use_clahe=False),
        pipeline_steps=steps_no_clahe,
    ))

    # 9) Pipeline A/B: drop blur step entirely
    steps_no_blur = [s for s in base_steps if s != "blur"]
    c.append(Candidate(
        name="pipeline_no_blur_step",
        laser=LaserExtractConfig(blur_ksize=0),
        pipeline_steps=steps_no_blur,
    ))

    return c


def filter_session_json(src: Path, dst: Path, *, clip_idx: Optional[int]) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    if clip_idx is not None:
        clips = data.get("clips", [])
        data["clips"] = [c for c in clips if int(c.get("idx", -1)) == int(clip_idx)]
    _write_json(dst, data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=Path, required=True, help="Path to session.json")
    ap.add_argument("--out", type=Path, required=True, help="Output folder root")
    ap.add_argument("--frame-step", type=int, default=1, help="Analyze every Nth frame (1 = all)")
    ap.add_argument("--max-frames", type=int, default=0, help="Stop after N processed frames per clip (0 = unlimited)")
    ap.add_argument("--clip-idx", type=int, default=None, help="Only analyze a specific clip idx")
    args = ap.parse_args()

    session_json = args.session.resolve()
    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # Optionally filter clips
    filtered_json = out_root / "_session_filtered.json"
    filter_session_json(session_json, filtered_json, clip_idx=args.clip_idx)

    candidates = build_candidates()
    rows = []

    for i, cand in enumerate(candidates):
        run_name = f"{i:02d}_{_safe_name(cand.name)}"
        run_dir = out_root / run_name
        run_dir.mkdir(parents=True, exist_ok=True)

        cfg = AnalysisConfig(
            crop=CropConfig(),
            laser=cand.laser,
            frame_step=int(args.frame_step),
            max_frames=int(args.max_frames),
            write_plots=False,
            write_npz=True,
            output_dir=str(run_dir),
            pipeline_steps=cand.pipeline_steps,
        )

        # Save config used
        _write_json(run_dir / "config_used.json", {
            "name": cand.name,
            "frame_step": cfg.frame_step,
            "max_frames": cfg.max_frames,
            "pipeline_steps": cand.pipeline_steps,
            "laser": asdict(cand.laser),
            "crop": asdict(cfg.crop),
        })

        try:
            summary = analyze_session_json(filtered_json, cfg)
        except Exception as e:
            (run_dir / "ERROR.txt").write_text(str(e), encoding="utf-8")
            continue

        dash = run_dir / "analysis_dashboard.jpg"
        best = run_dir / "best_pa.txt"
        summ = run_dir / "summary.csv"

        rows.append({
            "name": cand.name,
            "dir": run_name,
            "dashboard": dash.name if dash.exists() else None,
            "best_pa": best.read_text(encoding="utf-8").strip() if best.exists() else None,
            "csv": summ.name if summ.exists() else None,
        })

    # Build a tiny index.html
    html = [
        "<html><head><meta charset='utf-8'><title>Rubidium sweep</title></head><body>",
        "<h2>Rubidium sweep</h2>",
        "<p>Each row is one config preset. Click dashboard to view full size.</p>",
        "<ul>",
    ]
    for r in rows:
        html.append("<li>")
        html.append(f"<b>{r['dir']}</b> — {r['name']}<br/>")
        if r["best_pa"] is not None:
            html.append(f"best_pa: <code>{r['best_pa']}</code><br/>")
        html.append(f"<a href='{r['dir']}/config_used.json'>config_used.json</a> ")
        if r["csv"]:
            html.append(f"| <a href='{r['dir']}/{r['csv']}'>summary.csv</a> ")
        if r["dashboard"]:
            html.append(f"| <a href='{r['dir']}/{r['dashboard']}'>analysis_dashboard.jpg</a><br/>")
            html.append(f"<a href='{r['dir']}/{r['dashboard']}'><img src='{r['dir']}/{r['dashboard']}' style='max-width:1200px; height:auto; border:1px solid #666'/></a>")
        html.append("</li><hr/>")
    html += ["</ul>", "</body></html>"]
    (out_root / "index.html").write_text("\n".join(html), encoding="utf-8")

    # keep the filtered json for reproducibility
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
