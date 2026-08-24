#!/usr/bin/env python3
"""emoji 门禁（团队 P0-1 硬规则）。

规则来源：前端不得用 emoji 当功能图标，一律用 SVG。
- emoji 在不同系统/字体下渲染差异极大，作为功能图标会导致 UI 不一致
- 无法按设计稿控制尺寸、颜色、线重
- 屏幕阅读器朗读出的是「派对拉炮」这类名字，可访问性差

扫描范围（与 CI 一致）：
    static/*.{js,html,css}    前端资源 —— emoji 会直接呈现给用户
    app/**/*.py               后端 —— 注释和接口文案都可能流向前端
    scripts/*.py              运维脚本
    tests/*.py
    *.md                      文档

用法：
    python scripts/check_no_emoji.py          # 有违规则 exit 1
    python scripts/check_no_emoji.py --list   # 只列出，始终 exit 0
"""
import re
import sys
from pathlib import Path

# team-lead 提供并已在本项目验证的范围（0 误报）。
# 覆盖 emoji 主平面 + 杂项符号 + 装饰符 + 变体选择器（U+FE0F）+
# ZWJ（U+200D，组合 emoji 用）+ U+20E3（keycap 组合符）。
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # 杂项符号与象形文字、补充符号
    "\u2600-\u26FF"           # 杂项符号（含 U+26A0 警告号）
    "\u2700-\u27BF"           # 装饰符号
    "\uFE00-\uFE0F"           # 变体选择器（emoji 呈现修饰）
    "\U0001F000-\U0001F02F"   # 麻将牌
    "\U0001F0A0-\U0001F0FF"   # 扑克牌
    "\U0001F100-\U0001F64F"   # 封闭字母数字补充、表情
    "\U0001F680-\U0001F6FF"   # 交通与地图
    "\U0001F900-\U0001F9FF"   # 补充符号与象形文字
    "\U0001FA00-\U0001FA6F"   # 棋类符号
    "\U0001FA70-\U0001FAFF"   # 扩展 A
    "\u200D"                  # 零宽连接符
    "\u20E3"                  # 组合用键帽
    "]"
)

ROOT = Path(__file__).resolve().parent.parent

# 排除：第三方产物、虚拟环境、演示副本。
# dist/ 与 demo/ 是本地演示资源，不进生产镜像也不受此规则约束。
EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "node_modules",
                "dist", "demo", ".workbuddy", ".ruff_cache", ".pytest_cache"}


def targets() -> list[Path]:
    patterns = [
        "static/*.js", "static/*.html", "static/*.css",
        "app/**/*.py", "scripts/*.py", "tests/*.py", "*.md",
    ]
    files: list[Path] = []
    for pat in patterns:
        for p in ROOT.glob(pat):
            if not p.is_file():
                continue
            if EXCLUDE_DIRS & set(p.relative_to(ROOT).parts):
                continue
            files.append(p)
    return sorted(set(files))


def main() -> int:
    list_only = "--list" in sys.argv
    violations: list[tuple[Path, int, str, str]] = []

    for f in targets():
        try:
            text = f.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"跳过 {f}: {e}", file=sys.stderr)
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in EMOJI_PATTERN.finditer(line):
                violations.append((f.relative_to(ROOT), lineno, m.group(), line.strip()))

    if not violations:
        print(f"emoji 门禁通过：扫描 {len(targets())} 个文件，0 违规")
        return 0

    print(f"emoji 门禁未通过：{len(violations)} 处违规\n")
    for path, lineno, ch, line in violations:
        print(f"  {path}:{lineno}  U+{ord(ch):04X} {ch!r}")
        print(f"      {line[:100]}")
    print(
        "\n处理方式："
        "\n  前端图标 → 改用 SVG（static/app.js 已有 26 处 SVG 图标可参照）"
        "\n  代码注释/文档 → 改用文字标记，如「注意：」「重要：」"
    )
    return 0 if list_only else 1


if __name__ == "__main__":
    sys.exit(main())
