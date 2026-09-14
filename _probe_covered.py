"""「离开桌面之后光圈变方」的回归脚本。

症状：光标一离开桌面（压到窗口 / 任务栏上），桌面露着的那一角就会出现一块
d×d 的方块 —— 里面的图像和外图对不上，看着像"光圈变方了"。

机理（2026-09-14 已实测确认并修掉）：_park() 只搬画布 item 的位置
（canvas.coords），不重画图像；而那个 item 是一张不透明 d×d 的 RGB 图，
内容还停在"上一次真正渲染的位置"。搬到光标底下，方块就出现了。

判据（不问实现，只问看得见的效果）：

    移动光标前后各抓一张画布图，取以新光标为中心、边长 = 光斑直径的那个
    方块，比较两张图在这块里的差异比例。

        错位方块 → 整块内容都是别人家的像素 → 差异比例 ≈ 1   → 失败（退出码 1）
        正常     → 这一块要么是本地外图，要么是刚从本地外图渲染出来的光斑
                   → 差异比例 ≈ 0（冻住）或 ≈ 0.2（正常跟随）      → 通过（退出码 0）

不需要参照图，所以不受"角标 / 别的窗口 / 外图重建对不对"影响。

    用法：壁纸得先在跑。
      python _probe_covered.py
用完会把光标放回原处。
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes as wt

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))
BASE = os.getcwd()

import win32gui          # noqa: E402
import win32ui           # noqa: E402
from PIL import Image, ImageChops    # noqa: E402

SW, SH = 2560, 1600
TARGET = (260, 1380)          # 挪到左下角，离哪儿都远
DIFF_THRESH = 30              # RGB 曼哈顿距离，超过才算"不一样"
u = ctypes.windll.user32


# ---------------------------------------------------------------- 抓画布
def grab(hwnd):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hwnd_dc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    ok = u.PrintWindow(hwnd, mem.GetSafeHdc(), 2)      # PW_RENDERFULLCONTENT
    bits = bmp.GetBitmapBits(True)
    im = Image.frombuffer('RGB', (w, h), bits, 'raw', 'BGRX', 0, 1)
    mem.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bmp.GetHandle())
    return im if ok else None


def find_canvas():
    """画布 = Progman 下面最大的那个 Tk 子窗口（Tk 把 canvas 做成独立窗口）"""
    progman = win32gui.FindWindow('Progman', None)
    hits = []

    def cb(h, _):
        cls = win32gui.GetClassName(h)
        if cls in ('TkTopLevel', 'TkChild'):
            r = win32gui.GetWindowRect(h)
            hits.append((h, cls, r, (r[2] - r[0]) * (r[3] - r[1])))
        return True

    win32gui.EnumChildWindows(progman, cb, None)
    kids = sorted([x for x in hits if x[1] == 'TkChild'],
                  key=lambda x: x[3], reverse=True)
    return progman, hits, (kids[0][0] if kids else 0)


def wait_canvas(timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        progman, hits, cv = find_canvas()
        if cv:
            return progman, hits, cv, time.time() - t0
        time.sleep(0.5)
    return progman, hits, 0, time.time() - t0


def wait_stable(cv, need=3, gap=1.5, timeout=30.0):
    """等画面稳定：连续 need 次抓图完全一致。

    顺便把启动时那个提示角标（14 秒后消失）等过去，免得它混进差异里。
    """
    t0 = time.time()
    prev = grab(cv)
    same = 1
    while time.time() - t0 < timeout:
        time.sleep(gap)
        cur = grab(cv)
        same = same + 1 if (prev is not None and cur is not None
                            and _eq(prev, cur)) else 1
        prev = cur
        if same >= need:
            return prev, time.time() - t0
    return prev, time.time() - t0


def _eq(a, b):
    if a.size != b.size:
        return False
    return a.tobytes() == b.tobytes()


def cursor_info():
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    h = u.WindowFromPoint(wt.POINT(pt.x, pt.y))
    root = u.GetAncestor(h, 2) if h else 0
    progman = win32gui.FindWindow('Progman', None)
    return pt.x, pt.y, root == progman, (win32gui.GetClassName(root) if root else '')


def diff_mask(a, b):
    """逐像素比，返回 (差异掩码, 差异像素数)。

    用 PIL 的 C 实现算 —— 纯 Python 跑 2560×1600 要二十多秒，没必要。
    判据同纯 Python 版：三通道差的和 > DIFF_THRESH。
    """
    ch = ImageChops.difference(a, b).split()
    s = ImageChops.add(ImageChops.add(ch[0], ch[1]), ch[2])
    mask = s.point(lambda v: 255 if v > DIFF_THRESH else 0)
    return mask, mask.histogram()[255]


def box_fill(mask, cx, cy, half):
    """以 (cx, cy) 为中心、边长 2*half 的方块里，差异像素占比"""
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(mask.width, cx + half), min(mask.height, cy + half)
    if x1 <= x0 or y1 <= y0:
        return None, (x0, y0, x1, y1)
    crop = mask.crop((x0, y0, x1, y1))
    tot = crop.width * crop.height
    return crop.histogram()[255] / float(tot), (x0, y0, x1, y1)


def main():
    progman, hits, cv, waited = wait_canvas()
    print('Progman = %d' % progman)
    for h, cls, r, _ in hits:
        print('  %-11s hwnd=%-9d rect=%s' % (cls, h, r))
    if not cv:
        print('!! 没找到画布窗口（等了 %.1fs）—— 壁纸没在跑？' % waited)
        return 2
    print('画布 hwnd = %d（等了 %.1fs）' % (cv, waited))

    im, took = wait_stable(cv)
    print('画面已稳定（%.1fs）' % took)
    if im is None:
        print('!! PrintWindow 抓不到内容')
        return 2
    im.save('_covered_A.png')

    cx0, cy0, on_desktop0, cls0 = cursor_info()
    print('移动前光标 = (%d, %d)  底下是桌面？%s  [%s]'
          % (cx0, cy0, on_desktop0, cls0))

    try:
        print('移动光标 → %s' % (TARGET,))
        u.SetCursorPos(*TARGET)
        time.sleep(1.2)
        cx1, cy1, on_desktop1, cls1 = cursor_info()
        print('移动后光标 = (%d, %d)  底下是桌面？%s  [%s]'
              % (cx1, cy1, on_desktop1, cls1))
        im2 = grab(cv)
        if im2 is None:
            print('!! 第二次抓图失败')
            return 2
        im2.save('_covered_B.png')

        mask, n = diff_mask(im, im2)
        print()
        print('整幅差异像素 = %d / %d' % (n, im.size[0] * im.size[1]))
        if n == 0:
            print('→ 通过：画布**一动没动**（光斑原地冻住了，最理想）')
            return 0

        # 关键：新光标底下那个 d×d 方块，内容对不对得上
        half = int(round(float(os.environ.get('PROBE_D', '462')) / 2))
        f, box = box_fill(mask, cx1, cy1, half)
        print('光标底下 %dx%d 方块 = %s，差异占比 = %.3f'
              % (box[2] - box[0], box[3] - box[1], box, f))
        mask.save('_covered_diff.png')
        l, t, r, b = mask.getbbox() or (0, 0, 0, 0)
        print('整幅差异外接框 = %s（%dx%d）' % ((l, t, r, b), r - l, b - t))
        if f is not None and f > 0.9:
            print()
            print('→ ★ 失败：光标底下整块都是"别人家的像素" —— '
                  '错位方块又回来了（_park 那套被加回来了？）')
            return 1
        print()
        print('→ 通过：光标底下是干净的外图（或正常渲染的光斑），不是错位方块')
        return 0
    finally:
        u.SetCursorPos(cx0, cy0)
        print('光标已还原 → (%d, %d)' % (cx0, cy0))


if __name__ == '__main__':
    sys.exit(main())
