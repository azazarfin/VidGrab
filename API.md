# VidGrab Legacy Backend API Specification

> **Note:** This document describes the **legacy ASGI HTTP/WebSocket adapter** (`backend.main:app`) retained for backward compatibility and migration. Current VidGrab desktop and browser extension clients communicate through the versioned native **v1 contract** defined in [`packages/protocol/`](packages/protocol/) and documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

VidGrab's legacy backend exposes a loopback-only FastAPI REST API and WebSocket stream.

---

## 📡 REST API Endpoints

### 1. Health Status
Check if the server is running, audit system components, and inspect the `yt-dlp` version status.

- **URL**: `/status`
- **Method**: `GET`
- **Response**: `200 OK`
  ```json
  {
    "status": "online",
    "yt_dlp_version": "2025.01.15",
    "yt_dlp_latest": "2025.02.01",
    "update_available": true,
    "ffmpeg_available": true
  }
  ```

---

### 2. Format Probe
Examine video links to extract details, sizes, resolutions, subtitle languages, and play-list index trees.

- **URL**: `/probe`
- **Method**: `POST`
- **Request Body**:
  ```json
  {
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
  }
  ```
- **Response**: `200 OK`
  ```json
  {
    "is_playlist": false,
    "title": "Never Gonna Give You Up",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "uploader": "Rick Astley",
    "duration": 212,
    "formats": [
      {
        "format_id": "137+140",
        "resolution": "1080p",
        "ext": "mp4",
        "filesize": 48293810
      }
    ],
    "subtitles": {
      "en": [{"ext": "vtt", "name": "English"}]
    }
  }
  ```

---

### 3. Enqueue Download
Add a task to the queue and spawn downloading.

- **URL**: `/downloads`
- **Method**: `POST`
- **Request Body**:
  ```json
  {
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "format_id": "137+140",
    "title": "Never Gonna Give You Up",
    "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "audio_only": false,
    "audio_format": null,
    "download_subtitles": false,
    "subtitle_langs": null,
    "subtitle_format": "srt",
    "embed_subtitles": false,
    "embed_thumbnail": false,
    "playlist_items": null,
    "is_playlist": false,
    "output_template": "%(title)s.%(ext)s",
    "cookies": [],
    "scheduled_at": null,
    "extra_args": null,
    "duration": 212
  }
  ```
- **Response**: `200 OK`
  ```json
  {
    "task_id": "7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e"
  }
  ```

---

### 4. Cancel Task
Immediately stops an active download and clears it from the concurrency queue.

- **URL**: `/downloads/{task_id}`
- **Method**: `DELETE`
- **Response**: `200 OK`
  ```json
  {
    "message": "Download task 7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e cancelled"
  }
  ```

---

### 5. Retry Task
Re-enqueues a failed download task.

- **URL**: `/downloads/{task_id}/retry`
- **Method**: `POST`
- **Response**: `200 OK`
  ```json
  {
    "task_id": "7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e"
  }
  ```

---

### 6. Open File Folder
Instructs the backend to trigger OS-level commands (`xdg-open`, `open`, or `os.startfile`) to reveal the folder containing the downloaded file.

- **URL**: `/downloads/{task_id}/open-folder`
- **Method**: `POST`
- **Response**: `200 OK`
  ```json
  {
    "success": true,
    "message": "Folder opened successfully"
  }
  ```

---

### 7. Configuration Synchronization
Fetch or update configuration variables persisted inside SQLite.

- **URL**: `/config`
- **Methods**: `GET`, `PATCH`
- **Payload Schema**:
  ```json
  {
    "download_dir": "/path/to/Downloads/VidGrab",
    "max_concurrent": 3,
    "default_format": "bestvideo+bestaudio/best",
    "default_quality": "1080",
    "filename_template": "%(title)s.%(ext)s",
    "speed_limit": 0,
    "auto_update_ytdlp": true,
    "sponsorblock_enabled": false,
    "sponsorblock_categories": ["sponsor"],
    "sponsorblock_mode": "remove",
    "postprocess_codec": "none",
    "postprocess_normalize": false,
    "custom_ytdlp_args": ""
  }
  ```

---

## 🔌 WebSocket progress Relays

Used by the extension service worker to stream real-time task state transitions.

- **URL**: `WS /ws/progress`

### Event Types

#### 1. Initial State (`init_state`)
Sent immediately upon connection opening. Relays current progress states of all active tasks.
```json
{
  "event": "init_state",
  "data": [
    {
      "id": "7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e",
      "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "title": "Never Gonna Give You Up",
      "status": "downloading",
      "progress": 42.5,
      "speed": 5839201,
      "eta": 4
    }
  ]
}
```

#### 2. Progress Hook Update (`task_progress`)
Dispatched periodically (~2 frames per second per task) while downloading.
```json
{
  "event": "task_progress",
  "data": {
    "id": "7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e",
    "progress": 48.7,
    "speed": 6291038,
    "eta": 3
  }
}
```

#### 3. Task Complete (`task_complete`)
Dispatched when video files are fully downloaded, transcoded, and output.
```json
{
  "event": "task_complete",
  "data": {
    "id": "7c9ebda2-613d-4c31-ba2c-29b7fb1a2d5e",
    "status": "complete",
    "file_path": "/path/to/Downloads/VidGrab/Never Gonna Give You Up.mp4",
    "file_size": 24829381
  }
}
```
