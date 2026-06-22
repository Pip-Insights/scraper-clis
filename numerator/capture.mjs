#!/usr/bin/env node
// Reusable network-capture harness for reverse-engineering web portals.
//
// Launches a HEADED Chromium that YOU drive (log in, click around). It records:
//   - a full HAR  (captures/<label>.har)            — every request/response + bodies
//   - a grep-able JSONL (captures/<label>.net.jsonl) — one line per XHR/fetch/doc/ws,
//       with request headers + body and response headers + body (text/json capped)
//   - storageState (captures/<label>.state.json)    — cookies + localStorage, for
//       headless session-replay once the API is reversed
//
// Start CLEAN (no stored cookies) so the FULL login flow is captured — that's the
// part the headless CLI must replicate. The saved storageState is just a replay
// convenience afterward.
//
// Usage:
//   node capture.mjs <startUrl> [--label NAME] [--out DIR] [--state FILE]
//   node capture.mjs https://app.numerator.com --label numerator-login
//
// SECURITY: the capture WILL contain the login POST (your password in cleartext)
// and session tokens. Keep captures/ local; it is gitignored. Never commit/sync it.

import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

// ---- args ----
const argv = process.argv.slice(2);
let startUrl = "about:blank";
let label = null;
let outDir = path.join(path.dirname(new URL(import.meta.url).pathname), "captures");
let stateIn = null;
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--label") label = argv[++i];
  else if (a === "--out") outDir = argv[++i];
  else if (a === "--state") stateIn = argv[++i];
  else if (!a.startsWith("--")) startUrl = a;
}
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
label = (label || "capture") + "_" + stamp;
fs.mkdirSync(outDir, { recursive: true });
const harPath = path.join(outDir, `${label}.har`);
const netPath = path.join(outDir, `${label}.net.jsonl`);
const statePath = path.join(outDir, `${label}.state.json`);
const stopPath = path.join(outDir, `${label}.STOP`);

// Resource types we log to the JSONL (HAR still records EVERYTHING).
const LOG_TYPES = new Set(["document", "xhr", "fetch", "websocket", "other", "eventsource"]);
const BODY_CAP = 2 * 1024 * 1024; // don't inline bodies bigger than 2MB into JSONL
const TEXTUAL = /(json|text|xml|javascript|x-www-form-urlencoded|csv|html|graphql)/i;

const netStream = fs.createWriteStream(netPath, { flags: "a" });
let lineCount = 0;
function logLine(obj) {
  netStream.write(JSON.stringify(obj) + "\n");
  lineCount++;
}

function headersToObj(arr) {
  const o = {};
  for (const { name, value } of arr || []) {
    const k = name.toLowerCase();
    o[k] = o[k] ? o[k] + ", " + value : value;
  }
  return o;
}

async function bodyOf(response, contentType, contentLength) {
  const len = parseInt(contentLength || "0", 10);
  if (len && len > BODY_CAP) return { body: null, note: `omitted (${len} bytes)` };
  if (contentType && !TEXTUAL.test(contentType)) return { body: null, note: `binary (${contentType})` };
  try {
    const buf = await response.body();
    if (buf.length > BODY_CAP) return { body: null, note: `omitted (${buf.length} bytes)` };
    return { body: buf.toString("utf8") };
  } catch (e) {
    return { body: null, note: `unread (${e.message})` };
  }
}

(async () => {
  const browser = await chromium.launch({
    headless: process.env.CAPTURE_HEADLESS === "1",
    args: ["--start-maximized"],
    // We own shutdown (sentinel-file driven) so a stray signal can't kill
    // Chromium before the HAR is flushed via context.close().
    handleSIGINT: false,
    handleSIGTERM: false,
    handleSIGHUP: false,
  });
  const context = await browser.newContext({
    viewport: null,
    storageState: stateIn && fs.existsSync(stateIn) ? stateIn : undefined,
    recordHar: { path: harPath, content: "embed", mode: "full" },
  });

  const wire = (page) => {
    page.on("websocket", (ws) => {
      logLine({ ts: Date.now(), type: "websocket", event: "open", url: ws.url() });
      ws.on("framesent", (f) =>
        logLine({ ts: Date.now(), type: "websocket", dir: "sent", url: ws.url(), payload: String(f.payload).slice(0, 8192) }));
      ws.on("framereceived", (f) =>
        logLine({ ts: Date.now(), type: "websocket", dir: "recv", url: ws.url(), payload: String(f.payload).slice(0, 8192) }));
    });
  };
  context.on("page", wire);

  context.on("response", async (response) => {
    const req = response.request();
    const type = req.resourceType();
    if (!LOG_TYPES.has(type)) return;
    const resHeaders = headersToObj(await response.headersArray().catch(() => []));
    const { body, note } = await bodyOf(response, resHeaders["content-type"], resHeaders["content-length"]);
    let reqBody = null;
    try { reqBody = req.postData(); } catch {}
    logLine({
      ts: Date.now(),
      type,
      method: req.method(),
      url: req.url(),
      status: response.status(),
      reqHeaders: headersToObj(await req.headersArray().catch(() => [])),
      reqBody,
      resHeaders,
      resBody: body,
      resBodyNote: note,
    });
  });

  context.on("requestfailed", (req) => {
    const type = req.resourceType();
    if (!LOG_TYPES.has(type)) return;
    logLine({ ts: Date.now(), type: "requestfailed", method: req.method(), url: req.url(), error: req.failure()?.errorText });
  });

  const page = context.pages()[0] || (await context.newPage());
  wire(page);

  // Periodic storageState autosave (survives a hard window close).
  const saveState = async () => {
    try { await context.storageState({ path: statePath }); }
    catch (e) { console.error("storageState failed:", e.message); }
  };
  const ticker = setInterval(saveState, 15000);

  // Graceful stop — context still alive, so the HAR flushes cleanly.
  let closing = false;
  const gracefulClose = async (why) => {
    if (closing) return; closing = true;
    clearInterval(ticker);
    clearInterval(stopWatch);
    console.log(`\nstopping (${why})…`);
    await saveState();
    try { await context.close(); } catch (e) { console.error("context.close:", e.message); } // flush HAR
    try { await browser.close(); } catch {}
    await new Promise((r) => netStream.end(r));
    const har = fs.existsSync(harPath) ? `${(fs.statSync(harPath).size / 1024).toFixed(0)} KB` : "MISSING";
    console.log(`\n✓ capture saved\n  HAR:   ${harPath} (${har})\n  NET:   ${netPath} (${lineCount} entries)\n  STATE: ${statePath}`);
    process.exit(0);
  };

  // PRIMARY stop: a sentinel file (the driver `touch`es it when you're done) —
  // no terminal/signal interaction needed, and the browser is still alive so
  // the HAR is complete.
  const stopWatch = setInterval(() => {
    if (fs.existsSync(stopPath)) { try { fs.unlinkSync(stopPath); } catch {} gracefulClose("stop file"); }
  }, 1000);

  // Best-effort fallbacks. If the window is closed abruptly the HAR may be
  // partial, but the live JSONL is always complete.
  browser.on("disconnected", () => gracefulClose("browser closed"));
  process.on("SIGINT", () => gracefulClose("SIGINT"));
  process.on("SIGTERM", () => gracefulClose("SIGTERM"));

  if (startUrl !== "about:blank") {
    await page.goto(startUrl, { waitUntil: "domcontentloaded" }).catch((e) => console.log("goto:", e.message));
  }

  console.log("──────────────────────────────────────────────────────────────");
  console.log(" CAPTURE LIVE.  Drive the browser: log in, then do the actions");
  console.log(" you want Pip to be able to do (browse, build/run a report,");
  console.log(" export). Every XHR/fetch is being recorded.");
  console.log("");
  console.log(`  start url : ${startUrl}`);
  console.log(`  writing   : ${netPath}`);
  console.log("");
  console.log(` stop: touch "${stopPath}"  (the driver does this on "done")`);
  console.log("──────────────────────────────────────────────────────────────");
})().catch((e) => { console.error("capture harness error:", e); process.exit(1); });
