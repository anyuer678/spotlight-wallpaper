# -*- coding: utf-8 -*-
"""诊断右列高度分配：日志格子该随窗口变高而长个。

查两件事：
  1. 右列 .col-right 的每个子元素：矩形、computed margin —— 上次实测
     预览 542 + 日志 340 + 间隙对不上总高 930，多出 ~24px 查不清是谁；
     怀疑是 .card { margin-bottom } 和 flex gap 双份叠加。
  2. 矮窗口(830)和高窗口(1100)下日志卡片高度必须变高（flex:1 真在长个）。

跑法：python _probe_logheight.py   （自己起 serve-only 面板，结束即杀）
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
PORT = 8797
URL = 'http://127.0.0.1:%d/' % PORT

EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]

PROBE = r"""
() => {
  const col = document.querySelector('.col-right');
  const cs = getComputedStyle(col);
  const kids = [...col.children].map(e => {
    const r = e.getBoundingClientRect();
    const m = getComputedStyle(e);
    let n = e.tagName.toLowerCase();
    if (e.id) n += '#' + e.id;
    if (typeof e.className === 'string' && e.className.trim())
      n += '.' + e.className.trim().split(/\s+/).join('.');
    return {n, top: Math.round(r.top), h: Math.round(r.height),
            mt: m.marginTop, mb: m.marginBottom};
  });
  const log = document.querySelector('#log');
  const logCard = log.closest('.card');
  const lr = log.getBoundingClientRect();
  const cr = logCard.getBoundingClientRect();
  return {colCH: col.clientHeight, colSH: col.scrollHeight, colGap: cs.gap,
          kids, logH: Math.round(lr.height), logCardH: Math.round(cr.height),
          logScrollH: log.scrollHeight, colScroll: col.scrollTop};
}
"""


def main():
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
    if not ready:
        print('面板服务起不来')
        srv.kill()
        return 1

    ok_all = True
    try:
        with sync_playwright() as p:
            exe = next((e for e in EDGE_CANDIDATES
                        if e and os.path.exists(e)), None)
            br = (p.chromium.launch(executable_path=exe, headless=True)
                  if exe else p.chromium.launch(headless=True))
            results = {}
            for vh in (830, 1100):
                pg = br.new_page(viewport={'width': 1180, 'height': vh},
                                 device_scale_factor=1)
                pg.goto(URL, wait_until='domcontentloaded')
                pg.wait_for_selector('#log .l-time, #log', timeout=15000)
                time.sleep(2.0)
                g = pg.evaluate(PROBE)
                results[vh] = g
                print()
                print('【视口高 %d】 col clientH=%d scrollH=%d gap=%s 滚动位=%d'
                      % (vh, g['colCH'], g['colSH'], g['colGap'],
                         g['colScroll']))
                for k in g['kids']:
                    print('  子元素 %-34s top=%4d h=%4d margin-top=%s margin-bottom=%s'
                          % (k['n'], k['top'], k['h'], k['mt'], k['mb']))
                print('  日志卡片高=%d  日志区高=%d（内容 scrollH=%d）'
                      % (g['logCardH'], g['logH'], g['logScrollH']))
                pg.close()

            short, tall = results[830], results[1100]
            print()
            print('【判据】')
            grew = tall['logCardH'] > short['logCardH']
            print('  %s 日志卡片随窗口长个：%d → %d'
                  % ('✓' if grew else '✗', short['logCardH'],
                     tall['logCardH']))
            ok_all &= grew
            # 双份间隙判据：右列里卡片不该再带 margin-bottom（gap 已管间距）
            mb_ok = all(k['mb'] in ('0px',) for k in tall['kids'])
            print('  %s 右列子元素 margin-bottom 全为 0（不与 gap 叠加）'
                  % ('✓' if mb_ok else '✗'))
            ok_all &= mb_ok
            # 高窗口下右列不该出现滚动（所有内容都装得下）
            fit = tall['colSH'] <= tall['colCH'] + 1
            print('  %s 高窗口下右列无内部滚动（%d <= %d）'
                  % ('✓' if fit else '✗', tall['colSH'], tall['colCH']))
            ok_all &= fit
            br.close()
    finally:
        srv.kill()

    print()
    print('结论：%s' % ('全部通过' if ok_all else '存在未过项，见上'))
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
