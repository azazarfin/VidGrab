/**
 * VidGrab Content Script — Video Detector
 * Scans for <video> elements and known URL patterns.
 * Full implementation arrives in Phase 3 (Step 3.8).
 */

(function () {
  "use strict";

  const KNOWN_VIDEO_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "twitter.com",
    "x.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "bilibili.com",
  ];

  let detectedVideoCount = 0;

  /**
   * Check if the current page URL matches a known video site.
   */
  function isKnownVideoPage() {
    const hostname = window.location.hostname.replace("www.", "");
    return KNOWN_VIDEO_DOMAINS.some((d) => hostname.includes(d));
  }

  /**
   * Scan the DOM for <video> elements.
   */
  function scanForVideos() {
    const videos = document.querySelectorAll("video");
    const newCount = videos.length + (isKnownVideoPage() ? 1 : 0);

    if (newCount !== detectedVideoCount) {
      detectedVideoCount = newCount;
      // Notify background script of detected video count
      try {
        chrome.runtime.sendMessage({
          type: "video_count",
          count: detectedVideoCount,
          url: window.location.href,
        });
      } catch (e) {
        // Extension context may be invalidated
      }
    }

    return videos;
  }

  // Initial scan
  scanForVideos();

  // Debounced scan to prevent excessive callback firing on DOM-heavy pages
  let scanTimeout = null;
  function debouncedScan() {
    if (scanTimeout) return;
    scanTimeout = setTimeout(() => {
      scanTimeout = null;
      // Also check for URL changes (SPA navigation support)
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        detectedVideoCount = 0;
      }
      scanForVideos();
    }, 150);
  }

  // Single combined MutationObserver for both video detection and URL changes
  let lastUrl = location.href;
  const observer = new MutationObserver(debouncedScan);

  observer.observe(document.body, {
    childList: true,
    subtree: true,
  });

  // Expose detected videos for the overlay script
  window.__vidgrab_getVideos = scanForVideos;
})();
