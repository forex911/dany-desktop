"""
fetch_video_info.py — Electron ↔ Python bridge (Multi-Platform)

Routes URLs to the correct platform downloader:
  YouTube  → downloaders.youtube.fetch_youtube_info
  Spotify  → downloaders.spotify.fetch_spotify_info
  Instagram → downloaders.instagram.fetch_instagram_info
  Pinterest → downloaders.pinterest.fetch_pinterest_info

stdout → JSON only (Electron parses this)
stderr → debug logs (Electron shows in console)

Usage: python fetch_video_info.py <URL>
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
from downloaders.youtube import fetch_youtube_info
from downloaders.spotify import fetch_spotify_info
from downloaders.instagram import fetch_instagram_info
from downloaders.pinterest import fetch_pinterest_info


def log(msg):
    """Debug log — always goes to stderr"""
    print(f"[fetch_video_info] {msg}", flush=True)


def write_json(obj):
    """Write ONLY JSON to the real stdout — the only thing Electron parses"""
    _real_stdout.write(json.dumps(obj) + "\n")
    _real_stdout.flush()


def detect_platform(url):
    """Detect platform from URL using simple pattern matching"""
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
# STEP 4: Route to correct platform and return result
# ═══════════════════════════════════════════════════════════
try:
    if len(sys.argv) < 2:
        raise ValueError("No URL provided. Usage: python fetch_video_info.py <URL>")

    url = sys.argv[1]
    platform = detect_platform(url)

    log(f"URL received: {url}")
    log(f"Platform detected: {platform}")

    if platform == "youtube":
        log("Calling fetch_youtube_info()...")
        result = fetch_youtube_info(url)

    elif platform == "spotify":
        log("Calling fetch_spotify_info()...")
        result = fetch_spotify_info(url)

    elif platform == "instagram":
        log("Calling fetch_instagram_info()...")
        result = fetch_instagram_info(url)

    elif platform == "pinterest":
        log("Calling fetch_pinterest_info()...")
        result = fetch_pinterest_info(url)

    else:
        result = {
            "success": False,
            "error": f"Unsupported platform. URL: {url}"
        }

    # Add platform tag to response
    if "platform" not in result:
        result["platform"] = platform

    log(f"Result success: {result.get('success')}")
    if result.get("title"):
        log(f"Title: {result['title']}")
    if result.get("quality"):
        log(f"Quality: {result['quality']}")

    # ── Only this goes to real stdout ──
    write_json(result)

except Exception as e:
    log(f"FATAL ERROR: {e}")
    write_json({
        "success": False,
        "error": str(e)
    })