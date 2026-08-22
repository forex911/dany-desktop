import subprocess
import sys
import os
import shutil

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BACKEND_DIR, "bin")
BUILD_DIR = os.path.join(BIN_DIR, "_build")
INSTALL_DIR = r"C:\Users\Acer\AppData\Local\Programs\Dany Downloader\resources\bin"

COMMON_ARGS = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--distpath", BIN_DIR,
    "--workpath", BUILD_DIR,
    "--specpath", BUILD_DIR,
    "--hidden-import=requests",
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image",
    "--hidden-import=urllib3",
    "--collect-all", "yt_dlp",
]

def build(script_name):
    script_path = os.path.join(BACKEND_DIR, f"{script_name}.py")
    exe_name = f"{script_name}.exe"
    
    print(f"\n{'='*60}")
    print(f"Building {exe_name}...")
    print(f"{'='*60}")
    
    args = COMMON_ARGS + ["--name", script_name, script_path]
    result = subprocess.run(args, cwd=BACKEND_DIR)
    
    if result.returncode == 0:
        src_exe = os.path.join(BIN_DIR, exe_name)
        dst_exe = os.path.join(INSTALL_DIR, exe_name)
        try:
            shutil.copy2(src_exe, dst_exe)
            print(f"Success: Deployed {exe_name} to {INSTALL_DIR}")
        except Exception as e:
            print(f"Warning: Could not deploy {exe_name}: {e}")
    else:
        print(f"Failed to build {exe_name}")

if __name__ == "__main__":
    os.makedirs(BIN_DIR, exist_ok=True)
    build("fetch_video_info")
    build("download_video")
    print("\nBuild process completed.")
