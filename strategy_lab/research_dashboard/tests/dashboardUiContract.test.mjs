import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const appUrl = new URL("../src/App.tsx", import.meta.url);
const stylesUrl = new URL("../src/styles.css", import.meta.url);

async function sources() {
  return {
    app: await readFile(appUrl, "utf8"),
    styles: await readFile(stylesUrl, "utf8"),
  };
}

test("local reload is named accurately and never invokes a page or network refresh", async () => {
  const { app } = await sources();
  assert.match(app, /data-testid="reload-local-data"/);
  assert.match(app, />\s*重新读取本地结果\s*</);
  assert.match(app, /cache:\s*"no-store"/);
  assert.doesNotMatch(app, /window\.location\.reload/);
  assert.match(app, /本地重建摘要/);
  assert.match(app, /联网刷新后重建/);
  assert.match(app, /dev=/);
  assert.match(app, /preview=/);
});

test("trust, warning, cohort, action, and gate surfaces are present before history", async () => {
  const { app } = await sources();
  for (const marker of [
    'data-testid="trust-summary"',
    'data-testid="source-freshness"',
    "数据质量提示",
    "Cohort 一致性",
    "当前动作",
    "关键门禁",
  ]) assert.ok(app.includes(marker), `missing UI trust marker: ${marker}`);
  assert.match(app, /allGatesPass/);
  assert.ok(app.indexOf('data-testid="trust-summary"') < app.indexOf('data-testid="history-load"'));
});

test("history details are lazy loaded and verified before display", async () => {
  const { app } = await sources();
  assert.match(app, /data-testid="history-load"/);
  assert.match(app, /parseDashboardDetails, sha256Hex/);
  assert.match(app, /await sha256Hex\(raw\)/);
  assert.match(app, /历史明细 SHA-256 与摘要 manifest 不一致/);
  assert.match(app, /历史明细字节数不一致/);
  assert.match(app, /历史明细 schema 不受支持/);
  assert.match(app, /历史明细决策日期/);
});

test("mobile valuation keeps date, PE, PB, dividend yield, action, and gates", async () => {
  const { app, styles } = await sources();
  assert.match(app, /data-testid="valuation-mobile"/);
  assert.match(app, /估值日期/);
  assert.match(app, /<dt>PE<\/dt>/);
  assert.match(app, /<dt>PB<\/dt>/);
  assert.match(app, /<dt>股息率<\/dt>/);
  assert.match(styles, /@media \(max-width: 600px\)/);
  assert.match(styles, /\.valuation-mobile\s*\{\s*display:\s*grid/);
  assert.doesNotMatch(styles, /\.snapshot-date\s*\{\s*display:\s*none/);
  assert.doesNotMatch(styles, /\.value-table[^}]*nth-child\(n\+5\)[^}]*display:\s*none/s);
});

test("minimum accessibility and long-text protections remain explicit", async () => {
  const { app, styles } = await sources();
  assert.match(app, /aria-live="polite"/);
  assert.match(app, /role="alert"/);
  assert.match(app, /aria-labelledby=/);
  assert.match(app, /data-testid="fixed-notices"/);
  assert.match(styles, /:focus-visible/);
  assert.match(styles, /outline:\s*3px solid/);
  assert.match(styles, /overflow-wrap:\s*anywhere/);
  assert.match(styles, /prefers-reduced-motion/);
});
