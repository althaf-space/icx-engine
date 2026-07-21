// ICX session-capture harness (Playwright).
//
// Captures an authenticated browser session so the replay harness runs already logged in. Two
// artifacts are written:
//   <out>          - the Playwright storageState JSON (cookies + localStorage).
//   <out>.session  - a JSON snapshot of sessionStorage. Playwright storageState does NOT capture
//                    sessionStorage, yet many SPAs gate authenticated routes on it (e.g. a user
//                    object in sessionStorage). Without this companion, a restored session lands
//                    back on the login page. The replay harness re-injects it via addInitScript.
// NO credentials are ever typed into an agent chat - either the user logs in by hand in a real
// browser (capture), or the app credentials are passed straight to this process (inline).
//
// Modes:
//   capture: open a HEADED Chromium at the login URL. The user logs in manually. When the page
//            reaches --success-url (or the user closes the window) the session is saved.
//              node icx-auth.mjs --mode capture --url <loginUrl> --out <state.json> \
//                [--success-url <glob>] [--timeout <sec>]
//   inline:  drive the login form with credentials passed to THIS process (never via chat), then
//            save the session.
//              node icx-auth.mjs --mode inline --url <loginUrl> --out <state.json> \
//                --user <u> --pass <p> [--user-selector <s>] [--pass-selector <s>] \
//                [--submit-selector <s>] [--success-url <glob>] [--timeout <sec>] [--headless]
//
// Playwright + its Chromium are installed by ICX under ~/.icx/testing (runner-install manager).
// This file is a packaged ICX asset; it is not run in ICX's own Python test suite.

import { writeFileSync } from "node:fs";

function arg(name, def = "") {
  const i = process.argv.indexOf(name);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

function has(name) {
  return process.argv.indexOf(name) !== -1;
}

async function main() {
  const mode = arg("--mode", "capture");
  const url = arg("--url", "");
  const out = arg("--out", "");
  const successUrl = arg("--success-url", "");
  const timeoutMs = Math.max(1, parseInt(arg("--timeout", "300"), 10)) * 1000;

  if (!url || !out) {
    console.error("icx-auth: --url and --out are required");
    process.exit(2);
  }

  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch (e) {
    console.error(`icx-auth: playwright not available: ${e}`);
    process.exit(3);
  }

  // Inline runs headless by default; capture is always headed (the user needs to see it).
  const headless = mode === "inline" ? has("--headless") : false;
  const browser = await chromium.launch({ headless });
  const context = await browser.newContext();
  const page = await context.newPage();

  // sessionStorage is per-page and NOT part of storageState, so snapshot it on every navigation.
  // Keep the LAST good snapshot so we still have it even if the user closes the window before we
  // reach the save block (the capture-without-success-url path).
  let lastSession = "{}";
  const snapSession = async () => {
    try { lastSession = await page.evaluate(() => JSON.stringify(window.sessionStorage)); }
    catch (_) { /* page navigating/closed - keep the previous snapshot */ }
  };
  page.on("framenavigated", snapSession);

  let ok = false;
  try {
    await page.goto(url, { timeout: timeoutMs });

    if (mode === "inline") {
      const userSel = arg("--user-selector", "input[type=email], input[name=username], input[type=text]");
      const passSel = arg("--pass-selector", "input[type=password]");
      const submitSel = arg("--submit-selector", "button[type=submit], input[type=submit]");
      await page.locator(userSel).first().fill(arg("--user", ""));
      await page.locator(passSel).first().fill(arg("--pass", ""));
      await page.locator(submitSel).first().click();
      if (successUrl) {
        await page.waitForURL(successUrl, { timeout: timeoutMs });
      } else {
        await page.waitForLoadState("networkidle", { timeout: timeoutMs });
      }
      ok = true;
    } else {
      // capture: wait until the user finishes login. Success = reaching successUrl, or the user
      // closing the browser window (whichever comes first).
      if (successUrl) {
        try {
          await page.waitForURL(successUrl, { timeout: timeoutMs });
          ok = true;
        } catch (_) {
          ok = true; // fall through and still try to save whatever session exists
        }
      } else {
        await new Promise((resolve) => {
          const done = () => resolve();
          browser.on("disconnected", done);
          context.on("close", done);
          page.on("close", done);
          setTimeout(done, timeoutMs);
        });
        ok = true;
      }
    }

    // Save the session while the context is still alive.
    try {
      await context.storageState({ path: out });
      // Companion sessionStorage snapshot (best-effort, freshest available).
      await snapSession();
      try { writeFileSync(`${out}.session`, lastSession || "{}", "utf-8"); }
      catch (e) { console.error(`icx-auth: could not save sessionStorage companion: ${e}`); }
    } catch (e) {
      console.error(`icx-auth: could not save session: ${e}`);
      ok = false;
    }
  } catch (e) {
    console.error(`icx-auth: ${mode} failed: ${e}`);
    ok = false;
  } finally {
    try { await browser.close(); } catch (_) { /* ignore */ }
  }

  process.exit(ok ? 0 : 1);
}

main();
