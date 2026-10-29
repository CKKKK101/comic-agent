// 本地交互回归：BASE_URL 指向独立的 fake 测试实例。
// PLAYWRIGHT_MODULE 可指定本机已有的 Playwright 模块入口。
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright");
const base = process.env.BASE_URL || "http://127.0.0.1:18767";
const output = resolve(".venv-preview/ui-results");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", error => errors.push(error.message));
const check = (condition, message) => { if (!condition) throw new Error(message); };
async function waitProject(pid, predicate) {
  for (let i = 0; i < 150; i++) {
    const response = await page.request.get(`${base}/api/v1/projects/${pid}`);
    const view = await response.json();
    if (predicate(view)) return view;
    await new Promise(done => setTimeout(done, 100));
  }
  throw new Error("等待项目超时");
}
try {
  await page.goto(`${base}/#/characters`);
  await page.getByRole("button", { name: "新建角色", exact: true }).click();
  const name = `星野 · 界面测试 ${Date.now().toString().slice(-6)}`;
  await page.getByLabel("名字", { exact: true }).fill(name);
  await page.getByLabel("性格", { exact: true }).fill("冷静、好奇");
  await page.getByLabel("外貌设定", { exact: true }).fill("银白短发、蓝眼睛、深蓝外套");
  await page.getByLabel("视觉风格", { exact: true }).fill("水彩童话");
  await page.getByRole("button", { name: "保存角色", exact: true }).click();
  await page.getByRole("heading", { name, exact: true }).waitFor();
  await page.getByLabel(`上传${name}的形象`, { exact: true }).setInputFiles(resolve(".venv-preview/ui-reference.png"));
  await page.getByRole("status").filter({ hasText: "角色形象已更新" }).waitFor();
  await page.screenshot({ path: `${output}/character-library.png`, fullPage: true });

  const created = await page.request.post(`${base}/api/v1/projects`, { data: { topic: "星野乘末班车寻找记忆", style: "水彩童话" } });
  const { id: pid } = await created.json();
  await page.goto(`${base}/#/project/${pid}`);
  await page.getByRole("button", { name: "角色库", exact: true }).click();
  const card = page.locator("article").filter({ has: page.getByRole("heading", { name, exact: true }) });
  await card.getByRole("button", { name: "加入项目", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "加入项目" }).waitFor();
  await page.getByRole("button", { name: "参考图", exact: true }).click();
  await page.getByLabel("上传参考图", { exact: true }).setInputFiles(resolve(".venv-preview/ui-reference.png"));
  await page.getByRole("status").filter({ hasText: "参考图已保存" }).waitFor();
  await page.screenshot({ path: `${output}/references.png`, fullPage: true });

  const generated = await page.request.post(`${base}/api/v1/projects/${pid}/generate`, { data: { auto_mode: true } });
  check(generated.ok(), await generated.text());
  const before = await waitProject(pid, v => v.latest_run?.status === "succeeded");
  check(before.content.characters[0].name === name, "角色库阵容未保留");
  check(before.content.reference_images.length === 1, "参考图未保留");
  await page.reload();
  await page.getByRole("button", { name: "分镜", exact: true }).click();
  await page.getByRole("button", { name: "编辑分镜", exact: true }).first().click();
  await page.getByLabel("场景", { exact: true }).fill("雨夜的末班车站");
  await page.getByLabel("对白", { exact: true }).fill("这一次，我要找回自己的记忆。");
  await page.getByRole("button", { name: "保存分镜", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "分镜已保存" }).waitFor();
  await page.getByText("脚本已修改，当前画面需要重新生成。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "重新生成本镜", exact: true }).first().click();
  await page.getByRole("status").filter({ hasText: "本镜画面已更新" }).waitFor();
  const after = await waitProject(pid, v => !v.content.shot_images[0].stale);
  check(after.content.shots[0].scene === "雨夜的末班车站", "分镜修改没有持久化");
  check(after.content.shot_images[0].url !== before.content.shot_images[0].url, "目标镜头未更新");
  check(JSON.stringify(after.content.shot_images.slice(1)) === JSON.stringify(before.content.shot_images.slice(1)), "其他镜头被重画");
  await page.screenshot({ path: `${output}/shot-editor.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/mobile-workbench.png`, fullPage: true });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "窄屏页面出现横向溢出");
  check(!errors.length, `浏览器错误：${errors.join("; ")}`);
  await writeFile(`${output}/report.json`, JSON.stringify({ passed: true, projectId: pid, baseUrl: base, browserErrors: errors }, null, 2));
  console.log(JSON.stringify({ passed: true, projectId: pid, screenshots: output }));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  await writeFile(`${output}/failure.html`, await page.content());
  throw error;
} finally { await browser.close(); }
