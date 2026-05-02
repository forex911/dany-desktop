import yt_dlp
import os
import time
import shutil
import random

try:
    from utils.proxy_manager import proxy_manager
    from utils.cookie_manager import cookie_manager
except ImportError:
    # Fallback if run differently
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils.proxy_manager import proxy_manager
    from utils.cookie_manager import cookie_manager

# -----------------------------------
# Cookie resolver (local + Render)
# -----------------------------------
def get_cookie_path():

    local_cookie = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "cookies",
            "youtube_cookies.txt"
        )
    )

    render_cookie = "/etc/secrets/youtube_cookies.txt"
    tmp_cookie = "/tmp/youtube_cookies.txt"

    if os.path.exists(local_cookie):
        print("[DEBUG] Using local cookie:", local_cookie)
        return local_cookie

    if os.path.exists(render_cookie):
        try:
            shutil.copy(render_cookie, tmp_cookie)
            print("[DEBUG] Copied Render cookie →", tmp_cookie)
            return tmp_cookie
        except Exception as e:
            print("[DEBUG] Cookie copy failed:", e)

    return None


# -----------------------------------
# Base yt-dlp config
# -----------------------------------
def base_opts(use_proxy=False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "format_sort": ["res", "fps"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        },
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1
    }
    
    # Optional Proxy Mode
    if use_proxy:
        proxy = proxy_manager.get_random_proxy()
        if proxy:
            opts["proxy"] = proxy
            
    # Use app-managed FFmpeg if available
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        opts["ffmpeg_location"] = ffmpeg_dir
    return opts


# -----------------------------------
# Extraction pipeline (Preview)
# -----------------------------------
def try_extract(url, force_web_only=False):
    # Preview opts: NO format restriction — get ALL available formats
    opts_base = base_opts(use_proxy=False)
    opts_base["skip_download"] = True

    # ═══════════════════════════════════════════════════════════
    # STAGE 1: DIRECT (No cookies, no proxy)
    # ═══════════════════════════════════════════════════════════
    if not force_web_only:
        print("\n[YT-DLP] 🎬 Stage 1: DIRECT")
        test_opts = opts_base.copy()
        if "cookiefile" in test_opts:
            del test_opts["cookiefile"]

        try:
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmt_count = len(info.get("formats", []))
            print(f"[YT-DLP] ✅ Stage 1 Success! Formats found: {fmt_count}")
            return info, "direct", False, None
        except Exception as e:
            print(f"[YT-DLP Error] ❌ Stage 1 Failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # STAGE 2: COOKIE FALLBACK
    # Try browser cookies first, then file
    # ═══════════════════════════════════════════════════════════
    print("\n[YT-DLP] 🎬 Stage 2: COOKIE FALLBACK")
    for browser in ["chrome", "edge", "firefox", "brave", "safari", "opera"]:
        try:
            test_opts = opts_base.copy()
            test_opts["cookiesfrombrowser"] = (browser,)
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmt_count = len(info.get("formats", []))
            print(f"[YT-DLP] ✅ Stage 2 Success (Browser Cookies: {browser})! Formats found: {fmt_count}")
            return info, f"cookie_fallback_{browser}", True, None
        except Exception as e:
            print(f"[YT-DLP Error] ❌ Stage 2 {browser} Cookies Failed: {e}")
        
    cookie_path = get_cookie_path()
    if cookie_path:
        try:
            test_opts = opts_base.copy()
            test_opts["cookiefile"] = cookie_path
            if "cookiesfrombrowser" in test_opts:
                del test_opts["cookiesfrombrowser"]

            with yt_dlp.YoutubeDL(test_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmt_count = len(info.get("formats", []))
            print(f"[YT-DLP] ✅ Stage 2 Success (File Cookies)! Formats found: {fmt_count}")
            return info, "cookie_fallback", True, None
        except Exception as e:
            print(f"[YT-DLP Error] ❌ Stage 2 File Cookies Failed: {e}")
    else:
        print("[YT-DLP Error] ⚠️ Stage 2 File Cookies Skipped: No cookie file")

    if force_web_only:
        raise Exception("Direct and Cookie extraction strategies failed.")

    # ═══════════════════════════════════════════════════════════
    # STAGE 3: PROXY FALLBACK (Emergency only)
    # ═══════════════════════════════════════════════════════════
    print("\n[YT-DLP] 🎬 Stage 3: PROXY FALLBACK")
    for attempt in range(2):
        proxy = proxy_manager.get_random_proxy()
        if not proxy:
            print("[YT-DLP Error] ⚠️ Stage 3 Skipped: No proxies available")
            break
            
        test_opts = opts_base.copy()
        test_opts["proxy"] = proxy
        
        print(f"[YT-DLP DEBUG] 🔄 Attempt {attempt+1}/2 proxy={proxy}")
        try:
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmt_count = len(info.get("formats", []))
            print(f"[YT-DLP] ✅ Stage 3 Success! Formats found: {fmt_count}")
            return info, "proxy_fallback", False, proxy
        except Exception as e:
            err_msg = str(e).lower()
            print(f"[YT-DLP Error] ❌ Stage 3 Failed: {e}")
            if "429" in err_msg or "forbidden" in err_msg or "payment required" in err_msg:
                proxy_manager.mark_failed(proxy, 300)
            time.sleep(random.uniform(2, 4))

    # ═══════════════════════════════════════════════════════════
    # STAGE 4: LOWER SAFE QUALITY FALLBACK (Android VR)
    # ═══════════════════════════════════════════════════════════
    print("\n[YT-DLP] 🎬 Stage 4: FALLBACK (Lower Quality / Android VR)")
    try:
        test_opts = opts_base.copy()
        test_opts["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
        if "cookiefile" in test_opts:
            del test_opts["cookiefile"]

        with yt_dlp.YoutubeDL(test_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        fmt_count = len(info.get("formats", []))
        print(f"[YT-DLP] ✅ Stage 4 Success! Formats found: {fmt_count}")
        return info, "fallback", False, None
    except Exception as e:
        print(f"[YT-DLP Error] ❌ Stage 4 Failed: {e}")

    raise Exception("All 4 metadata extraction stages failed completely.")


# -----------------------------------
# Fetch metadata
# -----------------------------------
def fetch_youtube_info(url):
    try:
        info, source, used_cookie, proxy_used = try_extract(url)
        formats = info.get("formats", [])

        # ═══════════════════════════════════════════════════
        # DEBUG: Raw format inventory
        # ═══════════════════════════════════════════════════
        print(f"\n[FORMAT DEBUG] ═══════════════════════════════════")
        print(f"[FORMAT DEBUG] Source: {source} | Cookie: {used_cookie} | Proxy: {proxy_used}")
        print(f"[FORMAT DEBUG] Total raw formats: {len(formats)}")

        video_only = []    # DASH video (no audio)
        audio_only = []    # DASH audio (no video)
        progressive = []   # video + audio combined
        all_video_heights = set()

        for i, f in enumerate(formats):
            fid = f.get("format_id", "?")
            h = f.get("height")
            ext = f.get("ext", "?")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            size = f.get("filesize") or f.get("filesize_approx")
            size_mb = f"{size/1024/1024:.1f}MB" if size else "?"

            has_video = vcodec not in ("none", None)
            has_audio = acodec not in ("none", None)

            if has_video and has_audio:
                progressive.append(f)
                if h: all_video_heights.add(h)
            elif has_video:
                video_only.append(f)
                if h: all_video_heights.add(h)
            elif has_audio:
                audio_only.append(f)

        max_height = max(all_video_heights) if all_video_heights else 0
        print(f"[FORMAT DEBUG] Video-only: {len(video_only)} | Audio-only: {len(audio_only)} | Progressive: {len(progressive)}")
        print(f"[FORMAT DEBUG] Heights available: {sorted(all_video_heights, reverse=True)}")
        print(f"[FORMAT DEBUG] Max height: {max_height}p")

        if not formats:
            return {"success": False, "error": "No formats found"}

        # ═══════════════════════════════════════════════════
        # Build format list with EXACT format IDs (137+140 style)
        # ═══════════════════════════════════════════════════
        all_heights_sorted = sorted(all_video_heights, reverse=True)

        # Find best m4a audio format (prefer 140 for compatibility)
        best_audio = None
        for af in sorted(audio_only, key=lambda x: x.get("filesize", 0) or x.get("filesize_approx", 0) or 0, reverse=True):
            if af.get("ext") == "m4a" or (af.get("acodec", "").startswith("mp4a")):
                best_audio = af
                break
        if not best_audio and audio_only:
            best_audio = audio_only[-1]

        best_audio_id = best_audio["format_id"] if best_audio else "140"
        print(f"[FORMAT DEBUG] Best audio: id={best_audio_id} ({best_audio.get('acodec', '?') if best_audio else 'fallback'})")

        # Map: height → best video-only format (prefer mp4/h264, then any)
        best_video_per_height = {}
        for vf in video_only:
            h = vf.get("height", 0)
            if h < 360:
                continue
            existing = best_video_per_height.get(h)
            if not existing:
                best_video_per_height[h] = vf
            else:
                # Prefer mp4/h264 over webm/vp9 for merge compatibility
                new_is_mp4 = vf.get("ext") == "mp4" or vf.get("vcodec", "").startswith("avc")
                old_is_mp4 = existing.get("ext") == "mp4" or existing.get("vcodec", "").startswith("avc")
                if new_is_mp4 and not old_is_mp4:
                    best_video_per_height[h] = vf
                elif new_is_mp4 == old_is_mp4:
                    # Same type — prefer larger file (better quality)
                    new_size = vf.get("filesize") or vf.get("filesize_approx") or 0
                    old_size = existing.get("filesize") or existing.get("filesize_approx") or 0
                    if new_size > old_size:
                        best_video_per_height[h] = vf

        format_list = []

        # 1. Best Quality — generic fallback always first
        format_list.append({
            "format_id": f"bv*+ba/b[ext=mp4]/best",
            "label": "Best Quality (Auto Merge)",
            "ext": "mp4",
            "type": "video"
        })

        # 2. EXACT DASH quality tiers: video_id+audio_id
        for h in all_heights_sorted:
            if h in best_video_per_height:
                vf = best_video_per_height[h]
                vid = vf["format_id"]
                exact_id = f"{vid}+{best_audio_id}"
                codec = vf.get("vcodec", "?")[:8]
                label = f"{h}p"
                if h >= 2160:
                    label += " (4K)"
                elif h >= 1440:
                    label += " (QHD)"
                # Calculate total size
                v_size = vf.get("filesize") or vf.get("filesize_approx") or 0
                a_size = best_audio.get("filesize") or best_audio.get("filesize_approx") or 0 if best_audio else 0
                total_size = v_size + a_size
                if total_size > 0:
                    label += f" (~{total_size / 1024 / 1024:.1f} MB)"

                format_list.append({
                    "format_id": exact_id,
                    "label": label,
                    "ext": "mp4",
                    "type": "video"
                })
                print(f"[FORMAT DEBUG] DASH tier: {label} → {exact_id} [{codec}]")

        # 3. Progressive safe downloads (single-file, no merge)
        seen_prog = set()
        prog_sorted = sorted(progressive, key=lambda x: x.get("height", 0), reverse=True)
        for f in prog_sorted:
            h = f["height"]
            if h not in seen_prog and f.get("ext") == "mp4":
                seen_prog.add(h)
                f_size = f.get("filesize") or f.get("filesize_approx") or 0
                size_str = f" (~{f_size / 1024 / 1024:.1f} MB)" if f_size > 0 else ""
                format_list.append({
                    "format_id": str(f["format_id"]),
                    "label": f"{h}p (Safe Download - No Merge){size_str}",
                    "ext": "mp4",
                    "type": "video"
                })

        a_size = best_audio.get("filesize") or best_audio.get("filesize_approx") or 0 if best_audio else 0
        a_size_str = f" (~{a_size / 1024 / 1024:.1f} MB)" if a_size > 0 else ""
        format_list.append({
            "format_id": f"{best_audio_id}/bestaudio/best",
            "label": f"Audio Only (MP3){a_size_str}",
            "ext": "mp3",
            "type": "audio"
        })

        print(f"[FORMAT DEBUG] Final dropdown options: {len(format_list)}")
        for fl in format_list:
            print(f"[FORMAT DEBUG]   → {fl['label']} ({fl['format_id'][:40]})")

        # Quality summary
        qualities = [f"{r}p" for r in all_heights_sorted]
        if max_height >= 2160:
            recommended, msg = "2160p", "4K available!"
        elif max_height >= 1080:
            recommended, msg = "1080p", f"Max quality: {max_height}p"
        elif max_height > 0:
            recommended, msg = f"{max_height}p", f"Warning: Only {max_height}p available"
        else:
            recommended, msg = "audio", "No video formats found"

        quality_info = {
            "max_quality": qualities[0] if qualities else None,
            "available_qualities": qualities,
            "recommended": recommended,
            "message": msg,
        }

        return {
            "status": "success",
            "quality": quality_info.get("max_quality"),
            "source": source,
            "used_cookie": used_cookie,
            "proxy_used": proxy_used,
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "formats": format_list,
            "quality_info": quality_info
        }

    except Exception as e:
        print("[DEBUG] Metadata extraction failed:", e)
        return {
            "success": False,
            "error": str(e)
        }


# -----------------------------------
# Download (With Retry Fallbacks)
# -----------------------------------
def download_youtube(url, folder, progress_callback=None, format_id=None, task_id=None, extra_progress_hooks=None, extra_postprocessor_hooks=None):

    if not format_id:
        format_id = "bv*[height<=720]+ba/b/best"

    # Pre-flight FFmpeg Check
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir:
        ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(ffmpeg_exe):
            return {"success": False, "error": "FFmpeg executable is missing. It may have been quarantined by your Antivirus."}

    # Ensure download folder exists
    os.makedirs(folder, exist_ok=True)

    def progress_hook(d):
        if progress_callback and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                percent = (downloaded / total) * 100
                progress_callback(percent)

    # Common download setup (No Proxy by Default)
    opts_base = base_opts(use_proxy=False)
    
    # Build hooks list — internal + any extra from bridge
    hooks = [progress_hook]
    if extra_progress_hooks:
        hooks.extend(extra_progress_hooks)
    
    opts_base.update({
        "format": format_id,
        "outtmpl": os.path.join(
            folder,
            f"%(title)s_{task_id}.%(ext)s" if task_id else "%(title)s_%(id)s.%(ext)s"
        ),
        "progress_hooks": hooks,
        "prefer_ffmpeg": True
    })
    
    # Postprocessor hooks (for merge/finalize stage tracking)
    if extra_postprocessor_hooks:
        opts_base["postprocessor_hooks"] = extra_postprocessor_hooks
        
    print(f"[DOWNLOAD] Format: {format_id}")
    print(f"[DOWNLOAD] Output folder: {folder}")

    if format_id == "bestaudio/best":
        opts_base["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320"
        }]

    last_error = None

    def try_download(opts, source_label, proxy=None, used_cookie=False):
        nonlocal last_error
        print(f"\n[DOWNLOAD] ═══ {source_label} ═══")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info:
                    info = info["entries"][0]

                filename = ydl.prepare_filename(info)
                if format_id == "bestaudio/best":
                    filename = os.path.splitext(filename)[0] + ".mp3"

            basename = os.path.basename(filename)
            print(f"[DOWNLOAD] ✅ Success: {basename}")
            return {
                "status": "success",
                "quality": format_id,
                "source": source_label,
                "success": True,
                "filename": basename,
                "download_url": f"/file/{basename}",
                "title": info.get("title"),
                "proxy_used": proxy,
                "used_cookie": used_cookie
            }
        except Exception as e:
            last_error = str(e)
            print(f"[DOWNLOAD] ❌ {source_label} failed: {e}")
            return None

    # ═══════════════════════════════════════════════════
    # STAGE 1: DIRECT (No Cookies, No Proxy)
    # ═══════════════════════════════════════════════════
    opts_s1 = opts_base.copy()
    if "cookiefile" in opts_s1:
        del opts_s1["cookiefile"]
    res = try_download(opts_s1, "Stage 1: DIRECT")
    if res: return res
    
    # ═══════════════════════════════════════════════════
    # STAGE 2: COOKIE FALLBACK (Browser Chrome + File)
    # ═══════════════════════════════════════════════════
    for browser in ["chrome", "edge", "firefox", "brave", "safari", "opera"]:
        opts_s2_browser = opts_base.copy()
        if "cookiefile" in opts_s2_browser:
            del opts_s2_browser["cookiefile"]
        opts_s2_browser["cookiesfrombrowser"] = (browser,)
        res = try_download(opts_s2_browser, f"Stage 2: COOKIE FALLBACK ({browser})", used_cookie=True)
        if res: return res

    cookie_path = get_cookie_path()
    if cookie_path:
        opts_s2_file = opts_base.copy()
        opts_s2_file["cookiefile"] = cookie_path
        print(f"[DOWNLOAD] Using cookie file: {cookie_path}")
        res = try_download(opts_s2_file, "Stage 2: COOKIE FALLBACK (File)", used_cookie=True)
        if res: return res
    else:
        print("[DOWNLOAD] ⚠️ Stage 2 (File): Skipped — No cookie file found")
    
    # ═══════════════════════════════════════════════════
    # STAGE 3: PROXY FALLBACK
    # ═══════════════════════════════════════════════════
    for attempt in range(2):
        proxy = proxy_manager.get_random_proxy()
        if not proxy:
            print("[DOWNLOAD] ⚠️ Stage 3: PROXY FALLBACK Skipped — No proxies available")
            break
            
        opts_s3 = opts_base.copy()
        opts_s3["proxy"] = proxy
        
        print(f"[DOWNLOAD] 🔄 Stage 3 Attempt {attempt+1}/2 proxy={proxy}")
        res = try_download(opts_s3, f"Stage 3: PROXY FALLBACK (Attempt {attempt+1})", proxy=proxy)
        if res: return res
        
        # If proxy failed, mark it
        if last_error:
            err_msg = last_error.lower()
            if "429" in err_msg or "forbidden" in err_msg or "payment required" in err_msg:
                proxy_manager.mark_failed(proxy, 300)

    # ═══════════════════════════════════════════════════
    # STAGE 4: LOWER QUALITY FALLBACK (Android VR)
    # ═══════════════════════════════════════════════════
    opts_s4 = opts_base.copy()
    opts_s4["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
    if "cookiefile" in opts_s4:
        del opts_s4["cookiefile"]
    res = try_download(opts_s4, "Stage 4: FALLBACK (Lower Quality / Android VR)")
    if res: return res

    return {
        "success": False,
        "error": last_error,
        "status": "failed"
    }