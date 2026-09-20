# -*- coding: utf-8 -*-
"""可移植 CI 检查：不依赖 win32api / Playwright / 桌面 API。

在 Linux CI 上必须真实通过；用于证明仓库未处于完全不可测状态。
完整面板/e2e 用例仅在 Windows 本机执行。
"""

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
fails = []


def check(cond, msg):
    if cond:
        print("ok", msg)
    else:
        print("FAIL", msg)
        fails.append(msg)


# LICENSE
check((ROOT / "LICENSE").is_file(), "LICENSE exists")

# 关键脚本语法可解析
for name in ("wallpaper.pyw", "panel.pyw"):
    p = ROOT / name
    if not p.is_file():
        fails.append(f"missing {name}")
        continue
    try:
        ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        print("ok syntax", name)
    except SyntaxError as e:
        fails.append(f"syntax {name}: {e}")

# workflow 文件存在
wf = ROOT / ".github/workflows/ci.yml"
check(wf.is_file(), "ci.yml exists")

# 已知测试文件存在（内容可 Windows-only，但文件应在）
for tname in ("_test_panel_ui.py", "_test_panel_api.py", "_test_preview.py"):
    check((ROOT / tname).is_file(), f"{tname} present")

# win32 依赖仅允许出现在 Windows 相关脚本中被 CI 显式跳过
# （这里只做文档性检查：panel.pyw 确实引用 win32api，则 Linux 不得硬跑）
panel = (ROOT / "panel.pyw").read_text(encoding="utf-8", errors="replace")
if "win32api" in panel:
    print("ok panel.pyw uses win32api -> Linux CI must skip panel tests")
else:
    print("note: panel.pyw does not reference win32api")

if fails:
    print("PORTABLE CI FAILED", fails)
    sys.exit(1)
print("PORTABLE CI OK")
