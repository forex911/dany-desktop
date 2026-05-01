// danyv1/electron/main.js

const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const depManager = require("./dep-manager");

// ═══════════════════════════════════════════════════════════
// Paths
// ═══════════════════════════════════════════════════════════
const binariesDir = depManager.getBinariesDir();
const ffmpegDir = path.join(binariesDir, "ffmpeg");

// Platform-specific download subfolder mapping
const PLATFORM_FOLDERS = {
    youtube: "YouTube",
    spotify: "Music",
    instagram: "Instagram",
    pinterest: "Pinterest"
};

function getDownloadFolder(platform) {
    const base = app.getPath("downloads");
    const sub = PLATFORM_FOLDERS[platform] || "";
    const folder = sub ? path.join(base, sub) : base;
    fs.mkdirSync(folder, { recursive: true });
    return folder;
}

// Detect platform from URL (mirrors Python logic)
function detectPlatform(url) {
    const u = (url || "").toLowerCase();
    if (u.includes("youtube.com") || u.includes("youtu.be")) return "youtube";
    if (u.includes("spotify.com") || u.includes("spotify.link")) return "spotify";
    if (u.includes("instagram.com") || u.includes("instagr.am")) return "instagram";
    if (u.includes("pinterest.com") || u.includes("pin.it")) return "pinterest";
    return "unknown";
}

// ═══════════════════════════════════════════════════════════
// Download History — persistent JSON storage
// ═══════════════════════════════════════════════════════════
const historyPath = path.join(app.getPath("userData"), "download_history.json");

function loadHistory() {
    try {
        if (fs.existsSync(historyPath)) {
            return JSON.parse(fs.readFileSync(historyPath, "utf8"));
        }
    } catch (e) {
        console.error("[History] Failed to load:", e.message);
    }
    return [];
}

function saveHistory(history) {
    try {
        fs.writeFileSync(historyPath, JSON.stringify(history, null, 2), "utf8");
    } catch (e) {
        console.error("[History] Failed to save:", e.message);
    }
}

// Active download process reference (for cancel support)
let activeDownloadProcess = null;

function createWindow() {
    const win = new BrowserWindow({
        width: 950,
        height: 650,
        minWidth: 850,
        minHeight: 600,
        autoHideMenuBar: true,
        titleBarStyle: "hidden",
        titleBarOverlay: {
            color: "rgba(0,0,0,0)",
            symbolColor: "#ffffff",
            height: 36
        },
        icon: path.join(__dirname, "../frontend/assets/icons/icon.png"),
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false
        }
    });

    win.removeMenu();

    // Load frontend HTML
    win.loadFile(path.join(__dirname, "../frontend/index.html"));

    // ─── Run dependency check after page loads ───
    win.webContents.on("did-finish-load", async () => {
        console.log("[DEP] Window loaded — running startup dependency check...");
        const result = await depManager.runFullCheck(binariesDir);
        try {
            win.webContents.send("dep-check-result", result);
        } catch (e) {
            console.error("[DEP] Failed to send dep-check-result:", e.message);
        }
    });
}

// ═══════════════════════════════════════════════════════════
// IPC: fetch-video-info — Command-based Python backend call
// ═══════════════════════════════════════════════════════════
ipcMain.handle("fetch-video-info", async (event, url) => {
    console.log("[IPC] ────────────────────────────────────────");
    console.log("[IPC] Received 'fetch-video-info' from renderer");
    console.log("[IPC] URL:", url);

    const backendDir = path.join(__dirname, "..", "backend");
    const scriptPath = path.join(backendDir, "fetch_video_info.py");

    console.log("[IPC] Backend directory:", backendDir);
    console.log("[IPC] Script path:", scriptPath);
    console.log("[IPC] Spawning: python", scriptPath, url);

    return new Promise((resolve) => {
        const pythonProcess = spawn("python", [scriptPath, url], {
            cwd: backendDir,
            env: { ...process.env, DANY_FFMPEG_DIR: ffmpegDir },
            windowsHide: true
        });

        let stdoutData = "";
        let stderrData = "";

        pythonProcess.stdout.on("data", (chunk) => {
            const text = chunk.toString();
            stdoutData += text;
            console.log("[PYTHON stdout]", text.trim());
        });

        pythonProcess.stderr.on("data", (chunk) => {
            const text = chunk.toString();
            stderrData += text;
            console.log("[PYTHON stderr]", text.trim());
        });

        pythonProcess.on("error", (err) => {
            console.error("[IPC] ❌ Failed to start Python process:", err.message);
            resolve({
                success: false,
                error: "Failed to start Python: " + err.message
            });
        });

        pythonProcess.on("close", (code) => {
            console.log("[IPC] Python process exited with code:", code);
            console.log("[IPC] stdout length:", stdoutData.length);
            console.log("[IPC] stderr length:", stderrData.length);

            if (code !== 0 && !stdoutData.trim()) {
                console.error("[IPC] ❌ Python failed completely. stderr:", stderrData);
                resolve({
                    success: false,
                    error: stderrData || "Backend process failed with code " + code
                });
                return;
            }

            try {
                const result = JSON.parse(stdoutData.trim());
                console.log("[IPC] ✅ JSON parsed successfully");
                console.log("[IPC] Response success:", result.success);
                if (result.title) console.log("[IPC] Title:", result.title);
                resolve(result);
            } catch (err) {
                console.error("[IPC] ❌ JSON parse failed:", err.message);
                console.error("[IPC] Raw stdout:", stdoutData);
                resolve({
                    success: false,
                    error: "Invalid JSON from backend: " + err.message
                });
            }
        });
    });
});

// ═══════════════════════════════════════════════════════════
// IPC: download-video — Command-based Python download call
// ═══════════════════════════════════════════════════════════
ipcMain.handle("download-video", async (event, { url, formatId }) => {
    console.log("[IPC] ────────────────────────────────────────");
    console.log("[IPC] Received 'download-video' from renderer");
    console.log("[IPC] URL:", url);
    console.log("[IPC] Format:", formatId);

    const backendDir = path.join(__dirname, "..", "backend");
    const scriptPath = path.join(backendDir, "download_video.py");
    const platform = detectPlatform(url);
    const downloadFolder = getDownloadFolder(platform);
    const taskId = Date.now().toString();

    console.log("[IPC] Backend directory:", backendDir);
    console.log("[IPC] Script path:", scriptPath);
    console.log("[IPC] Platform:", platform);
    console.log("[IPC] Download folder:", downloadFolder);
    console.log("[IPC] Task ID:", taskId);
    console.log("[IPC] Spawning: python", scriptPath, url, downloadFolder, formatId, taskId);

    return new Promise((resolve) => {
        const pythonProcess = spawn("python", [scriptPath, url, downloadFolder, formatId, taskId], {
            cwd: backendDir,
            env: { ...process.env, DANY_FFMPEG_DIR: ffmpegDir },
            windowsHide: true
        });

        // Store reference for cancel support
        activeDownloadProcess = pythonProcess;

        let stdoutData = "";

        pythonProcess.stdout.on("data", (chunk) => {
            const text = chunk.toString();
            stdoutData += text;
            console.log("[PYTHON DL stdout]", text.trim());
        });

        pythonProcess.stderr.on("data", (chunk) => {
            const text = chunk.toString();
            console.log("[PYTHON DL stderr]", text.trim());

            // Parse stderr lines for progress data
            const lines = text.split("\n");
            for (const line of lines) {
                const trimmed = line.trim();

                // Rich progress: DLPROGRESS:{json}
                if (trimmed.startsWith("DLPROGRESS:")) {
                    try {
                        const jsonStr = trimmed.substring("DLPROGRESS:".length);
                        const data = JSON.parse(jsonStr);
                        event.sender.send("download-progress-detail", data);
                        // Also send basic percent for backward compat
                        if (typeof data.percent === "number") {
                            event.sender.send("download-progress", data.percent);
                        }
                    } catch (e) {
                        // Ignore malformed DLPROGRESS
                    }
                    continue;
                }

                // Legacy progress: PROGRESS:xx.x
                if (trimmed.startsWith("PROGRESS:")) {
                    const percent = parseFloat(trimmed.replace("PROGRESS:", ""));
                    if (!isNaN(percent)) {
                        try {
                            event.sender.send("download-progress", percent);
                        } catch (e) {
                            // Renderer may have been destroyed
                        }
                    }
                }
            }
        });

        pythonProcess.on("error", (err) => {
            activeDownloadProcess = null;
            console.error("[IPC] ❌ Failed to start Python download:", err.message);
            resolve({
                success: false,
                error: "Failed to start Python: " + err.message
            });
        });

        pythonProcess.on("close", (code) => {
            activeDownloadProcess = null;
            console.log("[IPC] Python download exited with code:", code);
            console.log("[IPC] stdout length:", stdoutData.length);

            if (code !== 0 && !stdoutData.trim()) {
                console.error("[IPC] ❌ Download process failed with code:", code);
                resolve({
                    success: false,
                    error: "Download process failed with code " + code
                });
                return;
            }

            try {
                const result = JSON.parse(stdoutData.trim());
                console.log("[IPC] ✅ Download JSON parsed. Success:", result.success);
                if (result.filename) console.log("[IPC] Filename:", result.filename);
                if (result.title) console.log("[IPC] Title:", result.title);
                
                // Add download folder path to result for history
                result._downloadFolder = downloadFolder;
                
                resolve(result);
            } catch (err) {
                console.error("[IPC] ❌ Download JSON parse failed:", err.message);
                console.error("[IPC] Raw stdout:", stdoutData);
                resolve({
                    success: false,
                    error: "Invalid JSON from download backend: " + err.message
                });
            }
        });
    });
});

// ═══════════════════════════════════════════════════════════
// IPC: cancel-download — Kill active download process
// ═══════════════════════════════════════════════════════════
ipcMain.handle("cancel-download", async () => {
    if (activeDownloadProcess) {
        console.log("[IPC] 🛑 Killing active download process...");
        try {
            activeDownloadProcess.kill();
            activeDownloadProcess = null;
            return { success: true };
        } catch (e) {
            console.error("[IPC] Failed to kill process:", e.message);
            return { success: false, error: e.message };
        }
    }
    return { success: false, error: "No active download" };
});

// ═══════════════════════════════════════════════════════════
// IPC: Download History
// ═══════════════════════════════════════════════════════════
ipcMain.handle("get-download-history", async () => {
    return loadHistory();
});

ipcMain.handle("add-download-history", async (event, entry) => {
    const history = loadHistory();
    history.unshift(entry); // newest first
    // Keep max 100 entries
    if (history.length > 100) history.length = 100;
    saveHistory(history);
    return { success: true };
});

ipcMain.handle("remove-download-history", async (event, id) => {
    let history = loadHistory();
    history = history.filter(h => h.id !== id);
    saveHistory(history);
    return { success: true };
});

ipcMain.handle("clear-download-history", async () => {
    saveHistory([]);
    return { success: true };
});

// ═══════════════════════════════════════════════════════════
// IPC: File System — Open file in Explorer
// ═══════════════════════════════════════════════════════════
ipcMain.handle("open-file-location", async (event, filePath) => {
    if (filePath && fs.existsSync(filePath)) {
        shell.showItemInFolder(filePath);
        return { success: true };
    }
    return { success: false, error: "File not found" };
});

ipcMain.handle("open-downloads-folder", async () => {
    const downloadsPath = app.getPath("downloads");
    shell.openPath(downloadsPath);
    return { success: true };
});

// ═══════════════════════════════════════════════════════════
// IPC: Dependency management
// ═══════════════════════════════════════════════════════════
ipcMain.handle("install-dependencies", async (event) => {
    console.log("[DEP] Install requested from renderer...");
    const check = await depManager.runFullCheck(binariesDir);

    const result = await depManager.installMissing(check, binariesDir, (status) => {
        try {
            event.sender.send("dep-install-status", status);
        } catch (e) { /* renderer may be gone */ }
    });

    // Re-check after install
    const finalCheck = await depManager.runFullCheck(binariesDir);
    try {
        event.sender.send("dep-check-result", finalCheck);
    } catch (e) { /* renderer may be gone */ }

    return { ok: result.ok, finalCheck };
});

ipcMain.handle("retry-dep-check", async (event) => {
    console.log("[DEP] Retry check requested...");
    const result = await depManager.runFullCheck(binariesDir);
    try {
        event.sender.send("dep-check-result", result);
    } catch (e) { /* renderer may be gone */ }
    return result;
});

// ═══════════════════════════════════════════════════════════
// IPC: About / Support Handlers
// ═══════════════════════════════════════════════════════════
ipcMain.handle("get-app-version", () => {
    return app.getVersion();
});

ipcMain.handle("open-external-url", (event, url) => {
    // Only allow http and https protocols for safety
    if (url.startsWith("http://") || url.startsWith("https://")) {
        shell.openExternal(url);
    }
});

app.whenReady().then(() => {
    createWindow();

    app.on("activate", () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
        app.quit();
    }
});