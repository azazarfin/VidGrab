document.addEventListener("DOMContentLoaded", () => {
  const api = window.vidGrabAPI;
  
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
  let appDefaults = null;
  let counts = { total: 0, unfinished: 0 };
  let probeNumber = 0;
  let actionBusy = false;
  const clientStatus = document.getElementById("client-status");
  const checkRequestBtn = document.getElementById("check-request-btn");
  const reviewRequestBtn = document.getElementById("review-request-btn");
  const loadMoreBtn = document.getElementById("load-more-btn");
  const jobsSummary = document.getElementById("jobs-summary");
  const freshProbeBtn = document.getElementById("fresh-probe-btn");

  function showClientStatus(message, error = false) {
    clientStatus.textContent = message;
    clientStatus.hidden = !message;
    clientStatus.setAttribute("role", error ? "alert" : "status");
    clientStatus.classList.toggle("error", error);
    checkRequestBtn.hidden = api.session.pending === null;
  }

  function updateDefaults() {
    for (const input of [modalDownloadDirInput, playlistDownloadDirInput, batchDownloadDirInput]) {
      input.readOnly = true;
      input.value = appDefaults?.download_dir || "";
    }
    modalOutputTemplateInput.readOnly = true;
    modalOutputTemplateInput.value = appDefaults?.filename_template || "";
    for (const select of [formatSelect, playlistFormatSelect, batchQualitySelect]) {
      const option = select.querySelector('option[value=""]');
      if (option) option.textContent = "App default (" + (appDefaults?.default_format || "loading") + ")";
    }
  }

  function isValidUrl(value) {
    try { window.vidGrabMediaUrl(value); return true; } catch { return false; }
  }

  let ffmpegAvailable = false;
  function connect() {
    api.connect();
    void api.getPending().then(pending => {
      reviewRequestBtn.hidden = pending.length === 0;
      if (pending.length) showClientStatus("A previous request may have succeeded. Review downloads before submitting the same request again.", true);
    }).catch(error => showClientStatus(error.message, true));
  }
  reviewRequestBtn.addEventListener("click", () => chrome.runtime.openOptionsPage());
  async function checkBackendHealth() {
    try { await api.sync(true); } catch (error) { onBackendOffline(error); }
  }
  function onBackendOffline(error) {
    connectionStatus.classList.remove("online");
    connectionStatus.querySelector(".status-text").textContent = error?.code === "APP_INCOMPATIBLE" ? "Update required" : "Unavailable";
    backendErrorOverlay.classList.add("active");
    backendErrorOverlay.hidden = false;
    backendErrorOverlay.querySelector("h3").textContent = error?.code === "APP_INCOMPATIBLE" ? "Update required" : "Companion unavailable";
    const description = backendErrorOverlay.querySelector("p");
    if (description) description.textContent = error?.message || "Open VidGrab and test the native browser connection.";
  }
  api.addEventListener("snapshot", event => {
    const state = event.detail;
    tasksMap = new Map(state.snapshot.jobs.map(job => [job.id, job]));
    appDefaults = state.snapshot.settings.values;
    counts = state.snapshot.counts;
    ffmpegAvailable = Boolean(state.capabilities.component_versions.ffmpeg);
    connectionStatus.classList.add("online");
    connectionStatus.querySelector(".status-text").textContent = "Connected";
    connectionStatus.title = "Native companion connected";
    backendErrorOverlay.classList.remove("active");
    backendErrorOverlay.hidden = true;
    const warning = document.getElementById("modal-ffmpeg-warning");
    if (warning) warning.style.display = ffmpegAvailable ? "none" : "block";
    loadMoreBtn.hidden = !state.snapshot.next_after;
    jobsSummary.textContent = state.snapshot.jobs.length + " shown · " + counts.total + " total · " + counts.unfinished + " unfinished";
    updateDefaults();
    renderAll();
  });
  api.addEventListener("connection_change", event => {
    if (!event.detail.connected) onBackendOffline(event.detail.error);
  });
  async function refreshLists() {
    try { await api.sync(); } catch (error) { showClientStatus(error.message, true); }
  }
  loadMoreBtn.addEventListener("click", async () => {
    loadMoreBtn.disabled = true;
    try { await api.loadMore(); } catch (error) { showClientStatus(error.message, true); }
    finally { loadMoreBtn.disabled = false; }
  });
  checkRequestBtn.addEventListener("click", async () => {
    checkRequestBtn.disabled = true;
    try {
      await api.retryPending();
      await api.sync(true);
      closeModal();
      showClientStatus("Request checked. Current downloads refreshed.");
    } catch (error) {
      if (error.details?.replay_available === false) reviewRequestBtn.hidden = false;
      showClientStatus(error.message, true);
    }
    finally { checkRequestBtn.disabled = false; }
  });
  window.addEventListener("pagehide", () => api.disconnect());

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
    const queuedTasks = tasks.filter(t => !["downloading", "postprocessing", "complete", "failed", "cancelled"].includes(t.status));
    const historyTasks = tasks.filter(t => ["complete", "failed", "cancelled"].includes(t.status));

    // Update badges
    badgeDownloads.textContent = activeTasks.length;
    badgeQueue.textContent = queuedTasks.length;
    badgeHistory.textContent = counts.total - counts.unfinished;

    // Render lists
    renderList(activeList, activeTasks, downloadsEmpty);
    renderList(queueList, queuedTasks, queueEmpty);
    renderList(historyList, historyTasks, historyEmpty);
    
    // Show/hide history clear button
    historyActions.style.display = "none";
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
    card.id = "task-card-" + task.id;
    const thumbnail = document.createElement("img");
    thumbnail.className = "card-thumbnail";
    thumbnail.src = "../icons/icon128.png";
    thumbnail.alt = "";
    card.appendChild(thumbnail);
    const content = document.createElement("div");
    content.className = "card-content";
    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = task.title || task.url || "Download";
    title.title = title.textContent;
    content.appendChild(title);
    if (!["complete", "failed", "cancelled"].includes(task.status)) {
      const progress = document.createElement("progress");
      progress.max = 100; progress.value = task.progress;
      progress.setAttribute("aria-label", title.textContent + " progress");
      content.appendChild(progress);
    }
    const meta = document.createElement("div");
    meta.className = "card-meta";
    const status = document.createElement("span");
    status.className = "card-status-badge";
    status.textContent = {
      waiting_for_auth: "Fresh cookies needed", retry_wait: "Retry scheduled",
      waiting_children: "Waiting for playlist items", cancelling: "Cancelling",
    }[task.status] || task.status;
    meta.appendChild(status);
    if (task.error_code || task.retry_at || task.file_path) {
      const detail = document.createElement("span");
      detail.className = "card-detail";
      detail.textContent = task.file_path || (task.retry_at ? "Retry after " + new Date(task.retry_at * 1000).toLocaleString() : task.error_code);
      meta.appendChild(detail);
    }
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const addAction = (label, execute) => {
      const button = document.createElement("button");
      button.className = "job-action";
      button.type = "button";
      button.textContent = label;
      button.disabled = actionBusy;
      button.addEventListener("click", async () => {
        if (actionBusy) return;
        actionBusy = true; renderAll();
        try { await execute(); await api.sync(); showClientStatus("Download action accepted."); }
        catch (error) {
          showClientStatus(error.code === "REVISION_CONFLICT" ? "This download changed. Review its current state before trying again."
            : api.session.pending ? "The request may have succeeded. Check it before submitting another action." : error.message, true);
          if (!api.session.pending) await refreshLists();
        } finally { actionBusy = false; renderAll(); }
      });
      actions.appendChild(button);
    };
    if (task.status === "waiting_for_auth") addAction("Share fresh cookies", () => api.authenticate(task));
    if (["failed", "cancelled"].includes(task.status)) addAction("Retry", () => api.retryDownload(task));
    else if (task.status !== "complete" && task.status !== "cancelling") addAction("Cancel", () => api.cancelDownload(task));
    meta.appendChild(actions); content.appendChild(meta); card.appendChild(content);
    return card;
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

  clearHistoryBtn.hidden = true;

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

  async function triggerProbe(url, fresh = false) {
    const sequence = ++probeNumber;
    currentProbedUrl = url;
    currentProbedData = null;
    showModalLoading();
    updateDefaults();
    freshProbeBtn.hidden = true;
    formatModal.classList.add("active");
    if (!formatModal.open) formatModal.showModal();
    try {
      const info = await api.probeUrl(url, { fresh });
      if (sequence !== probeNumber || currentProbedUrl !== url) return;
      currentProbedData = info;
      showModalDetails(info);
      updateDefaults();
    } catch (error) {
      if (sequence !== probeNumber) return;
      modalTitle.textContent = "Video information unavailable";
      modalAuthor.textContent = error.message;
      freshProbeBtn.hidden = !["AUTH_REQUIRED", "AUTH_CONTEXT_EXPIRED"].includes(error.code);
    }
  }
  freshProbeBtn.addEventListener("click", () => { if (currentProbedUrl) void triggerProbe(currentProbedUrl, true); });

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
    modalAuthor.textContent = "Reading media information…";
    modalDuration.textContent = "";
    formatSelect.replaceChildren(new Option("App default", ""));
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
          checkbox.dataset.position = String(idx + 1);
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
      bestOpt.value = "";
      bestOpt.textContent = "App default (" + (appDefaults?.default_format || "loading") + ")";
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
    ++probeNumber;
    formatModal.classList.remove("active");
    if (formatModal.open) formatModal.close();
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
  formatModal.addEventListener("cancel", event => { event.preventDefault(); closeModal(); });
  
  modalStartBtn.addEventListener("click", async () => {
    if (!currentProbedUrl || !currentProbedData || actionBusy) return;
    modalStartBtn.disabled = true;
    actionBusy = true;
    showClientStatus("");
    try {
      let formatId;
      let options;
      if (currentProbedData.is_playlist) {
        const selected = Array.from(playlistEntriesList.querySelectorAll(".playlist-item-checkbox:checked"));
        if (!selected.length) throw new Error("Select at least one playlist item.");
        formatId = playlistFormatSelect.value || null;
        options = { is_playlist: true, playlist_items: selected.map(checkbox => checkbox.dataset.position).join(","),
          audio_only: formatId === "bestaudio/best", audio_format: "mp3" };
      } else {
        formatId = formatSelect.value || null;
        const selected = currentProbedData.formats?.find(format => format.format_id === formatId);
        if (!optionAudioOnly.checked && selected?.vcodec && selected.vcodec !== "none" && (!selected.acodec || selected.acodec === "none")) {
          formatId += "+bestaudio/best";
        }
        const languages = Array.from(subtitleLangSelect.selectedOptions).map(option => option.value).filter(Boolean);
        options = { audio_only: optionAudioOnly.checked, audio_format: audioFormatSelect.value,
          download_subtitles: optionSubtitles.checked, subtitle_langs: languages.length ? languages : null,
          subtitle_format: subtitleFormatSelect.value, embed_subtitles: optionEmbedSubtitles.checked,
          embed_thumbnail: optionThumbnail.checked };
      }
      await api.startDownload(currentProbedUrl, formatId, customTitleInput.value || currentProbedData.title,
        currentProbedData.thumbnail, options);
      videoUrlInput.value = "";
      closeModal();
      await api.sync();
      document.querySelector('[data-tab="queue"]').click();
      showClientStatus("Download queued.");
    } catch (error) {
      if (api.session.pending) closeModal();
      else modalAuthor.textContent = error.message;
      showClientStatus(api.session.pending ? "The request may have succeeded. Check it before starting another download." : error.message, true);
      if (api.session.pending) checkRequestBtn.focus();
    } finally { actionBusy = false; modalStartBtn.disabled = false; renderAll(); }
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
    if (activeTabId == null) return;
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
            <input type="checkbox" class="detected-item-checkbox">
            <span class="checkbox-checkmark" style="left: 0;"></span>
          </label>
          <img class="card-thumbnail" src="../icons/icon128.png" alt="Thumbnail">
          <div class="card-content">
            <div class="card-title"></div>
            <div class="card-meta">
              <span class="card-status-badge complete" style="background: rgba(124, 107, 240, 0.15); color: #9b8fff; border-color: rgba(124, 107, 240, 0.3);">
                Detected
              </span>
              <span class="meta-details" style="text-overflow: ellipsis; overflow: hidden; white-space: nowrap; max-width: 160px;"></span>
              <div class="card-actions">
                <button class="icon-button download-detected-btn" title="Download Video">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                </button>
              </div>
            </div>
          </div>
        `;

        const checkbox = card.querySelector(".detected-item-checkbox");
        checkbox.dataset.url = video.url;
        checkbox.checked = isChecked;
        checkbox.setAttribute("aria-label", "Select " + (video.title || "detected video"));
        const title = card.querySelector(".card-title");
        title.textContent = video.title || "Detected video";
        title.title = title.textContent;
        const details = card.querySelector(".meta-details");
        details.textContent = hostname;
        details.title = video.url;
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
    if (batchModal?.open) batchModal.close();
    if (batchDownloadDirInput) batchDownloadDirInput.value = "";
  }

  // --- Batch Event Listeners ---
  if (detectedSelectAll) {
    detectedSelectAll.addEventListener("change", () => {
      if (activeTabId == null) return;
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
      if (activeTabId == null || selectedDetectedUrls.size === 0) return;
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
      if (batchModal && !batchModal.open) batchModal.showModal();
    });
  }

  if (batchModalCloseBtn) batchModalCloseBtn.addEventListener("click", closeBatchModal);
  if (batchModalCancelBtn) batchModalCancelBtn.addEventListener("click", closeBatchModal);
  if (batchModal) batchModal.addEventListener("cancel", event => { event.preventDefault(); closeBatchModal(); });

  if (batchModalStartBtn) batchModalStartBtn.addEventListener("click", async () => {
    if (actionBusy) return;
    actionBusy = true; batchModalStartBtn.disabled = true;
    let added = 0;
    try {
      const data = await new Promise(resolve => chrome.storage.local.get(["detected_videos_" + activeTabId], resolve));
      const videos = data["detected_videos_" + activeTabId] || [];
      for (const video of videos) {
        if (!selectedDetectedUrls.has(video.url)) continue;
        const audioOnly = batchOptionAudioOnly.checked;
        await api.startDownload(video.url, audioOnly ? "bestaudio/best" : batchQualitySelect.value || null,
          video.title || null, null, { audio_only: audioOnly, audio_format: "mp3" });
        added++;
      }
      closeBatchModal();
      await api.sync();
      document.querySelector('[data-tab="queue"]').click();
      showClientStatus(added + " downloads queued.");
    } catch (error) {
      closeBatchModal();
      showClientStatus(added + " queued. " + (api.session.pending ? "The last request may have succeeded. Check it before continuing." : error.message), true);
      if (api.session.pending) checkRequestBtn.focus();
    } finally { actionBusy = false; batchModalStartBtn.disabled = false; renderAll(); }
  });

  // --- Startup ---
  chkScheduleLater.disabled = true;
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
      if (areaName === "local" && activeTabId !== null && changes[`detected_videos_${activeTabId}`]) {
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
