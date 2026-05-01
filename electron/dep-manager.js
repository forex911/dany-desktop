// electron/dep-manager.js
// Professional dependency management for yt-dlp + FFmpeg

const { execFile, exec } = require("child_process");
const path = require("path");
const fs = require("fs");
const https = require("https");
const http = require("http");

// ═══════════════════════════════════════════════════════════
// Configuration
// ═══════════════════════════════════════════════════════════
const FFMPEG_DOWNLOAD_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip";

function getBinariesDir() {
    // In production (packaged), extraResources copies backend/bin → resources/bin
    // In dev, use backend/bin directly
    const isPacked = require("electron").app?.isPackaged ?? false;
    if (isPacked) {
        return path.join(process.resourcesPath, "bin");
    }
    return path.join(__dirname, "..", "backend", "bin");
}

// ═══════════════════════════════════════════════════════════
// Check: Python available?
// ═══════════════════════════════════════════════════════════
function checkPython() {
    return new Promise((resolve) => {
        exec("python --version", { windowsHide: true, timeout: 10000 }, (err, stdout, stderr) => {
            if (err) {
                // Try python3 as fallback
                exec("python3 --version", { windowsHide: true, timeout: 10000 }, (err2, stdout2, stderr2) => {
                    if (err2) {
                        resolve({ ok: false, version: null, error: "Python not found" });
                    } else {
                        const ver = (stdout2 || stderr2 || "").trim();
                        resolve({ ok: true, version: ver, cmd: "python3" });
                    }
                });
            } else {
                const ver = (stdout || stderr || "").trim();
                resolve({ ok: true, version: ver, cmd: "python" });
            }
        });
    });
}

// ═══════════════════════════════════════════════════════════
// Check: yt-dlp Python module installed?
// ═══════════════════════════════════════════════════════════
function checkYtDlpModule(pythonCmd = "python") {
    return new Promise((resolve) => {
        exec(
            `${pythonCmd} -c "import yt_dlp; print(yt_dlp.version.__version__)"`,
            { windowsHide: true, timeout: 15000 },
            (err, stdout) => {
                if (err) {
                    resolve({ ok: false, version: null, error: "yt-dlp module not installed" });
                } else {
                    const ver = (stdout || "").trim();
                    resolve({ ok: true, version: ver });
                }
            }
        );
    });
}

// ═══════════════════════════════════════════════════════════
// Check: FFmpeg binaries in managed folder?
// ═══════════════════════════════════════════════════════════
function checkFfmpeg(binariesDir) {
    // Flat structure: ffmpeg.exe lives directly in backend/bin/
    const ffmpegPath = path.join(binariesDir, "ffmpeg.exe");
    const ffprobePath = path.join(binariesDir, "ffprobe.exe");

    const ffmpegExists = fs.existsSync(ffmpegPath);
    const ffprobeExists = fs.existsSync(ffprobePath);

    if (ffmpegExists && ffprobeExists) {
        return { ok: true, path: binariesDir };
    }

    return {
        ok: false,
        error: `Missing: ${!ffmpegExists ? "ffmpeg.exe " : ""}${!ffprobeExists ? "ffprobe.exe" : ""}`.trim(),
        path: binariesDir
    };
}

// ═══════════════════════════════════════════════════════════
// Install: yt-dlp via pip
// ═══════════════════════════════════════════════════════════
function installYtDlp(pythonCmd = "python", statusCallback = null) {
    return new Promise((resolve) => {
        if (statusCallback) statusCallback("Installing yt-dlp...");
        console.log("[DEP] Installing yt-dlp via pip...");

        exec(
            `${pythonCmd} -m pip install --upgrade yt-dlp`,
            { windowsHide: true, timeout: 120000 },
            (err, stdout, stderr) => {
                if (err) {
                    console.error("[DEP] yt-dlp install failed:", stderr || err.message);
                    resolve({ ok: false, error: stderr || err.message });
                } else {
                    console.log("[DEP] yt-dlp installed successfully");
                    if (statusCallback) statusCallback("yt-dlp installed ✓");
                    resolve({ ok: true });
                }
            }
        );
    });
}

// ═══════════════════════════════════════════════════════════
// Install: FFmpeg — download and extract
// ═══════════════════════════════════════════════════════════
function downloadFfmpeg(binariesDir, statusCallback = null) {
    return new Promise(async (resolve) => {
        // Flat structure: extract directly into binariesDir (backend/bin/)
        fs.mkdirSync(binariesDir, { recursive: true });

        if (statusCallback) statusCallback("Downloading FFmpeg...");
        console.log("[DEP] Downloading FFmpeg...");

        const tempDir = path.join(binariesDir, "_ffmpeg_temp");
        fs.mkdirSync(tempDir, { recursive: true });
        const zipPath = path.join(tempDir, "ffmpeg-temp.zip");

        try {
            await downloadFile(FFMPEG_DOWNLOAD_URL, zipPath, (progress) => {
                if (statusCallback) statusCallback(`Downloading FFmpeg... ${progress}%`);
            });

            if (statusCallback) statusCallback("Extracting FFmpeg...");
            console.log("[DEP] Extracting FFmpeg...");

            // Extract to temp dir to avoid polluting backend/bin/
            await extractZip(zipPath, tempDir);

            // Find ffmpeg.exe and ffprobe.exe in the extracted tree and copy to flat bin/
            const extracted = findFilesRecursive(tempDir, ["ffmpeg.exe", "ffprobe.exe"]);

            for (const file of extracted) {
                const dest = path.join(binariesDir, path.basename(file));
                fs.copyFileSync(file, dest);
                console.log(`[DEP] Copied ${path.basename(file)} → ${binariesDir}`);
            }

            // Cleanup temp directory entirely
            try {
                fs.rmSync(tempDir, { recursive: true, force: true });
            } catch (cleanupErr) {
                console.warn("[DEP] Cleanup warning:", cleanupErr.message);
            }

            // Verify
            const check = checkFfmpeg(binariesDir);
            if (check.ok) {
                if (statusCallback) statusCallback("FFmpeg installed ✓");
                console.log("[DEP] FFmpeg installed successfully at", binariesDir);
                resolve({ ok: true, path: binariesDir });
            } else {
                resolve({ ok: false, error: "FFmpeg extraction failed — files not found after extract" });
            }
        } catch (e) {
            console.error("[DEP] FFmpeg download/extract failed:", e.message);
            resolve({ ok: false, error: e.message });
        }
    });
}

// ═══════════════════════════════════════════════════════════
// Helpers: Download file with redirect following
// ═══════════════════════════════════════════════════════════
function downloadFile(url, destPath, progressCallback = null, maxRedirects = 5) {
    return new Promise((resolve, reject) => {
        if (maxRedirects <= 0) {
            return reject(new Error("Too many redirects"));
        }

        const protocol = url.startsWith("https") ? https : http;
        const file = fs.createWriteStream(destPath);

        protocol.get(url, { headers: { "User-Agent": "DanyDownloader/1.0" } }, (response) => {
            // Handle redirects
            if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
                file.close();
                fs.unlinkSync(destPath);
                return downloadFile(response.headers.location, destPath, progressCallback, maxRedirects - 1)
                    .then(resolve)
                    .catch(reject);
            }

            if (response.statusCode !== 200) {
                file.close();
                fs.unlinkSync(destPath);
                return reject(new Error(`Download failed: HTTP ${response.statusCode}`));
            }

            const totalSize = parseInt(response.headers["content-length"] || "0", 10);
            let downloaded = 0;
            let lastReportedPercent = -1;

            response.on("data", (chunk) => {
                downloaded += chunk.length;
                if (totalSize > 0 && progressCallback) {
                    const percent = Math.floor((downloaded / totalSize) * 100);
                    if (percent !== lastReportedPercent) {
                        lastReportedPercent = percent;
                        progressCallback(percent);
                    }
                }
            });

            response.pipe(file);
            file.on("finish", () => {
                file.close();
                resolve();
            });
        }).on("error", (err) => {
            file.close();
            try { fs.unlinkSync(destPath); } catch (_) {}
            reject(err);
        });
    });
}

// ═══════════════════════════════════════════════════════════
// Helpers: Extract ZIP using PowerShell
// ═══════════════════════════════════════════════════════════
function extractZip(zipPath, destDir) {
    return new Promise((resolve, reject) => {
        const cmd = `powershell -NoProfile -Command "Expand-Archive -Path '${zipPath.replace(/'/g, "''")}' -DestinationPath '${destDir.replace(/'/g, "''")}' -Force"`;
        exec(cmd, { windowsHide: true, timeout: 120000 }, (err, stdout, stderr) => {
            if (err) {
                reject(new Error(`ZIP extraction failed: ${stderr || err.message}`));
            } else {
                resolve();
            }
        });
    });
}

// ═══════════════════════════════════════════════════════════
// Helpers: Recursively find files by name
// ═══════════════════════════════════════════════════════════
function findFilesRecursive(dir, fileNames, maxDepth = 5) {
    const found = [];
    if (maxDepth <= 0) return found;

    try {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const entry of entries) {
            const fullPath = path.join(dir, entry.name);
            if (entry.isFile() && fileNames.includes(entry.name.toLowerCase())) {
                found.push(fullPath);
            } else if (entry.isDirectory()) {
                found.push(...findFilesRecursive(fullPath, fileNames, maxDepth - 1));
            }
        }
    } catch (e) {
        // permission error, skip
    }
    return found;
}

// ═══════════════════════════════════════════════════════════
// Master: Run full dependency check
// ═══════════════════════════════════════════════════════════
async function runFullCheck(binariesDir = null) {
    if (!binariesDir) binariesDir = getBinariesDir();

    console.log("[DEP] ════════════════════════════════════════");
    console.log("[DEP] Running full dependency check...");
    console.log("[DEP] Binaries dir:", binariesDir);

    const result = {
        python: { ok: false, version: null },
        ytdlp: { ok: false, version: null },
        ffmpeg: { ok: false, path: null },
        allOk: false
    };

    // 1. Python
    const pyCheck = await checkPython();
    result.python = pyCheck;
    console.log(`[DEP] Python: ${pyCheck.ok ? "✅ " + pyCheck.version : "❌ " + pyCheck.error}`);

    if (!pyCheck.ok) {
        console.log("[DEP] ❌ Cannot proceed without Python");
        return result;
    }

    // 2. yt-dlp module
    const ytCheck = await checkYtDlpModule(pyCheck.cmd);
    result.ytdlp = ytCheck;
    console.log(`[DEP] yt-dlp: ${ytCheck.ok ? "✅ v" + ytCheck.version : "❌ " + ytCheck.error}`);

    // 3. FFmpeg
    const ffCheck = checkFfmpeg(binariesDir);
    result.ffmpeg = ffCheck;
    console.log(`[DEP] FFmpeg: ${ffCheck.ok ? "✅ " + ffCheck.path : "❌ " + ffCheck.error}`);

    result.allOk = result.python.ok && result.ytdlp.ok && result.ffmpeg.ok;
    console.log(`[DEP] All OK: ${result.allOk ? "✅" : "❌"}`);
    console.log("[DEP] ════════════════════════════════════════");

    return result;
}

// ═══════════════════════════════════════════════════════════
// Master: Install all missing dependencies
// ═══════════════════════════════════════════════════════════
async function installMissing(checkResult, binariesDir = null, statusCallback = null) {
    if (!binariesDir) binariesDir = getBinariesDir();

    const results = { ytdlp: null, ffmpeg: null };

    if (!checkResult.python.ok) {
        return { ok: false, error: "Python is not installed. Please install Python 3.10+ from python.org first.", results };
    }

    // Install yt-dlp if missing
    if (!checkResult.ytdlp.ok) {
        results.ytdlp = await installYtDlp(checkResult.python.cmd, statusCallback);
    } else {
        results.ytdlp = { ok: true, skipped: true };
    }

    // Install FFmpeg if missing
    if (!checkResult.ffmpeg.ok) {
        results.ffmpeg = await downloadFfmpeg(binariesDir, statusCallback);
    } else {
        results.ffmpeg = { ok: true, skipped: true };
    }

    const allOk = (results.ytdlp?.ok ?? false) && (results.ffmpeg?.ok ?? false);
    return { ok: allOk, results };
}

module.exports = {
    getBinariesDir,
    checkPython,
    checkYtDlpModule,
    checkFfmpeg,
    installYtDlp,
    downloadFfmpeg,
    runFullCheck,
    installMissing
};
