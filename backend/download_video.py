"""
download_video.py — Electron ↔ Python download bridge (Multi-Platform)

Routes URLs to the correct platform downloader:
  YouTube   → downloaders.youtube.download_youtube
  Spotify   → downloaders.spotify.download_spotify
  Instagram → downloaders.instagram.download_instagram
  Pinterest → downloaders.pinterest.download_pinterest

stdout → JSON only (Electron parses this)
stderr → debug logs + PROGRESS: lines (Electron reads for progress bar)

Usage: python download_video.py <URL> <FOLDER> <FORMAT_ID> [TASK_ID]
"""

import sys
import os
import re

# ═══════════════════════════════════════════════════════════
# STEP 1: Capture REAL stdout, redirect all print() → stderr
# ═══════════════════════════════════════════════════════════
_real_stdout = sys.stdout
sys.stdout = sys.stderr

# ═══════════════════════════════════════════════════════════
# STEP 2: Fix Python path for relative imports
# ═══════════════════════════════════════════════════════════
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════════════════════
# STEP 3: Import ALL platform downloaders
# ═══════════════════════════════════════════════════════════
import json
from downloaders.youtube import download_youtube
from downloaders.spotify import download_spotify
from downloaders.instagram import download_instagram
from downloaders.pinterest import download_pinterest


def log(msg):
    """Debug log — always goes to stderr"""
    print(f"[download_video] {msg}", flush=True)


def write_json(obj):
    """Write ONLY JSON to real stdout — Electron parses this"""
    _real_stdout.write(json.dumps(obj) + "\n")
    _real_stdout.flush()


def progress_callback(percent):
    """Legacy progress callback — keeps backward compat."""
    print(f"PROGRESS:{percent:.1f}", flush=True)


def rich_progress_hook(d):
    """
    yt-dlp progress_hook — emits structured DLPROGRESS JSON to stderr.
    Electron main.js parses this for speed, ETA, file size display.
    """
    status = d.get("status", "")

    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")
        percent = (downloaded / total * 100) if total > 0 else 0

        def fmt_size(b):
            if not b or b <= 0: return "?"
            if b < 1024**2: return f"{b/1024:.0f} KB"
            if b < 1024**3: return f"{b/1024**2:.1f} MB"
            return f"{b/1024**3:.2f} GB"

        def fmt_speed(s):
            if not s or s <= 0: return "—"
            if s < 1024**2: return f"{s/1024:.0f} KB/s"
            return f"{s/1024**2:.1f} MB/s"

        def fmt_eta(e):
            if not e or e <= 0: return "—"
            m, s = divmod(int(e), 60)
            h, m = divmod(m, 60)
            if h > 0: return f"{h}:{m:02d}:{s:02d}"
            return f"{m}:{s:02d}"

        info_dict = d.get("info_dict", {})
        item = info_dict.get("title", "")
        if not item:
            filename = d.get("filename", "")
            if filename:
                import os
                item = os.path.basename(filename)

        info = json.dumps({"speed": fmt_speed(speed), "eta": fmt_eta(eta), "downloaded": fmt_size(downloaded), "total": fmt_size(total), "stage": "downloading", "item": item})
        print(f"DLPROGRESS:{info}", flush=True)

    elif status == "finished":
        info_dict = d.get("info_dict", {})
        item = info_dict.get("title", "")
        if not item:
            filename = d.get("filename", "")
            if filename:
                import os
                item = os.path.basename(filename)

        info = json.dumps({"speed": "—", "eta": "—", "downloaded": "—", "total": "—", "stage": "merging", "item": item})
        print(f"DLPROGRESS:{info}", flush=True)


def postprocessor_hook(d):
    """Emits stage updates during post-processing (merge, convert)."""
    status = d.get("status", "")
    pp = d.get("postprocessor", "")
    info_dict = d.get("info_dict", {})
    item = info_dict.get("title", "")
    if not item:
        filename = d.get("info", {}).get("filepath", "")
        if filename:
            import os
            item = os.path.basename(filename)

    if status == "started":
        stage = "merging" if "Merge" in pp or "ffmpeg" in pp.lower() else "processing"
        info = json.dumps({"speed": "—", "eta": "—", "downloaded": "—", "total": "—", "stage": stage, "item": item})
        print(f"DLPROGRESS:{info}", flush=True)
    elif status == "finished":
        info = json.dumps({"speed": "—", "eta": "—", "downloaded": "—", "total": "—", "stage": "finalizing", "item": item})
        print(f"DLPROGRESS:{info}", flush=True)


def detect_platform(url):
    """Detect platform from URL"""
    url_lower = url.lower()
    if re.search(r'(youtube\.com|youtu\.be|youtube-nocookie\.com)', url_lower):
        return "youtube"
    elif re.search(r'(open\.spotify\.com|spotify\.link)', url_lower):
        return "spotify"
    elif re.search(r'(instagram\.com|instagr\.am)', url_lower):
        return "instagram"
    elif re.search(r'(pinterest\.com|pin\.it|pinterest\.\w+)', url_lower):
        return "pinterest"
    else:
        return "unknown"


# ═══════════════════════════════════════════════════════════
# STEP 4: Route to correct platform and execute download
# ═══════════════════════════════════════════════════════════
try:
    if len(sys.argv) < 4:
        raise ValueError("Usage: python download_video.py <URL> <FOLDER> <FORMAT_ID> [TASK_ID]")

    url = sys.argv[1]
    folder = sys.argv[2]
    format_id = sys.argv[3]
    task_id = sys.argv[4] if len(sys.argv) > 4 else None
    platform = detect_platform(url)

    log(f"URL: {url}")
    log(f"Platform: {platform}")
    log(f"Download folder: {folder}")
    log(f"Format ID: {format_id}")
    log(f"Task ID: {task_id}")

    if platform == "youtube":
        log("Calling download_youtube()...")
        result = download_youtube(
            url=url,
            folder=folder,
            progress_callback=progress_callback,
            format_id=format_id,
            task_id=task_id,
            extra_progress_hooks=[rich_progress_hook],
            extra_postprocessor_hooks=[postprocessor_hook]
        )

    elif platform == "spotify":
        log("Calling download_spotify()...")
        result = download_spotify(
            url=url,
            folder=folder,
            progress_callback=progress_callback,
            format_id=format_id,
            task_id=task_id,
            extra_progress_hooks=[rich_progress_hook],
            extra_postprocessor_hooks=[postprocessor_hook]
        )

    elif platform == "instagram":
        log("Calling download_instagram()...")
        result = download_instagram(
            url=url,
            download_folder=folder,
            progress_callback=progress_callback,
            format_id=format_id,
            task_id=task_id,
            extra_progress_hooks=[rich_progress_hook],
            extra_postprocessor_hooks=[postprocessor_hook]
        )

    elif platform == "pinterest":
        log("Calling download_pinterest()...")
        result = download_pinterest(
            url=url,
            download_folder=folder,
            progress_callback=progress_callback,
            format_id=format_id,
            task_id=task_id,
            extra_progress_hooks=[rich_progress_hook],
            extra_postprocessor_hooks=[postprocessor_hook]
        )

    else:
        result = {
            "success": False,
            "error": f"Unsupported platform. URL: {url}"
        }

    # Add platform tag
    if "platform" not in result:
        result["platform"] = platform

    log(f"Result success: {result.get('success')}")
    if result.get("filename"):
        log(f"Filename: {result['filename']}")
    if result.get("title"):
        log(f"Title: {result['title']}")

    # ── Only this goes to real stdout ──
    write_json(result)

except Exception as e:
    log(f"FATAL ERROR: {e}")
    write_json({
        "success": False,
        "error": str(e)
    })
