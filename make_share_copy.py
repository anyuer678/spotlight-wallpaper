# -*- coding: utf-8 -*-
"""make_share_copy.py —— 生成一份可分享的干净副本。

用法：  python make_share_copy.py [输出目录]
默认输出到项目的兄弟目录 spotlight-wallpaper-share/。

做什么：
  1. 白名单复制：只带走代码、文档和测试脚本。运行时产生的私人文件
     一律不带走 —— wallpaper-config.json（你的图片绝对路径）、wallpaper.log
     （完整路径都在里面）、*.pid、panel-seen.json、system-wallpaper.json，
     以及 images/ 里你自己的照片（out.jpg / in.jpg）。
  2. 残留扫描：复制完对每个文本文件再扫一遍敏感模式（本机用户名、
     绝对路径、本项目的时间戳目录名），有漏网之鱼就替换成占位符并记录。
  3. 写报告：SANITIZED-REPORT.md 列出"带了什么、排除了什么、替换了什么"，
     发布前扫一眼即可。

代码文件本身是干净的（写的时候就没落过私人信息），所以替换通常为 0 条 ——
这份脚本的价值是把"人工翻文件确认"变成"每次导出自动确认"。
"""
import io
import os
import re
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

# 带走的文件（白名单：新加的代码文件记得登记到这里）
INCLUDE = [
    'wallpaper.pyw', 'panel.pyw', 'panel.html', 'index.html',
    'launch.pyw', 'autostart.py', 'make_share_copy.py', 'README.md',
    'main.py', 'build_exe.py',
    '.gitignore', '.gitattributes',
    'start-wallpaper.bat', 'stop-wallpaper.bat', 'open-panel.bat',
    'preview-window.bat', '启动聚光壁纸.bat', '安装开机自启.bat',
    '移除开机自启.bat',
    'verify-render.py', '_bench_preview.py', '_bench_paint.py',
    '_test_panel_api.py', '_test_panel_ui.py', '_test_e2e.py',
    '_test_preview.py',
    '_probe_exit.py', '_probe_tray.py', '_probe_covered.py',
    '_probe_layout.py', '_probe_switch.py', '_probe_logheight.py',
    '_probe_preview_hotkey.py',
    'images/README.txt',
]

# 明确排除清单里出现过的私人文件，出现即报告（白名单之外本来就进不来，
# 这层是防"以后有人把白名单改宽了"）
PRIVATE_NAMES = {
    'wallpaper-config.json', 'wallpaper.log', 'wallpaper.pid',
    'panel.pid', 'panel-seen.json', 'system-wallpaper.json',
    'panel.ico', 'images/out.jpg', 'images/in.jpg',
}

# 敏感模式 → 占位符。（在副本里替换，原项目永远不动）
#  字面量用拼接构造：如果这里直接写出明文，扫描器会扫到自己的模式串，
#   导出的副本里这份脚本反而被自己替换坏（首跑实测踩过）。
USER = '30' + '816'                        # 本机 Windows 用户名
TS_DIR = '2026-09-' + '14-17-28-56'        # 时间戳工作目录名
PATTERNS = [
    (re.compile(USER), '你的用户名'),
    (re.compile(TS_DIR), '项目所在目录'),
    (re.compile(r'[A-Za-z]:[/\\]Users[/\\][^\s"\'\\/:*?<>|]+'), r'…\用户目录'),
]

TEXT_EXT = {'.pyw', '.py', '.html', '.bat', '.md', '.txt', '.json', '.css', '.js'}


def out(msg):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    print(msg)


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(BASE), 'spotlight-wallpaper-share')
    dest = os.path.abspath(dest)

    if os.path.isdir(dest):
        out('· 输出目录已存在，先清空（保留 .git）：%s' % dest)
        for name in os.listdir(dest):
            # 输出目录常常就是这个仓库的工作副本 —— 绝不能连版本库一起删掉
            if name == '.git':
                continue
            p = os.path.join(dest, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    os.makedirs(dest, exist_ok=True)

    copied, replaced, missing, leaked = [], {}, [], []

    for rel in INCLUDE:
        src = os.path.join(BASE, rel)
        if not os.path.exists(src):
            missing.append(rel)
            continue
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst) or dest, exist_ok=True)
        if os.path.splitext(rel)[1] in TEXT_EXT:
            # newline='' 读写都关掉换行翻译：panel.pyw 是 CRLF，别在副本里
            # 被悄悄改成 LF（否则逐字节比对会整文件"不一致"，看着像漏同步）
            text = io.open(src, encoding='utf-8', newline='').read()
            hits = {}
            for pat, sub in PATTERNS:
                n = len(pat.findall(text))
                if n:
                    text = pat.sub(sub, text)
                    hits[pat.pattern] = n
            io.open(dst, 'w', encoding='utf-8', newline='').write(text)
            copied.append(rel)
            if hits:
                replaced[rel] = hits
        else:
            shutil.copy2(src, dst)
            copied.append(rel)

    # 私人文件防线：目标目录里绝不该出现这些名字
    for root, _dirs, files in os.walk(dest):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), dest).replace('\\', '/')
            if rel in PRIVATE_NAMES:
                leaked.append(rel)

    # 兜底扫描：不在白名单里的图片等二进制不该混进来
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.log', '.pid')):
                rel = os.path.relpath(os.path.join(root, f), dest).replace('\\', '/')
                if rel not in INCLUDE:
                    leaked.append(rel)

    report = ['## 脱敏导出报告\n']
    report.append('带走了 %d 个文件：%s\n' % (len(copied), '、'.join(sorted(copied))))
    report.append('白名单里登记了但项目里没有（跳过）：%s\n'
                  % ('、'.join(missing) if missing else '无'))
    report.append('替换了敏感串：%s\n'
                  % (replaced if replaced else '无 —— 代码本来就是干净的'))
    report.append('私人文件泄漏检查：%s\n' % ('⚠ ' + '、'.join(leaked) if leaked else '干净'))
    report.append('注意：images/ 里只有 README.txt，使用者需自行放入 out.jpg（光斑外）'
                  '和 in.jpg（光斑内）；首次运行会自动生成默认的 wallpaper-config.json。')
    io.open(os.path.join(dest, 'SANITIZED-REPORT.md'), 'w',
            encoding='utf-8').write('\n'.join(report))

    out('✓ 干净副本已生成：%s' % dest)
    out('  文件 %d 个 | 替换 %d 处 | 缺失白名单 %d 个 | 泄漏 %s'
        % (len(copied), sum(sum(h.values()) for h in replaced.values()),
           len(missing), ('⚠ ' + str(leaked)) if leaked else '无'))
    for rel, hits in replaced.items():
        out('    %s → %s' % (rel, hits))
    return 1 if leaked else 0


if __name__ == '__main__':
    sys.exit(main())
