from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import os
import threading
import requests
import uuid
import time

from downloaders.youtube import download_youtube, fetch_youtube_info
from downloaders.spotify import download_spotify, fetch_spotify_info, download_spotify_track_by_index, download_spotify_playlist_zip
from downloaders.instagram import download_instagram, fetch_instagram_info, download_instagram_item_by_index, download_instagram_zip
from downloaders.pinterest import download_pinterest, fetch_pinterest_info
from utils.helpers import validate_url, cleanup_old_files
from utils.task_manager import create_task, update_progress, finish_task, fail_task, get_task, cleanup_file

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DOWNLOAD_FOLDER = "/tmp/dany_downloads/"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

@app.route("/progress/<task_id>", methods=["GET"])
def get_progress(task_id):
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400
    
    task_data = get_task(task_id)
    if not task_data:
        return jsonify({"status": "starting", "progress": 0, "filename": None})
    
    import urllib.parse
    response = dict(task_data)
    if response.get("status") == "finished" and response.get("filename"):
        encoded_filename = urllib.parse.quote(response['filename'])
        response["download_url"] = f"/file/{encoded_filename}"
    return jsonify(response)

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "message": "Universal Media Downloader API running"
    })

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})

@app.route("/thumbnail", methods=["GET"])
def proxy_thumbnail():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return Response(resp.content, status=resp.status_code, content_type=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502

# ==========================================
# PREVIEW ENDPOINTS
# ==========================================

@app.route("/preview/youtube", methods=["POST"])
def preview_youtube():
    try:
        data = request.get_json()
        url = data.get("url")
        if not url or not validate_url(url, "youtube"):
            return jsonify({"status": "error", "message": "Invalid YouTube URL", "error": "Invalid YouTube URL"}), 400
        return jsonify(fetch_youtube_info(url))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500

@app.route("/preview/spotify", methods=["POST"])
def preview_spotify():
    try:
        data = request.get_json()
        url = data.get("url")
        if not url or not validate_url(url, "spotify"):
            return jsonify({"status": "error", "message": "Invalid Spotify URL", "error": "Invalid Spotify URL"}), 400
        return jsonify(fetch_spotify_info(url))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500

@app.route("/preview/instagram", methods=["POST"])
def preview_instagram():
    try:
        data = request.get_json()
        url = data.get("url")
        if not url or not validate_url(url, "instagram"):
            return jsonify({"status": "error", "message": "Invalid Instagram URL", "error": "Invalid Instagram URL"}), 400
        return jsonify(fetch_instagram_info(url))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500

@app.route("/preview/pinterest", methods=["POST"])
def preview_pinterest():
    try:
        data = request.get_json()
        url = data.get("url")
        if not url or not validate_url(url, "pinterest"):
            return jsonify({"status": "error", "message": "Invalid Pinterest URL", "error": "Invalid Pinterest URL"}), 400
        return jsonify(fetch_pinterest_info(url))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500

# ==========================================
# DOWNLOAD ENDPOINTS
# ==========================================


def _run_download_in_background(downloader, url, task_id, format_id):
    def progress_callback(percent, status="downloading"):
        update_progress(task_id, percent, status)

    try:
        result = downloader(url, DOWNLOAD_FOLDER, progress_callback, format_id, task_id)

        if result.get("success"):
            finish_task(task_id, result.get("filename"), result.get("title"))
        else:
            fail_task(task_id, result.get("error", "Unknown error"))
    except Exception as e:
        fail_task(task_id, str(e))

def handle_download(platform, downloader):
    try:
        data = request.get_json()
        url = data.get("url")
        format_id = data.get("format_id", "best")

        if not url:
            return jsonify({"status": "error", "message": "URL is required", "error": "URL is required"}), 400

        if not validate_url(url, platform):
            return jsonify({"status": "error", "message": f"Invalid {platform} URL", "error": f"Invalid {platform} URL"}), 400

        task_id = str(uuid.uuid4())
        
        # Register task and return immediately
        create_task(task_id)
        threading.Thread(
            target=_run_download_in_background,
            args=(downloader, url, task_id, format_id)
        ).start()

        return jsonify({
            "success": True,
            "task_id": task_id
        }), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500


@app.route("/download/youtube", methods=["POST"])
def youtube_route():
    return handle_download("youtube", download_youtube)


@app.route("/download/spotify", methods=["POST"])
def spotify_route():
    return handle_download("spotify", download_spotify)


@app.route("/download/spotify/track", methods=["POST"])
def spotify_track_route():
    """Download a single track from a Spotify playlist by 1-based index."""
    try:
        data = request.get_json()
        playlist_url = data.get("playlist_url")
        track_index = int(data.get("track_index", 1))

        if not playlist_url:
            return jsonify({"status": "error", "message": "playlist_url is required"}), 400

        task_id = str(uuid.uuid4())

        def _run(playlist_url, track_index, task_id):
            def cb(pct, status="downloading"): update_progress(task_id, pct, status)
            try:
                result = download_spotify_track_by_index(playlist_url, track_index, DOWNLOAD_FOLDER, cb, task_id)
                if result.get("success"):
                    fname = str(result.get("filename") or "")
                    finish_task(task_id, fname, str(result.get("title") or fname))
                else:
                    fail_task(task_id, result.get("error", "Unknown error"))
            except Exception as e:
                fail_task(task_id, str(e))

        create_task(task_id)
        threading.Thread(target=_run, args=(playlist_url, track_index, task_id)).start()
        return jsonify({"success": True, "task_id": task_id}), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/download/spotify/zip", methods=["POST"])
def spotify_zip_route():
    """Download a full Spotify playlist and return a ZIP."""
    try:
        data = request.get_json()
        playlist_url = data.get("playlist_url")

        if not playlist_url:
            return jsonify({"status": "error", "message": "playlist_url is required"}), 400

        task_id = str(uuid.uuid4())

        def _run(playlist_url, task_id):
            def cb(pct, status="downloading"): update_progress(task_id, pct, status)
            try:
                result = download_spotify_playlist_zip(playlist_url, DOWNLOAD_FOLDER, cb, task_id)
                if result.get("success"):
                    fname = str(result.get("filename") or "")
                    finish_task(task_id, fname, str(result.get("title") or "Spotify Playlist"))
                else:
                    fail_task(task_id, result.get("error", "Unknown error"))
            except Exception as e:
                fail_task(task_id, str(e))

        create_task(task_id)
        threading.Thread(target=_run, args=(playlist_url, task_id)).start()
        return jsonify({"success": True, "task_id": task_id}), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/download/instagram", methods=["POST"])
def instagram_route():
    return handle_download("instagram", download_instagram)


@app.route("/download/instagram/item", methods=["POST"])
def instagram_item_route():
    """Download a single Instagram carousel item by its 1-based index."""
    try:
        data = request.get_json()
        post_url = data.get("post_url")
        item_index = data.get("item_index", 1)

        if not post_url:
            return jsonify({"status": "error", "message": "post_url is required"}), 400

        task_id = str(uuid.uuid4())

        def _run(post_url, item_index, task_id):
            def progress_callback(percent, status="downloading"):
                update_progress(task_id, percent, status)
            try:
                result = download_instagram_item_by_index(post_url, item_index, DOWNLOAD_FOLDER, progress_callback, task_id)
                if result.get("success"):
                    fname = str(result.get("filename") or "")
                    finish_task(task_id, fname, fname)
                else:
                    fail_task(task_id, result.get("error", "Unknown error"))
            except Exception as e:
                fail_task(task_id, str(e))

        create_task(task_id)
        threading.Thread(target=_run, args=(post_url, item_index, task_id)).start()
        return jsonify({"success": True, "task_id": task_id}), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/download/instagram/zip", methods=["POST"])
def instagram_zip_route():
    """Download all carousel items and return a ZIP (downloads whole post at once)."""
    try:
        data = request.get_json()
        post_url = data.get("post_url")
        total_items = data.get("total_items", 0)

        if not post_url:
            return jsonify({"status": "error", "message": "post_url is required"}), 400

        task_id = str(uuid.uuid4())

        def _run(post_url, total_items, task_id):
            def progress_callback(percent, status="downloading"):
                update_progress(task_id, percent, status)
            try:
                result = download_instagram_zip(post_url, total_items, DOWNLOAD_FOLDER, progress_callback, task_id)
                if result.get("success"):
                    fname = str(result.get("filename") or "")
                    finish_task(task_id, fname, "Instagram Carousel")
                else:
                    fail_task(task_id, result.get("error", "Unknown error"))
            except Exception as e:
                fail_task(task_id, str(e))

        create_task(task_id)
        threading.Thread(target=_run, args=(post_url, total_items, task_id)).start()
        return jsonify({"success": True, "task_id": task_id}), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/download/pinterest', methods=['POST'])
def pinterest_route():
    try:
        data = request.get_json()
        url = data.get("url") if data else None
        format_id = data.get("format_id", "best")

        if not url:
            return jsonify({'status': 'error', 'message': 'URL is required', 'error': 'URL is required'}), 400

        if not validate_url(url, 'pinterest'):
            return jsonify({'status': 'error', 'message': 'Invalid Pinterest URL', 'error': 'Invalid Pinterest URL'}), 400
            
        task_id = str(uuid.uuid4())

        create_task(task_id)
        threading.Thread(
            target=_run_download_in_background,
            args=(download_pinterest, url, task_id, format_id)
        ).start()

        return jsonify({
            "success": True,
            "task_id": task_id
        }), 202

    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "error": str(e)}), 500


@app.route('/file/<path:filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)


# ==========================================
# AUTO-CLEANUP CRON (every 5 min, delete files > 10 min old)
# ==========================================
def run_cleanup_cron():
    while True:
        time.sleep(300)  # 5 minutes
        try:
            now = time.time()
            for fname in os.listdir(DOWNLOAD_FOLDER):
                fpath = os.path.join(DOWNLOAD_FOLDER, fname)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 600:
                    os.remove(fpath)
                    print(f"[Cleanup] Deleted: {fname}")
        except Exception as e:
            print(f"[Cleanup Error] {e}")

cleanup_thread = threading.Thread(target=run_cleanup_cron, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
