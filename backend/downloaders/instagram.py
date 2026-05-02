import yt_dlp
import os
import time
import zipfile
import random
import threading

# PIL is optional — used for webp→jpg conversion
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[Instagram] PIL not installed — webp/png→jpg conversion disabled")

instagram_lock = threading.Lock()


def _extract_entries(info):
    """Flatten carousel entries or return a single-item list."""
    entries = info.get("entries")
    if entries:
        return [e for e in entries if isinstance(e, dict)]
    return [info]


def _base_ig_opts():
    """Base yt-dlp options for Instagram extraction."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"instagram": {"skip_dash_manifest": True}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
    }
    # Use app-managed FFmpeg if available
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        opts["ffmpeg_location"] = ffmpeg_dir
    return opts


def fetch_instagram_info(url):
    # Stage 1: Try without cookies
    ydl_opts = _base_ig_opts()
    ydl_opts["skip_download"] = True
    ydl_opts["extract_flat"] = False
    ydl_opts["noplaylist"] = False

    try:
        # Stage 1: Try without cookies
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e1:
            print(f"[Instagram] Public extraction failed: {e1}")
            print("[Instagram] Retrying with browser cookies...")
            # Stage 2: Retry with browser cookies (for private/login-required posts)
            success = False
            for browser in ["chrome", "edge", "firefox", "brave", "safari", "opera"]:
                try:
                    ydl_opts["cookiesfrombrowser"] = (browser,)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    success = True
                    break
                except Exception as e2:
                    print(f"[Instagram] {browser} extraction failed: {e2}")
            if not success:
                raise Exception("All browser cookie fallbacks failed")

        entries = _extract_entries(info)
        is_carousel = len(entries) > 1

        media_items = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue

            # Determine media type
            vcodec = str(entry.get("vcodec") or "none")
            ext = str(entry.get("ext") or "")
            is_video = vcodec not in ("none", "") or ext in ("mp4", "webm", "mov")
            media_type = "video" if is_video else "image"

            # Best thumbnail — iterate thumbnails list from highest quality down
            thumbnail = entry.get("thumbnail") or None
            if not thumbnail:
                thumbnails = entry.get("thumbnails") or []
                for t in reversed(thumbnails):
                    if isinstance(t, dict) and t.get("url"):
                        thumbnail = t["url"]
                        break

            # Fallback for images: use direct URL as preview
            if media_type == "image" and not thumbnail:
                direct = entry.get("url") or ""
                if direct and direct.startswith("http"):
                    thumbnail = direct

            title_raw = entry.get("title") or entry.get("description") or "Instagram Media"
            media_items.append({
                "type": media_type,
                "thumbnail": thumbnail,
                # index is 1-based (yt_dlp playlist_items is 1-based)
                "index": idx + 1,
                "title": str(title_raw),
            })

        if not media_items:
            raise ValueError("No media items found in post")

        # Top-level title/thumbnail
        title = info.get("title") or info.get("description") or "Instagram Post"
        if title and len(title) > 60:
            title = title[:57] + "..."

        top_thumb = info.get("thumbnail")
        if not top_thumb and media_items:
            top_thumb = media_items[0].get("thumbnail")

        return {
            "success": True,
            "title": title,
            "thumbnail": top_thumb,
            "is_carousel": is_carousel,
            "media_items": media_items,
            # Legacy format list — used for single-item / non-carousel flow
            "formats": [
                {
                    "format_id": "bestvideo+bestaudio/best",
                    "label": "Best Quality",
                    "ext": "mp4",
                    "type": "video"
                }
            ]
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def download_instagram(url, download_folder, progress_callback=None, format_id="best", task_id=None, extra_progress_hooks=None, extra_postprocessor_hooks=None):
    """Download the entire Instagram post (single video/image or whole carousel)."""
    if not format_id:
        format_id = "bestvideo+bestaudio/best"

    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(ffmpeg_exe):
            return {"success": False, "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."}

    os.makedirs(download_folder, exist_ok=True)

    def progress_hook(d):
        if progress_callback and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress_callback((downloaded / total) * 100)

    hooks = [progress_hook]
    if extra_progress_hooks:
        hooks.extend(extra_progress_hooks)

    ydl_opts = {
        "format": format_id,
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": os.path.join(download_folder, f"%(title)s_{task_id}_%(id)s.%(ext)s" if task_id else f"%(title)s_{int(time.time())}_%(id)s.%(ext)s"),
        "progress_hooks": hooks,
        "extractor_args": {"instagram": {"skip_dash_manifest": True}}
    }
    
    if format_id == "bestaudio/best":
        if "merge_output_format" in ydl_opts:
            del ydl_opts["merge_output_format"]
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320"
        }]

    if extra_postprocessor_hooks:
        ydl_opts["postprocessor_hooks"] = extra_postprocessor_hooks

    downloaded_files = []
    max_retries = 3
    last_error = "Unknown error"

    with instagram_lock:
        for attempt in range(max_retries):
            try:
                print(f"[Instagram] Download attempt {attempt+1}/{max_retries}")
                time.sleep(random.uniform(3, 6))
                before_files = set(os.listdir(download_folder))
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)

                after_files = set(os.listdir(download_folder))
                new_files = list(after_files - before_files)

                if not new_files:
                    raise Exception("No downloadable media found")

                for file in new_files:
                    filepath = os.path.join(download_folder, file)
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in [".webp", ".png"] and HAS_PIL:
                        jpg_file = filepath.rsplit(".", 1)[0] + ".jpg"
                        try:
                            with Image.open(filepath) as img:
                                img.convert("RGB").save(jpg_file, "JPEG")
                            os.remove(filepath)
                            file = os.path.basename(jpg_file)
                        except Exception:
                            pass
                    downloaded_files.append(file)

                return {
                    "success": True,
                    "files": downloaded_files,
                    "filename": downloaded_files[0],
                    "download_url": f"/file/{downloaded_files[0]}",
                    "platform": "instagram"
                }

            except Exception as e:
                last_error = str(e)
                print(f"[Instagram] ❌ Attempt {attempt+1} failed: {e}")
                if attempt == 0 and "cookiesfrombrowser" not in ydl_opts:
                    # Pick a browser sequentially based on retries, or just pick edge/chrome
                    print("[Instagram] Adding browser cookies for retry...")
                    ydl_opts["cookiesfrombrowser"] = ("edge" if os.name == "nt" else "chrome",)
                if attempt == max_retries - 1:
                    return {"success": False, "error": last_error, "platform": "instagram"}


def download_instagram_item_by_index(post_url, item_index, download_folder, progress_callback=None, task_id=None, extra_progress_hooks=None, extra_postprocessor_hooks=None):
    """
    Download a single carousel item by its 1-based index using yt_dlp's playlist_items.
    This is the reliable approach — avoids fragile per-entry-url downloads.
    """
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(ffmpeg_exe):
            return {"success": False, "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."}

    os.makedirs(download_folder, exist_ok=True)

    def progress_hook(d):
        if progress_callback and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress_callback((downloaded / total) * 100)

    hooks = [progress_hook]
    if extra_progress_hooks:
        hooks.extend(extra_progress_hooks)

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "playlist_items": str(item_index),          # only this item
        "outtmpl": os.path.join(download_folder, f"insta_{task_id}_item{item_index}_%(id)s.%(ext)s" if task_id else f"insta_{int(time.time())}_item{item_index}_%(id)s.%(ext)s"),
        "progress_hooks": hooks,
        "extractor_args": {"instagram": {"skip_dash_manifest": True}}
    }
    if extra_postprocessor_hooks:
        ydl_opts["postprocessor_hooks"] = extra_postprocessor_hooks

    max_retries = 3
    last_error = "Unknown error"

    with instagram_lock:
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(3, 6))
                before_files = set(os.listdir(download_folder))
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(post_url, download=True)

                after_files = set(os.listdir(download_folder))
                new_files = list(after_files - before_files)

                if not new_files:
                    raise Exception("No file downloaded — item may be unavailable")

                for i, file in enumerate(new_files):
                    filepath = os.path.join(download_folder, file)
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in [".webp", ".png"] and HAS_PIL:
                        jpg_file = filepath.rsplit(".", 1)[0] + ".jpg"
                        try:
                            with Image.open(filepath) as img:
                                img.convert("RGB").save(jpg_file, "JPEG")
                            os.remove(filepath)
                            new_files[i] = os.path.basename(jpg_file)
                        except Exception:
                            pass

                fname = str(new_files[0])
                return {"success": True, "filename": fname, "download_url": f"/file/{fname}"}

            except Exception as e:
                last_error = str(e)
                if attempt == max_retries - 1:
                    return {"success": False, "error": last_error}


def download_instagram_zip(post_url, total_items, download_folder, progress_callback=None, task_id=None):
    """
    Download all carousel items and bundle as ZIP.
    Downloads the full post (noplaylist=False) to get all items.
    """
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(ffmpeg_exe):
            return {"success": False, "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."}

    os.makedirs(download_folder, exist_ok=True)
    downloaded = []

    def progress_hook(d):
        if progress_callback and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            dl = d.get("downloaded_bytes", 0)
            if total:
                progress_callback((dl / total) * 100)

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,                         # get ALL carousel items
        "outtmpl": os.path.join(download_folder, f"insta_zip_{task_id}_%(autonumber)s_%(id)s.%(ext)s" if task_id else f"insta_zip_{int(time.time())}_%(autonumber)s_%(id)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "extractor_args": {"instagram": {"skip_dash_manifest": True}}
    }

    max_retries = 3
    last_error = "Unknown error"

    with instagram_lock:
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(3, 6))
                before_files = set(os.listdir(download_folder))
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(post_url, download=True)

                after_files = set(os.listdir(download_folder))
                new_files = list(after_files - before_files)

                if not new_files:
                    raise Exception("No files downloaded")

                downloaded.clear()
                for i, file in enumerate(new_files):
                    filepath = os.path.join(download_folder, file)
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in [".webp", ".png"] and HAS_PIL:
                        jpg_file = filepath.rsplit(".", 1)[0] + ".jpg"
                        try:
                            with Image.open(filepath) as img:
                                img.convert("RGB").save(jpg_file, "JPEG")
                            os.remove(filepath)
                            new_files[i] = os.path.basename(jpg_file)
                        except Exception:
                            pass
                    downloaded.append(str(new_files[i]))

                zip_name = f"instagram_carousel_{task_id}.zip" if task_id else f"instagram_carousel_{int(time.time())}.zip"
                zip_path = os.path.join(download_folder, zip_name)

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname in downloaded:
                        fpath = os.path.join(download_folder, fname)
                        if os.path.exists(fpath):
                            zf.write(fpath, str(fname))

                return {"success": True, "filename": zip_name, "download_url": f"/file/{zip_name}"}

            except Exception as e:
                last_error = str(e)
                if attempt == max_retries - 1:
                    return {"success": False, "error": last_error}