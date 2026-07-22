import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { parseDashboardData, parseDashboardDetails, sha256Hex } from "../src/dashboardDataContract.ts";

const inputPath = resolve(process.argv[2] ?? "public/data/dashboard_data.json");
const raw = await readFile(inputPath, "utf8");
const data = parseDashboardData(JSON.parse(raw));

const detailsPath = resolve(dirname(inputPath), data.detail_manifest.url.replace(/^data[\\/]/, ""));
const detailsRaw = await readFile(detailsPath, "utf8");
const digest = await sha256Hex(detailsRaw);
if (digest !== data.detail_manifest.sha256) {
  throw new Error(`dashboard details SHA-256 mismatch: expected ${data.detail_manifest.sha256}, got ${digest}`);
}
const details = parseDashboardDetails(JSON.parse(detailsRaw));
if (details.decision_as_of_date !== data.decision_as_of_date) {
  throw new Error("dashboard details decision_as_of_date must match summary");
}
if (Buffer.byteLength(detailsRaw, "utf8") !== data.detail_manifest.bytes) {
  throw new Error("dashboard details byte count must match manifest");
}

console.log(`dashboard data contract passed: ${inputPath}`);
console.log(`schema=${data.schema_version} generated_at=${data.generated_at}`);
console.log(`details=${detailsPath} sha256=${digest} bytes=${data.detail_manifest.bytes}`);
