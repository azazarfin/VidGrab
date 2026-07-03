// Import the API client and utilities
importScripts("../lib/api.js");
importScripts("../lib/utils.js");

const DEFAULT_SETTINGS = {
  backendUrl: "http://localhost:8000",
  defaultFormat: "bestvideo+bestaudio/best",
  defaultQuality: "1080",
  notificationPreference: "both",
};

// Listen for tab removals to clean up tab-specific detected videos
chrome.tabs?.onRemoved?.addListener((tabId) => {
  chrome.storage.local.remove(`detected_videos_${tabId}`);
});

// Initialize settings and context menus on install
chrome.runtime.onInstalled.addListener(() => {
  console.log("VidGrab Service Worker installed.");
  
  // Set default settings in local storage if not already present
  chrome.storage.local.get(Object.keys(DEFAULT_SETTINGS), (result) => {
    const updates = {};
    for (const [key, val] of Object.entries(DEFAULT_SETTINGS)) {
      if (result[key] === undefined) {
        updates[key] = val;
      }
    }
    if (Object.keys(updates).length > 0) {
      chrome.storage.local.set(updates);
    }
  });

  // Clear any old detected video keys on startup/install
  chrome.storage.local.get(null, (items) => {
    const keysToRemove = Object.keys(items).filter(k => k.startsWith("detected_videos_"));
    if (keysToRemove.length > 0) {
      chrome.storage.local.remove(keysToRemove);
    }
  });

  // Create context menu items
  chrome.contextMenus.create({
    id: "vidgrab-download-page",
    title: "Download video from this page",
    contexts: ["page"]
  });

  chrome.contextMenus.create({
    id: "vidgrab-download-link",
    title: "Download linked video",
    contexts: ["link"]
  });

  chrome.contextMenus.create({
    id: "vidgrab-download-video",
    title: "Download this video",
    contexts: ["video"]
  });
});

// Handle context menu clicks
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  let targetUrl = "";

  if (info.menuItemId === "vidgrab-download-video" && info.srcUrl) {
    targetUrl = info.srcUrl;
  } else if (info.menuItemId === "vidgrab-download-link" && info.linkUrl) {
    targetUrl = info.linkUrl;
  } else if (info.menuItemId === "vidgrab-download-page" && info.pageUrl) {
    targetUrl = info.pageUrl;
  }

  if (targetUrl) {
    console.log(`Context menu click triggered download for: ${targetUrl}`);
    await triggerQuickDownload(targetUrl);
  }
});

async function getCookiesForUrl(url) {
  return new Promise((resolve) => {
    if (typeof chrome === 'undefined' || !chrome.cookies) {
      resolve(null);
      return;
    }
    // Skip cookie extraction for localhost/loopback URLs
    try {
      const parsedUrl = new URL(url);
      if (parsedUrl.hostname === 'localhost' || parsedUrl.hostname === '127.0.0.1') {
        resolve(null);
        return;
      }
    } catch (e) {
      resolve(null);
      return;
    }
    chrome.storage.local.get(["cookieForwardingEnabled"], (settings) => {
      if (!settings.cookieForwardingEnabled) {
        resolve(null);
        return;
      }
      try {
        chrome.cookies.getAll({ url }, (cookies) => {
          if (chrome.runtime.lastError) {
            console.warn("Failed to get cookies:", chrome.runtime.lastError);
            resolve(null);
          } else {
            resolve(cookies || null);
          }
        });
      } catch (err) {
        console.warn("Error calling chrome.cookies.getAll:", err);
        resolve(null);
      }
    });
  });
}

/**
 * Sends a quick download request using default settings.
 */
async function triggerQuickDownload(url) {
  try {
    // Show a starting notification
    showNotification("download-start", "VidGrab: Starting Download", "Analyzing video URL...");

    // Get default format
    const settings = await new Promise((resolve) => {
      chrome.storage.local.get(["defaultFormat"], resolve);
    });
    const formatId = settings.defaultFormat || "bestvideo+bestaudio/best";

    // Get cookies if forwarding is enabled
    const cookies = await getCookiesForUrl(url);
    const options = {};
    if (cookies) {
      options.cookies = cookies;
    }

    // Call backend to start download
    const res = await self.vidGrabAPI.startDownload(url, formatId, null, null, options);
    console.log("Download started successfully:", res);

    showNotification("download-started", "VidGrab: Download Queued", "Video has been added to the download queue.");
  } catch (error) {
    console.error("Failed to trigger download from context menu:", error);
    showNotification("download-error", "VidGrab: Download Failed", `Could not start download: ${error.message}`);
  }
}

/**
 * Helper to show system notifications.
 */
function showNotification(id, title, message, contextMessage = "", buttons = []) {
  const options = {
    type: "basic",
    iconUrl: chrome.runtime.getURL("icons/icon128.png"),
    title: title,
    message: message,
    priority: 1
  };
  if (contextMessage) {
    options.contextMessage = contextMessage;
  }
  if (buttons && buttons.length > 0) {
    options.buttons = buttons;
  }
  chrome.notifications.create(id, options);
}

// --- Handle messages from content scripts ---
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "video_detected") {
    const newVideos = message.videos || [];
    if (sender.tab && sender.tab.id) {
      const tabId = sender.tab.id;
      const storageKey = `detected_videos_${tabId}`;
      
      chrome.storage.local.get([storageKey], (result) => {
        const accumulated = result[storageKey] || [];
        const updated = [...accumulated];
        
        newVideos.forEach(newVid => {
          if (!updated.some(v => v.url === newVid.url)) {
            updated.push(newVid);
          }
        });

        // Update badge count
        const total = updated.length;
        const text = total > 0 ? String(total) : "";
        chrome.action.setBadgeText({ text, tabId: tabId });
        chrome.action.setBadgeBackgroundColor({ color: "#7C6BF0", tabId: tabId });

        // Save back
        chrome.storage.local.set({ [storageKey]: updated });
      });
    }
  } else if (message.type === "download_request") {
    // Triggered from the floating overlay button on a page (legacy quick download)
    triggerQuickDownload(message.url);
  } else if (message.type === "open_popup") {
    // Programmatically open the extension popup if supported by the browser
    if (chrome.action && chrome.action.openPopup) {
      chrome.action.openPopup((err) => {
        if (chrome.runtime.lastError) {
          console.warn("VidGrab background: openPopup failed:", chrome.runtime.lastError);
        }
      });
    } else {
      console.warn("VidGrab background: chrome.action.openPopup is not supported in this browser.");
    }
  } else if (message.type === "probe_request") {
    // Triggered from the inline overlay panel to fetch video metadata
    (async () => {
      try {
        const info = await self.vidGrabAPI.probeUrl(message.url);
        const statusData = await self.vidGrabAPI.getStatus();
        sendResponse({ success: true, data: info, ffmpeg_available: statusData.ffmpeg_available !== false });
      } catch (err) {
        sendResponse({ success: false, error: err.message || "Probe failed" });
      }
    })();
    return true; // keep channel open for async sendResponse
  } else if (message.type === "download_with_options") {
    // Triggered from the inline overlay panel with user-selected options
    (async () => {
      try {
        const { url, formatId, title, thumbnailUrl, options } = message;

        // Get cookies if forwarding is enabled
        const cookies = await getCookiesForUrl(url);
        const finalOptions = { ...options };
        if (cookies) {
          finalOptions.cookies = cookies;
        }

        const res = await self.vidGrabAPI.startDownload(url, formatId, title, thumbnailUrl, finalOptions);
        showNotification("download-started", "VidGrab: Download Queued", `${title || "Video"} has been added to the download queue.`);
        sendResponse({ success: true, data: res });
      } catch (err) {
        showNotification("download-error", "VidGrab: Download Failed", `Could not start download: ${err.message}`);
        sendResponse({ success: false, error: err.message || "Download failed" });
      }
    })();
    return true; // keep channel open for async sendResponse
  }
  return false; // synchronous for other message types
});

// Keep-alive and connection management for badge updates
function updateBadge(activeCount) {
  const text = activeCount > 0 ? String(activeCount) : "";
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color: "#7C6BF0" });
}

// Connect to WebSocket and monitor active downloads to update badge
function setupWebSocketMonitoring() {
  self.vidGrabAPI.connectWebSocket();

  self.vidGrabAPI.addEventListener("init_state", (e) => {
    const tasks = e.detail;
    const activeCount = tasks.filter(t => t.status === "downloading" || t.status === "queued" || t.status === "postprocessing").length;
    updateBadge(activeCount);
  });

  self.vidGrabAPI.addEventListener("task_queued", () => refreshActiveCount());
  self.vidGrabAPI.addEventListener("task_started", () => refreshActiveCount());
  self.vidGrabAPI.addEventListener("task_complete", (e) => {
    refreshActiveCount();
    chrome.storage.local.get(["notificationPreference"], (settings) => {
      const pref = settings.notificationPreference || "both";
      if (pref === "both" || pref === "completion") {
        const task = e.detail;
        let options = {};
        try {
          options = JSON.parse(task.download_options || "{}");
        } catch(err){}
        
        let context = `Format: ${task.format_selected || 'default'}`;
        if (task.file_size) {
          context += ` | Size: ${formatBytes(task.file_size)}`;
        }
        if (options.duration) {
          context += ` | Duration: ${formatDuration(options.duration)}`;
        }
        
        showNotification(
          "success|" + task.id,
          "Download Complete",
          `${task.title || "Video"} finished downloading.`,
          context,
          [{ title: "Open Containing Folder" }]
        );
      }
    });
  });
  self.vidGrabAPI.addEventListener("task_failed", (e) => {
    refreshActiveCount();
    chrome.storage.local.get(["notificationPreference"], (settings) => {
      const pref = settings.notificationPreference || "both";
      if (pref === "both" || pref === "failure") {
        const task = e.detail;
        showNotification(
          "failure|" + task.id,
          "Download Failed",
          `${task.title || "Video"} failed.`,
          `Error: ${task.error_message || "Unknown error"}`,
          [{ title: "Retry Download" }]
        );
      }
    });
  });
  self.vidGrabAPI.addEventListener("task_cancelled", () => refreshActiveCount());
}

// Handle notification button clicks
chrome.notifications.onButtonClicked.addListener(async (notificationId, buttonIndex) => {
  const parts = notificationId.split("|");
  if (parts.length < 2) return;
  const status = parts[0];
  const taskId = parts[1];
  
  if (status === "success" && buttonIndex === 0) {
    try {
      await self.vidGrabAPI.openFolder(taskId);
    } catch (err) {
      console.error("Failed to open folder via notification:", err);
    }
  } else if (status === "failure" && buttonIndex === 0) {
    try {
      let cookies = null;
      try {
        const downloads = await self.vidGrabAPI.listDownloads();
        const task = downloads.find(d => d.id === taskId);
        if (task && task.url) {
          cookies = await getCookiesForUrl(task.url);
        }
      } catch (err) {
        console.warn("Failed to get cookies on retry via notification:", err);
      }
      await self.vidGrabAPI.retryDownload(taskId, cookies);
      chrome.notifications.clear(notificationId);
    } catch (err) {
      console.error("Failed to retry download via notification:", err);
    }
  }
});

// Handle notification body clicks
chrome.notifications.onClicked.addListener(async (notificationId) => {
  const parts = notificationId.split("|");
  if (parts.length < 2) return;
  const status = parts[0];
  const taskId = parts[1];
  
  if (status === "success") {
    try {
      await self.vidGrabAPI.openFolder(taskId);
    } catch (err) {
      console.error("Failed to open folder via notification click:", err);
    }
  }
});

async function refreshActiveCount() {
  try {
    // Only fetch active statuses instead of the entire download history
    const [downloading, queued] = await Promise.all([
      self.vidGrabAPI.listDownloads("downloading"),
      self.vidGrabAPI.listDownloads("queued")
    ]);
    const activeCount = downloading.length + queued.length;
    updateBadge(activeCount);
  } catch (e) {
    console.warn("Failed to refresh active count for badge:", e);
  }
}

// Start WebSocket monitoring
setupWebSocketMonitoring();

// Periodically check status to keep service worker alive when downloads are active
chrome.alarms.create("check-downloads", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "check-downloads") {
    refreshActiveCount();
  }
});
