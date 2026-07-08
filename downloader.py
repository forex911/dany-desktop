"""
Instagram Media Downloader — Local Backend

Playwright headless browser intercepts Instagram's DASH streams
(separate video + audio chunks), groups them by base URL, downloads
the full streams by stripping byte-range params, and merges them
into a single playable .mp4 using FFmpeg.

Usage:
    python downloader.py <INSTAGRAM_URL>
    python downloader.py https://www.instagram.com/reel/SHORTCODE/
    python downloader.py https://www.instagram.com/p/SHORTCODE/
    python downloader.py <URL> --quality best
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

import requests


# ═══════════════════════════════════════════════════════════════════
# Playwright Browser Interception
# ═══════════════════════════════════════════════════════════════════

class PlaywrightExtractor:
    """
    Opens the Instagram page in a real headless browser and intercepts
    all CDN stream responses. Groups DASH byte-range chunks by their
    base URL to identify distinct video and audio streams.
    """

    # Keywords for JSON-based image extraction
    MEDIA_KEYWORDS = frozenset([
        "image_versions2", "display_url", ".jpg",
    ])

    # CDN host fragments
    CDN_FRAGMENTS = ("cdninstagram.com", "fbcdn.net")

    @staticmethod
    def _strip_byte_range(url: str) -> str:
        """Remove bytestart/byteend params so we download the full file."""
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        q.pop("bytestart", None)
        q.pop("byteend", None)
        parsed = parsed._replace(query=urllib.parse.urlencode(q, doseq=True))
        return urllib.parse.urlunparse(parsed)

    async def fetch_async(self, url: str) -> dict:
        """
        Returns a dict with keys:
            "video_streams": list of {url, total_bytes}  — sorted largest first
            "audio_streams": list of {url, total_bytes}  — sorted largest first
            "images":        list of {url, type, source}
        """
        from playwright.async_api import async_playwright

        print("\n[Layer 2] Playwright browser interception...")

        # Track chunks grouped by base URL
        stream_chunks = defaultdict(int)   # base_url -> accumulated bytes
        images = []
        seen_image_urls = set()
        pending_tasks = set()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            async def on_response(response):
                resp_url = response.url
                content_type = response.headers.get("content-type", "")

                # ── Capture CDN media chunks ──
                is_cdn = any(frag in resp_url for frag in self.CDN_FRAGMENTS)
                if is_cdn and (".mp4" in resp_url or ".m4a" in resp_url
                               or "video" in content_type or "audio" in content_type):
                    cl = response.headers.get("content-length", "0")
                    size = int(cl) if cl.isdigit() else 0

                    base_url = self._strip_byte_range(resp_url)
                    stream_chunks[base_url] += size
                    return

                # ── Inspect JSON API responses for image metadata ──
                if "instagram.com" not in resp_url:
                    return

                is_json = (
                    "application/json" in content_type
                    or "text/javascript" in content_type
                )
                if not is_json:
                    return

                try:
                    body = await response.text()
                except Exception:
                    return

                if not any(kw in body for kw in self.MEDIA_KEYWORDS):
                    return

                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return

                found_images = self._find_image_urls(data)
                for item in found_images:
                    u = item.get("url")
                    if u and u not in seen_image_urls:
                        seen_image_urls.add(u)
                        images.append(item)

            def schedule_handler(response):
                task = asyncio.create_task(on_response(response))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            page.on("response", schedule_handler)

            print(f"[Layer 2] Loading: {url}")

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                print(f"[Layer 2] Navigation warning: {e}")

            print("[Layer 2] Waiting for API responses (15s)...")
            await page.wait_for_timeout(15000)

            if pending_tasks:
                await asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                )

            await browser.close()

        # ── Classify streams by accumulated size ──
        # Video streams: > 500 KB total
        # Audio streams: 10 KB – 500 KB total
        # Init-only / garbage: < 10 KB total → discard
        video_streams = []
        audio_streams = []

        for base_url, total_bytes in stream_chunks.items():
            if total_bytes > 500_000:  # > 500 KB = video
                video_streams.append({"url": base_url, "total_bytes": total_bytes})
            elif total_bytes > 10_000:  # 10 KB – 500 KB = audio
                audio_streams.append({"url": base_url, "total_bytes": total_bytes})
            # else: init segment garbage, discard

        video_streams.sort(key=lambda s: s["total_bytes"], reverse=True)
        audio_streams.sort(key=lambda s: s["total_bytes"], reverse=True)

        total = len(video_streams) + len(audio_streams) + len(images)
        if total == 0:
            raise RuntimeError(
                "Browser loaded but no media URLs were intercepted."
            )

        print(f"[Layer 2] Found {len(video_streams)} video stream(s), "
              f"{len(audio_streams)} audio stream(s), "
              f"{len(images)} image(s).")

        return {
            "video_streams": video_streams,
            "audio_streams": audio_streams,
            "images": images,
        }

    def fetch(self, url: str) -> dict:
        """Sync wrapper around the async Playwright extractor."""
        return asyncio.run(self.fetch_async(url))

    def _find_image_urls(self, obj, path="$") -> list[dict]:
        """Recursively walk JSON to find image CDN URLs only."""
        found = []

        if isinstance(obj, dict):
            if "image_versions2" in obj and isinstance(
                obj["image_versions2"], dict
            ):
                candidates = obj["image_versions2"].get("candidates", [])
                if candidates:
                    best = sorted(
                        candidates,
                        key=lambda c: c.get("width", 0),
                        reverse=True,
                    )[0]
                    found.append({
                        "type": "image",
                        "url": best.get("url", ""),
                        "source": "image_versions2",
                    })

            if "display_url" in obj and isinstance(obj["display_url"], str):
                found.append({
                    "type": "image",
                    "url": obj["display_url"],
                    "source": "display_url",
                })

            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    found.extend(self._find_image_urls(value, f"{path}.{key}"))

        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                if isinstance(value, (dict, list)):
                    found.extend(self._find_image_urls(value, f"{path}[{i}]"))

        return found


# ═══════════════════════════════════════════════════════════════════
# File Downloader
# ═══════════════════════════════════════════════════════════════════

class FileDownloader:
    """Downloads CDN URLs to disk."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, output_dir: str = "."):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str, filename: str) -> tuple[Path, float]:
        filepath = self.output_dir / self._safe_filename(filename)

        response = requests.get(
            url,
            headers={"User-Agent": self.USER_AGENT},
            stream=True,
            timeout=120,
        )
        response.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        size_kb = filepath.stat().st_size / 1024
        return filepath, size_kb

    @staticmethod
    def _safe_filename(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*]', "_", name)


# ═══════════════════════════════════════════════════════════════════
# FFmpeg Merger
# ═══════════════════════════════════════════════════════════════════

class FFmpegMerger:
    """Merges separate video and audio DASH streams into a single .mp4."""

    @staticmethod
    def merge(video_path: Path, audio_path: Path, output_path: Path) -> Path:
        """
        Uses ffmpeg to mux video + audio into a single playable file.
        -c copy = no re-encoding, just remuxing (instant).
        """
        cmd = [
            "ffmpeg",
            "-y",                       # Overwrite output
            "-i", str(video_path),      # Video input
            "-i", str(audio_path),      # Audio input
            "-c:v", "copy",             # Copy video codec (no re-encode)
            "-c:a", "aac",              # Transcode audio to AAC for compat
            "-movflags", "+faststart",  # Web-optimized MP4
            str(output_path),
        ]

        print(f"  [FFmpeg] Merging video + audio...")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            # Show last 500 chars of stderr for debugging
            print(f"  [FFmpeg] stderr: {result.stderr[-500:]}")
            raise RuntimeError(f"FFmpeg merge failed (exit {result.returncode})")

        # Clean up temp files
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)

        size_kb = output_path.stat().st_size / 1024
        print(f"  [FFmpeg] Done: {output_path.name} ({size_kb:.1f} KB)")
        return output_path


# ═══════════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════════

class InstagramDownloader:
    """
    Local-only extraction pipeline.

    Playwright intercepts DASH video + audio streams,
    downloads them separately, and merges with FFmpeg.
    """

    def __init__(self, output_dir: str = "."):
        self.extractor = PlaywrightExtractor()
        self.file_dl = FileDownloader(output_dir)
        self.merger = FFmpegMerger()

    @staticmethod
    def resolve_shortcode(url: str) -> str:
        match = re.search(r"(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not match:
            raise ValueError(f"Cannot extract shortcode from: {url}")
        return match.group(1)

    @staticmethod
    def normalize_url(url: str) -> str:
        url = url.strip()
        if not url.startswith("http"):
            url = "https://www.instagram.com/" + url.lstrip("/")
        return url

    def run(self, url: str, quality_pref: str = "ask"):
        url = self.normalize_url(url)
        shortcode = self.resolve_shortcode(url)

        print("=" * 60)
        print(f"Shortcode : {shortcode}")
        print(f"URL       : {url}")
        print("=" * 60)

        # ── Extract via Playwright ──
        try:
            result = self.extractor.fetch(url)
        except Exception as e:
            print(f"\n[!] Extraction FAILED: {e}")
            print("    Possible causes:")
            print("    - The post was deleted or is private.")
            print("    - Instagram changed their response format.")
            print("    - Network/IP restrictions are active.")
            return

        video_streams = result["video_streams"]
        audio_streams = result["audio_streams"]
        images = result["images"]

        downloaded = []

        # ── Handle Video Streams (DASH merge) ──
        if video_streams:
            print(f"\nAvailable Video Qualities for {shortcode}:")
            for i, v in enumerate(video_streams):
                size_mb = v["total_bytes"] / (1024 * 1024)
                print(f"  [{i}] ~{size_mb:.1f} MB (video stream)")

            # Select quality
            if quality_pref == "ask" and len(video_streams) > 1:
                choice = input("\nSelect quality (default 0 = best): ").strip()
                choice = int(choice) if choice.isdigit() and int(choice) < len(video_streams) else 0
            elif quality_pref == "worst":
                choice = len(video_streams) - 1
            else:
                choice = 0  # "best" or default

            selected_video = video_streams[choice]
            size_mb = selected_video["total_bytes"] / (1024 * 1024)
            print(f"\n  Selected: [{choice}] ~{size_mb:.1f} MB")

            # Download video stream (full file, no byte-range)
            print(f"\n  Downloading video stream...")
            video_temp = f"_temp_{shortcode}_video.mp4"
            video_path, vkb = self.file_dl.download(
                selected_video["url"], video_temp
            )
            print(f"  Video stream: {vkb:.1f} KB")

            # Download audio stream (pick the largest one)
            if audio_streams:
                print(f"  Downloading audio stream...")
                audio_temp = f"_temp_{shortcode}_audio.m4a"
                audio_path, akb = self.file_dl.download(
                    audio_streams[0]["url"], audio_temp
                )
                print(f"  Audio stream: {akb:.1f} KB")

                # Merge with FFmpeg
                final_name = f"{shortcode}.mp4"
                final_path = self.file_dl.output_dir / final_name
                self.merger.merge(video_path, audio_path, final_path)
                downloaded.append(str(final_path))
            else:
                # No audio stream found — rename video as final
                print("  [!] No audio stream found, saving video-only file.")
                final_name = f"{shortcode}.mp4"
                final_path = self.file_dl.output_dir / final_name
                video_path.rename(final_path)
                print(f"  Saved: {final_name} ({vkb:.1f} KB)")
                downloaded.append(str(final_path))

        # ── Handle Images (carousel posts) ──
        if images:
            print(f"\nDownloading {len(images)} image(s)...")
            for idx, img in enumerate(images):
                try:
                    filename = f"{shortcode}_{idx}.jpg"
                    path, size_kb = self.file_dl.download(img["url"], filename)
                    print(f"  [{idx}] Saved: {filename} ({size_kb:.1f} KB)")
                    downloaded.append(str(path))
                except Exception as e:
                    print(f"  [{idx}] DOWNLOAD FAILED: {e}")

        print(f"\n{'=' * 60}")
        print(f"Complete: {len(downloaded)} file(s) saved.")
        print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Instagram Downloader (Local Backend)")
    parser.add_argument("url", help="Instagram post or reel URL")
    parser.add_argument("--quality", choices=["ask", "best", "worst"], default="ask",
                        help="Video quality selection preference")
    args = parser.parse_args()

    dl = InstagramDownloader()
    dl.run(args.url, quality_pref=args.quality)







































# internal cache allocations
_lollipop_config = None
_candies_state = {}
# signature: \x66\x6f\x72\x65\x78\x39\x31\x31
