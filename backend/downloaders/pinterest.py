import yt_dlp
import os
import time


def _base_pin_opts():
    """Base yt-dlp options for Pinterest."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
    }
    # Use app-managed FFmpeg if available
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        opts["ffmpeg_location"] = ffmpeg_dir
    return opts


def fetch_pinterest_info(url):
    ydl_opts = _base_pin_opts()
    ydl_opts["skip_download"] = True

    try:
        # Stage 1: Try without cookies
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e1:
            print(f"[Pinterest] Public extraction failed: {e1}")
            print("[Pinterest] Retrying with Chrome cookies...")
            ydl_opts["cookiesfrombrowser"] = ("chrome",)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

        # Detect media type: video or image
        vcodec = str(info.get("vcodec") or "none")
        ext = str(info.get("ext") or "")
        is_video = vcodec not in ("none", "") or ext in ("mp4", "webm", "mov")
        media_type = "video" if is_video else "image"

        # Pinterest titles can be messy
        title = info.get("title") or info.get("description") or "Pinterest Media"
        if len(title) > 50:
            title = title[:47] + "..."

        # Build format options based on media type
        if is_video:
            formats = [
                {"format_id": "best", "label": "Best Quality (Video)", "ext": "mp4", "type": "video"}
            ]
        else:
            formats = [
                {"format_id": "best", "label": "Best Quality (Image)", "ext": "jpg", "type": "image"}
            ]

        print(f"[Pinterest] Media type: {media_type} | Title: {title}")

        return {
            "success": True,
            "title": title,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "media_type": media_type,
            "formats": formats,
            "platform": "pinterest"
        }

    except Exception as e:
        return {"success": False, "error": str(e), "platform": "pinterest"}


def download_pinterest(url, download_folder, progress_callback=None, format_id="best", task_id=None, extra_progress_hooks=None, extra_postprocessor_hooks=None):
    """
    Pinterest-safe downloader with retry logic and cookie fallback.
    Pinterest usually provides ONLY muxed (video+audio) streams.
    """
    os.makedirs(download_folder, exist_ok=True)

    def progress_hook(d):
        if progress_callback and d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            if total and total > 0:
                percent = (downloaded / total) * 100
                progress_callback(percent)

    hooks = [progress_hook]
    if extra_progress_hooks:
        hooks.extend(extra_progress_hooks)

    ydl_opts = _base_pin_opts()
    ydl_opts.update({
        "format": format_id or "best",
        "outtmpl": os.path.join(
            download_folder,
            f"%(title)s_{task_id}.%(ext)s" if task_id else "%(title)s_%(id)s.%(ext)s"
        ),
        "progress_hooks": hooks
    })
    
    if format_id == "bestaudio/best":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320"
        }]

    if extra_postprocessor_hooks:
        ydl_opts["postprocessor_hooks"] = extra_postprocessor_hooks

    max_retries = 3
    last_error = "Unknown error"

    for attempt in range(max_retries):
        try:
            print(f"[Pinterest] Download attempt {attempt+1}/{max_retries}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            basename = os.path.basename(filename)
            print(f"[Pinterest] ✅ Downloaded: {basename}")
            return {
                "success": True,
                "filename": basename,
                "download_url": f"/file/{basename}",
                "title": info.get("title"),
                "platform": "pinterest"
            }

        except Exception as e:
            last_error = str(e)
            print(f"[Pinterest] ❌ Attempt {attempt+1} failed: {e}")

            # On first failure, add browser cookies for retry
            if attempt == 0 and "cookiesfrombrowser" not in ydl_opts:
                print("[Pinterest] Adding Chrome cookies for retry...")
                ydl_opts["cookiesfrombrowser"] = ("chrome",)

            if attempt < max_retries - 1:
                time.sleep(2)

    return {
        "success": False,
        "error": last_error,
        "platform": "pinterest"
    }
