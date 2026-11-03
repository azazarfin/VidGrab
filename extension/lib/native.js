// P02 diagnostics transport. Download/settings UI migration is a P04 task.
(function (global) {
  const operations = new Set(["status", "activate", "fixture.download"]);

  global.vidGrabNative = {
    send(op, params = {}) {
      return new Promise((resolve, reject) => {
        if (!operations.has(op)) {
          reject(new Error("Unsupported companion operation"));
          return;
        }
        const keys = Object.keys(params);
        if ((op === "fixture.download" && (keys.length !== 1 || typeof params.url !== "string")) ||
            (op !== "fixture.download" && keys.length !== 0)) {
          reject(new Error("Invalid companion request"));
          return;
        }
        const message = { spike_version: 1, id: crypto.randomUUID(), op, params };
        chrome.runtime.sendNativeMessage("com.vidgrab.companion", message, response => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message || "The VidGrab companion is unavailable"));
            return;
          }
          if (!response || response.spike_version !== 1 || (response.id !== message.id && response.ok)) {
            reject(new Error("Incompatible companion response"));
            return;
          }
          if (!response.ok) {
            const error = new Error(response.error?.message || "The companion rejected the request");
            error.code = response.error?.code || "APP_UNAVAILABLE";
            reject(error);
            return;
          }
          resolve(response.data);
        });
      });
    },
  };
})(self);
