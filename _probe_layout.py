# -*- coding: utf-8 -*-
"""给面板界面做"变形"体检。

测三件事：
  A. 布局会不会被挤坏 —— 各档窗口宽度下有没有横向溢出。
  B. 前端发出去的取景框会不会越界 —— 一旦顶出屏幕，后端就只能把它夹回来，
     夹完比例就变了。
  C. 后端真正用的取景框比例对不对 —— 直接读响应头 `X-Box`，把它的宽高比和
     输出尺寸 vw:vh 比一比。不等就是硬缩 → 画面被拉扁/拉长 = 变形。
     （不自己再实现一遍后端的夹取逻辑：那样测的是我的复述，不是它的行为。）

顺带按 dpr=1.0 / 1.5 各跑一遍 —— 本机是 150% 缩放，必须按真实 dpr 看。

跑法：python _probe_layout.py
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
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'wallpaper-config.json')
PORT = 8793
URL = 'http://127.0.0.1:%d/' % PORT

EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''),
                 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
]

SIZES = [
    (1180, 830, '默认窗口', 'w1180'),
    (1500, 980, '拉大', 'w1500'),
    (1000, 700, '缩小', 'w1000'),
    (760, 1000, '单列窄', 'w760'),
]

# 走的就是前端真实那条路：同一个 boxOf()、同一组参数，只是顺手把响应头读回来。
PROBE = r"""
async () => {
  const sw = state.screen.w, sh = state.screen.h;
  const b = boxOf();
  const s = spotScreen();
  const out = {};
  document.querySelectorAll('#seg-zoom .seg-item').forEach(el => {
    if (el.classList.contains('on')) out.zoomLabel = el.textContent.trim();
  });
  const draw = async (tag, box) => {
    const q = new URLSearchParams({
      vw: CW, vh: CH,
      x0: box.x0, y0: box.y0, x1: box.x1, y1: box.y1,
      sx: s.x.toFixed(2), sy: s.y.toFixed(2),
      size: cfg.size, feather: cfg.feather, glow: cfg.glow,
    });
    const r = await fetch('/api/preview?' + q);
    await r.arrayBuffer();
    return { tag, sent: [box.x0, box.y0, box.x1, box.y1],
             box: r.headers.get('X-Box'), vw: CW, vh: CH };
  };
  const res = [];
  res.push(await draw('default', b));
  // 再单独逼一次"故意越界"的请求，验证后端的兜底
  res.push(await draw('故意越界', { x0: -50, y0: -50, x1: sw + 50, y1: sh + 50 }));
  const de = document.documentElement;
  return { screen: [sw, sh], results: res,
           docScrollW: de.scrollWidth, docClientW: de.clientWidth,
           canvasAspect: CW / CH, dpr: window.devicePixelRatio };
}
"""


def wait_ready():
    for _ in range(60):
        try:
            urllib.request.urlopen(URL + 'api/ping', timeout=1).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    cfg_backup = json.load(open(CFG, encoding='utf-8')) if os.path.exists(CFG) else {}
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    srv = subprocess.Popen([sys.executable, os.path.join(BASE, 'panel.pyw'),
                            '--serve-only', '--port', str(PORT)],
                           cwd=BASE, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        if not wait_ready():
            print('后端没起来'); return 1

        with sync_playwright() as p:
            br = None
            for exe in EDGE_CANDIDATES:
                if os.path.exists(exe):
                    br = p.chromium.launch(executable_path=exe, headless=True)
                    break
            if br is None:
                br = p.chromium.launch(headless=True)

            for dpr in (1.0, 1.5):
                print('\n############ device_scale_factor = %.1f ############' % dpr)
                for label, size, anchors in (
                        ('常规 size=381', 381, ('center', 'topleft', 'bottomright')),
                        ('极限 size=1200', 1200, ('center', 'topleft', 'bottomright')),
                        ('极小 size=60', 60, ('center',))):
                    data = json.load(open(CFG, encoding='utf-8'))
                    data['size'] = size
                    json.dump(data, open(CFG, 'w', encoding='utf-8'),
                              ensure_ascii=False, indent=2)
                    time.sleep(0.3)
                    print('\n=== %s ===' % label)

                    for w, h, tag, slug in SIZES:
                        pg = br.new_page(viewport={'width': w, 'height': h},
                                         device_scale_factor=dpr)
                        pg.goto(URL, wait_until='networkidle')
                        pg.wait_for_timeout(800)
                        for zk in ('auto', 'one'):
                            pg.click('#seg-zoom button[data-k="%s"]' % zk)
                            pg.wait_for_timeout(350)
                            for anc in anchors:
                                pg.click('#seg-anchor button[data-k="%s"]' % anc)
                                pg.wait_for_timeout(450)
                                r = pg.evaluate(PROBE)
                                sw, sh = r['screen']
                                for item in r['results']:
                                    x0, y0, x1, y1 = [float(v) for v in item['box'].split(',')]
                                    bar = (x1 - x0) / (y1 - y0)
                                    oar = item['vw'] / float(item['vh'])
                                    dev = abs(bar - oar) / oar
                                    over = (x0 < -1 or y0 < -1
                                            or x1 > sw + 1 or y1 > sh + 1)
                                    s0, sy0, s1, sy1 = [float(v) for v in item['sent']]
                                    asked_over = (s0 < -0.5 or sy0 < -0.5
                                                  or s1 > sw + 0.5 or sy1 > sh + 0.5)
                                    if item['tag'] == '故意越界':
                                        # 这条就是故意越界的，只关心后端有没有兜住比例
                                        if dev > 0.005:
                                            problems.append(
                                                '%s dpr=%.1f %s 越界兜底失败 '
                                                '(取景框比 %.4f vs 输出比 %.4f)'
                                                % (label, dpr, tag, bar, oar))
                                        elif over:
                                            problems.append(
                                                '%s dpr=%.1f %s 兜底后仍在屏幕外'
                                                % (label, dpr, tag))
                                        continue
                                    if dev > 0.005:
                                        problems.append(
                                            '%s dpr=%.1f %s/%s/%s 后端输出比例不符 '
                                            '(取景框比 %.4f vs 输出比 %.4f)'
                                            % (label, dpr, tag, zk, anc, bar, oar))
                                    if asked_over:
                                        problems.append(
                                            '%s dpr=%.1f %s/%s/%s 前端发出的框越界 '
                                            '(%.0f,%.0f,%.0f,%.0f vs 屏幕 %dx%d)'
                                            % (label, dpr, tag, zk, anc,
                                               s0, sy0, s1, sy1, sw, sh))
                                    if over:
                                        problems.append(
                                            '%s dpr=%.1f %s/%s/%s 后端最终框还在屏幕外'
                                            % (label, dpr, tag, zk, anc))
                                    print('  %-8s %-11s %-9s %-6s | 发出 '
                                          '(%.0f,%.0f,%.0f,%.0f)%s | 实渲 %-22s '
                                          '(比 %.4f / 输出 %.4f) %s'
                                          % (tag, anc, zk, '%dx%d' % (w, h),
                                             s0, sy0, s1, sy1,
                                             '★越界' if asked_over else '',
                                             item['box'], bar, oar,
                                             '★拉伸 %.1f%%' % (dev * 100)
                                             if dev > 0.005 else 'OK'))
                        hx = r['docScrollW'] - r['docClientW']
                        if hx > 0:
                            problems.append('%s dpr=%.1f %s: 横向溢出 %+d px'
                                            % (label, dpr, tag, hx))
                        else:
                            print('  %-10s %-11s 横向溢出 %+d px'
                                  % (tag, '（布局）', hx))
                        if dpr == 1.0:
                            pg.screenshot(path=os.path.join(
                                BASE, '_probe_%s_%s.png' % (size, slug)))
                        pg.close()
            br.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        json.dump(cfg_backup, open(CFG, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        print('\n配置已还原：size=%s' % cfg_backup.get('size'))
        print('\n================ 结论 ================')
        if problems:
            for x in problems:
                print('  ✗ ' + x)
        else:
            print('  ✓ 没有发现拉伸、越界或溢出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
