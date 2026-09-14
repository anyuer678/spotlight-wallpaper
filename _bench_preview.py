# -*- coding: utf-8 -*-
"""预览一帧到底花在哪。

把「HTTP 往返」和「服务端内部各步」分开量 —— 否则只能看到一个总数，
优化就变成了猜。

 必须用 keep-alive 持久连接量。一开始我用 urllib.request，它每次请求都
  新建一条 TCP 连接，量出来 12ms，而其中 7ms 是握手和线程创建的固定开销 ——
  真实浏览器开着 keep-alive，那部分根本不存在。量法不对会把人引向错误的优化。

跑法：python _bench_preview.py
"""

import http.client
import importlib.machinery
import importlib.util
import io
import os
import statistics
import threading
import time
from http.server import ThreadingHTTPServer

import sys

from PIL import Image, ImageChops

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader('panel_mod',
                                              os.path.join(BASE, 'panel.pyw'))
spec = importlib.util.spec_from_loader('panel_mod', loader)
P = importlib.util.module_from_spec(spec)
loader.exec_module(P)

httpd = ThreadingHTTPServer(('127.0.0.1', 0), P.Handler)
httpd.daemon_threads = True
PORT = httpd.server_address[1]
P.PANEL_ORIGIN = 'http://127.0.0.1:%d' % PORT
threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.1},
                 daemon=True).start()

VW, VH = 480, 312
X0, Y0 = 300, 220
BOX = (X0, Y0, X0 + VW, Y0 + VH)
SW, SH = P.screen_size()

conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
print('屏幕 %dx%d   画布 %dx%d   端口 %d' % (SW, SH, VW, VH, PORT))


def path(sx, sy, glow=25, size=380, feather=45):
    return ('/api/preview?vw=%d&vh=%d&x0=%d&y0=%d&x1=%d&y1=%d'
            '&sx=%d&sy=%d&size=%d&feather=%d&glow=%d'
            % (VW, VH, BOX[0], BOX[1], BOX[2], BOX[3], sx, sy, size, feather, glow))


def hit(p, body=False):
    t = time.perf_counter()
    conn.request('GET', p)
    r = conn.getresponse()
    data = r.read()
    dt = (time.perf_counter() - t) * 1000
    return (dt, data) if body else dt


def stats(name, ts):
    ts = sorted(ts)
    print('  %-34s 中位 %5.1f ms   最好 %5.1f   最差 %5.1f   ≈ %3.0f 帧/秒'
          % (name, statistics.median(ts), ts[0], ts[-1],
             1000.0 / max(0.01, statistics.median(ts))))


# ---------------------------------------------------------------- 冷启动
t = time.perf_counter()
hit(path(400, 300))
print('\n[冷启动] 第一帧（解码两张图 + 各缩放到整屏）：%.1f ms'
      % ((time.perf_counter() - t) * 1000))
print('         （服务启动时会预热这一步，用户感受不到）')

# ---------------------------------------------------------------- 热路径
for _ in range(3):
    hit(path(400, 300))

ts = [hit(path(400 + i, 300 + (i % 7))) for i in range(60)]
print('\n[热路径 · 光斑在动、取景框不变]  60 帧')
stats('拖光斑', ts)

ts = []
for i in range(40):
    ts.append(hit(path(400, 300, size=300 + i * 8)))
stats('拖「光圈大小」滑块（每帧都要重建蒙版）', ts)

ts = []
for i in range(40):
    ts.append(hit(path(400, 300, feather=20 + i, glow=10 + i)))
stats('拖「边缘柔和 + 光晕」滑块', ts)

# ---------------------------------------------------------------- 编码保真
print('\n[编码保真] JPEG 相对 PNG 的像素偏差（同一帧）')
_, png = hit(path(400, 300), body=True)
ref = Image.open(io.BytesIO(png)).convert('RGB')
b = P.SCENE.view('outer', BOX, VW, VH)
i2 = P.SCENE.view('inner', BOX, VW, VH)
# 取景框宽 = 画布宽 ⇒ 取景比 1:1 ⇒ 光斑画布直径就是 size（380）。
# （上一版这里写成 222，跟参考帧压根不是同一个画面，量出来的偏差自然离谱。）
D = 380
m = P.SCENE.get_mask(D, 45)
g = P.SCENE.get_glow(D, 25 / 100.0)
frame = P.compose_preview(b, i2, m, g, 240, 156)
for q in (80, 88, 92, 95):
    o = io.BytesIO()
    frame.save(o, 'JPEG', quality=q)
    o.seek(0)
    d = ImageChops.difference(ref, Image.open(o).convert('RGB'))
    hist = d.convert('L').histogram()
    n_diff = sum(hist[1:])
    big = sum(hist[5:])
    print('  q=%-3d  最大偏差 %3d   有差异的像素 %6d（其中 >4 的 %d）  体积 %5.1f KB'
          % (q, max(i for i, v in enumerate(hist) if v), n_diff, big,
             len(o.getvalue()) / 1024.0))

# ---------------------------------------------------------------- 内部拆解
print('\n[服务端内部] 各步中位数')


def med(fn, n=21):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


print('  取景底图（缓存命中）        %.2f ms' % med(lambda: P.SCENE.view('outer', BOX, VW, VH)))
print('  取景底图（换框，要重采样）  %.2f ms'
      % med(lambda: P.SCENE.view('outer', (X0 + 1, Y0, X0 + 1 + VW, Y0 + VH), VW, VH)))
print('  make_mask(380)  冷          %.2f ms'
      % med(lambda: P.CORE._CORE_CACHE.clear() or P.CORE.make_mask(380, 380, 45), 5))
print('  make_mask(380)  暖          %.2f ms' % med(lambda: P.CORE.make_mask(380, 380, 45)))
print('  make_glow(380)  暖          %.2f ms'
      % med(lambda: P.CORE.make_glow(380, 380, 0.25)))
print('  compose_preview             %.2f ms'
      % med(lambda: P.compose_preview(b, i2, m, g, 240, 156)))

frame = P.compose_preview(b, i2, m, g, 240, 156)
for fmt, kw in (('PNG', {'compress_level': 1}), ('JPEG', {'quality': 92}),
                ('BMP', {})):
    o = io.BytesIO()
    frame.save(o, fmt, **kw)
    n = len(o.getvalue())
    print('  编码 %-5s %-16s %.2f ms   %6.1f KB'
          % (fmt, kw or '', med(lambda f=fmt, k=kw: (lambda oo: frame.save(oo, f, **k))
                                (io.BytesIO())), n / 1024.0))

conn.close()
httpd.shutdown()
