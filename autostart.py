# -*- coding: utf-8 -*-
"""autostart.py —— 开机自启的安装 / 移除 / 查询。

用法（由 安装开机自启.bat / 移除开机自启.bat 调用，也可命令行直接用）：

    python autostart.py install    # 装一个指向 launch.pyw --no-panel 的快捷方式
    python autostart.py remove     # 移除
    python autostart.py status     # 只查询，不动任何东西

装的是 shell:startup（启动文件夹）里的 .lnk：
  - 任务管理器 → 启动应用 里能看到、能一键关掉，比注册表/计划任务透明；
  - 目标是 pythonw + launch.pyw --no-panel：登录后只起壁纸，不弹面板
    （桌面是给壁纸的，面板要调再 Ctrl+Alt+P）；
  - 快捷方式带着项目目录的绝对路径，项目挪走后重装一次即可。

pywin32 是本机既有依赖（panel.pyw 的托盘在用），这里用它拿 COM 的
WScript.Shell —— 不自己拼 .lnk 二进制，也不引入新依赖。
"""
import os
import sys

FROZEN = getattr(sys, 'frozen', False)
if FROZEN:
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
LNK_NAME = '聚光壁纸.lnk'
LAUNCH = os.path.join(BASE, 'launch.pyw')


def out(msg):
    # 控制台多半是 GBK，先把自己调成 UTF-8，✓/✗ 和中文才打得出来
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    print(msg)


def find_pythonw():
    exe = sys.executable
    if os.path.basename(exe).lower().startswith('python.exe'):
        cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(cand):
            return cand
    return exe


def shell():
    import win32com.client          # noqa: 延迟导入，status 失败也要能给话
    return win32com.client.Dispatch('WScript.Shell')


def startup_dir():
    return shell().SpecialFolders('Startup')


def lnk_path():
    return os.path.join(startup_dir(), LNK_NAME)


def install():
    # 打包成 exe 之后没有 pythonw + launch.pyw 这条链了，"目标"就是 exe
    # 自己，靠 --no-panel 这个参数换角色；没打包时仍旧指向 launch.pyw。
    if FROZEN:
        target, args = sys.executable, '--no-panel'
    else:
        if not os.path.exists(LAUNCH):
            out('✗ 找不到 %s' % LAUNCH)
            return 1
        target, args = find_pythonw(), '"%s" --no-panel' % LAUNCH
    ws = shell()
    lnk = ws.CreateShortcut(lnk_path())
    lnk.TargetPath = target
    lnk.Arguments = args
    lnk.WorkingDirectory = BASE
    ico = os.path.join(BASE, 'panel.ico')
    if os.path.exists(ico):
        lnk.IconLocation = ico
    lnk.Description = '聚光壁纸：开机自动挂到桌面（不弹面板）'
    lnk.Save()
    out('✓ 已安装开机自启：%s' % lnk_path())
    return 0


def remove():
    p = lnk_path()
    if os.path.exists(p):
        os.remove(p)
        out('✓ 已移除开机自启（%s）' % p)
    else:
        out('· 本来就没有开机自启，什么都不用做')
    return 0


def status():
    p = lnk_path()
    exists = os.path.exists(p)
    out('启动文件夹：%s' % os.path.dirname(p))
    out('开机自启：%s' % ('已安装 → %s' % p if exists else '未安装'))
    return 0 if exists else 3          # 3 = 未安装（给脚本判断用）


def inform(rc, action):
    """双击 .bat 场景下的反馈：没有控制台，用 Tk 弹个气泡框说人话。"""
    if os.environ.get('AUTOSTART_QUIET'):
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if action == 'install':
            msg = ('已安装开机自启：开机后自动挂上壁纸，不弹面板。\n'
                   '随时可以双击「移除开机自启.bat」取消，'
                   '或在任务管理器 → 启动应用里关掉。')
        else:
            msg = '已移除开机自启。'
        (messagebox.showinfo if rc == 0 else messagebox.showerror)(
            '聚光壁纸', msg)
        root.destroy()
    except Exception:
        pass                            # 没有桌面会话（ssh 之类）就只看命令行


if __name__ == '__main__':
    action = (sys.argv[1] if len(sys.argv) > 1 else 'status').lstrip('-')
    try:
        fn = {'install': install, 'remove': remove, 'status': status}[action]
    except KeyError:
        out('用法: python autostart.py install|remove|status')
        sys.exit(2)
    rc = fn()
    if action in ('install', 'remove'):
        inform(rc, action)
    sys.exit(rc)
