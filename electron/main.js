// danyv1/electron/main.js

const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, exec, execSync } = require("child_process");
const depManager = require("./dep-manager");
const { autoUpdater } = require("electron-updater");

// ═══════════════════════════════════════════════════════════
// Paths
// ═══════════════════════════════════════════════════════════
const binariesDir = depManager.getBinariesDir();

function resolveFfmpegDir() {
    if (app.isPackaged) {
        const appDataDir = depManager.getAppDataBinariesDir();
        if (fs.existsSync(path.join(appDataDir, "ffmpeg.exe"))) {
            return appDataDir;
        }
    }
    return binariesDir;
}

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
    fs.promises.writeFile(historyPath, JSON.stringify(history, null, 2), "utf8")
        .catch(e => console.error("[History] Failed to save asynchronously:", e.message));
}

// Active download process reference (for cancel support)
const activeDownloads = new Map();

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

    // ─── Security: Prevent Drag & Drop Navigation ───
    win.webContents.on("will-navigate", (event, url) => {
        event.preventDefault();
        console.log(`[Security] Blocked navigation to: ${url}`);
    });
    
    win.webContents.on("will-prevent-unload", (event) => {
        event.preventDefault();
    });
}

// ═══════════════════════════════════════════════════════════
// IPC: fetch-video-info — Command-based backend call
// ═══════════════════════════════════════════════════════════
ipcMain.handle("fetch-video-info", async (event, url) => {
    console.log("[IPC] ────────────────────────────────────────");
    console.log("[IPC] Received 'fetch-video-info' from renderer");
    console.log("[IPC] URL:", url);

    const executablePath = path.join(binariesDir, "fetch_video_info.exe");
    const isPacked = app.isPackaged;

    console.log("[IPC] Binaries directory:", binariesDir);
    console.log("[IPC] Executable path:", executablePath);
    console.log("[IPC] File exists:", fs.existsSync(executablePath));
    console.log("[IPC] Packaged mode:", isPacked);
    console.log("[IPC] Spawning:", executablePath, url);

    return new Promise((resolve) => {
        const backendProcess = spawn(executablePath, [url], {
            cwd: binariesDir,
            env: { ...process.env, DANY_FFMPEG_DIR: resolveFfmpegDir() },
            windowsHide: true
        });

        let stdoutData = "";
        let stderrData = "";

        backendProcess.stdout.on("data", (chunk) => {
            const text = chunk.toString();
            stdoutData += text;
            console.log("[BACKEND stdout]", text.trim());
        });

        backendProcess.stderr.on("data", (chunk) => {
            const text = chunk.toString();
            stderrData += text;
            console.log("[BACKEND stderr]", text.trim());
        });

        backendProcess.on("error", (err) => {
            console.error("[IPC] ❌ Failed to start backend process:", err.message);
            resolve({
                success: false,
                error: "Failed to start backend executable: " + err.message
            });
        });

        backendProcess.on("close", (code) => {
            console.log("[IPC] Backend process exited with code:", code);
            console.log("[IPC] stdout length:", stdoutData.length);
            console.log("[IPC] stderr length:", stderrData.length);

            if (code !== 0 && !stdoutData.trim()) {
                console.error("[IPC] ❌ Backend failed completely. stderr:", stderrData);
                resolve({
                    success: false,
                    error: stderrData || "Backend process failed with code " + code
                });
                return;
            }

            const dataStr = stdoutData.trim();
            if (dataStr.startsWith("<") || !dataStr.includes("{")) {
                console.error("[IPC] ❌ Backend returned HTML or non-JSON");
                resolve({
                    success: false,
                    error: "Network Error: Proxy interception or offline. Raw output: " + dataStr.substring(0, 50)
                });
                return;
            }

            try {
                const result = JSON.parse(dataStr);
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
// IPC: download-video — Command-based backend download call
// ═══════════════════════════════════════════════════════════
ipcMain.handle("download-video", async (event, { url, formatId, taskId }) => {
    console.log("[IPC] ────────────────────────────────────────");
    console.log("[IPC] Received 'download-video' from renderer");
    console.log("[IPC] URL:", url);
    console.log("[IPC] Format:", formatId);

    const executablePath = path.join(binariesDir, "download_video.exe");
    const isPacked = app.isPackaged;
    const platform = detectPlatform(url);
    const downloadFolder = getDownloadFolder(platform);
    if (!taskId) taskId = Date.now().toString();

    console.log("[IPC] Binaries directory:", binariesDir);
    console.log("[IPC] Executable path:", executablePath);
    console.log("[IPC] File exists:", fs.existsSync(executablePath));
    console.log("[IPC] Packaged mode:", isPacked);
    console.log("[IPC] Platform:", platform);
    console.log("[IPC] Download folder:", downloadFolder);
    console.log("[IPC] Task ID:", taskId);
    console.log("[IPC] Spawning:", executablePath, url, downloadFolder, formatId, taskId);

    return new Promise((resolve) => {
        const backendProcess = spawn(executablePath, [url, downloadFolder, formatId, taskId], {
            cwd: binariesDir,
            env: { ...process.env, DANY_FFMPEG_DIR: resolveFfmpegDir() },
            windowsHide: true
        });

        // Store reference for cancel support
        activeDownloads.set(taskId, backendProcess);

        let stdoutData = "";

        backendProcess.stdout.on("data", (chunk) => {
            const text = chunk.toString();
            stdoutData += text;
            console.log("[BACKEND DL stdout]", text.trim());
        });

        backendProcess.stderr.on("data", (chunk) => {
            const text = chunk.toString();
            console.log("[BACKEND DL stderr]", text.trim());

            // Parse stderr lines for progress data
            const lines = text.split("\n");
            for (const line of lines) {
                const trimmed = line.trim();

                // Rich progress: DLPROGRESS:{json}
                if (trimmed.startsWith("DLPROGRESS:")) {
                    try {
                        const jsonStr = trimmed.substring("DLPROGRESS:".length);
                        const data = JSON.parse(jsonStr);
                        if (!event.sender.isDestroyed()) {
                            event.sender.send("download-progress-detail", data);
                            // Also send basic percent for backward compat
                            if (typeof data.percent === "number") {
                                event.sender.send("download-progress", data.percent);
                            }
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
                        if (!event.sender.isDestroyed()) {
                            event.sender.send("download-progress", percent);
                        }
                    }
                }
            }
        });

        backendProcess.on("error", (err) => {
            activeDownloads.delete(taskId);
            console.error("[IPC] ❌ Failed to start backend download:", err.message);
            resolve({
                success: false,
                error: "Failed to start backend executable: " + err.message
            });
        });

        backendProcess.on("close", (code) => {
            activeDownloads.delete(taskId);
            console.log("[IPC] Backend download exited with code:", code);
            console.log("[IPC] stdout length:", stdoutData.length);

            if (code !== 0 && !stdoutData.trim()) {
                console.error("[IPC] ❌ Download process failed with code:", code);
                resolve({
                    success: false,
                    error: "Download process failed with code " + code
                });
                return;
            }

            const dataStr = stdoutData.trim();
            if (dataStr.startsWith("<") || !dataStr.includes("{")) {
                console.error("[IPC] ❌ Backend returned HTML or non-JSON");
                resolve({
                    success: false,
                    error: "Network Error: Proxy interception or offline. Raw output: " + dataStr.substring(0, 50)
                });
                return;
            }

            try {
                const result = JSON.parse(dataStr);
                if (result.success === true && result.filename) {
                    console.log("[IPC] ✅ Download JSON parsed. Success:", result.success);
                    console.log("[IPC] Filename:", result.filename);
                    if (result.title) console.log("[IPC] Title:", result.title);
                    
                    // Add download folder path to result for history
                    result._downloadFolder = downloadFolder;
                    resolve(result);
                } else {
                    throw new Error(result.error || "Missing success flag or filename in response");
                }
            } catch (err) {
                console.error("[IPC] ❌ Download validation/parse failed:", err.message);
                console.error("[IPC] Raw stdout:", stdoutData);
                resolve({
                    success: false,
                    error: "Invalid or failed response from download backend: " + err.message
                });
            }
        });
    });
});

// ═══════════════════════════════════════════════════════════
// IPC: cancel-download — Kill active download process
// ═══════════════════════════════════════════════════════════
ipcMain.handle("cancel-download", async (event, taskId) => {
    const proc = activeDownloads.get(taskId);
    if (proc) {
        console.log(`[IPC] 🛑 Killing process tree for task ${taskId} (PID: ${proc.pid})...`);
        try {
            if (process.platform === "win32") {
                exec(`taskkill /pid ${proc.pid} /t /f`);
            } else {
                proc.kill();
            }
            activeDownloads.delete(taskId);
            return { success: true };
        } catch (e) {
            console.error("[IPC] Failed to kill process:", e.message);
            return { success: false, error: e.message };
        }
    }
    return { success: false, error: "No active download for that task" };
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
    if (!filePath) return { success: false, error: "No path provided" };
    
    const downloadsPath = app.getPath("downloads");
    const resolvedPath = path.resolve(filePath);
    
    if (!resolvedPath.startsWith(downloadsPath)) {
        console.error(`[Security] Blocked path traversal attempt: ${resolvedPath}`);
        return { success: false, error: "Invalid path: Outside of downloads directory" };
    }

    if (fs.existsSync(resolvedPath)) {
        shell.showItemInFolder(resolvedPath);
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

// ═══════════════════════════════════════════════════════════
// Auto-Updater Configuration
// ═══════════════════════════════════════════════════════════
autoUpdater.autoDownload = true;
autoUpdater.autoInstallOnAppQuit = true;

autoUpdater.on("update-available", (info) => {
    console.log("[Updater] Update available:", info.version);
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0 && !wins[0].webContents.isDestroyed()) {
        wins[0].webContents.send("update-available", info);
    }
});

autoUpdater.on("download-progress", (progressObj) => {
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0 && !wins[0].webContents.isDestroyed()) {
        wins[0].webContents.send("update-progress", progressObj);
    }
});

autoUpdater.on("update-downloaded", (info) => {
    console.log("[Updater] Update downloaded:", info.version);
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0 && !wins[0].webContents.isDestroyed()) {
        wins[0].webContents.send("update-downloaded", info);
    }
});

autoUpdater.on("error", (err) => {
    console.error("[Updater] Error:", err.message);
    // Clear cache to prevent infinite update loop on corrupted download
    const cachePath = path.join(app.getPath("userData"), "..", app.getName() + "-updater");
    fs.promises.rm(cachePath, { recursive: true, force: true })
        .then(() => console.log("[Updater] Cleared corrupted update cache"))
        .catch(() => {});
});

ipcMain.handle("install-update", () => {
    autoUpdater.quitAndInstall();
});

app.whenReady().then(() => {
    createWindow();

    app.on("activate", () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });

    if (app.isPackaged) {
        autoUpdater.checkForUpdatesAndNotify().catch(err => {
            console.error("[Updater] Failed to check for updates:", err);
        });
    }
});

app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
        app.quit();
    }
});

app.on("before-quit", () => {
    console.log("[LIFECYCLE] before-quit triggered. Cleaning up active processes...");
    for (const pid of activeDownloads.keys()) {
        try {
            const processRef = activeDownloads.get(pid);
            if (processRef && processRef.pid) {
                const killCmd = `taskkill /pid ${processRef.pid} /T /F`;
                execSync(killCmd, { windowsHide: true });
                console.log(`[LIFECYCLE] Force killed zombie process PID: ${processRef.pid}`);
            }
        } catch (e) {
            console.error(`[LIFECYCLE] Failed to kill process ${pid}:`, e.message);
        }
    }
});