# -*- coding: utf-8 -*-
"""临时诊断 v2：量 Tk 画布各种重绘方式的真实耗时。

v1 死在 win32con.RDW_UPDATABLE 不存在 —— 顺带发现主程序里那两处
RedrawWindow 一直静默失败。这里用真正的常量 RDW_UPDATENOW。
跑完即删。
"""
import ctypes
import time
import tkinter as tk

ctypes.windll.winmm.timeBeginPeriod(1)
ctypes.windll.shcore.SetProcessDpiAwareness(2)

import win32con
import win32gui
from PIL import Image, ImageTk

SW, SH = 2560, 1600
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_UPDATENOW = 0x0100


def med(fn, n=7):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2]


root = tk.Tk()
root.overrideredirect(True)
root.geometry('%dx%d+0+0' % (SW, SH))
canvas = tk.Canvas(root, width=SW, height=SH, highlightthickness=0, bd=0)
canvas.pack()

src = Image.new('RGB', (SW, SH), (32, 44, 64))
tk_outer = ImageTk.PhotoImage(src)
outer_id = canvas.create_image(0, 0, anchor='nw', image=tk_outer)
spot_src = Image.new('RGB', (380, 380), (214, 186, 140))
tk_spot = ImageTk.PhotoImage(spot_src)
spot_id = canvas.create_image(900, 700, anchor='center', image=tk_spot)
root.update()

hwnd = root.winfo_id()
print('窗口 %dx%d hwnd=%d' % (SW, SH, hwnd))

print('① 整窗失效+立即重绘(同步)   : %7.2f ms'
      % med(lambda: win32gui.RedrawWindow(
          hwnd, None, None,
          RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW)))

r = 190
print('② 只失效光斑小区域+立即重绘 : %7.2f ms'
      % med(lambda: win32gui.RedrawWindow(
          hwnd, (900 - r, 700 - r, 900 + r, 700 + r), None,
          RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW)))


def deferred_full():
    win32gui.RedrawWindow(hwnd, None, None, RDW_INVALIDATE | RDW_ERASE)
    root.update()


print('③ 整窗失效+延后兑现         : %7.2f ms' % med(deferred_full))

xs = iter(range(900, 990))


def move():
    try:
        x = next(xs)
    except StopIteration:
        return
    canvas.coords(spot_id, x, 700)
    root.update()


print('④ 挪光斑 item               : %7.2f ms' % med(move))


def jump():
    for x, y in ((300, 300), (2200, 1300)):
        canvas.coords(spot_id, x, y)
        root.update()


print('⑤ 光斑跨半屏跳一次          : %7.2f ms' % med(jump, 5))


def rebuild():
    canvas.delete('all')
    ph = ImageTk.PhotoImage(src)
    canvas.create_image(0, 0, anchor='nw', image=ph)
    ps = ImageTk.PhotoImage(spot_src)
    canvas.create_image(900, 700, anchor='center', image=ps)
    root.update()
    globals()['_keep'] = (ph, ps)


print('⑥ 整画布重建(delete+建图)   : %7.2f ms' % med(rebuild, 5))

# ⑦ 光斑 PhotoImage 就地 paste 的纯开销（对照，应该有 1~2ms）
buf = Image.new('RGB', (380, 380), (180, 160, 120))
print('⑦ tk_spot.paste(380² RGB)   : %7.2f ms'
      % med(lambda: tk_spot.paste(buf), 20))

root.destroy()
