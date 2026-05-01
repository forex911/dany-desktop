import os
import time
from urllib.parse import urlparse

def validate_url(url, platform):
    """
    Validate if URL belongs to the specified platform
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if platform == 'youtube':
            return 'youtube.com' in domain or 'youtu.be' in domain

        elif platform == 'instagram':
            return 'instagram.com' in domain

        elif platform == 'spotify':
            return 'spotify.com' in domain

        elif platform == 'pinterest':
            # ✅ SUPPORTS pinterest.com, in.pinterest.com, pin.it
            return 'pinterest.com' in domain or 'pin.it' in domain

        return False

    except Exception:
        return False


def cleanup_old_files(download_folder, max_age_hours=1):
    """
    Delete files older than max_age_hours
    """
    try:
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600

        for filename in os.listdir(download_folder):
            filepath = os.path.join(download_folder, filename)

            if os.path.isfile(filepath):
                file_age = current_time - os.path.getctime(filepath)

                if file_age > max_age_seconds:
                    os.remove(filepath)
                    print(f"Deleted old file: {filename}")

    except Exception as e:
        print(f"Cleanup error: {e}")
