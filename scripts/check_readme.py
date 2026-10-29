"""检查 README 内部锚点、仓库文件链接、JSON 示例和关键配置覆盖。"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
readme = (ROOT / "README.md").read_text(encoding="utf-8")
errors: list[str] = []
anchors = set(re.findall(r'<a id="([^"]+)"></a>', readme))
links = re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)', readme)
for link in links:
    if link.startswith(("http://", "https://")):
        continue
    if link.startswith("#"):
        if link[1:] not in anchors:
            errors.append(f"Missing anchor: {link}")
    elif not (ROOT / link.split("#", 1)[0]).exists():
        errors.append(f"Missing file: {link}")

fences = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', readme, flags=re.MULTILINE | re.DOTALL)
for index, (language, text) in enumerate(fences, 1):
    if language.strip() == "json":
        try:
            json.loads(text)
        except ValueError as exc:
            errors.append(f"JSON block {index}: {exc}")

if len(re.findall(r'^```', readme, flags=re.MULTILINE)) != len(fences) * 2:
    errors.append("Unbalanced code fences")
if readme.count("<details>") != readme.count("</details>"):
    errors.append("Unbalanced details blocks")

tree = ast.parse((ROOT / "app/config.py").read_text(encoding="utf-8"))
settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
fields = [node.target.id for node in settings.body if isinstance(node, ast.AnnAssign)]
for field in fields:
    if f"`{field.upper()}`" not in readme:
        errors.append(f"Undocumented config field: {field}")

# 使用源码 AST 验证模型，避免仅根据旧文档维护节点总数。
state_tree = ast.parse((ROOT / "app/orchestration/state.py").read_text(encoding="utf-8"))
stage_expr = next(node.value for node in state_tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "ComicStage" for t in node.targets))
stages = [item.value for item in stage_expr.slice.elts]
for stage in stages:
    if f"`{stage}`" not in readme:
        errors.append(f"Undocumented stage: {stage}")

summary = {
    "lines": len(readme.splitlines()),
    "anchors": len(anchors),
    "local_and_anchor_links": sum(not link.startswith("http") for link in links),
    "code_blocks": len(fences),
    "mermaid_blocks": sum(language.strip() == "mermaid" for language, _ in fences),
    "documented_settings": len(fields),
    "documented_stages": len(stages),
    "errors": errors,
}
print(json.dumps(summary, ensure_ascii=True, indent=2))
if errors:
    raise SystemExit(1)
