# -*- coding: utf-8 -*-
"""main.py —— 打包成 exe 之后的统一入口。

源码方式跑（`pythonw wallpaper.pyw` 那一套）仍旧各走各的脚本，这个入口是给
"整个程序打成一个 exe"用的：exe 里不再有 .pyw 文件，所有"再拉一个进程"的
地方都改成"让 exe 换个角色重跑一遍自己"，靠的就是这里的命令行分发。

    SpotlightWallpaper.exe                     起壁纸 + 开面板（双击的默认行为）
    SpotlightWallpaper.exe --no-panel          只起壁纸（开机自启走这条）
    SpotlightWallpaper.exe --window            小窗口预览（安全模式）
    SpotlightWallpaper.exe --full              全屏预览
    SpotlightWallpaper.exe --wallpaper         壁纸进程本体（内部用）
    SpotlightWallpaper.exe --panel             面板进程本体（内部用）
    SpotlightWallpaper.exe --serve-only        面板服务模式（无窗口，测试用）
    SpotlightWallpaper.exe --autostart install|remove|status
    SpotlightWallpaper.exe --version

--wallpaper / --panel 是"角色"，角色之外的参数原样透传给那个模块的 main()，
所以 --window、--serve-only --port 8791 这些老参数照旧能用。

只 import 被点名的那个角色：wallpaper / panel 两个模块一加载就会把整套渲染、
Tk、HTTP server 都拖进来，全 import 一遍会让壁纸进程白白背上面板那一大坨。
"""
import os
import sys

BASE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, 'frozen', False)
        else os.path.dirname(os.path.abspath(__file__)))

# 打包成 exe 是窗口子系统：双击运行时 sys.stdout / sys.stderr 就是 None，任何
# print 都会抛 AttributeError，把一句无害的输出变成崩溃。先垫上空的。
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# 壁纸自己的模式参数：出现它们就说明用户想要的是壁纸，不是启动器。
WALLPAPER_FLAGS = ('--window', '--full', '--selftest', '--bench', '--restarted')
PANEL_FLAGS = ('--serve-only', '--port')

USAGE = __doc__


def _load(name):
    """取角色模块：打包后在包内，源码方式跑时在磁盘上（.pyw）。

    打包分支里写成一个个显式 import，是为了让 PyInstaller 能静态看到这四
    个模块 —— 用 __import__(name) 这种动态写法它认不出来，会当成缺模块。
    """
    if getattr(sys, 'frozen', False):
        if name == 'wallpaper':
            import wallpaper
            return wallpaper
        if name == 'panel':
            import panel
            return panel
        if name == 'launch':
            import launch
            return launch
        if name == 'autostart':
            import autostart
            return autostart
        raise RuntimeError('未知角色：%s' % name)
    import importlib.util
    fn = {'wallpaper': 'wallpaper.pyw', 'panel': 'panel.pyw',
          'launch': 'launch.pyw', 'autostart': 'autostart.py'}[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE, fn))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _role(argv):
    if '--wallpaper' in argv or any(f in argv for f in WALLPAPER_FLAGS):
        return 'wallpaper'
    if '--panel' in argv or any(f in argv for f in PANEL_FLAGS):
        return 'panel'
    if '--autostart' in argv:
        return 'autostart'
    return 'launch'


def _autostart(argv):
    i = argv.index('--autostart')
    action = argv[i + 1] if i + 1 < len(argv) else 'status'
    mod = _load('autostart')
    fn = {'install': mod.install, 'remove': mod.remove,
          'status': mod.status}.get(action)
    if fn is None:
        sys.stdout.write('用法: --autostart install|remove|status\n')
        return 2
    rc = fn()
    if action in ('install', 'remove'):
        mod.inform(rc, action)          # 双击场景下没有控制台，用气泡框说话
    return rc


def main():
    argv = sys.argv[1:]

    if '--version' in argv or '-V' in argv:
        # 版本号的唯一出处是 panel.pyw 的 VERSION —— 不在这儿再抄一份，
        # 免得哪天两处对不上。
        sys.stdout.write('聚光壁纸 %s\n' % _load('panel').VERSION)
        return 0
    if '--help' in argv or '-h' in argv or '/?' in argv:
        sys.stdout.write(USAGE)
        return 0

    role = _role(argv)
    if role == 'wallpaper':
        _load('wallpaper').main()
        return 0
    if role == 'panel':
        return _load('panel').main(argv) or 0
    if role == 'autostart':
        return _autostart(argv)
    return _load('launch').main() or 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        try:
            with open(os.path.join(BASE, 'wallpaper.log'), 'a',
                      encoding='utf-8') as f:
                f.write('入口崩溃: %s\n' % traceback.format_exc())
        except OSError:
            pass
        sys.exit(1)
