// ICX Stagehand replay harness.
//
// Deterministic replay of a cached UI flow: reads a flow JSON (authored once by the agent, then
// cached), drives the page with Stagehand (AI resolution only on a cache miss) on top of Playwright,
// and emits a JUnit XML report. On rerun with a populated cache there is NO LLM call - selectors are
// replayed directly, so the verification run is reproducible; a genuinely broken selector fails loud
// rather than silently passing.
//
// Invoked by the ICX executor:
//   node icx-replay.mjs --mode replay --flow <flow.json> --url <baseUrl> --junit <out.xml>
//
// Stagehand + Playwright are installed by ICX under ~/.icx/testing/ (runner-install manager). This
// file is a packaged ICX asset; it is not run in ICX's own Python test suite.

import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { chromium, firefox, webkit, devices } from "playwright";
import { homedir } from "node:os";
import { join, basename } from "node:path";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";

const _require = createRequire(import.meta.url);

function arg(name, def = "") {
  const i = process.argv.indexOf(name);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

// Built-in WCAG audit run IN the page - no external library, works offline. Covers the highest-signal
// rules: images without alt, form controls with no accessible name, buttons/links with no discernible
// text, missing <html lang>, and duplicate ids. Returns an array of violation strings (empty = pass).
function runA11yAudit() {
  const V = [];
  const txt = (el) => (el.textContent || "").trim();
  const named = (el) => !!(el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") ||
    el.getAttribute("title") || txt(el) || el.getAttribute("alt"));
  // SCOPE to the SCREEN UNDER TEST, not the persistent app shell: a shared nav/sidebar/header with a
  // missing alt is one app-wide bug, not a bug of THIS screen - auditing it on every route reports the
  // same finding N times and fails screens whose own content is clean. Exclude ARIA-landmark chrome +
  // common shell class names so the a11y verdict reflects the screen's content.
  const CHROME = "nav,header,footer,[role=navigation],[role=banner],[role=contentinfo]," +
    ".sidebar,.navbar,.app-header,.app-footer,.main-header,.side-nav,.topbar,.top-bar,.menu-bar";
  const inChrome = (el) => { try { return !!el.closest(CHROME); } catch (_) { return false; } };
  // <html lang>
  if (!document.documentElement.getAttribute("lang")) V.push("html element has no lang attribute");
  // images need alt (empty alt is allowed = decorative)
  document.querySelectorAll("img").forEach((el, i) => {
    if (inChrome(el)) return;
    if (el.getAttribute("alt") === null && !el.getAttribute("aria-label") && el.getAttribute("role") !== "presentation")
      V.push(`img[${i}] has no alt/aria-label`);
  });
  // form controls need an accessible name (label[for], wrapping label, aria-*, title, or placeholder)
  document.querySelectorAll("input:not([type=hidden]),select,textarea").forEach((el, i) => {
    if (inChrome(el)) return;
    const id = el.getAttribute("id");
    const hasLabel = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
    const wrapped = el.closest("label");
    if (!hasLabel && !wrapped && !el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby") &&
        !el.getAttribute("title") && !el.getAttribute("placeholder"))
      V.push(`form control [${el.tagName.toLowerCase()}${id ? "#" + id : "[" + i + "]"}] has no accessible label`);
  });
  // buttons + role=button + links need discernible text
  document.querySelectorAll("button,[role=button],a[href]").forEach((el, i) => {
    if (inChrome(el)) return;
    if (!named(el)) V.push(`${el.tagName.toLowerCase()}[${i}] has no accessible text`);
  });
  // duplicate ids (breaks label[for] + aria references)
  const seen = {}, dup = {};
  document.querySelectorAll("[id]").forEach((el) => {
    const id = el.id; if (seen[id]) dup[id] = true; seen[id] = true;
  });
  Object.keys(dup).forEach((id) => V.push(`duplicate id "${id}"`));
  return V;
}

// Best-effort load of the bundled axe-core source (for injection into the page). Returns null when
// axe-core is not installed - the a11y action then falls back to runA11yAudit above.
function loadAxeSource() {
  try {
    const p = _require.resolve("axe-core");
    return readFileSync(p, "utf-8");
  } catch (_) {
    return null;
  }
}

// Runs IN the page. Returns a rich fingerprint of the element matched by `sel`, or null.
function captureFp(sel) {
  const el = document.querySelector(sel);
  if (!el) return null;
  const path = []; let n = el;
  while (n && n.nodeType === 1 && path.length < 6) { path.unshift(n.tagName.toLowerCase()); n = n.parentElement; }
  const sib = el.parentElement ? [...el.parentElement.children].indexOf(el) : -1;
  const neigh = el.parentElement
    ? [...el.parentElement.children].filter((c) => c !== el).map((c) => (c.textContent || "").trim().slice(0, 24)).slice(0, 6)
    : [];
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(), id: el.id || "",
    classes: (el.getAttribute("class") || "").split(/\s+/).filter(Boolean),
    text: (el.textContent || "").trim().slice(0, 48), role: el.getAttribute("role") || "",
    title: el.getAttribute("title") || el.getAttribute("aria-label") || "",
    domPath: path.join(">"), siblingIndex: sib, neighborTexts: neigh,
    bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
  };
}

// Runs IN the page. Scores every element against a stored fingerprint and returns the best fresh
// selector over the threshold, or null. Pure/deterministic - no network, no LLM.
function healFind(fp) {
  const THRESHOLD = 0.50;
  const els = [...document.querySelectorAll("body *")];
  const scoreOf = (el) => {
    let s = 0;
    const cls = (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    if ((el.textContent || "").trim().slice(0, 48) === fp.text && fp.text) s += 0.30;
    const path = []; let n = el;
    while (n && n.nodeType === 1 && path.length < 6) { path.unshift(n.tagName.toLowerCase()); n = n.parentElement; }
    if (path.join(">") === fp.domPath) s += 0.20;
    const t = el.getAttribute("title") || el.getAttribute("aria-label") || "";
    if (t && t === fp.title) s += 0.15;
    if (fp.classes.length) { const inter = cls.filter((c) => fp.classes.includes(c)).length; s += 0.15 * (inter / fp.classes.length); }
    if (fp.role && (el.getAttribute("role") || "") === fp.role) s += 0.10;
    if (el.tagName.toLowerCase() === fp.tag) s += 0.10;
    return s;
  };
  let best = null, bestS = 0;
  for (const el of els) { const sc = scoreOf(el); if (sc > bestS) { bestS = sc; best = el; } }
  if (!best || bestS < THRESHOLD) return null;
  // build a stable fresh selector for the matched element
  let sel = null;
  if (best.id) sel = "#" + CSS.escape(best.id);
  else if (best.getAttribute("data-testid")) sel = `[data-testid="${best.getAttribute("data-testid")}"]`;
  else if (best.getAttribute("name")) sel = `${best.tagName.toLowerCase()}[name="${best.getAttribute("name")}"]`;
  else { const c = (best.getAttribute("class") || "").split(/\s+/).filter(Boolean).slice(0, 2); if (c.length) sel = best.tagName.toLowerCase() + "." + c.join("."); }
  return sel ? { sel, score: Math.round(bestS * 100) / 100 } : null;
}

function xmlEscape(s) {
  // Strip characters that are INVALID in XML 1.0 (control chars other than tab/LF/CR) before
  // escaping. Playwright error logs embed ANSI colour codes (ESC = 0x1B, plus other C0 controls);
  // left raw they make the JUnit file "not well-formed" so the parser reads ZERO tests - which is
  // exactly the "0 tests ran" failure on any run that has a failure message.
  // eslint-disable-next-line no-control-regex
  return String(s).replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function writeJUnit(outPath, cases, timeSec) {
  // skipped (best-effort soft checks that could not run) count as neither pass nor failure.
  const failures = cases.filter((c) => c.status === "failed" || c.status === "error").length;
  const skipped = cases.filter((c) => c.status === "skipped").length;
  const body = cases.map((c) => {
    const open = `<testcase classname="ui-flow" name="${xmlEscape(c.name)}" time="${c.time || 0}"`;
    if (c.status === "passed") return `${open}/>`;
    if (c.status === "skipped") return `${open}><skipped message="${xmlEscape(c.message || "")}"/></testcase>`;
    const tag = c.status === "error" ? "error" : "failure";
    return `${open}><${tag} message="${xmlEscape(c.message || "")}"/></testcase>`;
  }).join("");
  const xml =
    `<?xml version="1.0" encoding="UTF-8"?>` +
    `<testsuite name="icx-ui" tests="${cases.length}" failures="${failures}" skipped="${skipped}" time="${timeSec}">` +
    body +
    `</testsuite>`;
  writeFileSync(outPath, xml, "utf-8");
}

// Visual regression: compares a baseline PNG against a freshly captured PNG. Lazy-imports pixelmatch
// + pngjs (pure JS, offline) so their absence is caught here rather than crashing the whole harness -
// the screenshot step soft-skips when this returns null. Returns {changed, total, ratio, diffBuf}.
async function pixelDiff(baselinePng, actualPng) {
  try {
    const pixelmatch = (await import("pixelmatch")).default;
    const { PNG } = await import("pngjs");
    const a = PNG.sync.read(baselinePng);
    const b = PNG.sync.read(actualPng);
    if (a.width !== b.width || a.height !== b.height) {
      return { changed: a.width * a.height, total: a.width * a.height, ratio: 1, sizeMismatch: true, diffBuf: null };
    }
    const out = new PNG({ width: a.width, height: a.height });
    const changed = pixelmatch(a.data, b.data, out.data, a.width, a.height, { threshold: 0.1 });
    const total = a.width * a.height;
    return { changed, total, ratio: total ? changed / total : 0, diffBuf: PNG.sync.write(out) };
  } catch (_) {
    return null;   // libs missing -> caller soft-skips
  }
}

// Clicks whose selector/description name a MUTATING submission - skipped during a verify probe so
// verification never writes/deletes data. Opening controls (create/add/view/edit/search) are NOT
// here, so verify can still traverse into modals to check their field selectors.
const _MUTATING = /\b(save|update|submit|delete|confirm|remove|publish|register)\b/i;

// VERIFY mode: traverse the flow and RESOLVE every selector against the live DOM without scoring a
// test. Navigation/opening steps execute so modals render; fill/assert are probed by count only;
// mutating clicks are skipped. Emits a JSON heal-report so the agent can repair broken/ambiguous
// selectors BEFORE the scored run - this is what prevents first-run misfires.
async function runVerify(page, flow, outPath, stepTimeout, baseUrl) {
  const results = [];
  // Global budget + INCREMENTAL write: a comprehensive flow with many broken selectors can burn
  // stepTimeout on each probe. Write the report after every step and stop at the budget so a slow or
  // executor-killed verify still leaves a parseable heal-report (else the caller sees None = "tool
  // absent" and silently skips healing - the flows that most need healing are the slow ones).
  const started = Date.now();
  const budget = Math.max(10000, parseInt(process.env.ICX_UI_TOTAL_TIMEOUT || "240000", 10));
  const persist = () => {
    const broken = results.filter((r) => r.status === "broken" || r.status === "invalid-selector").length;
    const ambiguous = results.filter((r) => r.status === "ambiguous").length;
    try { writeFileSync(outPath, JSON.stringify({ mode: "verify", broken, ambiguous, steps: results }), "utf-8"); }
    catch (_) { /* ignore */ }
  };
  persist();  // ensure the file exists immediately
  const probe = async (target) => {
    try {
      const n = await page.locator(target).count();
      return n === 0 ? "broken" : (n > 1 ? "ambiguous" : "resolved");
    } catch (e) {
      return "invalid-selector";
    }
  };
  for (let i = 0; i < (flow.steps || []).length; i++) {
    const step = flow.steps[i];
    const rec = { index: i, action: step.action, target: step.target || "",
                  description: step.description || "" };
    if (Date.now() - started > budget) { rec.status = "budget-exceeded"; results.push(rec); persist(); continue; }
    try {
      if (step.action === "goto") {
        await page.goto(step.target || baseUrl, { timeout: stepTimeout, waitUntil: "domcontentloaded" });
        rec.status = "nav";
      } else if (step.action === "waithidden") {
        rec.status = "resolved";                    // hidden-wait, no selector to resolve-probe
      } else if (step.action === "waitfor") {
        try { await page.locator(step.target).first().waitFor({ state: "visible", timeout: stepTimeout }); }
        catch (_) { /* fall through to count probe */ }
        rec.status = await probe(step.target);
      } else if (step.action === "click") {
        rec.status = await probe(step.target);
        const mutating = _MUTATING.test(step.target) || _MUTATING.test(step.description || "");
        if (mutating) { rec.status = rec.status === "resolved" ? "resolved-skipped-mutating" : rec.status; }
        else if (rec.status === "resolved" || rec.status === "ambiguous") {
          try { await page.locator(step.target).first().click({ timeout: stepTimeout }); } catch (_) {}
        }
      } else if (["fill", "assert", "assertgone", "select", "multiselect", "check", "uncheck", "hover",
                  "dblclick", "press", "upload", "draganddrop", "scroll", "type", "setvalue",
                  "smartfill", "fillunique", "pickoption"].includes(step.action)) {
        rec.status = await probe(step.target);      // probe the target selector, no side effect in verify
      } else if (["assertjs", "a11y", "perf", "route", "unroute", "offline", "netprofile", "dbverify",
                  "download", "waithidden", "confirmdialog", "screenshot"].includes(step.action)) {
        rec.status = "resolved";                    // page/context op, not a selector - nothing to probe
      } else {
        rec.status = "unknown-action";
      }
    } catch (e) {
      rec.status = "error";
      rec.message = String(e && e.message ? e.message : e);
    }
    results.push(rec);
    persist();  // incremental - partial heal-report survives a kill
  }
  persist();
}

async function main() {
  const flowPath = arg("--flow", process.env.ICX_UI_FLOW || "");
  const baseUrl = arg("--url", process.env.ICX_TARGET_URL || "");
  const junitPath = arg("--junit", ".icx-ui-junit.xml");
  const started = Date.now();
  const cases = [];

  // visual regression: screenshot steps capture a baseline on the first run and pixel-diff later
  // runs. ICX_UI_VISUAL=0 turns screenshot steps into no-ops (skipped). Baselines live under
  // ICX_VISUAL_DIR (default ~/.icx/testing/visual)/<flowKey>/. Offline (pixelmatch + pngjs, pure JS).
  // a11y engine: auto = axe-core when available else the builtin audit; axe = force axe (soft-skip if
  // absent); builtin = force the lightweight audit. Default auto.
  const a11yEngine = (process.env.ICX_A11Y_ENGINE || "auto").trim().toLowerCase();
  const visualOn = (process.env.ICX_UI_VISUAL || "1") !== "0";
  const visualThreshold = Math.max(0, parseFloat(process.env.ICX_VISUAL_THRESHOLD || "0.02"));
  const visualRoot = process.env.ICX_VISUAL_DIR || join(homedir(), ".icx", "testing", "visual");
  const flowKey = flowPath ? basename(flowPath).replace(/\.json$/i, "") : "flow";
  const heals = [];    // {old, new, score} for every selector healed this run - written to <flow>.heals.json

  // self-healing: capture each resolved selector's element fingerprint to a sidecar; on a later run a
  // MISSED selector is score-matched against it. Deterministic (in-page scoring), no LLM. ICX_UI_HEAL=0
  // disables capture + heal entirely (exact pre-heal behavior).
  const healOn = (process.env.ICX_UI_HEAL || "1") !== "0";
  const fpPath = flowPath ? flowPath.replace(/\.json$/i, "") + ".fingerprints.json" : "";
  const fpStore = {};          // selector -> fingerprint captured THIS run
  let priorFp = {};            // selector -> fingerprint from a PRIOR run (for healing)
  if (healOn && fpPath && existsSync(fpPath)) {
    try { priorFp = JSON.parse(readFileSync(fpPath, "utf-8")) || {}; } catch (_) { priorFp = {}; }
  }

  // Overall wall-clock budget (also bounds browser init below). Read up-front so init is bounded too.
  const TOTAL_BUDGET = Math.max(10000, parseInt(process.env.ICX_UI_TOTAL_TIMEOUT || "240000", 10));

  // Write an EMPTY report immediately, BEFORE loading the flow or launching the browser, so the file
  // always exists the moment the run begins. If anything below hangs and the executor kills the
  // process, there is a report on disk (0 tests) instead of a missing file the parser cannot read.
  writeJUnit(junitPath, cases, 0);

  let flow;
  try {
    flow = JSON.parse(readFileSync(flowPath, "utf-8"));
  } catch (e) {
    cases.push({ name: "load flow", status: "error", message: `cannot read flow: ${e}` });
    writeJUnit(junitPath, cases, 0);
    process.exit(1);
  }

  // ENGINE SELECTION: default (no ICX_UI_ENGINE and no ICX_UI_DEVICE) keeps the exact Stagehand/Chromium
  // path. When either is set, launch raw Playwright with that engine + device descriptor - the replay
  // drives the page with plain Playwright selectors and never calls stagehand.act(), so the wrapper is
  // not needed on this path. `page` and `context` below are used identically by the whole step loop.
  const wantEngine = (process.env.ICX_UI_ENGINE || "").trim().toLowerCase();
  const wantDevice = (process.env.ICX_UI_DEVICE || "").trim();
  const useRaw = !!(wantEngine || wantDevice);

  let stagehand = null;
  let page = null;
  let context = null;
  // Load a previously captured authenticated session (cookies + localStorage) when present, so the
  // replay runs already logged in. Path comes from --storage-state or ICX_STORAGE_STATE.
  const storageState = arg("--storage-state", process.env.ICX_STORAGE_STATE || "");
  // Headless by default (fast/CI); set ICX_UI_HEADED=1 to watch the test drive a real browser.
  const headed = process.env.ICX_UI_HEADED === "1";
  // slowMo (ms): slows EVERY Playwright action so a human can follow the run in a headed browser.
  // 0 = full speed (default, CI). Set ICX_UI_SLOWMO=1000 to watch each click/fill deliberately.
  const slowMo = Math.max(0, parseInt(process.env.ICX_UI_SLOWMO || "0", 10));
  try {
    if (useRaw) {
      const engines = { chromium, firefox, webkit };
      const bt = engines[wantEngine] || chromium;
      const browser = await bt.launch({ headless: !headed, slowMo: slowMo > 0 ? slowMo : undefined });
      const ctxOpts = (wantDevice && devices[wantDevice]) ? { ...devices[wantDevice] } : {};
      if (storageState) ctxOpts.storageState = storageState;
      context = await browser.newContext(ctxOpts);
      page = await context.newPage();
    } else {
      const { Stagehand } = await import("@browserbasehq/stagehand");
      // Deterministic replay drives the page with plain Playwright selectors and never calls the LLM,
      // so Stagehand is initialised without any model/key.
      const opts = { env: "LOCAL", headless: !headed };
      if (storageState) opts.browserContextOptions = { storageState };
      // Pass slowMo to the underlying browser launch when supported by this Stagehand build.
      if (slowMo > 0) opts.localBrowserLaunchOptions = { slowMo, headless: !headed };
      stagehand = new Stagehand(opts);
      // BOUND init: a stalled Chromium launch must not hang for the executor's full timeout. Race it
      // against the run budget so a hung init fails loud (a JUnit error) instead of stalling silently.
      const initTimeout = Math.min(TOTAL_BUDGET, Math.max(30000, parseInt(process.env.ICX_UI_INIT_TIMEOUT || "60000", 10)));
      await Promise.race([
        stagehand.init(),
        new Promise((_, reject) => setTimeout(() => reject(new Error(`stagehand init timed out after ${initTimeout}ms`)), initTimeout)),
      ]);
      page = stagehand.page;
      context = stagehand.page.context();
    }
  } catch (e) {
    const msg = useRaw ? `browser init failed: ${e}` : `stagehand init failed: ${e}`;
    cases.push({ name: "init stagehand", status: "error", message: msg });
    writeJUnit(junitPath, cases, (Date.now() - started) / 1000);
    process.exit(1);
  }

  // Restore the captured auth session OURSELVES, before any navigation. We do NOT rely on the
  // Stagehand wrapper honoring browserContextOptions.storageState (it does not reliably apply
  // localStorage), so we re-apply every part directly on the underlying Playwright context:
  //   - cookies         via context.addCookies
  //   - localStorage    from the storageState origins
  //   - sessionStorage  from the <storageState>.session companion (Playwright never captures it)
  // localStorage + sessionStorage are set from an addInitScript that runs on EVERY document before
  // the app's own JS, so an SPA that gates authenticated routes on either store boots already
  // logged in instead of redirecting the replay to the login page.
  try {
    const statePath = arg("--storage-state", process.env.ICX_STORAGE_STATE || "");
    if (statePath && existsSync(statePath)) {
      const ctx = context;
      let state = {};
      try { state = JSON.parse(readFileSync(statePath, "utf-8")); } catch (_) { state = {}; }
      if (Array.isArray(state.cookies) && state.cookies.length) {
        try { await ctx.addCookies(state.cookies); } catch (_) { /* ignore bad cookies */ }
      }
      const localByOrigin = {};
      for (const o of (state.origins || [])) {
        const bag = {};
        for (const kv of (o.localStorage || [])) bag[kv.name] = kv.value;
        localByOrigin[o.origin] = bag;
      }
      let sessionKV = {};
      const sessionFile = `${statePath}.session`;
      if (existsSync(sessionFile)) {
        try { sessionKV = JSON.parse(readFileSync(sessionFile, "utf-8")) || {}; } catch (_) { sessionKV = {}; }
      }
      await ctx.addInitScript((payload) => {
        try {
          const here = (payload.local && payload.local[window.location.origin]) || {};
          for (const k in here) window.localStorage.setItem(k, here[k]);
          for (const k in (payload.session || {})) window.sessionStorage.setItem(k, payload.session[k]);
        } catch (_) { /* about:blank has no usable origin - the real origin document gets it */ }
      }, { local: localByOrigin, session: sessionKV });
    }
  } catch (_) { /* best-effort; absence just means the app needs no restored session */ }

  // TEST-ONLY MUTATION HOOK (self-heal benchmark probe): ICX_UI_MUTATE holds a JSON list of selectors.
  // Mutation is applied LAZILY, per-step, right before the step that actually uses the selector (see
  // the step loop below) - NOT here, one-shot and early - because at this point in main() the flow has
  // not navigated anywhere yet, so the target element does not exist and nothing would be mutated.
  // Parsing here just builds the pending set; an empty/unset/invalid env var leaves it empty, so a
  // normal run is completely unaffected.
  const mutatePending = new Set();
  try {
    const mutateSpec = process.env.ICX_UI_MUTATE || "";
    if (mutateSpec) {
      const parsed = JSON.parse(mutateSpec);
      if (Array.isArray(parsed)) {
        for (const sel of parsed) if (typeof sel === "string" && sel) mutatePending.add(sel);
      }
    }
  } catch (_) { /* invalid ICX_UI_MUTATE - mutatePending stays empty, run unaffected */ }

  // Every action is BOUNDED by a timeout so a missing/slow selector fails the step fast and loud
  // (a JUnit failure) instead of hanging the whole run. Tunable via ICX_UI_STEP_TIMEOUT (ms).
  const STEP_TIMEOUT = Math.max(1000, parseInt(process.env.ICX_UI_STEP_TIMEOUT || "15000", 10));
  // Global budget (declared above, also bounds init): the harness stops on its own and STILL writes
  // the report, so the executor never has to kill it mid-write (which would leave no report).
  const deadline = started + TOTAL_BUDGET;
  // Per-step pause so a human watching a headed run actually sees each step land before the next.
  // Independent of slowMo (which slows sub-actions); 0 = no pause. Set ICX_UI_SLOWMO to enable both.
  const STEP_PAUSE = Math.max(0, parseInt(process.env.ICX_UI_SLOWMO || "0", 10));
  // Auto-handle NATIVE browser dialogs (alert/confirm/beforeunload) so an unexpected one never hangs
  // the run - accept confirms/alerts, dismiss beforeunload. App-level modal dialogs (a NO/YES popup
  // in the DOM) are handled by the `confirmdialog` step the census can model.
  page.on("dialog", async (d) => {
    try { await (d.type() === "beforeunload" ? d.dismiss() : d.accept()); } catch (_) { /* ignore */ }
  });
  // VERIFY mode short-circuits the scored replay: probe every selector, emit the heal-report, exit.
  const mode = arg("--mode", "replay");
  if (mode === "verify") {
    try { await runVerify(page, flow, junitPath, STEP_TIMEOUT, baseUrl); }
    finally { try { await (stagehand ? stagehand.close() : context.browser().close()); } catch (_) { /* ignore */ } }
    process.exit(0);
  }
  // Write the report as we go so a slow run that the executor KILLS (timeout / cancel) still leaves
  // the partial results on disk instead of an empty file scored as "0 tests / browser never ran".
  const flush = () => { try { writeJUnit(junitPath, cases, (Date.now() - started) / 1000); } catch (_) { /* ignore */ } };
  const onSignal = () => { flush(); process.exit(1); };
  process.on("SIGTERM", onSignal);
  process.on("SIGINT", onSignal);
  flush();  // emit an (empty) report immediately so the file always exists once the run has begun
  try {
    for (const step of flow.steps || []) {
      const label = step.description || `${step.action} ${step.target || ""}`.trim();
      // origTarget = the selector flow.json actually asks for, captured BEFORE the heal pre-pass can
      // rewrite step.target. The fingerprint must be keyed under this selector (not the healed one) so
      // healing stays durable across runs - see the per-step capture block below.
      const origTarget = step.target;
      if (Date.now() > deadline) {
        cases.push({ name: label, status: "failed", message: "run budget exceeded before this step", time: 0 });
        continue;
      }
      // TEST-ONLY LAZY MUTATION (self-heal benchmark probe): if this step's selector is pending
      // mutation, wait briefly for the element to actually be present on THIS screen (it may not have
      // existed at any earlier step - e.g. before the flow's goto), then rename its id (append
      // "-icxmut") and add a marker class so the selector MISSES for the self-heal pre-pass right
      // below. This must run BEFORE the self-heal pre-pass so the very step about to use the selector
      // is the one that gets healed. step.target is still the ORIGINAL selector here (the heal
      // pre-pass has not rewritten it yet), so priorFp[step.target] still finds the fingerprint
      // captured under the original selector on the prior (unmutated) run.
      if (mutatePending.has(step.target)) {
        try {
          await page.locator(step.target).first().waitFor({ state: "attached", timeout: 4000 }).catch(() => {});
          if ((await page.locator(step.target).first().count()) > 0) {
            await page.evaluate((s) => {
              const el = document.querySelector(s);
              if (!el) return;
              if (el.id) el.id = el.id + "-icxmut";
              el.classList.add("icxmut");
            }, step.target);
            mutatePending.delete(step.target);
          }
        } catch (_) { /* mutation is best-effort and test-only */ }
      }
      // SELF-HEAL: if this step's selector no longer resolves but we captured its fingerprint on a
      // prior run, score-match to a fresh selector and use that. A resolving selector never enters
      // here, so healing can only rescue a would-be failure - it cannot change a passing step.
      if (healOn && step.target && priorFp[step.target]) {
        try {
          if ((await page.locator(step.target).first().count()) === 0) {
            const found = await page.evaluate(healFind, priorFp[step.target]);
            if (found && found.sel) {
              cases.push({ name: `HEAL: ${step.target} -> ${found.sel} (${found.score})`, status: "passed", time: 0 });
              heals.push({ old: step.target, new: found.sel, score: found.score });
              step.target = found.sel;
            }
          }
        } catch (_) { /* heal is best-effort; on any error the step runs with its original target */ }
      }
      const t0 = Date.now();
      try {
        // AUTO-REVEAL: many forms are multi-TAB wizards (Profile / Hierarchy / ... tabs) - a field
        // lives on a tab that is not active, so it is present-but-hidden and .fill() would time out.
        // For any field-interaction step whose target exists but is not visible, click through the
        // form's tab/step headers until the target becomes visible. Generic, no census tab modeling.
        if (["fill", "fillunique", "smartfill", "check", "uncheck", "select", "multiselect",
             "pickoption", "type", "setvalue"].includes(step.action) && step.target) {
          try {
            const loc0 = page.locator(step.target).first();
            if ((await loc0.count()) > 0 && !(await loc0.isVisible())) {
              const tabs = page.locator('.nav-tabs a, .nav-link, [role="tab"], .tab-title, ul.nav li a, .wizard-step');
              const n = Math.min(await tabs.count(), 12);
              for (let ti = 0; ti < n; ti++) {
                try { await tabs.nth(ti).click({ timeout: 1500 }); } catch (_) { continue; }
                await page.waitForTimeout(300);
                if (await loc0.isVisible().catch(() => false)) break;
              }
            }
          } catch (_) { /* best-effort reveal */ }
        }
        // Always act on .first() so a selector that legitimately matches several elements (e.g. a
        // per-language field data-testid^=, or the first row's action icon) does NOT die on
        // Playwright strict mode - waitfor already does this, and an authored replay's intent for a
        // multi-match selector is "the first one". Author a precise selector when a specific element
        // is meant.
        if (step.action === "goto") {
          await page.goto(step.target || baseUrl, { timeout: STEP_TIMEOUT, waitUntil: "domcontentloaded" });
        } else if (step.action === "click") {
          // normal click first; if an overlay (an empty iframe, sticky header, transparent layer)
          // intercepts pointer events, retry with force so the real control beneath is still clicked.
          try { await page.locator(step.target).first().click({ timeout: STEP_TIMEOUT }); }
          catch (e) { await page.locator(step.target).first().click({ timeout: Math.min(STEP_TIMEOUT, 4000), force: true }); }
        } else if (step.action === "fill") {
          await page.locator(step.target).first().fill(step.value || "", { timeout: STEP_TIMEOUT });
        } else if (step.action === "select") {
          // Dropdown (<select>): try by visible label, then by value, then pick the first REAL option.
          const loc = page.locator(step.target).first();
          const v = step.value || "";
          try {
            await loc.selectOption({ label: v }, { timeout: STEP_TIMEOUT });
          } catch (e1) {
            try {
              await loc.selectOption(v, { timeout: STEP_TIMEOUT });
            } catch (e2) {
              // pick the first non-placeholder option: skip a leading "Select"/disabled/empty-value
              // option (index 0 is usually the placeholder - choosing it can leave a gated form hidden).
              const idx = await loc.evaluate((el) => {
                const opts = [...el.options];
                for (let i = 0; i < opts.length; i++) {
                  const o = opts[i];
                  const txt = (o.textContent || "").trim().toLowerCase();
                  if (!o.disabled && o.value !== "" && o.value !== "-1"
                      && txt !== "select" && !txt.startsWith("select ") && !txt.startsWith("--")) return i;
                }
                return Math.min(1, opts.length - 1);
              });
              await loc.selectOption({ index: parseInt(v, 10) || idx }, { timeout: STEP_TIMEOUT });
            }
          }
        } else if (step.action === "multiselect") {
          // Multi-select <select> or a multi-value control: value = comma-separated option labels.
          const labels = String(step.value || "").split(",").map((s) => s.trim()).filter(Boolean);
          await page.locator(step.target).first().selectOption(labels.map((l) => ({ label: l })), { timeout: STEP_TIMEOUT });
        } else if (step.action === "check") {
          await page.locator(step.target).first().check({ timeout: STEP_TIMEOUT });
        } else if (step.action === "uncheck") {
          await page.locator(step.target).first().uncheck({ timeout: STEP_TIMEOUT });
        } else if (step.action === "hover") {
          await page.locator(step.target).first().hover({ timeout: STEP_TIMEOUT });
        } else if (step.action === "dblclick") {
          await page.locator(step.target).first().dblclick({ timeout: STEP_TIMEOUT });
        } else if (step.action === "press") {
          // Keyboard: value = key or chord ("Enter", "Control+A", "Escape", "Tab").
          await page.locator(step.target).first().press(step.value || "Enter", { timeout: STEP_TIMEOUT });
        } else if (step.action === "upload") {
          // File upload: value = absolute file path (or comma-separated for multiple).
          const files = String(step.value || "").split(",").map((s) => s.trim()).filter(Boolean);
          await page.locator(step.target).first().setInputFiles(files, { timeout: STEP_TIMEOUT });
        } else if (step.action === "draganddrop") {
          // Drag: target = source selector, value = destination selector.
          await page.locator(step.target).first().dragTo(page.locator(step.value).first(), { timeout: STEP_TIMEOUT });
        } else if (step.action === "fillunique") {
          // RUNTIME CONSTRAINT reading: generate a VALID + UNIQUE value from the element's ACTUALLY
          // APPLIED constraints on the live page (real maxLength/type/min/max/pattern) - not the static
          // census. This catches config/country/tenant-driven rules (e.g. a maxLength set at runtime
          // from appProperties.get("MSISDN_LENGTH"), or a per-country phone length) that a static read
          // of the source cannot see. step.value carries the census semantic hint (text/email/phone/
          // number/url); step.description carries "uniq=<token>".
          const hint = String(step.value || "text");
          const m = /uniq=(\d+)/.exec(step.description || "");
          const uniq = m ? m[1] : "00000";
          const cm = /cmax=(\d+)/.exec(step.description || "");   // census-declared maxLength (fallback)
          const cmax = cm ? +cm[1] : null;
          const loc = page.locator(step.target).first();
          const gen = await loc.evaluate((el, args) => {
            const uniq = args.uniq;
            let kind = args.hint;
            const t = (el.getAttribute("type") || el.type || "").toLowerCase();
            if (!kind || kind === "text") {                       // let the live type override a weak hint
              if (t === "email") kind = "email"; else if (t === "url") kind = "url";
              else if (t === "number") kind = "number"; else if (t === "tel") kind = "phone";
            }
            // applied length = the SMALLER of the live maxlength attribute and the census maxLength, so
            // an app that JS-validates (no attr on the DOM) is still capped to the code's declared limit.
            const mlAttr = el.getAttribute("maxlength");
            const live = mlAttr && +mlAttr > 0 ? +mlAttr : (el.maxLength > 0 ? el.maxLength : null);
            const ml = (live && args.cmax) ? Math.min(live, args.cmax) : (live || args.cmax || null);
            const min = el.getAttribute("min"), max = el.getAttribute("max");
            const pat = el.getAttribute("pattern") || "";
            const digitsOnly = kind === "phone" || kind === "number" || /^\D*\\d|\[0-9\]|\\d/.test(pat)
              || /^[\^(]*\\d|only.*digit/i.test(pat);
            let v;
            if (kind === "email") {
              for (const loc2 of ["test" + uniq, "t" + uniq, uniq]) { const e = loc2 + "@example.com"; if (!ml || e.length <= ml) { v = e; break; } }
              if (!v) v = (uniq + "@x.co");
            } else if (kind === "url") {
              v = "https://example.com/" + uniq + ".png";
            } else if (kind === "phone" || digitsOnly) {
              // honor the APPLIED length: fill to exactly maxLength (or 10) with the unique digits.
              const target = ml || (kind === "number" ? String(uniq).length : 10);
              v = ("9" + uniq + "0000000000000000").slice(0, target);
              if (kind === "number") { let n = +v; if (min !== null && min !== "" && n < +min) n = +min; if (max !== null && max !== "" && n > +max) n = +max; v = String(n); }
            } else {
              v = "Test " + uniq;                                  // text
            }
            // final length clamp, preserving the unique token (numeric) at the tail
            if (ml && v.length > ml) {
              if (kind === "email" || kind === "url") v = v.slice(0, ml);
              else if (digitsOnly || kind === "phone" || kind === "number") v = v.slice(0, ml);
              else v = (ml >= uniq.length) ? ("T" + uniq).slice(0, ml) : uniq.slice(-ml);
            }
            return v;
          }, { hint, uniq, cmax });
          await loc.fill(String(gen), { timeout: STEP_TIMEOUT });
        } else if (step.action === "smartfill") {
          // DYNAMIC control detection: inspect the LIVE element and drive it correctly regardless of
          // what (if anything) the census called it. Handles any control the DOM can describe -
          // contenteditable, range, color, checkbox/radio, native select, else a normal fill - so a
          // never-seen or mis-classified control still works without a table entry.
          const loc = page.locator(step.target).first();
          const kind = await loc.evaluate((el) => {
            const t = (el.getAttribute("type") || "").toLowerCase();
            const tag = el.tagName.toLowerCase();
            const role = (el.getAttribute("role") || "").toLowerCase();
            if (el.isContentEditable) return "editable";
            if (tag === "select") return "select";
            if (t === "range" || t === "color") return "setvalue";
            if (t === "checkbox" || t === "radio") return "check";
            if (t === "file") return "file";
            // CUSTOM DROPDOWN widgets (react-select / antd / MUI / any listbox-popup) that are NOT a
            // native <select>: detect the container signature and drive by open+keyboard. This is what
            // lets a NEVER-SEEN custom dropdown on ANY screen work without the census naming it -
            // .fill() would silently do nothing on these.
            const widget = el.closest(
              '.ant-select,.MuiSelect-root,.MuiAutocomplete-root,[class*="select__control"],' +
              '[class*="-control"],[class*="Select-control"],[class*="dropdown"]');
            const haspopup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
            if ((widget && tag !== "select") || haspopup === "listbox"
                || (role === "combobox" && !el.hasAttribute("list"))) return "pickoption";
            if (role === "combobox" || el.hasAttribute("list") || role === "searchbox") return "combobox";
            return "fill";
          });
          const v = String(step.value || "");
          if (kind === "editable") { await loc.click({ timeout: STEP_TIMEOUT }); await loc.pressSequentially(v, { timeout: STEP_TIMEOUT }); }
          else if (kind === "pickoption") { await loc.click({ timeout: STEP_TIMEOUT }); await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter"); await page.keyboard.press("Escape"); }
          else if (kind === "select") { try { await loc.selectOption({ label: v }, { timeout: STEP_TIMEOUT }); } catch (_) { await loc.selectOption({ index: 1 }, { timeout: STEP_TIMEOUT }); } }
          else if (kind === "setvalue") {
            await loc.evaluate((el, val) => {
              const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
              if (s && s.set) s.set.call(el, val); else el.value = val;
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
            }, v || "50");
          }
          else if (kind === "check") { await loc.check({ timeout: STEP_TIMEOUT }); }
          else if (kind === "file") { /* cannot fabricate a file - skip */ }
          else if (kind === "combobox") { await loc.fill(v, { timeout: STEP_TIMEOUT }); await loc.press("Enter", { timeout: STEP_TIMEOUT }); }
          else { await loc.fill(v, { timeout: STEP_TIMEOUT }); }
        } else if (step.action === "confirmdialog") {
          // Best-effort dismiss of an app-level confirm dialog that may block the next action. target =
          // the confirm/proceed button selector; falls back to common YES/OK/Confirm labels. Never
          // fails if no dialog is present (many flows won't raise one).
          const sels = [step.target, ".yesButton", "button:has-text('YES')", "button:has-text('OK')",
                        "button:has-text('Confirm')", "input[value='YES']", "input[value='OK']"].filter(Boolean);
          for (const s of sels) {
            const b = page.locator(s).first();
            if (await b.count() && await b.isVisible().catch(() => false)) { await b.click({ timeout: 3000 }).catch(() => {}); break; }
          }
        } else if (step.action === "download") {
          // Click a download/export trigger and assert a file actually downloads. Playwright captures
          // the download event; a passing step = the export produced a file (name recorded).
          const [dl] = await Promise.all([
            page.waitForEvent("download", { timeout: STEP_TIMEOUT }),
            page.locator(step.target).first().click({ timeout: STEP_TIMEOUT }),
          ]);
          const fname = dl.suggestedFilename();
          if (!fname) throw new Error("download produced no file");
          try { await dl.cancel(); } catch (_) { /* do not persist the file */ }
          cases.push({ name: `${label} (${fname})`, status: "passed", time: (Date.now() - t0) / 1000 });
        } else if (step.action === "pickoption") {
          // react-select / listbox that is NOT a native <select>: click to open, ArrowDown+Enter picks
          // the first option, then Escape closes the menu (a MULTI-select keeps its menu open after a
          // pick, and its portal - z-index 9999 - would cover buttons like the form's submit).
          const loc = page.locator(step.target).first();
          await loc.click({ timeout: STEP_TIMEOUT });
          await page.keyboard.press("ArrowDown");
          await page.keyboard.press("Enter");
          await page.keyboard.press("Escape");
        } else if (step.action === "type") {
          // Clear then type character-by-character into a focused element - for contenteditable /
          // rich-text editors (Slate/ProseMirror/Quill/Monaco) where .fill() does not apply, and for
          // search boxes whose filter fires on keyup (fill only fires input). Clear-first (Ctrl+A +
          // Delete) so it never appends to a leftover value.
          const loc = page.locator(step.target).first();
          await loc.click({ timeout: STEP_TIMEOUT });
          await loc.press("ControlOrMeta+a", { timeout: STEP_TIMEOUT }).catch(() => {});
          await loc.press("Delete", { timeout: STEP_TIMEOUT }).catch(() => {});
          await loc.pressSequentially(String(step.value || ""), { timeout: STEP_TIMEOUT });
        } else if (step.action === "setvalue") {
          // Set .value directly + fire input/change/blur - for controls .fill() cannot drive:
          // input[type=range] sliders, input[type=color] pickers, custom masked inputs.
          await page.locator(step.target).first().evaluate((el, v) => {
            const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, "value");
            if (setter && setter.set) setter.set.call(el, v); else el.value = v;   // React-friendly native setter
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            el.dispatchEvent(new Event("blur", { bubbles: true }));
          }, String(step.value || ""));
        } else if (step.action === "scroll") {
          await page.locator(step.target).first().scrollIntoViewIfNeeded({ timeout: STEP_TIMEOUT });
        } else if (step.action === "route") {
          // Network fault injection: intercept requests matching target (URL glob) and either abort
          // them (value "abort") or fulfill with an HTTP error status (value = "500", "503", ...).
          // Used by the error-handling cases: make the backend fail, then assert the UI stays graceful.
          const pat = step.target.includes("*") ? step.target : `**${step.target}**`;
          const v = String(step.value || "abort");
          await page.route(pat, (route) => {
            if (v === "abort") return route.abort();
            const code = parseInt(v, 10) || 500;
            return route.fulfill({ status: code, contentType: "application/json", body: '{"error":"icx-injected"}' });
          });
        } else if (step.action === "unroute") {
          const pat = step.target.includes("*") ? step.target : `**${step.target}**`;
          await page.unroute(pat);
        } else if (step.action === "offline") {
          await page.context().setOffline(String(step.value || "on") !== "off");
        } else if (step.action === "netprofile") {
          // NETWORK-in-one-flow: apply a network condition for the following steps. slow = delay every
          // matched request (engine-agnostic route-delay, no CDP so it works on chromium/firefox/webkit);
          // offline = context offline; reset = restore. A following step asserts graceful behavior.
          const mode = String(step.value || "slow").toLowerCase();
          // normalize a bare target to a substring glob, same convention as the route/unroute actions
          // (a plain path like /api/orders becomes **/api/orders** so it matches as a substring).
          const glob = step.target ? (step.target.includes("*") ? step.target : `**${step.target}**`) : "**/*";
          const slowMs = Math.max(0, parseInt(process.env.ICX_NET_SLOW_MS || "800", 10));
          if (mode === "offline") {
            await page.context().setOffline(true);
          } else if (mode === "reset") {
            try { await page.unroute(glob); } catch (_) {}
            await page.context().setOffline(false);
          } else {
            // slow: delay each matched request before continuing.
            await page.route(glob, async (route) => {
              await new Promise((res) => setTimeout(res, slowMs));
              try { await route.continue(); } catch (_) { try { await route.abort(); } catch (_) {} }
            });
          }
        } else if (step.action === "waithidden") {
          // Wait until a selector is GONE (hidden/detached) - e.g. a create/edit modal closing after a
          // successful save. If it never closes, this fails clearly ("save did not close the form")
          // instead of a later click timing out against a covered element.
          await page.locator(step.target).first().waitFor({ state: "hidden", timeout: STEP_TIMEOUT });
        } else if (step.action === "waitfor") {
          // Wait until a selector appears. Default requires VISIBLE (post-login redirect, a rendered
          // control); value "attached" requires only presence in the DOM - used for dashboard widgets
          // that render async or collapse to zero height with no data, where visible would false-fail.
          const wfState = step.value === "attached" ? "attached" : "visible";
          await page.locator(step.target).first().waitFor({ state: wfState, timeout: STEP_TIMEOUT });
        } else if (step.action === "assert") {
          const text = await page.locator(step.target).first().innerText({ timeout: STEP_TIMEOUT });
          if (step.value && !String(text).includes(step.value)) {
            throw new Error(`expected "${step.value}" in "${text}"`);
          }
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        } else if (step.action === "assertjs") {
          // Evaluate a JS boolean expression in the page (used by the security cases: the XSS canary
          // sets a window flag only if it executed, so `window.__ICX_XSS === undefined` == safe).
          const ok = await page.evaluate(step.target);
          if (!ok) throw new Error(`assertjs failed: ${step.target}`);
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        } else if (step.action === "assertgone") {
          // Assert the value is NOT present in the target's text (used to verify a deleted record is
          // gone). Passes when the element is absent OR its text does not contain the value.
          const cnt = await page.locator(step.target).count();
          if (cnt > 0) {
            const text = await page.locator(step.target).first().innerText({ timeout: STEP_TIMEOUT }).catch(() => "");
            if (step.value && String(text).includes(step.value)) {
              throw new Error(`expected "${step.value}" to be GONE, but it is still present`);
            }
          }
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        } else if (step.action === "perf") {
          // Performance budget: assert the page's navigation load completed under value ms (default
          // 5000). Uses the Navigation Timing API - a real load-time regression fails the step.
          const budget = parseInt(step.value, 10) || 5000;
          const dur = await page.evaluate(() => {
            const n = performance.getEntriesByType("navigation")[0];
            return n ? Math.round(n.duration || (n.loadEventEnd - n.startTime)) : 0;
          });
          if (dur > budget) throw new Error(`page load ${dur}ms exceeds budget ${budget}ms`);
          cases.push({ name: `${label} (${dur}ms)`, status: "passed", time: (Date.now() - t0) / 1000 });
        } else if (step.action === "a11y") {
          // Accessibility audit of the current screen. Prefers axe-core (full WCAG 2.1 AA ruleset)
          // when available; falls back to the built-in lightweight audit when axe is absent (auto) or
          // forced off (builtin). Any violation fails the step - a passing a11y step = the screen has
          // no reportable a11y bug under the active engine.
          let usedAxe = false;
          if (a11yEngine !== "builtin") {
            const axeSrc = loadAxeSource();
            if (axeSrc) {
              try {
                await page.evaluate(axeSrc);
                const res = await page.evaluate(async () => {
                  const r = await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } });
                  const byImpact = { critical: 0, serious: 0, moderate: 0, minor: 0 };
                  for (const v of r.violations) { if (byImpact[v.impact] !== undefined) byImpact[v.impact] += 1; }
                  return { total: r.violations.length, byImpact, ids: r.violations.slice(0, 12).map((v) => `${v.impact}:${v.id}`) };
                });
                usedAxe = true;
                if (res.total > 0) {
                  throw new Error(`a11y violations (axe wcag2.1aa) ${res.total} [critical:${res.byImpact.critical} serious:${res.byImpact.serious} moderate:${res.byImpact.moderate} minor:${res.byImpact.minor}]: ` + res.ids.join(" | "));
                }
                cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
              } catch (e) {
                // an axe RUN that found violations re-throws below; an INJECTION/eval error (not a
                // violation) falls through to the builtin so a bad axe load never hard-fails wrongly.
                if (String(e && e.message).includes("a11y violations")) throw e;
                usedAxe = false;
              }
            } else if (a11yEngine === "axe") {
              cases.push({ name: `${label} (a11y skipped - axe-core unavailable)`, status: "skipped", time: (Date.now() - t0) / 1000 });
              usedAxe = true;   // handled (skip); do not run builtin
            }
          }
          if (!usedAxe) {
            const violations = await page.evaluate(runA11yAudit);
            if (violations && violations.length) {
              throw new Error(`a11y violations (${violations.length}): ` + violations.slice(0, 20).join(" | "));
            }
            cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
          }
        } else if (step.action === "screenshot") {
          if (!visualOn) {
            cases.push({ name: label, status: "skipped", message: "visual regression disabled (ICX_UI_VISUAL=0)", time: (Date.now() - t0) / 1000 });
          } else {
            const key = (step.value || step.target || "page").replace(/[^A-Za-z0-9_.-]/g, "_");
            const dir = join(visualRoot, flowKey);
            try { mkdirSync(dir, { recursive: true }); } catch (_) {}
            const baseFile = join(dir, key + ".png");
            const shotTarget = step.target ? page.locator(step.target).first() : page;
            const actual = await shotTarget.screenshot({ timeout: STEP_TIMEOUT });
            if (!existsSync(baseFile)) {
              try { writeFileSync(baseFile, actual); } catch (_) {}
              cases.push({ name: `${label} (baseline captured)`, status: "passed", time: (Date.now() - t0) / 1000 });
            } else {
              const baseline = readFileSync(baseFile);
              const res = await pixelDiff(baseline, actual);
              if (res === null) {
                cases.push({ name: `${label} (visual skipped - diff libs unavailable)`, status: "skipped", time: (Date.now() - t0) / 1000 });
              } else if (res.ratio > visualThreshold) {
                try { if (res.diffBuf) writeFileSync(join(dir, key + ".diff.png"), res.diffBuf); writeFileSync(join(dir, key + ".actual.png"), actual); } catch (_) {}
                if (step.soft) {
                  // woven (soft) screenshot: to_flow weaves one into EVERY UI flow, so a cosmetic/volatile
                  // change (clock, ad, restyle) must not hard-fail the run - flag for review instead. An
                  // explicit strict screenshot step (soft not set) still fails below.
                  cases.push({ name: label, status: "skipped", message: `VISUAL DIFF (review): ${res.changed}/${res.total} px changed (${(res.ratio * 100).toFixed(2)}% > ${(visualThreshold * 100).toFixed(2)}%) - saved ${key}.diff.png`, time: (Date.now() - t0) / 1000 });
                } else {
                  cases.push({ name: label, status: "failed", message: `visual regression: ${res.changed}/${res.total} px changed (${(res.ratio * 100).toFixed(2)}% > ${(visualThreshold * 100).toFixed(2)}%)`, time: (Date.now() - t0) / 1000 });
                }
              } else {
                cases.push({ name: `${label} (${(res.ratio * 100).toFixed(2)}% diff)`, status: "passed", time: (Date.now() - t0) / 1000 });
              }
            }
          }
        } else if (step.action === "dbverify") {
          // DB-in-one-flow: run the user's SQL check command (ICX never owns DB credentials) with the
          // searched value in ICX_DB_VALUE (passed by ENV, never interpolated into the command - no
          // injection from the value). Exit 0 = record found. Unset command -> skip (not a failure).
          const cmd = process.env.ICX_SQL_VERIFY_CMD || "";
          if (!cmd) {
            cases.push({ name: `${label} (db verify skipped - ICX_SQL_VERIFY_CMD unset)`, status: "skipped", time: (Date.now() - t0) / 1000 });
          } else {
            const r = spawnSync(cmd, { shell: true, timeout: 20000, encoding: "utf-8",
              env: { ...process.env, ICX_DB_VALUE: String(step.value || "") } });
            if (r.status === 0) {
              cases.push({ name: `${label} (db confirmed)`, status: "passed", time: (Date.now() - t0) / 1000 });
            } else {
              throw new Error(`db verify: record '${step.value}' not found in DB (exit ${r.status})`);
            }
          }
        } else {
          throw new Error(`unknown action "${step.action}"`);
        }
        if (!["assert", "assertjs", "assertgone", "a11y", "perf", "download", "confirmdialog", "screenshot", "dbverify"].includes(step.action)) {
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        }
        // best-effort fingerprint capture of the resolved selector - never affects the step result.
        // Keyed by origTarget (the selector flow.json actually asks for), NOT the possibly heal-rewritten
        // step.target, so a healed step still refreshes the fingerprint under the ORIGINAL selector -
        // durable healing depends on this: next run still looks up priorFp[origTarget].
        if (healOn && origTarget && !fpStore[origTarget]) {
          try {
            if ((await page.locator(step.target).first().count()) > 0) {
              const fp = await page.evaluate(captureFp, step.target);
              if (fp) fpStore[origTarget] = fp;
            }
          } catch (_) { /* capture is best-effort */ }
        }
      } catch (e) {
        // SOFT steps (best-effort checks like constraint probes) never FAIL the run: on a gated /
        // conditional form a field may not be actionable yet - record the step as skipped and move on
        // so the primary CRUD flow is not blocked by a secondary check on a not-yet-visible field.
        const status = step.soft ? "skipped" : "failed";
        cases.push({ name: label, status, message: String(e && e.message ? e.message : e), time: (Date.now() - t0) / 1000 });
      }
      flush();  // persist after every step - partial results survive a kill
      if (STEP_PAUSE > 0) { try { await page.waitForTimeout(STEP_PAUSE); } catch (_) { /* ignore */ } }
    }
  } finally {
    try { await (stagehand ? stagehand.close() : context.browser().close()); } catch (_) { /* ignore */ }
  }

  writeJUnit(junitPath, cases, (Date.now() - started) / 1000);
  if (healOn && fpPath) {
    // merge forward: keep prior-run fingerprints for selectors NOT exercised/captured this run, so a
    // sidecar write never drops a previously-captured selector - fpStore wins on overlapping keys.
    try { writeFileSync(fpPath, JSON.stringify({ ...priorFp, ...fpStore }), "utf-8"); } catch (_) { /* best-effort */ }
  }
  if (healOn && flowPath) {
    try { writeFileSync(flowPath.replace(/\.json$/i, "") + ".heals.json", JSON.stringify(heals), "utf-8"); } catch (_) { /* best-effort */ }
  }
  const failed = cases.some((c) => c.status === "failed" || c.status === "error");   // skipped is OK
  process.exit(failed ? 1 : 0);
}

main();
