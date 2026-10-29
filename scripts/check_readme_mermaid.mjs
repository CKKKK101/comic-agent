// 从 README 提取 Mermaid，在浏览器中解析 / 渲染并导出可放大的 SVG。
// 依赖放在本地工具目录；README 是图定义的唯一维护来源。
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright");
const source = await readFile(resolve("README.md"), "utf8");
const diagrams = [...source.matchAll(/^```mermaid\s*\n([\s\S]*?)^```\s*$/gm)].map(match => match[1]);
const names = ["architecture", "execution-graph", "approval-sequence", "rollback", "critic", "editing", "run-state", "media-pipeline", "data-model"];
if (diagrams.length !== names.length) throw new Error("README 图数量变化，请同步导出文件名映射");
await mkdir(resolve("docs/diagrams"), { recursive: true });
await mkdir(resolve(".venv-preview/docs-check"), { recursive: true });
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const results = [];
try {
  await page.setContent('<html lang="zh-CN"><head><meta charset="utf-8"></head><body style="margin:0;background:#fff"></body></html>');
  await page.addScriptTag({ path: resolve(".venv-preview/docs-tools/node_modules/mermaid/dist/mermaid.min.js") });
  await page.evaluate(() => window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral", fontFamily: "Segoe UI, Microsoft YaHei, sans-serif", flowchart: { useMaxWidth: false }, sequence: { useMaxWidth: false }, er: { useMaxWidth: false } }));
  for (const [index, diagram] of diagrams.entries()) {
    const rendered = await page.evaluate(async ({ diagram, index }) => {
      await window.mermaid.parse(diagram);
      return (await window.mermaid.render(`readmeDiagram${index}`, diagram)).svg;
    }, { diagram, index });
    await writeFile(resolve(`docs/diagrams/${names[index]}.svg`), rendered);
    await page.locator("body").evaluate((body, svg) => { body.innerHTML = svg; }, rendered);
    const bounds = await page.locator("svg").boundingBox();
    if (!bounds || bounds.width < 30 || bounds.height < 30) throw new Error(`图无有效尺寸：${names[index]}`);
    await page.locator("svg").screenshot({ path: resolve(`.venv-preview/docs-check/${names[index]}.png`) });
    results.push({ name: names[index], width: Math.ceil(bounds.width), height: Math.ceil(bounds.height), parsed: true });
  }
  await writeFile(resolve(".venv-preview/docs-check/report.json"), JSON.stringify({ passed: true, diagrams: results }, null, 2));
  console.log(JSON.stringify({ passed: true, diagrams: results }));
} finally { await browser.close(); }
