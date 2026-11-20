document.addEventListener("DOMContentLoaded", () => {
  const api = window.vidGrabAPI || new VidGrabAPI();

  const backendStatus = document.getElementById("backend-status");
  const btnOpenApp = document.getElementById("btn-open-app");
  const btnEditDefaultsApp = document.getElementById("btn-edit-defaults-app");
  const btnTestBackend = document.getElementById("btn-test-backend");
  const btnReloadSettings = document.getElementById("btn-reload-settings");
  const btnCheckRequest = document.getElementById("btn-check-request");
  const statusMessage = document.getElementById("status-message");
  const ffmpegWarningBanner = document.getElementById("ffmpeg-warning-banner");

  // Read-only App Defaults Summary elements
  const summaryDownloadDir = document.getElementById("summary-download-dir");
  const summaryDefaultFormat = document.getElementById("summary-default-format");
  const summaryDefaultQuality = document.getElementById("summary-default-quality");
  const summaryMaxConcurrent = document.getElementById("summary-max-concurrent");
  const summaryFilenameTemplate = document.getElementById("summary-filename-template");
  const summaryTransliterateBangla = document.getElementById("summary-transliterate-bangla");
  const ytdlpVersionStatus = document.getElementById("ytdlp-version-status");

  // Browser permissions & preferences elements
  const permissionStatus = document.getElementById("permission-status");
  const requestObservationRestartNotice = document.getElementById("request-observation-restart-notice");
  const chkCookieForwarding = document.getElementById("chk-cookie-forwarding");
  const chkRequestObservation = document.getElementById("chk-request-observation");
  const selectNotifications = document.getElementById("select-notifications");

  // Request notices elements
  const requestNotices = document.getElementById("request-notices");
  const requestNoticeList = document.getElementById("request-notice-list");
  const requestNoticeStatus = document.getElementById("request-notice-status");

  let statusTimeout = null;
  function showStatus(text, type = "") {
    statusMessage.textContent = text;
    statusMessage.className = "status-banner " + type;
    if (statusTimeout) clearTimeout(statusTimeout);
    if (type === "success") {
      statusTimeout = setTimeout(() => {
        statusMessage.textContent = "";
        statusMessage.className = "status-banner";
      }, 4000);
    }
  }

  function applySummary(settingsSnapshot, capabilities = null) {
    const config = settingsSnapshot?.values;
    if (!config) return;
    summaryDownloadDir.textContent = config.download_dir || "Default downloads directory";
    summaryDefaultFormat.textContent = config.default_format || "bestvideo+bestaudio/best";
    const rawQuality = String(config.default_quality || "1080");
    summaryDefaultQuality.textContent = rawQuality.toLowerCase() === "best"
      ? "Best"
      : (rawQuality.endsWith("p") ? rawQuality : `${rawQuality}p`);
    summaryMaxConcurrent.textContent = String(config.max_concurrent ?? 3);
    summaryFilenameTemplate.textContent = config.filename_template || "%(title)s.%(ext)s";
    summaryTransliterateBangla.textContent = config.transliterate_bangla_filenames === false
      ? "Keep native Unicode" : "Transliterate to Latin";
    if (capabilities) {
      ytdlpVersionStatus.textContent = capabilities.component_versions?.yt_dlp
        || capabilities.component_versions?.["yt-dlp"] || "Unavailable";
      const hasFfmpeg = Boolean(capabilities.component_versions?.ffmpeg);
      ffmpegWarningBanner.hidden = hasFfmpeg;
    }
  }

  function setConnection(connected, capabilities = null) {
    backendStatus.className = "status-indicator " + (connected ? "online" : "offline");
    backendStatus.querySelector(".status-text").textContent = connected ? "Companion Connected" : "Companion Unavailable";
    if (!connected) {
      ffmpegWarningBanner.hidden = true;
      ytdlpVersionStatus.textContent = "Unavailable";
      if (summaryDownloadDir.textContent === "Connecting to VidGrab…") {
        summaryDownloadDir.textContent = "Companion unavailable — open VidGrab to view";
      }
    } else if (capabilities) {
      ytdlpVersionStatus.textContent = capabilities.component_versions?.yt_dlp
        || capabilities.component_versions?.["yt-dlp"] || "Unavailable";
      ffmpegWarningBanner.hidden = Boolean(capabilities.component_versions?.ffmpeg);
    }
  }

  async function refreshPermissions() {
    try {
      const permissions = await api.getPermissions();
      permissionStatus.textContent = permissions.permission_api_available
        ? "Cookies: " + (permissions.cookies_granted ? "granted" : "not granted")
          + " · Native messaging: " + (permissions.native_granted ? "granted" : "not granted")
          + " · Notifications: " + (permissions.notifications_granted ? "granted" : "not granted")
          + " · Request observation: " + (permissions.request_observation_granted
            ? (permissions.request_observation_restart_required ? "granted (restart Chrome to activate)" : "granted")
            : "not granted")
          + ". Site access: " + (permissions.origins.length ? permissions.origins.join(", ") : "none")
        : "This browser cannot report its granted permissions.";
      if (requestObservationRestartNotice) {
        requestObservationRestartNotice.hidden = !permissions?.request_observation_restart_required;
      }
      return permissions;
    } catch {
      permissionStatus.textContent = "Browser permissions could not be read.";
      if (requestObservationRestartNotice) requestObservationRestartNotice.hidden = true;
      return null;
    }
  }

  async function refreshNotices() {
    try {
      const notices = await api.getPending();
      requestNoticeList.replaceChildren();
      requestNotices.hidden = notices.length === 0;
      btnCheckRequest.hidden = api.session.pending === null;
      requestNoticeStatus.textContent = "";
      const names = {
        "downloads.enqueue": "Download request",
        "settings.patch": "Settings change",
        "downloads.action": "Download action",
        "downloads.authenticate": "Fresh authentication",
      };
      for (const notice of notices) {
        const row = document.createElement("li");
        const description = document.createElement("p");
        description.textContent = names[notice.operation] || "Companion request";
        const identity = document.createElement("code");
        identity.textContent = notice.key;
        description.append(identity);
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "btn btn-secondary";
        dismiss.textContent = "Dismiss reviewed notice";
        dismiss.addEventListener("click", async () => {
          dismiss.disabled = true;
          try {
            await api.dismissPending(notice.key);
            btnCheckRequest.hidden = api.session.pending === null;
            await refreshNotices();
            showStatus("Request notice dismissed. Downloads and settings are unchanged.", "success");
          } catch (error) {
            requestNoticeStatus.textContent = error.message;
            dismiss.disabled = false;
          }
        });
        row.append(description, dismiss);
        requestNoticeList.append(row);
      }
    } catch (error) {
      requestNotices.hidden = false;
      requestNoticeStatus.textContent = error.message;
    }
  }

  async function triggerOpenApp(button) {
    button.disabled = true;
    try {
      await api.openApp();
      showStatus("Opening VidGrab desktop app.", "success");
    } catch (error) {
      showStatus(error?.message || "Could not open VidGrab. Check that the desktop companion is installed.", "error");
    } finally {
      button.disabled = false;
    }
  }

  btnOpenApp.addEventListener("click", () => { void triggerOpenApp(btnOpenApp); });
  btnEditDefaultsApp.addEventListener("click", () => { void triggerOpenApp(btnEditDefaultsApp); });

  btnTestBackend.addEventListener("click", async () => {
    btnTestBackend.disabled = true;
    btnTestBackend.textContent = "Connecting...";
    try {
      const state = await api.sync(true);
      setConnection(true, state.capabilities);
      applySummary(state.snapshot.settings, state.capabilities);
      await refreshPermissions();
      showStatus("Connected to VidGrab. Download defaults come from the app.", "success");
    } catch (error) {
      setConnection(false);
      showStatus(error.message, "error");
    } finally {
      btnTestBackend.disabled = false;
      btnTestBackend.textContent = "Test connection";
    }
  });

  btnReloadSettings.addEventListener("click", async () => {
    btnReloadSettings.disabled = true;
    try {
      const state = await api.sync(true);
      setConnection(true, state.capabilities);
      applySummary(state.snapshot.settings, state.capabilities);
      showStatus("Current app defaults loaded.", "success");
    } catch (error) {
      setConnection(false);
      showStatus(error.message, "error");
    } finally {
      btnReloadSettings.disabled = false;
    }
  });

  btnCheckRequest.addEventListener("click", async () => {
    btnCheckRequest.disabled = true;
    try {
      await api.retryPending();
      const state = await api.sync(true);
      applySummary(state.snapshot.settings, state.capabilities);
      btnCheckRequest.hidden = true;
      await refreshNotices();
      showStatus("Request checked. Current app summary refreshed.", "success");
    } catch (error) {
      showStatus(error.message, "error");
    } finally {
      btnCheckRequest.disabled = false;
    }
  });

  async function saveBrowserPreference(patch, successMessage) {
    await new Promise((resolve, reject) => chrome.storage.local.set(patch, () => (
      chrome.runtime.lastError ? reject(new Error("Browser preferences could not be saved.")) : resolve()
    )));
    await refreshPermissions();
    showStatus(successMessage, "success");
  }

  chkCookieForwarding.addEventListener("change", async () => {
    try {
      await saveBrowserPreference(
        { cookieForwardingEnabled: chkCookieForwarding.checked },
        chkCookieForwarding.checked ? "Cookie forwarding enabled." : "Cookie forwarding disabled.",
      );
    } catch (error) {
      chkCookieForwarding.checked = !chkCookieForwarding.checked;
      showStatus(error.message, "error");
    }
  });

  selectNotifications.addEventListener("change", async () => {
    try {
      await saveBrowserPreference(
        { notificationPreference: selectNotifications.value },
        "Browser notification preference saved.",
      );
    } catch (error) {
      showStatus(error.message, "error");
    }
  });

  chkRequestObservation.addEventListener("change", async () => {
    const requested = chkRequestObservation.checked;
    chkRequestObservation.disabled = true;
    try {
      const granted = !requested || await new Promise(resolve => chrome.permissions.request(
        { permissions: ["webRequest", "webNavigation"] },
        result => resolve(!chrome.runtime.lastError && result === true),
      ));
      const enabled = requested && granted;
      await new Promise((resolve, reject) => chrome.storage.local.set(
        { requestObservationEnabled: enabled },
        () => (chrome.runtime.lastError ? reject(new Error("Browser preference could not be saved.")) : resolve()),
      ));
      chkRequestObservation.checked = enabled;
      const permissions = await refreshPermissions();
      showStatus(
        requested && !granted
          ? "Permission was not granted. Media element discovery remains available."
          : enabled
            ? (permissions?.request_observation_restart_required
              ? "Permission granted. Restart Chrome to activate request metadata observation; page discovery works now."
              : "Media request observation enabled for sites the browser permits.")
            : "Media request observation disabled.",
        requested && !granted ? "error" : "success",
      );
    } catch (error) {
      chkRequestObservation.checked = !requested;
      showStatus(error.message, "error");
    } finally {
      chkRequestObservation.disabled = !chrome.permissions?.request;
    }
  });

  api.addEventListener("snapshot", event => {
    setConnection(true, event.detail.capabilities);
    applySummary(event.detail.snapshot.settings, event.detail.capabilities);
  });

  api.addEventListener("connection_change", event => {
    if (!event.detail.connected) {
      setConnection(false);
    }
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.cookieForwardingEnabled) chkCookieForwarding.checked = changes.cookieForwardingEnabled.newValue === true;
    if (changes.notificationPreference) selectNotifications.value = changes.notificationPreference.newValue || "both";
    if (changes.requestObservationEnabled) chkRequestObservation.checked = changes.requestObservationEnabled.newValue === true;
    if (changes.cookieForwardingEnabled || changes.notificationPreference || changes.requestObservationEnabled) void refreshPermissions();
    if (changes.vidgrabPendingMutations) void refreshNotices();
  });

  chrome.permissions?.onAdded?.addListener?.(() => void refreshPermissions());
  chrome.permissions?.onRemoved?.addListener?.(() => void refreshPermissions());
  window.addEventListener("pagehide", () => api.disconnect());

  async function init() {
    showStatus("Loading settings...");
    const store = await new Promise(resolve => chrome.storage.local.get(
      ["cookieForwardingEnabled", "requestObservationEnabled", "notificationPreference"],
      resolve,
    ));
    chkCookieForwarding.checked = store.cookieForwardingEnabled === true;
    chkRequestObservation.checked = store.requestObservationEnabled === true;
    chkRequestObservation.disabled = !chrome.permissions?.request;
    selectNotifications.value = store.notificationPreference || "both";
    await refreshPermissions();
    await refreshNotices();
    try {
      const state = await api.sync(true);
      setConnection(true, state.capabilities);
      applySummary(state.snapshot.settings, state.capabilities);
      showStatus("Settings loaded successfully.", "success");
    } catch (error) {
      setConnection(false);
      showStatus(error.message || "Open VidGrab to connect to the companion. Browser preferences remain available.", "error");
    }
  }

  void init().finally(() => api.connect());
});
