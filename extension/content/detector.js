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

  let lastVideosJson = "";

  /**
   * Scan the DOM for <video> elements.
   */
  function scanForVideos() {
    const videos = document.querySelectorAll("video");
    const videoDetails = [];
    const seenUrls = new Set();

    // Check if the page itself is a known video page
    if (isKnownVideoPage() && !seenUrls.has(window.location.href)) {
      seenUrls.add(window.location.href);
      videoDetails.push({
        url: window.location.href,
        title: document.title || "Video Page",
        src: window.location.href,
        type: "page"
      });
    }

    videos.forEach((vid, index) => {
      if (vid.offsetWidth > 150 && vid.offsetHeight > 100) {
        let src = vid.currentSrc || vid.src || "";
        if (src.startsWith("blob:")) {
          src = window.location.href;
        }
        if (src && !seenUrls.has(src)) {
          seenUrls.add(src);
          let videoTitle = document.title || "Embedded Video";
          if (videos.length > 1) {
            videoTitle += ` (Video ${index + 1})`;
          }
          videoDetails.push({
            url: src,
            title: videoTitle,
            src: src,
            type: "element"
          });
        }
      }
    });

    const videosJson = JSON.stringify(videoDetails);
    if (videosJson !== lastVideosJson) {
      lastVideosJson = videosJson;
      detectedVideoCount = videoDetails.length;
      try {
        chrome.runtime.sendMessage({
          type: "video_detected",
          videos: videoDetails
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
