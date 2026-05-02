// danyv1/electron/preload.js

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
    // ── Preview ──
    fetchVideoInfo: (url) => ipcRenderer.invoke("fetch-video-info", url),

    // ── Download ──
    downloadVideo: (options) => ipcRenderer.invoke("download-video", options),

    // ── Progress: basic percent ──
    onDownloadProgress: (callback) => {
        ipcRenderer.on("download-progress", (_event, percent) => callback(percent));
    },
    removeDownloadProgressListener: () => {
        ipcRenderer.removeAllListeners("download-progress");
    },

    // ── Progress: rich detail (speed, ETA, stage) ──
    onDownloadProgressDetail: (callback) => {
        ipcRenderer.on("download-progress-detail", (_event, data) => callback(data));
    },
    removeDownloadProgressDetailListener: () => {
        ipcRenderer.removeAllListeners("download-progress-detail");
    },

    // ── Cancel ──
    cancelDownload: (taskId) => ipcRenderer.invoke("cancel-download", taskId),

    // ── History ──
    getDownloadHistory: () => ipcRenderer.invoke("get-download-history"),
    addDownloadHistory: (entry) => ipcRenderer.invoke("add-download-history", entry),
    removeDownloadHistory: (id) => ipcRenderer.invoke("remove-download-history", id),
    clearDownloadHistory: () => ipcRenderer.invoke("clear-download-history"),

    // ── File System ──
    openFileLocation: (filePath) => ipcRenderer.invoke("open-file-location", filePath),
    openDownloadsFolder: () => ipcRenderer.invoke("open-downloads-folder"),

    // ── Dependency Management ──
    onDepCheckResult: (callback) => {
        ipcRenderer.on("dep-check-result", (_event, data) => callback(data));
    },
    onDepInstallStatus: (callback) => {
        ipcRenderer.on("dep-install-status", (_event, status) => callback(status));
    },
    installDependencies: () => ipcRenderer.invoke("install-dependencies"),
    retryDepCheck: () => ipcRenderer.invoke("retry-dep-check"),

    // ── About / Support ──
    getAppVersion: () => ipcRenderer.invoke("get-app-version"),
    openExternalUrl: (url) => ipcRenderer.invoke("open-external-url", url),

    // ── Auto-Updater ──
    onUpdateAvailable: (callback) => {
        ipcRenderer.on("update-available", (_event, info) => callback(info));
    },
    onUpdateProgress: (callback) => {
        ipcRenderer.on("update-progress", (_event, info) => callback(info));
    },
    onUpdateDownloaded: (callback) => {
        ipcRenderer.on("update-downloaded", (_event, info) => callback(info));
    },
    installUpdate: () => ipcRenderer.invoke("install-update")
});