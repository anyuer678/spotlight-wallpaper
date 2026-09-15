# -*- coding: utf-8 -*-
"""build_exe.py —— 把整个项目打包成单个 exe。

三件事：

  1. 把 .pyw 复制成 .py。PyInstaller 只把 .py / .pyc 当模块找，而本项目里
     wallpaper.pyw / panel.pyw / launch.pyw 都是 .pyw —— 源码方式跑时这个后缀
     有意义（双击不弹黑窗），打包时没有。复制一份镜像给它分析，主项目一个
     字节都不用改。

  2. 调 PyInstaller 打成单文件、窗口子系统、带图标，并把 panel.html 作为
     数据文件塞进包里（打包版没有源码目录可读，界面文件得跟着走）。

  3. 报告产物在哪、多大。

为什么是单文件而不是文件夹：这程序是"装一次、常驻后台"，分发时一个 exe 最
省心；单文件首启要解包、慢两三秒，而开机自启那条路本来就在登录后才挂壁纸，
感觉不出来。

用法：
    python build_exe.py            打包到 dist/
    python build_exe.py --keep     保留中间产物（build/ 与镜像目录），排错用

需要当前 Python 里装有 pyinstaller / pillow / pywin32。
"""
import importlib.util
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))
MIRROR = os.path.join(BASE, '.build_pkg')
BUILD = os.path.join(BASE, 'build')
DIST = os.path.join(BASE, 'dist')
ICO = os.path.join(BASE, 'panel.ico')
ENTRY = os.path.join(BASE, 'main.py')
APP = 'SpotlightWallpaper'

# 源文件 → 包内模块名（模块名必须与 main.py 里 import 的一致）
MODULES = (('wallpaper.pyw', 'wallpaper'),
           ('panel.pyw', 'panel'),
           ('launch.pyw', 'launch'))

# main.py 里那几处 import 写在 frozen 分支里，PyInstaller 静态分析能看见；
# 仍然显式点一遍名，是因为动态 import 一旦被漏掉，报错要到运行时才炸。
HIDDEN = ('wallpaper', 'panel', 'launch', 'autostart')


def out(msg):
    print(msg, flush=True)


def check():
    r = subprocess.run([sys.executable, '-m', 'PyInstaller', '--version'],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if r.returncode != 0:
        out('x 当前 Python 里没有 PyInstaller：%s' % sys.executable)
        out('  先装：pip install pyinstaller pillow pywin32')
        return False
    out('· PyInstaller %s' % r.stdout.strip())
    warn_fat_env()
    return True


def warn_fat_env():
    """提醒"别拿日常那个 Python 打包"。

    日常环境里往往装着 numpy / scipy / opencv，而这个项目一个都不 import ——
    可 PyInstaller 会被某些 hook 带着把它们整包塞进去。实测同一份代码：
    干净 venv 出来 20 MB，系统 Python 出来 34 MB，多出来的全是没人用的东西。
    体积事小，把一大坨用不上的解析器塞进发行物事大。
    """
    fat = [m for m in ('numpy', 'scipy', 'cv2', 'matplotlib', 'pandas',
                       'pyarrow', 'sklearn')
           if importlib.util.find_spec(m)]
    if not fat:
        return
    out('! 这个环境里装着 %s。' % '、'.join(fat))
    out('  本项目一个都不 import，它们进 exe 只是白胖（20 MB → 34 MB 实测过）。')
    out('  想瘦下来：用一个只装 pyinstaller/pillow/pywin32 的虚拟环境打包。')


def make_mirror():
    if os.path.isdir(MIRROR):
        # 只清自己建的那个目录，并且先确认它真的只是镜像 —— 这个项目的导出
        # 脚本就是栽在"整目录删掉、把 .git 一起带走"上，这里不重蹈。
        if os.path.isdir(os.path.join(MIRROR, '.git')):
            out('x 镜像目录里有 .git，拒绝清理：%s' % MIRROR)
            return False
        shutil.rmtree(MIRROR)
    os.makedirs(MIRROR)
    for src_name, mod in MODULES:
        src = os.path.join(BASE, src_name)
        if not os.path.exists(src):
            out('x 缺文件：%s' % src)
            return False
        shutil.copyfile(src, os.path.join(MIRROR, mod + '.py'))
        out('· 镜像 %s → .build_pkg/%s.py' % (src_name, mod))
    return True


def build():
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--noconfirm', '--clean',
           '--onefile', '--windowed',
           '--name', APP,
           '--distpath', DIST, '--workpath', BUILD, '--specpath', BUILD,
           '--paths', MIRROR, '--paths', BASE,
           # 注意：--add-data 的源路径是按 spec 所在目录（--specpath）解析的，
           # 不是当前工作目录 —— 给相对路径它会跑到 build/ 里去找，找不到就
           # 报 "Unable to find ... when adding binary and data files"。
           '--add-data', os.path.join(BASE, 'panel.html') + os.pathsep + '.']
    for m in HIDDEN:
        cmd += ['--hidden-import', m]
    if os.path.exists(ICO):
        cmd += ['--icon', ICO]
    else:
        out('· 没有 panel.ico，这次不带图标（首次运行面板时会自动生成）')
    cmd.append(ENTRY)

    out('· 开始打包，首次要一两分钟...')
    return subprocess.run(cmd, cwd=BASE).returncode == 0


def seed_dist():
    """在 dist 里放好 images/ 骨架。

    拿到 exe 的人第一件事就是往里放两张图，而这个目录原本要等面板上传过一次
    才存在 —— 空目录看不到，没有目录更看不到。顺手带一份说明进去。
    """
    d = os.path.join(DIST, 'images')
    os.makedirs(d, exist_ok=True)
    src = os.path.join(BASE, 'images', 'README.txt')
    if os.path.exists(src):
        shutil.copyfile(src, os.path.join(d, 'README.txt'))


def report():
    exe = os.path.join(DIST, APP + '.exe')
    if not os.path.exists(exe):
        out('x 没找到产物：%s' % exe)
        return False
    mb = os.path.getsize(exe) / 1024.0 / 1024.0
    out('')
    out('· 完成：%s' % exe)
    out('  %.1f MB，双击即用（第一次启动会慢几秒，它在解包）' % mb)
    out('  壁纸要用的图放 images/ 里：out.jpg 是光斑外，in.jpg 是光斑内')
    return True


def main():
    keep = '--keep' in sys.argv[1:]
    if not check() or not make_mirror():
        return 1
    try:
        ok = build()
    finally:
        if not keep and os.path.isdir(MIRROR):
            shutil.rmtree(MIRROR, ignore_errors=True)
    if not ok:
        out('x 打包失败（加 --keep 保留中间产物再看）')
        return 1
    seed_dist()
    return 0 if report() else 1


if __name__ == '__main__':
    sys.exit(main())
