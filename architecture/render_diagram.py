#!/usr/bin/env python3
"""Render architecture/diagram.html: a static PNG for the repo, or a frame-by-frame video.

Usage:
  python architecture/render_diagram.py --static architecture/architecture.png
  python architecture/render_diagram.py --video demo/out/anim/architecture.mp4 --seconds 46 --fps 30
  python architecture/render_diagram.py --frames /tmp/arch-frames --at 0.1,0.3,0.5

The page exposes window.seek(ms) as a pure function of time, so every frame is exact and the
render is repeatable. Frames are screenshotted at 1920x1080 and encoded with ffmpeg.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "architecture" / "diagram.html").resolve().as_uri()
VIEWPORT = {"width": 1920, "height": 1080}


def open_page(p, static: bool):
    browser = p.chromium.launch()
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    page.goto(PAGE + ("?static=1" if static else "?autoplay=0"))
    page.wait_for_timeout(250)
    return browser, page


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static")
    ap.add_argument("--video")
    ap.add_argument("--frames", help="directory for preview PNGs")
    ap.add_argument("--at", default="0.1,0.3,0.5,0.7,0.9", help="fractions for --frames")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if args.static:
            browser, page = open_page(p, True)
            Path(args.static).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=args.static)
            browser.close()
            print(f"[arch] wrote {args.static}")
        if args.frames:
            browser, page = open_page(p, False)
            page.evaluate("ms => window.setDuration(ms)", int(args.seconds * 1000))
            out = Path(args.frames); out.mkdir(parents=True, exist_ok=True)
            for frac in [float(x) for x in args.at.split(",")]:
                page.evaluate("ms => window.seek(ms)", int(frac * args.seconds * 1000))
                dest = out / f"at_{int(round(frac * 100)):03d}.png"
                page.screenshot(path=str(dest))
                print(f"[arch] wrote {dest}")
            browser.close()
        if args.video:
            browser, page = open_page(p, False)
            total_ms = int(args.seconds * 1000)
            page.evaluate("ms => window.setDuration(ms)", total_ms)
            n = int(round(args.seconds * args.fps))
            t_start = time.time()
            with tempfile.TemporaryDirectory(prefix="arch_frames_") as tmp:
                for i in range(n):
                    page.evaluate("ms => window.seek(ms)", min(total_ms, int(round(i * 1000 / args.fps))))
                    page.screenshot(path=f"{tmp}/f_{i:05d}.png")
                    if i % 300 == 0:
                        print(f"[arch] frame {i}/{n}", flush=True)
                browser.close()
                dest = Path(args.video); dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(args.fps),
                     "-i", f"{tmp}/f_%05d.png", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest)],
                    check=True,
                )
            print(f"[arch] wrote {args.video} ({n} frames in {time.time() - t_start:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
