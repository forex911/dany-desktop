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

function getAppDataBinariesDir() {
    // Return %APPDATA%\dany-downloader\bin for writeable recovery
    return path.join(require("electron").app.getPath("userData"), "bin");
}

// ═══════════════════════════════════════════════════════════
// Check: Backend Executables in managed folder?
// ═══════════════════════════════════════════════════════════
function checkBackend(binariesDir) {
    const fetchPath = path.join(binariesDir, "fetch_video_info.exe");
    const downloadPath = path.join(binariesDir, "download_video.exe");
    const ffmpegPath = path.join(binariesDir, "ffmpeg.exe");
    const ffprobePath = path.join(binariesDir, "ffprobe.exe");

    const fetchExists = fs.existsSync(fetchPath);
    const downloadExists = fs.existsSync(downloadPath);
    const ffmpegExists = fs.existsSync(ffmpegPath);
    const ffprobeExists = fs.existsSync(ffprobePath);

    let missing = [];
    if (!fetchExists) missing.push("fetch_video_info.exe");
    if (!downloadExists) missing.push("download_video.exe");
    
    // Check AppData fallback for FFmpeg if missing in primary
    let ffmpegResolved = ffmpegExists;
    let ffprobeResolved = ffprobeExists;
    
    if (!ffmpegExists || !ffprobeExists) {
        try {
            const appDataDir = getAppDataBinariesDir();
            if (fs.existsSync(path.join(appDataDir, "ffmpeg.exe")) && fs.existsSync(path.join(appDataDir, "ffprobe.exe"))) {
                ffmpegResolved = true;
                ffprobeResolved = true;
            }
        } catch(e) {}
    }

    if (!ffmpegResolved) missing.push("ffmpeg.exe");
    if (!ffprobeResolved) missing.push("ffprobe.exe");

    if (fetchExists && downloadExists && ffmpegResolved && ffprobeResolved) {
        return { ok: true, path: binariesDir };
    }

    return {
        ok: false,
        error: `Missing: ${missing.join(", ")}`,
        path: binariesDir
    };
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
            const check = checkBackend(binariesDir);
            if (check.ok || (fs.existsSync(path.join(binariesDir, "ffmpeg.exe")) && fs.existsSync(path.join(binariesDir, "ffprobe.exe")))) {
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
        exec(cmd, { windowsHide: true, timeout: 300000 }, (err, stdout, stderr) => {
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
        backend: { ok: false, path: null },
        ffmpeg: { ok: false, path: null }, // for frontend compatibility
        python: { ok: true, version: "Bundled" }, // mock for frontend compatibility
        ytdlp: { ok: true, version: "Bundled" }, // mock for frontend compatibility
        allOk: false
    };

    const backendCheck = checkBackend(binariesDir);
    result.backend = backendCheck;
    result.ffmpeg = backendCheck; // they share the same folder

    console.log(`[DEP] Backend Executables: ${backendCheck.ok ? "✅ " + backendCheck.path : "❌ " + backendCheck.error}`);

    result.allOk = result.backend.ok;
    console.log(`[DEP] All OK: ${result.allOk ? "✅" : "❌"}`);
    console.log("[DEP] ════════════════════════════════════════");

    return result;
}

// ═══════════════════════════════════════════════════════════
// Master: Install all missing dependencies
// ═══════════════════════════════════════════════════════════
async function installMissing(checkResult, binariesDir = null, statusCallback = null) {
    if (!binariesDir) binariesDir = getBinariesDir();

    const results = { ffmpeg: null };

    const fetchPath = path.join(binariesDir, "fetch_video_info.exe");
    const downloadPath = path.join(binariesDir, "download_video.exe");
    if (!fs.existsSync(fetchPath) || !fs.existsSync(downloadPath)) {
         return { ok: false, error: "Antivirus blocked core executables (fetch_video_info.exe or download_video.exe missing). Please restore them from your antivirus quarantine.", results };
    }

    const ffmpegPath = path.join(binariesDir, "ffmpeg.exe");
    if (!fs.existsSync(ffmpegPath)) {
        // ALWAYS extract to AppData in production to avoid EPERM on C:\Program Files
        const isPacked = require("electron").app?.isPackaged ?? false;
        const targetDir = isPacked ? getAppDataBinariesDir() : binariesDir;
        
        results.ffmpeg = await downloadFfmpeg(targetDir, statusCallback);
    } else {
        results.ffmpeg = { ok: true, skipped: true };
    }

    const allOk = results.ffmpeg?.ok ?? false;
    return { ok: allOk, results };
}

module.exports = {
    getBinariesDir,
    getAppDataBinariesDir,
    checkBackend,
    downloadFfmpeg,
    runFullCheck,
    installMissing
};
