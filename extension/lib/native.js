// Lifecycle compatibility framing carries the generated v1 control envelope.
(function (global) {
  const operations = new Set(["status", "activate", "fixture.download", "v1.request"]);

  global.vidGrabNative = {
    send(op, params = {}) {
      return new Promise((resolve, reject) => {
        if (!operations.has(op)) {
          reject(new Error("Unsupported companion operation"));
          return;
        }
        const keys = Object.keys(params);
        if ((op === "fixture.download" && (keys.length !== 1 || typeof params.url !== "string")) ||
            (op === "v1.request" && !global.vidGrabProtocol?.validateRequest(params)) ||
            (op !== "fixture.download" && op !== "v1.request" && keys.length !== 0)) {
          const error = new Error("Invalid companion request");
          error.code = "BAD_REQUEST";
          reject(error);
          return;
        }
        const message = { spike_version: 1, id: op === "v1.request" ? params.request_id : crypto.randomUUID(), op, params };
        chrome.runtime.sendNativeMessage("com.vidgrab.companion", message, response => {
          if (chrome.runtime.lastError) {
            const error = new Error("The VidGrab companion is unavailable");
            error.code = op === "v1.request" && global.vidGrabProtocol.isMutation(params.operation)
              ? "REQUEST_OUTCOME_UNKNOWN" : "APP_UNAVAILABLE";
            reject(error);
            return;
          }
          if (!response || response.spike_version !== 1 || (response.id !== message.id && response.ok)) {
            const error = new Error("Incompatible companion response");
            error.code = "APP_INCOMPATIBLE";
            reject(error);
            return;
          }
          if (!response.ok) {
            const error = new Error(response.error?.message || "The companion rejected the request");
            error.code = response.error?.code || "APP_UNAVAILABLE";
            if (op === "v1.request" && ["UNSUPPORTED_OPERATION", "INCOMPATIBLE_VERSION"].includes(error.code)) {
              error.code = "APP_INCOMPATIBLE";
              error.message = "Update VidGrab and the browser extension to compatible versions";
            }
            reject(error);
            return;
          }
          resolve(response.data);
        });
      });
    },
    request(message) {
      return this.send("v1.request", message);
    },
  };
  if (global.vidGrabProtocol) {
    global.vidGrabCompanion = global.vidGrabProtocol.createClient(message => global.vidGrabNative.request(message));
  }
})(self);
