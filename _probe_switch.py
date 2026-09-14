# -*- coding: utf-8 -*-
"""回归判据：点开关不许让面板整体窜位。

背景（这是真发生过的 bug）：开关里藏着 <input type=checkbox>，它是绝对定位的，
而 .sw 当时没有 position:relative —— 于是它的包含块一路上升到文档，绕过
.col / .layout 的裁剪，在视口下方顶出 397px 的隐形内容。文档因此有了根滚动条，
浏览器在点击时把获得焦点的 checkbox 滚进视野，整个界面（连标题栏）整体上窜。
用户看到的就是"点一下开关界面就变形"，而布局其实一个像素都没重排。

所以判据分两层：
    1. 页面不许有根级溢出：html.scrollHeight 必须等于 clientHeight；
    2. 点每一个开关，window.scrollY / html.scrollTop 必须纹丝不动。

跑法：python _probe_switch.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'wallpaper-config.json')
PIDF = os.path.join(BASE, 'wallpaper.pid')
WALL = os.path.join(BASE, 'wallpaper.pyw')
PORT = 8795
URL = 'http://127.0.0.1:%d/' % PORT

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s%s' % ('✓' if ok else '✗', name,
                         ('   ← ' + str(detail)) if (detail and not ok) else ''))


EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    os.path.join(os.environ.get('LOCALAPPDATA') or '',
                 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
]

# 根级溢出的元凶会以"某个元素伸出视口"的形式露出来，一并记下来方便定位
GEO = r"""
() => {
  const de = document.documentElement;
  const H = innerHeight, W = innerWidth;
  const deepest = [];
  document.querySelectorAll('*').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.bottom > H + 1 || r.right > W + 1) {
      let s = e.tagName.toLowerCase();
      if (e.id) s += '#' + e.id;
      if (typeof e.className === 'string' && e.className.trim())
        s += '.' + e.className.trim().split(/\s+/).join('.');
      deepest.push({n: s, bottom: Math.round(r.bottom),
                    right: Math.round(r.right)});
    }
  });
  deepest.sort((a, b) => b.bottom - a.bottom);
  const scrollers = {};
  for (const sel of ['html', 'body', '.col', '#log', '#sliders'])
    document.querySelectorAll(sel).forEach((e, i) => {
      scrollers[sel + '#' + i] = Math.round(e.scrollTop);
    });
  return {
    vw: W, vh: H,
    docSH: de.scrollHeight, docCH: de.clientHeight,
    docSW: de.scrollWidth, docCW: de.clientWidth,
    scrollY: Math.round(window.scrollY),
    htmlST: Math.round(de.scrollTop),
    scrollers: scrollers,
    deepest: deepest.slice(0, 6),
  };
}
"""


def read_pid():
    try:
        return int(open(PIDF).read().strip())
    except Exception:
        return 0


def alive(pid):
    if not pid:
        return False
    out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                         capture_output=True, text=True)
    return 'python' in (out.stdout or '').lower()


def main():
    print('【0】准备')
    srv = subprocess.Popen([sys.executable, os.path.join(BASE, 'panel.pyw'),
                            '--serve-only', '--port', str(PORT)],
                           cwd=BASE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    ready = False
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL + 'api/ping', timeout=2) as r:
                if json.loads(r.read()).get('ok'):
                    ready = True
                    break
        except Exception:
            time.sleep(0.3)
    print('  面板服务：%s' % ('已起' if ready else '起不来'))
    if not ready:
        srv.kill()
        return 1

    if not alive(read_pid()):
        exe = sys.executable.replace('python.exe', 'pythonw.exe')
        subprocess.Popen([exe, WALL], cwd=BASE,
                         creationflags=0x08000000 | 0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(6)
    print('  壁纸：pid %s' % read_pid())

    backup = open(CFG, encoding='utf-8').read() if os.path.exists(CFG) else None
    errors = []
    try:
        with sync_playwright() as p:
            exe = next((e for e in EDGE_CANDIDATES if e and os.path.exists(e)),
                       None)
            br = (p.chromium.launch(executable_path=exe, headless=True)
                  if exe else p.chromium.launch(headless=True))
            pg = br.new_page(viewport={'width': 1180, 'height': 830},
                             device_scale_factor=1)
            pg.on('console', lambda m: errors.append('console.%s: %s'
                                                      % (m.type, m.text))
                  if m.type == 'error' else None)
            pg.on('pageerror', lambda e: errors.append('pageerror: %s' % e))
            pg.goto(URL, wait_until='domcontentloaded')
            pg.wait_for_selector('#switches .sw', timeout=15000)
            time.sleep(2.5)

            print()
            print('【1】根级溢出（面板必须正好一屏，不该有根滚动条）')
            g = pg.evaluate(GEO)
            over = g['docSH'] - g['docCH']
            print('  视口 %dx%d | 文档 %dx%d' % (g['vw'], g['vh'],
                                                g['docSW'], g['docSH']))
            check('文档高度 == 视口高度（无隐形溢出）', over <= 1,
                  '多出 %dpx；伸出视口最远的元素：%s'
                  % (over, g['deepest']))
            # 注意：列里面被 overflow 裁掉的那些卡片，getBoundingClientRect
            # 照样会给出视口外的坐标 —— 那是正常的（它们是被裁掉的，不参与
            # 文档级溢出）。所以这条只当参考信息看，真正的判据是上面那条。
            if g['deepest']:
                print('  参考：伸出视口最远的元素（列内被裁剪，正常）: %s'
                      % g['deepest'][0])

            print()
            print('【2】逐个点开关，看界面会不会窜位')
            n = pg.locator('#switches .sw').count()
            for i in range(n):
                sw = pg.locator('#switches .sw').nth(i)
                name = sw.inner_text().replace('\n', ' / ').split(' / ')[0]
                # 先自己把它滚进视野，免得把"测试框架的滚动"算到应用头上
                sw.scroll_into_view_if_needed()
                pg.wait_for_timeout(500)
                pg.evaluate("() => { window.scrollTo(0, 0); "
                            "document.documentElement.scrollTop = 0; }")
                pg.wait_for_timeout(250)
                a = pg.evaluate(GEO)
                before = 'on' in (sw.get_attribute('class') or '')
                sw.click()
                pg.wait_for_timeout(2200)
                b = pg.evaluate(GEO)
                cur = 'on' in (sw.get_attribute('class') or '')
                after = (cur != before)

                check('「%s」开关真的切换了' % name, after)
                check('「%s」点它不动根滚动（scrollY 0→0）' % name,
                      b['scrollY'] == 0 and b['htmlST'] == 0,
                      'scrollY %d→%d，html.scrollTop %d→%d'
                      % (a['scrollY'], b['scrollY'], a['htmlST'], b['htmlST']))
                moved = {k: (a['scrollers'].get(k), v)
                         for k, v in b['scrollers'].items()
                         if a['scrollers'].get(k) != v}
                check('「%s」点它不滚动任何容器' % name, not moved, str(moved))

            # 明着戳一下：直接 focus 那个隐藏 checkbox，看根会不会跟着动
            pg.evaluate("() => { window.scrollTo(0, 0); "
                        "document.documentElement.scrollTop = 0; }")
            pg.wait_for_timeout(250)
            pg.evaluate("() => document.querySelector('#switches input').focus()")
            pg.wait_for_timeout(700)
            c = pg.evaluate(GEO)
            check('把隐藏 checkbox 强行 focus 也不动根滚动',
                  c['scrollY'] == 0 and c['htmlST'] == 0,
                  'scrollY=%d html.scrollTop=%d' % (c['scrollY'], c['htmlST']))

            pg.screenshot(path=os.path.join(BASE, '_switch_after.png'),
                          full_page=False)
            br.close()
    finally:
        if backup is not None:
            with open(CFG, 'w', encoding='utf-8') as f:
                f.write(backup)
            print()
            print('  配置已还原')
        srv.kill()

    print()
    print('【3】JS 报错')
    if errors:
        for e in errors[:8]:
            print('  ✗ %s' % e)
    else:
        print('  ✓ 无')

    print()
    print('=' * 56)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    for f in FAIL:
        print('  ✗ %s' % f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
