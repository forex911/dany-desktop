import yt_dlp
import os
import time
import shutil
import requests
import json
import re
import sys


# -----------------------------------
# Cookie resolver (same as YouTube)
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
        return local_cookie

    if os.path.exists(render_cookie):
        try:
            shutil.copy(render_cookie, tmp_cookie)
            return tmp_cookie
        except:
            pass

    return None


# -----------------------------------
# Base yt-dlp config
# -----------------------------------
def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        },
        "concurrent_fragment_downloads": 3
    }
    # Use app-managed FFmpeg if available
    ffmpeg_dir = os.environ.get("DANY_FFMPEG_DIR")
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        opts["ffmpeg_location"] = ffmpeg_dir
    return opts


# -----------------------------------
# Attempt extraction (uses yt-dlp default strategy)
# -----------------------------------
def try_extract(query, opts):
    """Extract info using yt-dlp default multi-client strategy."""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            return info
        except Exception as e:
            print(f"[Spotify] Extraction attempt {attempt+1}/{max_retries} failed: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(2)
    raise Exception("Spotify extraction failed after all retries")

def find_track_info(artist, track_name, opts):
    queries = [
        f"ytmusicsearch1:{artist} - {track_name}",
        f"ytsearch1:{artist} - {track_name} official audio",
        f"ytsearch1:{track_name} {artist}"
    ]
    for query in queries:
        try:
            info = try_extract(query, opts)
            if info and "entries" in info and len(info["entries"]) > 0:
                return info["entries"][0], query
        except Exception as e:
            print(f"[DEBUG] Query failed: {query}", e, file=sys.stderr)
    raise Exception("All prioritized search queries failed")

# -----------------------------------
# Extract track + artist from Spotify
# -----------------------------------
def extract_spotify_metadata(url):

    if "/playlist/" in url:
        playlist_id = url.split("/playlist/")[1].split("?")[0]
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        resp = requests.get(embed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)
        if match:
            data = json.loads(match.group(1))
            
            def find_entity(obj):
                if isinstance(obj, dict):
                    if 'trackList' in obj and isinstance(obj['trackList'], list):
                        return obj
                    for v in obj.values():
                        res = find_entity(v)
                        if res: return res
                elif isinstance(obj, list):
                    for item in obj:
                        res = find_entity(item)
                        if res: return res
                return None
                
            playlist = find_entity(data)
            
            if not playlist:
                raise Exception("Could not find playlist data (trackList) in Spotify embed info")
                
            tracks = []
            for item in playlist['trackList']:
                tracks.append({
                    "track": item.get('title', 'Unknown'),
                    "artist": item.get('subtitle', 'Unknown')
                })
            return {
                "is_playlist": True,
                "title": playlist.get('name', 'Spotify Playlist'),
                "tracks": tracks,
                "thumbnail": playlist.get('coverArt', {}).get('sources', [{}])[0].get('url', '') if playlist.get('coverArt') else ""
            }
        else:
            raise Exception("Could not parse Spotify playlist embed data")
    else:
        resp = requests.get(
            f"https://open.spotify.com/oembed?url={url}",
            timeout=10
        )

        data = resp.json()

        title = data.get("title", "")

        # Spotify format: Track - Artist
        parts = title.split(" - ")

        track = parts[0].strip()
        artist = parts[1].strip() if len(parts) > 1 else ""

        return {
            "is_playlist": False,
            "track": track,
            "artist": artist,
            "thumbnail": data.get("thumbnail_url"),
            "title": title
        }


# -----------------------------------
# Fetch Spotify preview
# -----------------------------------
def fetch_spotify_info(url):

    try:

        meta = extract_spotify_metadata(url)

        ydl_opts = base_opts()
        ydl_opts["skip_download"] = True

        cookie = get_cookie_path()
        if cookie:
            ydl_opts["cookiefile"] = cookie
            
        if meta.get("is_playlist"):
            track_items = []
            for i, t in enumerate(meta.get("tracks", [])):
                track_items.append({
                    "index": i + 1,          # 1-based
                    "title": t.get("track", "Unknown Track"),
                    "artist": t.get("artist", "Unknown Artist"),
                    "thumbnail": meta.get("thumbnail") or None,
                    "type": "audio"
                })

            return {
                "success": True,
                "is_playlist": True,
                "title": meta["title"],
                "thumbnail": meta.get("thumbnail"),
                "track_items": track_items,
                "formats": [
                    {
                        "format_id": "bestaudio/best",
                        "label": "Best Audio (MP3 320kbps ZIP)",
                        "ext": "zip",
                        "type": "audio"
                    }
                ]
            }

        else:
            track_name = meta["track"]
            artist = meta["artist"]

            video, _ = find_track_info(artist, track_name, ydl_opts)

            formats = [

                {
                    "format_id": "bestaudio/best",
                    "label": "Best Audio (MP3 320kbps)",
                    "ext": "mp3",
                    "type": "audio"
                },

                {
                    "format_id": "bestaudio[abr<=192]/bestaudio",
                    "label": "192 kbps MP3",
                    "ext": "mp3",
                    "type": "audio"
                },

                {
                    "format_id": "bestaudio[abr<=128]/bestaudio",
                    "label": "128 kbps MP3",
                    "ext": "mp3",
                    "type": "audio"
                }
            ]

            return {
                "success": True,
                "title": f"{artist} - {track_name}",
                "thumbnail": meta["thumbnail"] or video.get("thumbnail"),
                "duration": video.get("duration"),
                "formats": formats,
                "track": track_name,
                "artist": artist
            }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# -----------------------------------
# Download Spotify audio
# -----------------------------------
def download_spotify(url, folder, progress_callback=None, format_id="bestaudio/best", task_id=None, extra_progress_hooks=None, extra_postprocessor_hooks=None):

    os.makedirs(folder, exist_ok=True)

    try:

        meta = extract_spotify_metadata(url)

        ydl_opts = base_opts()

        cookie = get_cookie_path()
        if cookie:
            ydl_opts["cookiefile"] = cookie

        ydl_opts.update({
            "format": format_id,
            "prefer_ffmpeg": True
        })

        # Always enforce MP3 audio extraction for Spotify downloads
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320" if format_id == "bestaudio/best" else ("192" if "192" in format_id else "128")
        }]

        if meta.get("is_playlist"):
            playlist_name = meta["title"]
            sanitized_name = "".join([c for c in playlist_name if c.isalnum() or c in ' -_']).strip()
            if not sanitized_name:
                sanitized_name = f"playlist_{int(time.time())}"
            playlist_folder = os.path.join(folder, sanitized_name)
            os.makedirs(playlist_folder, exist_ok=True)
            
            total_tracks = len(meta["tracks"])
            for idx, track_info in enumerate(meta["tracks"]):
                track_name = track_info["track"]
                artist = track_info["artist"]
                
                try:
                    video, successful_query = find_track_info(artist, track_name, ydl_opts)
                    
                    def progress_hook(d):
                        if progress_callback and d["status"] == "downloading":
                            total = d.get("total_bytes") or d.get("total_bytes_estimate")
                            downloaded = d.get("downloaded_bytes", 0)
                            if total:
                                track_percent = downloaded / total
                                percent = ((idx + track_percent) / total_tracks) * 100
                                progress_callback(percent)

                    hooks = [progress_hook]
                    if extra_progress_hooks:
                        hooks.extend(extra_progress_hooks)

                    track_opts = ydl_opts.copy()
                    track_opts["outtmpl"] = os.path.join(playlist_folder, f"%(title)s_{task_id}.%(ext)s" if task_id else "%(title)s_%(id)s.%(ext)s")
                    track_opts["progress_hooks"] = hooks
                    if extra_postprocessor_hooks:
                        track_opts["postprocessor_hooks"] = extra_postprocessor_hooks

                    with yt_dlp.YoutubeDL(track_opts) as ydl:
                        info = ydl.extract_info(successful_query, download=True)
                except Exception as e:
                    print(f"[DEBUG] Failed to download {track_name}: {e}", file=sys.stderr)
                    continue
            
            return {
                "success": True,
                "filename": sanitized_name,
                "_downloadFolder": folder,
                "title": f"Playlist: {playlist_name}"
            }

        else:
            track_name = meta["track"]
            artist = meta["artist"]

            video, successful_query = find_track_info(artist, track_name, ydl_opts)

            def progress_hook(d):

                if progress_callback and d["status"] == "downloading":

                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    downloaded = d.get("downloaded_bytes", 0)

                    if total:
                        percent = (downloaded / total) * 100
                        progress_callback(percent)

            hooks = [progress_hook]
            if extra_progress_hooks:
                hooks.extend(extra_progress_hooks)

            ydl_opts.update({
                "outtmpl": os.path.join(
                    folder,
                    f"%(title)s_{task_id}.%(ext)s" if task_id else "%(title)s_%(id)s.%(ext)s"
                ),
                "progress_hooks": hooks
            })
            if extra_postprocessor_hooks:
                ydl_opts["postprocessor_hooks"] = extra_postprocessor_hooks

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:

                info = ydl.extract_info(successful_query, download=True)

                video = info["entries"][0]

                filename = ydl.prepare_filename(video)

                # Fix extension after MP3 conversion
                filename = os.path.splitext(filename)[0] + ".mp3"

            basename = os.path.basename(filename)
            return {
                "success": True,
                "filename": basename,
                "download_url": f"/file/{basename}",
                "title": video.get("title")
            }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# -----------------------------------
# Download a single track by 1-based index from a Spotify playlist
# -----------------------------------
def download_spotify_track_by_index(playlist_url, track_index, folder, progress_callback=None, format_id="bestaudio/best", task_id=None):
    """
    Downloads a single track from a Spotify playlist by its 1-based index.
    Fetches playlist metadata, picks the Nth track, searches YouTube Music, downloads.
    """
    os.makedirs(folder, exist_ok=True)

    try:
        meta = extract_spotify_metadata(playlist_url)
        if not meta.get("is_playlist"):
            return {"success": False, "error": "URL is not a Spotify playlist"}

        tracks = meta.get("tracks", [])
        if track_index < 1 or track_index > len(tracks):
            return {"success": False, "error": f"Track index {track_index} out of range (1–{len(tracks)})"}

        track_info = tracks[track_index - 1]
        track_name = track_info.get("track", "")
        artist = track_info.get("artist", "")

        ydl_opts = base_opts()
        ydl_opts["skip_download"] = False
        ydl_opts["format"] = format_id
        ydl_opts["prefer_ffmpeg"] = True

        if format_id == "bestaudio/best":
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320"
            }]

        cookie = get_cookie_path()
        if cookie:
            ydl_opts["cookiefile"] = cookie

        def progress_hook(d):
            if progress_callback and d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    progress_callback((downloaded / total) * 100)

        ydl_opts["outtmpl"] = os.path.join(folder, f"%(title)s_{task_id}.%(ext)s" if task_id else "%(title)s_%(id)s.%(ext)s")
        ydl_opts["progress_hooks"] = [progress_hook]

        video, successful_query = find_track_info(artist, track_name, ydl_opts)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(successful_query, download=True)
            entry = info["entries"][0] if "entries" in info else info
            filename = ydl.prepare_filename(entry)
            filename = os.path.splitext(filename)[0] + ".mp3"

        basename = os.path.basename(filename)
        return {
            "success": True,
            "filename": basename,
            "download_url": f"/file/{basename}",
            "title": f"{artist} - {track_name}"
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# -----------------------------------
# Download full playlist as ZIP (wraps existing download_spotify playlist logic)
# -----------------------------------
def download_spotify_playlist_zip(playlist_url, folder, progress_callback=None, format_id="bestaudio/best", task_id=None):
    """Alias to download_spotify for a playlist URL — returns ZIP download_url."""
    return download_spotify(playlist_url, folder, progress_callback, format_id, task_id)