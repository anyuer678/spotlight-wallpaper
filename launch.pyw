# -*- coding: utf-8 -*-
"""launch.pyw —— 聚光壁纸统一启动入口。

设计目标（为什么不用两个 .bat 分头启动）：
  用户只想"把这套东西跑起来"，不该背"先开壁纸还是先开面板、哪个已经
  开着了"的心智负担。这一个入口负责把缺的补上、在的抬起来：

    壁纸：单实例互斥量（wallpaper.pyw 自己的 SpotlightWallpaper_SingleInstance）
          在 → 不动它；不在 → 拉起，并清掉可能骗人的旧 pid 文件。
    面板：panel.pid 活着 → 把已开的窗口抬到前台（可能藏在托盘里）；
          没在 → 拉起一个新的。

  交互式双击 = 壁纸 + 面板（进来八成是想调效果）；
  开机自启（autostart.py 装的快捷方式）带 --no-panel = 只起壁纸，
  不在登录时弹窗口，面板随时 Ctrl+Alt+P 叫回来。

所有"怎么判断在不在、怎么拉起"的权威实现都在 panel.pyw 里
（pid_alive / _spawn / focus_existing_panel）—— 这里只复用，不重写，
避免出现两套判据打架（这个项目在"找窗口被 TabProxyWindow 骗"上栽过）。
"""
import ctypes
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

LOG_PATH = os.path.join(BASE, 'wallpaper.log')
WALLPAPER_MUTEX = 'SpotlightWallpaper_SingleInstance'
WALLPAPER_READY_ANCHOR = '启动完成'      # wallpaper.pyw 装载成功后打的日志锚点
WALLPAPER_WAIT_S = 25                    # 登录时磁盘忙，给宽一点


def log(msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] 启动器 %s\n' % (time.strftime('%H:%M:%S'), msg))
    except OSError:
        pass


def wallpaper_mutex_exists():
    """壁纸在不在跑，以互斥量为准。

    pid 文件会骗人（强杀不走清理、pid 还可能被复用），互斥量跟着进程
    生命周期走，是 wallpaper.pyw 自己 already_running() 用的同一判据。

     这里必须 OpenMutexW，不能 CreateMutexW：
      Create 只要有名字就"没有就建一个"——本进程从此攥着句柄，紧跟着
      在同一进程里拉起的壁纸会看到互斥量已存在，误判"已有实例"直接
      退出（00:23:34 实测：新壁纸留下「已有实例在运行，本次启动取消」）。
      Open 打不开 = 真没人持有 = 可以放心拉起。
    """
    try:
        k = ctypes.windll.kernel32
        k.OpenMutexW.restype = ctypes.c_void_p
        h = k.OpenMutexW(0x00100000, False, WALLPAPER_MUTEX)   # SYNCHRONIZE
        if h:
            k.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False


def load_panel_module():
    """加载 panel.pyw 拿它的单实例/拉起/聚焦实现。

    只在确实要动面板时才 import：加载面板模块会连带加载壁纸渲染核心
    （panel 模块级 load_core()），没必要的加载就省掉。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'panel', os.path.join(BASE, 'panel.pyw'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_wallpaper_ready(deadline_s):
    """等壁纸日志打出锚点，返回 True/False（只记日志，不阻塞启动）。"""
    start = time.time()
    size = 0
    try:
        size = os.path.getsize(LOG_PATH)
    except OSError:
        pass
    while time.time() - start < deadline_s:
        try:
            with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(size)
                if WALLPAPER_READY_ANCHOR in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


def main():
    want_panel = '--no-panel' not in sys.argv[1:]

    # ---- 壁纸 ------------------------------------------------------
    if wallpaper_mutex_exists():
        log('壁纸已在跑，不动它')
    else:
        PM = load_panel_module()
        # 互斥量都不在了，pid 文件要么过期要么骗人 —— 一律清掉再拉起
        stale = PM.read_pid()
        if stale:
            log('清掉过期 pid 文件（记录 %d，互斥量已不在）' % stale)
            PM._clear_pid()
        log('拉起壁纸进程')
        PM._spawn('wallpaper.pyw')
        if wait_wallpaper_ready(WALLPAPER_WAIT_S):
            log('壁纸装载完成')
        else:
            # 失败不致命：壁纸自己有降级路径（挂不上桌面层会开小窗口），
            # 就算真死了，用户还有 preview-window.bat / 日志可查。
            log('⚠ %d 秒内没等到「%s」锚点，详见 wallpaper.log 后续'
                % (WALLPAPER_WAIT_S, WALLPAPER_READY_ANCHOR))

    # ---- 面板 ------------------------------------------------------
    if not want_panel:
        log('开机自启模式：不起面板（Ctrl+Alt+P 随时叫回来）')
        return

    PM = load_panel_module()
    if PM.focus_existing_panel():
        # 面板进程活着 → 窗口已经抬到前台（可能刚从托盘里捞出来）
        log('面板已开着，抬到前台')
    else:
        log('拉起面板进程')
        PM._spawn('panel.pyw')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        log('启动器出错: %s' % traceback.format_exc(limit=3))
        sys.exit(1)
