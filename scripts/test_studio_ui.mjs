// UI 改造验收：使用独立 fake 服务，覆盖明暗主题、工作台布局和人工审批。
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright");
const base = process.env.BASE_URL || "http://127.0.0.1:18767";
const output = resolve(".venv-preview/ui-redesign");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", e => errors.push(e.message));
const check = (value, message) => { if (!value) throw new Error(message); };
async function waitForView(pid, predicate) {
  for (let n = 0; n < 150; n++) {
    const view = await (await page.request.get(`${base}/api/v1/projects/${pid}`)).json();
    if (predicate(view)) return view;
    await new Promise(done => setTimeout(done, 100));
  }
  throw new Error("等待阶段超时");
}
async function capture(name) { await page.screenshot({ path: `${output}/${name}.png`, fullPage: true, animations: "disabled" }); }
async function noOverflow() { check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `页面横向溢出：${page.url()}`); }
try {
  const demoId = JSON.parse(await readFile(resolve(".venv-preview/ui-results/report.json"), "utf8")).projectId;
  await page.goto(`${base}/`);
  await page.getByRole("heading", { name: "写下你的故事" }).waitFor();
  await capture("home-dark");
  await page.getByRole("button", { name: "切换浅色主题" }).click();
  await capture("home-light");
  await page.reload();
  await page.getByRole("button", { name: "切换深色主题" }).waitFor();
  await page.goto(`${base}/#/project/${demoId}`);
  await page.getByRole("button", { name: "分镜", exact: true }).click();
  await capture("workbench-light");
  await page.getByRole("button", { name: "切换深色主题" }).click();
  await capture("workbench-dark");
  await page.getByRole("button", { name: "收起记录", exact: true }).click();
  check(await page.locator(".workbench-activity").count() === 0, "创作记录未收起");
  await capture("workbench-focus");
  await page.getByRole("button", { name: "创作记录", exact: true }).click();
  await page.getByRole("button", { name: "编辑分镜", exact: true }).first().click();
  await page.getByLabel("场景", { exact: true }).waitFor();
  await capture("shot-editing");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "角色", exact: true }).click();
  await page.locator(".asset-preview-button").first().click();
  check(await page.getByRole("dialog").isVisible(), "图片大图未打开");
  await page.keyboard.press("Escape");
  check(!await page.getByRole("dialog").isVisible(), "Escape 未关闭图片预览");
  for (const [path, file] of [["/projects", "projects"], ["/characters", "characters"], ["/settings", "settings"]]) {
    await page.goto(`${base}/#${path}`);
    await page.locator("h1").waitFor();
    await capture(file); await noOverflow();
  }
  await page.goto(`${base}/#/projects`);
  await page.getByLabel("搜索项目").fill("不会存在的搜索结果_123456");
  await page.getByText("没有找到匹配的故事，换个关键词试试。", { exact: true }).waitFor();
  await page.setViewportSize({ width: 768, height: 1024 });
  for (const path of ["/", `/project/${demoId}`, "/characters", "/projects", "/settings"]) {
    await page.goto(`${base}/#${path}`); await page.locator("h1").waitFor(); await noOverflow();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/`);
  await page.getByLabel("故事创意").waitFor();
  await capture("mobile-home"); await noOverflow();
  await page.goto(`${base}/#/project/${demoId}`);
  await page.getByRole("button", { name: "分镜", exact: true }).click();
  check(!await page.locator(".workbench-activity").isVisible(), "手机默认内容区未独占空间");
  await capture("mobile-workbench"); await noOverflow();
  await page.getByRole("button", { name: "创作记录", exact: true }).click();
  check(await page.locator(".workbench-activity").isVisible(), "手机创作记录无法打开");
  check(!await page.locator(".workbench-content").isVisible(), "手机切换记录后内容未隐藏");
  await capture("mobile-activity");
  for (const path of ["/characters", "/projects", "/settings"]) {
    await page.goto(`${base}/#${path}`); await page.locator("h1").waitFor(); await noOverflow();
  }
  // 完整经过六个审批门，确认重排后的操作依然可达。
  const pid = (await (await page.request.post(`${base}/api/v1/projects`, { data: { topic: "工作室界面人工审批验证" } })).json()).id;
  check((await page.request.post(`${base}/api/v1/projects/${pid}/generate`, { data: {} })).ok(), "启动审批验证失败");
  await page.goto(`${base}/#/project/${pid}`);
  await page.getByRole("button", { name: "创作记录", exact: true }).click();
  for (const gate of ["outline_approval", "characters_approval", "shots_approval", "character_images_approval", "shot_images_approval", "compose_approval"]) {
    await waitForView(pid, v => v.latest_run.current_stage === gate && v.actions.can_manage_references !== undefined);
    await page.locator(".record-current").filter({ hasText: ({ outline_approval: "大纲确认", characters_approval: "角色确认", shots_approval: "分镜确认", character_images_approval: "立绘确认", shot_images_approval: "画面确认", compose_approval: "成片确认" })[gate] }).waitFor();
    await page.getByRole("button", { name: "通过", exact: true }).click();
  }
  await waitForView(pid, v => v.latest_run.status === "succeeded");
  check(!errors.length, `浏览器异常：${errors.join("; ")}`);
  await writeFile(`${output}/report.json`, JSON.stringify({ passed: true, themes: ["dark", "light"], viewportWidths: [1440, 768, 390], approvalGates: 6, browserErrors: errors }, null, 2));
  console.log(JSON.stringify({ passed: true, screenshots: output, approvalGates: 6 }));
} catch (e) { await capture("failure"); await writeFile(`${output}/failure.html`, await page.content()); throw e; }
finally { await browser.close(); }
