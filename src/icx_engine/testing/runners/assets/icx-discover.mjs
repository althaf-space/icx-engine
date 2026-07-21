// ICX runtime census AUTO-DISCOVERY.
//
// Instead of an agent reading source to write the census, this opens the LIVE screen and inspects the
// rendered DOM to build the census ITSELF - so the result is deterministic (same on every agent) and
// needs no per-screen code for a covered archetype. It detects: the search box, the toolbar create /
// export controls, the per-row action icons (view / edit / delete), and - by opening the create form -
// its fields (type, real maxLength, required, control kind) and whether it is a multi-step wizard.
//
// Output: a census JSON (the exact shape census_to_flow consumes) written to --out.
//
//   node icx-discover.mjs --url <screenUrl> --state <storageState.json> --out <census.json>

import { chromium } from "playwright";
import { readFileSync, writeFileSync, existsSync } from "node:fs";

function arg(name, def = "") {
  const i = process.argv.indexOf(name);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

const URL = arg("--url");
const STATE = arg("--state");
const OUT = arg("--out", "census.json");
const TIMEOUT = parseInt(arg("--timeout", "30000"), 10);

async function restore(ctx, sp) {
  if (!sp || !existsSync(sp)) return;
  try {
    const st = JSON.parse(readFileSync(sp, "utf-8"));
    try { await ctx.addCookies(st.cookies || []); } catch (_) {}
    const local = {};
    for (const o of (st.origins || [])) { const bag = {}; for (const kv of (o.localStorage || [])) bag[kv.name] = kv.value; local[o.origin] = bag; }
    let session = {};
    const sf = sp + ".session";
    if (existsSync(sf)) { try { session = JSON.parse(readFileSync(sf, "utf-8")) || {}; } catch (_) {} }
    await ctx.addInitScript((p) => {
      try {
        const here = (p.local && p.local[window.location.origin]) || {};
        for (const k in here) window.localStorage.setItem(k, here[k]);
        for (const k in (p.session || {})) window.sessionStorage.setItem(k, p.session[k]);
      } catch (_) {}
    }, { local, session });
  } catch (_) {}
}

// -- in-page DOM inspection (runs in the browser) ----------------------------

// Detect the list-level controls: search box, toolbar create/export, row action icons.
function discoverTopLevel() {
  const q = (s) => [...document.querySelectorAll(s)];
  const visible = (el) => el && el.offsetParent !== null && el.getBoundingClientRect().width > 0;
  const cssId = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const tid = el.getAttribute("data-testid");
    if (tid) return `[data-testid="${tid}"]`;
    const nm = el.getAttribute("name");
    if (nm) return `${el.tagName.toLowerCase()}[name="${nm}"]`;
    const ph = el.getAttribute("placeholder");
    if (ph) return `${el.tagName.toLowerCase()}[placeholder="${ph}"]`;
    return null;
  };
  const labelText = (el) => (el.getAttribute("title") || el.getAttribute("aria-label") || el.getAttribute("alt") || el.value || el.textContent || "").trim();

  const out = { search: null, create: null, exports: [], row: { view: null, edit: null, delete: null } };

  // SEARCH: a text/search input whose placeholder or id mentions search.
  for (const el of q('input[type="search"], input[placeholder], input[type="text"]')) {
    if (!visible(el)) continue;
    const hay = ((el.placeholder || "") + " " + (el.id || "") + " " + (el.className || "")).toLowerCase();
    if (el.type === "search" || /search|filter|lookup/.test(hay)) { out.search = cssId(el); break; }
  }

  // a usable selector for a control even when it has no id/testid/name: fall back to a class combo,
  // else a text-based selector (buttons reliably carry their action label as text/value).
  const btnSel = (el) => {
    const base = cssId(el);
    if (base) return base;
    const cls = (el.className || "").toString().trim().split(/\s+/).filter((c) => c && !/\d/.test(c));
    if (cls.length) return el.tagName.toLowerCase() + "." + cls.join(".");
    const txt = labelText(el);
    if (txt) return `${el.tagName.toLowerCase()}:has-text("${txt.replace(/"/g, "")}")`;
    return null;
  };
  // TOOLBAR buttons: create/add/new + export/download - a button NOT inside a table row.
  for (const el of q('button, input[type="button"], input[type="submit"], a.btn, a[role="button"], [role="button"], .btn')) {
    if (!visible(el) || el.closest("tr, tbody")) continue;
    const t = labelText(el).toLowerCase();
    const sel = btnSel(el);
    if (!sel) continue;
    if (!out.create && /(^|\b)(create|add|new)\b|^\+$/.test(t)) out.create = { sel, name: labelText(el) };
    if (/export|download|csv|excel|xlsx/.test(t)) out.exports.push({ sel, name: labelText(el) });
  }

  // ROW action icons: img/button/a inside a table row with a title/aria-label naming the action.
  const rowEls = q('table tbody tr img, table tbody tr button, table tbody tr a, [class*="table"] [role="row"] img');
  const pick = (re) => {
    for (const el of rowEls) {
      // match the action keyword in the visible label OR the element's testid/id/class/src (an icon
      // with no title still names its action in data-testid="team-delete-5" or src="delete.png").
      const hay = (labelText(el) + " " + (el.getAttribute("data-testid") || "") + " " + (el.id || "") +
                   " " + (el.className || "") + " " + (el.getAttribute("src") || "")).toLowerCase();
      if (!re.test(hay)) continue;
      // Prefer a GENERIC selector that matches EVERY row (never a per-record value). data-testid is
      // usually `<action>-<rowId>` -> strip the id suffix to a prefix match. title is next (icons share
      // a title across rows). alt often embeds the record name, so use it LAST and generalize to prefix.
      const tid = el.getAttribute("data-testid");
      // strip the trailing row-id but KEEP the separator so the prefix stays precise:
      // "team-edit-5" -> "team-edit-" (not "team-edit", which would also match "team-editX").
      if (tid) { const pre = tid.replace(/([-_])[0-9A-Za-z ]+$/, "$1"); return `[data-testid^="${pre !== tid ? pre : tid}"]`; }
      const title = el.getAttribute("title");
      if (title && !/\d/.test(title)) return `${el.tagName.toLowerCase()}[title="${title}"]`;
      const alt = el.getAttribute("alt");
      if (alt) { const base = alt.replace(/\s*\S*\d\S*.*$/, "").trim(); if (base) return `${el.tagName.toLowerCase()}[alt^="${base}"]`; }
    }
    return null;
  };
  out.row.view = pick(/view|overview|detail|preview|show/);
  out.row.edit = pick(/edit|modify|update/);
  out.row.delete = pick(/delete|remove|deactivate/);
  return out;
}

// Read the fields + submit inside the currently-open create/edit modal.
function discoverForm(modalSel) {
  const root = document.querySelector(modalSel) || document;
  const q = (s) => [...root.querySelectorAll(s)];
  const visible = (el) => el && el.offsetParent !== null;
  const cssId = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const tid = el.getAttribute("data-testid");
    if (tid) return `[data-testid="${tid}"]`;
    const nm = el.getAttribute("name");
    if (nm) return `${el.tagName.toLowerCase()}[name="${nm}"]`;
    return null;
  };
  const humanize = (s) => (s || "").replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const nearbyLabel = (el) => {
    // 1) explicit association - the most reliable, per-field.
    if (el.id) { const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && clean(l.textContent)) return clean(l.textContent); }
    // 2) wrapping label (its own text, minus the control).
    const wrap = el.closest("label"); if (wrap && clean(wrap.textContent)) return clean(wrap.textContent);
    // 3) per-field attributes - never shared across fields.
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    if (el.getAttribute("placeholder")) return el.getAttribute("placeholder").trim();
    // 4) a TIGHTLY-scoped label: the immediate previous sibling, or a label that is a direct child of
    // the field's own wrapper (NOT any label anywhere up the tree - that grabs a neighbour's label).
    let prev = el.previousElementSibling;
    for (let h = 0; prev && h < 3; h++, prev = prev.previousElementSibling) {
      if (/^(label|span|div)$/i.test(prev.tagName) && clean(prev.textContent) && clean(prev.textContent).length < 40) return clean(prev.textContent);
    }
    const wrapEl = el.closest(".form-group, .field, .col, td, div");
    if (wrapEl) { const lab = [...wrapEl.children].find((c) => /^(label)$/i.test(c.tagName) && clean(c.textContent)); if (lab) return clean(lab.textContent); }
    // 5) humanized id / name - always unique per field.
    return humanize(el.id) || humanize(el.name) || "field";
  };
  const controlKind = (el) => {
    const t = (el.getAttribute("type") || el.type || "").toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return { kind: "text", type: "text" };
    if (tag === "select") return { kind: "select", type: "text" };
    if (t === "checkbox" || t === "radio") return { kind: "checkbox", type: "text" };
    if (t === "range" || t === "color") return { kind: t, type: t };
    if (t === "file") return { kind: "file", type: "file" };
    if (["email", "url", "number", "tel", "date"].includes(t)) return { kind: "text", type: t };
    return { kind: "text", type: "text" };
  };

  const fields = [];
  const seen = new Set();
  // native inputs / selects / textareas
  for (const el of q('input:not([type="hidden"]):not([type="button"]):not([type="submit"]), select, textarea')) {
    if (!visible(el) || el.disabled) continue;
    const t = (el.type || "").toLowerCase();
    if (t === "search") continue;                                   // that is the list search, not a field
    // skip the hidden/inner input of a custom dropdown (react-select / antd / MUI) - the WIDGET is
    // captured below and driven by pickoption; its inner input is not a real field.
    if (el.closest('[class*="select__control"], [class*="select__value"], .ant-select, .MuiSelect-root, .MuiAutocomplete-root')) continue;
    if (/^react-select-|-input$/.test(el.id || "")) continue;
    const sel = cssId(el); if (!sel || seen.has(sel)) continue; seen.add(sel);
    const k = controlKind(el);
    const ml = el.getAttribute("maxlength");
    const f = { label: nearbyLabel(el), domSelectors: [sel], type: k.type,
                required: el.required || el.getAttribute("aria-required") === "true" };
    if (k.kind === "checkbox") f.interactionPattern = "checkbox";
    else if (k.kind === "select") f.interactionPattern = "select";
    else if (k.kind === "range" || k.kind === "color") f.interactionPattern = k.kind;
    else if (k.kind === "file") f.interactionPattern = "file-upload";
    const v = {};
    if (ml && +ml > 0) v.maxLength = +ml;
    if (el.getAttribute("min")) v.min = el.getAttribute("min");
    if (el.getAttribute("max")) v.max = el.getAttribute("max");
    if (el.getAttribute("pattern")) v.pattern = el.getAttribute("pattern");
    if (Object.keys(v).length) f.validations = v;
    fields.push(f);
  }
  // custom dropdowns (react-select / antd / MUI) - a container we can drive by pickoption.
  for (const el of q('[class*="select__control"], .ant-select, .MuiSelect-root, .MuiAutocomplete-root')) {
    if (!visible(el)) continue;
    const host = el.closest("[id]") || el;
    const sel = host.id ? "#" + CSS.escape(host.id) : null;
    if (!sel || seen.has(sel)) continue; seen.add(sel);
    fields.push({ label: nearbyLabel(el), domSelectors: [sel], interactionPattern: "react-select", required: true });
  }

  // submit + cancel + next in the modal footer.
  const btnText = (el) => (el.value || el.textContent || "").trim();
  let submit = null, cancel = null, next = null;
  for (const el of q('input[type="button"], input[type="submit"], button, a.btn')) {
    if (!visible(el)) continue;
    const t = btnText(el).toLowerCase();
    const sel = `${el.tagName.toLowerCase()}[value="${el.value}"]`;
    const useSel = el.value ? sel : cssId(el) || (el.className ? "." + el.className.trim().split(/\s+/).join(".") : null);
    if (!useSel) continue;
    if (/next|proceed/.test(t)) next = next || useSel;
    else if (/cancel|close|back/.test(t)) cancel = cancel || useSel;
    else if (/create|save|submit|update|add|confirm|ok/.test(t)) submit = submit || useSel;
  }
  // modal title - strip a trailing close glyph (x / times / multiplication sign) the header often
  // renders next to the title, and any surrounding whitespace.
  let modalName = ((root.querySelector(".modal-title, .modalHeaderLabel, [class*=modalHeader], h4, h5") || {}).textContent || "");
  modalName = modalName.replace(/[^A-Za-z0-9)]+$/, "").replace(/[ 	]+/g, " ").trim();
  return { fields, submit, cancel, next, modalName };
}

// Fill a wizard step's fields just enough to pass validation so NEXT advances (during discovery only).
async function fillStepForNav(page, fields, modalSel) {
  for (const f of (fields || [])) {
    const sel = f.domSelectors[0];
    try {
      const loc = page.locator(sel).first();
      if (f.interactionPattern === "checkbox") { await loc.check({ timeout: 1500 }).catch(() => {}); }
      else if (f.interactionPattern === "select") { await loc.selectOption({ index: 1 }, { timeout: 1500 }).catch(() => {}); }
      else if (f.interactionPattern === "react-select") { await loc.click({ timeout: 1500 }).catch(() => {}); await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter"); await page.keyboard.press("Escape"); }
      else {
        const t = (f.type || "text");
        const v = t === "email" ? ("d" + Date.now() % 100000 + "@example.com")
          : t === "tel" ? "9" + String(Date.now() % 1000000000).padStart(9, "0")
          : t === "number" ? "5" : t === "url" ? "https://example.com/x" : ("D" + Date.now() % 100000);
        await loc.fill(v, { timeout: 1500 }).catch(() => {});
      }
    } catch (_) { /* best-effort */ }
  }
  await page.waitForTimeout(300);
}

// Detect NON-CRUD content: charts, data grids, KPI/stat cards (a dashboard/analytics screen) and
// page-level filters + a generate/apply button (a report screen). This is what makes the tool cover a
// screen that has no create/edit/rows - otherwise such a screen generates nothing but a trivial render.
function discoverWidgets() {
  const q = (s) => { try { return [...document.querySelectorAll(s)]; } catch (_) { return []; } };
  const vis = (el) => el && el.offsetParent !== null && el.getBoundingClientRect().width > 8 && el.getBoundingClientRect().height > 8;
  const inModal = (el) => !!el.closest('.modal.show, .modal.fade.show, [role="dialog"]');
  // an auto-generated id (highcharts/chartjs/react-select/radix/pure-hash) CHANGES every render - never
  // use it as a selector; fall through to the class or a classed ancestor instead.
  const isDynamicId = (id) => /^(highcharts|chartjs|apexcharts|echarts|react-select-\d|radix-|headlessui-|:r[a-z0-9]+:)/i.test(id) || /^[a-z]+[-_][a-z0-9]{8,}$/i.test(id);
  const selOf = (el) => {
    if (!el) return null;
    if (el.id && !isDynamicId(el.id)) return "#" + CSS.escape(el.id);
    const tid = el.getAttribute && el.getAttribute("data-testid"); if (tid) return `[data-testid="${tid}"]`;
    // el.className on an SVG element is an SVGAnimatedString OBJECT (".toString()" -> "[object ...]");
    // getAttribute("class") is a plain string for BOTH svg and html.
    const cn = ((el.getAttribute && el.getAttribute("class")) || "").trim();
    const cls = cn.split(/\s+/).filter((c) => c && !/\d/.test(c) && c.length < 32);
    if (cls.length) return el.tagName.toLowerCase() + "." + cls.slice(0, 2).join(".");
    return null;
  };
  // a stable selector for the element OR its nearest classed/id'd ancestor (a bare <svg> chart has no
  // usable selector of its own - the chart CONTAINER does).
  const stableSel = (el) => {
    for (let n = el, h = 0; n && h < 3; n = n.parentElement, h++) { const s = selOf(n); if (s) return s; }
    return null;
  };
  const widgets = [];
  const CAP = { chart: 6, grid: 3, card: 4 };
  const push = (kind, el, useAncestor) => {
    if (!vis(el) || inModal(el)) return;
    if (widgets.filter((w) => w.kind === kind).length >= (CAP[kind] || 6)) return;   // per-kind cap
    const s = useAncestor ? stableSel(el) : selOf(el); if (!s) return;
    if (widgets.some((w) => w.selector === s)) return;
    widgets.push({ kind, selector: s });
  };
  // charts (library-agnostic incl amCharts v4/v5) + ANY sizeable svg/canvas (a real viz, not an icon).
  for (const el of q('.amcharts-chart-div, [class*="amChart"], [id*="chartdiv" i], [id*="chart-div" i], svg.recharts-surface, .recharts-wrapper, .highcharts-container, [class*="apexcharts-canvas"], .echarts, [_echarts_instance_], .nvd3, .js-plotly-plot, [class*="chart"] canvas, [class*="chart"] svg, [class*="graph"] canvas, [class*="graph"] svg')) push("chart", el, true);
  for (const el of q('svg, canvas')) { const r = el.getBoundingClientRect(); if (r.width >= 120 && r.height >= 80) push("chart", el, true); }
  // data grids/tables with rows - keep only the OUTERMOST per grid (react-bs-table etc. nest a container
  // + a header table + a body table; capturing each would assert the same grid 3-4x, and the header/empty
  // body sub-tables are not independently "visible").
  const gridEls = q('table, [role="grid"], [class*="react-bs-table"], [class*="dataTable"], [class*="ag-root"], .rt-table, [class*="DataGrid"]').filter((el) => el.querySelector('tr, [role="row"], .rt-tr') && vis(el) && !inModal(el));
  for (const el of gridEls) { if (!gridEls.some((o) => o !== el && o.contains(el))) push("grid", el, true); }
  // KPI / stat / summary / segment cards (the common analytics widget). Bare .card counts too - on a
  // dashboard the cards ARE the content; duplicates collapse to one selector, capped per kind above.
  for (const el of q('[class*="kpi"], [class*="statistic"], [class*="stat-"], [class*="widget"], [class*="tile"], [class*="count-box"], [class*="summary"], [class*="metric"], [class*="card-body"], .card')) push("card", el);
  return widgets.slice(0, 12);
}

function discoverReport() {
  const q = (s) => { try { return [...document.querySelectorAll(s)]; } catch (_) { return []; } };
  const vis = (el) => el && el.offsetParent !== null && el.getBoundingClientRect().width > 0;
  const inModal = (el) => !!el.closest('.modal.show, .modal.fade.show, [role="dialog"]');
  const cssId = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const nm = el.getAttribute("name"); if (nm) return `${el.tagName.toLowerCase()}[name="${nm}"]`;
    const ph = el.getAttribute("placeholder"); if (ph) return `${el.tagName.toLowerCase()}[placeholder="${ph}"]`;
    const cls = (el.className || "").toString().trim().split(/\s+/).filter((c) => c && !/\d/.test(c) && c.length < 32);
    if (cls.length) return el.tagName.toLowerCase() + "." + cls.slice(0, 2).join(".");
    return null;
  };
  const humanize = (s) => (s || "").replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
  const filters = [];
  const seen = new Set();
  // page-level date inputs + selects (NOT inside a create/edit modal): a report's filter row.
  for (const el of q('input[type="date"], input[type="month"], [class*="datepicker"] input, [class*="DateRange"] input, [class*="date-range"] input, input[placeholder*="date" i], select')) {
    if (!vis(el) || inModal(el) || el.disabled) continue;
    const s = cssId(el); if (!s || seen.has(s)) continue; seen.add(s);
    const kind = el.tagName.toLowerCase() === "select" ? "select" : "text";
    filters.push({ label: humanize(el.id) || humanize(el.name) || (el.getAttribute("placeholder") || "filter"), domSelectors: [s], interactionPattern: kind === "select" ? "select" : undefined, type: el.getAttribute("type") === "date" ? "date" : "text" });
  }
  // page-level react-select / antd / MUI dropdown filters
  for (const el of q('[class*="select__control"], .ant-select, .MuiSelect-root')) {
    if (!vis(el) || inModal(el)) continue;
    const host = el.closest("[id]"); const s = host && host.id ? "#" + CSS.escape(host.id) : null;
    if (!s || seen.has(s)) continue; seen.add(s);
    filters.push({ label: "filter", domSelectors: [s], interactionPattern: "react-select" });
  }
  // an apply/generate/search/go/run/show button (NOT a row action, NOT create/add) that loads the data.
  let apply = null;
  for (const el of q('button, input[type="button"], input[type="submit"], a.btn, .btn')) {
    if (!vis(el) || inModal(el) || el.closest("tr, tbody")) continue;
    const t = ((el.value || el.textContent || "") + "").trim().toLowerCase();
    if (/\b(generate|apply|search|go|run|show|submit|view report|get report|proceed|display|fetch)\b/.test(t) && !/create|add|new/.test(t)) {
      apply = el.value ? `${el.tagName.toLowerCase()}[value="${el.value}"]` : (cssId(el) || `${el.tagName.toLowerCase()}:has-text("${((el.textContent || "").trim()).replace(/"/g, "")}")`);
      if (apply) break;
    }
  }
  return { filters, apply };
}

// -- orchestration -----------------------------------------------------------

async function main() {
  const b = await chromium.launch();
  const ctx = await b.newContext();
  await restore(ctx, STATE);
  const page = await ctx.newPage();
  page.on("dialog", async (d) => { try { await (d.type() === "beforeunload" ? d.dismiss() : d.accept()); } catch (_) {} });
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  // wait for the screen to actually populate - a toolbar create button or a table row or a search box
  // (a heavy grid renders its toolbar only after the first data fetch).
  await page.waitForSelector('button, input[type="button"], table tbody tr, input[placeholder]', { timeout: 12000 }).catch(() => {});
  await page.waitForTimeout(4000);

  let top = await page.evaluate(discoverTopLevel);
  if (!top.create && !top.row.view && !top.row.edit) { await page.waitForTimeout(3000); top = await page.evaluate(discoverTopLevel); }
  const MODAL = '.modal.show, .modal.fade.show, [role="dialog"], [class*="ModalWindow"], [class*="modal-content"], [class*="modal-dialog"]';

  const funcs = [];
  const summary = [];
  const add = (id, type, fn) => { funcs.push(fn); summary.push({ id, type }); };

  if (top.search) add("F_SEARCH", "Search", { id: "F_SEARCH", functionality: "Search", modalDetails: { triggerSelector: top.search } });
  for (let i = 0; i < top.exports.length; i++)
    add("F_EXPORT" + i, "Download", { id: "F_EXPORT" + i, functionality: top.exports[i].name || "Export", modalDetails: { triggerSelector: top.exports[i].sel } });
  if (top.row.view) add("F_VIEW", "View", { id: "F_VIEW", functionality: "View", modalDetails: { triggerSelector: top.row.view, modalSelector: MODAL }, cancelButton: { selectors: [] } });

  // NON-CRUD content (charts/grids/cards) + page filters: capture NOW on the clean list view, BEFORE the
  // create block below opens a form / navigates into a builder (which would hide the list's widgets).
  const widgets = await page.evaluate(discoverWidgets).catch(() => []);
  const report = await page.evaluate(discoverReport).catch(() => ({ filters: [], apply: null }));

  // CREATE: open the form and read it (fields, submit, wizard steps).
  let createFn = null;
  if (top.create) {
    try {
      // normal click first; if an overlay (an empty iframe, a sticky header, a transparent layer)
      // intercepts pointer events, retry with force so the create button beneath is still opened.
      try { await page.locator(top.create.sel).first().click({ timeout: 8000 }); }
      catch (_) { await page.locator(top.create.sel).first().click({ timeout: 5000, force: true }); }
      await page.waitForTimeout(2000);
      const modalSel = (await page.locator(MODAL).count()) ? MODAL : "body";
      // a modal may load its fields async (a KPI / options fetch) - re-read until fields appear.
      let form = await page.evaluate(discoverForm, modalSel);
      for (let r = 0; r < 4 && form.fields.length === 0; r++) { await page.waitForTimeout(1500); form = await page.evaluate(discoverForm, modalSel); }
      // MULTI-STAGE OPEN: some "Create" buttons open a CHOOSER (e.g. "AI Builder" vs "Manual Builder"),
      // not the form directly. When no fields appeared, click a manual/build/continue option once and
      // re-scan so the real builder is reached (best-effort; a bespoke rule builder may still have none).
      if (form.fields.length === 0) {
        for (const opt of ['button:has-text("Build Manually")', 'button:has-text("Manual")', 'button:has-text("Continue")', 'button:has-text("Get Started")', 'button:has-text("Advanced")', 'button:has-text("Next")']) {
          const o = page.locator(opt).first();
          if (await o.count() && await o.isVisible().catch(() => false)) {
            await o.click({ timeout: 3000 }).catch(() => {});
            await page.waitForTimeout(2000);
            const ms2 = (await page.locator(MODAL).count()) ? MODAL : "body";
            form = await page.evaluate(discoverForm, ms2);
            break;
          }
        }
      }
      createFn = { id: "F_CREATE", functionality: top.create.name || "Create",
                   modalDetails: { triggerSelector: top.create.sel, modalSelector: modalSel, modalName: form.modalName.trim() },
                   submitButton: { selectors: form.submit ? [form.submit] : [] },
                   cancelButton: { selectors: form.cancel ? [form.cancel] : [] } };
      // WIZARD: if the open form has a NEXT button, walk the steps. Each step is FILLED before NEXT so
      // navigation-gated steps reveal their fields (a validated wizard blocks NEXT until the current
      // step is complete), and so later steps that only appear after prior ones are captured.
      if (form.next) {
        const steps = [{ name: "Step 1", nextButton: { selectors: [form.next] }, fields: form.fields }];
        for (let s = 0; s < 8; s++) {
          await fillStepForNav(page, form.fields, modalSel);          // satisfy validation so NEXT works
          const nextSel = form.next;
          const advanced = await page.locator(nextSel).first().click({ timeout: 4000 }).then(() => true).catch(() => false);
          if (!advanced) break;
          await page.waitForTimeout(1500);
          // dismiss any async-check confirm popup that a field raised
          for (const y of ['.yesButton', 'button:has-text("YES")', 'input[value="YES"]', 'button:has-text("OK")']) {
            const yb = page.locator(y).first();
            if (await yb.count() && await yb.isVisible().catch(() => false)) { await yb.click({ timeout: 2000 }).catch(() => {}); break; }
          }
          form = await page.evaluate(discoverForm, modalSel);
          const isLast = !form.next;
          steps.push({ name: "Step " + (s + 2), nextButton: form.next ? { selectors: [form.next] } : undefined, fields: form.fields });
          if (form.submit) createFn.submitButton.selectors = [form.submit];
          if (isLast) break;
        }
        if (steps.length) delete steps[steps.length - 1].nextButton;
        // the final submit is often validation-gated (only appears once the last step is complete) -
        // fill the last step and re-scan for it.
        if (!createFn.submitButton.selectors.length) {
          await fillStepForNav(page, form.fields, modalSel);
          for (const y of ['.yesButton', 'button:has-text("YES")', 'input[value="YES"]']) {
            const yb = page.locator(y).first();
            if (await yb.count() && await yb.isVisible().catch(() => false)) { await yb.click({ timeout: 2000 }).catch(() => {}); break; }
          }
          const again = await page.evaluate(discoverForm, modalSel);
          if (again.submit) createFn.submitButton.selectors = [again.submit];
        }
        createFn.steps = steps;
      } else {
        createFn.fields = form.fields;
      }
      // CONSTRAINT PROBE (real typing): a maxlength attribute truncates TYPED input even when reading
      // the attr missed it. Type a long value with real key events and read back the capped length.
      // (A length enforced only in JS on SUBMIT is not visible to any runtime probe - that is the known
      // boundary where a source-read census is still more complete.)
      const allF = createFn.steps ? createFn.steps.flatMap((s) => s.fields || []) : (createFn.fields || []);
      for (const fld of allF) {
        if (fld.interactionPattern || (fld.validations && fld.validations.maxLength) || (fld.type && fld.type !== "text")) continue;
        try {
          const loc = page.locator(fld.domSelectors[0]).first();
          await loc.fill(""); await loc.pressSequentially("A".repeat(40), { timeout: 2500 });
          const len = await loc.evaluate((el) => el.value.length);
          await loc.fill("");
          if (len > 0 && len < 40) fld.validations = Object.assign(fld.validations || {}, { maxLength: len });
        } catch (_) { /* best-effort */ }
      }
      // close the form
      if (form.cancel) { try { await page.locator(form.cancel).first().click({ timeout: 3000 }); } catch (_) {} }
    } catch (_) { /* create not openable - skip */ }
  }
  if (createFn) add("F_CREATE", "Create", createFn);

  // EDIT + DELETE reuse the row icons; edit's form shape mirrors create (its own submit read at run time
  // is not needed - the row-scoped open + identifying-field change + submit covers it).
  if (top.row.edit && createFn) {
    // the edit's identifying field must be a NAME-like text field (searchable), NOT a url/email/logo
    // (the first field is often an image URL). Pick the first plain text field whose label is not a
    // url/email/image; fall back to the first field.
    const cfields = createFn.fields || (createFn.steps || []).flatMap((s) => s.fields || []);
    const idf = cfields.find((f) => {
      const lab = (f.label || "").toLowerCase();
      const kind = f.interactionPattern || "";
      return (!f.type || f.type === "text") && !kind && !/url|email|image|logo|link|photo|captain|file/.test(lab);
    }) || cfields[0];
    add("F_EDIT", "Edit", { id: "F_EDIT", functionality: "Edit",
      modalDetails: { triggerSelector: top.row.edit, modalSelector: MODAL },
      submitButton: { selectors: createFn.submitButton.selectors },
      cancelButton: { selectors: createFn.cancelButton.selectors },
      fields: idf ? [idf] : [] });
  }
  if (top.row.delete) add("F_DELETE", "Delete", { id: "F_DELETE", functionality: "Delete",
    modalDetails: { triggerSelector: top.row.delete },
    submitButton: { selectors: [".swal-button--confirm", ".yesButton", "button:has-text(\"YES\")", "button:has-text(\"OK\")", "input[value=\"YES\"]"] } });

  // NON-CRUD coverage (widgets + filters captured on the clean list above): a dashboard/analytics/report
  // screen has no create/rows but IS testable - its widgets must render and its filters must load data.
  if (widgets && widgets.length) {
    add("F_RENDER", "Render", { id: "F_RENDER", functionality: "Render",
      modalDetails: { triggerSelector: widgets[0].selector }, widgets });
  }
  // REPORT / FILTERED screen: page-level filters with or without an explicit Generate/Apply button (many
  // dashboards auto-reload on filter change). Emit whenever filters exist so the filter interaction is
  // exercised, not just the static render.
  if (report && (report.filters || []).length) {
    add("F_REPORT", "Report", { id: "F_REPORT",
      functionality: report.apply ? "Generate Report" : "Filter Data",
      fields: report.filters, submitButton: { selectors: report.apply ? [report.apply] : [] },
      resultSelector: (widgets && widgets[0]) ? widgets[0].selector : "" });
  }

  const census = { screenName: (await page.title()) || "Screen", functionalitySummaryTable: summary, functionalities: funcs };
  writeFileSync(OUT, JSON.stringify(census, null, 2), "utf-8");
  await b.close();
  process.stdout.write("discovered " + funcs.length + " functionalities\n");
}

main().catch((e) => { process.stderr.write("discover failed: " + (e && e.message ? e.message : e) + "\n"); process.exit(1); });
