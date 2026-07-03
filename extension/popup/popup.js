document.addEventListener("DOMContentLoaded", () => {
  const api = window.vidGrabAPI;
  
  // Helper to fetch cookies if forwarding is enabled
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
  
  // DOM Elements
  const connectionStatus = document.getElementById("connection-status");
  const backendErrorOverlay = document.getElementById("backend-error-overlay");
  const retryBackendBtn = document.getElementById("retry-backend-btn");
  const settingsBtn = document.getElementById("settings-btn");
  
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabContents = document.querySelectorAll(".tab-content");
  
  const activeList = document.getElementById("active-download-list");
  const queueList = document.getElementById("queue-list");
  const historyList = document.getElementById("history-list");
  
  const downloadsEmpty = document.getElementById("downloads-empty");
  const queueEmpty = document.getElementById("queue-empty");
  const historyEmpty = document.getElementById("history-empty");
  const historyActions = document.getElementById("history-actions");
  const clearHistoryBtn = document.getElementById("clear-history-btn");
  
  const badgeDownloads = document.getElementById("badge-downloads");
  const badgeQueue = document.getElementById("badge-queue");
  const badgeHistory = document.getElementById("badge-history");
  const badgeDetected = document.getElementById("badge-detected");

  const detectedList = document.getElementById("detected-list");
  const detectedEmpty = document.getElementById("detected-empty");

  // Batch / Selection DOM Elements
  const detectedActions = document.getElementById("detected-actions");
  const detectedSelectAll = document.getElementById("detected-select-all");
  const detectedSelectedCountText = document.getElementById("detected-selected-count");
  const detectedDownloadBtn = document.getElementById("detected-download-btn");
  const detectedRemoveBtn = document.getElementById("detected-remove-btn");

  const batchModal = document.getElementById("batch-modal");
  const batchModalCloseBtn = document.getElementById("batch-modal-close-btn");
  const batchModalCancelBtn = document.getElementById("batch-modal-cancel-btn");
  const batchModalStartBtn = document.getElementById("batch-modal-start-btn");
  const batchQualitySelect = document.getElementById("batch-quality-select");
  const batchOptionAudioOnly = document.getElementById("batch-option-audio-only");

  let selectedDetectedUrls = new Set();
  
  const videoUrlInput = document.getElementById("video-url-input");
  const pasteBtn = document.getElementById("paste-btn");
  const downloadBtn = document.getElementById("download-btn");
  
  // Modal Elements
  const formatModal = document.getElementById("format-modal");
  const modalHeaderTitle = document.getElementById("modal-header-title");
  const modalCloseBtn = document.getElementById("modal-close-btn");
  const modalCancelBtn = document.getElementById("modal-cancel-btn");
  const modalStartBtn = document.getElementById("modal-start-btn");
  const modalThumbnail = document.getElementById("modal-thumbnail");
  const modalTitle = document.getElementById("modal-title");
  const modalAuthor = document.getElementById("modal-author");
  const modalDuration = document.getElementById("modal-duration");
  const formatSelect = document.getElementById("format-select");
  const optionAudioOnly = document.getElementById("option-audio-only");
  const optionSubtitles = document.getElementById("option-subtitles");
  const optionThumbnail = document.getElementById("option-thumbnail");

  // Scheduler Elements
  const chkScheduleLater = document.getElementById("chk-schedule-later");
  const scheduleInputs = document.getElementById("schedule-inputs");
  const inputScheduleTime = document.getElementById("input-schedule-time");
  const btnPresetTonight = document.getElementById("btn-preset-tonight");
  const btnPresetTomorrow = document.getElementById("btn-preset-tomorrow");

  // Extended Advanced Modal Elements
  const modalSingleVideoContent = document.getElementById("modal-single-video-content");
  const videoQualityGroup = document.getElementById("video-quality-group");
  const audioFormatGroup = document.getElementById("audio-format-group");
  const audioFormatSelect = document.getElementById("audio-format-select");
  const subtitleOptionsGroup = document.getElementById("subtitle-options-group");
  const subtitleLangSelect = document.getElementById("subtitle-lang-select");
  const subtitleFormatSelect = document.getElementById("subtitle-format-select");
  const optionEmbedSubtitles = document.getElementById("option-embed-subtitles");

  // Playlist Modal Elements
  const modalPlaylistContent = document.getElementById("modal-playlist-content");
  const playlistTitle = document.getElementById("playlist-title");
  const playlistMeta = document.getElementById("playlist-meta");
  const playlistSelectedCount = document.getElementById("playlist-selected-count");
  const playlistSelectAll = document.getElementById("playlist-select-all");
  const playlistDeselectAll = document.getElementById("playlist-deselect-all");
  const playlistEntriesList = document.getElementById("playlist-entries-list");
  const playlistFormatSelect = document.getElementById("playlist-format-select");
  const customTitleInput = document.getElementById("custom-title-input");

  const modalDownloadDirInput = document.getElementById("modal-download-dir-input");
  const modalOutputTemplateInput = document.getElementById("modal-output-template-input");
  const playlistDownloadDirInput = document.getElementById("playlist-download-dir-input");
  const batchDownloadDirInput = document.getElementById("batch-download-dir-input");

  // App State
  let tasksMap = new Map();
  let currentProbedUrl = "";
  let currentProbedData = null;
  let renderAllTimer = null;
  let activeTabId = null;

  // HTML escaping to prevent XSS from untrusted video titles
  function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // URL validation helper
  function isValidUrl(string) {
    try { new URL(string); return true; } catch { return false; }
  }

  // --- Initialize Connections ---
  function connect() {
    api.connectWebSocket();
    checkBackendHealth();
  }

  let ffmpegAvailable = true;

  async function checkBackendHealth() {
    try {
      const statusData = await api.getStatus();
      ffmpegAvailable = statusData.ffmpeg_available !== false;
      onBackendOnline(statusData);
    } catch (e) {
      onBackendOffline();
    }
  }

  function onBackendOnline(statusData) {
    connectionStatus.classList.add("online");
    connectionStatus.querySelector(".status-text").textContent = "Online";
    backendErrorOverlay.classList.remove("active");
    
    const modalFfmpegWarning = document.getElementById("modal-ffmpeg-warning");
    if (statusData && statusData.ffmpeg_available === false) {
      ffmpegAvailable = false;
      if (modalFfmpegWarning) modalFfmpegWarning.style.display = "block";
    } else {
      ffmpegAvailable = true;
      if (modalFfmpegWarning) modalFfmpegWarning.style.display = "none";
    }
    
    refreshLists();
  }

  function onBackendOffline() {
    connectionStatus.classList.remove("online");
    connectionStatus.querySelector(".status-text").textContent = "Offline";
    backendErrorOverlay.classList.add("active");
  }

  // --- API Event Listeners ---
  api.addEventListener("connection_change", (e) => {
    if (e.detail.connected) {
      checkBackendHealth();
    } else {
      onBackendOffline();
    }
  });

  api.addEventListener("init_state", (e) => {
    tasksMap.clear();
    e.detail.forEach(task => tasksMap.set(task.id, task));
    renderAll();
  });

  api.addEventListener("task_queued", (e) => {
    tasksMap.set(e.detail.id, e.detail);
    renderAll();
  });

  api.addEventListener("task_started", (e) => {
    tasksMap.set(e.detail.id, e.detail);
    renderAll();
  });

  api.addEventListener("task_progress", (e) => {
    const task = e.detail;
    // Only update and re-render if it exists
    if (tasksMap.has(task.id)) {
      tasksMap.set(task.id, { ...tasksMap.get(task.id), ...task });
      updateTaskCardDOM(task.id);
    }
  });

  api.addEventListener("task_complete", (e) => {
    tasksMap.set(e.detail.id, e.detail);
    renderAll();
    showDesktopNotification("Download Finished", `${e.detail.title || "Video"} downloaded successfully!`);
  });

  api.addEventListener("task_failed", (e) => {
    tasksMap.set(e.detail.id, e.detail);
    renderAll();
    showDesktopNotification("Download Failed", `${e.detail.title || "Video"} failed to download.`);
  });

  api.addEventListener("task_cancelled", (e) => {
    tasksMap.set(e.detail.id, e.detail);
    renderAll();
  });

  // --- Refresh Lists ---
  async function refreshLists() {
    try {
      const allTasks = await api.listDownloads();
      tasksMap.clear();
      allTasks.forEach(task => tasksMap.set(task.id, task));
      renderAll();
    } catch (e) {
      console.error("Failed to refresh lists:", e);
    }
  }

  // --- Helper: Format Utilities ---
  function formatBytes(bytes, decimals = 2) {
    if (!bytes) return "0 Bytes";
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ["Bytes", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
  }

  function formatSpeed(bytesPerSec) {
    if (!bytesPerSec) return "0 B/s";
    return formatBytes(bytesPerSec, 1) + "/s";
  }

  function formatDuration(seconds) {
    if (!seconds) return "0:00";
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    if (hrs > 0) {
      return `${hrs}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
    }
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  }

  function formatETA(seconds) {
    if (seconds === null || seconds === undefined) return "--:--";
    if (seconds === Infinity) return "calc...";
    return formatDuration(seconds);
  }

  // --- DOM Rendering ---
  function renderAll() {
    // Debounce: collapse rapid successive calls into one
    if (renderAllTimer) cancelAnimationFrame(renderAllTimer);
    renderAllTimer = requestAnimationFrame(_renderAllNow);
  }

  function _renderAllNow() {
    const tasks = Array.from(tasksMap.values());
    
    const activeTasks = tasks.filter(t => t.status === "downloading" || t.status === "postprocessing");
    const queuedTasks = tasks.filter(t => t.status === "queued" || t.status === "scheduled");
    const historyTasks = tasks.filter(t => ["complete", "failed", "cancelled"].includes(t.status));

    // Update badges
    badgeDownloads.textContent = activeTasks.length;
    badgeQueue.textContent = queuedTasks.length;
    badgeHistory.textContent = historyTasks.length;

    // Render lists
    renderList(activeList, activeTasks, downloadsEmpty);
    renderList(queueList, queuedTasks, queueEmpty);
    renderList(historyList, historyTasks, historyEmpty);
    
    // Show/hide history clear button
    historyActions.style.display = historyTasks.length > 0 ? "flex" : "none";
  }

  function renderList(listElement, tasks, emptyElement) {
    listElement.innerHTML = "";
    if (tasks.length === 0) {
      emptyElement.style.display = "flex";
      return;
    }
    emptyElement.style.display = "none";

    tasks.forEach(task => {
      listElement.appendChild(createTaskCard(task));
    });
  }

  function createTaskCard(task) {
    const card = document.createElement("div");
    card.className = "download-card";
    card.id = `task-card-${task.id}`;
    
    const isFinished = ["complete", "failed", "cancelled"].includes(task.status);
    const showProgress = !isFinished && task.status !== "scheduled";

    card.innerHTML = `
      <img class="card-thumbnail" src="${escapeHtml(task.thumbnail_url) || '../icons/icon128.png'}" alt="Thumbnail">
      <div class="card-content">
        <div class="card-title" title="${escapeHtml(task.title || task.url)}">${escapeHtml(task.title || task.url)}</div>
        
        ${showProgress ? `
          <div class="progress-container">
            <div class="progress-bar" id="bar-${task.id}" style="width: ${task.progress || 0}%"></div>
          </div>
        ` : ''}

        <div class="card-meta">
          <span class="card-status-badge ${task.status}" id="status-${task.id}">
            ${task.status === "scheduled" ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width: 10px; height: 10px; vertical-align: middle; margin-right: 4px; display: inline-block;"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>' : ''}${task.status}
          </span>
          <span class="meta-details" id="meta-${task.id}">
            ${getMetaText(task)}
          </span>
          <div class="card-actions">
            ${getActionButtons(task)}
          </div>
        </div>
      </div>
    `;

    // Attach event listeners to buttons
    const cancelBtn = card.querySelector(".cancel-btn");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", () => api.cancelDownload(task.id));
    }
    
     const retryBtn = card.querySelector(".retry-btn");
     if (retryBtn) {
       retryBtn.addEventListener("click", async () => {
         let cookies = null;
         try {
           cookies = await getCookiesForUrl(task.url);
         } catch (err) {
           console.warn("Failed to get cookies on retry:", err);
         }
         api.retryDownload(task.id, cookies);
       });
     }

    const deleteBtn = card.querySelector(".delete-btn");
    if (deleteBtn) {
      deleteBtn.addEventListener("click", async () => {
        try {
          await api.deleteHistoryItem(task.id);
          tasksMap.delete(task.id);
          renderAll();
        } catch (e) {
          console.error("Failed to delete history item:", e);
        }
      });
    }

    return card;
  }

  function getMetaText(task) {
    if (task.status === "downloading") {
      const sizeStr = task.file_size ? `/ ${formatBytes(task.file_size)}` : "";
      return `${task.progress}% ${sizeStr} • ${formatSpeed(task.speed)} • ETA ${formatETA(task.eta)}`;
    } else if (task.status === "postprocessing") {
      return "Merging/Processing files...";
    } else if (task.status === "complete") {
      return `${formatBytes(task.file_size)} • Complete`;
    } else if (task.status === "failed") {
      return `Failed: ${task.error_message || "Unknown error"}`;
    } else if (task.status === "cancelled") {
      return "Cancelled";
    } else if (task.status === "queued") {
      return "Waiting in queue...";
    } else if (task.status === "scheduled") {
      const timeStr = task.scheduled_at ? new Date(task.scheduled_at).toLocaleString() : "Unknown time";
      return `Start at ${timeStr}`;
    }
    return "";
  }

  function getActionButtons(task) {
    if (task.status === "downloading" || task.status === "queued" || task.status === "postprocessing" || task.status === "scheduled") {
      return `
        <button class="icon-button cancel-btn" title="Cancel Download">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      `;
    } else if (task.status === "failed" || task.status === "cancelled") {
      return `
        <button class="icon-button retry-btn" title="Retry Download">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path></svg>
        </button>
        <button class="icon-button delete-btn" title="Remove from History">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      `;
    } else if (task.status === "complete") {
      return `
        <button class="icon-button delete-btn" title="Remove from History">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      `;
    }
    return "";
  }

  function updateTaskCardDOM(taskId) {
    const task = tasksMap.get(taskId);
    if (!task) return;

    const card = document.getElementById(`task-card-${taskId}`);
    if (!card) return;

    // Update progress bar
    const bar = card.querySelector(`#bar-${taskId}`);
    if (bar) {
      bar.style.width = `${task.progress}%`;
    }

    // Update status badge
    const statusBadge = card.querySelector(`#status-${taskId}`);
    if (statusBadge) {
      statusBadge.textContent = task.status;
      statusBadge.className = `card-status-badge ${task.status}`;
    }

    // Update meta details
    const metaDetails = card.querySelector(`#meta-${taskId}`);
    if (metaDetails) {
      metaDetails.innerHTML = getMetaText(task);
    }
  }

  // --- Tab Navigation ---
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      tabContents.forEach(c => c.classList.remove("active"));
      
      btn.classList.add("active");
      const targetTab = btn.getAttribute("data-tab");
      document.getElementById(`panel-${targetTab}`).classList.add("active");
    });
  });

  // --- History Actions ---
  clearHistoryBtn.addEventListener("click", async () => {
    if (confirm("Are you sure you want to clear all download history?")) {
      try {
        await api.clearHistory();
        refreshLists();
      } catch (e) {
        console.error("Failed to clear history:", e);
      }
    }
  });

  // --- Quick Download Trigger & Probe ---
  downloadBtn.addEventListener("click", () => {
    const url = videoUrlInput.value.trim();
    if (url) {
      if (!isValidUrl(url)) {
        alert("Please enter a valid URL (e.g. https://www.youtube.com/watch?v=...).");
        return;
      }
      triggerProbe(url);
    }
  });

  videoUrlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const url = videoUrlInput.value.trim();
      if (url) {
        if (!isValidUrl(url)) {
          alert("Please enter a valid URL (e.g. https://www.youtube.com/watch?v=...).");
          return;
        }
        triggerProbe(url);
      }
    }
  });

  pasteBtn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      videoUrlInput.value = text;
      videoUrlInput.focus();
    } catch (err) {
      console.warn("Could not read from clipboard:", err);
    }
  });

  async function triggerProbe(url) {
    currentProbedUrl = url;
    showModalLoading();
    formatModal.classList.add("active");

    try {
      const info = await api.probeUrl(url);
      currentProbedData = info;
      showModalDetails(info);
    } catch (e) {
      console.error("Probing failed:", e);
      modalTitle.textContent = "Error loading video details";
      modalAuthor.textContent = e.message || "Invalid URL or unsupported site";
    }
  }

  // --- Extended Option Listeners ---
  optionAudioOnly.addEventListener("change", () => {
    if (optionAudioOnly.checked) {
      videoQualityGroup.style.display = "none";
      audioFormatGroup.style.display = "block";
    } else {
      videoQualityGroup.style.display = "block";
      audioFormatGroup.style.display = "none";
    }
  });

  optionSubtitles.addEventListener("change", () => {
    if (optionSubtitles.checked) {
      subtitleOptionsGroup.style.display = "block";
    } else {
      subtitleOptionsGroup.style.display = "none";
    }
  });

  playlistSelectAll.addEventListener("click", () => {
    playlistEntriesList.querySelectorAll(".playlist-item-checkbox").forEach(cb => cb.checked = true);
    updatePlaylistSelectedCount();
  });

  playlistDeselectAll.addEventListener("click", () => {
    playlistEntriesList.querySelectorAll(".playlist-item-checkbox").forEach(cb => cb.checked = false);
    updatePlaylistSelectedCount();
  });

  function updatePlaylistSelectedCount() {
    const checked = playlistEntriesList.querySelectorAll(".playlist-item-checkbox:checked");
    playlistSelectedCount.textContent = `${checked.length} of ${playlistEntriesList.querySelectorAll(".playlist-item-checkbox").length} items selected`;
    modalStartBtn.disabled = checked.length === 0;
  }

  // --- Modal View Management ---
  function showModalLoading() {
    modalThumbnail.src = "../icons/icon128.png";
    modalTitle.textContent = "Fetching video information...";
    modalAuthor.textContent = "Please wait while yt-dlp analyzes the link.";
    modalDuration.textContent = "";
    formatSelect.innerHTML = '<option value="bestvideo+bestaudio/best">Best Quality (Default)</option>';
    formatSelect.disabled = true;
    modalStartBtn.disabled = true;
    
    // Reset options
    optionAudioOnly.checked = false;
    optionSubtitles.checked = false;
    optionThumbnail.checked = false;
    optionEmbedSubtitles.checked = false;
    videoQualityGroup.style.display = "block";
    audioFormatGroup.style.display = "none";
    subtitleOptionsGroup.style.display = "none";
    subtitleLangSelect.innerHTML = "";
    
    modalSingleVideoContent.style.display = "block";
    modalPlaylistContent.style.display = "none";
  }

  function showModalDetails(info) {
    // Disable/Enable ffmpeg dependent fields
    if (!ffmpegAvailable) {
      optionAudioOnly.checked = false;
      optionAudioOnly.disabled = true;
      optionAudioOnly.parentElement.style.opacity = "0.5";
      optionAudioOnly.parentElement.style.pointerEvents = "none";
      
      optionSubtitles.checked = false;
      optionSubtitles.disabled = true;
      optionSubtitles.parentElement.style.opacity = "0.5";
      optionSubtitles.parentElement.style.pointerEvents = "none";

      optionEmbedSubtitles.checked = false;
      optionEmbedSubtitles.disabled = true;
      optionEmbedSubtitles.parentElement.style.opacity = "0.5";
      optionEmbedSubtitles.parentElement.style.pointerEvents = "none";

      optionThumbnail.checked = false;
      optionThumbnail.disabled = true;
      optionThumbnail.parentElement.style.opacity = "0.5";
      optionThumbnail.parentElement.style.pointerEvents = "none";
      
      // Ensure audio format group is hidden since audio only is disabled
      videoQualityGroup.style.display = "block";
      audioFormatGroup.style.display = "none";
      subtitleOptionsGroup.style.display = "none";
    } else {
      optionAudioOnly.disabled = false;
      optionAudioOnly.parentElement.style.opacity = "1";
      optionAudioOnly.parentElement.style.pointerEvents = "auto";
      
      optionSubtitles.disabled = false;
      optionSubtitles.parentElement.style.opacity = "1";
      optionSubtitles.parentElement.style.pointerEvents = "auto";

      optionEmbedSubtitles.disabled = false;
      optionEmbedSubtitles.parentElement.style.opacity = "1";
      optionEmbedSubtitles.parentElement.style.pointerEvents = "auto";

      optionThumbnail.disabled = false;
      optionThumbnail.parentElement.style.opacity = "1";
      optionThumbnail.parentElement.style.pointerEvents = "auto";
    }

    if (info.is_playlist) {
      modalHeaderTitle.textContent = "Download Playlist";
      modalSingleVideoContent.style.display = "none";
      modalPlaylistContent.style.display = "block";
      
      playlistTitle.textContent = info.title;
      playlistMeta.textContent = `${info.entries ? info.entries.length : 0} videos • ${info.uploader || 'Unknown Channel'}`;
      
      playlistEntriesList.innerHTML = "";
      if (info.entries && info.entries.length > 0) {
        info.entries.forEach((entry, idx) => {
          const row = document.createElement("div");
          row.className = "playlist-entry-row";
          row.style.display = "flex";
          row.style.alignItems = "center";
          row.style.gap = "10px";
          row.style.padding = "6px 8px";
          row.style.borderBottom = "1px solid rgba(255,255,255,0.03)";
          
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.checked = true;
          checkbox.className = "playlist-item-checkbox";
          checkbox.dataset.url = entry.url;
          checkbox.dataset.title = entry.title;
          checkbox.dataset.thumbnail = entry.thumbnail || "";
          checkbox.dataset.duration = entry.duration || "";
          checkbox.addEventListener("change", updatePlaylistSelectedCount);
          
          const thumbnail = document.createElement("img");
          thumbnail.src = entry.thumbnail || "../icons/icon128.png";
          thumbnail.style.width = "40px";
          thumbnail.style.height = "24px";
          thumbnail.style.objectFit = "cover";
          thumbnail.style.borderRadius = "4px";
          
          const titleText = document.createElement("span");
          titleText.textContent = `${idx + 1}. ${entry.title}`;
          titleText.style.flex = "1";
          titleText.style.fontSize = "11px";
          titleText.style.whiteSpace = "nowrap";
          titleText.style.overflow = "hidden";
          titleText.style.textOverflow = "ellipsis";
          
          const durationText = document.createElement("span");
          durationText.textContent = entry.duration ? formatDuration(entry.duration) : "";
          durationText.style.fontSize = "10px";
          durationText.style.color = "var(--text-secondary)";
          
          row.appendChild(checkbox);
          row.appendChild(thumbnail);
          row.appendChild(titleText);
          row.appendChild(durationText);
          playlistEntriesList.appendChild(row);
        });
      }
      
      updatePlaylistSelectedCount();
      modalStartBtn.disabled = false;
    } else {
      modalHeaderTitle.textContent = "Download Options";
      modalSingleVideoContent.style.display = "block";
      modalPlaylistContent.style.display = "none";
      
      modalThumbnail.src = info.thumbnail || "../icons/icon128.png";
      modalTitle.textContent = info.title || "Unknown Title";
      customTitleInput.value = info.title || "";
      modalAuthor.textContent = info.uploader || "Unknown Uploader";
      modalDuration.textContent = info.duration ? formatDuration(info.duration) : "";
      
      // Populate formats
      formatSelect.innerHTML = "";
      
      // Default option
      const bestOpt = document.createElement("option");
      bestOpt.value = "bestvideo+bestaudio/best";
      bestOpt.textContent = "Best Quality (Default)";
      formatSelect.appendChild(bestOpt);

      if (info.formats) {
        const uniqueResolutions = new Set();
        const cleanFormats = [];
        
        info.formats.forEach(f => {
          if (f.resolution && !uniqueResolutions.has(f.resolution)) {
            uniqueResolutions.add(f.resolution);
            cleanFormats.push(f);
          }
        });

        // Add cleaner parsed resolutions
        cleanFormats.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.format_id;
          const sizeStr = f.filesize ? ` (~${formatBytes(f.filesize)})` : "";
          opt.textContent = `${f.resolution} (${f.ext.toUpperCase()})${sizeStr}`;
          formatSelect.appendChild(opt);
        });
      }
      
      // Populate subtitles
      subtitleLangSelect.innerHTML = "";
      if (info.subtitles && Object.keys(info.subtitles).length > 0) {
        Object.keys(info.subtitles).forEach(lang => {
          const opt = document.createElement("option");
          opt.value = lang;
          const subType = info.subtitles[lang][0]?.name || "Official";
          opt.textContent = `${lang.toUpperCase()} (${subType})`;
          subtitleLangSelect.appendChild(opt);
        });
      } else {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No subtitles available";
        opt.disabled = true;
        subtitleLangSelect.appendChild(opt);
      }
      
      formatSelect.disabled = false;
      modalStartBtn.disabled = false;
    }
  }

  // --- Modal Button Clicks ---
  function closeModal() {
    formatModal.classList.remove("active");
    currentProbedUrl = "";
    currentProbedData = null;
    if (customTitleInput) customTitleInput.value = "";
    if (modalDownloadDirInput) modalDownloadDirInput.value = "";
    if (modalOutputTemplateInput) modalOutputTemplateInput.value = "";
    if (playlistDownloadDirInput) playlistDownloadDirInput.value = "";
    
    // Reset Scheduler
    if (chkScheduleLater) {
      chkScheduleLater.checked = false;
      scheduleInputs.style.display = "none";
      inputScheduleTime.value = "";
    }
  }

  // Helper to format local date for datetime-local input
  function formatLocalDateForInput(date) {
    const tzoffset = date.getTimezoneOffset() * 60000;
    return (new Date(date.getTime() - tzoffset)).toISOString().slice(0, 16);
  }

  if (chkScheduleLater) {
    chkScheduleLater.addEventListener("change", () => {
      if (chkScheduleLater.checked) {
        scheduleInputs.style.display = "flex";
        const defaultTime = new Date(Date.now() + 60 * 60 * 1000); // 1 hour from now
        inputScheduleTime.value = formatLocalDateForInput(defaultTime);
      } else {
        scheduleInputs.style.display = "none";
      }
    });

    btnPresetTonight.addEventListener("click", () => {
      const tonight = new Date();
      tonight.setHours(23, 0, 0, 0);
      if (tonight.getTime() < Date.now()) {
        tonight.setDate(tonight.getDate() + 1);
      }
      inputScheduleTime.value = formatLocalDateForInput(tonight);
    });

    btnPresetTomorrow.addEventListener("click", () => {
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      tomorrow.setHours(6, 0, 0, 0);
      inputScheduleTime.value = formatLocalDateForInput(tomorrow);
    });
  }

  modalCloseBtn.addEventListener("click", closeModal);
  modalCancelBtn.addEventListener("click", closeModal);
  
  modalStartBtn.addEventListener("click", async () => {
    if (!currentProbedUrl) return;

    // Check duplicate download (look for completed downloads in history)
    let isDuplicate = false;
    for (const [id, task] of tasksMap.entries()) {
      if (task.url === currentProbedUrl && task.status === "complete") {
        isDuplicate = true;
        break;
      }
    }
    if (isDuplicate) {
      const confirmDownload = confirm("This video has already been downloaded successfully. Do you want to download it again?");
      if (!confirmDownload) return;
    }

    if (currentProbedData && currentProbedData.is_playlist) {
      const selectedCBs = playlistEntriesList.querySelectorAll(".playlist-item-checkbox:checked");
      if (selectedCBs.length === 0) {
        alert("Please select at least one video to download.");
        return;
      }
      
      const formatId = playlistFormatSelect.value;
      const audioOnly = (formatId === "bestaudio/best");
      
      modalStartBtn.disabled = true;
      modalStartBtn.textContent = "Enqueuing...";
      
      try {
        let scheduledAt = null;
        if (chkScheduleLater && chkScheduleLater.checked && inputScheduleTime.value) {
          try {
            scheduledAt = new Date(inputScheduleTime.value).toISOString();
          } catch (err) {
            console.error("Invalid scheduled datetime:", err);
          }
        }

        const cookies = await getCookiesForUrl(currentProbedUrl);
        const options = { audio_only: audioOnly };
        if (cookies) {
          options.cookies = cookies;
        }
        if (scheduledAt) {
          options.scheduled_at = scheduledAt;
        }

        const customDownloadDir = playlistDownloadDirInput ? playlistDownloadDirInput.value.trim() : "";

        const promises = Array.from(selectedCBs).map(cb => {
          const itemOptions = { ...options };
          if (cb.dataset.duration) {
            itemOptions.duration = parseInt(cb.dataset.duration);
          }
          if (customDownloadDir) {
            itemOptions.download_dir = customDownloadDir;
          }
          return api.startDownload(
            cb.dataset.url,
            formatId,
            cb.dataset.title,
            cb.dataset.thumbnail || null,
            itemOptions
          );
        });
        
        await Promise.all(promises);
        videoUrlInput.value = "";
        closeModal();
        document.querySelector('[data-tab="downloads"]').click();
      } catch (e) {
        alert(`Failed to enqueue playlist items: ${e.message}`);
      } finally {
        modalStartBtn.disabled = false;
        modalStartBtn.textContent = "Start Download";
      }
    } else {
      let formatId = formatSelect.value;
      const audioOnly = optionAudioOnly.checked;
      const audioFormat = audioFormatSelect.value;
      const downloadSubtitles = optionSubtitles.checked;

      // If it's a video-only format, automatically append +bestaudio/best so yt-dlp merges them
      if (!audioOnly && currentProbedData && currentProbedData.formats) {
        const selectedFormat = currentProbedData.formats.find(f => f.format_id === formatId);
        if (selectedFormat) {
          const hasVideo = selectedFormat.vcodec && selectedFormat.vcodec !== 'none';
          const hasAudio = selectedFormat.acodec && selectedFormat.acodec !== 'none';
          if (hasVideo && !hasAudio) {
            formatId = `${formatId}+bestaudio/best`;
          }
        }
      }
      
      const subtitleLangs = Array.from(subtitleLangSelect.selectedOptions).map(opt => opt.value);
      const subtitleFormat = subtitleFormatSelect.value;
      const embedSubtitles = optionEmbedSubtitles.checked;
      const embedThumbnail = optionThumbnail.checked;

      const options = {
        audio_only: audioOnly,
        audio_format: audioFormat,
        download_subtitles: downloadSubtitles,
        subtitle_langs: (subtitleLangs.length > 0 && subtitleLangs[0] !== "") ? subtitleLangs : null,
        subtitle_format: subtitleFormat,
        embed_subtitles: embedSubtitles,
        embed_thumbnail: embedThumbnail
      };

      const customDownloadDir = modalDownloadDirInput ? modalDownloadDirInput.value.trim() : "";
      const customOutputTemplate = modalOutputTemplateInput ? modalOutputTemplateInput.value.trim() : "";

      if (customDownloadDir) {
        options.download_dir = customDownloadDir;
      }
      if (customOutputTemplate) {
        options.output_template = customOutputTemplate;
      }

      try {
        let scheduledAt = null;
        if (chkScheduleLater && chkScheduleLater.checked && inputScheduleTime.value) {
          try {
            scheduledAt = new Date(inputScheduleTime.value).toISOString();
          } catch (err) {
            console.error("Invalid scheduled datetime:", err);
          }
        }

        const cookies = await getCookiesForUrl(currentProbedUrl);
        if (cookies) {
          options.cookies = cookies;
        }
        if (scheduledAt) {
          options.scheduled_at = scheduledAt;
        }
        if (currentProbedData && currentProbedData.duration) {
          options.duration = parseInt(currentProbedData.duration);
        }

        const customTitle = customTitleInput.value.trim();
        const finalTitle = customTitle || (currentProbedData ? currentProbedData.title : null);

        await api.startDownload(
          currentProbedUrl,
          formatId,
          finalTitle,
          currentProbedData ? currentProbedData.thumbnail : null,
          options
        );
        videoUrlInput.value = "";
        closeModal();
        document.querySelector('[data-tab="downloads"]').click();
      } catch (e) {
        alert(`Failed to start download: ${e.message}`);
      }
    }
  });

  retryBackendBtn.addEventListener("click", () => {
    checkBackendHealth();
  });

  settingsBtn.addEventListener("click", () => {
    if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.openOptionsPage) {
      chrome.runtime.openOptionsPage();
    } else {
      const url = typeof chrome !== 'undefined' && chrome.runtime ? chrome.runtime.getURL("options/options.html") : "../options/options.html";
      window.open(url);
    }
  });

  // --- Notifications Helper ---
  function showDesktopNotification(title, message) {
    if (typeof chrome !== 'undefined' && chrome.notifications) {
      chrome.notifications.create(null, {
        type: "basic",
        iconUrl: "../icons/icon128.png",
        title,
        message,
        priority: 1
      });
    }
  }

  // --- Detected Videos Handling ---
  function loadDetectedVideos() {
    if (!activeTabId) return;
    const storageKey = `detected_videos_${activeTabId}`;
    chrome.storage.local.get([storageKey], (result) => {
      renderDetectedVideos(result[storageKey] || []);
    });
  }

  function renderDetectedVideos(videos) {
    if (!detectedList) return;

    badgeDetected.textContent = videos.length;
    detectedList.innerHTML = "";

    // Sync selection Set to only contain URLs that are actually in the current list
    const currentUrls = new Set(videos.map(v => v.url));
    for (const url of selectedDetectedUrls) {
      if (!currentUrls.has(url)) {
        selectedDetectedUrls.delete(url);
      }
    }

    if (videos.length === 0) {
      detectedEmpty.style.display = "flex";
      detectedList.style.display = "none";
      if (detectedActions) detectedActions.style.display = "none";
    } else {
      detectedEmpty.style.display = "none";
      detectedList.style.display = "block";
      if (detectedActions) detectedActions.style.display = "flex";

      videos.forEach((video, index) => {
        const card = document.createElement("div");
        card.className = "download-card";
        card.style.paddingLeft = "8px";
        
        let hostname = "";
        try {
          hostname = new URL(video.url).hostname;
        } catch(e) {
          hostname = video.url;
        }

        const isChecked = selectedDetectedUrls.has(video.url);

        card.innerHTML = `
          <label class="checkbox-container" style="margin: 0; padding-left: 16px; align-self: center; width: 16px; height: 16px;">
            <input type="checkbox" class="detected-item-checkbox" data-url="${escapeHtml(video.url)}" ${isChecked ? 'checked' : ''}>
            <span class="checkbox-checkmark" style="left: 0;"></span>
          </label>
          <img class="card-thumbnail" src="../icons/icon128.png" alt="Thumbnail">
          <div class="card-content">
            <div class="card-title" title="${escapeHtml(video.title)}">${escapeHtml(video.title)}</div>
            <div class="card-meta">
              <span class="card-status-badge complete" style="background: rgba(124, 107, 240, 0.15); color: #9b8fff; border-color: rgba(124, 107, 240, 0.3);">
                Detected
              </span>
              <span class="meta-details" style="text-overflow: ellipsis; overflow: hidden; white-space: nowrap; max-width: 160px;" title="${escapeHtml(video.url)}">
                ${escapeHtml(hostname)}
              </span>
              <div class="card-actions">
                <button class="icon-button download-detected-btn" title="Download Video">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                </button>
              </div>
            </div>
          </div>
        `;

        const checkbox = card.querySelector(".detected-item-checkbox");
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) {
            selectedDetectedUrls.add(video.url);
          } else {
            selectedDetectedUrls.delete(video.url);
          }
          updateDetectedSelectionUI(videos);
        });

        const downloadBtn = card.querySelector(".download-detected-btn");
        downloadBtn.addEventListener("click", () => {
          triggerProbe(video.url);
        });

        detectedList.appendChild(card);
      });
    }

    updateDetectedSelectionUI(videos);
  }

  function updateDetectedSelectionUI(videos) {
    if (!detectedSelectedCountText) return;
    detectedSelectedCountText.textContent = `${selectedDetectedUrls.size} selected`;
    
    const hasSelection = selectedDetectedUrls.size > 0;
    detectedDownloadBtn.disabled = !hasSelection;
    detectedRemoveBtn.disabled = !hasSelection;

    if (videos.length > 0 && videos.every(v => selectedDetectedUrls.has(v.url))) {
      detectedSelectAll.checked = true;
    } else {
      detectedSelectAll.checked = false;
    }
  }

  function closeBatchModal() {
    if (batchModal) batchModal.classList.remove("active");
    if (batchDownloadDirInput) batchDownloadDirInput.value = "";
  }

  // --- Batch Event Listeners ---
  if (detectedSelectAll) {
    detectedSelectAll.addEventListener("change", () => {
      if (!activeTabId) return;
      const storageKey = `detected_videos_${activeTabId}`;
      chrome.storage.local.get([storageKey], (result) => {
        const videos = result[storageKey] || [];
        if (detectedSelectAll.checked) {
          videos.forEach(v => selectedDetectedUrls.add(v.url));
        } else {
          videos.forEach(v => selectedDetectedUrls.delete(v.url));
        }
        
        detectedList.querySelectorAll(".detected-item-checkbox").forEach(cb => {
          cb.checked = detectedSelectAll.checked;
        });
        
        updateDetectedSelectionUI(videos);
      });
    });
  }

  if (detectedRemoveBtn) {
    detectedRemoveBtn.addEventListener("click", () => {
      if (!activeTabId || selectedDetectedUrls.size === 0) return;
      const storageKey = `detected_videos_${activeTabId}`;
      chrome.storage.local.get([storageKey], (result) => {
        const videos = result[storageKey] || [];
        const filtered = videos.filter(v => !selectedDetectedUrls.has(v.url));
        
        selectedDetectedUrls.clear();
        
        chrome.storage.local.set({ [storageKey]: filtered });
        
        const total = filtered.length;
        const text = total > 0 ? String(total) : "";
        chrome.action.setBadgeText({ text, tabId: activeTabId });
        chrome.action.setBadgeBackgroundColor({ color: "#7C6BF0", tabId: activeTabId });
      });
    });
  }

  if (detectedDownloadBtn) {
    detectedDownloadBtn.addEventListener("click", () => {
      if (selectedDetectedUrls.size === 0) return;
      if (batchModal) batchModal.classList.add("active");
    });
  }

  if (batchModalCloseBtn) batchModalCloseBtn.addEventListener("click", closeBatchModal);
  if (batchModalCancelBtn) batchModalCancelBtn.addEventListener("click", closeBatchModal);

  if (batchModalStartBtn) {
    batchModalStartBtn.addEventListener("click", async () => {
      const quality = batchQualitySelect.value;
      const audioOnly = batchOptionAudioOnly.checked;
      const customDownloadDir = batchDownloadDirInput ? batchDownloadDirInput.value.trim() : "";
      
      closeBatchModal();
      
      if (!activeTabId) return;
      const storageKey = `detected_videos_${activeTabId}`;
      chrome.storage.local.get([storageKey], async (result) => {
        const videos = result[storageKey] || [];
        const selectedVideos = videos.filter(v => selectedDetectedUrls.has(v.url));
        
        selectedDetectedUrls.clear();
        renderDetectedVideos(videos);
        
        for (const video of selectedVideos) {
          try {
            let formatId = quality;
            let options = {};
            
            if (audioOnly) {
              formatId = "bestaudio/best";
              options.audio_only = true;
            }
            if (customDownloadDir) {
              options.download_dir = customDownloadDir;
            }

            const cookies = await getCookiesForUrl(video.url);
            if (cookies) {
              options.cookies = cookies;
            }

            await api.startDownload(
              video.url,
              formatId,
              video.title,
              null,
              options
            );
          } catch (e) {
            console.error(`Failed to start batch download for ${video.url}:`, e);
          }
        }
        
        document.querySelector('[data-tab="downloads"]').click();
      });
    });
  }

  // --- Startup ---
  connect();

  // Retrieve active tab details for Detected tab tracking
  if (typeof chrome !== 'undefined' && chrome.tabs) {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs && tabs[0]) {
        activeTabId = tabs[0].id;
        loadDetectedVideos();
      }
    });

    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName === "local" && activeTabId && changes[`detected_videos_${activeTabId}`]) {
        renderDetectedVideos(changes[`detected_videos_${activeTabId}`].newValue || []);
      }
    });
  }

  // Check for any pending video URL from the content script floating button click
  if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
    chrome.storage.local.get(["pendingVideoUrl"], (result) => {
      if (result.pendingVideoUrl) {
        const url = result.pendingVideoUrl;
        // Clear it immediately so it doesn't trigger again on subsequent popup opens
        chrome.storage.local.remove("pendingVideoUrl");
        
        videoUrlInput.value = url;
        triggerProbe(url);
      }
    });
  }
});
