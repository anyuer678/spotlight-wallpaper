#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""聚光壁纸 · 控制面板（网页版）

    wallpaper.pyw   壁纸本体（挂在桌面图标下面）
    panel.pyw       控制面板 = 微型 HTTP 服务 + 界面宿主 + 托盘图标   ← 本文件
    panel.html      界面本体（自包含，零外部请求）

「最小化到托盘」为什么得由我们从外面看着
------------------------------------------------------------------
面板窗口是 Edge 的 `--app` 窗口，窗口过程在 Edge 手里 —— 我们改不了它的最小化
行为。所以只能在外面轮询：一旦发现它变成最小化，就 `SW_HIDE` 藏掉（任务栏按钮
跟着消失），再靠托盘图标把它捞回来。托盘本身是一个隐藏窗口 + 消息循环，
全用 ctypes 手写（不用 pywin32 的 WNDCLASS 包装），因为那块的行为没有明确
文档、赌错的表现是"窗口建不出来"或"菜单点了不响应"，都不好查。
细则和两条安全线见下面「托盘」那一节。

为什么从 tkinter 换到网页
------------------------------------------------------------------
面板要的是「能拖、能输入精确值、有过渡、能放缩略图、能滚动」，这恰好全是
Tk 原生控件的短板 —— 旧版滑块手柄和背景同色、滑块根本看不见；窗口被硬编码
成不可缩放且永远置顶；长内容没有滚动条。而这些用 CSS 表达几乎零成本。

所以界面交给浏览器渲染，Python 只做它真正擅长的事：读写文件、解码图片、
按主程序同一套算法合成预览（这样"预览里看到的"就等于"桌面上出现的"）、
以及开系统原生选图框。

为什么不用 pywebview / Flask
------------------------------------------------------------------
这个项目的硬规矩是「零第三方依赖、双击即用」。http.server 和 Edge 都是系统
里现成的，够用。服务只绑 127.0.0.1，不对外开端口。

跟主程序怎么通信
------------------------------------------------------------------
仍然是最省事、最不容易坏的那条路：原子写 wallpaper-config.json。
主程序在主循环里盯着这个文件的 (mtime_ns, size) 签名，变了就即时套用 ——
不重启进程、不 IPC、不共享内存。

（这条通道有个反面教训：旧面板"选完图没反应"，就是因为写盘前的比对基准
  用错了变量 —— 先把新值写进了内存里的 cfg，再拿 cfg 跟自己比，永远"没变"。
  现在所有写盘都只从界面传来的值重新算一份完整配置，不再"就地改内存"。）
"""

import ctypes
import importlib.machinery
import importlib.util
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import win32api
import win32con
import win32gui

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'wallpaper-config.json')
CFG_TMP = CFG_PATH + '.tmp'
PID_PATH = os.path.join(BASE, 'wallpaper.pid')
LOG_PATH = os.path.join(BASE, 'wallpaper.log')
WALLPAPER = os.path.join(BASE, 'wallpaper.pyw')
PAGE = os.path.join(BASE, 'panel.html')
IMAGES_DIR = os.path.join(BASE, 'images')

VERSION = '2.0'
WINDOW_TITLE = '聚光壁纸 · 控制面板'
WINDOW_W, WINDOW_H = 1180, 830

# 只允许从本机访问。Origin 白名单在端口确定后填。
PANEL_ORIGIN = None
_PORT = 0

# 滑块合法区间（面板与主程序两侧都要夹一次：主程序那道在 _apply_config_file）
LIMITS = {
    'size': (60.0, 1200.0),
    'feather': (0.0, 100.0),
    'glow': (0.0, 100.0),
    'follow': (0.02, 0.90),
    'fps': (20, 144),
}
INT_KEYS = ('size', 'feather', 'glow', 'fps')
FLOAT_KEYS = ('follow',)
BOOL_KEYS = ('drift', 'pause_when_covered', 'verbose')
PATH_KEYS = ('outer', 'inner')

# 取值只能是几种固定写法的键。
#  新增配置键必须在这里登记：sanitize() 是"按认识的键逐个过一遍"的写法，
#   没登记的键就算 POST 上来、写进了 json，也会在构造新配置的那一刻被丢掉 ——
#   而且一声不吭（这就是规则 A 的 panel_exit_action 必须登记的原因）。
STR_KEYS = {
    'panel_exit_action': ('ask', 'keep_wallpaper', 'stop_wallpaper'),
}

# 只属于控制面板、不属于"效果参数"的键。
# 主程序那个「恢复默认效果」会拿 DEFAULT_CFG 重写整个配置，不特殊照顾的话
# 会把用户"不再询问"的记忆一起清掉 —— 那不是"恢复默认"，那是弄丢设置。
PANEL_ONLY_KEYS = tuple(STR_KEYS)

ALLOWED_IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif')

#  必须在模块级就声明 DPI 感知，而且必须早于任何 GetSystemMetrics 调用。
#
# 本机是 2560×1600 物理 / 150% 缩放。不声明的话，Windows 会把 GetSystemMetrics
# 的返回值按 0.667 虚拟化，屏幕尺寸量出来是 1707×1067 —— 于是预览的取景框
# 会按一块根本不存在的屏幕去裁图，画面位置全错。
#
# 只写在 main() 里是不够的：那样"被 import 进来用"的场景（测试、别的脚本复用）
# 全都会量错。我就是在把 panel 当模块加载做自测时撞上的。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
except Exception:
    pass


def log(msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] 面板 %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass


# ================================================================ 复用主程序
def load_core():
    """把 wallpaper.pyw 当模块加载，共用它的图像工具与选图框。

    wallpaper.pyw 里所有"会建窗口"的代码都在 main() 里且有 __main__ 保护，
    所以加载它不会顺手开一个壁纸出来。
    """
    loader = importlib.machinery.SourceFileLoader('spotlight_core', WALLPAPER)
    spec = importlib.util.spec_from_loader('spotlight_core', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CORE = load_core()


def cfg_read():
    """读配置，永远返回一份含全部默认键的完整字典。

    读坏了（比如主程序正在写）就退回默认值 —— 面板宁可显示默认值，
    也不能因为读到半个文件就把界面搞崩。
    """
    cfg = dict(CORE.DEFAULT_CFG)
    try:
        with open(CFG_PATH, 'r', encoding='utf-8') as f:
            got = json.load(f)
        if isinstance(got, dict):
            cfg.update(got)
    except FileNotFoundError:
        pass
    except Exception as e:
        log('读配置失败（用默认值）: %r' % e)
    return cfg


# 配置的读-改-写要串起来。用 RLock：apply_config 持锁期间还会调 cfg_write，
# 不可重入的 Lock 会当场自锁死。
_CFG_LOCK = threading.RLock()


def cfg_write(cfg):
    """原子写：先写 .tmp 再 os.replace。

    主程序随时可能在读这个文件，绝不能让它读到写了一半的内容。
    os.replace 在同一卷上是原子的，读的人只会看到"旧的完整版"或"新的完整版"。

     必须串行化。 这是 ThreadingHTTPServer，两个 POST 完全可能同时在跑；
      两个线程同时 `open(CFG_TMP, 'w')`，在 Windows 上前一个已经持有写句柄，
      后一个会直接 `PermissionError: [Errno 13]`。界面上表现为"拖一下滑块没生效"，
      而日志里只有一条 500 —— 实测就是键盘连按 / 快速拖动时撞出来的。
      另外对瞬时占用（杀毒、索引器扫到这个文件）留几次重试。
    """
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    with _CFG_LOCK:
        err = None
        for attempt in range(6):
            try:
                with open(CFG_TMP, 'w', encoding='utf-8') as f:
                    f.write(data)
                os.replace(CFG_TMP, CFG_PATH)
                return True
            except PermissionError as e:      # 多半是别的进程/线程正攥着它
                err = e
                time.sleep(0.02 * (attempt + 1))
            except Exception as e:
                err = e
                break
        log('写配置失败: %r' % (err,))
        return False


# ================================================================ 小工具
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def pid_alive(pid):
    """进程还活着吗。

    不能只看 pid 文件在不在 —— 被强杀时不走清理逻辑，pid 文件会留下来骗人，
    而那个 pid 可能已经被系统分配给了别的程序。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def read_pid():
    try:
        with open(PID_PATH, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return 0


def proc_cpu_seconds(pid):
    """进程累计占用 CPU 的秒数（内核态 + 用户态）。

    用 GetProcessTimes 而不是墙钟 —— 面板上要显示"壁纸到底吃了多少 CPU"，
    这个数必须是真的 CPU 时间。
    """
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid or 0))
    if not h:
        return None
    try:
        c, e, k, u = (ctypes.c_ulonglong() for _ in range(4))
        ok = ctypes.windll.kernel32.GetProcessTimes(
            h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
        if not ok:
            return None
        return (k.value + u.value) / 1e7      # 100ns 单位 → 秒
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def screen_size():
    """屏幕逻辑尺寸。进程已声明 DPI 感知，所以直接拿到的就是真实像素。"""
    try:
        return (int(ctypes.windll.user32.GetSystemMetrics(0)),
                int(ctypes.windll.user32.GetSystemMetrics(1)))
    except Exception:
        return (1920, 1080)


# ---------------------------------------------------------------- CPU 采样
_cpu_lock = threading.Lock()
_cpu_prev = [0, 0.0, 0.0]        # [pid, 采样时刻, 那时的 cpu 秒数]


def wallpaper_cpu_percent(pid):
    """两次 /api/state 之间的增量算单核占比。

    第一次调用只能建档、给不出数字，返回 None —— 显示"—"比显示"0%"诚实。
    """
    if not pid or not pid_alive(pid):
        with _cpu_lock:
            _cpu_prev[0] = 0
        return None
    now = time.perf_counter()
    sec = proc_cpu_seconds(pid)
    if sec is None:
        return None
    with _cpu_lock:
        if _cpu_prev[0] != pid or _cpu_prev[1] == 0.0:
            _cpu_prev[:] = [pid, now, sec]
            return None
        dt = now - _cpu_prev[1]
        dsec = sec - _cpu_prev[2]
        _cpu_prev[:] = [pid, now, sec]
    if dt <= 0.2:                # 问得太密，噪声比信号大
        return None
    return max(0.0, dsec / dt * 100.0)


# ---------------------------------------------------------------- 图片信息
_size_cache = {}


def image_size(path):
    """只读文件头拿宽高（不是整张解码），带 (path, mtime, size) 缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _size_cache.get(key)
    if hit is not None:
        return hit
    try:
        with Image.open(path) as im:
            got = im.size
    except Exception:
        got = None
    if len(_size_cache) > 64:      # 换个图就换一批 key，别让它无限长
        _size_cache.clear()
    _size_cache[key] = got
    return got


def image_info(cfg, side):
    """左右两侧的图现在到底用的是哪张 —— 如实报告，不猜。"""
    path = CORE.resolve_side(cfg, side)
    info = {
        'cfg': cfg.get(side) or '',
        'path': path or '',
        'name': os.path.basename(path) if path else '',
        'dir': os.path.dirname(path) if path else '',
        'exists': bool(path),
        'placeholder': not path,
        'bytes': 0, 'w': 0, 'h': 0,
    }
    if path:
        try:
            info['bytes'] = os.stat(path).st_size
        except OSError:
            pass
        w_h = image_size(path)
        if w_h:
            info['w'], info['h'] = w_h
    return info


# ---------------------------------------------------------------- 壁纸状态
def probe_wallpaper():
    """壁纸进程的真实状态。

    光看"pid 还活着"是不够的 —— 它可能活着但压根没挂上去（降级成了小窗口），
    或者被别的程序从桌面层里挤掉了。**硬证据只有一个：Progman 底下有没有
    我们这个分层子窗口。** 所以这里直接去枚举 Progman 的子窗口按样式认人，
    而不是猜。
    """
    win32gui, win32con = CORE.win32gui, CORE.win32con
    pid = read_pid()
    out = {'pid': pid, 'alive': bool(pid) and pid_alive(pid),
           'attached': False, 'hwnd': 0, 'mode': '未运行',
           'zorder': [], 'cpu_percent': None, 'note': ''}
    if not out['alive']:
        if pid:
            out['note'] = 'pid 文件里记着 %d，但那个进程已经不在了' % pid
        return out

    out['cpu_percent'] = wallpaper_cpu_percent(pid)
    try:
        progman = win32gui.FindWindow('Progman', None)
        if progman:
            kids = CORE.children_zorder(progman)
            out['zorder'] = [win32gui.GetClassName(h) for h in kids]
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOACTIVATE = 0x08000000
            for h in kids:
                if win32gui.GetClassName(h) != 'TkTopLevel':
                    continue
                ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
                if (ex & WS_EX_LAYERED) and (ex & WS_EX_NOACTIVATE):
                    out['attached'] = True
                    out['hwnd'] = int(h)
                    break
    except Exception as e:
        out['note'] = repr(e)

    if out['attached']:
        out['mode'] = '已挂到桌面层（在桌面图标下面）'
    else:
        out['mode'] = '没挂上桌面层（多半降级成了安全小窗口，按 Esc 能关掉）'
    return out


# ================================================================ 预览合成
# 下面四个函数是从旧面板原样搬过来的 —— 它们已经被 _test_preview.py 逐像素
# 验证过（29 项，含四边四角越界裁剪、光晕落位零偏差）。搬过来时一行没改，
# 因为"预览和桌面要对得上"这件事，容不下第二个实现。


def paste_at(dst, src, dx, dy, mask=None, mx=0, my=0):
    """把 src 按 (dx, dy) 对齐叠到 dst 上，越界一律跳过。

    两套偏移是互相独立的，这是最容易搅混的地方：

        dst(i, j) ← src(i + dx, j + dy)      图像怎么对齐
        混色权重 ← mask(i - mx, j - my)       蒙版左上角落在 dst 的 (mx, my)

    为什么不直接用 Pillow 的 crop：它在越界处会填黑，光斑贴到画面边缘
    就会出现黑框。画布外面本来就画不出来，只贴交集才对。
    """
    DW, DH = dst.size
    SW, SH = src.size
    i0, i1 = max(0, -dx), min(DW, SW - dx)
    j0, j1 = max(0, -dy), min(DH, SH - dy)
    if mask is not None:
        MW, MH = mask.size
        i0, i1 = max(i0, max(0, mx)), min(i1, min(DW, mx + MW))
        j0, j1 = max(j0, max(0, my)), min(j1, min(DH, my + MH))
    if i1 <= i0 or j1 <= j0:
        return
    piece = src.crop((dx + i0, dy + j0, dx + i1, dy + j1))
    if mask is None:
        dst.paste(piece, (i0, j0))
    else:
        dst.paste(piece, (i0, j0), mask.crop((i0 - mx, j0 - my,
                                              i1 - mx, j1 - my)))


def place_at(dst, src, x, y, mask=None, mask_xy=None):
    """把 src 的左上角放到 dst 的 (x, y)，蒙版默认跟着一起放。

    "把一块东西放到某个位置"才是脑子里的直觉写法。它和 paste_at 之间只差一个
    符号，而那个符号正是最容易写错的地方 —— 光晕就因为符号写反，被整块贴到了
    画布左上角外面，画面看着只是"左上角有点发灰"，靠眼睛根本发现不了。
    """
    if mask is None:
        paste_at(dst, src, -x, -y)
        return
    mx, my = mask_xy if mask_xy is not None else (x, y)
    paste_at(dst, src, -x, -y, mask, mx, my)


def alpha_as_mask(img):
    """RGBA → 它自己的 alpha 就是蒙版；L → 灰度蒙版；RGB → 不用蒙版。"""
    return img if img.mode in ('RGBA', 'L') else None


def compose_preview(bg, inner, mask, glow, cx, cy):
    """预览合成 = 底图 + 光晕 + 羽化内图。

    特意抽成纯函数（不碰任何控件），就是为了能脱离窗口单独做数值验证 ——
    光晕的符号写反时，画面上只是"左上角有点发灰"，看截图根本发现不了。
    """
    buf = bg.copy()
    r = mask.size[0] / 2.0
    ox, oy = int(cx - r), int(cy - r)     # 光斑左上角
    if glow is not None:
        place_at(buf, glow, ox, oy, alpha_as_mask(glow))
    # 内图与底图是同一块画面的两个版本，所以按原位 0,0 叠加；
    # 决定"哪儿露出内图"的活全交给蒙版，蒙版才需要放到光斑左上角。
    place_at(buf, inner, 0, 0, mask, mask_xy=(ox, oy))
    return buf


# ---------------------------------------------------------------- 场景缓存
class SceneCache(object):
    """整屏两幅图 + 按取景框裁好的预览底图，都缓起来。

    一帧预览的真实成本几乎全在"把 2560×1600 裁一块再重采样到画布大小"
    这两下上（LANCZOS，各 ~2ms）。而拖着光斑跑的时候取景框根本不变 ——
    缓存命中之后一帧只剩两次 paste，1ms 出头。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.token = None         # 两张图的"身份"（路径+mtime+大小）
        self._token_at = 0.0      # 上次算这个身份的时刻，见 _current_token
        self.full = {}            # side -> cover 到屏幕尺寸的图
        self.box = {}             # side -> 按取景框裁好并缩放到画布尺寸的图
        self.mask = None
        self.mask_key = None
        self.glow = None
        self.glow_key = None

    def prime(self):
        """提前把"解码两张图 + 各缩放到整屏"做掉。

        实测这一步要 ~340ms（换成 4K 原图更久）。留到用户第一次在预览上
        拖动时才做的话，第一下拖动就会像卡住 —— 而它其实只是一次性的准备，
        完全可以趁界面还在加载的时候在后台做完。
        """
        try:
            self.ensure_full(*screen_size())
            log('预览底图已预热（解码 + 铺满整屏）')
        except Exception as e:
            log('预览预热失败（不影响使用，只是首次预览会慢一下）: %r' % e)

    def _current_token(self):
        """图的"当前身份"：路径 + 修改时间 + 大小。

         别每帧都算：它要读一次配置文件（open + json 解析）。预览是每秒几十次
          的热路径，几百微秒乘以几十次就不划算了。0.4 秒足够 —— 换图之后
          最迟 0.4 秒预览就会跟上，肉眼看不出来。
        """
        now = time.perf_counter()
        with self.lock:
            if (self.token is not None
                    and now - self._token_at < 0.4):
                return self.token
        cfg = cfg_read()
        parts = []
        for side in ('outer', 'inner'):
            p = CORE.resolve_side(cfg, side)
            if p:
                try:
                    st = os.stat(p)
                    parts.append((p, st.st_mtime_ns, st.st_size))
                except OSError:
                    parts.append((p, 0, 0))
            else:
                parts.append((None, 0, 0))
        token = tuple(parts)
        with self.lock:
            self._token_at = now
        return token

    def ensure_full(self, sw, sh):
        """把两幅源图铺满屏幕（和主程序 cover() 完全一致的取景方式）。

         这里必须和主程序用同一个 cover()。取景方式一旦不同，预览里
          看到的构图就和桌面上不一样，"预览"就没有意义了。
        """
        token = self._current_token()
        with self.lock:
            if token != self.token or not self.full:
                src = {}
                for side in ('outer', 'inner'):
                    p = CORE.resolve_side(cfg_read(), side)
                    im = None
                    if p:
                        try:
                            im = Image.open(p)
                            im.load()
                            im = im.convert('RGB')
                        except Exception as e:
                            log('面板读图失败 %s: %r' % (p, e))
                            im = None
                    if im is None:
                        im = CORE.make_placeholder(side, 1600, 900)
                    src[side] = im
                self.full = {
                    'outer': CORE.cover(src['outer'], sw, sh),
                    'inner': CORE.cover(src['inner'], sw, sh),
                }
                self.token = token
                self.box = {}
                self.box_token = None
            return self.full

    def view(self, side, box, vw, vh):
        """取景框 → 画布尺寸的底图（带缓存）"""
        sw, sh = screen_size()
        full = self.ensure_full(sw, sh)
        key = (side, box, int(vw), int(vh))
        with self.lock:
            if key in self.box:
                return self.box[key]
            im = full[side].crop(box).resize((int(vw), int(vh)), Image.LANCZOS)
            if len(self.box) > 12:       # 只留着刚用过的那几档
                self.box.clear()
            self.box[key] = im
            return im

    def get_mask(self, d, feather):
        key = (d, feather)
        with self.lock:
            if self.mask_key != key:
                # 预览尺寸比真实光斑小一截，没必要一律 192 超采样
                self.mask = CORE.make_mask(d, d, feather,
                                           ss=max(24, min(192, d)))
                self.mask_key = key
            return self.mask

    def get_glow(self, d, strength):
        key = (d, round(strength, 2))
        with self.lock:
            if self.glow_key != key:
                self.glow = (CORE.make_glow(d, d, strength,
                                            ss=max(16, min(128, d)))
                             if strength > 0 else None)
                self.glow_key = key
            return self.glow


SCENE = SceneCache()


def preview_zoom(size, vh):
    """自动缩放比：小光斑 1:1（羽化细节看得最清楚），大光斑等比缩到放得下。

    1200px 的光斑丢进 400px 高的画布，羽化带会被压成一条线，什么都看不出来。
    """
    return min(1.0, vh * 0.84 / max(80.0, float(size) * 1.18))


# ================================================================ 写操作
def sanitize(incoming, cur):
    """把界面传来的值夹到合法区间，返回 (完整配置, 被夹取的说明列表)。

     完整配置从 cur 派生，不是从界面传来的残片拼 ——
      这样"界面只发了 size"也不会把别的键弄丢。
      这正是旧面板翻车的地方：它先改了内存里那份再拿它跟自己比，于是判定
      "没变化"，选完图根本没写盘。
    """
    cfg = dict(cur)
    notes = []
    for k in INT_KEYS:
        if k not in incoming:
            continue
        lo, hi = LIMITS[k]
        try:
            v = float(incoming[k])
        except (TypeError, ValueError):
            notes.append('%s 不是数字，已忽略' % k)
            continue
        v2 = max(lo, min(hi, v))
        if abs(v2 - v) > 1e-9:
            notes.append('%s 从 %g 夹到 %g（允许 %g~%g）' % (k, v, v2, lo, hi))
        cfg[k] = int(round(v2))
    for k in FLOAT_KEYS:
        if k not in incoming:
            continue
        lo, hi = LIMITS[k]
        try:
            v = float(incoming[k])
        except (TypeError, ValueError):
            notes.append('%s 不是数字，已忽略' % k)
            continue
        v2 = round(max(lo, min(hi, v)), 2)
        if abs(v2 - v) > 1e-9:
            notes.append('%s 从 %g 夹到 %g' % (k, v, v2))
        cfg[k] = v2
    for k in BOOL_KEYS:
        if k in incoming:
            cfg[k] = bool(incoming[k])
    for k, allowed in STR_KEYS.items():
        if k not in incoming:
            continue
        v = str(incoming[k] if incoming[k] is not None else '').strip()
        if v not in allowed:
            # 不认识的取值一律拒绝并说明 —— 静默丢弃会让"我勾了不再询问，
            # 它还是每次都问"这种事查无可查。
            notes.append('%s 只能是 %s（收到 %r，已忽略）'
                         % (k, ' / '.join(allowed), incoming[k]))
            continue
        cfg[k] = v
    for k in PATH_KEYS:
        if k not in incoming:
            continue
        p = str(incoming[k] or '')
        if not p:
            continue
        ap = p if os.path.isabs(p) else os.path.join(BASE, p)
        if not os.path.exists(ap):
            notes.append('%s 的路径不存在：%s' % (k, p))
            continue
        cfg[k] = p
    try:
        cfg['rev'] = int(cur.get('rev', 0) or 0)
    except (TypeError, ValueError):
        cfg['rev'] = 0
    return cfg, notes


def apply_config(incoming):
    # 整个"读磁盘 → 构造新配置 → 落盘"必须在同一把锁里完成。
    # 拆开锁的话，两个并发 POST 会各自读到同一份旧值、各自写回 —— 后写的那个
    # 把先写的那次改动吞掉。界面上就是"拖了滑块没反应"，而且一个字都不报错。
    with _CFG_LOCK:
        cur = cfg_read()
        cfg, notes = sanitize(incoming, cur)
        cfg_write(cfg)
    log('写配置：%s' % json.dumps({k: cfg[k] for k in sorted(cfg)},
                                 ensure_ascii=False))
    return cfg, notes


def bump_rev(reason):
    """图片重载计数 +1。

    只改这个数字、路径一个字不动，主程序也会把两张图重读一遍 ——
    这是"改完图覆盖同名文件"唯一的刷新通道（比对路径永远发现不了）。

    同样要整段持锁：它和滑块写入是完全可能同时发生的
    （一边拖滑块一边点"重新读取"），而两者都是"读-改-写"。
    """
    with _CFG_LOCK:
        cfg = cfg_read()
        try:
            rev = int(cfg.get('rev', 0) or 0)
        except (TypeError, ValueError):
            rev = 0
        cfg['rev'] = rev + 1
        cfg_write(cfg)
    log('请求重读图片（rev → %d，%s）' % (cfg['rev'], reason))
    return cfg


# ================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = 'SpotlightPanel/' + VERSION
    protocol_version = 'HTTP/1.1'          # 开 keep-alive，图片请求省一次握手

    # 默认实现会往 stderr 刷每一行请求 —— pythonw 下没有 stderr，纯浪费
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    # ------------------------------------------------------------ 回应
    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in (extra or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass          # 客户端主动取消了（拖滑块时会大量发生），不是错误

    def _json(self, obj, code=200):
        self._send(code, 'application/json; charset=utf-8',
                   json.dumps(obj, ensure_ascii=False), {'X-Panel': VERSION})

    def _err(self, msg, code=400):
        self._json({'ok': False, 'error': msg}, code)

    def _q(self, name, default=None):
        v = parse_qs(urlparse(self.path).query).get(name)
        return v[0] if v else default

    def _qf(self, name, default=0.0):
        try:
            return float(self._q(name, default))
        except (TypeError, ValueError):
            return float(default)

    def _qi(self, name, default=0):
        try:
            return int(float(self._q(name, default)))
        except (TypeError, ValueError):
            return int(default)

    def _body_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        raw = self.rfile.read(n)
        try:
            got = json.loads(raw.decode('utf-8'))
        except Exception:
            return {}
        return got if isinstance(got, dict) else {}

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ('/', '/index.html'):
                return self._page()
            if path == '/api/ping':
                return self._json({'ok': True, 'version': VERSION,
                                   'pid': os.getpid()})
            if path == '/api/state':
                return self._json(build_state())
            if path == '/api/zoom':
                return self._json({'zoom': preview_zoom(self._qf('size', 380),
                                                        self._qf('vh', 400))})
            if path == '/api/thumb':
                return self._thumb()
            if path == '/api/preview':
                return self._preview()
            if path == '/api/log':
                return self._log_tail()
            if path == '/favicon.ico':
                return self._send(200, 'image/svg+xml; charset=utf-8', FAVICON)
            return self._err('没有这个接口: %s' % path, 404)
        except Exception as e:
            log('GET %s 出错: %s' % (path, traceback.format_exc()))
            return self._err(repr(e), 500)

    def _page(self):
        try:
            with open(PAGE, 'r', encoding='utf-8') as f:
                html = f.read()
        except Exception as e:
            html = ('<pre style="font:14px/1.6 Consolas;padding:24px">'
                    '读不到 panel.html：%s\n\n%s</pre>' % (e, PAGE))
        self._send(200, 'text/html; charset=utf-8', html)

    def _thumb(self):
        side = self._q('side', 'outer')
        if side not in PATH_KEYS:
            return self._err('side 只能是 outer / inner')
        w = max(40, min(640, self._qi('w', 168)))
        full = SCENE.ensure_full(*screen_size())
        im = full[side].copy()
        im.thumbnail((w, w), Image.LANCZOS)     # 同时限宽高，保持比例
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=88)
        self._send(200, 'image/jpeg', buf.getvalue())

    def _preview(self):
        """按取景框渲染一帧预览 —— 和主程序同一套算法。

        参数全是显式的屏幕坐标，服务端不做任何"猜你想要什么"的判断：
            x0,y0,x1,y1   取景框（屏幕坐标）
            sx,sy         光斑中心（屏幕坐标）
            size,feather,glow   当前（可能还没落盘的）参数
        """
        t0 = time.perf_counter()
        sw, sh = screen_size()
        vw = max(80, min(1200, self._qi('vw', 452)))
        vh = max(60, min(900, self._qi('vh', 296)))

        x0, y0 = self._qf('x0'), self._qf('y0')
        x1, y1 = self._qf('x1'), self._qf('y1')
        if x1 <= x0 or y1 <= y0:                 # 取景框非法就退回屏幕中心
            x0, y0, x1, y1 = (sw - vw) / 2, (sh - vh) / 2, \
                             (sw + vw) / 2, (sh + vh) / 2
        x0 = max(0, min(float(sw), x0))
        y0 = max(0, min(float(sh), y0))
        x1 = max(x0 + 8, min(float(sw), x1))
        y1 = max(y0 + 8, min(float(sh), y1))

        #  取景框的宽高比必须等于输出比例，否则下面那次 resize 就是硬缩 ——
        #   画面被拉扁或拉长。这正是"预览容易变形"的根源：只要取景框顶出了屏幕，
        #   上面那两行就会把它夹短，夹完比例就不再是 vw:vh 了。
        #   （实测：前端把光斑拉到 1200 时，2560×1600 被夹成整屏，再硬缩进
        #     480×312，横向压掉 4%。）
        #   对策是宁可贵一点、裁掉一条边，也绝不拉伸。
        #   留 2‰ 的死区，是为了让比例本来就对的请求一个像素都不动 ——
        #   预览几何的逐像素回归依赖这一点，不能因为浮点误差把框挪了。
        want = vw / float(vh)
        bw, bh = x1 - x0, y1 - y0
        if bh > 0 and abs(bw / bh - want) > 0.002:
            if bw / bh > want:                  # 太宽 → 左右各裁掉一点
                nw = bh * want
                x0 += (bw - nw) / 2.0
                x1 = x0 + nw
            else:                               # 太高 → 上下各裁掉一点
                nh = bw / want
                y0 += (bh - nh) / 2.0
                y1 = y0 + nh
        box = (int(round(x0)), int(round(y0)),
               int(round(x1)), int(round(y1)))

        size = max(2.0, self._qf('size', 380))
        feather = self._qf('feather', 45)
        glow = self._qf('glow', 25) / 100.0
        sx, sy = self._qf('sx'), self._qf('sy')

        box_w = box[2] - box[0]
        z = vw / float(box_w)
        # 光斑在画布里的直径：按取景缩放比等比折算
        d = max(2, int(round(size * z)))

        bg = SCENE.view('outer', box, vw, vh)
        inner = SCENE.view('inner', box, vw, vh)
        mask = SCENE.get_mask(d, feather)
        g = SCENE.get_glow(d, glow)
        buf = compose_preview(bg, inner, mask, g,
                              (sx - box[0]) * z, (sy - box[1]) * z)

        out = io.BytesIO()
        buf.save(out, 'PNG', compress_level=1)     # 压缩等级低一点，快
        ms = (time.perf_counter() - t0) * 1000.0
        self._send(200, 'image/png', out.getvalue(),
                   {'X-Zoom': '%.6f' % z, 'X-Spot-D': d, 'X-Render-Ms': '%.1f' % ms,
                    # 把真正用的取景框回给前端。有了它，几何就不用靠"再实现一遍
                    # 后端的夹取逻辑"来验证了 —— 直接看它和 vw:vh 同不同比例即可，
                    # 也方便排查"预览怎么歪了"这类问题。
                    'X-Box': '%d,%d,%d,%d' % box})

    def _log_tail(self):
        n = max(1, min(2000, self._qi('n', 120)))
        # 日志动辄几百 KB，只读尾部 64KB 再切行 —— 整个读进来没必要
        try:
            size = os.path.getsize(LOG_PATH)
            with open(LOG_PATH, 'rb') as f:
                if size > 65536:
                    f.seek(size - 65536)
                    f.readline()               # 丢掉可能截断的半行
                raw = f.read()
            lines = raw.decode('utf-8', 'replace').splitlines()
        except Exception:
            lines = []
        self._send(200, 'text/plain; charset=utf-8',
                   '\n'.join(lines[-n:]))

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        # 跨站页面发的 POST 一定带 Origin，和我们不一致就拒掉。
        # 免得某个网页顺手把"仅本机"的这个服务当跳板去改壁纸配置。
        origin = self.headers.get('Origin')
        if origin and PANEL_ORIGIN and origin != PANEL_ORIGIN:
            return self._err('拒绝跨站请求（Origin=%s）' % origin, 403)
        try:
            if path == '/api/config':
                return self._post_config()
            if path == '/api/reload-images':
                bump_rev('按钮')
                return self._json({'ok': True, 'state': build_state()})
            if path == '/api/pick':
                return self._post_pick()
            if path == '/api/upload':
                return self._post_upload()
            if path == '/api/action':
                return self._post_action()
            if path == '/api/quit':
                return self._post_quit()
            return self._err('没有这个接口: %s' % path, 404)
        except Exception as e:
            log('POST %s 出错: %s' % (path, traceback.format_exc()))
            return self._err(repr(e), 500)

    def _post_quit(self):
        """界面上的「退出面板」（规则 A）。

        顺序很重要：先问 → 再回话 → 最后才关窗口。
        反过来（先回话再关）的话，界面在对话框还没弹出来的时候就以为"退成功了"；
        用户一旦点「取消」，看到的就是一个已经在装死的面板 —— 而且窗口过一会儿
        还会自己消失，完全说不通。

        `--serve-only` 是无窗口的测试/服务模式，没有"用户"可问，直接停。
        """
        if SERVE_ONLY['on']:
            SERVER_INTENT['stop'] = True
            return self._json({'ok': True, 'exiting': True, 'stopped': False})
        choice = ask_exit_choice('面板按钮')
        if choice is None:
            return self._json({'ok': True, 'canceled': True, 'state': build_state()})
        if choice == EXIT_STOP:
            stop_wallpaper_now('面板按钮选择一并停止')
        self._json({'ok': True, 'exiting': True, 'stopped': choice == EXIT_STOP,
                    'state': build_state()})
        threading.Thread(target=finish_exit, args=('面板按钮',), daemon=True).start()

    def _post_config(self):
        body = self._body_json()
        cfg, notes = apply_config(body)
        st = build_state()
        st['notes'] = notes
        return self._json({'ok': True, 'config': cfg, 'notes': notes,
                           'state': st})

    def _post_pick(self):
        """弹系统原生选图框。

        故意不用 HTML 的 <input type=file>：浏览器出于安全拿不到"文件的完整
        路径"，只能拿到文件内容。而这个配置里存的就是路径 ——
        （拖拽上传那条路走的是另一套：复制一份进 images/，见 _post_upload。）

        对话框以面板窗口为 owner，这样它会正确地模态在面板前面，
        而不是弹到别的程序后面去。
        """
        side = self._q('side', 'outer')
        if side not in PATH_KEYS:
            return self._err('side 只能是 outer / inner')
        cfg = cfg_read()
        title = ('选择「光斑外」的图 —— 平时整个桌面看到的那张'
                 if side == 'outer'
                 else '选择「光斑内」的图 —— 鼠标附近透出来的那张')
        initial = CORE.resolve_side(cfg, side) or ''
        path = CORE.pick_image_file(title, initial, owner=panel_hwnd())
        if not path:
            log('选图被取消（%s）' % side)
            return self._json({'ok': True, 'canceled': True})
        cfg = cfg_read()
        cfg[side] = path
        try:
            cfg['rev'] = int(cfg.get('rev', 0) or 0) + 1
        except (TypeError, ValueError):
            cfg['rev'] = 1
        cfg_write(cfg)
        log('选图 %s → %s' % (side, path))
        SCENE.token = None            # 立刻让预览缓存失效，别显示旧图
        return self._json({'ok': True, 'path': path, 'state': build_state()})

    def _post_upload(self):
        """拖进面板的图：复制一份进 images/，配置指向这份副本。

        为什么复制而不是记住原路径：浏览器只给文件内容、不给路径，
        想指向原文件只能再弹一次系统对话框。复制一份反而更省事，
        而且副本能主动压到最长边 2560px —— 直接拿 8000px 的手机原图当壁纸，
        内存和每次重采样的代价都很实在。
        """
        side = self._q('side', 'outer')
        if side not in PATH_KEYS:
            return self._err('side 只能是 outer / inner')
        name = os.path.basename(self._q('name', '') or '')
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT:
            ext = '.png'
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return self._err('没有收到文件内容')
        if n > 64 * 1024 * 1024:
            return self._err('图太大了（上限 64MB）')
        raw = self.rfile.read(n)

        # 先整张解码一次，确认真的是张图 —— 别把半点关系没有的文件写进 images/
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
        except Exception as e:
            return self._err('这不是一张能识别的图片：%r' % e)
        im = im.convert('RGB')
        sw, sh = screen_size()
        if im.width > 2560 or im.height > 2560:
            s = 2560.0 / max(im.width, im.height)
            im = im.resize((max(1, int(im.width * s)),
                            max(1, int(im.height * s))), Image.LANCZOS)
        os.makedirs(IMAGES_DIR, exist_ok=True)
        dest = os.path.join(IMAGES_DIR, 'panel-%s%s' % (side, ext))
        if ext in ('.jpg', '.jpeg'):
            im.save(dest, quality=92)
        else:
            im.save(dest)
        rel = os.path.relpath(dest, BASE).replace('\\', '/')
        cfg = cfg_read()
        cfg[side] = rel
        try:
            cfg['rev'] = int(cfg.get('rev', 0) or 0) + 1
        except (TypeError, ValueError):
            cfg['rev'] = 1
        cfg_write(cfg)
        SCENE.token = None
        log('拖入图片 %s → %s（%dx%d，%.1f MB 源文件）'
            % (side, rel, im.width, im.height, n / 1048576.0))
        return self._json({'ok': True, 'path': rel, 'state': build_state()})

    def _post_action(self):
        name = (self._body_json().get('action') or
                self._q('action') or '').strip()
        pid = read_pid()
        alive = bool(pid) and pid_alive(pid)
        if name == 'start':
            if alive:
                return self._json({'ok': True, 'note': '已经在跑了', 'state': build_state()})
            _spawn(WALLPAPER)
            log('面板：请求启动壁纸')
        elif name == 'stop':
            if not alive:
                _clear_pid()
                return self._json({'ok': True, 'note': '本来就没在跑',
                                   'state': build_state()})
            _kill(pid)
            log('面板：请求停止壁纸 pid=%d' % pid)
        elif name == 'restart':
            if alive:
                _kill(pid)
                time.sleep(1.2)
            _spawn(WALLPAPER)
            log('面板：请求重启壁纸')
        elif name == 'reset':
            cfg = dict(CORE.DEFAULT_CFG)
            cfg['outer'] = cfg_read().get('outer', cfg['outer'])
            cfg['inner'] = cfg_read().get('inner', cfg['inner'])
            for k in PANEL_ONLY_KEYS:
                # 「恢复默认」恢复的是效果参数。panel_exit_action 这类
                # 只属于面板的设置不属于"效果" —— 一起清掉的话，用户勾了
                # 「不再询问」之后点一下这个按钮，下一次退出又开始弹框，
                # 而且找不到任何提示能解释为什么。
                cur = cfg_read().get(k)
                if cur is not None:
                    cfg[k] = cur
            try:
                cfg['rev'] = int(cfg_read().get('rev', 0) or 0) + 1
            except (TypeError, ValueError):
                cfg['rev'] = 1
            cfg_write(cfg)
            log('面板：恢复默认参数（图片和面板设置不动）')
            return self._json({'ok': True, 'state': build_state()})
        elif name == 'reload':
            bump_rev('按钮')
        elif name == 'open-log':
            _reveal(LOG_PATH)
        elif name == 'open-folder':
            _reveal(BASE)
        elif name == 'ping-wallpaper':
            pass
        elif name == 'syswp-seen':
            # 规则 B 的「知道了」/「停止壁纸」：把"这行提示我看过了"记到
            # 面板自己的文件里（见 mark_syswp_seen 的注释），提示条随之消失。
            # 纯本地记录，不需要像上面那些 action 一样睡 0.35 秒等状态落地。
            mark_syswp_seen()
            return self._json({'ok': True, 'state': build_state()})
        elif name in ('to-tray', 'from-tray'):
            return self._tray_action(name == 'to-tray')
        else:
            return self._err('不认识的 action: %r' % name)
        time.sleep(0.35)          # 给启动/退出留一点落地时间，状态才准
        return self._json({'ok': True, 'state': build_state()})

    def _tray_action(self, to_tray):
        """界面上的「收进托盘」/「从托盘回来」。

        为什么不跟上面那些 action 一起走"睡 0.35 秒再报状态"——因为**藏窗口
        这件事本身不需要等**：SW_HIDE 是同步生效的，状态立刻就是准的。
        反过来，多睡这 350ms 会让界面在窗口消失前卡一下，观感很差。

        `ok=False` 不是错误，是拒绝：托盘图标没装上时绝不许藏窗口（那是
        唯一能把窗口捞回来的入口）。前端拿 `ok` 决定弹什么提示。
        """
        if TRAY is None:
            return self._json({'ok': False, 'note': '本次启动没启用托盘（--serve-only）',
                               'state': build_state()})
        if to_tray:
            ok, why = TRAY.hide_to_tray('界面按钮')
            return self._json({'ok': ok, 'note': why or '面板已收进托盘',
                               'state': build_state()})
        TRAY.restore()
        return self._json({'ok': True, 'note': '面板已从托盘回来',
                           'state': build_state()})


SERVER_INTENT = {'stop': False}

# `--serve-only`（无窗口的服务模式，测试在用）：没有"用户"可问，
# 退出请求一律直接停，不弹任何框 —— 否则自动化测试会挂在一个没人按的对话框上。
SERVE_ONLY = {'on': False}


def _spawn(script):
    """拉一个脱离本进程的壁纸进程。

    CREATE_NO_WINDOW 而不是 DETACHED_PROCESS：两者语义不同，而
    DETACHED_PROCESS 会让子进程连标准句柄都没有，配合 pythonw 反而更容易
    被某些环境连带清理。CREATE_NO_WINDOW 已经是"没有控制台窗口"了。
    """
    exe = sys.executable
    low = os.path.basename(exe).lower()
    if low.startswith('python.exe'):          # 有控制台版就地换成 pythonw
        cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(cand):
            exe = cand
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    return subprocess.Popen([exe, script], cwd=BASE,
                            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                            close_fds=True,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _kill(pid):
    subprocess.Popen(['taskkill', '/F', '/PID', str(int(pid))],
                     creationflags=0x08000000, close_fds=True,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _clear_pid():
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def _reveal(path):
    """在资源管理器里定位一个文件 / 打开一个目录"""
    try:
        if os.path.isdir(path):
            os.startfile(path)
        else:
            subprocess.Popen(['explorer', '/select,%s' % os.path.normpath(path)])
    except Exception as e:
        log('打开 %s 失败: %r' % (path, e))


# ---------------------------------------------------------------- 状态汇总
PANEL_SEEN_PATH = os.path.join(BASE, 'panel-seen.json')
# 壁纸进程写的 sidecar（wallpaper.pyw 里的 SYS_WP_PATH，同一路径的只此一份）。
#  这个名字曾经"只写不定义"——syswp_state 的 try/except 把 NameError 静默吞了，
#   表现是"提示条永远不亮"，而且查无可查。新键引用跨文件路径时先 grep 定义。
SYS_WP_PATH = os.path.join(BASE, 'system-wallpaper.json')


def syswp_state():
    """系统壁纸提示的状态（规则 B）。

    壁纸进程把"系统壁纸变了"写进 system-wallpaper.json；这里再合上"面板提示过没有"，
    得出该不该显示那行提示。

     "看过了"这个标记存在面板自己的文件里，不写回壁纸那个 sidecar：
      两个进程各写各的文件，就不存在"A 写完被 B 覆盖回去"的丢更新。
    """
    out = {'path': '', 'name': '', 'changed_at': 0.0, 'seen': True}
    try:
        with open(SYS_WP_PATH, 'r', encoding='utf-8') as f:
            side = json.load(f)
    except Exception:
        return out                    # 没这个文件 = 系统壁纸一直没变过
    if not isinstance(side, dict):
        return out
    try:
        changed_at = float(side.get('changed_at') or 0.0)
    except (TypeError, ValueError):
        changed_at = 0.0
    seen_at = 0.0
    try:
        with open(PANEL_SEEN_PATH, 'r', encoding='utf-8') as f:
            seen_at = float((json.load(f) or {}).get('syswp_changed_at') or 0.0)
    except Exception:
        pass
    out['path'] = str(side.get('path') or '')
    out['name'] = str(side.get('name') or out['path'])
    out['changed_at'] = changed_at
    out['seen'] = bool(changed_at) and seen_at >= changed_at - 1e-6
    return out


def mark_syswp_seen():
    """记下"这行提示用户已经处理过了"（点了那个按钮或「知道了」）。"""
    st = syswp_state()
    if not st['changed_at']:
        return False
    try:
        with open(PANEL_SEEN_PATH, 'w', encoding='utf-8') as f:
            json.dump({'syswp_changed_at': st['changed_at']}, f, ensure_ascii=False)
        log('系统壁纸提示已确认（%s）' % (st['name'] or st['path']))
        return True
    except Exception as e:
        log('记系统壁纸提示状态失败: %r' % e)
        return False


def build_state():
    cfg = cfg_read()
    sw, sh = screen_size()
    return {
        'ok': True,
        'version': VERSION,
        'config': cfg,
        'defaults': dict(CORE.DEFAULT_CFG),
        'limits': {k: list(v) for k, v in LIMITS.items()},
        'screen': {'w': sw, 'h': sh},
        'images': {s: image_info(cfg, s) for s in PATH_KEYS},
        'wallpaper': probe_wallpaper(),
        'system_wallpaper': syswp_state(),
        'exit_action': cfg.get('panel_exit_action', ASK),
        'tray': TRAY.info() if TRAY is not None else {
            'active': False, 'hidden': False, 'error': '本次没启用托盘',
            'nid_size': ctypes.sizeof(_NOTIFYICONDATAW)},
        'origin': PANEL_ORIGIN,
    }


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<defs><radialGradient id="g" cx="50%" cy="50%" r="50%">'
    '<stop offset="0%" stop-color="#e6c07b"/>'
    '<stop offset="60%" stop-color="#8a6a30"/>'
    '<stop offset="100%" stop-color="#12151d"/></radialGradient></defs>'
    '<rect width="32" height="32" rx="7" fill="#12151d"/>'
    '<circle cx="16" cy="16" r="11" fill="url(#g)"/></svg>'
)


# ================================================================ 宿主窗口
#
# 面板自己记一个 pid 文件。**单实例判据就用它，不要用"屏幕上有没有这个标题
# 的窗口"** —— 后者被 Windows 11 的 tab 机制骗过（见下面 find_panel_window）。
PANEL_PID_PATH = os.path.join(BASE, 'panel.pid')

# 我们拉起来的那只浏览器进程的 pid（launch_window 填）。
#
# 面板窗口是 Edge 的 `--app` 窗口，归 msedge.exe，不归我们自己 ——
# 所以"本进程 pid"这个判据找不着面板窗口。真正有用的线索是这只子进程的 pid：
# 按它去找，既能命中，又不会被"别的浏览器里恰好开着同名标签页"骗到。
LAUNCHED_PID = 0

# shell 自己建的窗口类：它们会冒用别的窗口的标题，永远不能当成我们的窗口。
_SHELL_WINDOW_CLASSES = frozenset((
    'Windows.Internal.Shell.TabProxyWindow',   # Win11 每个标签页一个，标题=标签标题
    'Progman', 'WorkerW',                      # 桌面本体
    'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',  # 任务栏
))


def read_panel_pid():
    try:
        with open(PANEL_PID_PATH, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return 0


def write_panel_pid(pid):
    try:
        with open(PANEL_PID_PATH, 'w') as f:
            f.write(str(int(pid)))
    except Exception:
        pass


def clear_panel_pid():
    try:
        os.remove(PANEL_PID_PATH)
    except Exception:
        pass


def _window_pid(hwnd):
    """某个窗口归哪个进程所有。用 ctypes 直接问，省得为了这一件事引 win32process。"""
    pid = ctypes.c_ulong(0)
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(
            int(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def _shell_owner_pid():
    """explorer.exe 的 pid —— 桌面、任务栏、tab 代理窗口都归它。"""
    try:
        return _window_pid(CORE.win32gui.GetShellWindow())
    except Exception:
        return 0


def find_panel_window(pid=None):
    """找我们自己的那个面板窗口。找不到返回 0。

     不能用 `FindWindow(None, WINDOW_TITLE)`。 Windows 11 会给每个浏览器
      标签页建一个 `Windows.Internal.Shell.TabProxyWindow`，**归 explorer.exe
      所有**，标题就是那个标签页的标题。于是 FindWindow 有相当大概率拿到这个
      幽灵窗口，后果是致命的：
        · 它既抬不到前面（SetForegroundWindow 无效）；
        · 又让 focus_existing_panel() 误判"面板已经开着" → 新面板**一声不响地
          不启动**：退出码 0、日志只有一行"面板已经在运行"、屏幕上什么都没出现。
      这正是"点了没反应"最典型的死法。所以这里改成自己挨个枚举，并排除：
        ① 排除 shell 自己建的窗口类（TabProxyWindow / 桌面 / 任务栏）；
        ② 排除 explorer.exe 名下的窗口；
        ③ 给了 pid 就只认这个 pid 的窗口。
      顺带把"可见且没最小化"的挑出来优先返回 —— 抬一个最小化的窗口才是本意。
    """
    gui = CORE.win32gui
    hits = []
    shell_pid = _shell_owner_pid()

    def cb(h, _):
        try:
            if gui.GetWindowText(h) != WINDOW_TITLE:
                return True
            if gui.GetClassName(h) in _SHELL_WINDOW_CLASSES:
                return True
            owner = _window_pid(h)
            if shell_pid and owner == shell_pid:
                return True
            if pid and owner != pid:
                return True
            hits.append(h)
        except Exception:
            pass
        return True

    try:
        gui.EnumWindows(cb, None)
    except Exception:
        return 0
    if not hits:
        return 0
    for h in hits:
        try:
            if gui.IsWindowVisible(h) and not gui.IsIconic(h):
                return int(h)
        except Exception:
            pass
    return int(hits[0])


def current_panel_window():
    """面板窗口句柄。找不到返回 0。

    先按"我们拉起的那只浏览器"的 pid 找，找不到再退回按标题找。
    两步都要，缺一不可：
      · 只按 pid 找 —— 单实例那种"由新进程去抬旧面板"的场景没有 pid 可用；
      · 只按标题找 —— 别的浏览器里碰巧开着一个同名标签页就会认错窗口，
        对托盘来说认错的后果是把别人的窗口藏进托盘里。
    """
    return find_panel_window(LAUNCHED_PID) or find_panel_window()


def panel_hwnd():
    """把系统选图框挂到面板窗口上（让它模态、居中）。

     这里一度写的是 `find_panel_window(os.getpid())` —— 永远返回 0。面板窗口
      是 Edge 的 `--app` 窗口，归 msedge.exe 所有，本进程 pid 一个窗口都不拥有。
      后果不至于崩，只是选图框不再挂在面板上：不模态、不在面板居中，
      还能被主窗口盖住。这类"功能还在、但少了点东西"的坏法最难自己发现。
    """
    return current_panel_window() or None


def _force_foreground(hwnd):
    """把窗口抬到最前面，并且真的拿到焦点。

    `SetForegroundWindow` 有个众所周知的脾气：只有当调用它的进程"够资格"抢
    前台时才生效，否则它只是让任务栏图标闪一下，窗口还在后面。从托盘图标点
    回来的时候正好撞上这条 —— 那会儿前台是 explorer，不是我们。
    绕法是临时把两个线程的输入队列接在一起，再抢一次。
    """
    u = ctypes.windll.user32
    try:
        if GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass
    try:
        fg = GetForegroundWindow() or 0
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        pid = ctypes.c_ulong(0)
        other = GetWindowThreadProcessId(fg, ctypes.byref(pid)) if fg else 0
        attached = False
        if other and other != me:
            attached = bool(AttachThreadInput(me, other, True))
        try:
            BringWindowToTop(hwnd)
            SetForegroundWindow(hwnd)
        finally:
            if attached:
                AttachThreadInput(me, other, False)
    except Exception:
        pass
    try:
        return GetForegroundWindow() == hwnd
    except Exception:
        return False


def show_panel_window():
    """把面板窗口显示出来并拿到前台。托盘 / 单实例 / 外部脚本都走这一条路。

    顺序有讲究：被收进托盘的窗口是 SW_HIDE 掉的，而且内部多半还带着"最小化"
    状态，所以要先 SW_RESTORE 清掉它，再 SW_SHOW 保证可见。
    已经在正常显示（含最大化）的窗口不调 SW_RESTORE —— 那会把用户手动
    最大化的窗口打回原尺寸，属于没事找事。

    返回窗口句柄，找不到返回 0。
    """
    h = current_panel_window()
    if not h:
        return 0
    u = ctypes.windll.user32
    try:
        if u.IsIconic(h):
            u.ShowWindow(h, win32con.SW_RESTORE)
        u.ShowWindow(h, win32con.SW_SHOW)
    except Exception as e:
        log('显示面板窗口失败: %r' % e)
        return 0
    _force_foreground(h)
    return int(h)


def _find_exe(*rel):
    """在几个标准安装根目录下找一个可执行文件。

     除了环境变量，还写死了两个标准路径当兜底：某些宿主环境（比如本进程被
      别的程序以精简过的环境变量拉起来时）拿不到 ProgramFiles 这类变量，
      光靠 os.environ 就会"找不着浏览器"—— 而它其实就装在老地方。
      Playwright 就踩过这个坑：它靠 ProgramFiles 推路径，推出来是
      "undefined\\Program Files\\Microsoft\\Edge\\..."。
    """
    roots = [r'C:\Program Files (x86)', r'C:\Program Files']
    for var in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        v = os.environ.get(var)
        if v:
            roots.append(v)
    for root in dict.fromkeys(roots):        # 去重但保持顺序
        p = os.path.join(root, *rel)
        if os.path.exists(p):
            return p
    return None


def edge_exe():
    return _find_exe('Microsoft', 'Edge', 'Application', 'msedge.exe')


def chrome_exe():
    return _find_exe('Google', 'Chrome', 'Application', 'chrome.exe')


def launch_window(url):
    """把界面开成一个无边框的"应用窗口"。

    用 --app：没有标签栏地址栏，看着就是个独立工具，而不是混在浏览器
    标签里的一页。

     profile 用一个每次全新的临时目录，不复用常驻 profile。
      理由是可靠性：Chromium 的 profile 是单实例的，如果用户已经开着一个
      用同一个 profile 的 Edge，新进程会立刻把 URL 转交给老进程然后自己退出
      —— 于是"等 Edge 退出就关服务"这个逻辑当场失效，用户会看到一个活着
      但连不上服务的空壳窗口。全新 profile 保证这次启动一定是我们自己的
      进程，wait() 才可信。
    """
    for exe in (edge_exe(), chrome_exe()):
        if not exe:
            continue
        profile = tempfile.mkdtemp(prefix='spotlight-panel-')
        args = [
            exe,
            '--app=' + url,
            '--user-data-dir=' + profile,
            '--window-size=%d,%d' % (WINDOW_W, WINDOW_H),
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-features=Translate,MediaRouter',
            '--disable-background-networking',
            '--disable-sync',
            '--hide-crash-restore-bubble',
            '--new-window',
        ]
        try:
            proc = subprocess.Popen(args, close_fds=True)
        except Exception as e:
            log('启动 %s 失败: %r' % (os.path.basename(exe), e))
            shutil.rmtree(profile, ignore_errors=True)
            continue
        log('界面窗口已启动（%s，pid=%d）' % (os.path.basename(exe), proc.pid))
        global LAUNCHED_PID
        LAUNCHED_PID = proc.pid     # 找面板窗口时最有用的那条线索，见 current_panel_window
        return proc, profile
    # 连 Edge / Chrome 都没有 —— 退回默认浏览器开一个普通标签页。
    # 功能一样，只是不那么像"一个工具"。
    log('没找到 Edge / Chrome，改用默认浏览器打开')
    try:
        os.startfile(url)
    except Exception as e:
        log('打开默认浏览器也失败: %r' % e)
    return None, None


def close_panel_window():
    """请面板窗口自己关掉 —— 等价于用户点标题栏那个 ×。返回窗口句柄，没有则 0。

     必须发 WM_SYSCOMMAND / SC_CLOSE，不能发 WM_CLOSE。
      Chromium 的窗口过程不理会别的进程直接投过来的 WM_CLOSE：实测
      PostMessage(WM_CLOSE) 之后窗口纹丝不动、进程还活着、日志里连一行都没有
      —— 于是托盘菜单里的「退出面板」看起来点了没反应。
      点 × 的时候系统发的本来就是 SC_CLOSE，照抄它才对路。

    这一条是 `_probe_tray.py` 抓出来的，不是推理出来的 —— 也正是为什么托盘
    这块必须端到端测：静态读代码它"明明写了 WM_CLOSE 啊"。
    """
    h = current_panel_window()
    if not h:
        return 0
    try:
        PostMessageW(h, win32con.WM_SYSCOMMAND, win32con.SC_CLOSE, 0)
    except Exception as e:
        log('请求关闭面板窗口失败: %r' % e)
        return 0
    return int(h)


def focus_existing_panel():
    """已经开着一个面板就把它抬到前面，而不是再开一个。

    两个面板同时改同一个配置文件，会出现"我改了怎么没反应"（其实是另一个
    面板把值写回去了），这类问题装死都难查，索性从源头堵掉。

     判据是"记下来的面板进程还活着"，不是"屏幕上有这个标题的窗口"。
      面板进程的生命周期恰好就是面板窗口的生命周期：main() 里 `proc.wait()`
      一等 Edge 退出，服务就跟着退。所以 pid 活着 == 面板在开着，这个等价关系
      比找窗口可靠得多 —— 找窗口会被 TabProxyWindow 骗（见 find_panel_window）。
    """
    pid = read_panel_pid()
    if not pid or not pid_alive(pid):
        if pid:
            clear_panel_pid()      # 过期的记录必须清掉，否则会一直误判"已开着"
        return False

    if not show_panel_window():
        # 进程活着但窗口枚举不到（极少见：Edge 刚起还没建窗口）。宁可多开一个，
        # 也不要让用户点了没反应 —— 配置写入是"基于磁盘当前值构造"的，
        # 两个面板并存也不会互相覆盖没提到的字段。
        log('面板进程 %d 还在，但没枚举到窗口 → 照常开新的' % pid)
        return False

    # 窗口是我们显示出来的，托盘那边的"藏着"状态要同步过来，
    # 否则下一次点托盘图标会以为它还藏着，去"显示"一个已经显示着的窗口。
    if TRAY is not None:
        TRAY.note_shown()
    return True


# ================================================================ 托盘
#
# 「最小化到托盘」为什么得由我们从外面模拟
# ------------------------------------------------------------------
# 面板窗口是 Edge 的 `--app` 窗口 —— 窗口过程在 Edge 手里，我们改不了它的
# 最小化行为。所以只能在外面看着它：
#
#     轮询 IsIconic → 一旦变成最小化，就 SW_HIDE 藏掉（任务栏按钮随之消失），
#     再靠托盘图标把它捞回来。
#
# 两条必须守住的安全线
# ------------------------------------------------------------------
# 1. 托盘图标没装上，就绝不许藏窗口。 图标是唯一能把窗口捞回来的入口；
#    入口不存在而窗口藏起来 = 用户永远找不回面板，只能去任务管理器杀进程。
#    所以 hide_to_tray() 第一件事就是看 active。（这个项目在"窗口盖住全屏
#    又没出口"上已经栽过一次，不再栽第二次。）
# 2. 托盘线程和 HTTP 线程都会关心"窗口藏没藏"。状态只由托盘一侧改，
#    HTTP 侧一律走 TRAY 的公开方法，不自己动窗口。
#
# 为什么这块全用 ctypes，不用 win32gui
# ------------------------------------------------------------------
# 托盘要一个隐藏窗口来收消息。pywin32 的 WNDCLASS 派发规则（消息不在 dict
# 里的时候到底调不调 DefWindowProc）没有明确文档，赌错的表现是"窗口建不出来"
# 或者"菜单点了不响应"，都不好查。ctypes 这条路每一步都是我写的，出问题能
# 一眼看出来。
# ================================================================ 退出询问（规则 A）
# 点「退出面板」时问一次「壁纸要不要一起停」，可以勾「不再询问」把选择记进配置。
#
# 为什么三个入口能收敛到同一个函数：因为标题栏那个 × 根本拦不住。
# 面板窗口是 Edge 的 --app 窗口，窗口过程在 Edge 手里 —— 我们收不到 WM_CLOSE，
# 也没机会在它关掉之前插话。所以这里的思路反过来：等窗口真没了再问。
# 原生对话框是独立于面板窗口的顶层窗口，窗口没了照样弹得出来。
#
#   面板按钮 → 先问 → 关窗口 → main() 发现"已经问过了"，不再问
#   托盘菜单 → 先问 → 关窗口 → 同上
#   标题栏 × → 窗口已经没了 → 由 main() 在那个分支里问（选「取消」就把窗口重新拉起）
#
# 一句话：问的时机可以不同，但问题本身和结果处理只有一份实现。

EXIT_KEEP = 'keep_wallpaper'
EXIT_STOP = 'stop_wallpaper'
ASK = 'ask'
EXIT_ACTIONS = (ASK, EXIT_KEEP, EXIT_STOP)

# 只问一次的记忆：按钮问完 → 紧接着"窗口关闭"触发的下一步直接沿用，不再弹第二个框。
EXIT_STATE = {'asked': False, 'choice': None, 'lock': threading.Lock()}


def stop_wallpaper_now(why):
    """真正把壁纸停掉：杀进程 + 清 pid 文件。返回有没有真的停到东西。"""
    pid = read_pid()
    if pid and pid_alive(pid):
        _kill(pid)
        _clear_pid()
        log('停止壁纸 pid=%d（%s）' % (pid, why))
        return True
    _clear_pid()
    log('想停壁纸，但它本来就没在跑（%s）' % why)
    return False


# ---- 三选一对话框 ----
# 为什么用 Tk，而不是 MessageBoxW / TaskDialogIndirect（前两个都在本机实测过）：
#   MessageBoxW —— 按钮文案固定是「是 / 否 / 取消」，和"只退面板 / 连壁纸一起停 /
#     取消"对不上。用户会按错，而按错的代价是壁纸被意外停掉（或以为停了其实没停）。
#   TaskDialogIndirect —— 文案和复选框都能自定义，本是理想方案；但本机实测两条路都不通：
#     · 不带 comctl32 v6 激活上下文 → 一律 E_INVALIDARG（0x80070057），连最小配置都一样；
#     · 用 CreateActCtxW 挂上 v6 清单后 → 对话框创建即段错误（python / pythonw、
#       沙箱内外都一样，无回调也一样）。段错误是进程级硬死，连"降级"的机会都没有。
#   Tk —— stdlib，壁纸进程（wallpaper.pyw）的整套界面就是 Tk，在这台机器上长期
#     稳定运行；按钮文案、复选框、置顶、居中全在自己手里。就它了。
ID_TD_KEEP, ID_TD_STOP = 100, 101     # 保留这两个"按钮 id"：_probe_exit.py 靠它们
IDCANCEL = 2                          #   指名要按下哪个按钮（auto_click 参数）

_EXIT_TITLE = '退出控制面板'
_EXIT_BODY = ('壁纸要一起停吗？\n'
              '只退面板 —— 壁纸继续在桌面上跑，热键还能把面板叫回来。\n'
              '连壁纸一起停 —— 桌面立刻恢复成你的系统壁纸。')


def ask_exit_native(owner=0, auto_click=None):
    """Tk 三选一。返回 (选择, 勾了不再询问)；返回 None = tkinter 用不了，去降级。

    auto_click 只给测试用（`_probe_exit.py`）：传按钮 id（或 (按钮id, 勾选) 元组）
    就让对话框在出现后自动"按下"那个按钮 —— 走的是按钮自己的 command 回调，
    和真人点下去是同一条代码路径，映射写没写对一测便知。
    """
    try:
        import tkinter as tk
    except Exception as e:
        log('tkinter 用不了（%r），改用 MessageBox 问' % e)
        return None

    result = {'choice': None, 'checked': False}
    root = tk.Tk()
    try:
        root.title(WINDOW_TITLE + ' · ' + _EXIT_TITLE)
        root.resizable(False, False)
        root.attributes('-topmost', True)
        root.withdraw()                   # 先藏起来摆好位置再露脸，避免闪角落

        body = tk.Frame(root, padx=20, pady=16)
        body.pack()
        tk.Label(body, text=_EXIT_TITLE,
                 font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        tk.Label(body, text=_EXIT_BODY, justify='left').pack(anchor='w', pady=(8, 2))
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text='不再询问，记住这次的选择',
                       variable=var).pack(anchor='w', pady=(6, 4))

        row = tk.Frame(body)
        row.pack(fill='x', pady=(8, 0))

        def finish(choice):
            result['choice'] = choice
            result['checked'] = bool(var.get())
            root.destroy()

        b_keep = tk.Button(row, text='只退面板\n壁纸继续跑', width=14, height=2,
                           command=lambda: finish(EXIT_KEEP))
        b_stop = tk.Button(row, text='连壁纸一起停\n桌面恢复系统壁纸', width=20, height=2,
                           command=lambda: finish(EXIT_STOP))
        b_cancel = tk.Button(row, text='取消', width=8, height=2,
                             command=lambda: finish(None))
        b_keep.pack(side='left', padx=(0, 8))
        b_stop.pack(side='left', padx=(0, 8))
        b_cancel.pack(side='left')
        b_keep.focus_set()                # 默认焦点落在破坏性最小的那个上
        root.bind('<Return>', lambda e: finish(EXIT_KEEP))
        root.bind('<Escape>', lambda e: finish(None))
        root.protocol('WM_DELETE_WINDOW', lambda: finish(None))

        if auto_click:
            bid, mark = (auto_click if isinstance(auto_click, tuple)
                         else (auto_click, False))
            if mark:
                var.set(True)
            target = {ID_TD_KEEP: b_keep, ID_TD_STOP: b_stop,
                      IDCANCEL: b_cancel}.get(bid)
            if target is not None:
                # invoke() = 按钮自己的 command 回调，和真点击同一条路径
                root.after(250, target.invoke)

        # 露脸：屏幕居中（owner 是别的进程的窗口，跨进程坐标换算不值当）
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry('+%d+%d' % (max(0, (sw - w) // 2), max(0, (sh - h) // 2)))
        root.deiconify()
        root.lift()
        try:
            root.grab_set()               # 模态：这一次只回答这一个问题
        except Exception:
            pass
        root.focus_force()                # 从 HTTP 线程弹出来也必须抢到前台
        root.mainloop()
    except Exception as e:
        log('Tk 对话框出错（改用 MessageBox 问）: %r' % e)
        try:
            root.destroy()
        except Exception:
            pass
        return None
    return result['choice'], result['checked']


def ask_exit_fallback(owner=0):
    """降级方案：原生 MessageBoxW。

     它给不了自定义文案，按钮只能写「是 / 否 / 取消」，所以**正文里必须把
      对应关系写死**；默认按钮放「否」= 只退面板，让顺手按回车的人不会把壁纸停掉。
      代价是这条路上没有「不再询问」（原生 MessageBox 挂不了复选框）——
      只在 tkinter 都 import 不了这种极端情况下才会走到这里。
    """
    MB_YESNOCANCEL = 0x00000003
    MB_ICONQUESTION = 0x00000020
    MB_DEFBUTTON2 = 0x00000100
    MB_SETFOREGROUND = 0x00010000
    IDYES, IDNO = 6, 7
    text = ('壁纸要不要一起停？\n\n'
            '是　——　连壁纸一起停，桌面恢复系统壁纸\n'
            '否　——　只退面板，壁纸继续跑（默认）\n'
            '取消 ——　不退出')
    try:
        r = ctypes.windll.user32.MessageBoxW(
            owner or None, text, WINDOW_TITLE + ' · 退出面板',
            MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND)
    except Exception as e:
        log('MessageBox 也失败了: %r' % e)
        return EXIT_KEEP, False               # 连问都问不出来 → 按最安全的理解办
    if r == IDYES:
        return EXIT_STOP, False
    if r == IDNO:
        return EXIT_KEEP, False
    return None, False


def remember_exit_action(action):
    """把「不再询问」的选择写进配置。

    新键 panel_exit_action 已在 STR_KEYS 登记 —— 没登记的话这里写得再对，
    也会在 sanitize() 构造新配置时被静默丢掉。
    """
    try:
        with _CFG_LOCK:
            new = dict(cfg_read())
            new['panel_exit_action'] = action
            cfg_write(new)
        log('记住退出选择：%s（以后不再询问）' % action)
    except Exception as e:
        log('记住退出选择失败: %r' % e)


def ask_exit_choice(source='未知'):
    """问一次「退出面板时壁纸怎么办」。

    返回 EXIT_KEEP / EXIT_STOP / None(取消)。只问一次 —— 三个入口共用
    EXIT_STATE：面板按钮问过之后，紧接着窗口关闭触发的那一步就不再弹第二个框。
    """
    with EXIT_STATE['lock']:
        if EXIT_STATE['asked']:
            return EXIT_STATE['choice']
        EXIT_STATE['asked'] = True
        choice, checked = None, False
        try:
            act = cfg_read().get('panel_exit_action', ASK)
            if act not in EXIT_ACTIONS:
                act = ASK
            if act == ASK:
                owner = current_panel_window()
                if owner and not ctypes.windll.user32.IsWindowVisible(owner):
                    # 收在托盘里的窗口不能当 owner：对话框有可能跟着它一起藏起来，
                    # 那就成了"点了托盘菜单却什么都没弹出来"。
                    owner = 0
                got = ask_exit_native(owner)
                if got is None:
                    got = ask_exit_fallback(owner)
                choice, checked = got
                log('退出（%s）：用户选了 %s' % (source, choice or '取消'))
            else:
                choice = act
                log('退出（%s）：按记住的选择执行 —— %s'
                    % (source, '只退面板' if choice == EXIT_KEEP else '连壁纸一起停'))
            if checked and choice:
                remember_exit_action(choice)
        except Exception as e:
            # 问不出来绝不能变成"点了没反应"：按破坏性最小的理解继续（只退面板）。
            log('退出询问出错（按"只退面板"继续）: %r' % e)
            choice = EXIT_KEEP
        if choice is None:
            EXIT_STATE['asked'] = False       # 取消 = 什么都没发生，下次还得问
        EXIT_STATE['choice'] = choice
        return choice


def reset_exit_state():
    """回到"下次还得问"的状态（用户取消之后）。"""
    with EXIT_STATE['lock']:
        EXIT_STATE['asked'] = False
        EXIT_STATE['choice'] = None


def finish_exit(source='未知'):
    """已经问完了 —— 关掉面板窗口，让 main() 的等待循环收摊。

     关窗口必须发 SC_CLOSE（理由见 close_panel_window），而且要重试：
      万一窗口没关掉，而 SERVER_INTENT 又已经置位，就会留下"面板进程退了、
      窗口还开着且永远连不上服务"的半死不活状态。
    """
    time.sleep(0.25)                 # 先把 HTTP 应答送回浏览器，再关窗
    SERVER_INTENT['stop'] = True
    for i in range(3):
        if close_panel_window():
            if i:
                log('退出（%s）：第 %d 次请求关窗口才成功' % (source, i + 1))
            return True
        time.sleep(0.5)
    # 3 次都关不掉 = 那个窗口的窗口过程出了状况。面板是**我们自己拉起来的
    # 临时 --app 进程**（独立 profile），杀掉它碰不到用户的主浏览器；不兜这一下，
    # 用户就只剩一个永远连不上服务的空壳窗口。
    if LAUNCHED_PID and pid_alive(LAUNCHED_PID):
        log('退出（%s）：改用 taskkill 收掉残留的面板窗口进程 %d'
            % (source, LAUNCHED_PID))
        _kill(LAUNCHED_PID)
        return True
    log('退出（%s）：关不掉面板窗口，也没找到残留进程' % source)
    return False


TRAY_CLASS = 'SpotlightPanelTray'
WM_TRAY = win32con.WM_APP + 1        # 托盘回调消息（自定义）
TRAY_ID = 1
IDM_SHOW, IDM_RESTART, IDM_QUIT = 1001, 1002, 1003
TRAY_POLL_MS = 300                   # 看护"窗口被最小化"的轮询间隔
NIIF_INFO = 0x00000001

TIP_NORMAL = '聚光壁纸 · 控制面板 —— 单击收起 / 显示，右键更多'
TIP_HIDDEN = '聚光壁纸 · 控制面板（已收进托盘）—— 单击打开'
TRAY_ICON = os.path.join(BASE, 'panel.ico')


class _NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIconW 的那个大结构体（V4，x64 上正好 976 字节）。

    字段和顺序一个字都不能动：Windows 靠 cbSize 判断你填到哪一版，填错的
    后果是"图标出来了但气泡不显示"这种半死不活的状态。下面 _NID_SIZE 会
    把它印进日志，改坏了一眼能看见。
    """
    _fields_ = [
        ('cbSize', ctypes.c_uint),
        ('hWnd', ctypes.c_void_p),
        ('uID', ctypes.c_uint),
        ('uFlags', ctypes.c_uint),
        ('uCallbackMessage', ctypes.c_uint),
        ('hIcon', ctypes.c_void_p),
        ('szTip', ctypes.c_wchar * 128),
        ('dwState', ctypes.c_uint),
        ('dwStateMask', ctypes.c_uint),
        ('szInfo', ctypes.c_wchar * 256),
        ('uVersion', ctypes.c_uint),          # 和 uTimeout 共用这一个字段
        ('szInfoTitle', ctypes.c_wchar * 64),
        ('dwInfoFlags', ctypes.c_uint),
        ('guidItem', ctypes.c_byte * 16),
        ('hBalloonIcon', ctypes.c_void_p),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_size_t, ctypes.c_ssize_t)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.c_uint),
        ('style', ctypes.c_uint),
        ('lpfnWndProc', _WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', ctypes.c_void_p),
        ('hIcon', ctypes.c_void_p),
        ('hCursor', ctypes.c_void_p),
        ('hbrBackground', ctypes.c_void_p),
        ('lpszMenuName', ctypes.c_wchar_p),
        ('lpszClassName', ctypes.c_wchar_p),
        ('hIconSm', ctypes.c_void_p),
    ]


_H = ctypes.c_void_p
_U = ctypes.c_uint
_I = ctypes.c_int
_SZ = ctypes.c_size_t
_SS = ctypes.c_ssize_t
_RET = ctypes.c_ssize_t                     # LRESULT
_PMSG = ctypes.POINTER(wintypes.MSG)


def _fn(dll, name, res, *args):
    """取一个 Win32 函数，并把原型钉死。

     这不是洁癖。不声明 argtypes 时 ctypes 把 Python 整数按 32 位 传参，
      句柄一旦落到 0x80000000 以上就被截断成垃圾 —— 而 Windows 的句柄池恰好
      会用到那个区间。症状是"十次九次正常，偶尔句柄变成不存在的东西"，
      这种 bug 查起来能把人耗死。所有带句柄参数的调用都走这里。
    """
    f = getattr(getattr(ctypes.windll, dll), name)
    f.restype = res
    f.argtypes = list(args)
    return f


DefWindowProcW = _fn('user32', 'DefWindowProcW', _RET, _H, _U, _SZ, _SS)
RegisterClassExW = _fn('user32', 'RegisterClassExW', ctypes.c_ushort,
                       ctypes.POINTER(_WNDCLASSEXW))
CreateWindowExW = _fn('user32', 'CreateWindowExW', _H, _U, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, _U, _I, _I, _I, _I, _H, _H, _H, _H)
DestroyWindow = _fn('user32', 'DestroyWindow', _I, _H)
ShowWindow = _fn('user32', 'ShowWindow', _I, _H, _I)
IsIconic = _fn('user32', 'IsIconic', _I, _H)
IsWindow = _fn('user32', 'IsWindow', _I, _H)
IsWindowVisible = _fn('user32', 'IsWindowVisible', _I, _H)
SetForegroundWindow = _fn('user32', 'SetForegroundWindow', _I, _H)
BringWindowToTop = _fn('user32', 'BringWindowToTop', _I, _H)
GetForegroundWindow = _fn('user32', 'GetForegroundWindow', _H)
GetCursorPos = _fn('user32', 'GetCursorPos', _I, ctypes.POINTER(wintypes.POINT))
CreatePopupMenu = _fn('user32', 'CreatePopupMenu', _H)
DestroyMenu = _fn('user32', 'DestroyMenu', _I, _H)
AppendMenuW = _fn('user32', 'AppendMenuW', _I, _H, _U, _SZ, ctypes.c_wchar_p)
TrackPopupMenu = _fn('user32', 'TrackPopupMenu', _U, _H, _U, _I, _I, _I, _H, _H)
SetMenuDefaultItem = _fn('user32', 'SetMenuDefaultItem', _I, _H, _U, _U)
PostMessageW = _fn('user32', 'PostMessageW', _I, _H, _U, _SZ, _SS)
GetMessageW = _fn('user32', 'GetMessageW', _I, _PMSG, _H, _U, _U)
TranslateMessage = _fn('user32', 'TranslateMessage', _I, _PMSG)
DispatchMessageW = _fn('user32', 'DispatchMessageW', _RET, _PMSG)
GetWindowThreadProcessId = _fn('user32', 'GetWindowThreadProcessId', _U, _H,
                               ctypes.POINTER(ctypes.c_ulong))
AttachThreadInput = _fn('user32', 'AttachThreadInput', _I, _U, _U, _I)
LoadImageW = _fn('user32', 'LoadImageW', _H, _H, ctypes.c_wchar_p, _U, _I, _I, _U)
Shell_NotifyIconW = _fn('shell32', 'Shell_NotifyIconW', _I, _U,
                        ctypes.POINTER(_NOTIFYICONDATAW))


def _draw_tray_icon(path):
    """画一个托盘图标并存成多尺寸 .ico。

    暗底 + 一团金色光，和面板 favicon 同一套配色 —— 缩到 16px 还认得出是个
    发光点。256 画好之后交给 Pillow 存成多种尺寸，系统按通知区域的实际像素
    尺寸自己挑（本机 150% 缩放，托盘要的是 24px）。
    """
    base = 256
    im = Image.new('RGBA', (base, base), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, base - 1, base - 1], radius=int(base * 0.22),
                        fill=(18, 21, 29, 255))
    cx = (base - 1) / 2.0
    rad = base * 0.33
    steps = 28
    for i in range(steps, 0, -1):        # 由外向内叠，越靠圆心越亮
        t = i / float(steps)
        r = rad * t
        k = 1.0 - t
        d.ellipse([cx - r, cx - r, cx + r, cx + r],
                  fill=(int(122 + 124 * k), int(96 + 126 * k),
                        int(34 + 118 * k), 255))
    d.ellipse([cx - rad, cx - rad, cx + rad, cx + rad],
              outline=(92, 70, 30, 255), width=max(1, base // 96))
    im.save(path, format='ICO',
            sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48),
                   (256, 256)])


class Tray(object):
    """面板的托盘图标：收起 / 显示、右键菜单、最小化看护。

    线程模型：一个线程建隐藏窗口并跑消息循环，另一个线程轮询窗口的最小化
    状态。对 self.hidden 的写只发生在托盘这一侧，用 _lock 串起来。
    """

    def __init__(self):
        self.hwnd = 0
        self.hidden = False
        self.active = False
        self.err = ''
        self._nid = None
        self._hicon = 0
        self._wndproc = None      #  必须常驻：WNDCLASSEXW 里存的是它的地址
        self._cls = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._win_cache = 0
        self._last_click = 0.0

    # ------------------------------------------------------------ 对外接口
    def start(self):
        threading.Thread(target=self._run, name='tray', daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._nid is not None:
            try:
                Shell_NotifyIconW(win32gui.NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
        self.active = False
        if self.hwnd:
            try:
                PostMessageW(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        log('托盘已摘掉')

    def info(self):
        return {'active': self.active, 'hidden': self.hidden,
                'hwnd': self.hwnd, 'class': TRAY_CLASS,
                'icon': bool(self._hicon), 'nid_size': ctypes.sizeof(
                    _NOTIFYICONDATAW), 'error': self.err}

    def note_shown(self):
        """窗口被别的途径显示出来了（单实例把它抬到前面、外部脚本…）。"""
        with self._lock:
            if self.hidden:
                self.hidden = False
                self._tip(TIP_NORMAL)

    def hide_to_tray(self, why='手动'):
        """把面板窗口收进托盘。返回 (成功?, 说明)。"""
        if not self.active:
            return False, '托盘没启用，收起来就找不回来了'
        h = self._win()
        if not h:
            return False, '找不到面板窗口'
        try:
            ShowWindow(h, win32con.SW_HIDE)
        except Exception as e:
            return False, '收起失败：%r' % (e,)
        with self._lock:
            self.hidden = True
        self._tip(TIP_HIDDEN)
        log('面板已收进托盘（%s）' % why)
        return True, ''

    def restore(self):
        h = show_panel_window()
        with self._lock:
            self.hidden = False
        self._tip(TIP_NORMAL)
        if not h:
            log('托盘：想显示面板，但没找到窗口')
        return bool(h)

    def toggle(self):
        with self._lock:
            hidden = self.hidden
        if hidden:
            self.restore()
        else:
            self.hide_to_tray('托盘单击')

    # ------------------------------------------------------------ 内部：图标
    def _tip(self, text):
        if self._nid is None:
            return
        self._nid.szTip = text[:127]
        self._nid.uFlags = (win32gui.NIF_MESSAGE | win32gui.NIF_ICON |
                            win32gui.NIF_TIP)
        Shell_NotifyIconW(win32gui.NIM_MODIFY, ctypes.byref(self._nid))

    def _balloon(self, title, text):
        if self._nid is None:
            return
        nid = self._nid
        nid.uFlags = win32gui.NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_INFO
        if not Shell_NotifyIconW(win32gui.NIM_MODIFY, ctypes.byref(nid)):
            log('托盘气泡没弹出来（不影响功能）')
        self._tip(TIP_HIDDEN)      # 复位标志，否则图标会一直挂着"有气泡"状态

    def _load_icon(self):
        try:
            _draw_tray_icon(TRAY_ICON)
        except Exception as e:
            log('画托盘图标失败（改用系统图标）: %r' % e)
        try:
            sz = win32api.GetSystemMetrics(win32con.SM_CXSMICON) or 16
            h = LoadImageW(None, TRAY_ICON, win32con.IMAGE_ICON, sz, sz,
                           win32con.LR_LOADFROMFILE)
            if h:
                return h
        except Exception as e:
            log('载入 panel.ico 失败（改用系统图标）: %r' % e)
        try:
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        except Exception:
            return 0

    # ------------------------------------------------------------ 内部：窗口
    def _win(self):
        """面板窗口句柄。缓存住，省得每 300ms 全量枚举一遍顶层窗口。

         缓存必须带校验：窗口句柄会被系统回收再利用，Edge 某次刷新
          把窗口重建掉之后，旧句柄可能已经落到别的窗口身上了 —— 拿着它去
          SW_HIDE，藏掉的就是别人家的窗口。所以每次都用"窗口还在 && 标题还
          对"验一遍，验不过就重新找。
        """
        h = self._win_cache
        if h:
            try:
                if IsWindow(h) and win32gui.GetWindowText(h) == WINDOW_TITLE:
                    return h
            except Exception:
                pass
        h = current_panel_window()
        self._win_cache = h
        return h

    def _install(self):
        try:
            self._wndproc = _WNDPROC(self._on_msg)
            wcx = _WNDCLASSEXW()
            wcx.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wcx.style = 0
            wcx.lpfnWndProc = self._wndproc
            wcx.hInstance = win32api.GetModuleHandle(None)
            wcx.lpszClassName = TRAY_CLASS
            self._cls = wcx                      # 常驻，别让 GC 把它收走
            atom = RegisterClassExW(ctypes.byref(wcx))
            if not atom:
                # 同一个进程里第二次注册同名类会走到这里（自测脚本会遇到）。
                # 类已经在了，按名字建窗口照样能成。
                log('托盘窗口类注册失败，按类名继续')
            hwnd = CreateWindowExW(0, TRAY_CLASS, TRAY_CLASS, 0,
                                   0, 0, 0, 0, None, None, wcx.hInstance, None)
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = hwnd
        except Exception as e:
            self.err = '托盘窗口建不出来：%r' % (e,)
            log(self.err)
            return False

        self._hicon = self._load_icon()
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = TRAY_ID
        nid.uFlags = (win32gui.NIF_MESSAGE | win32gui.NIF_ICON |
                      win32gui.NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = TIP_NORMAL
        self._nid = nid
        if not Shell_NotifyIconW(win32gui.NIM_ADD, ctypes.byref(nid)):
            self.err = 'Shell_NotifyIconW(NIM_ADD) 失败（通知区域被策略关了？）'
            log('托盘图标加不上：%s' % self.err)
            return False
        self.active = True
        return True

    def _on_msg(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                self._on_tray(lparam)
                return 0
            if msg == win32con.WM_CLOSE:
                DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception as e:
            log('托盘消息处理出错: %r' % e)
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_tray(self, lparam):
        ev = int(lparam) & 0xFFFF
        if ev in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            now = time.time()
            if now - self._last_click < 0.35:
                return                   # 双击会连发 UP + DBLCLK，只认第一下
            self._last_click = now
            self.toggle()
        elif ev == win32con.WM_RBUTTONUP:
            self._menu()

    def _menu(self):
        with self._lock:
            hidden = self.hidden
        menu = CreatePopupMenu()
        if not menu:
            return
        cmd = 0
        try:
            AppendMenuW(menu, win32con.MF_STRING, IDM_SHOW,
                        '显示面板' if hidden else '收起面板')
            SetMenuDefaultItem(menu, IDM_SHOW, 0)
            AppendMenuW(menu, win32con.MF_STRING, IDM_RESTART, '重启壁纸')
            AppendMenuW(menu, win32con.MF_SEPARATOR, 0, None)
            AppendMenuW(menu, win32con.MF_STRING, IDM_QUIT, '退出面板')
            pt = wintypes.POINT()
            GetCursorPos(ctypes.byref(pt))
            # TrackPopupMenu 之前必须把本窗口设成前台，否则菜单点空白处不消失
            # —— 这是 MSDN 写明的经典要求，不是迷信。
            SetForegroundWindow(self.hwnd)
            cmd = TrackPopupMenu(menu, win32con.TPM_RETURNCMD |
                                 win32con.TPM_RIGHTBUTTON,
                                 pt.x, pt.y, 0, self.hwnd, None)
            PostMessageW(self.hwnd, win32con.WM_NULL, 0, 0)
        except Exception as e:
            log('托盘菜单出错: %r' % e)
        finally:
            DestroyMenu(menu)

        if cmd == IDM_SHOW:
            if hidden:
                self.restore()
            else:
                self.hide_to_tray('托盘菜单')
        elif cmd == IDM_RESTART:
            self._restart_wallpaper()
        elif cmd == IDM_QUIT:
            self._quit_panel()

    def _restart_wallpaper(self):
        try:
            pid = read_pid()
            if pid and pid_alive(pid):
                _kill(pid)
                time.sleep(1.2)
            _spawn(WALLPAPER)
            log('托盘：请求重启壁纸')
        except Exception as e:
            log('托盘重启壁纸失败: %r' % e)

    def _quit_panel(self):
        """托盘菜单里的「退出面板」—— 规则 A：先问壁纸怎么办。

        关窗口、不杀自己：main() 的等待循环一发现窗口没了（或 SERVER_INTENT
        置位）就顺着正常退出路径收拾干净 —— 服务、托盘图标、pid 登记、临时
        profile 全在 finally 里。
         不能用 taskkill 杀自己：那样 finally 里的收尾一律不跑，托盘上会留
          一个点不动的死图标（得把鼠标划过去它才消失）。

         面板正收在托盘里时也必须问：原生对话框不依赖面板窗口，窗口藏起来
          一点不影响它弹出来 —— 而"窗口藏着就静默退出"正是这里最不能有的行为。
        """
        choice = ask_exit_choice('托盘菜单')
        if choice is None:
            log('托盘：退出被取消，面板保持原样')
            return
        if choice == EXIT_STOP:
            stop_wallpaper_now('托盘菜单选择一并停止')
        SERVER_INTENT['stop'] = True
        if not close_panel_window():
            log('托盘：想退出面板，但没找到窗口')

    # ------------------------------------------------------------ 内部：主循环
    def _run(self):
        if not self._install():
            return
        log('托盘图标已就位（左键收起/显示，右键菜单）')
        threading.Thread(target=self._watch_loop, name='tray-watch',
                         daemon=True).start()
        msg = wintypes.MSG()
        try:
            while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                TranslateMessage(ctypes.byref(msg))
                DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log('托盘消息循环异常退出: %r' % e)
        self.active = False

    def _watch_loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log('托盘看护出错: %r' % e)
            self._stop.wait(TRAY_POLL_MS / 1000.0)

    def _tick(self):
        h = self._win()
        if not h:
            return
        if not self.active and not self._stop.is_set():
            # 托盘失活了（图标被策略摘掉 / 消息循环挂了）而窗口还藏着 —— 必须
            # 先把窗口还回来。否则面板就永远锁在一个点不开的图标后面，用户只剩
            # 任务管理器一条路。这条是"藏"这个动作的安全闸，和 hide_to_tray
            # 入口处的 `if not self.active` 一前一后，两头都堵住。
            with self._lock:
                was_hidden = self.hidden
                self.hidden = False
            if was_hidden:
                log('托盘失活 → 把面板窗口还回来')
                show_panel_window()
            return
        with self._lock:
            if self.hidden:
                if IsWindowVisible(h) and not IsIconic(h):
                    # 别人把它显示回来了 → 跟上状态，否则下次点击要去"显示"
                    # 一个已经显示着的窗口。
                    self.hidden = False
                    self._tip(TIP_NORMAL)
                return
        if IsIconic(h):
            with self._lock:
                self.hidden = True
            try:
                ShowWindow(h, win32con.SW_HIDE)
            except Exception as e:
                log('藏面板窗口失败: %r' % e)
            self._tip(TIP_HIDDEN)
            log('面板被最小化 → 收进托盘')
            self._balloon(WINDOW_TITLE,
                          '面板已收进托盘，单击托盘图标可以再打开')


TRAY = None          # main() 里装配；--serve-only 模式永远是 None


# ================================================================ main
def main(argv):
    global PANEL_ORIGIN, _PORT
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 必须在任何取尺寸之前
    except Exception:
        pass

    serve_only = '--serve-only' in argv
    SERVE_ONLY['on'] = serve_only
    if not os.path.exists(WALLPAPER):
        _die('找不到 %s，面板无法工作。' % WALLPAPER)
        return 2
    if not os.path.exists(PAGE):
        _die('找不到 %s（界面文件），面板无法工作。' % PAGE)
        return 2

    if not serve_only and focus_existing_panel():
        log('面板已经在运行，把它抬到前面')
        return 0

    want_port = 0
    for i, a in enumerate(argv):
        if a == '--port' and i + 1 < len(argv):
            try:
                want_port = int(argv[i + 1])
            except ValueError:
                pass

    # 端口交给系统分配（绑 0）—— 不写死端口就不会跟别的程序撞，
    # 而且天然只监听 127.0.0.1，外网碰不到。
    httpd = ThreadingHTTPServer(('127.0.0.1', want_port), Handler)
    httpd.daemon_threads = True
    _PORT = httpd.server_address[1]
    PANEL_ORIGIN = 'http://127.0.0.1:%d' % _PORT
    url = PANEL_ORIGIN + '/'
    log('面板服务已起：%s（pid=%d）' % (url, os.getpid()))
    print('PANEL_URL %s' % url, flush=True)
    # 单实例登记只属于"有窗口"的那种模式。`--serve-only` 是测试用的无窗口模式，
    # 它跑完就走，绝不能让它去登记/清除：否则它会覆盖掉真面板的 pid 记录，
    # 退出时又把真面板的记录删掉 —— 于是真面板还在跑，单实例判据却已经失灵。
    owns_slot = not serve_only
    if owns_slot:
        # 绑定成功之后才登记自己 —— 早写会留下一个"进程在、服务不在"的假记录，
        # 下次启动就会误判"面板已开着"，又是"点了没反应"。
        write_panel_pid(os.getpid())

    threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.2},
                     daemon=True).start()
    # 趁界面还在加载，后台把预览底图焐热 —— 免得第一下拖动像卡住（实测那一步 340ms）
    threading.Thread(target=SCENE.prime, daemon=True).start()

    if serve_only:
        try:
            while not SERVER_INTENT['stop']:
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
        return 0

    proc, profile = launch_window(url)

    # 托盘在窗口之后装。装不上也不影响面板本身：只是"收进托盘"这一项
    # 整个不可用（hide_to_tray 自己会拒绝），面板照常显示、照常改参数。
    global TRAY
    if proc is not None:
        TRAY = Tray()
        TRAY.start()

    try:
        # ---- 等"该收摊了"，顺带把规则 A 在 × 那条路上补上 ----
        # proc.wait() 只能告诉你"窗口没了"，而标题栏 × 恰恰是唯一拦不住的入口
        # （窗口过程在 Edge 手里，我们收不到 WM_CLOSE）。所以这里改成轮询 + 分支：
        #   窗口没了 → 现在才问。原生对话框不依赖那个窗口，窗口没了照样弹得出来。
        #     · 选「取消」→ 把窗口重新拉起来，继续服务
        #     · 选「连壁纸一起停」→ 停掉壁纸再退
        # 按钮和托盘那两条路早问完了，会先把 SERVER_INTENT 置位 → 这里直接 break，
        # 不会问第二次。
        while True:
            while not SERVER_INTENT['stop'] and (proc is None or proc.poll() is None):
                # proc is None = 没有自家窗口可等（Edge / Chrome 都没有，退回了
                # 默认浏览器）。这里不能直接返回 —— 主进程一退，那个标签页就
                # 再也连不上后端，界面永远停在"离线"，用户只会觉得"面板坏了"。
                time.sleep(0.15)
            if SERVER_INTENT['stop']:
                break
            choice = ask_exit_choice('标题栏 ×')
            if choice is None:
                # 用户其实还不想退，只是把窗口关了 —— 把窗口还回来。
                # 不这么做的话，「取消」这个选项在 × 这条路上就等于"面板必消失"。
                log('退出被取消 → 重新拉起面板窗口')
                old_profile = profile
                proc, profile = launch_window(url)
                if old_profile:
                    shutil.rmtree(old_profile, ignore_errors=True)
                if proc is None:
                    log('面板窗口拉不起来，按退出处理')
                    break
                continue
            if choice == EXIT_STOP:
                stop_wallpaper_now('关闭窗口时选择一并停止')
            break
    except KeyboardInterrupt:
        pass
    finally:
        if TRAY is not None:
            # 先摘图标再收摊。顺序反了会在通知区域留一个点不动的死图标，
            # 得把鼠标划过去它才消失 —— 用户会以为面板还开着。
            TRAY.stop()
        httpd.shutdown()
        try:
            httpd.server_close()
        except Exception:
            pass
        if profile:
            # Edge 刚退出时 profile 里还有文件被占着，删不干净就留着，
            # 系统临时目录自己会清 —— 这属于"锦上添花"，不值得为它报错
            shutil.rmtree(profile, ignore_errors=True)
        if owns_slot:
            clear_panel_pid()  # 退出必须清干净，否则下次启动会被判成"已经开着了"
    log('面板已退出')
    return 0


def _die(text):
    log('面板致命错误: %s' % text)
    try:
        ctypes.windll.user32.MessageBoxW(None, text, WINDOW_TITLE, 0x10)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        log('面板崩溃: %s' % traceback.format_exc())
        _die('控制面板出错了，详情见 wallpaper.log。')
        sys.exit(1)
