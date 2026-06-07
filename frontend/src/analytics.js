/**
 * Hearthkeeper - Event Tracking
 *
 * Sends UI events to the backend for persistent logging.
 * Events are fire-and-forget (non-blocking, no retry).
 * Also captures unhandled JS errors automatically.
 */

const EVENT_ENDPOINT = "/api/event";

// Batch events to avoid spamming the server on rapid interactions
let _queue = [];
let _flushTimer = null;
const FLUSH_INTERVAL = 2000; // flush every 2 seconds

function _flush() {
  if (_queue.length === 0) return;
  const batch = _queue.splice(0);
  // Send each event (backend expects single events, not batches)
  for (const evt of batch) {
    try {
      fetch(EVENT_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(evt),
        keepalive: true, // survives page unload
      }).catch(() => {}); // silent fail — never block the UI
    } catch {
      // ignore
    }
  }
}

function _scheduleFlush() {
  if (_flushTimer) return;
  _flushTimer = setTimeout(() => {
    _flushTimer = null;
    _flush();
  }, FLUSH_INTERVAL);
}

/**
 * Track a UI event.
 *
 * @param {string} eventName - e.g., "chat_open", "order_action", "feedback"
 * @param {object} payload - additional data
 */
export function trackEvent(eventName, payload = {}) {
  _queue.push({
    event: eventName,
    source: "frontend",
    detail: payload.detail || null,
    ...payload,
  });
  _scheduleFlush();
}

/**
 * Track an error from the frontend.
 */
export function trackError(error, context = "") {
  const detail = error?.message || String(error);
  const stack = error?.stack?.split("\n").slice(0, 3).join(" | ") || "";
  trackEvent("frontend_error", {
    detail: `${context}: ${detail}`,
    level: "error",
    stack,
  });
}

// ── Auto-capture unhandled errors ──
if (typeof window !== "undefined") {
  window.addEventListener("error", (e) => {
    trackError(e.error || e.message, "unhandled");
  });

  window.addEventListener("unhandledrejection", (e) => {
    trackError(e.reason, "unhandled_promise");
  });

  // Flush on page hide (user navigating away or closing tab)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") _flush();
  });
}
