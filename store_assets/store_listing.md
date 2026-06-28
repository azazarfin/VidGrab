# 🛒 Store Submission Metadata & Preparation

This document contains the metadata, promotional assets list, description copy, privacy declarations, and reviewer instructions required to submit VidGrab to the Chrome Web Store and Firefox Add-ons (AMO).

---

## 🖼️ Store Graphics & Assets Checklist

- [x] **Store Promotional Banner (1400x560)**
  - File: `store_assets/vidgrab_promo_banner.png`
  - Style: Dark theme, glassmorphic glowing gradients, central VidGrab logomark.
- [ ] **Small Promotion Tile (440x280)**
  - Use cropped version of the main banner.
- [ ] **Extension Icons**
  - Location: `extension/icons/`
  - Sizes: `16x16px`, `32x32px`, `48x48px`, `128x128px`.

---

## 📝 Store Copy (Chrome Web Store & Firefox AMO)

### 1. Extension Name
`VidGrab — Premium Video Downloader & Queue Manager`

### 2. Short Summary
`Download videos, audios, playlists, and subtitles locally from popular video sharing websites with scheduled queues and SponsorBlock.`

### 3. Detailed Description
```
VidGrab is a high-fidelity, local-first video downloader extension for Google Chrome and Mozilla Firefox. Built with speed and customization in mind, VidGrab connects your browser seamlessly to a local FastAPI-driven background helper utilizing yt-dlp.

KEY FEATURES:
- 🚀 Instant Detection: Detects video players in real-time and places a smooth, hover-sensitive quick download button above them.
- 📋 Queue Manager: Schedule multiple downloads, track live download speeds, ETA, and progress bar percentages over WebSockets.
- 🎛️ Advanced Post-Processing: Convert video formats (MP4, MKV, WebM), extract audio streams (MP3, FLAC, Opus), and normalize sound levels with a single click.
- 🚫 SponsorBlock: Automatically skip or excise annoying sponsorships, intros, outros, or interactive alerts directly from the file stream.
- 🍪 Secure Cookie Jar: Forwards active session cookies to support downloading age-gated, private, or subscription-tier videos.
- 🗓️ Task Scheduler: Queue downloads to process overnight or during custom hours.
- 🛠️ Maintenance & CLI customization: Auto-update the yt-dlp binary on startup and inject custom command-line arguments directly.

HOW TO USE IT:
1. Make sure you run the VidGrab backend server on your computer (installer scripts provided in the package).
2. Browse to any video sharing website.
3. Click the VidGrab extension icon in the toolbar or hover over a video to click the floating download button.
4. Customize quality, subtitles, scheduler, or SponsorBlock, and hit "Start Download".
```

---

## 🔒 Privacy Policy & Data Handling

### 1. Permissions Justification (Required for Store Review)
- **`activeTab`**: Used to get the URL of the current page when the user opens the download popup or hovers over a video to inject content scripts.
- **`cookies` & host permissions**: Required to extract session cookies for the active video domain. This is essential to authenticate and download private/members-only streams that require logged-in sessions. Cookies are processed entirely in-memory and are never sent to external servers other than the user's localhost backend.
- **`notifications`**: Required to display desktop notifications for task completion or failure with folder opening triggers.
- **`storage`**: Local extension storage stores the custom backend connection URL and preferences.

### 2. Privacy Policy Statement
```
VidGrab is a local-first application. No user data, browse history, session cookies, or downloaded files are collected, tracked, or sent to external servers or third-party entities. All download requests are sent directly to a helper server running locally on the user's host machine (http://localhost:8000). Cookies forwarded for downloading restricted content remain strictly in volatile RAM memory during download execution and are destroyed immediately upon task termination.
```

---

## 🧑‍💻 Notes for Reviewers (Firefox AMO / Chrome Web Store)

When submitting the extension, reviewers may notice it requires a local connection. Provide the following guide:

> **IMPORTANT FOR REVIEWERS:**
> VidGrab is a client wrapper for a locally hosted Python helper service. To test or review this extension:
> 1. Run the local backend server included in the source package by installing dependencies (`pip install -r backend/requirements.txt`) and running `uvicorn backend.main:app`.
> 2. Ensure the local service is running on `http://127.0.0.1:8000`.
> 3. Go to the extension options page to confirm connection.
> 4. Enter a public video URL to test downloading, enqueuing, and progress relays.
