# -*- coding: utf-8 -*-
"""控制面板 API 端到端自测。

在进程内把 panel.pyw 当模块加载、起一个真的 HTTP 服务，然后用 urllib
打真接口 —— 测的是"用户点下去会发生什么"，不是"某个函数返回值对不对"。

重点覆盖三件容易悄悄坏掉的事：

1. 屏幕尺寸必须是物理像素（本机 2560×1600）。DPI 感知没在模块级声明的话
   会量成 1707×1067，预览取景框就落到错误的位置 —— 而这种错在界面上
   看起来只是"预览好像不太对"，靠眼睛根本判不出来。

2. 预览的几何：取景比 1:1 时，光斑正中心的像素必须逐像素等于内图
   在那个屏幕坐标上的像素，远处必须等于外图。这是对"取景框 → 画布"整套
   坐标换算的独立验证（_test_preview.py 验证的是合成函数本身）。

3. 写配置真的落盘。旧面板"选完图没反应"，根因就是写盘前的比对基准
   用错变量，判成"没变化"直接 return。这类 bug 不看文件内容是发现不了的。

跑法：python _test_panel_api.py
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

from PIL import Image

# 中文 Windows 的控制台默认是 GBK，打 ✓/✗ 会直接 UnicodeEncodeError 把脚本
# 打断 —— 而且断在"第一次打印判定结果"那一行，看起来像测试本身挂了。
# 显式改成 UTF-8，重定向到管道或文件时也不会再出这种事。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'wallpaper-config.json')


def load_panel():
    loader = importlib.machinery.SourceFileLoader('panel_mod',
                                                 os.path.join(BASE, 'panel.pyw'))
    spec = importlib.util.spec_from_loader('panel_mod', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


P = load_panel()
from http.server import ThreadingHTTPServer            # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s%s' % ('✓' if ok else '✗', name,
                         ('   ← ' + str(detail)) if (detail and not ok) else ''))


def get(path, raw=False):
    with urllib.request.urlopen(BASE_URL + path, timeout=20) as r:
        data = r.read()
        return data if raw else json.loads(data.decode('utf-8'))


def post(path, body=None, headers=None, expect_error=False):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    h = {'Content-Type': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(BASE_URL + path, data=data, headers=h,
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if not expect_error:
            raise
        return e.code, json.loads(e.read().decode('utf-8'))


# ---------------------------------------------------------------- 起服务
httpd = ThreadingHTTPServer(('127.0.0.1', 0), P.Handler)
httpd.daemon_threads = True
PORT = httpd.server_address[1]
BASE_URL = 'http://127.0.0.1:%d' % PORT
P.PANEL_ORIGIN = BASE_URL
threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.1},
                 daemon=True).start()
print('面板服务已起：%s\n' % BASE_URL)

# 备份原配置，测完还原 —— 不能因为跑了一次测试就把用户的设置改掉
BACKUP = None
if os.path.exists(CFG_PATH):
    with open(CFG_PATH, 'r', encoding='utf-8') as f:
        BACKUP = f.read()


def restore():
    try:
        if BACKUP is not None:
            with open(CFG_PATH, 'w', encoding='utf-8') as f:
                f.write(BACKUP)
        elif os.path.exists(CFG_PATH):
            os.remove(CFG_PATH)
    except Exception as e:
        print('  ! 还原配置失败：%r' % e)


#  另外还挂到 atexit 上。这个脚本曾经因为一个 UnicodeEncodeError 崩在流程中间，
#   "还原配置"那一步压根没跑到，把用户的尺寸/羽化值静默留在了测试途中的状态 ——
#   我过了好几轮才在一次基准里偶然发现。atexit 能兜住所有退出路径
#   （正常结束、未捕获异常、sys.exit）。
import atexit                                                       # noqa: E402
atexit.register(restore)


# ================================================================ 1. 基本
print('【1】基本通道')
ping = get('/api/ping')
check('/api/ping 通', ping.get('ok') is True, ping)

html = urllib.request.urlopen(BASE_URL + '/', timeout=10).read().decode('utf-8')
check('/ 返回面板页面', '聚光壁纸' in html and '<canvas' in html, len(html))
check('页面标题与窗口标题一致（找窗口靠它）', P.WINDOW_TITLE == '聚光壁纸 · 控制面板')

st = get('/api/state')
check('/api/state 结构完整',
      all(k in st for k in ('config', 'defaults', 'limits', 'screen',
                            'images', 'wallpaper')), list(st.keys()))
sw, sh = st['screen']['w'], st['screen']['h']
check('屏幕尺寸是物理像素（DPI 感知生效）',
      (sw, sh) == P.screen_size() and sw > 1900,
      '量到 %dx%d，期望本机 2560x1600' % (sw, sh))
check('两侧图片都被解析出来（不是占位）',
      st['images']['outer']['exists'] and st['images']['inner']['exists'],
      {k: st['images'][k]['path'] for k in ('outer', 'inner')})
check('图片尺寸读到了', st['images']['outer']['w'] > 0
      and st['images']['inner']['w'] > 0,
      {k: (st['images'][k]['w'], st['images'][k]['h']) for k in ('outer', 'inner')})
check('壁纸状态字段齐全',
      {'pid', 'alive', 'attached', 'mode', 'cpu_percent'} <= set(st['wallpaper']),
      st['wallpaper'])

# ================================================================ 2. 预览几何
print('\n【2】预览几何（取景 1:1，逐像素对账）')
VW, VH = 480, 312
X0, Y0 = 300, 220
box = (X0, Y0, X0 + VW, Y0 + VH)          # 宽 = VW ⇒ 取景比正好 1:1
SX, SY = X0 + 240, Y0 + 156               # 光斑落在取景框正中

q = ('/api/preview?vw=%d&vh=%d&x0=%d&y0=%d&x1=%d&y1=%d&sx=%d&sy=%d'
     '&size=300&feather=45&glow=0' % (VW, VH, box[0], box[1], box[2], box[3], SX, SY))
req = urllib.request.Request(BASE_URL + q)
with urllib.request.urlopen(req, timeout=20) as r:
    png = r.read()
    hdr_zoom = r.headers.get('X-Zoom')
    hdr_d = r.headers.get('X-Spot-D')
    hdr_ms = r.headers.get('X-Render-Ms')

check('返回的是 PNG', png[:8] == b'\x89PNG\r\n\x1a\n', png[:8])
check('取景比 = 1.000000', abs(float(hdr_zoom) - 1.0) < 1e-6, hdr_zoom)
check('光斑画布直径 = size × 取景比', int(hdr_d) == 300, hdr_d)

pim = Image.open(io.BytesIO(png)).convert('RGB')
check('PNG 尺寸 = 请求的画布尺寸', pim.size == (VW, VH), pim.size)

# 参考：主程序用的那套 cover()，铺满整屏
cfg = P.cfg_read()
full = {}
for side in ('outer', 'inner'):
    src = Image.open(P.CORE.resolve_side(cfg, side)).convert('RGB')
    full[side] = P.CORE.cover(src, sw, sh)

cx, cy = SX - box[0], SY - box[1]
got_center = pim.getpixel((cx, cy))
want_center = full['inner'].getpixel((SX, SY))
check('光斑正中 = 内图同坐标像素（羽化中心不透明）',
      max(abs(a - b) for a, b in zip(got_center, want_center)) <= 2,
      '得到 %s，期望 %s' % (got_center, want_center))

fx, fy = 3, 3
got_far = pim.getpixel((fx, fy))
want_far = full['outer'].getpixel((box[0] + fx, box[1] + fy))
check('远离光斑处 = 外图同坐标像素',
      max(abs(a - b) for a, b in zip(got_far, want_far)) <= 2,
      '得到 %s，期望 %s' % (got_far, want_far))

# 光斑贴到取景框左上角 ⇒ 越界裁剪。绝不能出现黑框或杂色。
#  断言是"暗像素不超过源图自带的上限"，不是 dark == 0：默认测试图是亮图，
#   但用户换成夜景照片后，源图本身就可能有暗像素（0.09% 也够把 == 0 打爆）。
#   越界裁剪产生的黑边是整行/整块（≥取景框宽度级别），与源图自带的暗像素
#   差着数量级 —— 用源图暗像素总数做上限，两种情况都分得开。
q2 = q.replace('&sx=%d&sy=%d' % (SX, SY), '&sx=%d&sy=%d' % (X0, Y0))
with urllib.request.urlopen(BASE_URL + q2, timeout=20) as r:
    pim2 = Image.open(io.BytesIO(r.read())).convert('RGB')
dark = sum(pim2.convert('L').histogram()[:6])       # 亮度 < 6 的像素个数
src_dark = sum(full['outer'].convert('L').histogram()[:6]) \
    + sum(full['inner'].convert('L').histogram()[:6])
check('光斑贴角：越界裁剪，无黑边', dark <= src_dark, '暗像素 %d（源图自带 %d）'
      % (dark, src_dark))

# 光晕必须真的起作用。
#  别去取样"光斑正中心"—— 那里被不透明的内图完整盖住了，光晕本来就看不见。
#   光晕的作用位置是羽化环：内图在那一圈是半透明的，光晕从底下透出来形成亮边。
#   所以这里用"有多少像素被点亮、且只变亮不变暗"来判定，不依赖具体的环半径。
from PIL import ImageChops                                          # noqa: E402
q3 = q.replace('glow=0', 'glow=80')
with urllib.request.urlopen(BASE_URL + q3, timeout=20) as r:
    pim3 = Image.open(io.BytesIO(r.read())).convert('RGB')
n_bright = sum(ImageChops.subtract(pim3, pim).convert('L').histogram()[1:])
n_dark = sum(ImageChops.subtract(pim, pim3).convert('L').histogram()[1:])
check('光晕在羽化环上真的点亮了画面', n_bright > 800, '被点亮的像素 %d' % n_bright)
check('光晕只加光不减光（没写反方向）', n_dark <= 20, '变暗的像素 %d' % n_dark)

# 非法取景框（x1 < x0）不能 500 —— 应该自动回落到屏幕中心，照样给一张正常 PNG
try:
    with urllib.request.urlopen(
            BASE_URL + '/api/preview?vw=480&vh=312&x0=900&y0=900'
                       '&x1=10&y1=10&sx=5&sy=5', timeout=20) as r:
        raw = r.read()
    ok = (raw[:8] == b'\x89PNG\r\n\x1a\n'
          and Image.open(io.BytesIO(raw)).size == (VW, VH))
    check('取景框非法时回落到屏幕中心（仍是正常 PNG）', ok, raw[:12])
except Exception as e:
    check('取景框非法时回落到屏幕中心（仍是正常 PNG）', False, repr(e))

print('  （预览单帧 %.1f ms）' % float(hdr_ms))

# ================================================================ 3. 缩略图
print('\n【3】缩略图')
th = urllib.request.urlopen(BASE_URL + '/api/thumb?side=inner&w=168',
                            timeout=15).read()
check('缩略图是 JPEG 且能解码',
      th[:2] == b'\xff\xd8' and Image.open(io.BytesIO(th)).size[0] <= 168,
      th[:4])

# ================================================================ 4. 写配置
print('\n【4】写配置（旧面板在这里翻过车）')
before = json.load(open(CFG_PATH, encoding='utf-8')) if os.path.exists(CFG_PATH) else {}
code, r = post('/api/config', {'size': 999999, 'feather': -50})
on_disk = json.load(open(CFG_PATH, encoding='utf-8'))
check('超范围的值被夹到上限', on_disk['size'] == 1200, on_disk['size'])
check('负值被夹到下限', on_disk['feather'] == 0, on_disk['feather'])
check('真的写进了 wallpaper-config.json（不是只改了内存）',
      on_disk.get('size') == 1200 and before.get('size') != 1200,
      '盘上 size=%r' % on_disk.get('size'))
check('夹取有说明回给界面', len(r.get('notes') or []) >= 2, r.get('notes'))

code, r = post('/api/config', {'size': 444, 'glow': 33})
on_disk = json.load(open(CFG_PATH, encoding='utf-8'))
check('正常值原样落盘', on_disk['size'] == 444 and on_disk['glow'] == 33,
      (on_disk['size'], on_disk['glow']))
check('只发部分键时其他键不丢',
      set(before.keys()) <= set(on_disk.keys()), sorted(on_disk.keys()))

r = post('/api/config', {'fps': 60, 'follow': 0.45})[1]
check('字符串数字也能接受', r['config']['fps'] == 60, r['config']['fps'])
r = post('/api/config', {'size': 'abc'})[1]
check('非数字被拒绝但不写坏文件',
      any('不是数字' in n for n in (r.get('notes') or [])), r.get('notes'))

# ================================================================ 5. rev 通道
print('\n【5】图片重载通道（rev）')
rev0 = P.cfg_read().get('rev', 0)
r = post('/api/reload-images')[1]
rev1 = P.cfg_read().get('rev', 0)
check('rev 递增（路径不变也能触发重读）', rev1 == rev0 + 1, '%r → %r' % (rev0, rev1))
check('路径没被改动', json.load(open(CFG_PATH, encoding='utf-8'))['outer']
      == before['outer'])
before_outer = before['outer']
post('/api/config', {'outer': before_outer})
check('重新写回同一个路径后 rev 不倒退', P.cfg_read()['rev'] >= rev1)

# ================================================================ 6. 其他
print('\n【6】安全与容错')
code, r = post('/api/config', {'size': 300},
               headers={'Origin': 'https://evil.example.com'}, expect_error=True)
check('带外站 Origin 的 POST 被拒', code == 403, code)
code, r = post('/api/nothing', expect_error=True)
check('不存在的接口返回 404', code == 404, code)
log = urllib.request.urlopen(BASE_URL + '/api/log?n=20', timeout=10).read()
check('/api/log 有内容且是 UTF-8 文本', len(log) > 0, len(log))
check('日志里能看到刚才的操作记录',
      b'rev' in log or b'write' in log or b'\xe5\x86\x99\xe9\x85\x8d\xe7\xbd\xae' in log,
      log[-120:])

r = post('/api/action', {'action': 'reset'})[1]
d = json.load(open(CFG_PATH, encoding='utf-8'))
check('恢复默认：效果回到默认值', d['size'] == P.CORE.DEFAULT_CFG['size'], d['size'])
check('恢复默认：图片路径保留不动', d['outer'] == before_outer, d['outer'])

# ================================================================ 7. 并发
print('\n【7】并发写配置（ThreadingHTTPServer + 同一把锁）')
#
# 面板是 ThreadingHTTPServer，两个 POST 完全可能同时在跑。没串行化之前：
#   ① 两个线程同时 open('wallpaper-config.json.tmp') → Windows 上前一个已经
#      持有写句柄，后一个直接 PermissionError → 界面收到 500；表现成
#      "拖一下滑块没生效 / 键盘连按没反应"，而日志里只有孤零零一条报错。
#   ② 两个"读-改-写"交错 → 后写的把先写的那次改动吞掉，一个字都不报。
# 两条都是偶发的，用手点很难复现出来，所以在这里把并发直接拉满。
import concurrent.futures as _cf                                 # noqa: E402


def fire(bodies):
    with _cf.ThreadPoolExecutor(max_workers=len(bodies)) as ex:
        return list(ex.map(lambda b: post('/api/config', b), bodies))


codes = [c for c, _ in fire([{'size': 120 + i * 7} for i in range(16)])]
check('16 个并发写全部 200（不会因为抢 .tmp 而 500）',
      all(c == 200 for c in codes), '状态码：%s' % sorted(set(codes)))

lost, snap = 0, None
for rnd in range(8):
    f, g = 11 + rnd, 21 + rnd
    fire([{'feather': f}, {'glow': g}])
    snap = json.load(open(CFG_PATH, encoding='utf-8'))
    if snap.get('feather') != f or snap.get('glow') != g:
        lost += 1
check('并发读-改-写不会互相吞掉（8 轮两两并发）', lost == 0,
      '丢了 %d 轮，最后落盘 feather=%s glow=%s'
      % (lost, snap.get('feather'), snap.get('glow')))

# ================================================================ 8. 托盘
# 这一节的处境正好是"托盘没启用"（测试用的服务是裸 Handler，TRAY 恒为 None），
# 而这恰恰是最要紧的那条安全线：托盘图标没装上，就绝不许把窗口藏起来。
# 图标是唯一能把窗口捞回来的入口；入口不存在还藏窗口 = 用户再也开不回面板，
# 只能去任务管理器杀进程。这个项目在"窗口盖住全屏又没出口"上栽过一次，
# 所以这条拒绝必须由测试守着，不能只靠代码里那句 if。
#
# 完整的托盘链路（图标装上、最小化收起、点图标回来、干净退出）在
# `_probe_tray.py` 里跑 —— 那些全靠 Win32 消息，接口层验不了。
print('\n【8】托盘接口')
tr = (get('/api/state') or {}).get('tray') or {}
check('/api/state 带 tray 字段', 'active' in tr, tr)
check('没启用托盘时 active=false', tr.get('active') is False, tr.get('active'))
check('NOTIFYICONDATAW 尺寸 = 976（填错的后果是"图标在、气泡不弹"）',
      tr.get('nid_size') == 976, tr.get('nid_size'))

_, j = post('/api/action', {'action': 'to-tray'})
check('没装托盘时 to-tray 必须拒绝（ok=false）', j.get('ok') is False, j.get('note'))
check('拒绝的理由说得清楚（不是干巴巴一句失败）',
      '托盘' in (j.get('note') or ''), j.get('note'))
_, j = post('/api/action', {'action': 'from-tray'})
# 没托盘时这个方向也拒绝 —— 此时"收进托盘"整个功能就是不可用的，与其假装
# 成功（用户看不出区别），不如把话说清楚。要守的是"别炸成 500"。
check('from-tray 不炸，并明确说明托盘没启用',
      j.get('ok') is False and '托盘' in (j.get('note') or ''), j.get('note'))

# ================================================================ 收尾
restore()
httpd.shutdown()
print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
if FAIL:
    print('失败：')
    for f in FAIL:
        print('  ✗ ' + f)
print('=' * 60)
sys.exit(1 if FAIL else 0)
