# -*- coding: utf-8 -*-
"""用真浏览器驱动面板界面。

覆盖三件靠肉眼看截图发现不了的事：
  1. 有没有 JS 报错。界面上一片安静、某个按钮就是没反应，最常见的原因
     就是回调里抛了异常而用户看不到控制台。
  2. 预览画布真的画上了东西。空画布和"画了但画面恰好很暗"在截图里可能
     长得一样，所以直接读回 canvas 的像素统计，还要求光斑正中与远处角落
     不是同一个像素（那才说明内图确实透出来了）。
  3. 拖 / 键盘 / 直接输入 三条改值路径都通，而且都真的落到了配置文件上。

顺便产出两张截图（暗色 / 浅色）留档。

跑法：python _test_panel_ui.py
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
PORT = 8791
URL = 'http://127.0.0.1:%d/' % PORT
SHOT_DARK = os.path.join(BASE, '_ui_dark.png')
SHOT_LIGHT = os.path.join(BASE, '_ui_light.png')
SHOT_LEFT = os.path.join(BASE, '_ui_left.png')

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s%s' % ('✓' if ok else '✗', name,
                         ('   ← ' + str(detail)) if (detail and not ok) else ''))


def port_busy(port):
    """端口上已经有人在监听吗。

    端口是写死的一个值。万一上次跑崩留下个 --serve-only 实例，新进程 bind 会
    直接失败退出，而下面的 ready 轮询会被旧实例应答 —— 于是整套测试跑在
    旧代码上，还一点异常都看不出来。宁可当场报错，也不要静默测错对象。
    """
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.4)
        return s.connect_ex(('127.0.0.1', port)) == 0
    finally:
        s.close()


def cfg_now():
    try:
        return json.load(open(CFG, encoding='utf-8'))
    except Exception:
        return {}


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


def kill(pid):
    if pid:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        try:
            os.remove(PIDF)
        except OSError:
            pass


BACKUP = open(CFG, encoding='utf-8').read() if os.path.exists(CFG) else None
pre_pid = read_pid()
we_started = False

#  不要用 channel='msedge'：Playwright 靠 ProgramFiles 环境变量推路径，而这个
#   shell 环境里那个变量是空的，它会去找 "undefined\Program Files\Microsoft\...".
#   直接给可执行文件路径，绕开那套推断。
EDGE_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    os.path.join(os.environ.get('LOCALAPPDATA') or '',
                 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
]


def launch_browser(p):
    for exe in EDGE_CANDIDATES:
        if exe and os.path.exists(exe):
            print('  用浏览器：%s' % exe)
            return p.chromium.launch(executable_path=exe, headless=True)
    print('  没找到 Edge，退回 Playwright 自带的 Chromium')
    return p.chromium.launch(headless=True)


def set_val(pg, key, value):
    """通过"点数值 → 输入 → 回车"精确设一个值。
    走这条路而不是拖滑块，是为了让测试的起点是确定的数字。
    """
    pg.locator('.val[data-k="%s"]' % key).click()
    pg.keyboard.press('Control+a')
    pg.keyboard.type(str(value))
    pg.keyboard.press('Enter')
    time.sleep(0.9)


print('【0】准备：面板服务 + 壁纸主程序')
if port_busy(PORT):
    print('  端口 %d 已被占用（上次的残留实例？）→ 拒绝继续，'
          '否则整套测试会跑在旧进程上' % PORT)
    sys.exit(1)
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
check('面板服务起来了', ready)
if not ready:
    srv.kill()
    sys.exit(1)

wall_pid = pre_pid
if alive(pre_pid):
    print('  壁纸已经在跑（pid %d），拿它测' % pre_pid)
else:
    exe = sys.executable.replace('python.exe', 'pythonw.exe')
    if not os.path.exists(exe):
        exe = sys.executable
    subprocess.Popen([exe, WALL], cwd=BASE,
                     creationflags=0x08000000 | 0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    we_started = True
    deadline = time.time() + 25
    while time.time() < deadline:
        wall_pid = read_pid()
        if wall_pid and alive(wall_pid):
            break
        time.sleep(0.4)
    print('  壁纸已拉起 pid=%r' % wall_pid)
check('壁纸在跑（状态区才有东西可验）', bool(wall_pid) and alive(wall_pid),
      'pid=%r' % wall_pid)

errors = []
shots = []
try:
    with sync_playwright() as p:
        browser = launch_browser(p)
        pg = browser.new_page(viewport={'width': 1180, 'height': 830})
        pg.on('console', lambda m: errors.append('console:' + m.text)
              if m.type == 'error' else None)
        pg.on('pageerror', lambda e: errors.append('pageerror:' + str(e)))

        print('\n【1】页面与骨架')
        pg.goto(URL, wait_until='load')
        check('标题 = 聚光壁纸 · 控制面板（FindWindow 找窗口靠它）',
              pg.title() == '聚光壁纸 · 控制面板', pg.title())
        for sel, want, name in (
                ('.slider', 5, '滑块'),
                ('.sw', 3, '开关'),
                ('.imgrow', 2, '图片行'),
                ('#seg-theme button', 4, '主题'),
                ('#seg-anchor button', 3, '取景位置'),
                ('#seg-zoom button', 2, '缩放档'),
        ):
            n = pg.locator(sel).count()
            check('%s 有 %d 个' % (name, want), n == want, '实际 %d 个' % n)
        check('长内容不横向溢出（旧面板是固定尺寸、没滚动）',
              pg.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2'),
              pg.evaluate('document.documentElement.scrollWidth + " > " + window.innerWidth'))
        check('回到首页没有横向滚动条（.col 自己滚）',
              pg.evaluate('document.body.scrollWidth <= window.innerWidth + 2'))
        # 纵向同理，而且这条踩过雷：开关里那个绝对定位的隐藏 checkbox 曾因为
        # .sw 没有定位祖先而逃出所有裁剪，在视口下方顶出 397px 隐形内容，
        # 于是文档凭空多出一条根滚动条 —— 点开关时浏览器把获得焦点的 checkbox
        # 滚进视野，整个界面连标题栏一起上窜。布局一个像素都没重排，所以只量
        # 元素尺寸是查不出来的，必须量文档高度。
        check('文档高度 == 视口高度（没有根级纵向溢出）',
              pg.evaluate('document.documentElement.scrollHeight'
                          ' <= window.innerHeight + 2'),
              pg.evaluate('document.documentElement.scrollHeight + " > "'
                          ' + window.innerHeight'))

        print('\n【2】状态区如实反映壁纸状态')
        pg.wait_for_function(
            "() => !document.querySelector('#st-run .t').textContent.includes('检查中')",
            timeout=15000)
        run_txt = pg.inner_text('#st-run .t')
        at_txt = pg.inner_text('#st-attach .t')
        cpu_txt = pg.inner_text('#st-cpu .t')
        check('运行状态显示"运行中 · pid N"',
              '运行中' in run_txt and str(wall_pid) in run_txt, run_txt)
        check('挂载状态显示"已挂到桌面层"（枚举 Progman 子窗口得到的，不是猜的）',
              '桌面层' in at_txt and '未' not in at_txt, at_txt)
        check('CPU 有读数或明确说在采样', 'CPU' in cpu_txt, cpu_txt)
        check('按钮文字与运行状态一致（此时应为"停止壁纸"）',
              pg.inner_text('#btn-run') == '停止壁纸', pg.inner_text('#btn-run'))
        check('离线遮罩没被误触发', pg.locator('#offline').is_hidden())

        # 托盘按钮：这一轮面板是 `--serve-only` 起的，托盘图标装不上，
        # 所以按钮必须是停用状态。这不是"少了个功能"，是安全线：
        # 托盘图标是唯一能把面板捞回来的入口，装不上还让人收进去就等于
        # 把面板锁死在一个点不开的图标后面。
        tray_btn = pg.locator('#btn-tray')
        check('顶栏有「最小化到托盘」按钮', tray_btn.count() == 1)
        check('托盘不可用时按钮是停用的（不许把人关在外面）',
              tray_btn.is_disabled(), tray_btn.get_attribute('title'))
        check('停用原因写在 title 里（鼠标悬停能看到为什么）',
              '托盘' in (tray_btn.get_attribute('title') or ''),
              tray_btn.get_attribute('title'))

        print('\n【3】预览画布真的画上了东西')
        pg.wait_for_function(
            "() => document.getElementById('pv-ms').textContent !== '—'", timeout=30000)
        stat = pg.evaluate("""() => {
            const c = document.getElementById('pv');
            const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
            const uniq = new Set();
            for (let i = 0; i < d.length; i += 4 * 53)
                uniq.add(d[i] + ',' + d[i+1] + ',' + d[i+2]);
            const px = (x,y) => { const o = (y*c.width+x)*4;
                return [d[o],d[o+1],d[o+2]].join(','); };
            return {n: uniq.size, center: px(240,156), far: px(4,4)};
        }""")
        check('画布不是空白（%d 种颜色）' % stat['n'], stat['n'] > 200, stat)
        check('光斑正中与远处角落不是同一个像素（内图确实透出来了）',
              stat['center'] != stat['far'], stat)
        check('缩放标记有内容', len(pg.inner_text('#pv-zoom')) > 2,
              pg.inner_text('#pv-zoom'))
        pg.screenshot(path=SHOT_DARK)
        shots.append(SHOT_DARK)

        print('\n【4】三条改值路径都要真的落盘')
        pg.wait_for_function(
            "() => document.querySelector('.val[data-k=\"size\"]').textContent.length > 1",
            timeout=10000)

        # ① 拖动
        before = cfg_now().get('size')
        b = pg.locator('.slider[data-k="size"]').bounding_box()
        pg.mouse.move(b['x'] + b['width'] * 0.5, b['y'] + b['height'] / 2)
        pg.mouse.down()
        pg.mouse.move(b['x'] + b['width'] * 0.74, b['y'] + b['height'] / 2, steps=10)
        pg.mouse.up()
        time.sleep(1.2)
        badge = pg.inner_text('.val[data-k="size"]')
        dragged = cfg_now().get('size')
        check('拖动滑块：真的写进了配置文件',
              isinstance(dragged, int) and abs(dragged - before) > 50,
              '%r → %r' % (before, dragged))
        check('徽标数字与配置一致', str(dragged) in badge.replace(' ', ''),
              '%s vs %s' % (badge, dragged))
        check('拖动时预览跟着刷新', pg.inner_text('#pv-ms') not in ('—', ''),
              pg.inner_text('#pv-ms'))

        # ② 键盘微调（先把起点设成中间值，免得撞上下限）
        set_val(pg, 'feather', 60)
        check('点数值直接输入：生效', cfg_now().get('feather') == 60,
              cfg_now().get('feather'))
        pg.locator('.slider[data-k="feather"]').focus()
        pg.keyboard.press('ArrowRight')
        pg.keyboard.press('ArrowRight')
        time.sleep(0.9)
        check('方向键微调：每次 +1', cfg_now().get('feather') == 62,
              cfg_now().get('feather'))
        pg.keyboard.press('Shift+ArrowLeft')
        time.sleep(0.9)
        check('Shift+方向键：一次 -10', cfg_now().get('feather') == 52,
              cfg_now().get('feather'))
        set_val(pg, 'feather', 5)
        pg.locator('.slider[data-k="feather"]').focus()
        pg.keyboard.press('Shift+ArrowLeft')
        time.sleep(0.9)
        check('触底时被夹在下限，不会变成负数', cfg_now().get('feather') == 0,
              cfg_now().get('feather'))

        # ③ 输入非法值。基准取"当前值"而不是默认值 —— 前面的步骤可能已经改过
        #    它，硬写 25 只会测出一个假失败。
        g0 = cfg_now().get('glow')
        pg.locator('.val[data-k="glow"]').click()
        pg.keyboard.press('Control+a')
        pg.keyboard.type('abc')
        pg.keyboard.press('Enter')
        time.sleep(0.9)
        check('输入非数字：值保持不变（不会被写坏）', cfg_now().get('glow') == g0,
              '%r → %r' % (g0, cfg_now().get('glow')))
        check('输入非数字：提示说明了原因',
              pg.locator('.toast', has_text='只接受数字').count() > 0,
              pg.locator('.toast').all_inner_texts())

        print('\n【5】双击复位与整卡复位')
        pg.locator('.slider[data-k="size"]').dblclick()
        time.sleep(1.0)
        check('双击滑块把手：该项回到默认值',
              cfg_now().get('size') == 380, cfg_now().get('size'))
        pg.click('#btn-reset')
        time.sleep(1.5)
        d = cfg_now()
        check('「恢复默认效果」把参数都复位',
              d.get('size') == 380 and d.get('feather') == 45
              and d.get('glow') == 25 and d.get('fps') == 60, d)
        check('「恢复默认效果」不动图片路径',
              d.get('outer') == (json.loads(BACKUP).get('outer') if BACKUP else None),
              d.get('outer'))

        print('\n【6】主题与预览交互')
        pg.click('#seg-theme button[data-k="light"]')
        time.sleep(0.4)
        check('切浅色主题：<html data-theme> 跟着变',
              pg.get_attribute('html', 'data-theme') == 'light',
              pg.get_attribute('html', 'data-theme'))
        check('浅色主题下正文真的变深色（变量生效了）',
              pg.evaluate('getComputedStyle(document.body).color') == 'rgb(28, 32, 39)',
              pg.evaluate('getComputedStyle(document.body).color'))
        pg.screenshot(path=SHOT_LIGHT)
        shots.append(SHOT_LIGHT)
        pg.click('#seg-theme button[data-k="violet"]')
        time.sleep(0.4)
        check('切暗紫主题也算数',
              pg.get_attribute('html', 'data-theme') == 'violet',
              pg.get_attribute('html', 'data-theme'))
        check('换主题后选择被记住了（localStorage）',
              pg.evaluate("localStorage.getItem('panel-theme')") == 'violet')
        pg.click('#seg-theme button[data-k="night"]')
        time.sleep(0.3)
        # 左列往下滚，把"开关"和"操作"两张卡也留一张档
        pg.evaluate("document.querySelector('.col-left').scrollTop = 9999")
        time.sleep(0.6)
        pg.screenshot(path=SHOT_LEFT)
        shots.append(SHOT_LEFT)
        # 开关的标题与说明必须是两行 —— 它们都是 <span>，忘了改 display
        # 就会糊成一行，而截图上只是"文案有点长"，很容易看漏
        sw_lines = pg.evaluate("""() => {
            const rows = [...document.querySelectorAll('.sw')];
            return rows.map(r => {
                const t = r.querySelector('.t'), d = r.querySelector('.d');
                return [t.getBoundingClientRect(), d.getBoundingClientRect()]
                       .map(b => Math.round(b.top)).join('|');
            });
        }""")
        check('开关的标题与说明分行显示（不糊成一行）',
              all(a != b for a, b in (s.split('|') for s in sw_lines)),
              sw_lines)

        # 点开关不许让界面窜位：那个隐藏 checkbox 一旦逃出裁剪，浏览器就会把
        # "获得焦点的元素滚进视野"，整个界面跟着整体上窜（真发生过，397px）。
        pg.evaluate('window.scrollTo(0, 0)')
        time.sleep(0.3)
        sw0 = pg.locator('.sw').first
        on_before = 'on' in (sw0.get_attribute('class') or '')
        sw0.click()
        time.sleep(1.6)
        on_after = 'on' in (sw0.get_attribute('class') or '')
        sy = pg.evaluate('window.scrollY')
        st = pg.evaluate('document.documentElement.scrollTop')
        check('点开关真的切换了状态', on_before != on_after,
              '%s → %s' % (on_before, on_after))
        check('点开关不会让整个界面窜位（根滚动纹丝不动）',
              sy == 0 and st == 0,
              'scrollY=%d html.scrollTop=%d' % (sy, st))
        check('点开关后文档高度仍然 == 视口高度',
              pg.evaluate('document.documentElement.scrollHeight'
                          ' <= window.innerHeight + 2'))
        sw0.click()                     # 点回原状，别影响后面的用例
        time.sleep(1.2)
        pg.evaluate("document.querySelector('.col-left').scrollTop = 0")
        time.sleep(0.3)

        pg.click('#seg-anchor button[data-k="topleft"]')
        time.sleep(0.6)
        check('切换取景位置后预览仍正常刷新',
              pg.inner_text('#pv-ms') not in ('—', ''), pg.inner_text('#pv-ms'))
        pg.click('#seg-zoom button[data-k="one"]')
        time.sleep(0.6)
        check('切到 1:1 后标记说明变化',
              '实际大小' in pg.inner_text('#pv-zoom'), pg.inner_text('#pv-zoom'))
        pg.click('#seg-zoom button[data-k="auto"]')
        pg.click('#seg-anchor button[data-k="center"]')
        time.sleep(0.5)

        cb = pg.locator('#pv').bounding_box()
        pg.mouse.move(cb['x'] + cb['width'] * 0.3, cb['y'] + cb['height'] * 0.3)
        pg.mouse.down()
        pg.mouse.move(cb['x'] + cb['width'] * 0.45, cb['y'] + cb['height'] * 0.6,
                      steps=6)
        pg.mouse.up()
        time.sleep(0.9)
        check('在预览上拖光斑：坐标读数跟着变',
              '光斑' in pg.inner_text('#pv-pos'), pg.inner_text('#pv-pos'))

        print('\n【7】窗口缩放')
        # 断点写在 CSS 里是 max-width:900px，所以 940 仍然是两列、880 才是单列。
        # 断言要跟着断点走，不能凭感觉写"窄了就该是一列"。
        for w, want_cols in ((1180, 2), (940, 2), (880, 1), (700, 1)):
            pg.set_viewport_size({'width': w, 'height': 700})
            time.sleep(0.6)
            no_overflow = pg.evaluate(
                'document.documentElement.scrollWidth <= window.innerWidth + 2')
            cols = pg.evaluate(
                "getComputedStyle(document.querySelector('.layout'))"
                ".gridTemplateColumns.trim().split(/\\s+/).length")
            check('宽 %d：不横向溢出（旧面板 resizable(False,False) 且没滚动）' % w,
                  no_overflow,
                  pg.evaluate('document.documentElement.scrollWidth + " > " + window.innerWidth'))
            check('宽 %d：%d 列布局' % (w, want_cols), cols == want_cols,
                  '实际 %d 列' % cols)
        pg.set_viewport_size({'width': 1180, 'height': 830})
        time.sleep(0.4)

        print('\n【8】开始 / 停止按钮')
        pg.click('#btn-run')                       # 当前在跑 → 停止
        deadline = time.time() + 15
        while time.time() < deadline and alive(read_pid()):
            time.sleep(0.5)
        check('点「停止壁纸」确实把进程停了', not alive(read_pid()),
              'pid=%r' % read_pid())
        time.sleep(1.5)
        check('状态区跟着变成"未运行"', '未运行' in pg.inner_text('#st-run .t'),
              pg.inner_text('#st-run .t'))
        check('按钮文字跟着变成"启动壁纸"',
              pg.inner_text('#btn-run') == '启动壁纸', pg.inner_text('#btn-run'))
        pg.click('#btn-run')                       # 再启动
        deadline = time.time() + 25
        while time.time() < deadline and not alive(read_pid()):
            time.sleep(0.5)
        check('点「启动壁纸」确实把进程拉起来了', alive(read_pid()),
              'pid=%r' % read_pid())

        print('\n【9】日志区')
        pg.click('#btn-logrefresh')
        time.sleep(1.2)
        logtxt = pg.inner_text('#log')
        check('日志区有内容', len(logtxt) > 40, len(logtxt))
        check('日志带时间戳（说明读的是真日志，不是占位）',
              '[' in logtxt and ']' in logtxt, logtxt[:80])
        check('关键行被着色（挂载/启动完成标绿）',
              pg.locator('#log .l-ok').count() > 0,
              pg.locator('#log .l-ok').count())

        print('\n【10】JS 报错必须为零')
        check('没有 console 错误 / 未捕获异常', len(errors) == 0, errors[:6])

        browser.close()
finally:
    srv.kill()
    try:
        if BACKUP is not None:
            with open(CFG, 'w', encoding='utf-8') as f:
                f.write(BACKUP)
    except Exception:
        pass
    if we_started:
        kill(read_pid())
        print('  已停掉本次由测试启动的壁纸')

print('\n截图：%s' % ', '.join(os.path.basename(s) for s in shots))
print('=' * 60)
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('  ✗ ' + f)
print('=' * 60)
sys.exit(1 if FAIL else 0)
