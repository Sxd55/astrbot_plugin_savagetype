"""Verify README claims against code. Prints MISMATCH lines, exits nonzero on any."""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(r"C:\Users\24122\Desktop\astrbot_plugin_savagetype")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
APPJS = (ROOT / "pages" / "console" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "pages" / "console" / "index.html").read_text(encoding="utf-8")

issues: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(("OK   " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        issues.append(name)


# 1. commands in README table vs handlers in main.py
readme_cmds = re.findall(r"`/stype (\w+)", README)
code_cmds = re.findall(r'@stype\.command\("(\w+)"\)', MAIN)
for cmd in sorted(set(readme_cmds)):
    check(f"cmd /stype {cmd} has handler", f'@stype.command("{cmd}")' in MAIN)
for cmd in sorted(set(code_cmds)):
    check(f"handler {cmd} documented", f"`/stype {cmd}" in README)

# 2. llm tools
for tool in ("savagetype_recall", "savagetype_remember", "savagetype_navigate"):
    check(f"llm tool {tool}", f'llm_tool(name="{tool}")' in MAIN and f"`{tool}(" in README)

# 3. config keys in README section 5 vs schema
section5 = README.split("## 五、主要配置项")[1].split("\n---\n")[0]
readme_keys = set(re.findall(r"`([a-z][a-z0-9_]+)`", section5)) - {"off", "owner", "strict"}
print(f"INFO readme section5 keys found: {len(readme_keys)}")
for key in sorted(readme_keys):
    check(f"config {key} in schema", key in SCHEMA)
undoc = sorted(k for k in SCHEMA if k not in readme_keys)
print(f"INFO schema keys not in README section 5: {undoc}")

# 4. defaults quoted in README vs schema defaults
for key, spec in SCHEMA.items():
    default = spec.get("default")
    if isinstance(default, bool) or default in ("", None):
        continue
    # find "key ... 默认 X" nearby mentions: search whole README for `key` then 默认
    pattern = re.compile(rf"`{re.escape(key)}`[^`]*?默认\s*`?([0-9.]+|strict|off|owner)[`，,）)]?", re.S)
    for match in pattern.finditer(README):
        claimed = match.group(1)
        actual = str(default)
        check(f"default {key}=={claimed}", claimed == actual, f"schema={actual}")

# 4a. v4.4.0 模型策略配置必须在文档里出现
for key in (
    "quality_provider_id",
    "fast_provider_id",
    "fallback_provider_id",
    "daily_token_limit",
    "soft_token_limit",
    "single_call_token_cap",
):
    check(f"llm config {key} documented", f"`{key}`" in README)

# 5. panel tabs
for tab in ("记忆库", "事件", "人物档案", "诊断", "设置"):
    check(f"panel tab {tab}", f'data-tab="' in INDEX and tab in INDEX)
for route in ("overview", "events", "events/update", "entities", "config/save",
              "memory/review", "reviews/set", "pending", "facts/update", "sleep",
              "diagnostics", "export", "microscope"):
    check(f"panel api {route}", f'("{route}"' in MAIN)

# 6. versions
versions = {
    "README": re.search(r"当前版本 `v([^`]+)`", README).group(1),
    "__init__": re.search(r'__version__ = "([^"]+)"', (ROOT / "savagetype" / "__init__.py").read_text(encoding="utf-8")).group(1),
    "metadata": re.search(r"version: v([^\s]+)", (ROOT / "metadata.yaml").read_text(encoding="utf-8")).group(1),
}
base = versions["README"]
for where, ver in versions.items():
    check(f"version {where}=={base}", ver == base, f"{ver} vs {base}")
check("main register version", f'"{base}",' in MAIN)

# 7. architecture files
for fname in ("service.py", "store.py", "extract.py", "events.py", "pipeline.py",
              "contradiction.py", "slots.py", "retrieve.py", "inject.py", "learn.py",
              "archive.py", "profiles.py", "coexistence.py", "util.py"):
    check(f"arch file {fname}", (ROOT / "savagetype" / fname).is_file() and fname in README)
for fname in ("index.html", "app.js", "style.css", "shader.js"):
    check(f"panel file {fname}", (ROOT / "pages" / "console" / fname).is_file() and fname in README)

# 8. export tables
exported = ("facts", "timeline", "pending", "reviews", "profiles", "memory_reviews", "aliases", "events")
for table in exported:
    check(f"export table {table} documented", table in README.split("### 10.")[1].split("### 11.")[0])

# 9. config count claim
claimed_count = re.search(r"(\d+) 项配置", README)
check("config count claim", claimed_count and int(claimed_count.group(1)) == len(SCHEMA),
      f"README={claimed_count.group(1) if claimed_count else '?'} schema={len(SCHEMA)}")

# 10. settings groups in app.js vs README list
for group in ("总开关与采集", "抽取与整理", "检索与注入", "重要性与维护",
              "学习与人格草稿", "图片", "Embedding 与 Rerank", "外观", "事件记忆与隐私"):
    check(f"settings group {group}", group in APPJS or group in INDEX)

print()
if issues:
    print(f"{len(issues)} MISMATCHES")
    sys.exit(1)
print("ALL CHECKS PASSED")
