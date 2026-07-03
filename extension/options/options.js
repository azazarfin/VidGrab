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
      
      navItems.forEach(nav => nav.classList.remove("active"));
      tabPanels.forEach(panel => panel.classList.remove("active"));
      
      item.classList.add("active");
      document.getElementById(tabId).classList.add("active");
      
      // Update header
      const meta = tabMetadata[tabId];
      if (meta) {
        tabTitle.textContent = meta.title;
        tabSubtitle.textContent = meta.subtitle;
      }
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

  // --- Load Settings ---
  async function loadSettings() {
    showStatus("Loading settings...", "");
    
    // 1. Load extension-specific settings from local storage
    let backendUrl = "http://localhost:8000";
    let cookieForwarding = false;
    let notificationPref = "both";
    
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
      const store = await new Promise(resolve => {
        chrome.storage.local.get(["backendUrl", "cookieForwardingEnabled", "notificationPreference"], resolve);
      });
      if (store.backendUrl) backendUrl = store.backendUrl;
      if (store.cookieForwardingEnabled !== undefined) cookieForwarding = store.cookieForwardingEnabled;
      if (store.notificationPreference) notificationPref = store.notificationPreference;
    } else {
      if (localStorage.getItem("backendUrl")) backendUrl = localStorage.getItem("backendUrl");
      if (localStorage.getItem("cookieForwardingEnabled") !== null) {
        cookieForwarding = localStorage.getItem("cookieForwardingEnabled") === "true";
      }
      if (localStorage.getItem("notificationPreference")) {
        notificationPref = localStorage.getItem("notificationPreference");
      }
    }
    
    inputBackendUrl.value = backendUrl;
    chkCookieForwarding.checked = cookieForwarding;
    if (selectNotifications) {
      selectNotifications.value = notificationPref;
    }
    api.setBackendUrl(backendUrl);
    
    // 2. Fetch config from backend
    try {
      const backendOnline = await checkBackendHealth(backendUrl);
      if (backendOnline) {
        const config = await api.getConfig();
        populateBackendSettings(config);
        
        try {
          const statusData = await api.getStatus();
          updateYtdlpVersionUI(statusData);
        } catch (e) {
          console.error("Failed to load yt-dlp status:", e);
        }
        
        showStatus("Settings loaded successfully.", "success");
      } else {
        showStatus("Could not connect to backend server. Some settings are disabled.", "error");
      }
    } catch (err) {
      console.error("Failed to load backend configurations:", err);
      showStatus("Error communicating with backend.", "error");
    }
  }

  function updateYtdlpVersionUI(statusData) {
    if (!statusData) {
      ytdlpVersionStatus.textContent = "Error fetching version info";
      return;
    }
    const current = statusData.yt_dlp_version || "unknown";
    const latest = statusData.yt_dlp_latest;
    const hasUpdate = statusData.update_available;
    if (latest && hasUpdate) {
      ytdlpVersionStatus.innerHTML = `${current} &rarr; <span style="color: var(--warning);">${latest} available</span>`;
    } else if (latest) {
      ytdlpVersionStatus.innerHTML = `${current} <span style="color: var(--success); font-size: 11px; margin-left: 5px;">(Up to date)</span>`;
    } else {
      ytdlpVersionStatus.textContent = current;
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
      chkAutoUpdateYtdlp.checked = config.auto_update_ytdlp || false;
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
      inputCustomSpeed.value = Math.round(speed / 1024); // convert bytes back to KB
    }
    toggleCustomSpeedInput();

    // Custom yt-dlp args
    if (inputCustomYtdlpArgs) {
      inputCustomYtdlpArgs.value = config.custom_ytdlp_args || "";
    }
  }

  // --- Backend Health Checker ---
  async function checkBackendHealth(url) {
    try {
      const targetUrl = url.replace(/\/$/, "");
      const res = await fetch(`${targetUrl}/status`, { method: "GET" });
      if (res.ok) {
        const statusData = await res.json();
        backendStatus.className = "status-indicator online";
        backendStatus.querySelector(".status-text").textContent = "Backend Online";
        
        const ffmpegWarningBanner = document.getElementById("ffmpeg-warning-banner");
        if (statusData.ffmpeg_available === false) {
          if (ffmpegWarningBanner) ffmpegWarningBanner.style.display = "block";
          selectPostprocessCodec.disabled = true;
          chkPostprocessNormalize.disabled = true;
          chkPostprocessNormalize.checked = false;
        } else {
          if (ffmpegWarningBanner) ffmpegWarningBanner.style.display = "none";
          selectPostprocessCodec.disabled = false;
          chkPostprocessNormalize.disabled = false;
        }
        
        return true;
      }
    } catch (e) {}
    
    backendStatus.className = "status-indicator offline";
    backendStatus.querySelector(".status-text").textContent = "Backend Offline";
    const ffmpegWarningBanner = document.getElementById("ffmpeg-warning-banner");
    if (ffmpegWarningBanner) ffmpegWarningBanner.style.display = "none";
    return false;
  }

  // --- Test Backend Button ---
  btnTestBackend.addEventListener("click", async () => {
    const url = inputBackendUrl.value.trim();
    if (!url) return;
    
    btnTestBackend.disabled = true;
    btnTestBackend.textContent = "Testing...";
    
    const isOnline = await checkBackendHealth(url);
    
    btnTestBackend.disabled = false;
    btnTestBackend.textContent = "Test";
    
    if (isOnline) {
      showStatus("Connection test successful! Backend is online.", "success");
      // Try reloading settings from the new URL
      api.setBackendUrl(url);
      try {
        const config = await api.getConfig();
        populateBackendSettings(config);
      } catch (e) {}
    } else {
      showStatus("Connection test failed. Verify URL is correct and backend is running.", "error");
    }
  });

  // --- Save Settings ---
  btnSaveSettings.addEventListener("click", async () => {
    btnSaveSettings.disabled = true;
    btnSaveSettings.textContent = "Saving...";
    showStatus("Saving settings...", "");
    
    const backendUrl = inputBackendUrl.value.trim();
    const cookieForwarding = chkCookieForwarding.checked;
    const notificationPref = selectNotifications ? selectNotifications.value : "both";
    
    // 1. Save extension-specific configuration in local storage
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
      await new Promise(resolve => {
        chrome.storage.local.set({
          backendUrl: backendUrl,
          cookieForwardingEnabled: cookieForwarding,
          notificationPreference: notificationPref
        }, resolve);
      });
    } else {
      localStorage.setItem("backendUrl", backendUrl);
      localStorage.setItem("cookieForwardingEnabled", cookieForwarding);
      localStorage.setItem("notificationPreference", notificationPref);
    }
    
    api.setBackendUrl(backendUrl);
    
    // Check if backend is online before updating config
    const isOnline = await checkBackendHealth(backendUrl);
    if (!isOnline) {
      showStatus("Saved locally, but backend is offline. Failed to sync backend config.", "error");
      btnSaveSettings.disabled = false;
      btnSaveSettings.textContent = "Save Changes";
      return;
    }
    
    // Calculate speed limit in bytes
    let speedLimitBytes = 0;
    if (selectSpeedLimit.value === "custom") {
      const customKb = parseInt(inputCustomSpeed.value) || 0;
      speedLimitBytes = customKb * 1024;
    } else {
      speedLimitBytes = parseInt(selectSpeedLimit.value) || 0;
    }
    
    // Gather SponsorBlock categories
    const sbCategories = [];
    sbCategoryChecks.forEach(chk => {
      if (chk.checked) sbCategories.push(chk.value);
    });
    
    // Build ConfigModel payload
    const payload = {
      download_dir: inputDownloadDir.value.trim(),
      max_concurrent: parseInt(inputMaxConcurrent.value) || 3,
      default_format: selectDefaultFormat.value.trim() || "bestvideo+bestaudio/best",
      default_quality: selectDefaultQuality.value,
      filename_template: inputFilenameTemplate.value.trim() || "%(title)s.%(ext)s",
      speed_limit: speedLimitBytes,
      auto_update_ytdlp: chkAutoUpdateYtdlp ? chkAutoUpdateYtdlp.checked : true,
      sponsorblock_enabled: chkSponsorblockEnabled.checked,
      sponsorblock_categories: sbCategories,
      sponsorblock_mode: selectSponsorblockMode.value,
      postprocess_codec: selectPostprocessCodec.value,
      postprocess_normalize_audio: chkPostprocessNormalize.checked,
      custom_ytdlp_args: inputCustomYtdlpArgs ? inputCustomYtdlpArgs.value.trim() : "",
      download_threads: parseInt(inputDownloadThreads.value) || 4,
      gpu_accelerated_conversion: chkGpuAccelerated.checked
    };
    
    try {
      await api.updateConfig(payload);
      showStatus("Settings saved and synced successfully.", "success");
    } catch (err) {
      console.error("Failed to update backend configuration:", err);
      showStatus(`Error syncing settings: ${err.message}`, "error");
    } finally {
      btnSaveSettings.disabled = false;
      btnSaveSettings.textContent = "Save Changes";
    }
  });

  if (btnUpdateYtdlp) {
    btnUpdateYtdlp.addEventListener("click", async () => {
      btnUpdateYtdlp.disabled = true;
      btnUpdateYtdlp.textContent = "Updating...";
      ytdlpVersionStatus.textContent = "Updating yt-dlp, please wait...";
      try {
        const res = await api.updateYtdlp();
        if (res && res.success) {
          showStatus(res.message || "yt-dlp updated successfully.", "success");
        } else {
          showStatus(res.message || "Failed to update yt-dlp.", "error");
        }
      } catch (e) {
        console.error("Failed to update yt-dlp:", e);
        showStatus("Error triggering update endpoint.", "error");
      } finally {
        btnUpdateYtdlp.disabled = false;
        btnUpdateYtdlp.textContent = "Update Now";
        // Re-fetch status
        try {
          const statusData = await api.getStatus();
          updateYtdlpVersionUI(statusData);
        } catch (e) {}
      }
    });
  }

  // Bind presets
  if (btnPresetRobust) {
    btnPresetRobust.addEventListener("click", () => {
      inputCustomYtdlpArgs.value = "--ignore-errors --no-mtime";
    });
  }
  if (btnPresetIpv4) {
    btnPresetIpv4.addEventListener("click", () => {
      inputCustomYtdlpArgs.value = "--force-ipv4";
    });
  }
  if (btnPresetBypass) {
    btnPresetBypass.addEventListener("click", () => {
      inputCustomYtdlpArgs.value = "--geo-bypass";
    });
  }
  if (btnPresetClear) {
    btnPresetClear.addEventListener("click", () => {
      inputCustomYtdlpArgs.value = "";
    });
  }

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
  loadSettings();
});
