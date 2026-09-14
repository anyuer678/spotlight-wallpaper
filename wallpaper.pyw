# -*- coding: utf-8 -*-
"""
聚光壁纸 · Windows 桌面版
================================================================
鼠标移到哪里，那里就"透出"另一幅画。光斑内一幅图，光斑外另一幅图。

三种运行模式（默认是第一种）：

  pythonw wallpaper.pyw              桌面模式 —— 真正当壁纸用
                                     窗口挂进桌面层，待在桌面图标下面，
                                     图标照常显示、照常双击，不挡任何操作。

  pythonw wallpaper.pyw --window     小窗口预览 —— 安全模式
                                     开一个 1200x720 的普通窗口，有标题栏，
                                     点右上角 × 就能关掉。绝不会遮挡全屏。

  pythonw wallpaper.pyw --full       全屏预览 —— 仅为临时看效果
                                     全屏但不置顶，你点别的窗口它就让开；
                                     按 Esc 或 Ctrl+Alt+Q 退出。

退出方式（任何模式都有效）：
  · 全局热键  Ctrl + Alt + Q      ← 不占用焦点，随时能按
  · 窗口模式  点标题栏的 ×        或按 Esc
  · 命令行    双击 停止壁纸.bat

换自己的图（三条路，随便走哪条）：
  1. 全局热键  Ctrl + Alt + 1     选「光斑外」的图（平时桌面看到的那张）
     global    Ctrl + Alt + 2     选「光斑内」的图（鼠标附近透出来的那张）
     选的图会写进 wallpaper-config.json，下次开机还是它，不用重选。
  2. 把图片丢进 images/ 目录，命名成下面这样，再按 Ctrl + Alt + R 重载：
       images/out.jpg  ← 范围外        images/in.jpg  ← 范围内
     （也认 png / webp / jpeg，以及 outer.* / inner.*）
  3. 直接编辑 wallpaper-config.json 里的 "outer" 和 "inner"，填绝对路径。

  · 日志    wallpaper.log     （pythonw 没有控制台，出问题看这里）
================================================================
"""

import ctypes
import math
import os
import sys
import time
import json
import threading

import tkinter as tk
from PIL import Image, ImageTk, ImageChops

import win32api
import win32con
import win32gui

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, 'wallpaper.log')
PID_PATH = os.path.join(BASE, 'wallpaper.pid')
CFG_PATH = os.path.join(BASE, 'wallpaper-config.json')

# ---------------------------------------------------------------- 默认配置
DEFAULT_CFG = {
    "outer": "images/out.jpg",   # 范围外的图
    "inner": "images/in.jpg",    # 光斑里的图
    "size": 380,                 # 光斑基准尺寸（圆形时即直径）
    "feather": 45,               # 边缘柔和 0-100（越大越柔）
    "glow": 25,                  # 光晕强度 0-100
    "follow": 0.45,              # 跟随灵敏度 0.03-0.9（越小越"黏"、越滞后）
    "drift": False,              # 鼠标静止 3 秒后自动漂移
                                 #  开着会一直重绘、一直吃 CPU，默认关掉
    "fps": 60,                   # 刷新上限。改不透明合成后单帧只要 ~3ms，
                                 # 60fps 也只占单核 20% 左右，放心拉满
    "pause_when_covered": True,  # 光标不在桌面上时冻住光斑（不跟随、不重绘，省 CPU）
    "verbose": False,            # 每 5 秒往日志写一次实测帧率（排查性能用）
    # 图片重载计数。只把这个数字 +1、路径一个字都不改，也要求主程序把两张图
    # 重新读一遍。没有它就没法表达"路径没变、但文件内容被换掉了"（改完图覆盖同名
    # 文件是最常见的用法），那种情况靠比对路径是永远发现不了的。
    "rev": 0,
}

# 预览窗口尺寸（安全模式用）
WINDOW_W, WINDOW_H = 1200, 720

# 空闲轮询间隔（ms）。光斑停了就不必按帧率空转，但也不能睡太久 ——
# 睡太久的话，鼠标一动要等下一轮才发现，手感就是"黏"的。
IDLE_POLL_MS = 8        # 桌面露着：8ms 一轮，响应够快，CPU 几乎为 0
IDLE_HIDDEN_MS = 30     # 桌面被别的窗口盖住：反正看不见，可以慢一点 ——
                        # 但也不能像原来那样慢到 60ms，那是"进入桌面"延迟的大头

# "桌面露没露出来"的复核间隔（秒）。
#  这个方向必须不对称：画→停可以迟钝（多看一会儿只是白画几帧），
#   停→画必须灵敏（慢一拍就是你眼睛能看到的"鼠标动了没反应"）。
#   原来两个方向共用 0.25 秒，叠加上隐藏时的 60ms 轮询，最坏要 ~310ms
#   光斑才出现 —— 这就是"鼠标进入桌面要卡好久"。
COVER_RECHECK_S = 0.25  # 可见时：多久复核一次"是不是被盖住了"
# 隐藏时：每一轮都查（WindowFromPoint + GetAncestor，微秒级，32 次/秒无所谓）

# 主循环卡顿日志的阈值（ms）。超出"计划延时"这么多就记一条 ——
# 下次再有人喊卡，翻日志就能直接看到卡了多久、卡在哪一帧。
STALL_LOG_MS = 80

# 配置文件轮询间隔（秒）。控制面板靠"改文件 + 主程序看 mtime"这条通道
# 做到即时生效，不需要重启进程。stat 一次几微秒，8 次/秒可以忽略。
CFG_POLL_S = 0.12

# ---------------------------------------------------------------- 系统壁纸探测（规则 B）
# 用户跑到「设置 → 个性化 → 背景」里换了壁纸，屏幕不会有任何变化 ——
# 因为本程序从不改系统壁纸，只是把一个整屏窗口盖在画壁纸的 WorkerW 上面。
# 用户只会以为"设置坏了"。所以这里查一下系统壁纸路径，变了就在面板上提示一句：
# 只提示，不改变行为（不自动停壁纸、不自动露出桌面）。
SYS_WP_PATH = os.path.join(BASE, 'system-wallpaper.json')
SYS_WP_POLL_S = 1.5         # 单独节流。绝不放进每帧的循环里 —— SPI 是同步的，
                            # 按 60fps 调它会平白吃掉一截帧预算

# 幻灯片 / 聚焦（Spotlight）这类动态壁纸会让路径自己频繁变化（常是转码临时
# 文件），按"路径变化就提示"会变成刷屏式误报。这里用行为判据兜住，而不是去猜
# 注册表里哪个键代表"幻灯片开着"（那玩意儿在不同版本上名字都不一样）：
# 短时间内变太多次 = 它本来就在自己换，不是用户在设置里改的 → 静默；
# 等路径稳下来 SYS_WP_SETTLE_S 秒，才把最后那个稳定值报一次。
SYS_WP_STORM_N = 4          # 窗口内变这么多就算"它自己在动"
SYS_WP_STORM_S = 30.0       # 上面那个"窗口"
SYS_WP_SETTLE_S = 20.0      # 静默之后要稳这么久才回报一次

RELAUNCH_HANDSHAKE_S = 8.0  # 重启时等新进程接过 pid 文件的上限（见 _relaunch）


def read_system_wallpaper():
    """读当前系统壁纸路径（SPI_GETDESKWALLPAPER）。

     只读，绝不写。 本程序的设计是"只覆盖、不篡改"：把一个整屏窗口盖在
      画壁纸的 WorkerW 上面，用户设置里那张壁纸一直好好的，停掉就露出来。
      真去调 SPI_SETDESKWALLPAPER 才是不可接受的 —— 那是真的改用户的设置。
    """
    try:
        SPI_GETDESKWALLPAPER = 0x0073
        buf = ctypes.create_unicode_buffer(520)      # MAX_PATH 够用，留点余量
        if not ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETDESKWALLPAPER, len(buf), buf, 0):
            return ''
        return buf.value or ''
    except Exception:
        return ''


def slideshow_active():
    """系统壁纸是不是"幻灯片"模式（自己在换）。

    只认注册表里那个明确信号：Windows 打开幻灯片时会在
    HKCU\\Control Panel\\Personalization\\Desktop Slideshow 下写 Interval。
    查不到就当"不是" —— 这只是少一层误报，兜底的还是
    poll_system_wallpaper 里"变得太频繁就静默"那套行为判据，两条一起用。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Control Panel\Personalization\Desktop Slideshow') as k:
            winreg.QueryValueEx(k, 'Interval')
            return True
    except Exception:
        return False


# 探测状态。只有主线程（tick）会碰它，不需要锁。
_SYSWP = {'init': False, 'path': None, 'last_check': 0.0,
          'changes': [], 'suppress': False, 'stable_since': 0.0, 'reported': None}


def report_system_wallpaper(path, prev):
    """把"系统壁纸变了"写进 sidecar 文件，面板读它来提示。

    为什么走文件：面板和壁纸是两个进程，现成的通道只有 pid 文件 / 配置文件 /
    日志三种。这件事不属于配置（面板改它没有意义），也不适合塞日志（面板需要
    结构化地读），所以给它一个自己的小 sidecar。只在真变化时写一次。
    """
    _SYSWP['reported'] = path
    data = {'path': path, 'name': os.path.basename(path) or path,
            'prev': prev, 'changed_at': time.time()}
    try:
        with open(SYS_WP_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log('系统壁纸已变 → 面板会提示（新图：%s）' % data['name'])
    except Exception as e:
        log('写系统壁纸提示失败: %r' % e)


def poll_system_wallpaper():
    """按 SYS_WP_POLL_S 节流地看一眼系统壁纸有没有被用户改掉。

    只提示，不改变任何行为：不自动停壁纸、不自动露出桌面。用户明确要的是
    "我的修改生效了吗"这个确认，而不是程序替他做决定。
    """
    now = time.time()
    if now - _SYSWP['last_check'] < SYS_WP_POLL_S:
        return
    _SYSWP['last_check'] = now
    try:
        p = read_system_wallpaper()
    except Exception:
        return

    if not _SYSWP['init']:
        # 第一次只记基线：启动时读到的那个路径不是"用户刚改的"
        _SYSWP['init'] = True
        _SYSWP['path'] = p
        _SYSWP['reported'] = p
        return

    if p == _SYSWP['path']:
        # 没变。但静默模式要靠"一直没变"来解除 —— 幻灯片停了之后，
        # 最后那张稳定下来的壁纸值得报一次。
        if _SYSWP['suppress'] and p:
            if _SYSWP['stable_since'] == 0.0:
                _SYSWP['stable_since'] = now
            elif now - _SYSWP['stable_since'] >= SYS_WP_SETTLE_S:
                _SYSWP['suppress'] = False
                _SYSWP['changes'] = []
                _SYSWP['stable_since'] = 0.0
                if p != _SYSWP['reported']:
                    log('系统壁纸稳定下来了，恢复提示')
                    report_system_wallpaper(p, _SYSWP['reported'])
        return

    prev, _SYSWP['path'] = _SYSWP['path'], p
    name = lambda q: (os.path.basename(q) or q or '(空)')
    log('系统壁纸变了：%s → %s' % (name(prev), name(p)))

    if slideshow_active():
        # 幻灯片模式下路径本来就会自己变，不是用户在设置里改的 → 一律不报
        _SYSWP['suppress'] = True
        _SYSWP['stable_since'] = 0.0
        _SYSWP['reported'] = p
        return

    _SYSWP['changes'] = [t for t in _SYSWP['changes'] if now - t <= SYS_WP_STORM_S]
    _SYSWP['changes'].append(now)
    if len(_SYSWP['changes']) >= SYS_WP_STORM_N:
        if not _SYSWP['suppress']:
            _SYSWP['suppress'] = True
            log('系统壁纸在 %.0f 秒里变了 %d 次 → 判定为动态壁纸（幻灯片/聚焦），暂停提示'
                % (SYS_WP_STORM_S, len(_SYSWP['changes'])))
        _SYSWP['stable_since'] = 0.0
        return
    if _SYSWP['suppress']:
        return

    report_system_wallpaper(p, prev)



# RedrawWindow 的标志位，自己写死。
# 教训：代码里原来用的是 win32con.RDW_UPDATABLE —— Win32 里根本没有这个常量，
# 于是那两处调用一直在抛 AttributeError，又被外层 `except Exception: pass` 吞掉，
# 挂了几个月都没人知道。真正的名字是 RDW_UPDATENOW（立即兑现）。
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080     # 连子窗口一起失效（退出时清残影要用）

# 单实例互斥体，避免重复启动叠出好几层窗口
_MUTEX = None


def log(msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            log('配置文件读取失败，用默认值: %r' % e)
    return cfg


def save_default_config():
    """首次运行时写一份配置，方便用户改"""
    if os.path.exists(CFG_PATH):
        return
    try:
        with open(CFG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def already_running():
    """已经有实例在跑就不要重复开窗口"""
    global _MUTEX
    try:
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(
            None, False, 'SpotlightWallpaper_SingleInstance')
        return ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return False


# ================================================================ 图像工具
def find_image(*candidates):
    for c in candidates:
        if not c:
            continue
        p = c if os.path.isabs(c) else os.path.join(BASE, c)
        if os.path.exists(p):
            return p
    return None


# 没在配置里指定图片时，按这些名字去 images/ 里找。顺序就是优先级。
IMAGE_CANDIDATES = {
    'outer': ('images/out.jpg', 'images/out.png', 'images/out.webp',
              'images/out.jpeg', 'images/out.bmp',
              'images/outer.jpg', 'images/outer.png'),
    'inner': ('images/in.jpg', 'images/in.png', 'images/in.webp',
              'images/in.jpeg', 'images/in.bmp',
              'images/inner.jpg', 'images/inner.png'),
}


def resolve_side(cfg, side):
    """某一侧最终会用哪张图：配置里写的优先，其次 images/ 下的约定命名。

     主程序和控制面板共用这一个函数，别在各自那边再抄一份候选名单。
      面板要如实告诉用户"现在实际生效的是哪张图"，两边各写一份迟早会走样，
      到时候就成了"面板说用的 A，桌面显示的却是 B"。
    """
    return find_image(cfg.get(side), *IMAGE_CANDIDATES[side])


def cover(img, w, h):
    """等比缩放到铺满 w×h，多余部分居中裁掉 —— 等价于 CSS 的 background-size: cover"""
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        return Image.new('RGB', (w, h), (10, 14, 26))
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
    if (nw, nh) != (iw, ih):
        img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


class _OPENFILENAMEW(ctypes.Structure):
    """Win32 的 OPENFILENAMEW（只用到前一半字段，其余留给系统）"""
    _fields_ = [
        ('lStructSize', ctypes.c_uint32),
        ('hwndOwner', ctypes.c_void_p),
        ('hInstance', ctypes.c_void_p),
        ('lpstrFilter', ctypes.c_wchar_p),
        ('lpstrCustomFilter', ctypes.c_void_p),
        ('nMaxCustFilter', ctypes.c_uint32),
        ('nFilterIndex', ctypes.c_uint32),
        ('lpstrFile', ctypes.c_void_p),
        ('nMaxFile', ctypes.c_uint32),
        ('lpstrFileTitle', ctypes.c_void_p),
        ('nMaxFileTitle', ctypes.c_uint32),
        ('lpstrInitialDir', ctypes.c_wchar_p),
        ('lpstrTitle', ctypes.c_wchar_p),
        ('Flags', ctypes.c_uint32),
        ('nFileOffset', ctypes.c_uint16),
        ('nFileExtension', ctypes.c_uint16),
        ('lpstrDefExt', ctypes.c_wchar_p),
        ('lCustData', ctypes.c_void_p),
        ('lpfnHook', ctypes.c_void_p),
        ('lpTemplateName', ctypes.c_wchar_p),
        ('pvReserved', ctypes.c_void_p),
        ('dwReserved', ctypes.c_uint32),
        ('FlagsEx', ctypes.c_uint32),
    ]


def pick_image_file(title, initial='', owner=None):
    """弹系统原生的"打开"对话框挑一张图。

    特意不用 tkinter.filedialog：壁纸窗口被挂到了桌面层（Progman 的子窗口），
    Tk 自带的对话框在这种父子关系下有显示不出来的风险。直接调 comdlg32
    是独立窗口，一定看得见、一定在最前面。

    owner 是对话框的属主窗口句柄（可以不传）。控制面板在别的进程里，把面板
    窗口塞进来当属主，对话框才会规规矩矩地模态在面板前面 —— 否则它可能
    弹到别的程序后面去，用户点了"选择"却觉得"什么都没发生"。
    """
    buf = ctypes.create_unicode_buffer(2048)
    if initial and os.path.exists(initial):
        buf.value = initial
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.hwndOwner = int(owner) if owner else None
    ofn.lpstrFilter = ('图片文件\0*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif\0'
                       '所有文件\0*.*\0\0')
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_void_p)
    ofn.nMaxFile = 2048
    ofn.lpstrTitle = title
    ofn.Flags = (0x00080000 |      # OFN_EXPLORER
                 0x00001000 |      # OFN_FILEMUSTEXIST
                 0x00000800 |      # OFN_PATHMUSTEXIST
                 0x00000008)       # OFN_NOCHANGEDIR
    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception as e:
        log('打开选图对话框失败: %r' % e)
        return None
    return buf.value if ok else None


def make_placeholder(kind, w, h):
    """没放图片时的默认图案：外层深夜蓝，内层暖金"""
    small = (96, 54)   # 先在小尺寸上画再放大，天然柔和
    im = Image.new('RGB', small, (6, 10, 24))
    px = im.load()
    for y in range(small[1]):
        for x in range(small[0]):
            u, v = x / small[0], y / small[1]
            if kind == 'outer':
                r = int(6 + 30 * u + 18 * (1 - v))
                g = int(10 + 40 * (1 - v) + 20 * u)
                b = int(24 + 90 * (1 - v) + 60 * u)
            else:
                r = int(255 - 90 * v)
                g = int(150 - 60 * v + 40 * u)
                b = int(90 + 120 * v)
            px[x, y] = (min(255, r), min(255, g), min(255, b))
    return im.resize((w, h), Image.LANCZOS)


# 遮罩 / 光晕的逐像素核心循环是纯 Python：192² 要 5~7ms，是"拖滑块不跟手"
# 的最大一笔开销。但那张 ss×ss 的小图**只跟 feather / strength 有关，跟目标
# 尺寸完全无关** —— 所以按参数把它缓存下来，每次只做一次 LANCZOS 放大（~0.3ms）。
#
# 结果与原来逐字节相同：原实现本来就是"先算 ss×ss 再放大到 (w,h)"，
# 这里只是把前半步的结果留下了，后半步一行没动。
_CORE_CACHE = {}
_CORE_MAX = 64


def _core_get(key):
    img = _CORE_CACHE.get(key)
    if img is not None:
        # 命中就挪到末尾 —— 于是下面的淘汰自然就是 LRU，
        # 用户正在来回磨的那几个值不会被刚生成的中间值挤掉。
        _CORE_CACHE.pop(key, None)
        _CORE_CACHE[key] = img
    return img


def _core_put(key, img):
    #  这里踩过一次坑：原先写的是"超过 24 项就整体 clear()"。
    #   而拖动"边缘柔和"滑块会一口气造出几十个中间值 —— 于是每次调用都撞上
    #   清空、每次都退回冷算那个纯 Python 循环，实测把拖这条滑块从 8ms
    #   拖到 17ms。改成只淘汰最旧的一个就好。
    if len(_CORE_CACHE) >= _CORE_MAX:
        try:
            _CORE_CACHE.pop(next(iter(_CORE_CACHE)), None)
        except (StopIteration, KeyError):
            _CORE_CACHE.clear()
    _CORE_CACHE[key] = img


def _mask_core(ss, feather):
    key = ('m', ss, round(float(feather), 3))
    hit = _core_get(key)
    if hit is not None:
        return hit
    m = Image.new('L', (ss, ss), 0)
    px = m.load()
    c = (ss - 1) / 2.0
    solid = c * (1.0 - feather / 100.0)      # 实心区半径
    span = max(0.5, c - solid)               # 渐变过渡宽度
    for y in range(ss):
        dy = y - c
        for x in range(ss):
            d = math.hypot(x - c, dy)
            if d <= solid:
                px[x, y] = 255
            elif d >= c:
                px[x, y] = 0
            else:
                px[x, y] = int(255 * (1.0 - (d - solid) / span))
    _core_put(key, m)
    return m


def _glow_core(ss, strength):
    key = ('g', ss, round(float(strength), 4))
    hit = _core_get(key)
    if hit is not None:
        return hit
    m = Image.new('RGBA', (ss, ss), (0, 0, 0, 0))
    px = m.load()
    c = (ss - 1) / 2.0
    for y in range(ss):
        dy = y - c
        for x in range(ss):
            d = math.hypot(x - c, dy) / c
            a = max(0.0, 1.0 - d / 0.86) ** 1.6
            px[x, y] = (255, 246, 224, int(255 * a * strength))
    _core_put(key, m)
    return m


def make_mask(w, h, feather, ss=192):
    """生成椭圆形羽化遮罩（灰度 L 图）。

    为了快，先在 ss×ss 的小图上逐像素算，再 LANCZOS 放大 —— 天然抗锯齿。
    feather=0 硬边，feather=100 从圆心就开始渐隐。

    小图本身按 (ss, feather) 缓存（见 _mask_core）：主程序和控制面板都会在
    拖滑块时每帧要一张新尺寸的遮罩，尺寸每次都不同，但小图是同一张。
    """
    w, h = max(2, int(w)), max(2, int(h))
    return _mask_core(ss, feather).resize((w, h), Image.LANCZOS)


def make_glow(w, h, strength, ss=128):
    """暖色柔光。现在是合成进光斑里的，不再单独占一个画布 item。

    0.86 这个衰减系数是配合"光晕和光斑同尺寸"定的：亮度在 0.86×半径 处归零，
    正好落在外圈羽化带之内。以前光晕单独做成 1.2 倍大，其实超出的那圈
    全透明，白白把画布脏区撑大了 1.4 倍。
    """
    w, h = max(2, int(w)), max(2, int(h))
    return _glow_core(ss, strength).resize((w, h), Image.LANCZOS)


# ---------------------------------------------------------------- CPU 计时
_CPU_HANDLE = [None]


def process_cpu_seconds():
    """本进程累计吃掉的 CPU 秒数（内核 + 用户）。

    比"墙钟时间"诚实得多：GIL 上等待、被系统调度掉的时间都不算进去，
    所以它是"这套渲染到底吃掉多少 CPU"最直接的证据。
    """
    try:
        k32 = ctypes.windll.kernel32
        if _CPU_HANDLE[0] is None:
            _CPU_HANDLE[0] = k32.OpenProcess(0x1000, False, os.getpid())

        class _FT(ctypes.Structure):
            _fields_ = [('lo', ctypes.c_uint32), ('hi', ctypes.c_uint32)]

        c, e, k, u = _FT(), _FT(), _FT(), _FT()
        if not k32.GetProcessTimes(_CPU_HANDLE[0], ctypes.byref(c),
                                   ctypes.byref(e), ctypes.byref(k),
                                   ctypes.byref(u)):
            return 0.0
        return ((k.hi << 32) | k.lo) / 1e7 + ((u.hi << 32) | u.lo) / 1e7
    except Exception:
        return 0.0


# ============================================================ 桌面图层挂载
WORKERW_MSG = 0x052C      # 让 Progman 分裂出壁纸层 WorkerW 的私有消息


def _full_screen(h, sw, sh):
    try:
        l, t, r, b = win32gui.GetWindowRect(h)
        return (r - l) >= sw * 0.9 and (b - t) >= sh * 0.9
    except Exception:
        return False


def find_wallpaper_layer(progman, sw, sh):
    """找壁纸层 WorkerW。

    踩过的坑：这台机器上 0x052C 生成的 WorkerW 是 Progman 的子窗口，
    不是顶层窗口 —— 只枚举顶层窗口（EnumWindows）永远找不到它。
    所以两种位置都要找：

      a. Progman 的子窗口（本机 Windows 11 实测就是这个）
      b. 顶层窗口，且紧跟"含有 SHELLDLL_DefView 的那个窗口"之后（多数 Win10）

    另外：系统里飘着一堆别的应用创建的小 WorkerW（本机 11 个，202×56 或 0×0），
    必须校验尺寸，否则会把壁纸贴到别人界面上。
    """
    h = win32gui.FindWindowEx(progman, 0, 'WorkerW', None)
    while h:
        if _full_screen(h, sw, sh):
            return h, 'WorkerW 壁纸层（Progman 子窗口）'
        h = win32gui.FindWindowEx(progman, h, 'WorkerW', None)

    found = []

    def cb(hwnd, _):
        if win32gui.FindWindowEx(hwnd, 0, 'SHELLDLL_DefView', None):
            w = win32gui.FindWindowEx(0, hwnd, 'WorkerW', None)
            if w and _full_screen(w, sw, sh):
                found.append(w)
        return True

    win32gui.EnumWindows(cb, None)
    if found:
        return found[0], 'WorkerW 壁纸层（顶层兄弟窗口）'
    return 0, ''


WS_EX_LAYERED = 0x00080000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
LWA_ALPHA = 0x00000002


def needs_layered_child(progman):
    """判断是否需要"分层子窗口"方案。

    Windows 11 24H2（build 26100+）改了桌面背景的渲染方式（微软官方说明）：
      · Progman 用 WS_EX_NOREDIRECTIONBITMAP 创建 —— 它自己没有 GDI 内容；
      · 图标层 SHELLDLL_DefView 变成了 WS_EX_LAYERED 分层窗口；
      · 壁纸由 Progman 下一个 z 序更低的 WorkerW 子窗口来画。

    后果：挂在 Progman 下的普通（非分层）子窗口，DWM 不会合成它 ——
    结构全对，但屏幕上一个像素都不显示（本机实测就是这个症状）。

    官方给出的正确做法：应用程序自己创建一个 WS_EX_LAYERED 子窗口，
    z 序放在 DefView 之下、WorkerW 之上，并用
    SetLayeredWindowAttributes(alpha=0xFF) 设成不透明。
    """
    try:
        if win32gui.GetWindowLong(progman,
                                  win32con.GWL_EXSTYLE) & WS_EX_NOREDIRECTIONBITMAP:
            return True
        dv = win32gui.FindWindowEx(progman, 0, 'SHELLDLL_DefView', None)
        if dv and (win32gui.GetWindowLong(dv,
                                          win32con.GWL_EXSTYLE) & WS_EX_LAYERED):
            return True
    except Exception:
        pass
    return False


def resolve_host(sw, sh):
    """找桌面层的宿主窗口。纯查询，不创建任何窗口。

    返回 (宿主 hwnd, 插到谁下面, 描述, Progman)

    优先级：
      1. Win11 24H2+ ：Progman 的分层子窗口，z 序插在 SHELLDLL_DefView 之下
      2. WorkerW 壁纸层 —— 系统专门放壁纸的那一层（Win10 / 早期 Win11）
      3. 兜底：直接压进 Progman 的图标层之下
    """
    progman = win32gui.FindWindow('Progman', None)
    if not progman:
        return 0, 0, '', 0

    # 这条消息让 Progman 分裂出壁纸层。wParam 必须是 0x0D，
    # lParam 给 0 还是 1 各版本不一，两个都发一遍最稳。
    for lp in (0x0, 0x1):
        try:
            win32gui.SendMessageTimeout(progman, WORKERW_MSG, 0x0D, lp,
                                        win32con.SMTO_NORMAL, 2000)
        except Exception:
            pass

    defview = win32gui.FindWindowEx(progman, 0, 'SHELLDLL_DefView', None)

    # 新版 Windows 必须走分层子窗口，插在图标层正下方
    if defview and needs_layered_child(progman):
        return (progman, defview,
                '桌面层（Progman 的分层子窗口，在图标层下面）', progman)

    w, desc = find_wallpaper_layer(progman, sw, sh)
    if w:
        return w, 0, desc, progman

    if defview:
        return progman, defview, 'Progman（压在图标层之下）', progman
    return progman, 0, 'Progman', progman


def children_zorder(parent):
    """从最上层到最下层列出某窗口的子窗口"""
    out = []
    h = win32gui.GetWindow(parent, win32con.GW_CHILD)
    while h:
        out.append(h)
        h = win32gui.GetWindow(h, win32con.GW_HWNDNEXT)
    return out


def capture_window(hwnd, path):
    """把某个窗口（含子窗口）渲染成 PNG —— 用于确认壁纸真的画出来了"""
    import win32ui
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hdc = win32gui.GetWindowDC(hwnd)
    try:
        src = win32ui.CreateDCFromHandle(hdc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        img = Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                               bmp.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        img.save(path)
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.DeleteObject(bmp.GetHandle())
        return img
    finally:
        win32gui.ReleaseDC(hwnd, hdc)


# ==================================================================== 主体
class SpotlightWallpaper:

    def __init__(self, mode='desktop', selftest=False, bench=0):
        self.mode = mode            # desktop | window | full
        self.selftest = selftest
        self.bench_frames = int(bench or 0)   # >0 = 跑合成轨迹做性能基准
        self.attach_mode = '未挂载'
        self.fallback_reason = ''
        self.cfg = load_config()
        self.sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        self.sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        log('屏幕 %dx%d，模式 %s' % (self.sw, self.sh, mode))

        # ---- 光斑状态
        self.size = float(self.cfg['size'])
        self.feather = float(self.cfg['feather'])
        self.glow_strength = max(0.0, min(1.0, self.cfg['glow'] / 100.0))
        self.follow = max(0.02, min(0.9, float(self.cfg['follow'])))
        self.drift_on = bool(self.cfg['drift'])
        self.pause_when_covered = bool(self.cfg.get('pause_when_covered', True))
        # 图片重载计数：控制面板点"重新载入图片"就把它 +1，主程序据此重读两张图
        # （路径可能一个字都没变，光比对路径发现不了"文件内容被换掉了"）
        self.rev = int(self.cfg.get('rev', 0) or 0)
        self._pending = None         # 热键线程递过来的换图请求

        # 性能统计字段。必须在这里初始化 —— 放在主循环里懒初始化的话，
        # 任何"先渲染、后进主循环"的路径都会 AttributeError（自检模式就撞过）。
        self.verbose = bool(self.cfg.get('verbose'))
        self._fps_t0 = time.time()
        self._ticks = 0
        self._frames = 0
        self._step_ms = 0.0
        self._render_ms = 0.0
        self._visible = True
        self._cover_check = 0.0
        # 分项计时（--bench 用）：把 PIL / 上传 / 移动 拆开看
        self._t_pil = 0.0
        self._t_paste = 0.0
        self._t_coord = 0.0
        self._synth = None           # 合成光标位置；None = 用真实光标
        self.last_dt = 1.0 / 60.0    # 上一帧间隔，给"与帧率无关的缓动"用
        self._next_due = None        # 下一帧的绝对时刻（定时器对齐用）
        self._last_tick = None       # 上一次 tick 的时刻
        self._sched_ms = 0.0         # 上一轮打算睡多久（卡顿日志的基准）
        self._last_drew = None       # 上一轮画了没有（卡顿日志的现场记录）
        self._ignore_next_gap = False
        self._cfg_check = 0.0        # 上次查配置 mtime 的时刻
        self._cfg_sign = None        # 配置变更签名 (mtime_ns, size)，热重载靠它
        self._cfg_log_at = 0.0       # 上次打"控制面板改了参数"日志的时刻（限流）
        self._hidden_at = None       # 这一轮"被盖住"是从什么时候开始的
        self._hidden_rounds = 0      # 被盖住期间空转了多少轮
        # 基准模式（--bench）用的字段
        self._bench_left = 0
        self._bench_n = 0
        self._bench_gaps = []
        self._bench_prev = None
        self._bench_drawn = 0
        self._bench_cpu0 = 0.0

        self.cur_x = self.sw / 2.0
        self.cur_y = self.sh / 2.0
        self.tgt_x = self.cur_x
        self.tgt_y = self.cur_y
        self.last_move = 0.0
        self.last_mouse = (-1, -1)
        self.hint_job = None
        self.resize_job = None
        self.last_render = None
        self.running = True

        # ---- 0. 先决定能不能挂桌面层（纯查询，不建窗口）
        self.host, self.after, self.host_desc, self.progman = (0, 0, '', 0)
        self.layered = False
        if mode == 'desktop':
            (self.host, self.after,
             self.host_desc, self.progman) = resolve_host(self.sw, self.sh)
            self.layered = bool(self.progman) and needs_layered_child(self.progman)
            log('桌面层方案：宿主=%s 分层子窗口=%s' % (self.host_desc, self.layered))

        # ---- 1. 建窗口
        self._create_window()

        # ---- 2. 挂桌面层；失败就降级成小窗口，绝不留一个盖住全屏的窗口
        if mode == 'desktop':
            self._attach_to_desktop()

        # ---- 3. 按最终尺寸准备图像并落盘
        self._prepare_images()
        self._draw_static()

        # ---- 4. 兜底退出通道 + 看护 + 主循环
        if self.bench_frames:
            # 基准模式：走真实主循环，只把光标换成合成轨迹。
            # 热键/看护/提示都不装 —— 它们会插进计时里。
            # 帧率上限拉到 1000，测的是"这套渲染最快能跑多少"，不是配置值。
            self._bench_left = self.bench_frames
            self._bench_n = self.bench_frames
            self.cfg['fps'] = 1000
            # 基准模式必须关掉"遮挡就不画"：跑起来时真实光标多半在别的
            # 窗口上，否则整场测试一帧都不会画，测出来全是假的。
            self.pause_when_covered = False
            self._visible = True
            self._bench_cpu0 = process_cpu_seconds()
            self.tick()
        elif not selftest:
            self._start_hotkey()
            self._show_hint()
            self._start_watchdog()
            self.tick()

    # ------------------------------------------------------- 宿主 / 窗口
    def _create_window(self):
        self.root = tk.Tk()
        self.root.configure(bg='black')

        if self.mode == 'window':
            # 安全模式：普通窗口，有标题栏，能拖能缩，× 直接关
            x = max(0, (self.sw - WINDOW_W) // 2)
            y = max(0, (self.sh - WINDOW_H) // 2 - 40)
            self.root.title('聚光壁纸 · 预览（不会遮挡全屏，关闭点右上角 ×）')
            self.root.geometry('%dx%d+%d+%d' % (WINDOW_W, WINDOW_H, x, y))
            self.root.minsize(420, 260)
            self.root.protocol('WM_DELETE_WINDOW', self._quit)
            self.root.bind('<Escape>', lambda e: self._quit())
            self.cw, self.ch = WINDOW_W, WINDOW_H
            self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, bg='black')
            self.canvas.pack(fill='both', expand=True)
            self.root.update_idletasks()
            self.canvas.config(width=self.cw, height=self.ch)
            self.root.bind('<Configure>', self._on_resize)
        else:
            # 桌面模式 / 全屏预览：无边框满屏
            self.root.overrideredirect(True)
            self.root.geometry('%dx%d+0+0' % (self.sw, self.sh))
            self.cw, self.ch = self.sw, self.sh
            self.canvas = tk.Canvas(self.root, width=self.cw, height=self.ch,
                                    highlightthickness=0, bd=0, bg='black')
            self.canvas.pack(fill='both', expand=True)
            if self.mode == 'desktop':
                self.root.withdraw()          # 先藏起来，挂载成功才显示
            else:
                # 全屏预览：不置顶，你点别的窗口它就会让开
                self.root.attributes('-topmost', False)
                self.root.bind('<Escape>', lambda e: self._quit())
                self.root.after(120, self._grab_focus)

        self.root.update_idletasks()
        self.ox = self.root.winfo_rootx()
        self.oy = self.root.winfo_rooty()

    def _grab_focus(self):
        """只有全屏预览才抢焦点；桌面模式下绝不抢，那会打断你干活"""
        try:
            self.root.focus_force()
        except Exception:
            pass

    def _hwnd(self):
        self.root.update_idletasks()
        h = win32gui.GetParent(self.root.winfo_id())
        return h or self.root.winfo_id()

    def _on_resize(self, ev):
        if ev.widget is not self.root or self.selftest:
            return
        w, h = ev.width, ev.height
        if w < 80 or h < 60:
            return
        # 只是挪窗口位置（尺寸没变）就不用重建图片
        if abs(w - self.cw) <= 2 and abs(h - self.ch) <= 2:
            return
        self.cw, self.ch = w, h
        if self.resize_job:
            try:
                self.root.after_cancel(self.resize_job)
            except Exception:
                pass
        self.resize_job = self.root.after(160, self._rebuild_after_resize)

    def _rebuild_after_resize(self):
        self.resize_job = None
        self._mask = self._glow_img = None
        self._mask_key = self._glow_key = None
        self._prepare_images()
        self._draw_static()

    def _make_layered(self):
        """把窗口变成不透明的分层窗口。

        必须做这一步：Win11 24H2+ 的桌面层只有分层窗口才画得出来。
        SetLayeredWindowAttributes(alpha=0xFF) 不能漏 —— 只加 LAYERED 而不设
        属性的话，窗口是全透明的（默认 alpha=0），照样什么都看不见。
        """
        ex = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        want = ex | WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE
        if want != ex:
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, want)
        ctypes.windll.user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, LWA_ALPHA)

    def _apply_placement(self, repaint=False):
        """认领宿主 + 压 z 序。

        插到图标层下面时不能带 SWP_NOZORDER，否则 after 会被忽略。
        分层样式要在 SetParent 之前设好 —— 反过来（先挂载再改样式）实测会
        让窗口一直停在透明状态，WM_PAINT 不再触发。
        """
        if self.layered:
            self._make_layered()
        win32gui.SetParent(self.hwnd, self.host)
        flags = win32con.SWP_NOACTIVATE
        if not self.after:
            flags |= win32con.SWP_NOZORDER
        win32gui.SetWindowPos(self.hwnd, self.after, 0, 0,
                              self.sw, self.sh, flags)
        if repaint:
            # 挂载后逼 DWM 重新合成一次。这里要立即兑现（RDW_UPDATENOW）：
            # 只失效不兑现的话，这笔账会挂到"桌面下次露出来"的那一刻去还，
            # 而那正好是用户看得见的一帧。
            try:
                win32gui.RedrawWindow(self.hwnd, None, None,
                                      RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW)
            except Exception as e:
                log('重绘请求失败（不影响挂载，仅可能画面迟一拍）: %r' % e)

    def _attach_to_desktop(self):
        """把窗口塞进桌面层；任何一步不对就降级成小窗口"""
        self.hwnd = self._hwnd()

        if not self.host:
            log('找不到桌面层宿主，降级为小窗口预览')
            self._fallback_to_window('未找到可用的桌面层窗口（Progman / WorkerW）')
            return

        try:
            # 不进 Alt+Tab、不抢焦点
            ex = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                self.hwnd, win32con.GWL_EXSTYLE,
                ex | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
            self._apply_placement()
        except Exception as e:
            log('挂载异常: %r' % e)
            self._fallback_to_window('挂载过程出错：%r' % (e,))
            return

        # 显示出来。deiconify 可能顺手把窗口抬到最上面，所以之后必须重压一次 z 序，
        # 否则图标会被我们的窗口盖住 —— 那就变成"桌面图标不见了"。
        self.root.deiconify()
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
        try:
            self._apply_placement(repaint=True)
        except Exception as e:
            log('二次压 z 序失败: %r' % e)

        # 有些人（尤其 GDI 应用）改完父窗口后画面要"抖一下"才会出来：
        # 尺寸先挪 1 像素再回来，强制 DWM 重新合成一次。
        if self.layered:
            try:
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, self.sw - 1, self.sh - 1,
                                      win32con.SWP_NOACTIVATE |
                                      win32con.SWP_NOZORDER | win32con.SWP_NOMOVE)
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, self.sw, self.sh,
                                      win32con.SWP_NOACTIVATE |
                                      win32con.SWP_NOZORDER | win32con.SWP_NOMOVE)
                win32gui.UpdateWindow(self.hwnd)
            except Exception as e:
                log('刷新分层窗口失败: %r' % e)

        if not self._verify_above_icons():
            log('z 序校验未通过，降级为小窗口预览')
            self._fallback_to_window('窗口没能正确落到桌面图标下面')
            return

        self.attach_mode = self.host_desc
        log('挂载校验通过：%s（窗口=%s → 宿主=%s，分层=%s）'
            % (self.host_desc, self.hwnd, self.host, self.layered))

    def _style_ok(self):
        """新版 Windows 上，窗口必须真的带 WS_EX_LAYERED，否则就是白忙一场"""
        if not self.layered:
            return True
        try:
            return bool(win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
                        & WS_EX_LAYERED)
        except Exception:
            return False

    def _verify_above_icons(self):
        """结构校验：我们的窗口确实待在桌面层里，且桌面图标画在我们上面。

        两种布局都要能判：
          · 挂进 WorkerW      → 先爬到"挂在 Progman 下的那一级祖先"（就是 WorkerW）
          · 直接压进 Progman  → 同样爬到那一级（就是我们自己）
        然后要求图标层 SHELLDLL_DefView 排在它之前（= 更靠上 = 盖住我们）。

        这里不拿 GetParent 判定 —— 实测在 Tk 的 TkTopLevel 上它返回 0，
        但窗口确实已经进了宿主的子窗口链，所以以 z 序列表为准。
        """
        try:
            if not self.hwnd or not win32gui.IsWindow(self.hwnd):
                return False
            if not self.progman:
                return False
            if not self._style_ok():
                return False

            # 从我们的窗口往上爬，找到挂在 Progman 下面的那一级
            node, guard = self.hwnd, 0
            while guard < 8:
                guard += 1
                parent = win32gui.GetAncestor(node, win32con.GA_PARENT)
                if not parent or parent == node:
                    return False
                if parent == self.progman:
                    break
                node = parent
            else:
                return False

            kids = children_zorder(self.progman)
            if node not in kids:
                return False
            defview = win32gui.FindWindowEx(self.progman, 0,
                                            'SHELLDLL_DefView', None)
            if not defview or defview not in kids:
                return True          # 连图标层都没有，就不存在被盖住的问题
            # 列表从上到下，图标层下标更小 = 更靠上 = 盖住我们，才对
            return kids.index(defview) < kids.index(node)
        except Exception as e:
            log('校验出错: %r' % e)
            return False

    def _fallback_to_window(self, why):
        """降级：拆掉满屏窗口，改成安全的小窗口，避免遮挡用户屏幕。

        这里只负责重建窗口本身；图像准备、热键、主循环都由 __init__ 的
        正常流程接着做，免得启动两套循环、两个热键线程。
        """
        log('降级原因：%s' % why)
        self.attach_mode = '小窗口预览（桌面层挂载失败）'
        self.fallback_reason = why
        try:
            self.root.destroy()
        except Exception:
            pass
        self.mode = 'window'
        self._create_window()

    # ------------------------------------------------------------ 图像
    def _radius(self):
        return self.size / 2.0

    @staticmethod
    def _open_image(path):
        if not path:
            return None
        try:
            im = Image.open(path)
            im.load()
            return im.convert('RGB')
        except Exception as e:
            log('图片读取失败：%s（%r）' % (path, e))
            return None

    def _fit_source(self, img):
        """把源图预缩到不超过屏幕尺寸。

        4K 原图（3840×2400）留着没有任何意义 —— 画的时候反正要缩到屏幕大小，
        但常驻内存要多占 50MB，每次重建还要多做一次大尺寸重采样。
        """
        sw, sh = max(self.sw, 1600), max(self.sh, 900)
        if img.width > sw or img.height > sh:
            scale = min(sw / img.width, sh / img.height)
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))), Image.LANCZOS)
        return img

    def _load_sources(self):
        """读入两张源图。

        解析顺序（先命中先用）：配置里写的路径 → images/ 目录的约定命名。
        用 Ctrl+Alt+1 / 2 选过的图会作为绝对路径写进配置，永久生效 ——
        这也是为什么配置里的路径要排在目录约定前面。
        """
        # 候选名单统一收在 resolve_side() 里 —— 控制面板要如实显示"现在实际
        # 生效的是哪张图"，用的也是同一个函数。两边各抄一份迟早会走样。
        outer_path = resolve_side(self.cfg, 'outer')
        inner_path = resolve_side(self.cfg, 'inner')
        self.outer_path = outer_path
        self.inner_path = inner_path
        # 记下"当前这两张图是由配置里哪两个值解析出来的"。
        # 热重载要靠它判断控制面板到底换没换图 —— 不能拿解析后的绝对路径去比，
        # 因为用户可能在配置里写的是相对路径。
        self.outer_cfg = self.cfg.get('outer')
        self.inner_cfg = self.cfg.get('inner')
        self.src_outer = self._fit_source(
            self._open_image(outer_path) or make_placeholder('outer', 1600, 900))
        self.src_inner = self._fit_source(
            self._open_image(inner_path) or make_placeholder('inner', 1600, 900))
        log('范围外图片: %s' % (outer_path or '未找到，用默认图案'))
        log('范围内的图片: %s' % (inner_path or '未找到，用默认图案'))

    def _apply_action(self):
        """处理热键线程递过来的换图请求。

        必须在 Tk 主线程里执行（窗口、对话框都归它管），所以热键线程只负责
        写下 self._pending，由这里取走 —— 跨线程直接调 Tk 是不安全的。
        """
        act, self._pending = self._pending, None
        if not act:
            return
        if act == 'panel':
            self._open_panel()
            return
        try:
            if act == 'reload':
                self._load_sources()
            else:
                initial = self.outer_path if act == 'outer' else self.inner_path
                title = ('选择「光斑外」的图 —— 平时桌面看到的那张'
                         if act == 'outer'
                         else '选择「光斑内」的图 —— 鼠标附近透出来的那张')
                path = pick_image_file(title, initial or '')
                if not path:
                    log('换图已取消')
                    return
                self.cfg[act] = path
                try:
                    with open(CFG_PATH, 'w', encoding='utf-8') as f:
                        json.dump(self.cfg, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    log('配置写入失败（本次有效，下次要重选）: %r' % e)
                self._load_sources()
        except Exception as e:
            log('换图出错: %r' % e)
            return
        self._prepare_images()
        self._draw_static()
        log('图片已更新 → 范围外=%s ｜ 范围内=%s'
            % (self.outer_path or '默认图案', self.inner_path or '默认图案'))

    def _prepare_images(self):
        w, h = self.cw, self.ch

        if not hasattr(self, 'src_outer'):
            self._load_sources()

        # 两张都保持 RGB —— 整条渲染路径上不出现 alpha 通道，这是性能关键
        self.img_outer = cover(self.src_outer, w, h)
        self.img_inner = cover(self.src_inner, w, h)

        self._mask = None
        self._mask_key = None
        self._glow_img = None
        self._glow_key = None
        self._spot_buf = None        # 光斑合成缓冲，_draw_static 里按尺寸建

    def _get_mask(self, w, h):
        key = (w, h, int(self.feather))
        if self._mask_key != key:
            self._mask = make_mask(w, h, self.feather)
            self._mask_key = key
        return self._mask

    def _get_glow(self, w, h):
        key = (w, h, round(self.glow_strength, 2))
        if self._glow_key != key:
            self._glow_img = make_glow(w, h, self.glow_strength)
            self._glow_key = key
        return self._glow_img

    @staticmethod
    def _paste_clipped(dst, src, dx, dy, mask=None):
        """把 src 的 (dx, dy) 起点对齐到 dst 的 (0, 0)，越界部分自动裁掉。

        约定：dst 的 (i, j) 像素取 src 的 (dx+i, dy+j)。

        为什么要手写：Pillow 的 crop 在越界处会填黑，光斑贴到屏幕边缘
        就会出现黑框。而画布外面根本画不出来，所以只贴交集 ——
        既不出现黑边，也不浪费时间去算看不见的像素。
        """
        # dst 上要写的范围：i ∈ [max(0,-dx), min(dst.w, src.w - dx))
        i0, j0 = max(0, -dx), max(0, -dy)
        i1 = min(dst.width, src.width - dx)
        j1 = min(dst.height, src.height - dy)
        if i1 <= i0 or j1 <= j0:
            return
        piece = src.crop((dx + i0, dy + j0, dx + i1, dy + j1))
        m = mask.crop((i0, j0, i1, j1)) if mask is not None else None
        dst.paste(piece, (i0, j0), m)

    def _draw_static(self):
        """整屏外图 + 一块光斑画布。只在换图 / 改分辨率时才调。

         整屏那一步很贵：实测 delete('all') + 新建 2560×1600 的 PhotoImage
        + 重画 = 44ms。所以它被拆成了两半 —— 每天在动的那一半（光斑）
        单独走 _rebuild_spot()，只有毫秒级。控制面板拖滑块时走后者。
        """
        self.canvas.delete('all')
        self.tk_outer = ImageTk.PhotoImage(self.img_outer)
        self.canvas.create_image(0, 0, anchor='nw', image=self.tk_outer,
                                 tags='outer')
        self._rebuild_spot()

    def _rebuild_spot(self):
        """只重建光斑那一小块 —— 改 光圈大小 / 边缘柔和 / 光晕强度 时走这里。

        不碰整屏外图，所以是毫秒级（外图那步要 44ms）。控制面板能"拖着滑块
        实时看效果"，靠的就是这个拆分。

        三条实测结论，别凭感觉改：

        * 光斑必须是不透明 RGB，绝不能带 alpha。 Tk 显示带 alpha 的相册时
          每帧要多做一次全图 alpha 合成 —— 实测 380²：带 alpha 11.75ms，
          不带只要 1.15ms，差 10 倍。所以光晕不做成独立 item，而是合成进光斑。
        * 画布上只留两个 item：全屏外图（永不动）+ 光斑（每帧挪）。
          会动的 item 越少，画布重绘的脏区越小。
        * 光斑相册只建一次，之后就地 paste。每帧新建 PhotoImage 要 1.6ms 起。
        """
        self.canvas.delete('spot')
        d = max(2, int(self._radius() * 2))
        # 不透明 RGB 相册：Tk 走纯 blit，不做 alpha 合成
        self.tk_spot = ImageTk.PhotoImage(Image.new('RGB', (d, d), (0, 0, 0)))
        self.spot_id = self.canvas.create_image(self.cur_x, self.cur_y,
                                                anchor='center',
                                                image=self.tk_spot, tags='spot')
        self._spot_d = d
        # 合成缓冲：外图 + 光晕 + 羽化内图 每帧在这里拼好，一次上传
        self._spot_buf = Image.new('RGB', (d, d))
        self._glow_small = (self._get_glow(d, d)
                            if self.glow_strength > 0 else None)
        self.last_render = None
        # 先按当前位置合成一帧，否则会留下一块黑方块
        self._render(self.cur_x, self.cur_y)

    # ------------------------------------------------------- 提示角标
    def _show_hint(self, extra=''):
        if self.selftest:
            return
        if self.mode == 'window':
            text = '预览窗口 · 点右上角 × 关闭，或按 Esc / Ctrl+Alt+Q'
        elif self.mode == 'full':
            text = '全屏预览 · 按 Esc 退出（或 Ctrl+Alt+Q）'
        else:
            text = ('壁纸已就位 · 控制面板 Ctrl+Alt+P'
                    ' · 换图 Ctrl+Alt+1（外）/ 2（内） · 退出 Ctrl+Alt+Q')
        if self.fallback_reason and not extra:
            extra = '（%s，已自动切成小窗口）' % self.fallback_reason
        text += extra
        try:
            self.hint = tk.Label(self.root, text=text, bg='#1b1f2a', fg='#f2e8d5',
                                 font=('Microsoft YaHei UI', 10), padx=14, pady=8)
            self.hint.place(relx=0.5, y=16, anchor='n')
            # 桌面模式下提示也是贴在桌面上的，十几秒后自动消失
            self.hint_job = self.root.after(14000, self._hide_hint)
        except Exception as e:
            log('提示角标绘制失败: %r' % e)

    def _hide_hint(self):
        self.hint_job = None
        try:
            self.hint.place_forget()
            self.hint.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ 主循环
    def tick(self):
        if not self.running:
            return
        t0 = time.perf_counter()

        # ---- 卡顿日志。跟"上一轮打算睡多久"比，超出 STALL_LOG_MS 就记一笔。
        # 以后再有人说"卡"，翻日志就能直接看到卡了多久、当时画没画、桌面露没露，
        # 不用再靠猜。--bench 模式不记（那是故意的满速跑）。
        if self._last_tick is not None and not self._bench_left:
            if self._ignore_next_gap:
                self._ignore_next_gap = False
            else:
                gap_ms = (t0 - self._last_tick) * 1000.0
                if gap_ms - self._sched_ms > STALL_LOG_MS:
                    log('主循环卡顿 %.0f ms（计划睡 %.0f，超出 %.0f）'
                        '｜上一轮 画了=%s 桌面露出=%s'
                        % (gap_ms, self._sched_ms, gap_ms - self._sched_ms,
                           self._last_drew, self._visible))

        if self._bench_left:
            self._bench_drive()          # 基准模式：喂合成光标轨迹
        # 与帧率无关的缓动需要真实帧间隔，先算好给 _step 用
        self.last_dt = min(0.25, max(0.001, t0 - (self._last_tick or t0)))
        self._last_tick = t0

        # 控制面板改过配置就即时生效（内部节流到 ~8 次/秒，stat 一次几微秒）
        self._poll_config()
        # 规则 B：用户可能在系统设置里换了壁纸（我们盖着，他看不见效果）→
        # 看一眼，变了就在面板上提示。只提示，不改变行为。
        # 内部自带 1.5 秒节流（SPI 是同步调用，绝不能按帧率来）。
        poll_system_wallpaper()

        drew = False
        try:
            if self._pending:
                self._apply_action()     # 换图请求（会弹系统选图框，阻塞是有意的）
                # 选图框挂在那儿几秒很正常，别把它报成卡顿
                self._ignore_next_gap = True
            drew = self._step()
        except Exception as e:
            log('渲染出错: %r' % e)
        self._last_drew = drew
        self._ticks += 1
        if drew and self._bench_left:
            self._bench_drawn += 1
        self._step_ms += (time.perf_counter() - t0) * 1000
        self._log_stats()

        # ---- 定时器对齐：按"下一个绝对时刻"排延时，而不是"睡固定毫秒"。
        # 睡固定值的话，渲染耗时会被累加进周期里 —— 设 40fps 实际只有 30fps，
        # 而且时长时短，眼睛看到的就是"不平滑"。
        period = 1.0 / max(10, self.cfg['fps'])
        tnow = time.perf_counter()
        if self._next_due is None:
            self._next_due = tnow + period
        self._next_due += period
        if self._next_due < tnow:        # 落后太多就重新对齐，不追债
            self._next_due = tnow + period
        delay_ms = (self._next_due - tnow) * 1000.0

        if not drew:
            # 没东西要画 → 不按帧率空转。但轮询要够密，否则鼠标一动要等
            # 下一轮才发现，手感就黏了。
            self._next_due = None
            delay_ms = IDLE_POLL_MS if self._visible else IDLE_HIDDEN_MS

        try:
            self._sched_ms = delay_ms    # 卡顿日志要拿它当基准
            self.root.after(max(1, int(round(delay_ms))), self.tick)
        except Exception:
            pass

    def _log_stats(self):
        """把"循环跑多快"和"实际画了多少帧"分开报 —— 两者不是一回事"""
        if not self.verbose:
            return
        now = time.time()
        if now - self._fps_t0 < 5.0:
            return
        span = now - self._fps_t0
        log('循环 %.1f Hz（%d 次）｜渲染 %.1f 帧/秒（%d 帧）｜'
            '每次循环 %.2f ms，其中渲染 %.2f ms｜上限 %d fps'
            % (self._ticks / span, self._ticks,
               self._frames / span, self._frames,
               self._step_ms / max(1, self._ticks),
               self._render_ms / max(1, self._frames),
               self.cfg['fps']))
        self._ticks = 0
        self._frames = 0
        self._step_ms = 0.0
        self._render_ms = 0.0
        self._fps_t0 = now

    # ------------------------------------------- 配置热重载（控制面板的通道）
    def _poll_config(self):
        """看配置文件被改过没有。控制面板就靠这条通道跟主程序说话 ——
        不用重启进程、不用 IPC、不用开端口，一个文件 + mtime 就够了。

        节流到 CFG_POLL_S 秒查一次：os.path.getmtime 是微秒级的，
        但也没必要每帧都去问文件系统。
        """
        now = time.perf_counter()
        if now - self._cfg_check < CFG_POLL_S:
            return
        self._cfg_check = now
        try:
            st = os.stat(CFG_PATH)
        except OSError:
            return
        # 变更签名用 (纳秒 mtime, 文件长度) 两个一起。
        # 单看 mtime 有个真实的漏网场景：两次写入落在同一个时钟滴答里，mtime 完全
        # 相同，后一次就被判成"没改过"而丢掉 —— 控制面板写配置正是高频动作。
        sig = (st.st_mtime_ns, st.st_size)
        if self._cfg_sign is None:
            self._cfg_sign = sig      # 启动时先记下来，别把现有配置当成"刚被改"
            return
        if sig == self._cfg_sign:
            return
        self._cfg_sign = sig
        self._apply_config_file()

    def _apply_config_file(self):
        """把 wallpaper-config.json 的内容即时套用到运行时（不重启）。"""
        try:
            with open(CFG_PATH, 'r', encoding='utf-8') as f:
                new = json.load(f)
        except Exception as e:
            # 写了一半的文件会读不动 —— 保持现状，等下一次写入
            log('配置重载失败（沿用当前设置）: %r' % e)
            return
        if not isinstance(new, dict):
            return

        def num(key, cur, lo, hi):
            try:
                return max(lo, min(hi, float(new.get(key, cur))))
            except (TypeError, ValueError):
                return cur

        size = num('size', self.size, 60.0, 2400.0)
        feather = num('feather', self.feather, 0.0, 100.0)
        glow = num('glow', self.glow_strength * 100.0, 0.0, 100.0)
        follow = num('follow', self.follow, 0.02, 0.9)
        fps = int(num('fps', self.cfg['fps'], 10, 144))

        # 换没换图，要看"配置里的原始值"变没变，不能比解析出来的绝对路径 ——
        # 用户可能在配置里写的是相对路径，一解析就变成绝对路径，那样一比永远"变了"。
        want_outer = new.get('outer', DEFAULT_CFG['outer'])
        want_inner = new.get('inner', DEFAULT_CFG['inner'])
        try:
            want_rev = int(new.get('rev', 0) or 0)
        except (TypeError, ValueError):
            want_rev = 0
        # rev 变化 = "路径一个字没改，也请把图重读一遍"。
        # 判据是"路径或 rev 任一变了"，覆盖两种用法：换图、改完图覆盖同名文件。
        need_img = (want_outer != getattr(self, 'outer_cfg', None)
                    or want_inner != getattr(self, 'inner_cfg', None)
                    or want_rev != getattr(self, 'rev', 0))
        self.rev = want_rev

        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(new)     # 内存里的配置跟文件保持一致（_apply_action 会写回它）
        self.size, self.feather = size, feather
        self.glow_strength = glow / 100.0
        self.follow = follow
        self.cfg['fps'] = fps
        self.drift_on = bool(new.get('drift', DEFAULT_CFG['drift']))
        self.pause_when_covered = bool(
            new.get('pause_when_covered', DEFAULT_CFG['pause_when_covered']))
        self.verbose = bool(new.get('verbose', DEFAULT_CFG['verbose']))

        if need_img:
            self._load_sources()
            self._prepare_images()
            self._draw_static()      # 换图 = 整屏贴图重来，44ms，一次无所谓
            log('控制面板：图片已更新 ｜ 外=%s ｜ 内=%s'
                % (self.outer_path or '默认图案', self.inner_path or '默认图案'))
        else:
            self._rebuild_spot()     # 只重建光斑那一块，毫秒级 —— 拖滑块才跟手
            # 拖滑块时这个函数能被调 8 次/秒，日志刷太快会淹掉有用的信息
            now = time.time()
            if now - self._cfg_log_at > 1.5:
                self._cfg_log_at = now
                log('控制面板：光圈=%.0f 柔和=%.0f 光晕=%.0f 跟随=%.2f 帧率=%d'
                    % (size, feather, glow, follow, fps))

    def _open_panel(self):
        """拉起控制面板。

        必须是独立进程：面板得是一个能看、能点、能拖的普通窗口，
        而我们这个主窗口是 Progman 的分层子窗口，挂在桌面图标下面 ——
        把面板做成它的 Toplevel，多半会被图标层盖住，看得见摸不着。
        """
        try:
            import subprocess
            panel = os.path.join(BASE, 'panel.pyw')
            if not os.path.exists(panel):
                log('找不到 panel.pyw，无法打开控制面板')
                return
            flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, panel], cwd=BASE,
                             creationflags=flags, close_fds=True)
            log('控制面板已拉起')
        except Exception as e:
            log('打开控制面板失败: %r' % e)

    def _local_cursor(self):
        """把屏幕坐标换算成窗口内坐标（小窗口模式下两者不一样）"""
        if self._synth is not None:
            return self._synth[0], self._synth[1], True
        try:
            mx, my = win32api.GetCursorPos()
        except Exception:
            mx, my = self.sw // 2, self.sh // 2
        if self.mode == 'window':
            try:
                self.ox = self.root.winfo_rootx()
                self.oy = self.root.winfo_rooty()
            except Exception:
                pass
            mx, my = mx - self.ox, my - self.oy
            mx = max(0, min(self.cw, mx))
            my = max(0, min(self.ch, my))
            return float(mx), float(my), True
        return float(mx), float(my), True

    def _spot_visible(self, now):
        """光斑所在的这一小块，到底露没露在桌面上。

        桌面壁纸只可能在没有别的窗口压着的地方被看见，而光斑永远跟着鼠标 ——
        所以只要鼠标底下是别的窗口（你在 IDE / 浏览器里干活），这一帧就完全
        没必要画。实测这是省 CPU 最有效的一条：切到别的窗口后几乎降到 0。

        （原先用"前台窗口是否全屏"来判断，实测不行：窗口不最大化时照样盖住桌面，
          判据就失效了。直接问系统"这个点上是哪个窗口"才准。）

         复核节奏两个方向必须不对称 —— 这是"鼠标进入桌面卡好久"的病根：

            画 → 停：可以迟钝。多画几帧只是白烧点 CPU，眼睛看不出来。
                     → 每 COVER_RECHECK_S 秒复核一次足够。
            停 → 画：必须灵敏。慢一拍就是你眼睛能看到的"鼠标动了没反应"。
                     → 每一轮都查（eager 分支，不做缓存）。

        原来两个方向共用同一个 0.25 秒缓存，再叠加隐藏时 60ms 的轮询周期，
        最坏要 ~310ms 光斑才动。现在最坏 = 一轮轮询（30ms）。

        "每一轮都查"贵不贵？WindowFromPoint + GetAncestor 是微秒级的，
        30 次/秒完全无所谓 —— 用一个测不准的缓存去省这点开销，代价是多 0.3 秒
        的响应延迟，这笔账算错了。
        """
        eager = not self._visible          # 现在没在画 → 每一轮都查，抢响应
        if not eager and now - self._cover_check < COVER_RECHECK_S:
            return self._visible
        self._cover_check = now
        visible = True
        try:
            h = win32gui.WindowFromPoint(win32api.GetCursorPos())
            if h:
                # 爬到顶层祖先：桌面上的图标、我们自己的窗口，根都是 Progman
                visible = (bool(self.progman)
                           and win32gui.GetAncestor(h, win32con.GA_ROOT) == self.progman)
        except Exception:
            visible = True      # 查不出来就照常画 —— 宁可多费点 CPU，也别让壁纸卡住
        self._visible = visible
        return visible

    def _step(self):
        now = time.time()
        mx, my, _ = self._local_cursor()

        if (mx, my) != self.last_mouse:
            first = self.last_mouse == (-1, -1)
            self.last_mouse = (mx, my)
            self.tgt_x, self.tgt_y = mx, my
            self.last_move = now
            if first:
                self.cur_x, self.cur_y = mx, my

        # 鼠标静止一会儿之后，让光斑自己缓慢游走（默认关闭，很费 CPU）
        if self.drift_on and (now - self.last_move) > 3.0:
            t = now - self.last_move
            self.tgt_x = self.cw * (0.5 + 0.34 * math.sin(t * 0.21) * math.cos(t * 0.13))
            self.tgt_y = self.ch * (0.5 + 0.30 * math.sin(t * 0.17 + 1.1))

        # ---- 缓动必须与帧率无关。
        # 原来每帧固定走掉剩余距离的 follow 比例：30fps 和 60fps 下"跟手程度"
        # 完全不同，帧间隔一抖速度就跟着抖 —— 看着就是不平滑。
        # 这里换算成时间常数：follow 仍表示"每 1/60 秒走掉剩余距离的 follow"。
        tau = -1.0 / (60.0 * math.log(max(1e-6, 1.0 - self.follow)))
        k = 1.0 - math.exp(-self.last_dt / tau)
        self.cur_x += (self.tgt_x - self.cur_x) * k
        self.cur_y += (self.tgt_y - self.cur_y) * k

        # 光标底下不是桌面（压在别的窗口上）→ 这一帧什么都不做，光斑原地冻住。
        #
        # 这里原来调 _park()：只搬 item 的位置（canvas.coords），不重新合成像素，
        # 想省掉"回到桌面时跨屏跳"的那次重绘。账算错了 ——
        # 画布上那个 item 是一张不透明的 d×d 图，内容还停在上一次真正渲染的
        # 位置上。把它挪到光标底下，桌面只要露着一角，就会看到一块 d×d 的
        # 错位方块（实测 462×462、填充率 0.96、行宽恒等于 d：是方块不是圆），
        # 和周围外图对不上 —— 眼睛看到的就是"光圈变方了"。
        #
        # 现在改成冻结：位置和像素都不动，画布一次都不碰（比原来更省 CPU）。
        # 代价是回到桌面那一帧要付一次跨屏跳的重绘（十几毫秒、一帧），
        # 换来"桌面露着的时候永远不会有错位方块" —— 这笔账才划算。
        # 想让它照常跟着鼠标走（接受更高的 CPU），把面板里那个开关关掉即可。
        if (self.mode == 'desktop' and self.pause_when_covered
                and not self._spot_visible(now)):
            if self._hidden_at is None:
                self._hidden_at = now
                self._hidden_rounds = 0
            self._hidden_rounds += 1
            self.last_render = None   # 回到桌面时必须重画（光标已经挪过位置了）
            return False
        if self._hidden_at is not None:
            log('桌面重新露出来（被盖住 %.2f 秒 / %d 轮）→ 恢复重绘'
                % (now - self._hidden_at, self._hidden_rounds))
            self._hidden_at = None

        # 完全静止就不重绘，省 CPU —— 壁纸要挂一整天。
        # 按整数像素去重：光斑最终就画在整数坐标上，亚像素的差别画出来
        # 一模一样。缓动收尾那几帧每帧只挪零点几像素，改之前全是白烧的。
        key = (int(round(self.cur_x)), int(round(self.cur_y)),
               self.size, self.feather)
        if key == self.last_render:
            return False
        self.last_render = key
        self._render(self.cur_x, self.cur_y)
        return True

    #  这里原来有个 _park()：被盖住时把光斑 item 悄悄挪到光标底下，
    #   只搬位置不重画像素，想省掉"回到桌面时跨屏跳"的那一次重绘。
    #   已删除，别加回来 —— 画布上那张 item 是不透明的 d×d 图，内容还停在
    #   上一次渲染的位置上，挪过去就在桌面上留下一块错位方块（实测
    #   462×462、填充率 0.96、行宽恒等于 d）。细节见 _step 里那段说明。

    def _render(self, x, y):
        """把光斑画到 (x, y)。

        核心思路：**在 PIL 里就把该混的色混完，交给 Tk 的永远是一张不透明的
        成品图。** 这样 Tk 侧只有「贴一张不透明图 + 挪一下位置」两件事，
        不做任何 alpha 合成 —— 那才是真正的性能杀手。
        """
        t0 = time.perf_counter()
        r = self._radius()
        d = max(2, int(r * 2))
        x, y = int(round(x)), int(round(y))

        # 光圈大小变了才需要换容器。换容器只重建光斑那一块 ——
        # 千万别顺手调 _draw_static()，那会连整屏外图一起重贴（44ms）。
        if getattr(self, '_spot_d', None) != d:
            self._rebuild_spot()
            return

        buf = self._spot_buf
        # 屏幕点 (x-r, y-r) 要落在缓冲的 (0, 0)，所以源图偏移是 (x-r, y-r)
        dx, dy = int(x - r), int(y - r)

        # ① 外图打底
        self._paste_clipped(buf, self.img_outer, dx, dy)
        # ② 光晕（和光斑同尺寸，永远不需要裁）
        g = self._glow_small
        if g is not None:
            buf.paste(g, (0, 0), g)
        # ③ 内图，直接拿羽化遮罩当蒙版混进去 —— alpha 在这一步被消化掉
        self._paste_clipped(buf, self.img_inner, dx, dy, self._get_mask(d, d))
        t1 = time.perf_counter()

        try:
            self.tk_spot.paste(buf)
        except Exception as e:
            log('就地更新失败，回退为重建成新图: %r' % e)
            self.tk_spot = ImageTk.PhotoImage(buf)
            self.canvas.itemconfig(self.spot_id, image=self.tk_spot)
        t2 = time.perf_counter()

        self.canvas.coords(self.spot_id, x, y)
        t3 = time.perf_counter()

        self._t_pil += (t1 - t0) * 1000
        self._t_paste += (t2 - t1) * 1000
        self._t_coord += (t3 - t2) * 1000

        if self.verbose:
            self._frames += 1
            self._render_ms += (t3 - t0) * 1000

    # ------------------------------------------------------------ 性能基准
    def _bench_drive(self):
        """基准模式：喂一个合成的平滑光标轨迹，并记下每帧的实际间隔。"""
        self._bench_left -= 1
        i = self._bench_n - self._bench_left
        a = i / 45.0
        self._synth = (self.cw * (0.5 + 0.36 * math.sin(a)),
                       self.ch * (0.5 + 0.30 * math.cos(a * 0.73)))
        now = time.perf_counter()
        if self._bench_prev is not None:
            self._bench_gaps.append((now - self._bench_prev) * 1000.0)
        self._bench_prev = now
        if self._bench_left <= 0:
            self._bench_report()

    def _bench_report(self):
        """报告真实帧率与真实 CPU 消耗。

        跑在真实主循环里，测出来的帧率和 CPU 时间就是实际运行的数字。
        """
        gaps = sorted(self._bench_gaps)
        cnt = max(1, len(gaps))
        med = gaps[cnt // 2]
        avg = sum(gaps) / cnt
        p90 = gaps[min(cnt - 1, int(cnt * 0.9))]
        cpu_ms = 0.0
        try:
            cpu_ms = (process_cpu_seconds() - self._bench_cpu0) * 1000.0 / cnt
        except Exception:
            pass
        drawn = max(1, self._bench_drawn)
        pil = self._t_pil / drawn
        pst = self._t_paste / drawn
        crd = self._t_coord / drawn
        rest = max(0.0, avg - pil - pst - crd)

        print('')
        print('=' * 76)
        print('  光斑渲染基准（真实主循环，不封顶）')
        print('  屏幕 %dx%d   光斑直径 %d   羽化 %d   光晕 %d'
              % (self.cw, self.ch, int(self._radius() * 2),
                 self.feather, self.cfg.get('glow', 0)))
        print('=' * 76)
        print('  实测帧率          %.1f fps（跑满上限所需）' % (1000.0 / max(0.01, avg)))
        print('  帧间隔            中位 %.2f ms   p90 %.2f ms   平均 %.2f ms'
              % (med, p90, avg))
        print('  ★ 每帧 CPU 占用   %.2f ms（进程实测，%d 帧采样）' % (cpu_ms, cnt))
        print('')
        print('  墙钟时间拆解（每帧）')
        print('    ├─ PIL 合成         %.2f ms' % pil)
        print('    ├─ 上传 Tk 相册     %.2f ms' % pst)
        print('    ├─ 移动 item        %.2f ms' % crd)
        print('    └─ 画布重绘 + 空闲  %.2f ms' % rest)
        print('    （渲染 %d 帧 / 共 %d 帧）' % (self._bench_drawn, cnt))
        print('')
        print('  按每帧 CPU %.2f ms 折算的常年占用' % cpu_ms)
        for fps in (30, 40, 60):
            print('    %-3d fps → 单核 %5.1f%%' % (fps, cpu_ms * fps / 10.0))
        print('  （鼠标不动或桌面被盖住时接近 0%：不重绘）')
        print('=' * 76)
        print('')
        try:
            sys.stdout.flush()
        except Exception:
            pass
        self._synth = None
        self._quit()

    # ------------------------------------------------------------ 看护
    def _start_watchdog(self):
        self.attach_fails = 0
        self.root.after(4000, self._watchdog)

    def _watchdog(self):
        """常驻看护：处理换分辨率、explorer 重启这类"壁纸会掉"的情况。

        挂得住的就顺手刷新一下；挂不住的先把自己藏起来（防止盖住图标），
        连续几次都不行才退出并重启自己。
        """
        if not self.running:
            return
        try:
            if self.mode == 'desktop':
                self._watch_desktop()
            else:
                self._watch_screen()
        except Exception as e:
            log('看护出错: %r' % e)
        if self.running:
            try:
                self.root.after(4000, self._watchdog)
            except Exception:
                pass

    def _watch_screen(self):
        """小窗口/全屏预览：只关心分辨率变了没"""
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        if (sw, sh) != (self.sw, self.sh):
            log('分辨率变化 %s → %s' % ((self.sw, self.sh), (sw, sh)))
            self.sw, self.sh = sw, sh
            if self.mode == 'full':
                self.root.geometry('%dx%d+0+0' % (sw, sh))

    def _watch_desktop(self):
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

        # 窗口自己没了（父窗口被销毁会连带销毁子窗口）→ 重启一个自己
        if not win32gui.IsWindow(self.hwnd):
            log('看护：窗口已销毁，重新启动自己')
            self._relaunch()
            return

        # 先判"现在是不是一切正常"，正常就什么都别做。
        # 关键：resolve_host 会向 Progman 发 0x052C，绝不能每 4 秒发一次。
        ok = False
        try:
            ok = (bool(self.host) and win32gui.IsWindow(self.host)
                  and (sw, sh) == (self.sw, self.sh)
                  and self.hwnd in children_zorder(self.host)
                  and self._style_ok()
                  and self._verify_above_icons())
        except Exception:
            ok = False

        if ok:
            # 这里原来每 4 秒发一次 RedrawWindow，想"让桌面重画一次，免得被
            # 别的程序刷没了"。现在整条拿掉，两条理由：
            #   ① 它从来没生效过 —— 常量名写错（win32con 里没有 RDW_UPDATABLE），
            #      一直在抛异常被吞掉；
            #   ② 就算修好也是有害的：实测整窗失效+兑现要 44ms，而且是
            #      延迟兑现的 —— 这笔账会一直挂着，等桌面下一次露出来的
            #      那一刻才还，正好砸在用户看得见的一帧上。
            # 现在靠"露面时自然重画"顶替：从被盖住切回可见的第一帧反正要
            # 重新合成一次光斑，那一下顺带就把该刷的地方刷掉了。
            self.attach_fails = 0
            return

        log('看护：需要重挂（尺寸 %s→%s，旧宿主=%s）'
            % ((self.sw, self.sh), (sw, sh), self.host_desc or '无'))
        host, after, desc, progman = resolve_host(sw, sh)
        self.sw, self.sh = sw, sh
        self.host, self.after, self.host_desc, self.progman = (
            host, after, desc, progman)
        self.layered = bool(progman) and needs_layered_child(progman)
        if not self.host:
            log('看护：暂时找不到桌面层宿主，下一轮再试')
            return
        try:
            self.root.geometry('%dx%d+0+0' % (sw, sh))
            self.canvas.config(width=sw, height=sh)
            if (self.cw, self.ch) != (sw, sh):
                self.cw, self.ch = sw, sh
                self._prepare_images()
                self._draw_static()
            self.hwnd = self._hwnd()
            self._apply_placement()
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
            self._apply_placement(repaint=True)   # 再压一次，防它被抬到图标上面
            if self.layered:
                # 同上：抖动一像素，逼 DWM 重新合成
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, sw - 1, sh - 1,
                                      win32con.SWP_NOACTIVATE |
                                      win32con.SWP_NOZORDER | win32con.SWP_NOMOVE)
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, sw, sh,
                                      win32con.SWP_NOACTIVATE |
                                      win32con.SWP_NOZORDER | win32con.SWP_NOMOVE)
                win32gui.UpdateWindow(self.hwnd)
        except Exception as e:
            log('看护重挂失败: %r' % e)
            return

        if self._verify_above_icons():
            self.attach_fails = 0
            log('看护：重挂成功 —— %s' % self.host_desc)
        else:
            # 位置不对就先藏起来，宁可暂时没有壁纸，也不能盖住桌面图标
            self.attach_fails += 1
            log('看护：z 序不对，先隐藏窗口（第 %d 次）' % self.attach_fails)
            try:
                win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            if self.attach_fails >= 3:
                log('看护：连续 3 次失败，退出')
                self._quit()

    def _relaunch(self):
        """重新拉起一个自己（explorer 重启后窗口会被连带销毁）。

         必须等新进程真的接管了再退自己。原来是拉起就跑、自己立刻
          os._exit(0) —— 中间有一段两个进程都在跑的窗口期：pid 文件已经是新进程
          写的，屏幕上却可能还挂着旧窗口（或者反过来），用户看到的就是"闪一下"。
          握手的判据用 pid 文件：新进程在 run() 里第一件事就是登记自己，
          等文件里的 pid 换成一个活着的、且不是我的 pid，就算交接完成。
        """
        import subprocess

        def alive(p):
            """这个 pid 还活着吗（OpenProcess 能开 + 退出码是 STILL_ACTIVE）"""
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(p))       # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == 259
            finally:
                k32.CloseHandle(h)

        try:
            flags = 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, os.path.abspath(__file__), '--restarted'],
                             cwd=BASE, creationflags=flags, close_fds=True)
            log('看护：新进程已拉起')
        except Exception as e:
            log('看护：重启失败 %r' % e)
            self.running = False
            os._exit(0)

        me = os.getpid()
        t0 = time.time()
        while time.time() - t0 < RELAUNCH_HANDSHAKE_S:
            try:
                with open(PID_PATH, 'r') as f:
                    newpid = int((f.read() or '0').strip() or 0)
            except Exception:
                newpid = 0
            if newpid and newpid != me and alive(newpid):
                log('看护：新进程 %d 已接管（等了 %.1fs），本进程让位'
                    % (newpid, time.time() - t0))
                break
            time.sleep(0.1)
        else:
            log('看护：等了 %.1fs 没等到新进程接管，仍然退出'
                '（宁可短暂没壁纸，也不留两个进程抢同一块桌面）' % RELAUNCH_HANDSHAKE_S)
        self.running = False
        os._exit(0)

    # -------------------------------------------------------- 退出通道
    def _start_hotkey(self):
        t = threading.Thread(target=self._hotkey_loop, daemon=True)
        t.start()

    def _hotkey_loop(self):
        """全局热键，轮询实现：不占焦点，窗口失焦也照样生效。

          Ctrl + Alt + Q   退出
          Ctrl + Alt + P   打开控制面板（选图 / 调光圈大小等，改完即时生效）
          Ctrl + Alt + 1   换「光斑外」的图
          Ctrl + Alt + 2   换「光斑内」的图
          Ctrl + Alt + R   重新读取图片（刚往 images/ 里放了图时用）

        这里只写 self._pending，具体的换图动作交给 Tk 主线程做。
        """
        while self.running:
            time.sleep(0.1)
            try:
                if (win32api.GetAsyncKeyState(win32con.VK_CONTROL) < 0 and
                        win32api.GetAsyncKeyState(win32con.VK_MENU) < 0):
                    if win32api.GetAsyncKeyState(0x51) < 0:          # Q
                        log('收到退出热键 Ctrl+Alt+Q')
                        self._quit()
                        return
                    if self._pending is None:
                        act = None
                        if win32api.GetAsyncKeyState(0x31) < 0:      # 1
                            act = 'outer'
                        elif win32api.GetAsyncKeyState(0x32) < 0:    # 2
                            act = 'inner'
                        elif win32api.GetAsyncKeyState(0x52) < 0:    # R
                            act = 'reload'
                        elif win32api.GetAsyncKeyState(0x50) < 0:    # P
                            act = 'panel'
                        if act:
                            self._pending = act
                            time.sleep(0.45)      # 免得按住不放反复触发
            except Exception:
                pass

    def _quit(self, *_):
        log('退出')
        self.running = False
        try:
            self.root.after(0, self._destroy)
        except Exception:
            self._destroy()

    def _destroy(self):
        # 先藏 → 摘 → 请重画，最后才 destroy。
        #
        # 为什么不直接 destroy：我们的窗口是 Progman / WorkerW 的分层子窗口，
        # 而 Progman 带 WS_EX_NOREDIRECTIONBITMAP（它自己没有 GDI 内容，画面全靠
        # DWM 合成）。子窗口一销毁，父窗口那块区域由谁重画、什么时候重画，文档里
        # 没写死 —— 赌错的表现就是"退出之后桌面上留着一块没擦掉的旧画面"。
        # SetParent(hwnd, 0) 先把窗口从子窗口链里摘出来（父窗口会收到通知、拿到
        # 失效区域），再向宿主要一次立即兑现的重画，destroy 就只剩收尾了。
        try:
            h = getattr(self, 'hwnd', 0)
            if h:
                win32gui.ShowWindow(h, win32con.SW_HIDE)
                win32gui.SetParent(h, 0)
            if getattr(self, 'host', 0):
                win32gui.RedrawWindow(self.host, None, None,
                                      RDW_INVALIDATE | RDW_ERASE |
                                      RDW_UPDATENOW | RDW_ALLCHILDREN)
        except Exception as e:
            log('退出前摘窗口失败（不影响退出，最多留一下残影）: %r' % e)
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            ctypes.windll.winmm.timeEndPeriod(1)     # 归还定时器精度
        except Exception:
            pass
        try:
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except Exception:
            pass
        os._exit(0)

    # ------------------------------------------------------------ 启动
    def run(self):
        try:
            with open(PID_PATH, 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        log('启动完成 pid=%s 模式=%s 挂载=%s'
            % (os.getpid(), self.mode, self.attach_mode))
        if self.selftest:
            self._selftest_report()
            try:
                self.root.destroy()
            except Exception:
                pass
            return
        self.root.mainloop()

    def _selftest_report(self):
        """只做结构自检 —— 用来确认挂载是否真的正确"""
        print('屏幕          : %dx%d' % (self.sw, self.sh))
        print('模式          : %s' % self.mode)
        print('宿主          : %s (hwnd=%s)' % (self.host_desc or '无', self.host))
        print('插入到        : %s (hwnd=%s)' % (self.after or '默认', self.after))
        print('挂载结果      : %s' % self.attach_mode)
        if self.fallback_reason:
            print('降级原因      : %s' % self.fallback_reason)

        try:
            self.hwnd = self._hwnd()      # 降级后窗口是新建的，句柄要重新取
        except Exception as e:
            print('取窗口句柄    : 失败 %r' % e)
            return

        try:
            print('本窗口        : hwnd=%s 类名=%s' % (self.hwnd,
                                                     win32gui.GetClassName(self.hwnd)))
            ex = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
            print('窗口矩形      : %s 可见=%s'
                  % (win32gui.GetWindowRect(self.hwnd),
                     bool(win32gui.IsWindowVisible(self.hwnd))))
            print('扩展样式      : %s  分层要求=%s → %s'
                  % (hex(ex & 0xFFFFFFFF), self.layered,
                     '已带 WS_EX_LAYERED' if ex & WS_EX_LAYERED else '没有 WS_EX_LAYERED'))
        except Exception as e:
            print('窗口信息      : 读取失败 %r' % e)

        progman = win32gui.FindWindow('Progman', None)
        print('z 序校验      : %s' % ('通过（图标层盖在我们上面）'
                                      if self._verify_above_icons() else '不通过'))
        print('-- Progman 的子窗口（从上到下，索引小 = 更靠上）--')
        for i, h in enumerate(children_zorder(progman)):
            try:
                r = win32gui.GetWindowRect(h)
                print('   [%d] %-22s %-18s %5dx%-5d %s%s'
                      % (i, win32gui.GetClassName(h),
                         win32gui.GetWindowText(h)[:18],
                         r[2] - r[0], r[3] - r[1],
                         '可见' if win32gui.IsWindowVisible(h) else '隐藏',
                         '  <== 本程序' if h == self.hwnd else ''))
            except Exception:
                pass

        # 抓一张桌面层的图：确认壁纸是真的被画出来了，而不是只有结构对
        try:
            out = os.path.join(BASE, '_desktop_capture.png')
            img = capture_window(progman, out)
            print('桌面层截图    : %s  (%dx%d)' % (out, img.width, img.height))
        except Exception as e:
            print('桌面层截图    : 失败 %r' % e)


# ==================================================================== 入口
def main():
    # 高 DPI 屏下按物理像素计算，避免模糊和坐标错位
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # 把系统定时器精度提到 1ms。
    # 不这样做的话 Windows 默认精度是 15.6ms，Tk 的 after(16) 会一直错过
    # 第一个 tick、落到第二个 —— 实测恒定只有 32Hz，比设定的 60fps 差一半。
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    argv = sys.argv[1:]
    if '--restarted' in argv:
        time.sleep(1.5)      # 等旧进程彻底退出，免得互斥体还没释放
    if '--window' in argv:
        mode = 'window'
    elif '--full' in argv:
        mode = 'full'
    else:
        mode = 'desktop'

    if already_running():
        log('已有实例在运行，本次启动取消')
        print('聚光壁纸已经在运行了。要重开请先双击「停止壁纸.bat」。')
        return

    save_default_config()

    bench = 0
    if '--bench' in argv:
        i = argv.index('--bench')
        bench = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 240

    app = SpotlightWallpaper(mode=mode, selftest='--selftest' in argv, bench=bench)
    app.run()


if __name__ == '__main__':
    main()
