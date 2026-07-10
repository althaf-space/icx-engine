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

import { readFileSync, writeFileSync } from "node:fs";

function arg(name, def = "") {
  const i = process.argv.indexOf(name);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

function xmlEscape(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function writeJUnit(outPath, cases, timeSec) {
  const failures = cases.filter((c) => c.status !== "passed").length;
  const body = cases.map((c) => {
    const open = `<testcase classname="ui-flow" name="${xmlEscape(c.name)}" time="${c.time || 0}"`;
    if (c.status === "passed") return `${open}/>`;
    const tag = c.status === "error" ? "error" : "failure";
    return `${open}><${tag} message="${xmlEscape(c.message || "")}"/></testcase>`;
  }).join("");
  const xml =
    `<?xml version="1.0" encoding="UTF-8"?>` +
    `<testsuite name="icx-ui" tests="${cases.length}" failures="${failures}" time="${timeSec}">` +
    body +
    `</testsuite>`;
  writeFileSync(outPath, xml, "utf-8");
}

async function main() {
  const flowPath = arg("--flow", process.env.ICX_UI_FLOW || "");
  const baseUrl = arg("--url", process.env.ICX_TARGET_URL || "");
  const junitPath = arg("--junit", ".icx-ui-junit.xml");
  const started = Date.now();
  const cases = [];

  let flow;
  try {
    flow = JSON.parse(readFileSync(flowPath, "utf-8"));
  } catch (e) {
    cases.push({ name: "load flow", status: "error", message: `cannot read flow: ${e}` });
    writeJUnit(junitPath, cases, 0);
    process.exit(1);
  }

  let stagehand;
  try {
    const { Stagehand } = await import("@browserbasehq/stagehand");
    stagehand = new Stagehand({ env: "LOCAL", headless: true });
    await stagehand.init();
  } catch (e) {
    cases.push({ name: "init stagehand", status: "error", message: `stagehand init failed: ${e}` });
    writeJUnit(junitPath, cases, (Date.now() - started) / 1000);
    process.exit(1);
  }

  const page = stagehand.page;
  try {
    for (const step of flow.steps || []) {
      const label = step.description || `${step.action} ${step.target || ""}`.trim();
      const t0 = Date.now();
      try {
        if (step.action === "goto") {
          await page.goto(step.target || baseUrl);
        } else if (step.action === "click") {
          await page.locator(step.target).click();
        } else if (step.action === "fill") {
          await page.locator(step.target).fill(step.value || "");
        } else if (step.action === "assert") {
          const text = await page.locator(step.target).innerText();
          if (step.value && !String(text).includes(step.value)) {
            throw new Error(`expected "${step.value}" in "${text}"`);
          }
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        }
        if (step.action !== "assert") {
          cases.push({ name: label, status: "passed", time: (Date.now() - t0) / 1000 });
        }
      } catch (e) {
        cases.push({ name: label, status: "failed", message: String(e), time: (Date.now() - t0) / 1000 });
      }
    }
  } finally {
    try { await stagehand.close(); } catch (_) { /* ignore */ }
  }

  writeJUnit(junitPath, cases, (Date.now() - started) / 1000);
  const failed = cases.some((c) => c.status !== "passed");
  process.exit(failed ? 1 : 0);
}

main();
