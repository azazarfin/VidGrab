/**
 * VidGrab Content Script — Floating Download Overlay
 * Shows a premium download button on hover near detected <video> elements.
 * Clicking it saves the video URL to local storage and guides the user to open the extension popup.
 */

(function () {
  "use strict";

  let overlayBtn = null;
  let tooltipEl = null;
  let currentVideo = null;
  let hideTimeout = null;
  let tooltipTimeout = null;
  let resizeObserver = null;

  const DOWNLOAD_ICON = `
    <svg class="vidgrab-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
         stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/>
      <line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
  `;

  const CHECK_ICON = `
    <svg class="vidgrab-icon success" viewBox="0 0 24 24" fill="none" stroke="#00D2A0" stroke-width="3"
         stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  `;

  function createOverlay() {
    if (overlayBtn) return;

    overlayBtn = document.createElement("div");
    overlayBtn.id = "vidgrab-overlay-btn";
    
    setButtonState("idle");

    overlayBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      
      let url = window.location.href;
      if (currentVideo && currentVideo.currentSrc && currentVideo.currentSrc.startsWith('http')) {
        url = currentVideo.currentSrc;
      }
      
      // Save URL to storage
      chrome.storage.local.set({ pendingVideoUrl: url }, () => {
        // Visual feedback on the button
        setButtonState("success");
        showTooltip();

        // Optional: Attempt to programmatically open the popup (works on Chrome 127+)
        chrome.runtime.sendMessage({ type: "open_popup" });
        
        // Reset state after a delay
        setTimeout(() => {
          setButtonState("idle");
        }, 4000);
      });
    });

    overlayBtn.addEventListener("mouseenter", () => {
      clearTimeout(hideTimeout);
    });

    overlayBtn.addEventListener("mouseleave", () => {
      scheduleHide();
    });

    document.body.appendChild(overlayBtn);
  }

  function createTooltip() {
    if (tooltipEl) return;
    tooltipEl = document.createElement("div");
    tooltipEl.id = "vidgrab-tooltip";
    tooltipEl.innerHTML = "Video grabbed! Click the <strong>VidGrab</strong> extension icon in your toolbar to configure download.";
    document.body.appendChild(tooltipEl);
  }

  function showTooltip() {
    createTooltip();
    clearTimeout(tooltipTimeout);
    
    // Position it
    positionTooltip();
    tooltipEl.classList.add("visible");

    // Auto-hide after 5 seconds
    tooltipTimeout = setTimeout(() => {
      hideTooltip();
    }, 5000);
  }

  function hideTooltip() {
    if (tooltipEl) {
      tooltipEl.classList.remove("visible");
    }
  }

  function positionTooltip() {
    if (!tooltipEl || !overlayBtn) return;
    const btnRect = overlayBtn.getBoundingClientRect();
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const scrollLeft = window.scrollX || document.documentElement.scrollLeft;

    tooltipEl.style.top = `${btnRect.bottom + scrollTop + 8}px`;
    // Center it horizontally with respect to the button
    const leftPos = btnRect.left + scrollLeft + (overlayBtn.offsetWidth / 2) - (110); // 110 is half of max-width 220
    tooltipEl.style.left = `${Math.max(10, leftPos)}px`;
  }

  function setButtonState(state) {
    if (!overlayBtn) return;

    if (state === "idle") {
      overlayBtn.className = "";
      overlayBtn.innerHTML = `
        ${DOWNLOAD_ICON}
        <span class="vidgrab-overlay-label">Download</span>
      `;
    } else if (state === "success") {
      overlayBtn.className = "success";
      overlayBtn.innerHTML = `
        ${CHECK_ICON}
        <span class="vidgrab-overlay-label">Grabbed!</span>
      `;
    }
  }

  function positionOverlay(videoEl) {
    if (!overlayBtn || !videoEl) return;

    const rect = videoEl.getBoundingClientRect();
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const scrollLeft = window.scrollX || document.documentElement.scrollLeft;

    // Position in the top-right corner of the video player
    overlayBtn.style.top = `${rect.top + scrollTop + 12}px`;
    overlayBtn.style.left = `${rect.right + scrollLeft - 12 - overlayBtn.offsetWidth}px`;
    
    // If tooltip is visible, update its position too
    if (tooltipEl && tooltipEl.classList.contains("visible")) {
      positionTooltip();
    }
  }

  function showOverlay(videoEl) {
    clearTimeout(hideTimeout);
    createOverlay();
    currentVideo = videoEl;
    
    setupTracking(videoEl);
    positionOverlay(videoEl);
    overlayBtn.classList.add("visible");
  }

  function setupTracking(videoEl) {
    cleanupTracking();

    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => {
        positionOverlay(videoEl);
      });
      resizeObserver.observe(videoEl);
    }

    window.addEventListener("resize", handleWindowUpdate);
    window.addEventListener("scroll", handleWindowUpdate);
  }

  function cleanupTracking() {
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
    window.removeEventListener("resize", handleWindowUpdate);
    window.removeEventListener("scroll", handleWindowUpdate);
  }

  function handleWindowUpdate() {
    if (currentVideo) {
      positionOverlay(currentVideo);
    }
  }

  function scheduleHide() {
    hideTimeout = setTimeout(() => {
      if (overlayBtn && !overlayBtn.classList.contains("success")) {
        overlayBtn.classList.remove("visible");
        currentVideo = null;
        hideTooltip();
        cleanupTracking();
      }
    }, 600);
  }

  // Listen for mouse entering video elements
  document.addEventListener("mouseenter", (e) => {
    const videoEl = e.target.closest("video");
    // Ignore small thumbnails, ads, or tiny preview videos
    if (videoEl && videoEl.offsetWidth > 150 && videoEl.offsetHeight > 100) {
      showOverlay(videoEl);
    }
  }, true);

  document.addEventListener("mouseleave", (e) => {
    const videoEl = e.target.closest("video");
    if (videoEl && videoEl === currentVideo) {
      scheduleHide();
    }
  }, true);
})();
