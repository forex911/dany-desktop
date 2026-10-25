"""
transcoder.py — Editor-compatible media transcoding utility

Handles FFmpeg-based H.264 + AAC conversion with:
  - Automatic NVENC hardware acceleration detection + fallback
  - ffprobe-based codec validation
  - Smart stream copy (only transcode streams that need it)
  - Safe temp-file handling with atomic replacement (os.replace)
  - Progress stage reporting for the Electron bridge

Used by download_video.py when --output-mode=editor_compatible is set.
"""

import os
import sys
import subprocess
import json
import shutil

# Ensure UTF-8 stdout/stderr on Windows to prevent charmap encoding errors
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# FFmpeg / FFprobe discovery
# ═══════════════════════════════════════════════════════════════════

def _search_candidate_dirs():
    """Yield directories where ffmpeg/ffprobe might reside."""
    # 1. Explicit env var
    env_dir = os.environ.get("DANY_FFMPEG_DIR")
    if env_dir and os.path.isdir(env_dir):
        yield env_dir

    # 2. Dany Downloader AppData managed bin
    appdata = os.environ.get("APPDATA")
    if appdata:
        yield os.path.join(appdata, "dany-downloader", "bin")

    # 3. Local backend/bin directory
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(os.path.dirname(cur_dir), "bin")

    # 4. Dany Downloader installed resources
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        yield os.path.join(local_appdata, "Programs", "Dany Downloader", "resources", "bin")


def get_ffmpeg_path():
    """Find ffmpeg executable via environment, well-known paths, or PATH."""
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for candidate_dir in _search_candidate_dirs():
        candidate_path = os.path.join(candidate_dir, exe_name)
        if os.path.isfile(candidate_path):
            return candidate_path

    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


def get_ffprobe_path():
    """Find ffprobe executable via environment, well-known paths, or PATH."""
    exe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for candidate_dir in _search_candidate_dirs():
        candidate_path = os.path.join(candidate_dir, exe_name)
        if os.path.isfile(candidate_path):
            return candidate_path

    found = shutil.which("ffprobe")
    if found:
        return found
    return None


# ═══════════════════════════════════════════════════════════════════
# Media probing
# ═══════════════════════════════════════════════════════════════════

def probe_media(filepath):
    """
    Run ffprobe on a file. Returns a dict with:
      video_codec, audio_codec, width, height, duration, fps, pix_fmt
    or None on error.
    """
    ffprobe = get_ffprobe_path()
    if not ffprobe or not os.path.exists(filepath):
        return None

    try:
        cmd = [
            ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            filepath,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        info = {
            "video_codec": None,
            "audio_codec": None,
            "width": 0,
            "height": 0,
            "duration": 0,
            "fps": 0,
            "pix_fmt": None,
        }

        for s in streams:
            if s.get("codec_type") == "video" and not info["video_codec"]:
                info["video_codec"] = s.get("codec_name")
                info["width"] = int(s.get("width", 0))
                info["height"] = int(s.get("height", 0))
                info["pix_fmt"] = s.get("pix_fmt")
                rfr = s.get("r_frame_rate", "0/1")
                try:
                    num, den = rfr.split("/")
                    info["fps"] = round(int(num) / int(den), 3) if int(den) else 0
                except (ValueError, ZeroDivisionError):
                    info["fps"] = 0

            elif s.get("codec_type") == "audio" and not info["audio_codec"]:
                info["audio_codec"] = s.get("codec_name")

        duration_str = fmt.get("duration", "0")
        try:
            info["duration"] = float(duration_str)
        except (ValueError, TypeError):
            info["duration"] = 0

        return info

    except Exception as e:
        print(f"[Transcoder] probe_media error: {e}")
        return None


def is_editor_compatible(filepath):
    """
    Returns True if the file already has H.264 (AVC) video + AAC audio
    in an MP4 container with standard 8-bit yuv420p pixel format,
    meaning it will import into Premiere Pro, DaVinci Resolve, Final Cut, etc.
    """
    if not filepath or not os.path.exists(filepath):
        return False

    # Check extension — must be .mp4
    ext = os.path.splitext(filepath)[1].lower()
    if ext != ".mp4":
        return False

    info = probe_media(filepath)
    if not info:
        return False

    vc = (info.get("video_codec") or "").lower()
    ac = (info.get("audio_codec") or "").lower()
    pix = (info.get("pix_fmt") or "").lower()

    has_video = bool(info.get("video_codec"))
    has_audio = bool(info.get("audio_codec"))

    # If video stream exists, must be H.264 / AVC and yuv420p
    if has_video:
        if vc not in ("h264", "avc1"):
            return False
        if pix and pix not in ("yuv420p", "yuvj420p"):
            return False

    # If audio stream exists, must be AAC
    if has_audio:
        if ac not in ("aac", "mp4a"):
            return False

    # Must have at least one stream
    return has_video or has_audio


# ═══════════════════════════════════════════════════════════════════
# Hardware encoder detection
# ═══════════════════════════════════════════════════════════════════

def detect_hw_encoder():
    """
    Detect if NVIDIA NVENC H.264 encoder is available.
    Returns "h264_nvenc" if usable, else "libx264".
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return "libx264"

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if "h264_nvenc" not in result.stdout:
            print("[Transcoder] h264_nvenc not listed in encoders")
            return "libx264"

        # Smoke-test: encode a frame (256x256 minimum dimension check for NVENC)
        test_result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1:r=1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if test_result.returncode == 0:
            print("[Transcoder] [OK] h264_nvenc available and verified")
            return "h264_nvenc"
        else:
            print(f"[Transcoder] h264_nvenc smoke test failed: {test_result.stderr[:200]}")

    except Exception as e:
        print(f"[Transcoder] NVENC detection error: {e}")

    return "libx264"


# ═══════════════════════════════════════════════════════════════════
# Transcoding engine
# ═══════════════════════════════════════════════════════════════════

def _build_transcode_cmd(ffmpeg, input_path, output_path, encoder="libx264"):
    """
    Build the FFmpeg command for H.264 + AAC transcoding.
    Uses smart stream copy when a stream is already compatible:
      - If video is already H.264 yuv420p, copy video (-c:v copy)
      - If audio is already AAC, copy audio (-c:a copy)
    """
    info = probe_media(input_path) or {}
    vc = (info.get("video_codec") or "").lower()
    ac = (info.get("audio_codec") or "").lower()
    pix = (info.get("pix_fmt") or "").lower()

    has_video = bool(info.get("video_codec"))
    has_audio = bool(info.get("audio_codec"))

    cmd = [ffmpeg, "-y", "-i", input_path]

    # Video stream configuration
    if has_video:
        video_is_h264 = vc in ("h264", "avc1") and pix in ("yuv420p", "yuvj420p", "")
        if video_is_h264:
            # Stream copy: 0% quality loss, near-instant
            cmd.extend(["-c:v", "copy"])
        elif encoder == "h264_nvenc":
            cmd.extend([
                "-c:v", "h264_nvenc",
                "-preset", "p4",       # balanced speed/quality
                "-rc", "vbr",
                "-cq", "20",           # visually lossless
                "-pix_fmt", "yuv420p", # universal 8-bit format
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "18",          # high quality
                "-pix_fmt", "yuv420p",
            ])
    else:
        cmd.append("-vn")

    # Audio stream configuration
    if has_audio:
        audio_is_aac = ac in ("aac", "mp4a")
        if audio_is_aac:
            # Stream copy: 0% quality loss, near-instant
            cmd.extend(["-c:a", "copy"])
        else:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")

    # MP4 optimization and metadata
    cmd.extend([
        "-movflags", "+faststart",  # Web/editor optimized MP4 atom order
        "-map_metadata", "0",       # Preserve tags / metadata
        output_path,
    ])

    return cmd


def transcode_to_editor_compatible(input_path, output_path, progress_callback=None):
    """
    Transcode a media file to H.264 + AAC MP4.

    Args:
        input_path:  Path to the source file.
        output_path: Path for the transcoded output.
        progress_callback: Optional callable(stage_str) for stage updates.

    Returns:
        (success: bool, output_path: str | None, error: str | None)
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return False, None, "FFmpeg not found"

    if not os.path.exists(input_path):
        return False, None, f"Input file not found: {input_path}"

    # Detect best encoder
    encoder = detect_hw_encoder()
    encoder_label = "NVENC (GPU)" if encoder == "h264_nvenc" else "libx264 (CPU)"
    print(f"[Transcoder] Encoder: {encoder_label}")

    if progress_callback:
        progress_callback(f"transcoding ({encoder_label})")

    cmd = _build_transcode_cmd(ffmpeg, input_path, output_path, encoder)
    print(f"[Transcoder] Transcoding: {os.path.basename(input_path)} → {os.path.basename(output_path)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-500:]
            print(f"[Transcoder] [ERROR] FFmpeg exit {result.returncode}: {stderr_tail}")

            # NVENC failure → software fallback
            if encoder == "h264_nvenc":
                print("[Transcoder] NVENC failed — falling back to libx264...")
                if progress_callback:
                    progress_callback("transcoding (libx264 fallback)")
                return _software_fallback(ffmpeg, input_path, output_path)

            return False, None, f"FFmpeg transcode failed (exit {result.returncode})"

        # Validate output
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            return False, None, "Transcoded output is empty or missing"

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[Transcoder] [OK] Done: {os.path.basename(output_path)} ({size_mb:.1f} MB, {encoder_label})")
        return True, output_path, None

    except subprocess.TimeoutExpired:
        return False, None, "Transcoding timed out (exceeded 1 hour)"
    except Exception as e:
        return False, None, f"Transcoding error: {e}"


def _software_fallback(ffmpeg, input_path, output_path):
    """Pure-software libx264 fallback if NVENC fails."""
    cmd = _build_transcode_cmd(ffmpeg, input_path, output_path, "libx264")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return False, None, f"Software transcode also failed (exit {result.returncode})"
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            return False, None, "Software transcode produced empty file"
        return True, output_path, None
    except Exception as e:
        return False, None, f"Software fallback error: {e}"


# ═══════════════════════════════════════════════════════════════════
# Safe in-place transcoding with atomic file replacement
# ═══════════════════════════════════════════════════════════════════

def safe_transcode_in_place(filepath, progress_callback=None):
    """
    Transcode a file in place, using a temp file for atomicity.

    - If the file is already editor-compatible, returns immediately.
    - Transcodes to a temp file in the same directory.
    - On success: replaces original atomically with os.replace.
    - On failure: cleans up temp, returns original path as fallback.

    Returns:
        (success: bool, final_path: str, error: str | None)
    """
    if not os.path.exists(filepath):
        return False, filepath, f"File not found: {filepath}"

    if is_editor_compatible(filepath):
        print(f"[Transcoder] Already H.264+AAC MP4, skipping: {os.path.basename(filepath)}")
        return True, filepath, None

    folder = os.path.dirname(filepath)
    basename = os.path.basename(filepath)
    name, _ext = os.path.splitext(basename)

    # Temp file in same directory for same-filesystem atomic rename
    temp_path = os.path.join(folder, f"_transcoding_{name}.mp4")
    final_path = os.path.join(folder, f"{name}.mp4")

    try:
        success, _, error = transcode_to_editor_compatible(
            filepath, temp_path, progress_callback
        )

        if not success:
            _safe_remove(temp_path)
            print(f"[Transcoder] [WARN] Transcode failed, keeping original: {error}")
            return False, filepath, error

        # Verify the transcoded file is actually editor-compatible
        if not is_editor_compatible(temp_path):
            _safe_remove(temp_path)
            return False, filepath, "Transcoded file failed H.264+AAC validation"

        # If original was a different extension (e.g. .webm, .mkv), remove it
        if os.path.abspath(filepath) != os.path.abspath(final_path):
            _safe_remove(filepath)

        # Atomic replace: overwrites final_path without race condition
        os.replace(temp_path, final_path)
        print(f"[Transcoder] [OK] In-place complete: {os.path.basename(final_path)}")
        return True, final_path, None

    except Exception as e:
        _safe_remove(temp_path)
        return False, filepath, f"In-place transcode error: {e}"


def _safe_remove(path):
    """Remove a file, ignoring errors."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
