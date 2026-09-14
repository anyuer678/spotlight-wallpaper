# -*- coding: utf-8 -*-
"""临时验证：面板预览的合成是否像素正确。

为什么不能靠看截图：上一版蒙版偏移写错了，画面上"看着差不多"，
必须逐像素跟参考实现比。跑完即删。
"""
import importlib.machinery
import importlib.util
import os
import sys

from PIL import Image, ImageChops

BASE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader(
    'panel_mod', os.path.join(BASE, 'panel.pyw'))
spec = importlib.util.spec_from_loader('panel_mod', loader)
P = importlib.util.module_from_spec(spec)
loader.exec_module(P)
CORE = P.CORE

W, H = P.PV_W, P.PV_H
BG_C = (20, 20, 20)
IN_C = (200, 100, 50)

fails = []


def check(name, ok, detail=''):
    print('  %-44s %s %s' % (name, '通过' if ok else '★ 失败', detail))
    if not ok:
        fails.append(name)


def span_row(img, y, bg=BG_C):
    """这一行上"非底色"像素的跨度（首尾距离）"""
    xs = [x for x in range(img.width) if img.getpixel((x, y)) != bg]
    return (xs[-1] - xs[0] + 1) if xs else 0


def span_col(img, x, bg=BG_C):
    ys = [y for y in range(img.height) if img.getpixel((x, y)) != bg]
    return (ys[-1] - ys[0] + 1) if ys else 0


print('画布 %dx%d' % (W, H))

bg = Image.new('RGB', (W, H), BG_C)
inner = Image.new('RGB', (W, H), IN_C)

D = 200
mask = CORE.make_mask(D, D, 0)
cx, cy = W // 2, H // 2
img = P.compose_preview(bg, inner, mask, None, cx, cy)

check('圆心 = 内图', img.getpixel((cx, cy)) == IN_C, img.getpixel((cx, cy)))
check('圆内 (中心+60px) = 内图', img.getpixel((cx, cy + 60)) == IN_C)
check('圆外 (中心+140px) = 底图', img.getpixel((cx, cy + 140)) == BG_C)
check('左上角 = 底图', img.getpixel((2, 2)) == BG_C)
check('右下角 = 底图', img.getpixel((W - 3, H - 3)) == BG_C)

# 直径：蒙版 D=200，LANCZOS 边界会糊 1~3px，允许 ±6
dw = span_row(img, cy)
dh = span_col(img, cx)
check('横向直径 ≈ %d' % D, abs(dw - D) <= 6, '实测 %d' % dw)
check('纵向直径 ≈ %d' % D, abs(dh - D) <= 6, '实测 %d' % dh)

# ② 内外同图：合成结果必须与底图逐像素完全一致（无缝、无黑边、无错位）
same = P.compose_preview(bg, bg.copy(), mask, None, cx, cy)
check('内外同图 → 逐像素零差异',
      ImageChops.difference(same, bg).getbbox() is None)

# ③ 蒙版自己挪到非中心位置时，圆心必须真的跟着走
img_off = P.compose_preview(bg, inner, mask, None, 120, 90)
check('挪到 (120,90)：该点 = 内图', img_off.getpixel((120, 90)) == IN_C)
check('挪到 (120,90)：旧圆心 = 底图', img_off.getpixel((cx, cy)) == BG_C)
check('挪到 (120,90)：直径仍 ≈ %d' % D, abs(span_row(img_off, 90) - D) <= 6,
      '实测 %d' % span_row(img_off, 90))

# ④ 羽化蒙版
soft = CORE.make_mask(D, D, 45)
img2 = P.compose_preview(bg, inner, soft, None, cx, cy)
check('羽化：圆心 = 内图', img2.getpixel((cx, cy)) == IN_C)
check('羽化：过渡带是混色',
      BG_C != img2.getpixel((cx, cy + int(D * 0.45))) != IN_C,
      img2.getpixel((cx, cy + int(D * 0.45))))
check('羽化：圆外 8px = 底图', img2.getpixel((cx, cy + D // 2 + 8)) == BG_C)

# ⑤ 贴到边缘 / 角落：不抛异常，且不允许出现"区间外"的颜色。
#    越界填黑会得到 (0,0,0) —— 比底图 (20,20,20) 还暗，一眼就能抓出来。
#    （注意不能要求"只能有两种颜色"：蒙版边缘是渐变的，本来就该有中间色。）
def in_range(px):
    return all(min(BG_C[i], IN_C[i]) <= px[i] <= max(BG_C[i], IN_C[i])
               for i in range(3))


for pos in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)):
    try:
        im = P.compose_preview(bg, inner, mask, None, pos[0], pos[1])
        bad = [px for px in set(im.getdata()) if not in_range(px)]
        ok, detail = not bad, ('' if not bad else '区间外颜色 %s' % bad[:4])
    except Exception as e:
        ok, detail = False, repr(e)
    check('越界 %-12s 无异常无杂色' % str(pos), ok, detail)

# ⑥ 光晕的落位。它最容易错 —— 符号写反就跑出画布了，
#    画面上只表现为"某一块有点发灰"，肉眼根本判断不了。
#    蒙版故意全 0（内图完全不出现），这样画面里只有光晕，最亮点必然在光斑中心。
ZERO = Image.new('L', (4, 4), 0)
for d in (160, 210, 380):
    glow = CORE.make_glow(d, d, 0.9, ss=64)
    zero = Image.new('L', (d, d), 0)
    for pos in ((W // 2, H // 2), (300, 120), (90, 240)):
        im = P.compose_preview(bg, inner, zero, glow, pos[0], pos[1])
        best, bx, by = -1, -1, -1
        for yy in range(0, H, 2):
            for xx in range(0, W, 2):
                s = sum(im.getpixel((xx, yy)))
                if s > best:
                    best, bx, by = s, xx, yy
        dist = ((bx - pos[0]) ** 2 + (by - pos[1]) ** 2) ** 0.5
        check('光晕 d=%-4d @%-12s 最亮点落位' % (d, str(pos)),
              dist <= 4, '最亮在 (%d,%d)，偏离 %.1f px' % (bx, by, dist))
    # 居中时光晕够不到的角上必须原封不动
    im = P.compose_preview(bg, inner, zero, glow, W // 2, H // 2)
    check('光晕 d=%-4d 远处四角 = 底图' % d,
          all(im.getpixel(c) == BG_C
              for c in ((1, 1), (W - 2, 1), (1, H - 2), (W - 2, H - 2))))

# ⑥ 真图：合成后不应出现纯黑（越界填黑的典型症状）
out_p = os.path.join(BASE, 'images', 'out.jpg')
in_p = os.path.join(BASE, 'images', 'in.jpg')
if os.path.exists(out_p) and os.path.exists(in_p):
    src_o = CORE.cover(Image.open(out_p).convert('RGB'), 2560, 1600)
    src_i = CORE.cover(Image.open(in_p).convert('RGB'), 2560, 1600)
    box = (1080, 650, 1080 + W, 650 + H)
    a, b = src_o.crop(box), src_i.crop(box)
    for pos in ((30, 30), (W - 20, H - 20), (W // 2, H // 2)):
        im = P.compose_preview(a, b, CORE.make_mask(210, 210, 45), None, *pos)
        dark = sum(1 for px in im.getdata() if max(px) < 6)
        check('真图 @%-14s 无纯黑' % str(pos), dark == 0, '暗像素 %d' % dark)
else:
    print('  （没有 images/out.jpg|in.jpg，跳过真图用例）')

print('')
if fails:
    print('★ 失败 %d 项：%s' % (len(fails), '、'.join(fails)))
    sys.exit(1)
print('全部通过')
