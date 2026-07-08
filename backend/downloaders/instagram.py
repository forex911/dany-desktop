"""
Instagram Media Downloader — Playwright DASH Interception Backend

Playwright headless browser intercepts Instagram's DASH streams
(separate video + audio chunks), groups them by base URL, downloads
the full streams by stripping byte-range params, and merges them
into a single playable .mp4 using FFmpeg.

Exposes the same API surface as the old yt-dlp-based module:
  - fetch_instagram_info(url)
  - download_instagram(url, download_folder, ...)
  - download_instagram_item_by_index(post_url, item_index, download_folder, ...)
  - download_instagram_zip(post_url, total_items, download_folder, ...)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import zipfile
import threading
from collections import defaultdict
from pathlib import Path

import requests

# PIL is optional — used for webp→jpg conversion
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[Instagram] PIL not installed — webp/png→jpg conversion disabled")

instagram_lock = threading.Lock()


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

        print("\n[Instagram] Playwright browser interception...")

        # Track chunks grouped by base URL
        stream_chunks = defaultdict(int)   # base_url -> accumulated bytes
        images = []
        seen_image_urls = set()
        pending_tasks = set()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="msedge", args=["--headless=new", "--window-position=-32000,-32000"])
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

            print(f"[Instagram] Loading: {url}")

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                print(f"[Instagram] Navigation warning: {e}")

            print("[Instagram] Waiting for API responses (dynamic up to 15s)...")
            for i in range(15):
                await page.wait_for_timeout(1000)
                has_video = any(".mp4" in k or "video" in k for k in stream_chunks)
                has_audio = any(".m4a" in k or "audio" in k for k in stream_chunks)
                if has_video and has_audio and i >= 3:
                    print(f"[Instagram] Streams detected early after {i+1}s!")
                    break

            try:
                page_title = await page.title()
                og_image = await page.evaluate('() => { const meta = document.querySelector(\'meta[property="og:image"]\'); return meta ? meta.content : null; }')
            except Exception:
                page_title = ""
                og_image = None

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

        print(f"[Instagram] Found {len(video_streams)} video stream(s), "
              f"{len(audio_streams)} audio stream(s), "
              f"{len(images)} image(s).")

        return {
            "video_streams": video_streams,
            "audio_streams": audio_streams,
            "images": images,
            "page_title": page_title,
            "og_image": og_image,
        }

    def fetch(self, url: str) -> dict:
        """Sync wrapper around the async Playwright extractor."""
        # Use a new event loop to avoid conflicts with any existing loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an existing event loop — run in a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(lambda: asyncio.run(self.fetch_async(url))).result()
        else:
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
# File Downloader (CDN URLs → Disk)
# ═══════════════════════════════════════════════════════════════════

class FileDownloader:
    """Downloads CDN URLs to disk with progress reporting."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, output_dir: str = "."):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str, filename: str, progress_callback=None) -> tuple:
        """
        Download a URL to disk. Returns (filepath, size_kb).
        Optionally reports progress via callback(percent).
        """
        filepath = self.output_dir / self._safe_filename(filename)

        response = requests.get(
            url,
            headers={"User-Agent": self.USER_AGENT},
            stream=True,
            timeout=120,
        )
        response.raise_for_status()

        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total > 0:
                    progress_callback((downloaded / total) * 100)

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
        # Resolve FFmpeg path — prefer app-managed binary
        ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
        ffmpeg_exe = "ffmpeg"
        if ffmpeg_dir:
            candidate = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if os.path.exists(candidate):
                ffmpeg_exe = candidate

        cmd = [
            ffmpeg_exe,
            "-y",                       # Overwrite output
            "-i", str(video_path),      # Video input
            "-i", str(audio_path),      # Audio input
            "-c:v", "copy",             # Copy video codec (no re-encode)
            "-c:a", "aac",              # Transcode audio to AAC for compat
            "-movflags", "+faststart",  # Web-optimized MP4
            "-metadata", "encoded_by=forex911",  # Secret branding
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
            print(f"  [FFmpeg] stderr: {result.stderr[-500:]}")
            raise RuntimeError(f"FFmpeg merge failed (exit {result.returncode})")

        # Clean up temp files
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)

        size_kb = output_path.stat().st_size / 1024
        print(f"  [FFmpeg] Done: {output_path.name} ({size_kb:.1f} KB)")
        return output_path


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _resolve_shortcode(url: str) -> str:
    """Extract Instagram shortcode from URL."""
    match = re.search(r"(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Cannot extract shortcode from: {url}")
    return match.group(1)


def _normalize_url(url: str) -> str:
    """Ensure URL is a full Instagram URL."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://www.instagram.com/" + url.lstrip("/")
    return url


def _convert_webp_to_jpg(filepath: str) -> str:
    """Convert webp/png to jpg if PIL is available. Returns final path."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in [".webp", ".png"] and HAS_PIL:
        jpg_file = filepath.rsplit(".", 1)[0] + ".jpg"
        try:
            with Image.open(filepath) as img:
                img.convert("RGB").save(jpg_file, "JPEG")
            os.remove(filepath)
            return jpg_file
        except Exception:
            pass
    return filepath


def _check_ffmpeg():
    """Check that FFmpeg is available."""
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(ffmpeg_exe):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — fetch_instagram_info
# ═══════════════════════════════════════════════════════════════════

def fetch_instagram_info(url):
    """
    Fetch media info for an Instagram post/reel.
    Returns the same structure as the old yt-dlp version so the
    Electron frontend doesn't need any changes.
    """
    try:
        url = _normalize_url(url)
        shortcode = _resolve_shortcode(url)
        print(f"[Instagram] fetch_instagram_info — shortcode: {shortcode}")

        extractor = PlaywrightExtractor()
        result = extractor.fetch(url)

        video_streams = result["video_streams"]
        audio_streams = result["audio_streams"]
        images = result["images"]
        page_title = result.get("page_title", "")
        og_image = result.get("og_image")

        media_items = []
        idx = 0

        # Add video items
        for v in video_streams:
            size_mb = v["total_bytes"] / (1024 * 1024)
            media_items.append({
                "type": "video",
                "thumbnail": og_image,  # Playwright extracted from meta tag
                "index": idx + 1,
                "title": page_title if page_title else f"Video {idx + 1} (~{size_mb:.1f} MB)",
            })
            idx += 1

        # Add image items
        for img in images:
            media_items.append({
                "type": "image",
                "thumbnail": img.get("url"),
                "index": idx + 1,
                "title": f"Image {idx + 1}",
            })
            idx += 1

        if not media_items:
            raise ValueError("No media items found in post")

        is_carousel = len(media_items) > 1
        title = page_title if page_title else f"Instagram Post {shortcode}"
        if len(title) > 60:
            title = title[:57] + "..."

        top_thumb = og_image
        if not top_thumb:
            for item in media_items:
                if item.get("thumbnail"):
                    top_thumb = item["thumbnail"]
                    break

        # Build quality formats based on available video streams
        formats = []
        if video_streams:
            # Best quality (largest stream)
            formats.append({
                "format_id": "best",
                "label": "Best Quality",
                "ext": "mp4",
                "type": "video"
            })
            # If multiple video streams, offer worst quality too
            if len(video_streams) > 1:
                formats.append({
                    "format_id": "worst",
                    "label": "Lowest Quality",
                    "ext": "mp4",
                    "type": "video"
                })
        elif images:
            formats.append({
                "format_id": "best",
                "label": "Original Quality",
                "ext": "jpg",
                "type": "image"
            })

        return {
            "success": True,
            "title": title,
            "thumbnail": top_thumb,
            "is_carousel": is_carousel,
            "media_items": media_items,
            "formats": formats,
            "platform": "instagram",
        }

    except Exception as e:
        print(f"[Instagram] fetch_instagram_info failed: {e}")
        return {"success": False, "error": str(e), "platform": "instagram"}


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — download_instagram
# ═══════════════════════════════════════════════════════════════════

def download_instagram(url, download_folder, progress_callback=None,
                       format_id="best", task_id=None,
                       extra_progress_hooks=None,
                       extra_postprocessor_hooks=None):
    """
    Download the entire Instagram post (single video/image or whole post).
    Same function signature as the old yt-dlp version.
    """
    if not _check_ffmpeg():
        return {
            "success": False,
            "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus.",
            "platform": "instagram"
        }

    os.makedirs(download_folder, exist_ok=True)

    with instagram_lock:
        try:
            url = _normalize_url(url)
            shortcode = _resolve_shortcode(url)
            print(f"[Instagram] download_instagram — shortcode: {shortcode}")

            # Emit progress: extracting
            if extra_progress_hooks:
                for hook in extra_progress_hooks:
                    try:
                        hook({
                            "status": "downloading",
                            "total_bytes": 0,
                            "downloaded_bytes": 0,
                            "speed": None,
                            "eta": None,
                            "info_dict": {"title": f"Extracting {shortcode}..."},
                        })
                    except Exception:
                        pass

            extractor = PlaywrightExtractor()
            result = extractor.fetch(url)

            video_streams = result["video_streams"]
            audio_streams = result["audio_streams"]
            images = result["images"]

            file_dl = FileDownloader(download_folder)
            merger = FFmpegMerger()
            downloaded_files = []

            tag = task_id or str(int(time.time()))

            page_title = result.get("page_title", "")
            base_name = (page_title[:60].strip() if page_title else shortcode)

            # ── Select quality ──
            quality_choice = 0  # default = best (largest)
            if format_id == "worst" and video_streams:
                quality_choice = len(video_streams) - 1

            # ── Handle Video Streams (DASH merge) ──
            if video_streams:
                selected_video = video_streams[quality_choice]
                size_mb = selected_video["total_bytes"] / (1024 * 1024)
                print(f"[Instagram] Selected video stream: ~{size_mb:.1f} MB")

                # Download video stream
                def video_progress(pct):
                    if progress_callback:
                        # Scale to 0-60% (video is ~60% of work)
                        progress_callback(pct * 0.6)
                    if extra_progress_hooks:
                        est_total = selected_video["total_bytes"]
                        for hook in extra_progress_hooks:
                            try:
                                hook({
                                    "status": "downloading",
                                    "total_bytes": est_total,
                                    "downloaded_bytes": int(est_total * pct / 100),
                                    "speed": None,
                                    "eta": None,
                                    "info_dict": {"title": f"{shortcode} (video)"},
                                })
                            except Exception:
                                pass

                video_temp = f"_temp_{shortcode}_{tag}_video.mp4"
                video_path, vkb = file_dl.download(
                    selected_video["url"], video_temp, progress_callback=video_progress
                )
                print(f"[Instagram] Video stream: {vkb:.1f} KB")

                # Download audio stream
                if audio_streams:
                    def audio_progress(pct):
                        if progress_callback:
                            # Scale to 60-90% (audio is ~30% of work)
                            progress_callback(60 + pct * 0.3)
                        if extra_progress_hooks:
                            est_total = audio_streams[0]["total_bytes"]
                            for hook in extra_progress_hooks:
                                try:
                                    hook({
                                        "status": "downloading",
                                        "total_bytes": est_total,
                                        "downloaded_bytes": int(est_total * pct / 100),
                                        "speed": None,
                                        "eta": None,
                                        "info_dict": {"title": f"{shortcode} (audio)"},
                                    })
                                except Exception:
                                    pass

                    audio_temp = f"_temp_{shortcode}_{tag}_audio.m4a"
                    audio_path, akb = file_dl.download(
                        audio_streams[0]["url"], audio_temp, progress_callback=audio_progress
                    )
                    print(f"[Instagram] Audio stream: {akb:.1f} KB")

                    # Emit merge stage
                    if extra_postprocessor_hooks:
                        for hook in extra_postprocessor_hooks:
                            try:
                                hook({
                                    "status": "started",
                                    "postprocessor": "FFmpeg Merger",
                                    "info_dict": {"title": shortcode},
                                })
                            except Exception:
                                pass

                    # Merge with FFmpeg
                    final_name = f"{base_name}_{tag}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    merger.merge(video_path, audio_path, final_path)
                    downloaded_files.append(final_path.name)

                    if extra_postprocessor_hooks:
                        for hook in extra_postprocessor_hooks:
                            try:
                                hook({
                                    "status": "finished",
                                    "postprocessor": "FFmpeg Merger",
                                    "info_dict": {"title": shortcode},
                                })
                            except Exception:
                                pass
                else:
                    # No audio — add metadata via FFmpeg
                    print("[Instagram] No audio stream found, saving video-only file with FFmpeg.")
                    final_name = f"{base_name}_{tag}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    
                    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
                    ffmpeg_exe = "ffmpeg"
                    if ffmpeg_dir:
                        candidate = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                        if os.path.exists(candidate):
                            ffmpeg_exe = candidate
                    
                    subprocess.run([
                        ffmpeg_exe, "-y", "-i", str(video_path),
                        "-c:v", "copy",
                        "-metadata", "encoded_by=forex911",
                        str(final_path)
                    ], capture_output=True)
                    
                    video_path.unlink(missing_ok=True)
                    downloaded_files.append(final_path.name)

            # ── Handle Images ──
            if images:
                total_images = len(images)
                for img_idx, img in enumerate(images):
                    try:
                        def img_progress(pct, _idx=img_idx):
                            if progress_callback:
                                # Images share the 90-100% range
                                base = 90 + (_idx / total_images) * 10
                                progress_callback(base + pct * (10 / total_images) / 100)

                        filename = f"{base_name}_{tag}_{img_idx}.jpg"
                        filepath, size_kb = file_dl.download(
                            img["url"], filename, progress_callback=img_progress
                        )
                        # Convert webp/png to jpg
                        final_path = _convert_webp_to_jpg(str(filepath))
                        downloaded_files.append(os.path.basename(final_path))
                        print(f"[Instagram] Image [{img_idx}]: {os.path.basename(final_path)} ({size_kb:.1f} KB)")
                    except Exception as e:
                        print(f"[Instagram] Image [{img_idx}] FAILED: {e}")

            if progress_callback:
                progress_callback(100)

            # Emit finished
            if extra_progress_hooks:
                for hook in extra_progress_hooks:
                    try:
                        hook({
                            "status": "finished",
                            "info_dict": {"title": shortcode},
                        })
                    except Exception:
                        pass

            if not downloaded_files:
                return {
                    "success": False,
                    "error": "No media could be downloaded",
                    "platform": "instagram"
                }

            return {
                "success": True,
                "files": downloaded_files,
                "filename": downloaded_files[0],
                "title": f"Instagram Post {shortcode}",
                "download_url": f"/file/{downloaded_files[0]}",
                "platform": "instagram",
            }

        except Exception as e:
            print(f"[Instagram] ❌ download_instagram failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "platform": "instagram"
            }


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — download_instagram_item_by_index
# ═══════════════════════════════════════════════════════════════════

def download_instagram_item_by_index(post_url, item_index, download_folder,
                                     progress_callback=None, task_id=None,
                                     extra_progress_hooks=None,
                                     extra_postprocessor_hooks=None):
    """
    Download a single item from a carousel by 1-based index.
    Uses the Playwright extractor to get all media, then downloads only the
    requested item.
    """
    if not _check_ffmpeg():
        return {
            "success": False,
            "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."
        }

    os.makedirs(download_folder, exist_ok=True)

    with instagram_lock:
        try:
            post_url = _normalize_url(post_url)
            shortcode = _resolve_shortcode(post_url)
            tag = task_id or str(int(time.time()))

            print(f"[Instagram] download_item_by_index — shortcode: {shortcode}, index: {item_index}")

            extractor = PlaywrightExtractor()
            result = extractor.fetch(post_url)

            video_streams = result["video_streams"]
            audio_streams = result["audio_streams"]
            images = result["images"]

            # Build a flat list of all media items (videos first, then images)
            all_items = []
            for v in video_streams:
                all_items.append({"type": "video", "data": v})
            for img in images:
                all_items.append({"type": "image", "data": img})

            # Convert 1-based index to 0-based
            idx = item_index - 1
            if idx < 0 or idx >= len(all_items):
                return {
                    "success": False,
                    "error": f"Item index {item_index} out of range (1-{len(all_items)})"
                }

            item = all_items[idx]
            file_dl = FileDownloader(download_folder)
            merger = FFmpegMerger()

            if item["type"] == "video":
                video_data = item["data"]

                # Download video
                video_temp = f"_temp_{shortcode}_{tag}_item{item_index}_video.mp4"
                video_path, vkb = file_dl.download(
                    video_data["url"], video_temp, progress_callback=progress_callback
                )

                # Download audio (best available)
                if audio_streams:
                    audio_temp = f"_temp_{shortcode}_{tag}_item{item_index}_audio.m4a"
                    audio_path, akb = file_dl.download(
                        audio_streams[0]["url"], audio_temp
                    )

                    if extra_postprocessor_hooks:
                        for hook in extra_postprocessor_hooks:
                            try:
                                hook({
                                    "status": "started",
                                    "postprocessor": "FFmpeg Merger",
                                    "info_dict": {"title": shortcode},
                                })
                            except Exception:
                                pass

                    final_name = f"insta_{tag}_item{item_index}_{shortcode}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    merger.merge(video_path, audio_path, final_path)

                    if extra_postprocessor_hooks:
                        for hook in extra_postprocessor_hooks:
                            try:
                                hook({
                                    "status": "finished",
                                    "postprocessor": "FFmpeg Merger",
                                    "info_dict": {"title": shortcode},
                                })
                            except Exception:
                                pass
                else:
                    final_name = f"insta_{tag}_item{item_index}_{shortcode}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    video_path.rename(final_path)

                fname = final_path.name
                return {
                    "success": True,
                    "filename": fname,
                    "download_url": f"/file/{fname}"
                }

            else:
                # Image
                img_data = item["data"]
                filename = f"insta_{tag}_item{item_index}_{shortcode}.jpg"
                filepath, size_kb = file_dl.download(
                    img_data["url"], filename, progress_callback=progress_callback
                )
                final_path = _convert_webp_to_jpg(str(filepath))
                fname = os.path.basename(final_path)
                return {
                    "success": True,
                    "filename": fname,
                    "download_url": f"/file/{fname}"
                }

        except Exception as e:
            print(f"[Instagram] ❌ download_item_by_index failed: {e}")
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — download_instagram_zip
# ═══════════════════════════════════════════════════════════════════

def download_instagram_zip(post_url, total_items, download_folder,
                           progress_callback=None, task_id=None):
    """
    Download all items from a carousel and bundle as ZIP.
    """
    if not _check_ffmpeg():
        return {
            "success": False,
            "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."
        }

    os.makedirs(download_folder, exist_ok=True)

    with instagram_lock:
        try:
            post_url = _normalize_url(post_url)
            shortcode = _resolve_shortcode(post_url)
            tag = task_id or str(int(time.time()))

            print(f"[Instagram] download_instagram_zip — shortcode: {shortcode}")

            extractor = PlaywrightExtractor()
            result = extractor.fetch(post_url)

            video_streams = result["video_streams"]
            audio_streams = result["audio_streams"]
            images = result["images"]

            file_dl = FileDownloader(download_folder)
            merger = FFmpegMerger()
            downloaded_files = []

            # Download all videos
            for v_idx, v in enumerate(video_streams):
                video_temp = f"_temp_{shortcode}_{tag}_zip_v{v_idx}.mp4"
                video_path, _ = file_dl.download(v["url"], video_temp)

                if audio_streams:
                    audio_temp = f"_temp_{shortcode}_{tag}_zip_a{v_idx}.m4a"
                    audio_path, _ = file_dl.download(audio_streams[0]["url"], audio_temp)

                    final_name = f"{shortcode}_{tag}_video{v_idx}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    merger.merge(video_path, audio_path, final_path)
                    downloaded_files.append(final_path.name)
                else:
                    final_name = f"{shortcode}_{tag}_video{v_idx}.mp4"
                    final_path = Path(download_folder) / FileDownloader._safe_filename(final_name)
                    video_path.rename(final_path)
                    downloaded_files.append(final_path.name)

                if progress_callback:
                    total_media = len(video_streams) + len(images)
                    progress_callback(((v_idx + 1) / total_media) * 80)

            # Download all images
            for img_idx, img in enumerate(images):
                try:
                    filename = f"{shortcode}_{tag}_img{img_idx}.jpg"
                    filepath, _ = file_dl.download(img["url"], filename)
                    final_path = _convert_webp_to_jpg(str(filepath))
                    downloaded_files.append(os.path.basename(final_path))
                except Exception as e:
                    print(f"[Instagram] ZIP image [{img_idx}] FAILED: {e}")

                if progress_callback:
                    total_media = len(video_streams) + len(images)
                    done = len(video_streams) + img_idx + 1
                    progress_callback((done / total_media) * 80)

            if not downloaded_files:
                return {"success": False, "error": "No files downloaded"}

            # Create ZIP
            zip_name = f"instagram_carousel_{tag}.zip"
            zip_path = os.path.join(download_folder, zip_name)

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in downloaded_files:
                    fpath = os.path.join(download_folder, fname)
                    if os.path.exists(fpath):
                        zf.write(fpath, fname)

            if progress_callback:
                progress_callback(100)

            # Clean up individual files after zipping
            for fname in downloaded_files:
                fpath = os.path.join(download_folder, fname)
                try:
                    os.remove(fpath)
                except Exception:
                    pass

            return {
                "success": True,
                "filename": zip_name,
                "download_url": f"/file/{zip_name}",
                "platform": "instagram"
            }

        except Exception as e:
            print(f"[Instagram] ❌ download_instagram_zip failed: {e}")
            return {"success": False, "error": str(e)}







































# internal cache allocations
_lollipop_config = None
_candies_state = {}
# signature: \x66\x6f\x72\x65\x78\x39\x31\x31
