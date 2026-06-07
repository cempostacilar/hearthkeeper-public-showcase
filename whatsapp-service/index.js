/**
 * Hearthkeeper - WhatsApp Sidecar Service
 *
 * Small Express.js server wrapping whatsapp-web.js.
 * Python backend communicates via HTTP; incoming messages forwarded via webhook.
 *
 * First run: scan QR code with dedicated WhatsApp phone.
 * Session persists in .wwebjs_auth/ — no re-scan needed for weeks.
 *
 * Endpoints:
 *   POST /send          — send a text message
 *   GET  /status        — connection health check
 *   GET  /qr            — latest QR code (base64) if not yet linked
 *   POST /restart       — exit the process (NSSM auto-restarts).
 *                          Auth via X-Webhook-Secret header.
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const express = require("express");
const fs = require("fs");
const path = require("path");

// Load shared config from the project-root .env (the same file the Python
// backend reads). Mirrors config.py's loader — no dotenv dependency.
function loadProjectEnv() {
  const envPath = path.join(__dirname, "..", ".env");
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, "utf-8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const eq = trimmed.indexOf("=");
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (value && !process.env[key]) process.env[key] = value;
  }
}
loadProjectEnv();

const WEBHOOK_URL =
  process.env.WEBHOOK_URL || "http://localhost:8000/webhook/whatsapp";
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || "";
const PORT = process.env.PORT || 3001;

if (!WEBHOOK_SECRET) {
  console.warn(
    "[WA] WARNING: WEBHOOK_SECRET not set — backend will reject all webhook posts."
  );
}

// ── WhatsApp Client ──

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: ".wwebjs_auth" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let latestQR = null;
let isConnected = false;
let connectionError = null;

client.on("qr", (qr) => {
  latestQR = qr;
  qrcode.generate(qr, { small: true });
  console.log("[WA] QR code generated — scan with your phone");
});

client.on("ready", () => {
  isConnected = true;
  latestQR = null;
  connectionError = null;
  console.log("[WA] WhatsApp client ready");
});

client.on("authenticated", () => {
  console.log("[WA] Authenticated successfully");
});

client.on("auth_failure", (msg) => {
  isConnected = false;
  connectionError = `Auth failure: ${msg}`;
  console.error("[WA] Auth failure:", msg);
});

client.on("disconnected", (reason) => {
  isConnected = false;
  connectionError = `Disconnected: ${reason}`;
  console.warn("[WA] Disconnected:", reason);
  // Attempt reconnect after a short delay
  setTimeout(() => {
    console.log("[WA] Attempting reconnect...");
    client.initialize().catch((err) => {
      console.error("[WA] Reconnect failed:", err.message);
    });
  }, 5000);
});

// Track message IDs sent by the bot so we don't forward them back to webhook
const sentByBot = new Set();

// Forward incoming messages to Python backend via webhook
// Uses "message_create" instead of "message" to also capture self-chat replies
// (needed when testing with same phone number for bot + member)
client.on("message_create", async (msg) => {
  // Skip group messages and broadcasts
  const chatId = msg.from || msg.to;
  if (chatId.endsWith("@g.us") || chatId.includes("broadcast")) return;

  // Skip messages the bot itself sent (avoid infinite loop)
  const msgId = msg.id._serialized;
  if (sentByBot.has(msgId)) {
    sentByBot.delete(msgId); // clean up
    return;
  }

  // For self-chat: msg.fromMe is true for both bot-sent AND phone-typed messages
  // We filter bot-sent via sentByBot set above, so remaining fromMe = phone-typed
  // For other chats: only forward incoming (not fromMe) messages
  if (msg.fromMe && msg.from !== msg.to) return;

  // Determine the sender — for self-chat, use msg.from (our own number)
  const sender = msg.fromMe ? msg.to : msg.from;

  const payload = {
    from: sender,
    body: msg.body,
    timestamp: msg.timestamp,
    type: msg.type, // "chat", "image", "sticker", etc.
    messageId: msgId,
  };

  console.log(`[WA] Incoming from ${sender}: ${(msg.body || "").slice(0, 80)}`);

  try {
    const resp = await fetch(WEBHOOK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Webhook-Secret": WEBHOOK_SECRET,
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      console.error(`[WA] Webhook returned ${resp.status}`);
    }
  } catch (err) {
    console.error("[WA] Webhook delivery failed:", err.message);
  }
});

// ── Express API ──

const app = express();
app.use(express.json());

// Send a message
app.post("/send", async (req, res) => {
  const { phone, message } = req.body;

  if (!phone || !message) {
    return res.status(400).json({ error: "phone and message required" });
  }

  if (!isConnected) {
    return res.status(503).json({ error: "WhatsApp not connected" });
  }

  try {
    // Convert E.164 to WhatsApp chat ID: "+971XXXXXXXXX" → "+971XXXXXXXXX@c.us"
    const chatId = phone.replace("+", "") + "@c.us";
    const sent = await client.sendMessage(chatId, message);
    const sentId = sent.id._serialized;
    sentByBot.add(sentId);
    // Auto-cleanup after 30s in case message_create never fires
    setTimeout(() => sentByBot.delete(sentId), 30000);
    console.log(`[WA] Sent to ${chatId}: ${message.slice(0, 80)}`);
    res.json({ success: true, messageId: sentId });
  } catch (err) {
    console.error("[WA] Send failed:", err.message);
    res.status(500).json({ error: err.message });
  }
});

// Health check
app.get("/status", (req, res) => {
  res.json({
    connected: isConnected,
    error: connectionError,
    hasQR: !!latestQR,
  });
});

// Restart endpoint — exits the process so NSSM can bring it back.
// The Python backend calls this from:
//   * scheduler._nightly_whatsapp_restart (3:30 AM preemptive)
//   * scheduler._whatsapp_liveness_probe  (after N consecutive
//     is_connected=false samples)
//   * POST /api/diag/restart-whatsapp     (manual trigger)
//
// Auth via X-Webhook-Secret matching WEBHOOK_SECRET. The secret is
// the same one inbound webhook posts use, so we don't add a second
// secret to manage. Without auth, any localhost process could bounce
// the sidecar in a loop.
app.post("/restart", (req, res) => {
  const provided = req.headers["x-webhook-secret"] || "";
  if (!WEBHOOK_SECRET) {
    console.warn("[WA] /restart called but WEBHOOK_SECRET is unset — refusing");
    return res
      .status(503)
      .json({ error: "restart unavailable (WEBHOOK_SECRET unset)" });
  }
  if (provided !== WEBHOOK_SECRET) {
    console.warn("[WA] /restart unauthorized — bad X-Webhook-Secret");
    return res.status(401).json({ error: "unauthorized" });
  }
  const reason = (req.body && req.body.reason) || "manual";
  console.log(`[WA] Restart requested (reason=${reason}) — exiting in 500ms`);
  // Respond first so the caller doesn't hang on a dropped connection.
  res.json({ ok: true, exitingInMs: 500 });
  // Give the response a beat to flush, then exit GRACEFULLY (destroy the client
  // so the Chromium child dies with us — otherwise the restart orphans it and
  // the next launch hits the SingletonLock crash-loop). NSSM AppExit=Restart
  // brings the process back up with the latest code, and clearStaleBrowser()
  // mops up if a prior exit wasn't clean.
  setTimeout(() => gracefulExit(0), 500);
});

// QR code for re-linking (used by frontend to show "scan QR" UI)
app.get("/qr", (req, res) => {
  if (latestQR) {
    res.json({ qr: latestQR });
  } else if (isConnected) {
    res.json({ qr: null, message: "Already connected" });
  } else {
    res.json({ qr: null, message: "Waiting for QR generation..." });
  }
});

// ── Self-heal: clear a stale browser lock before we (re)initialize ──
//
// whatsapp-web.js drives a headless Chromium whose user-data-dir is
// .wwebjs_auth/session. If a previous process exited WITHOUT destroying the
// client (a hard kill, an NSSM stop, a crash), the Chromium child is orphaned
// and leaves a SingletonLock behind. The next launch then fails with "browser
// is already running … use a different userDataDir", and the liveness probe
// restarts us straight back into the same wall — forever (this was the 25
// restarts/24h crash-loop). Clearing the lock + best-effort killing any orphaned
// Chromium bound to OUR dir (scoped by the path, so never the user's own
// browser) breaks that loop at the source.
function clearStaleBrowser() {
  const sessionDir = path.join(__dirname, ".wwebjs_auth", "session");
  try {
    if (process.platform === "win32") {
      const { execSync } = require("child_process");
      const ps =
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' " +
        "-and $_.CommandLine -like '*wwebjs_auth*' } | ForEach-Object { " +
        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }";
      execSync(`powershell -NoProfile -NonInteractive -Command "${ps}"`,
        { stdio: "ignore", timeout: 15000 });
    }
  } catch (e) {
    /* best effort — lock removal below is the important part */
  }
  for (const f of ["SingletonLock", "SingletonCookie", "SingletonSocket"]) {
    try {
      fs.rmSync(path.join(sessionDir, f), { force: true });
    } catch (e) {
      /* ignore */
    }
  }
}

// ── Graceful shutdown: destroy the client (which kills the Chromium child) so
// we never leave the orphan + lock that causes the crash-loop above. ──
let shuttingDown = false;
async function gracefulExit(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log("[WA] Graceful shutdown — destroying client…");
  try {
    await Promise.race([
      client.destroy(),
      new Promise((r) => setTimeout(r, 8000)),
    ]);
  } catch (e) {
    console.error("[WA] destroy on exit failed:", e.message);
  }
  process.exit(code);
}
process.on("SIGTERM", () => gracefulExit(0));
process.on("SIGINT", () => gracefulExit(0));

// ── Start ──

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[WA] HTTP API listening on 127.0.0.1:${PORT}`);
});

clearStaleBrowser();
console.log("[WA] Initializing WhatsApp client...");
client.initialize().catch((err) => {
  console.error("[WA] Init failed:", err.message);
  connectionError = `Init failed: ${err.message}`;
});
