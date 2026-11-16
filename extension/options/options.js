document.addEventListener("DOMContentLoaded", () => {
  const api = window.vidGrabAPI || new VidGrabAPI();
  
  // DOM Elements - Navigation
  const navItems = document.querySelectorAll(".nav-item");
  const tabPanels = document.querySelectorAll(".tab-panel");
  const tabTitle = document.getElementById("tab-title");
  const tabSubtitle = document.getElementById("tab-subtitle");
  const backendStatus = document.getElementById("backend-status");
  
  // DOM Elements - General settings
  const inputDownloadDir = document.getElementById("input-download-dir");
  const inputMaxConcurrent = document.getElementById("input-max-concurrent");
  const inputFilenameTemplate = document.getElementById("input-filename-template");
  const selectDefaultQuality = document.getElementById("select-default-quality");
  const selectDefaultFormat = document.getElementById("select-default-format");
  const inputDownloadThreads = document.getElementById("input-download-threads");
  const downloadThreadsValue = document.getElementById("download-threads-value");
  
  // DOM Elements - SponsorBlock settings
  const chkSponsorblockEnabled = document.getElementById("chk-sponsorblock-enabled");
  const selectSponsorblockMode = document.getElementById("select-sponsorblock-mode");
  const sponsorblockDetailsSection = document.getElementById("sponsorblock-details-section");
  const sbCategoryChecks = document.querySelectorAll(".chk-sb-category");
  
  // DOM Elements - Postprocessing settings
  const selectPostprocessCodec = document.getElementById("select-postprocess-codec");
  const chkPostprocessNormalize = document.getElementById("chk-postprocess-normalize");
  const chkGpuAccelerated = document.getElementById("chk-gpu-accelerated");
  const gpuConversionSection = document.getElementById("gpu-conversion-section");
  
  // DOM Elements - Advanced settings
  const inputBackendUrl = document.getElementById("input-backend-url");
  const btnTestBackend = document.getElementById("btn-test-backend");
  const chkCookieForwarding = document.getElementById("chk-cookie-forwarding");
  const selectNotifications = document.getElementById("select-notifications");
  const selectSpeedLimit = document.getElementById("select-speed-limit");
  const customSpeedGroup = document.getElementById("custom-speed-group");
  const inputCustomSpeed = document.getElementById("input-custom-speed");
  const chkAutoUpdateYtdlp = document.getElementById("chk-auto-update-ytdlp");
  const ytdlpVersionStatus = document.getElementById("ytdlp-version-status");
  const btnUpdateYtdlp = document.getElementById("btn-update-ytdlp");
  const inputCustomYtdlpArgs = document.getElementById("input-custom-ytdlp-args");
  const btnPresetRobust = document.getElementById("btn-preset-robust");
  const btnPresetIpv4 = document.getElementById("btn-preset-ipv4");
  const btnPresetBypass = document.getElementById("btn-preset-bypass");
  const btnPresetClear = document.getElementById("btn-preset-clear");
  
  // DOM Elements - Footer
  const btnSaveSettings = document.getElementById("btn-save-settings");
  const statusMessage = document.getElementById("status-message");
  const btnReloadSettings = document.getElementById("btn-reload-settings");
  const btnCheckRequest = document.getElementById("btn-check-request");
  const permissionStatus = document.getElementById("permission-status");
  const requestNotices = document.getElementById("request-notices");
  const requestNoticeList = document.getElementById("request-notice-list");
  const requestNoticeStatus = document.getElementById("request-notice-status");
  let loaded = null;
  const touched = new Set();
  const appFields = [
    [inputDownloadDir, "download_dir"], [inputMaxConcurrent, "max_concurrent"],
    [inputFilenameTemplate, "filename_template"], [selectDefaultFormat, "default_format"],
    [selectDefaultQuality, "default_quality"], [inputDownloadThreads, "download_threads"],
    [chkSponsorblockEnabled, "sponsorblock_enabled"], [selectSponsorblockMode, "sponsorblock_mode"],
    [selectPostprocessCodec, "postprocess_codec"], [chkPostprocessNormalize, "postprocess_normalize_audio"],
    [chkGpuAccelerated, "gpu_accelerated_conversion"], [selectSpeedLimit, "speed_limit"],
    [inputCustomSpeed, "speed_limit"], [inputCustomYtdlpArgs, "custom_ytdlp_args"],
    ...Array.from(sbCategoryChecks, checkbox => [checkbox, "sponsorblock_categories"]),
  ];
  for (const [control, key] of appFields) {
    control.addEventListener("input", () => touched.add(key));
    control.addEventListener("change", () => touched.add(key));
  }

  const tabMetadata = {
    "tab-general": {
      title: "General Settings",
      subtitle: "Configure paths, defaults and concurrent operations."
    },
    "tab-sponsorblock": {
      title: "SponsorBlock Settings",
      subtitle: "Automatically skip or mark unwanted segments in YouTube videos."
    },
    "tab-postprocess": {
      title: "Post-Processing Settings",
      subtitle: "Configure video transcoding, container formatting, and audio normalization."
    },
    "tab-advanced": {
      title: "Advanced Settings",
      subtitle: "Configure cookie forwarding, speed limits, and extension connection."
    }
  };

  // --- Tab Navigation ---
  navItems.forEach(item => {
    item.addEventListener("click", () => {
      const tabId = item.dataset.tab;
      
      navItems.forEach(nav => {
        nav.classList.remove("active");
        nav.setAttribute("aria-pressed", "false");
      });
      tabPanels.forEach(panel => panel.classList.remove("active"));
      
      item.classList.add("active");
      item.setAttribute("aria-pressed", "true");
      document.getElementById(tabId).classList.add("active");
      
      // Update header
      const meta = tabMetadata[tabId];
      if (meta) {
        tabTitle.textContent = meta.title;
        tabSubtitle.textContent = meta.subtitle;
      }
      if (tabId === "tab-advanced") void refreshNotices();
    });
  });

  // --- Dynamic Subsections Visibility ---
  chkSponsorblockEnabled.addEventListener("change", () => {
    toggleSponsorBlockSection();
  });

  function toggleSponsorBlockSection() {
    if (chkSponsorblockEnabled.checked) {
      sponsorblockDetailsSection.style.display = "block";
    } else {
      sponsorblockDetailsSection.style.display = "none";
    }
  }

  selectSpeedLimit.addEventListener("change", () => {
    toggleCustomSpeedInput();
  });

  function toggleCustomSpeedInput() {
    if (selectSpeedLimit.value === "custom") {
      customSpeedGroup.style.display = "block";
    } else {
      customSpeedGroup.style.display = "none";
    }
  }

  // --- Dynamic Postprocessing GPU Section ---
  selectPostprocessCodec.addEventListener("change", () => {
    toggleGpuSection();
  });

  function toggleGpuSection() {
    if (selectPostprocessCodec.value === "mp4") {
      gpuConversionSection.style.display = "flex";
    } else {
      gpuConversionSection.style.display = "none";
      chkGpuAccelerated.checked = false;
    }
  }

  // Update thread slider text
  inputDownloadThreads.addEventListener("input", (e) => {
    downloadThreadsValue.textContent = e.target.value;
  });

  function applySettings(snapshot) {
    loaded = snapshot;
    populateBackendSettings(snapshot.values);
    touched.clear();
  }

  async function refreshPermissions() {
    try {
      const permissions = await api.getPermissions();
      permissionStatus.textContent = permissions.permission_api_available
        ? "Cookies: " + (permissions.cookies_granted ? "granted" : "not granted")
          + " · Native messaging: " + (permissions.native_granted ? "granted" : "not granted")
          + " · Notifications: " + (permissions.notifications_granted ? "granted" : "not granted")
          + ". Site access: " + (permissions.origins.length ? permissions.origins.join(", ") : "none")
        : "This browser cannot report its granted permissions.";
    } catch { permissionStatus.textContent = "Browser permissions could not be read."; }
  }

  async function refreshNotices() {
    try {
      const notices = await api.getPending();
      requestNoticeList.replaceChildren();
      requestNotices.hidden = notices.length === 0;
      requestNoticeStatus.textContent = "";
      const names = {"downloads.enqueue": "Download request", "settings.patch": "Settings change",
        "downloads.action": "Download action", "downloads.authenticate": "Fresh authentication"};
      for (const notice of notices) {
        const row = document.createElement("li");
        const description = document.createElement("p");
        description.textContent = names[notice.operation] || "Companion request";
        const identity = document.createElement("code");
        identity.textContent = notice.key;
        description.append(identity);
        const dismiss = document.createElement("button");
        dismiss.type = "button"; dismiss.className = "btn btn-secondary";
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
        row.append(description, dismiss); requestNoticeList.append(row);
      }
    } catch (error) {
      requestNotices.hidden = false;
      requestNoticeStatus.textContent = error.message;
    }
  }

  function setConnection(connected, capabilities = null) {
    backendStatus.className = "status-indicator " + (connected ? "online" : "offline");
    backendStatus.querySelector(".status-text").textContent = connected ? "Companion Connected" : "Companion Unavailable";
    for (const [control] of appFields) control.disabled = !connected;
    inputBackendUrl.disabled = true;
    inputCustomYtdlpArgs.disabled = true;
    for (const button of [btnPresetRobust, btnPresetIpv4, btnPresetBypass]) button.disabled = true;
    btnPresetClear.disabled = !connected || !inputCustomYtdlpArgs.value;
    const ffmpeg = Boolean(capabilities?.component_versions.ffmpeg);
    const warning = document.getElementById("ffmpeg-warning-banner");
    if (warning) warning.style.display = connected && !ffmpeg ? "block" : "none";
    selectPostprocessCodec.disabled = !connected || !ffmpeg;
    chkPostprocessNormalize.disabled = !connected || !ffmpeg;
    ytdlpVersionStatus.textContent = capabilities?.component_versions.yt_dlp
      || capabilities?.component_versions["yt-dlp"] || "Unavailable";
  }

  api.addEventListener("snapshot", event => {
    const snapshot = event.detail.snapshot.settings;
    setConnection(true, event.detail.capabilities);
    if (!loaded || (snapshot.revision !== loaded.revision && !touched.size)) applySettings(snapshot);
    else if (snapshot.revision !== loaded.revision) showStatus("Settings changed in another window. Your edits are kept; reload app settings before saving.", "error");
  });
  api.addEventListener("connection_change", event => {
    if (!event.detail.connected) {
      setConnection(false);
      showStatus(event.detail.error?.message || "Open VidGrab to connect to the companion.", "error");
    }
  });

  async function loadSettings() {
    showStatus("Loading settings...", "");
    const store = await new Promise(resolve => chrome.storage.local.get(["backendUrl", "cookieForwardingEnabled", "notificationPreference"], resolve));
    inputBackendUrl.value = store.backendUrl || "";
    chkCookieForwarding.checked = store.cookieForwardingEnabled === true;
    selectNotifications.value = store.notificationPreference || "both";
    await refreshPermissions();
    await refreshNotices();
    try {
      const state = await api.sync(true);
      applySettings(state.snapshot.settings);
      setConnection(true, state.capabilities);
      showStatus("Settings loaded successfully.", "success");
    } catch (error) {
      setConnection(false);
      showStatus(error.message || "Open VidGrab to change app settings. Browser preferences can still be saved.", "error");
    }
  }

  function populateBackendSettings(config) {
    if (!config) return;
    
    // General
    inputDownloadDir.value = config.download_dir || "";
    inputMaxConcurrent.value = config.max_concurrent || 3;
    inputFilenameTemplate.value = config.filename_template || "";
    selectDefaultQuality.value = config.default_quality || "1080";
    selectDefaultFormat.value = config.default_format || "";
    inputDownloadThreads.value = config.download_threads || 4;
    downloadThreadsValue.textContent = inputDownloadThreads.value;
    
    if (chkAutoUpdateYtdlp) {
      chkAutoUpdateYtdlp.checked = false;
      chkAutoUpdateYtdlp.disabled = true;
    }
    
    // SponsorBlock
    chkSponsorblockEnabled.checked = config.sponsorblock_enabled || false;
    toggleSponsorBlockSection();
    selectSponsorblockMode.value = config.sponsorblock_mode || "remove";
    
    const categories = config.sponsorblock_categories || [];
    sbCategoryChecks.forEach(chk => {
      chk.checked = categories.includes(chk.value);
    });
    
    // Post-processing
    selectPostprocessCodec.value = config.postprocess_codec || "none";
    chkPostprocessNormalize.checked = config.postprocess_normalize_audio || false;
    chkGpuAccelerated.checked = config.gpu_accelerated_conversion || false;
    toggleGpuSection();
    
    // Speed limit
    const speed = config.speed_limit || 0;
    if (speed === 0) {
      selectSpeedLimit.value = "0";
      inputCustomSpeed.value = "";
    } else if ([1048576, 2097152, 5242880, 10485760].includes(speed)) {
      selectSpeedLimit.value = String(speed);
      inputCustomSpeed.value = "";
    } else {
      selectSpeedLimit.value = "custom";
      inputCustomSpeed.value = speed / 1024; // convert bytes back to KB
    }
    toggleCustomSpeedInput();

    // Custom yt-dlp args
    if (inputCustomYtdlpArgs) {
      inputCustomYtdlpArgs.value = config.custom_ytdlp_args || "";
    }
  }

  btnTestBackend.addEventListener("click", async () => {
    btnTestBackend.disabled = true;
    btnTestBackend.textContent = "Connecting...";
    try {
      await api.sync(true);
      await refreshPermissions();
      showStatus("Connected to VidGrab. Download defaults come from the app.", "success");
    } catch (error) { showStatus(error.message, "error"); }
    finally { btnTestBackend.disabled = false; btnTestBackend.textContent = "Test connection"; }
  });

  function integer(control, min, max, factor = 1) {
    const value = Number(control.value) * factor;
    if (!control.value || !Number.isSafeInteger(value) || value < min || value > max) {
      control.setCustomValidity("Enter a value within the supported range.");
      control.reportValidity();
      throw new Error("Check the highlighted numeric setting.");
    }
    control.setCustomValidity("");
    return value;
  }

  function patch() {
    const values = {
      download_dir: () => inputDownloadDir.value,
      filename_template: () => inputFilenameTemplate.value,
      max_concurrent: () => integer(inputMaxConcurrent, 1, 10),
      download_threads: () => integer(inputDownloadThreads, 1, 32),
      default_format: () => selectDefaultFormat.value,
      default_quality: () => selectDefaultQuality.value,
      sponsorblock_enabled: () => chkSponsorblockEnabled.checked,
      sponsorblock_mode: () => selectSponsorblockMode.value,
      sponsorblock_categories: () => Array.from(sbCategoryChecks).filter(checkbox => checkbox.checked).map(checkbox => checkbox.value),
      postprocess_codec: () => selectPostprocessCodec.value,
      postprocess_normalize_audio: () => chkPostprocessNormalize.checked,
      gpu_accelerated_conversion: () => chkGpuAccelerated.checked,
      custom_ytdlp_args: () => inputCustomYtdlpArgs.value,
      speed_limit: () => selectSpeedLimit.value === "custom"
        ? integer(inputCustomSpeed, 0, Number.MAX_SAFE_INTEGER, 1024)
        : integer(selectSpeedLimit, 0, Number.MAX_SAFE_INTEGER),
    };
    const changed = {};
    for (const key of touched) {
      const value = values[key]();
      if (!loaded || JSON.stringify(value) !== JSON.stringify(loaded.values[key])) changed[key] = value;
    }
    return changed;
  }

  btnSaveSettings.addEventListener("click", async () => {
    btnSaveSettings.disabled = true;
    btnSaveSettings.textContent = "Saving...";
    showStatus("Saving settings...", "");
    try {
      await new Promise((resolve, reject) => chrome.storage.local.set({
        cookieForwardingEnabled: chkCookieForwarding.checked,
        notificationPreference: selectNotifications.value,
      }, () => chrome.runtime.lastError ? reject(new Error("Browser preferences could not be saved.")) : resolve()));
      await refreshPermissions();
      const changes = patch();
      if (Object.keys(changes).length) {
        if (!loaded) throw new Error("Browser preferences saved. Connect VidGrab before changing app settings.");
        await api.updateConfig(changes, loaded.revision);
        const current = await api.sync(true);
        applySettings(current.snapshot.settings);
      }
      showStatus("Settings saved and synced successfully.", "success");
    } catch (error) {
      if (error.code === "REVISION_CONFLICT") showStatus("Settings changed in another window. Your edits are kept. Reload app settings before saving.", "error");
      else if (api.session.pending) {
        showStatus("The request may have succeeded. Check the request before saving again.", "error");
        btnCheckRequest.hidden = false;
      } else showStatus(error.message, "error");
    } finally { btnSaveSettings.disabled = false; btnSaveSettings.textContent = "Save Changes"; }
  });

  btnReloadSettings.addEventListener("click", async () => {
    try {
      const current = await api.sync(true);
      applySettings(current.snapshot.settings);
      showStatus("Current app settings loaded.", "success");
    } catch (error) { showStatus(error.message, "error"); }
  });
  btnCheckRequest.addEventListener("click", async () => {
    btnCheckRequest.disabled = true;
    try {
      await api.retryPending();
      applySettings((await api.sync(true)).snapshot.settings);
      btnCheckRequest.hidden = true;
      showStatus("Request checked. Current app settings loaded.", "success");
    } catch (error) { showStatus(error.message, "error"); }
    finally { btnCheckRequest.disabled = false; }
  });
  btnPresetClear.addEventListener("click", () => {
    inputCustomYtdlpArgs.value = "";
    touched.add("custom_ytdlp_args");
    showStatus("Save Changes to clear legacy executable arguments.", "");
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.cookieForwardingEnabled) chkCookieForwarding.checked = changes.cookieForwardingEnabled.newValue === true;
    if (changes.notificationPreference) selectNotifications.value = changes.notificationPreference.newValue || "both";
    if (changes.cookieForwardingEnabled || changes.notificationPreference) void refreshPermissions();
    if (changes.vidgrabPendingMutations) void refreshNotices();
  });
  window.addEventListener("pagehide", () => api.disconnect());

  // --- Helper to show status ---
  let statusTimeout = null;
  function showStatus(text, type = "") {
    statusMessage.textContent = text;
    statusMessage.className = "status-msg " + type;
    
    if (statusTimeout) clearTimeout(statusTimeout);
    
    if (type === "success") {
      statusTimeout = setTimeout(() => {
        statusMessage.textContent = "";
        statusMessage.className = "status-msg";
      }, 4000);
    }
  }

  // --- Initial Launch ---
  void loadSettings().finally(() => api.connect());
});
