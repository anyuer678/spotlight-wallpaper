# -*- coding: utf-8 -*-
"""验证新的「不透明合成」渲染是不是逐像素正确。

方法：把两张源图换成坐标编码图（R=x 低 8 位，G=y 低 8 位，B=标记），
这样每个像素自己就带着"我来自屏幕哪个位置"的答案。然后：

  1. 几何对不对：光斑缓冲里 (i,j) 处的像素，是不是屏幕 (x-r+i, y-r+j) 的内容？
  2. 混合对不对：跟 Image.alpha_composite 逐层叠加的独立参考比，差多少？
  3. 边缘会不会发黑：光斑贴到屏幕边上时，Pillow 的 crop 越界填黑有没有漏出来？

重点照顾贴边的情况 —— 那里以前的写法会出黑框。
"""
import os, sys, ctypes, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ctypes.windll.shcore.SetProcessDpiAwareness(2)

spec = importlib.util.spec_from_file_location('wl', os.path.join(HERE, 'wallpaper.pyw'))
wl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wl)
from PIL import Image

app = wl.SpotlightWallpaper(mode='window', selftest=True)
try:
    app.root.withdraw()
except Exception:
    pass

W, H = app.cw, app.ch
print('测试画布 %dx%d   光斑直径 %d   羽化 %d   光晕 %d'
      % (W, H, int(app._radius() * 2), app.feather, app.cfg.get('glow', 0)))

# ---------------------------------------------------------------- 坐标编码图
def coord_image(tag):
    im = Image.new('RGB', (W, H))
    px = im.load()
    for y in range(H):
        yb = y & 255
        for x in range(W):
            px[x, y] = (x & 255, yb, tag)
    return im


app.img_outer = coord_image(7)
app.img_inner = coord_image(200)
app._draw_static()          # 用坐标图重建相册和缓冲

r = int(app._radius())
d = int(r * 2)
mask = app._get_mask(d, d)
glow = app._glow_small
mpx = mask.load()
gpx = glow.load() if glow is not None else None


def expected(x, y):
    """独立参考实现：完全用 Pillow 官方的 alpha_composite 逐层叠加。"""
    dx, dy = int(x - r), int(y - r)
    o = app.img_outer.crop((dx, dy, dx + d, dy + d)).convert('RGBA')
    layers = o
    if glow is not None:
        layers = Image.alpha_composite(layers, glow)
    inner = app.img_inner.crop((dx, dy, dx + d, dy + d)).convert('RGBA')
    inner.putalpha(mask)
    return Image.alpha_composite(layers, inner).convert('RGB')


def check(x, y, label):
    app._render(x, y)
    buf = app._spot_buf
    ref = expected(x, y)
    bpx = buf.load()
    rpx = ref.load()
    dx, dy = int(x - r), int(y - r)

    # ---- 1) 几何：取几个羽化遮罩=0 的角落点，看内容是不是屏幕对应位置
    geo_ok = geo_bad = 0
    black = 0
    blend_max = 0
    blend_bad = 0
    for j in range(0, d, 7):
        for i in range(0, d, 7):
            sx, sy = dx + i, dy + j
            if not (0 <= sx < W and 0 <= sy < H):
                continue                     # 屏幕外看不见，不比较
            got = bpx[i, j]
            if got == (0, 0, 0):
                black += 1
            # 与独立参考比
            e = rpx[i, j]
            diff = max(abs(got[k] - e[k]) for k in range(3))
            blend_max = max(blend_max, diff)
            if diff > 2:
                blend_bad += 1
            # 遮罩全透明处，内容必须就是外图（坐标图）
            if mpx[i, j] == 0:
                if got == ((sx & 255), (sy & 255), 7):
                    geo_ok += 1
                else:
                    geo_bad += 1

    print('  %-22s 几何 %4d/%-4d  与独立参考最大差 %3d（>2 的像素 %d）  纯黑 %d'
          % (label, geo_ok, geo_ok + geo_bad, blend_max, blend_bad, black))
    return geo_bad == 0 and blend_bad == 0 and black == 0


print('')
print('=' * 78)
print('  逐像素校验（几何 / 混合 / 边缘发黑）')
print('=' * 78)
cases = [
    (W // 2, H // 2, '正中间'),
    (r, H // 2, '左边缘刚好齐'),
    (r - 130, H // 2, '左边缘越界 130px'),
    (5, 5, '左上角'),
    (W - 5, H - 5, '右下角'),
    (W // 2, 4, '上边缘'),
    (0, 0, '原点'),
    (W - 1, H // 2, '最右一列'),
]
allok = True
for x, y, label in cases:
    allok &= check(x, y, label)

print('=' * 78)
print('  结论：', '全部通过 —— 几何正确、与参考实现一致、边缘无黑块'
      if allok else '**** 有不通过项，见上表 ****')
print('=' * 78)

# 额外：确认光斑内容确实来自内图（中心点遮罩=255）
app._render(W // 2, H // 2)
c = app._spot_buf.load()[d // 2, d // 2]
sx, sy = W // 2, H // 2
print('\n中心像素 = %s   期望 = %s（内图编码）  %s'
      % (c, (sx & 255, sy & 255, 200),
         'OK' if c == (sx & 255, sy & 255, 200) else '不一致'))

try:
    app.root.destroy()
except Exception:
    pass
sys.exit(0 if allok else 1)
