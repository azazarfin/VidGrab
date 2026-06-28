/**
 * VidGrab Backend API Client
 * Handles HTTP REST calls and WebSocket progress streaming.
 */
class VidGrabAPI extends EventTarget {
  constructor() {
    super();
    this.baseUrl = "http://127.0.0.1:8000";
    this.wsUrl = "ws://127.0.0.1:8000/ws/progress";
    this.ws = null;
    this.reconnectTimeout = null;
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 30000;
    this.isConnected = false;
    
    // Load saved API URL if configured
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
      chrome.storage.local.get(["backendUrl"], (result) => {
        if (result.backendUrl) {
          this.setBackendUrl(result.backendUrl);
        }
      });
      
      // Listen for changes to backend URL
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.backendUrl) {
          this.setBackendUrl(changes.backendUrl.newValue);
        }
      });
    }
  }

  setBackendUrl(url) {
    // Strip trailing slash
    this.baseUrl = url.replace(/\/$/, "");
    // Convert http/https to ws/wss
    const wsProto = this.baseUrl.startsWith("https") ? "wss" : "ws";
    const host = this.baseUrl.replace(/^https?:\/\//, "");
    this.wsUrl = `${wsProto}://${host}/ws/progress`;
    
    // Reconnect if we have an active socket
    if (this.ws) {
      this.disconnectWebSocket();
      this.connectWebSocket();
    }
  }

  // --- HTTP REST Endpoints ---

  async _request(endpoint, method = "GET", body = null) {
    const url = `${this.baseUrl}${endpoint}`;
    const options = {
      method,
      headers: {
        "Content-Type": "application/json",
      },
    };
    if (body) {
      options.body = JSON.stringify(body);
    }
    
    try {
      const response = await fetch(url, options);
      if (!response.ok) {
        let errorDetail = response.statusText;
        try {
          const errJson = await response.json();
          errorDetail = errJson.detail || errorDetail;
        } catch (e) {}
        throw new Error(errorDetail);
      }
      return await response.json();
    } catch (error) {
      console.error(`API Request failed (${method} ${endpoint}):`, error);
      throw error;
    }
  }

  async getStatus() {
    return this._request("/status");
  }

  async probeUrl(url) {
    return this._request("/probe", "POST", { url });
  }

  async startDownload(url, formatId, title = null, thumbnailUrl = null, options = {}) {
    return this._request("/downloads", "POST", {
      url,
      format_id: formatId,
      title,
      thumbnail_url: thumbnailUrl,
      ...options
    });
  }

  async cancelDownload(taskId) {
    return this._request(`/downloads/${taskId}`, "DELETE");
  }

  async retryDownload(taskId) {
    return this._request(`/downloads/${taskId}/retry`, "POST");
  }

  async openFolder(taskId) {
    return this._request(`/downloads/${taskId}/open-folder`, "POST");
  }

  async listDownloads(status = null) {
    const query = status ? `?status=${status}` : "";
    return this._request(`/downloads${query}`);
  }

  async getHistory() {
    return this._request("/downloads/history");
  }

  async deleteHistoryItem(taskId) {
    return this._request(`/downloads/history/${taskId}`, "DELETE");
  }

  async clearHistory() {
    return this._request("/downloads/history", "DELETE");
  }

  async getConfig() {
    return this._request("/config");
  }

  async updateConfig(config) {
    return this._request("/config", "PATCH", config);
  }

  async updateYtdlp() {
    return this._request("/update-ytdlp", "POST");
  }

  // --- WebSocket Progress Streaming ---

  connectWebSocket() {
    if (this.ws) return;

    console.log(`Connecting to WebSocket: ${this.wsUrl}`);
    this.ws = new WebSocket(this.wsUrl);

    this.ws.onopen = () => {
      console.log("WebSocket connection established.");
      this.isConnected = true;
      this.reconnectDelay = 1000; // Reset delay
      this.dispatchEvent(new CustomEvent("connection_change", { detail: { connected: true } }));
    };

    this.ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        // Dispatch specific event types (e.g. task_progress, task_complete)
        if (message.event) {
          this.dispatchEvent(new CustomEvent(message.event, { detail: message.data }));
          this.dispatchEvent(new CustomEvent("any_event", { detail: message }));
        }
      } catch (e) {
        console.error("Failed to parse WebSocket message:", e);
      }
    };

    this.ws.onclose = (event) => {
      console.log(`WebSocket connection closed (code: ${event.code}).`);
      this.isConnected = false;
      this.ws = null;
      this.dispatchEvent(new CustomEvent("connection_change", { detail: { connected: false } }));
      this._scheduleReconnect();
    };

    this.ws.onerror = (error) => {
      console.error("WebSocket error:", error);
      // Let onclose handle the reconnection
    };
  }

  disconnectWebSocket() {
    if (this.ws) {
      this.ws.onclose = null; // Remove listener to avoid auto-reconnect
      this.ws.close();
      this.ws = null;
      this.isConnected = false;
      this.dispatchEvent(new CustomEvent("connection_change", { detail: { connected: false } }));
    }
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }
  }

  _scheduleReconnect() {
    if (this.reconnectTimeout) return;
    
    console.log(`Scheduling reconnect in ${this.reconnectDelay}ms`);
    this.reconnectTimeout = setTimeout(() => {
      this.reconnectTimeout = null;
      this.connectWebSocket();
      
      // Exponential backoff
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    }, this.reconnectDelay);
  }
}

// Export instance if using modules, or attach to global scope
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { VidGrabAPI };
} else {
  const globalScope = typeof window !== 'undefined' ? window : self;
  globalScope.vidGrabAPI = new VidGrabAPI();
}
