"""文档校验：Python标准库和Node解析静态帮助；检查链接、默认值、版本及资产，不联网。"""
from __future__ import annotations

from collections import Counter
import argparse
import ast
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def configuration_guide(root: Path) -> str:
    """使用页面同源帮助和 schema；仅解析静态常量，不启动宿主。"""
    program = r"""
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const names = ["GATE_FIELDS","VISION_FIELDS","CONTEXT_FIELDS","BUDGET_FIELDS","REPLY_FIELDS",
  "EXPRESSION_FIELDS","INTEGRATION_FIELDS","UNDERSTANDING_FIELDS","RECALL_FIELDS",
  "MENTION_FIELDS","OUTAGE_FIELDS","PRIMARY_TIMEOUT_FIELD","OUTAGE_WATCH_JUDGE","OUTAGE_WATCH_UNDERSTAND",
  "OUTAGE_WATCH_REPLY","FIELD_HELP","SWITCH_HELP","MODE_CONFIG_HELP","FIELD_GROUPS"];
let code = "";
for (const name of names) {
  const start = source.indexOf("  const " + name + " =");
  if (start < 0) throw Error("Missing " + name);
  const tail = source.slice(start);
  const end = tail.search(/\n  [}\]];/);
  if (end < 0) throw Error("Missing static terminator " + name);
  code += tail.slice(0, end + 6) + "\n";
}
const loop = source.slice(source.indexOf('  for (const [key, model] of Object.entries({classifier:'),
  source.indexOf("  function fieldHelp(key)"));
code += loop;
code += "\nJSON.stringify({help:FIELD_HELP,switches:SWITCH_HELP,mode:MODE_CONFIG_HELP,groups:FIELD_GROUPS})";
console.log(vm.runInNewContext(code, {}, {timeout:1000}));
"""
    page = root / "pages/guard/index.html"
    data = json.loads(subprocess.check_output(["node", "-e", program, str(page)], text=True))
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    controls = {f["key"]: (g["title"], f) for g in data["groups"] for f in g["fields"]}
    lines = ["### 逐项设置与用法", "",
             "每项同时可在AstrBot插件配置页按字段名设置。表中位置是LH控制台入口；",
             "已有保存值优先于默认值。数值范围由页面、保存校验与运行时共同约束，",
             "互相关联的时限另见下文。提示词字段可在“预置提示词”编辑并恢复预置。", "",
             "| 配置项 | 控制台位置 / 作用与例子 |", "| --- | --- |"]

    def cell(value):
        return str(value).replace("|", "&#124;").replace("\n", "<br>")

    for key, item in schema.items():
        title, control = controls.get(key, ("插件配置", {}))
        if key.startswith("prompt_"):
            title = "回复与私聊 → 预置提示词"
        elif key.startswith("coupling_"):
            title = "回复与私聊 → 功能耦合"
        elif any(word in key for word in ("blacklist", "whitelist", "graylist")):
            title = "回复与私聊 → 消息名单"
        if key == "gate_mode":
            help_ = data["mode"]
            title = "判定设置 → 闸门模式"
        else:
            help_ = data["help"].get(key, {})
        switch = data["switches"].get(key, {})
        description = help_.get("desc") or item.get("hint") or item["description"]
        details = f"位置：{switch.get('setup') or title}。{description}"
        if switch:
            details += f" 开启：{switch['on']} 关闭：{switch['off']}"
        elif item["type"] == "bool":
            details += " 开启执行上述功能；关闭停止该项功能，其他独立开关仍分别生效。"
        if "min" in control:
            details += f" 页面范围{control['min']}–{control['max']}{control.get('unit', '')}。"
        example = help_.get("ex") or (
            "开启后在此编辑说明中关联的参数，再保存。"
            if item["type"] == "bool" else
            "在此选择已注册模型；留空行为以本项说明为准。"
            if "provider_id" in key else
            "填写本项要求的说明，保存后作用于后续请求；恢复预置可撤销自定义。"
            if key.startswith("prompt_") else
            "先使用上表默认值，按本项单位调整并保存，随后用试跑核对。")
        lines.append(f"| `{key}` | {cell(details)} 例：{cell(example)} |")
    lines += ["", "### 生图逐项设置", "",
              "入口为“回复与私聊 → 原生配置 → AI生图”。各字段保存至独立生图配置，",
              "先选择实际支持生图的连接；每天次数按全插件共用。", "",
              "| 配置项 | 作用、开启关闭效果与用法 |", "| --- | --- |"]
    fields = literal_assignment(root / "images/guard_image_settings.py", "FIELDS")
    for key, (label, kind, hint) in fields.items():
        effect = hint
        if kind == "bool" and "关闭" not in hint:
            effect += "关闭停止本项功能；其他独立设置保持各自作用。"
        lines.append(f"| `{key}` / {label} | {cell(effect)} 在上述入口设置并保存。 |")
    return "\n".join(lines).rstrip()


def prose(text: str) -> str:
    """忽略代码围栏，避免把代码样例当成文档链接或标题。"""
    return re.sub(r"(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$", "", text)


def anchors(text: str) -> set[str]:
    result: set[str] = set()
    seen: Counter[str] = Counter()
    for heading in re.findall(r"(?m)^#{1,6} +(.+?) *#* *$", prose(text)):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        suffix = f"-{seen[slug]}" if seen[slug] else ""
        seen[slug] += 1
        result.add(slug + suffix)
    result.update(re.findall(r'(?:id|name)=["\']([^"\']+)["\']', text))
    return result


def literal_assignment(path: Path, name: str):
    """解析源码常量，不导入插件或启动宿主。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "dict":
            return {keyword.arg: ast.literal_eval(keyword.value) for keyword in node.value.keywords}
        return ast.literal_eval(node.value)
    raise ValueError(f"{path.name} 缺少 {name}")


def marked_table(text: str, marker: str, failures: list[str]) -> str:
    match = re.search(rf"<!-- {marker}:start -->(.*?)<!-- {marker}:end -->", text, re.S)
    if not match:
        failures.append(f"缺少配置表标记: {marker}")
        return ""
    return match[1]


def check(root: Path) -> list[str]:
    failures = []
    docs = [root / "README.md", root / "README.en.md", root / "CHANGELOG.md",
            *sorted((root / "docs").rglob("*.md"))]
    for path in docs:
        if not path.is_file():
            failures.append(f"缺少 {path.relative_to(root)}")
            continue
        text = path.read_text(encoding="utf-8")
        if any(line.rstrip() != line for line in text.splitlines()):
            failures.append(f"{path.relative_to(root)}: 行末空白")
        targets = re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", prose(text))
        targets += re.findall(r'(?:src|href)=["\']([^"\']+)["\']', prose(text))
        for raw in targets:
            target = urlsplit(raw.strip().split(' "', 1)[0].strip("<>"))
            if target.scheme or target.netloc:
                continue
            destination = (path.parent / unquote(target.path)).resolve() if target.path else path
            if not destination.is_relative_to(root):
                failures.append(f"{path.relative_to(root)}: 链接越出仓库 {raw}")
            elif not destination.exists():
                failures.append(f"{path.relative_to(root)}: 无效路径 {raw}")
            elif target.fragment and destination.suffix == ".md":
                if unquote(target.fragment) not in anchors(destination.read_text(encoding="utf-8")):
                    failures.append(f"{path.relative_to(root)}: 无效锚点 {raw}")

    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    table = (root / "docs/configuration.md").read_text(encoding="utf-8")
    generated = configuration_guide(root)
    if marked_table(table, "field-usage", failures).strip() != generated:
        failures.append("逐项用法已过期：运行 python scripts/check_docs.py --write")
    rows = re.findall(r"(?m)^\| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$",
                      marked_table(table, "schema-defaults", failures))
    listed = {}
    for key, default, title in rows:
        if key in listed:
            failures.append(f"配置表重复字段: {key}")
        try:
            listed[key] = (json.loads(default), title.strip())
        except ValueError:
            failures.append(f"配置表默认值不是 JSON: {key}")
    if set(listed) != set(schema):
        failures.append(f"配置表字段差异: {sorted(set(listed) ^ set(schema))}")
    for key in set(listed) & set(schema):
        value, title = listed[key]
        # bool 与 int 在 Python 中可能相等；JSON 序列化比较保留类型。
        if json.dumps(value, ensure_ascii=False) != json.dumps(schema[key]["default"], ensure_ascii=False):
            failures.append(f"配置默认值过期: {key}")
        if title != schema[key]["description"]:
            failures.append(f"配置标题过期: {key}")

    # 独立生图配置不在宿主schema中，直接以源码DEFAULTS核对。
    defaults = literal_assignment(root / "images/guard_image_settings.py", "DEFAULTS")
    image_rows = re.findall(r"(?m)^\| `([^`]+)` \| `([^`]+)` \|",
                            marked_table(table, "image-defaults", failures))
    image_listed = {}
    for key, default in image_rows:
        if key in image_listed:
            failures.append(f"生图配置表重复字段: {key}")
        try:
            image_listed[key] = json.loads(default)
        except ValueError:
            failures.append(f"生图默认值不是JSON: {key}")
    if set(image_listed) != set(defaults):
        failures.append(f"生图配置表字段差异: {sorted(set(image_listed) ^ set(defaults))}")
    for key in set(image_listed) & set(defaults):
        if json.dumps(image_listed[key]) != json.dumps(defaults[key]):
            failures.append(f"生图默认值过期: {key}")
    native_fields = literal_assignment(root / "integrations/guard_native_settings.py", "FIELDS")
    section = re.search(r"## AstrBot 原生字段\n(.*?)\n## AstrBot 原生分段", table, re.S)
    native_rows = re.findall(r"(?m)^\| ([a-z0-9_]+) \| ([a-z0-9_.]+) \|", section[1] if section else "")
    documented_native = dict(native_rows)
    if documented_native != {key: value[0] for key, value in native_fields.items()}:
        failures.append("原生字段及保存路径已变化，请同步配置参考")

    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    required = {"name", "display_name", "short_desc", "desc", "version", "author",
                "repo", "help", "astrbot_version", "social_link", "tags"}
    keys = set(re.findall(r"(?m)^([a-z_]+):", metadata))
    if required - keys:
        failures.append(f"缺少元数据字段: {sorted(required - keys)}")
    version_match = re.search(r"(?m)^version: (\d+\.\d+\.\d+)$", metadata)
    if not version_match:
        failures.append("metadata.version 不是语义版本")
    else:
        version = version_match[1]
        for path in (root / "README.md", root / "CHANGELOG.md", root / "console/guard_webui.py"):
            if version not in path.read_text(encoding="utf-8"):
                failures.append(f"{path.name} 缺少当前版本 {version}")

    for filename, dimensions in (("logo.png", (256, 256)), (".github/img/Banner.png", (1200, 360))):
        asset = root / filename
        header = asset.read_bytes()[:24] if asset.is_file() else b""
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            failures.append(f"缺少有效 PNG: {filename}")
        elif struct.unpack(">II", header[16:24]) != dimensions:
            failures.append(f"图片尺寸不匹配: {filename}")
    if list(root.glob("test_*.py")) or not list((root / "tests").glob("test_*.py")):
        failures.append("测试应集中在 tests/")
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        path = ROOT / "docs/configuration.md"
        text = path.read_text(encoding="utf-8")
        pattern = r"(<!-- field-usage:start -->\n).*?(<!-- field-usage:end -->)"
        assert re.search(pattern, text, re.S), "Missing field-usage markers"
        path.write_text(re.sub(pattern, lambda m: m[1] + configuration_guide(ROOT)
                              + "\n" + m[2], text, flags=re.S), encoding="utf-8")
    errors = check(ROOT)
    for error in errors:
        print(f"[FAIL] {error}")
    if not errors:
        count = len(json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8")))
        image_count = len(literal_assignment(ROOT / "images/guard_image_settings.py", "DEFAULTS"))
        print(f"[PASS] 文档链接、{count}+{image_count} 项默认值、原生字段、版本、图片及 tests/ 布局")
    sys.exit(bool(errors))
