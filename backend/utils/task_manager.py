import threading
import time
import os

# Thread-safe dictionary for tracking all active/completed download tasks
tasks = {}
tasks_lock = threading.Lock()

def create_task(task_id):
    with tasks_lock:
        tasks[task_id] = {
            "status": "pending",
            "progress": 0,
            "filename": None,
            "error": None
        }

def update_progress(task_id, percent, status="downloading"):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]["progress"] = percent
            tasks[task_id]["status"] = status

def finish_task(task_id, filename, title=None):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]["status"] = "finished"
            tasks[task_id]["progress"] = 100
            tasks[task_id]["filename"] = filename
            tasks[task_id]["title"] = title

def fail_task(task_id, error_msg):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["error"] = str(error_msg)

def get_task(task_id):
    with tasks_lock:
        return tasks.get(task_id)

def cleanup_file(filepath, delay=600):
    """
    Sleeps for `delay` seconds, then deletes the file to free up disk space.
    Intended to be run in a daemon thread.
    """
    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"[Cleanup] Deleted stale file: {filepath}")
        except Exception as e:
            print(f"[Cleanup Error] Failed to delete {filepath}: {e}")

    threading.Thread(target=_delete, daemon=True).start()
