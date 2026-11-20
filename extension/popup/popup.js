document.addEventListener("DOMContentLoaded", () => {
  const api = window.vidGrabAPI;

  // DOM Elements
  const connectionStatus = document.getElementById("connection-status");
  const backendErrorOverlay = document.getElementById("backend-error-overlay");
  const retryBackendBtn = document.getElementById("retry-backend-btn");
  const openAppBtn = document.getElementById("open-app-btn");
  const offlineOpenAppBtn = document.getElementById("offline-open-app-btn");
  const recentOpenAppBtn = document.getElementById("recent-open-app-btn");
  const settingsBtn = document.getElementById("settings-btn");

  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabContents = document.querySelectorAll(".tab-content");

  const queueList = document.getElementById("queue-list");
  const queueEmpty = document.getElementById("queue-empty");
  const recentSection = document.getElementById("recent-section");
  const recentList = document.getElementById("recent-list");

  const badgeQueue = document.getElementById("badge-queue");
  const badgeDetected = document.getElementById("badge-detected");

  const detectedList = document.getElementById("detected-list");
  const detectedEmpty = document.getElementById("detected-empty");
  const detectedRestartNotice = document.getElementById("detected-restart-notice");
  const detectedRestartSettingsBtn = document.getElementById("detected-restart-settings-btn");

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
  let detectedCandidates = [];
  const jobSources = new Map();

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

  // Extended Modal Elements
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
  let currentCandidate = null;
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
  const probeRecovery = document.getElementById("probe-recovery");
  const probeActionBtn = document.getElementById("probe-action-btn");
  let probeRecoveryAction = null;
  let probeDeadlineTimer = null;
  let expiredProbeRefreshed = false;
  let needsQualityChoice = false;

  const TERMINAL_STATUSES = new Set(["complete", "failed", "cancelled"]);
  const PAUSABLE_STATUSES = new Set(["queued", "scheduled", "retry_wait", "downloading", "probing"]);

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

  async function refreshPermissions() {
    if (!detectedRestartNotice) return;
    try {
      const permissions = await api.getPermissions();
      detectedRestartNotice.hidden = !permissions?.request_observation_restart_required;
    } catch {
      detectedRestartNotice.hidden = true;
    }
  }
  if (detectedRestartSettingsBtn) detectedRestartSettingsBtn.addEventListener("click", () => settingsBtn.click());

  async function triggerOpenApp(triggerButton) {
    if (triggerButton) triggerButton.disabled = true;
    try {
      await api.openApp();
      showClientStatus("Opening VidGrab desktop app.");
    } catch (error) {
      showClientStatus(error?.message || "Could not open VidGrab. Check that the desktop companion is installed and connected.", true);
    } finally {
      if (triggerButton) triggerButton.disabled = false;
    }
  }

  for (const button of [openAppBtn, offlineOpenAppBtn, recentOpenAppBtn]) {
    if (button) button.addEventListener("click", () => { void triggerOpenApp(button); });
  }

  let ffmpegAvailable = false;
  function connect() {
    api.connect();
    void refreshPermissions();
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
    connectionStatus.title = error?.message || "Companion unavailable";
    backendErrorOverlay.hidden = false;
    backendErrorOverlay.querySelector("h3").textContent = error?.code === "APP_INCOMPATIBLE" ? "Update required" : "Companion unavailable";
    const description = backendErrorOverlay.querySelector("p");
    if (description) {
      description.textContent = error?.message || "Open VidGrab and test the native browser connection. Page detection in Detected remains usable.";
    }
    renderAll();
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
    } finally { checkRequestBtn.disabled = false; }
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

  // --- DOM Rendering ---
  function renderAll() {
    if (renderAllTimer) cancelAnimationFrame(renderAllTimer);
    renderAllTimer = requestAnimationFrame(_renderAllNow);
  }

  function _renderAllNow() {
    const tasks = Array.from(tasksMap.values());
    const unfinishedTasks = tasks.filter(task => !TERMINAL_STATUSES.has(task.status));
    const recentTasks = tasks.filter(task => TERMINAL_STATUSES.has(task.status)).slice(0, 6);

    badgeQueue.textContent = counts.unfinished || unfinishedTasks.length;

    queueList.replaceChildren();
    if (unfinishedTasks.length === 0) {
      queueEmpty.style.display = backendErrorOverlay.hidden ? "flex" : "none";
    } else {
      queueEmpty.style.display = "none";
      for (const task of unfinishedTasks) {
        queueList.appendChild(createTaskCard(task));
      }
    }

    recentList.replaceChildren();
    recentSection.hidden = recentTasks.length === 0;
    for (const task of recentTasks) {
      recentList.appendChild(createTaskCard(task));
    }
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
    if (!TERMINAL_STATUSES.has(task.status)) {
      const progress = document.createElement("progress");
      progress.max = 100; progress.value = task.progress;
      progress.setAttribute("aria-label", title.textContent + " progress");
      content.appendChild(progress);
    }
    const meta = document.createElement("div");
    meta.className = "card-meta";
    const status = document.createElement("span");
    status.className = "card-status-badge " + task.status;
    status.textContent = {
      waiting_for_auth: "Fresh cookies needed",
      retry_wait: "Retry scheduled",
      waiting_children: "Waiting for playlist items",
      waiting_for_children: "Waiting for playlist items",
      cancelling: "Cancelling",
      pausing: "Pausing",
      paused: "Paused",
      scheduled: "Scheduled",
      probing: "Checking media",
    }[task.status] || task.status;
    meta.appendChild(status);
    const headerRequired = task.error_code === "BROWSER_CONTEXT_REQUIRED";
    if (task.status === "waiting_for_auth" && headerRequired) status.textContent = "Fresh browser context needed";
    const sources = detectedCandidates.filter(candidate => candidate.url === task.url && candidate.id
      && candidate.browser_context?.tab_id === activeTabId && isValidUrl(candidate.frame_url));
    const sourceActions = [];
    if (["waiting_for_auth", "failed", "cancelled"].includes(task.status) && sources.length) {
      const group = document.createElement("div");
      group.className = "job-context";
      const label = document.createElement("label");
      label.htmlFor = "job-source-" + task.id;
      label.textContent = "Source frame";
      const select = document.createElement("select");
      select.id = label.htmlFor;
      select.className = "form-select";
      select.disabled = actionBusy;
      select.appendChild(new Option("Choose source frame", ""));
      for (const candidate of sources) {
        const frame = new URL(candidate.frame_url);
        select.appendChild(new Option("Frame " + candidate.browser_context.frame_id + " · " + frame.host + frame.pathname, candidate.id));
      }
      select.value = sources.some(candidate => candidate.id === jobSources.get(task.id)) ? jobSources.get(task.id) : "";
      select.addEventListener("change", () => {
        if (select.value) jobSources.set(task.id, select.value);
        else jobSources.delete(task.id);
        for (const button of sourceActions) button.disabled = actionBusy || !select.value;
      });
      group.append(label, select);
      content.appendChild(group);
    }
    if (task.error_code || task.retry_at || task.scheduled_at || task.file_path) {
      const detail = document.createElement("span");
      detail.className = "card-detail";
      detail.textContent = task.file_path
        || (task.scheduled_at && task.status === "scheduled" ? "Scheduled for " + new Date(task.scheduled_at).toLocaleString()
          : task.retry_at ? "Retry after " + new Date(task.retry_at * 1000).toLocaleString() : task.error_code);
      meta.appendChild(detail);
    }
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const addAction = (label, execute, requiresSource = false) => {
      const button = document.createElement("button");
      button.className = "job-action";
      button.type = "button";
      button.textContent = label;
      button.disabled = actionBusy || (requiresSource && !jobSources.get(task.id));
      if (requiresSource) sourceActions.push(button);
      button.addEventListener("click", async () => {
        if (actionBusy || (requiresSource && !jobSources.get(task.id))) return;
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
    const selectedSource = () => ({ candidate_id: jobSources.get(task.id) || null });
    if (task.status === "waiting_for_auth") {
      if (headerRequired && !sources.length) addAction("Choose another source", chooseAnotherSource);
      else addAction(headerRequired ? "Share fresh context" : "Share fresh cookies", () => api.authenticate(task, selectedSource()),
        headerRequired || sources.length > 1);
    }
    if (PAUSABLE_STATUSES.has(task.status)) addAction("Pause", () => api.pauseDownload(task));
    if (task.status === "paused") addAction("Resume", () => api.resumeDownload(task));
    if (["failed", "cancelled"].includes(task.status)) addAction("Retry", () => api.retryDownload(task, selectedSource()),
      headerRequired || sources.length > 1);
    else if (task.status !== "complete" && task.status !== "cancelling") addAction("Cancel", () => api.cancelDownload(task));
    meta.appendChild(actions); content.appendChild(meta); card.appendChild(content);
    return card;
  }

  // --- Tab Navigation ---
  function switchTab(targetTab) {
    tabBtns.forEach(b => b.classList.toggle("active", b.getAttribute("data-tab") === targetTab));
    tabContents.forEach(c => c.classList.toggle("active", c.id === `panel-${targetTab}`));
    if (targetTab === "detected") void refreshPermissions();
  }

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      switchTab(btn.getAttribute("data-tab"));
    });
  });

  // --- Quick Download Trigger & Probe ---
  function submitInputUrl() {
    const url = videoUrlInput.value.trim();
    if (!url) return;
    if (!isValidUrl(url)) {
      showClientStatus("Enter an HTTP or HTTPS media URL without embedded credentials.", true);
      return;
    }
    triggerProbe(url);
  }

  downloadBtn.addEventListener("click", submitInputUrl);
  videoUrlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitInputUrl();
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

  function clearProbeRecovery() {
    clearTimeout(probeDeadlineTimer);
    probeDeadlineTimer = null;
    probeRecoveryAction = null;
    probeRecovery.hidden = true;
    probeActionBtn.hidden = true;
    probeActionBtn.disabled = false;
    freshProbeBtn.hidden = true;
  }

  function chooseAnotherSource() {
    closeModal();
    switchTab("detected");
  }

  function showProbeFailure(error) {
    clearProbeRecovery();
    currentProbedData = null;
    modalStartBtn.disabled = true;
    modalTitle.textContent = "Video information unavailable";
    modalAuthor.textContent = error.message;
    if (["AUTH_REQUIRED", "AUTH_CONTEXT_EXPIRED", "BROWSER_CONTEXT_REQUIRED"].includes(error.code)) {
      probeActionBtn.textContent = "Open source page";
      probeRecoveryAction = () => {
        const source = currentCandidate?.page_url || currentProbedUrl;
        if (isValidUrl(source)) chrome.tabs.create({ url: source });
      };
      freshProbeBtn.hidden = false;
    } else if (error.code === "RATE_LIMITED") {
      const deadline = Number.isFinite(error.retry_at) ? error.retry_at * 1000 : null;
      const updateDeadline = () => {
        const remaining = deadline === null ? 0 : Math.max(0, deadline - Date.now());
        probeActionBtn.textContent = remaining ? "Retry after " + new Date(deadline).toLocaleString() : "Retry probe";
        probeActionBtn.disabled = remaining > 0;
        if (remaining) probeDeadlineTimer = setTimeout(updateDeadline, Math.min(remaining, 2147483647));
      };
      updateDeadline();
      probeRecoveryAction = () => {
        if (deadline !== null && Date.now() < deadline) return;
        void triggerProbe(currentProbedUrl, true, currentCandidate);
      };
    } else if (error.code === "APP_INCOMPATIBLE") {
      probeActionBtn.textContent = "Review VidGrab update";
      probeRecoveryAction = () => chrome.runtime.openOptionsPage();
    } else if (error.code === "APP_UNAVAILABLE") {
      probeActionBtn.textContent = "Open in VidGrab";
      probeRecoveryAction = () => { void triggerOpenApp(probeActionBtn); };
    } else if (["UNSUPPORTED_MEDIA", "BROWSER_CONTEXT_EXPIRED", "FORMAT_UNAVAILABLE"].includes(error.code)) {
      probeActionBtn.textContent = "Choose another source";
      probeRecoveryAction = chooseAnotherSource;
    } else if (["PROBE_EXPIRED", "PROBE_CONTEXT_SCOPE"].includes(error.code)) {
      freshProbeBtn.hidden = false;
    } else if (["NETWORK_ERROR", "HTTP_ERROR"].includes(error.code)) {
      probeActionBtn.textContent = "Retry probe";
      probeRecoveryAction = () => { void triggerProbe(currentProbedUrl, true, currentCandidate); };
    }
    freshProbeBtn.textContent = currentCandidate || ["PROBE_EXPIRED", "PROBE_CONTEXT_SCOPE"].includes(error.code)
      ? "Probe with fresh context" : "Probe with fresh cookies";
    probeActionBtn.hidden = probeRecoveryAction === null;
    probeRecovery.hidden = probeActionBtn.hidden && freshProbeBtn.hidden;
  }

  probeActionBtn.addEventListener("click", () => {
    if (!probeActionBtn.disabled && probeRecoveryAction) probeRecoveryAction();
  });

  function modalSelection() {
    return { format: currentProbedData?.formats?.find(format => format.format_id === formatSelect.value),
      title: customTitleInput.value, audio_only: optionAudioOnly.checked, audio_format: audioFormatSelect.value,
      subtitles: optionSubtitles.checked, languages: Array.from(subtitleLangSelect.selectedOptions).map(option => option.value),
      subtitle_format: subtitleFormatSelect.value, embed_subtitles: optionEmbedSubtitles.checked, thumbnail: optionThumbnail.checked };
  }

  function equivalentFormat(left, right) {
    return left.resolution === right.resolution && left.ext === right.ext && (left.fps ?? null) === (right.fps ?? null)
      && (left.container || left.ext) === (right.container || right.ext)
      && (left.acodec === "none" || !left.acodec) === (right.acodec === "none" || !right.acodec);
  }

  function restoreModalSelection(review, info) {
    const equivalent = review.format && info.formats?.find(format => equivalentFormat(review.format, format));
    formatSelect.value = equivalent?.format_id || "";
    customTitleInput.value = review.title;
    optionAudioOnly.checked = review.audio_only && !optionAudioOnly.disabled;
    audioFormatSelect.value = review.audio_format;
    optionSubtitles.checked = review.subtitles && !optionSubtitles.disabled;
    for (const option of subtitleLangSelect.options) option.selected = review.languages.includes(option.value);
    subtitleFormatSelect.value = review.subtitle_format;
    optionEmbedSubtitles.checked = review.embed_subtitles && !optionEmbedSubtitles.disabled;
    optionThumbnail.checked = review.thumbnail && !optionThumbnail.disabled;
    optionAudioOnly.dispatchEvent(new Event("change"));
    optionSubtitles.dispatchEvent(new Event("change"));
    needsQualityChoice = Boolean(review.format && !equivalent);
    modalStartBtn.disabled = needsQualityChoice;
    modalAuthor.textContent = equivalent || !review.format
      ? "Formats refreshed once. Review the selection, then press Start Download."
      : "Formats refreshed once. The previous quality is unavailable. Choose another quality before starting.";
    formatSelect.focus();
  }

  formatSelect.addEventListener("change", () => {
    needsQualityChoice = false;
    if (currentProbedData && !actionBusy) modalStartBtn.disabled = false;
  });

  async function triggerProbe(url, fresh = false, candidate = null, review = null) {
    const sequence = ++probeNumber;
    if (review === null) expiredProbeRefreshed = false;
    needsQualityChoice = false;
    currentProbedUrl = url;
    currentProbedData = null;
    currentCandidate = candidate;
    showModalLoading();
    updateDefaults();
    clearProbeRecovery();
    formatModal.classList.add("active");
    if (!formatModal.open) formatModal.showModal();
    try {
      const info = await api.probeUrl(url, { fresh, candidate_id: candidate?.id || null });
      if (sequence !== probeNumber || currentProbedUrl !== url) return;
      currentProbedData = info;
      showModalDetails(info);
      updateDefaults();
      if (review) restoreModalSelection(review, info);
    } catch (error) {
      if (sequence !== probeNumber) return;
      showProbeFailure(error);
    }
  }
  freshProbeBtn.addEventListener("click", () => { if (currentProbedUrl) void triggerProbe(currentProbedUrl, true, currentCandidate); });

  // --- Extended Option Listeners ---
  optionAudioOnly.addEventListener("change", () => {
    if (optionAudioOnly.checked) {
      videoQualityGroup.style.display = "none";
      audioFormatGroup.style.display = "block";
      needsQualityChoice = false;
      if (currentProbedData && !actionBusy) modalStartBtn.disabled = false;
    } else {
      videoQualityGroup.style.display = "block";
      audioFormatGroup.style.display = "none";
    }
  });

  optionSubtitles.addEventListener("change", () => {
    subtitleOptionsGroup.style.display = optionSubtitles.checked ? "block" : "none";
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
    for (const checkbox of [optionAudioOnly, optionSubtitles, optionEmbedSubtitles, optionThumbnail]) {
      checkbox.disabled = !ffmpegAvailable;
      if (!ffmpegAvailable) checkbox.checked = false;
      checkbox.parentElement.style.opacity = ffmpegAvailable ? "1" : "0.5";
      checkbox.parentElement.style.pointerEvents = ffmpegAvailable ? "auto" : "none";
    }
    if (!ffmpegAvailable) {
      videoQualityGroup.style.display = "block";
      audioFormatGroup.style.display = "none";
      subtitleOptionsGroup.style.display = "none";
    }

    if (info.is_playlist) {
      modalHeaderTitle.textContent = "Download Playlist";
      modalSingleVideoContent.style.display = "none";
      modalPlaylistContent.style.display = "block";

      playlistTitle.textContent = info.title;
      playlistMeta.textContent = `${info.entries ? info.entries.length : 0} videos • ${info.uploader || "Unknown Channel"}`;

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
          titleText.style.fontSize = "12px";
          titleText.style.whiteSpace = "nowrap";
          titleText.style.overflow = "hidden";
          titleText.style.textOverflow = "ellipsis";

          const durationText = document.createElement("span");
          durationText.textContent = entry.duration ? formatDuration(entry.duration) : "";
          durationText.style.fontSize = "12px";
          durationText.style.color = "var(--text-secondary)";

          row.append(checkbox, thumbnail, titleText, durationText);
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

      formatSelect.innerHTML = "";
      const bestOpt = document.createElement("option");
      bestOpt.value = "";
      bestOpt.textContent = "App default (" + (appDefaults?.default_format || "loading") + ")";
      formatSelect.appendChild(bestOpt);

      if (info.formats) {
        const uniqueFormats = new Set();
        const cleanFormats = [];
        info.formats.forEach(f => {
          const identity = JSON.stringify([f.resolution, f.ext, f.fps, f.container, f.acodec === "none" || !f.acodec]);
          if (f.resolution && !uniqueFormats.has(identity)) {
            uniqueFormats.add(identity);
            cleanFormats.push(f);
          }
        });
        cleanFormats.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.format_id;
          const sizeStr = f.filesize ? ` (~${formatBytes(f.filesize)})` : "";
          opt.textContent = `${f.resolution} (${f.ext.toUpperCase()}${f.fps ? ", " + f.fps + " fps" : ""})${sizeStr}`;
          formatSelect.appendChild(opt);
        });
      }

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

  function closeModal() {
    ++probeNumber;
    clearProbeRecovery();
    formatModal.classList.remove("active");
    if (formatModal.open) formatModal.close();
    currentProbedUrl = "";
    currentProbedData = null;
    currentCandidate = null;
    if (customTitleInput) customTitleInput.value = "";
    if (modalDownloadDirInput) modalDownloadDirInput.value = "";
    if (modalOutputTemplateInput) modalOutputTemplateInput.value = "";
    if (playlistDownloadDirInput) playlistDownloadDirInput.value = "";
  }

  modalCloseBtn.addEventListener("click", closeModal);
  modalCancelBtn.addEventListener("click", closeModal);
  formatModal.addEventListener("cancel", event => { event.preventDefault(); closeModal(); });

  modalStartBtn.addEventListener("click", async () => {
    if (!currentProbedUrl || !currentProbedData || actionBusy || needsQualityChoice) return;
    const submissionSequence = probeNumber;
    modalStartBtn.disabled = true;
    actionBusy = true;
    const review = modalSelection();
    showClientStatus("");
    try {
      let formatId;
      let options;
      let probeId = null;
      if (currentProbedData.is_playlist) {
        const selected = Array.from(playlistEntriesList.querySelectorAll(".playlist-item-checkbox:checked"));
        if (!selected.length) throw new Error("Select at least one playlist item.");
        formatId = playlistFormatSelect.value || null;
        options = { is_playlist: true, playlist_items: selected.map(checkbox => checkbox.dataset.position).join(","),
          audio_only: formatId === "bestaudio/best", audio_format: "mp3" };
      } else {
        formatId = optionAudioOnly.checked ? null : formatSelect.value || null;
        const selected = currentProbedData.formats?.find(format => format.format_id === formatId);
        if (selected && !optionAudioOnly.checked) probeId = currentProbedData.probe_id;
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
        currentProbedData.thumbnail, options, { candidate_id: currentCandidate?.id || null, probe_id: probeId });
      videoUrlInput.value = "";
      closeModal();
      await api.sync();
      switchTab("queue");
      showClientStatus("Download queued.");
    } catch (error) {
      if (submissionSequence !== probeNumber && !api.session.pending) return;
      if (error.code === "PROBE_EXPIRED" && !api.session.pending) {
        if (!expiredProbeRefreshed) {
          expiredProbeRefreshed = true;
          await triggerProbe(currentProbedUrl, true, currentCandidate, review);
        } else showProbeFailure({ ...error, message: "The refreshed selection expired again. Probe explicitly to review new formats." });
        return;
      }
      if (api.session.pending) closeModal();
      else if (["AUTH_REQUIRED", "AUTH_CONTEXT_EXPIRED", "BROWSER_CONTEXT_REQUIRED", "BROWSER_CONTEXT_EXPIRED", "PROBE_CONTEXT_SCOPE", "APP_INCOMPATIBLE", "APP_UNAVAILABLE"].includes(error.code)) showProbeFailure(error);
      else modalAuthor.textContent = error.message;
      showClientStatus(api.session.pending ? "The request may have succeeded. Check it before starting another download." : error.message, true);
      if (api.session.pending) checkRequestBtn.focus();
    } finally { actionBusy = false; modalStartBtn.disabled = !currentProbedData || needsQualityChoice; renderAll(); }
  });

  retryBackendBtn.addEventListener("click", () => { void checkBackendHealth(); });

  settingsBtn.addEventListener("click", () => {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.openOptionsPage) {
      chrome.runtime.openOptionsPage();
    } else {
      const url = typeof chrome !== "undefined" && chrome.runtime ? chrome.runtime.getURL("options/options.html") : "../options/options.html";
      window.open(url);
    }
  });

  // --- Detected Videos Handling ---
  const selectionKey = video => video.id || video.url;
  function loadDetectedVideos() {
    if (activeTabId == null) return;
    const storageKey = `detected_videos_${activeTabId}`;
    chrome.storage.local.get([storageKey], (result) => {
      renderDetectedVideos(result[storageKey] || []);
    });
  }

  function renderDetectedVideos(videos) {
    detectedCandidates = videos;
    for (const [job, source] of jobSources) if (!videos.some(candidate => candidate.id === source)) jobSources.delete(job);
    renderAll();
    if (!detectedList) return;

    badgeDetected.textContent = videos.length;
    detectedList.innerHTML = "";

    const currentUrls = new Set(videos.map(selectionKey));
    for (const url of selectedDetectedUrls) {
      if (!currentUrls.has(url)) selectedDetectedUrls.delete(url);
    }

    if (videos.length === 0) {
      detectedEmpty.style.display = "flex";
      detectedList.style.display = "none";
      if (detectedActions) detectedActions.style.display = "none";
    } else {
      detectedEmpty.style.display = "none";
      detectedList.style.display = "block";
      if (detectedActions) detectedActions.style.display = "flex";

      videos.forEach((video) => {
        const card = document.createElement("div");
        card.className = "download-card";
        card.style.paddingLeft = "8px";

        let hostname = "";
        try { hostname = new URL(video.url).hostname; } catch { hostname = video.url; }
        const isChecked = selectedDetectedUrls.has(selectionKey(video));

        card.innerHTML = `
          <label class="checkbox-container" style="margin: 0; padding-left: 16px; align-self: center; width: 16px; height: 16px;">
            <input type="checkbox" class="detected-item-checkbox">
            <span class="checkbox-checkmark" style="left: 0;"></span>
          </label>
          <img class="card-thumbnail" src="../icons/icon128.png" alt="Thumbnail">
          <div class="card-content">
            <div class="card-title"></div>
            <div class="card-meta">
              <span class="card-status-badge complete" style="background: rgba(124, 107, 240, 0.15); color: #b5acff; border-color: rgba(124, 107, 240, 0.3);">
                Detected
              </span>
              <span class="meta-details" style="text-overflow: ellipsis; overflow: hidden; white-space: nowrap; max-width: 160px;"></span>
              <div class="card-actions">
                <button class="icon-button download-detected-btn" type="button" title="Download Video">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                </button>
              </div>
            </div>
          </div>
        `;

        const checkbox = card.querySelector(".detected-item-checkbox");
        checkbox.dataset.url = video.url;
        checkbox.dataset.selection = selectionKey(video);
        checkbox.checked = isChecked;
        checkbox.setAttribute("aria-label", "Select " + (video.title || "detected video"));
        const title = card.querySelector(".card-title");
        title.textContent = video.title || "Detected video";
        title.title = title.textContent;
        const details = card.querySelector(".meta-details");
        details.textContent = hostname;
        details.title = video.url;
        const kinds = { audio: "Audio", video: "Video", hls: "HLS", dash: "DASH", page: "Page", browser_stream: "Browser stream" };
        card.querySelector(".card-status-badge").textContent = kinds[video.kind] || "Detected";
        if (video.frame_url) {
          const frameHost = new URL(video.frame_url).hostname;
          details.textContent = (video.browser_local ? "Page probe · " : "") + frameHost;
          details.title = (video.observed_content_type || video.source || "Media") + " · " + frameHost;
        }
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selectedDetectedUrls.add(selectionKey(video));
          else selectedDetectedUrls.delete(selectionKey(video));
          updateDetectedSelectionUI(videos);
        });

        const dlBtn = card.querySelector(".download-detected-btn");
        dlBtn.title = video.browser_local ? "Check page for downloadable media" : "Choose media download options";
        dlBtn.setAttribute("aria-label", dlBtn.title);
        dlBtn.addEventListener("click", () => { triggerProbe(video.url, false, video); });

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
    detectedSelectAll.checked = videos.length > 0 && videos.every(v => selectedDetectedUrls.has(selectionKey(v)));
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
        if (detectedSelectAll.checked) videos.forEach(v => selectedDetectedUrls.add(selectionKey(v)));
        else videos.forEach(v => selectedDetectedUrls.delete(selectionKey(v)));
        detectedList.querySelectorAll(".detected-item-checkbox").forEach(cb => { cb.checked = detectedSelectAll.checked; });
        updateDetectedSelectionUI(videos);
      });
    });
  }

  if (detectedRemoveBtn) {
    detectedRemoveBtn.addEventListener("click", async () => {
      if (activeTabId == null || selectedDetectedUrls.size === 0) return;
      try {
        await api.removeCandidates(activeTabId, [...selectedDetectedUrls]);
        selectedDetectedUrls.clear();
        loadDetectedVideos();
      } catch (error) { showClientStatus(error.message, true); }
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
        if (!selectedDetectedUrls.has(selectionKey(video))) continue;
        const audioOnly = batchOptionAudioOnly.checked;
        await api.startDownload(video.url, audioOnly ? "bestaudio/best" : batchQualitySelect.value || null,
          video.title || null, null, { audio_only: audioOnly, audio_format: "mp3" }, { candidate_id: video.id || null });
        added++;
      }
      closeBatchModal();
      await api.sync();
      switchTab("queue");
      showClientStatus(added + " downloads queued.");
    } catch (error) {
      closeBatchModal();
      showClientStatus(added + " queued. " + (api.session.pending ? "The last request may have succeeded. Check it before continuing." : error.message), true);
      if (api.session.pending) checkRequestBtn.focus();
    } finally { actionBusy = false; batchModalStartBtn.disabled = false; renderAll(); }
  });

  // --- Startup ---
  connect();

  if (typeof chrome !== "undefined" && chrome.tabs) {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs && tabs[0]) {
        activeTabId = tabs[0].id;
        loadDetectedVideos();
        chrome.storage.local.get(["pendingVideoCandidate", "pendingVideoUrl", "detected_videos_" + activeTabId], result => {
          const pending = result.pendingVideoCandidate;
          if (pending) {
            if (pending.browser_context?.tab_id !== activeTabId) return;
            chrome.storage.local.remove(["pendingVideoCandidate", "pendingVideoUrl"]);
            const selected = (result["detected_videos_" + activeTabId] || []).find(item => item.id === pending.id);
            if (!selected || selected.page_url !== tabs[0].url) {
              showClientStatus("The source page changed. Select media from the current tab.", true); return;
            }
            videoUrlInput.value = selected.url;
            void triggerProbe(selected.url, false, selected);
          } else if (isValidUrl(result.pendingVideoUrl)) {
            chrome.storage.local.remove("pendingVideoUrl");
            videoUrlInput.value = result.pendingVideoUrl;
            void triggerProbe(result.pendingVideoUrl);
          }
        });
      }
    });

    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName === "local" && activeTabId !== null && changes[`detected_videos_${activeTabId}`]) {
        renderDetectedVideos(changes[`detected_videos_${activeTabId}`].newValue || []);
      }
      if (areaName === "local" && changes.requestObservationEnabled) void refreshPermissions();
    });
  }

  chrome.permissions?.onAdded?.addListener?.(() => void refreshPermissions());
  chrome.permissions?.onRemoved?.addListener?.(() => void refreshPermissions());
});
