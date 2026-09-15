# -*- coding: utf-8 -*-
"""规则 A（退出面板三选一）+ 规则 B（系统壁纸提示）端到端体检。

起真进程、走真链路，不靠"读代码相信它写对了"。分三段：

  A. 纯函数层（不起面板）
     - panel_exit_action 在 STR_KEYS 登记、非法值被拒绝且有说明（不是静默丢）
     - ask_exit_native 的 auto_click 三连：KEEP/STOP/CANCEL 的按钮→选择映射
       —— 对话框在创建时被自动按下，无人值守可跑
     - 「不再询问」复选框勾选后 checked 能正确回传（TDM_CLICK_VERIFICATION）
     - ask_exit_choice：记住的选择直接执行、EXIT_STATE 只问一次、取消后复位
     - remember_exit_action 真的把选择写进配置

  B. 真面板 e2e
     - POST /api/config 的校验（非法值拒绝）
     - 恢复默认效果不弄丢 panel_exit_action（PANEL_ONLY_KEYS）
     - panel_exit_action=keep_wallpaper 时 POST /api/quit：
       不弹框、壁纸 pid 不动、面板进程自己退干净
     - panel_exit_action=stop_wallpaper 时 POST /api/quit：
       壁纸进程被停掉、pid 清掉、面板退出
     - 伪造 system-wallpaper.json（sidecar）→ state.system_wallpaper.seen=false
       → POST syswp-seen → seen=true → 重启面板后仍然是 true（持久化）

  C. 壁纸侧规则 B 探测（import wallpaper 模块，时间轴造假驱动）
     - read_system_wallpaper 真调 SPI 能拿到非空路径
     - 单次变化立即报（不等 20s）
     - 30s 窗口内变 4 次 → 判定动态壁纸、静默；稳定 20s 后报最后值一次
     - 幻灯片模式（注册表信号）下变化一律不报

  D. 端到端真换一次系统壁纸（SPI_SETDESKWALLPAPER）
      只有这一段会碰系统壁纸：先读原路径 → 设成另一张现成图（fWinIni=0，
       不广播 WM_SETTINGCHANGE）→ 等 sidecar 报新路径 → 立即设回原路径
       → 等 sidecar 报回原路径。全程约 10 秒，屏幕本来就盖着我们的壁纸窗口。
     - 验证壁纸进程对新代码的探测真的工作（A/C 段测的是逻辑，这段测真实进程）

注意：panel_exit_action='ask'（默认）时 POST /api/quit 会弹真框等人点，
无人值守没法测 —— 按钮映射与选择记忆在 A 段用 auto_click 覆盖，B 段只测
"已记住选择"的两条执行路径。真框长什么样请人肉点一次确认。

    python _probe_exit.py
"""

import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'wallpaper-config.json')
WP_PID = os.path.join(BASE, 'wallpaper.pid')
PANEL_PID = os.path.join(BASE, 'panel.pid')
LOG = os.path.join(BASE, 'wallpaper.log')
SYS_WP_JSON = os.path.join(BASE, 'system-wallpaper.json')
PANEL_SEEN = os.path.join(BASE, 'panel-seen.json')

_PYW = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                    'Programs', 'Python', 'Python312', 'pythonw.exe')
PYW = _PYW if os.path.exists(_PYW) else sys.executable

ok_n, bad_n, skip_n = 0, 0, 0


def check(name, ok, detail=''):
    global ok_n, bad_n
    if ok:
        ok_n += 1
    else:
        bad_n += 1
    tail = ('  — %s' % (detail,)) if detail != '' else ''
    print('%s %s%s' % ('[ok]' if ok else '[!!]', name, tail), flush=True)


def skip(n, name, detail=''):
    """明说"这几项这次没跑"。

    跳过的项如果只是 print 一行就 return，汇总数字会把它们悄悄算成覆盖过的
    部分 —— 看到"33 通过 / 0 失败"的人会以为规则 B 那条链路也验过了。
    """
    global skip_n
    skip_n += n
    tail = ('  — %s' % (detail,)) if detail != '' else ''
    print('[--] 跳过 %d 项：%s%s' % (n, name, tail), flush=True)


def load_module(fname, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(BASE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def http(method, url, body=None, timeout=8):
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def wait_for(fn, timeout, step=0.2, label=''):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if last:
            return True
        time.sleep(step)
    if label:
        print('   （等 %s 超时 %.1fs，最后一次 %r）' % (label, timeout, last))
    return False


def pid_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)   # PROCESS_QUERY_LIMITED
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def read_pid_file(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def kill_tree(pid):
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def spawn_detached(*args):
    return subprocess.Popen([PYW] + list(args), cwd=BASE,
                            creationflags=0x08000000 | 0x00000008)


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def find_url(base_size, timeout=30):
    import re
    end = time.time() + timeout
    pat = re.compile(r'http://127\.0\.0\.1:\d+')
    while time.time() < end:
        try:
            with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(base_size)
                for ln in f:
                    if '面板服务已起' not in ln:
                        continue
                    m = pat.search(ln)
                    if m:
                        return m.group(0) + '/'
        except OSError:
            pass
        time.sleep(0.2)
    return None


# ---------------------------------------------------------------- 备份/恢复
_BAK = {}


def save_state():
    for p in (CFG, PANEL_SEEN, SYS_WP_JSON):
        _BAK[p] = (open(p, 'rb').read() if os.path.exists(p) else None)


def restore_state():
    for p, data in _BAK.items():
        try:
            if data is None:
                if os.path.exists(p):
                    os.remove(p)
            else:
                with open(p, 'wb') as f:
                    f.write(data)
        except Exception as e:
            print('恢复 %s 失败: %r' % (p, e))


# ================================================================ A. 纯函数层
def part_a():
    print('\n—— A. 纯函数层 ——')
    pm = load_module('panel.pyw', 'panel_probe')
    globals()['PM'] = pm

    # A1 登记
    check('panel_exit_action 已登记进 STR_KEYS',
          'panel_exit_action' in pm.STR_KEYS,
          str(pm.STR_KEYS.get('panel_exit_action')))
    allowed = set(pm.STR_KEYS.get('panel_exit_action', ()))
    check('允许取值 = ask / keep_wallpaper / stop_wallpaper',
          allowed == {'ask', 'keep_wallpaper', 'stop_wallpaper'}, sorted(allowed))

    # A2-A5 auto_click 映射（每个都会短暂闪一个对话框，随即自动关闭）
    r = pm.ask_exit_native(0, pm.ID_TD_KEEP)
    check('TaskDialog: 按「只退面板」→ (keep_wallpaper, 未勾)',
          r is not None and r[0] == pm.EXIT_KEEP and r[1] is False, r)
    r = pm.ask_exit_native(0, pm.ID_TD_STOP)
    check('TaskDialog: 按「连壁纸一起停」→ (stop_wallpaper, 未勾)',
          r is not None and r[0] == pm.EXIT_STOP and r[1] is False, r)
    r = pm.ask_exit_native(0, pm.IDCANCEL)
    check('TaskDialog: 按「取消」→ None（不退出）', r == (None, False), r)
    r = pm.ask_exit_native(0, (pm.ID_TD_KEEP, True))
    check('TaskDialog: 勾「不再询问」+ 只退面板 → checked 回传 True',
          r is not None and r[0] == pm.EXIT_KEEP and r[1] is True, r)

    # A6 remember_exit_action 写配置（CFG 已备份，收尾恢复）
    pm.remember_exit_action(pm.EXIT_KEEP)
    got = pm.cfg_read().get('panel_exit_action')
    check('remember_exit_action 写进了配置', got == pm.EXIT_KEEP, got)

    # A7-A8 记住的选择直接执行、不弹框（计时兜底：弹框会等人）
    t0 = time.time()
    c1 = pm.ask_exit_choice('探针A')
    dt = time.time() - t0
    check('panel_exit_action=keep → ask_exit_choice 直接返回 keep',
          c1 == pm.EXIT_KEEP and dt < 1.0, 'choice=%r dt=%.2fs' % (c1, dt))

    pm.remember_exit_action(pm.EXIT_STOP)
    pm.reset_exit_state()
    c2 = pm.ask_exit_choice('探针A')
    check('panel_exit_action=stop → 返回 stop', c2 == pm.EXIT_STOP, c2)

    # A9 EXIT_STATE 只问一次：同一批里第二次直接沿缓存
    with pm.EXIT_STATE['lock']:
        asked = pm.EXIT_STATE['asked']
        cached = pm.EXIT_STATE['choice']
    check('ask_exit_choice 之后 EXIT_STATE 记住了本次选择',
          asked and cached == pm.EXIT_STOP, (asked, cached))

    # A10 配置校验：非法值拒绝且给说明（走 apply_config 全链路）
    cfg_before = pm.cfg_read()
    new_cfg, notes = pm.apply_config({'panel_exit_action': '随便'})
    check('非法取值被拒绝且写明原因',
          new_cfg.get('panel_exit_action') == cfg_before.get('panel_exit_action')
          and any('panel_exit_action' in n for n in notes), notes)
    new_cfg, notes = pm.apply_config({'panel_exit_action': 'stop_wallpaper'})
    check('合法取值通过校验',
          new_cfg.get('panel_exit_action') == 'stop_wallpaper', notes)

    pm.reset_exit_state()


# ================================================================ B. e2e
def start_panel(timeout=30):
    base = log_size()
    proc = spawn_detached('panel.pyw')
    url = find_url(base, timeout)
    if not url:
        kill_tree(proc.pid)
        return None, None, None
    st = wait_for(lambda: _safe_state(url), 15, label='面板服务可访问')
    if not st:
        return None, None, None
    return proc, url, (_safe_state(url) or {})


def _safe_state(url):
    try:
        return http('GET', url + 'api/state')
    except Exception:
        return None


def log_since(base):
    try:
        with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(base)
            return f.read()
    except OSError:
        return ''


def wait_log(base, needle, timeout, step=0.2):
    return wait_for(lambda: needle in log_since(base), timeout, step,
                    label='日志出现「%s」' % needle)


def ensure_wallpaper():
    """停掉可能在跑的旧壁纸，起一个新代码壁纸，等它日志打出"启动完成"。

    判据全用日志和 pid 文件，不用 pid_alive —— 探针频繁起杀进程，Windows
    会立刻复用 pid，按 pid 查存活会查到占了 pid 的新进程（本轮实测踩过）。
    """
    old = read_pid_file(WP_PID)
    if old:
        kill_tree(old)
        try:
            os.remove(WP_PID)
        except OSError:
            pass
        time.sleep(1.5)
    base = log_size()
    spawn_detached('wallpaper.pyw')
    if not wait_log(base, '启动完成', 30):
        return False, base
    return True, base


def part_b():
    print('\n—— B. 真面板 e2e ——')
    PM.remember_exit_action('ask')
    PM.reset_exit_state()
    proc, url, st = start_panel()
    if not url:
        check('面板 e2e：面板起得来', False, '30 秒内没等到服务地址')
        return
    check('面板 e2e：面板起得来', True, url)

    try:
        # B2 reset 保留 panel_exit_action（PANEL_ONLY_KEYS 修复）
        http('POST', url + 'api/config', {'panel_exit_action': 'keep_wallpaper'})
        st = http('POST', url + 'api/action', {'action': 'reset'})
        got = st['state']['config'].get('panel_exit_action')
        check('「恢复默认效果」不弄丢 panel_exit_action', got == 'keep_wallpaper', got)

        # B3 keep 路径：退出面板、壁纸不动
        ok_wp, wp_log_base = ensure_wallpaper()
        check('e2e 前置：新代码壁纸跑起来', ok_wp)
        if not ok_wp:
            return
        wp_pid_before = read_pid_file(WP_PID)
        t0 = time.time()
        r = http('POST', url + 'api/quit', timeout=6)
        dt = time.time() - t0
        check('keep_wallpaper: /api/quit 不弹框直接应答',
              r.get('ok') and r.get('exiting') and not r.get('stopped')
              and dt < 2.0,
              '%r dt=%.2fs' % ({k: r.get(k) for k in ('ok', 'exiting', 'stopped')}, dt))
        ok = wait_for(lambda: proc.poll() is not None, 20, label='面板进程退出')
        check('keep_wallpaper: 面板进程自己退干净', ok)
        check('keep_wallpaper: panel.pid 已清', read_pid_file(PANEL_PID) == 0)
        # 壁纸没被动过：pid 文件不变 + 日志里没有"停止壁纸"
        tail = log_since(wp_log_base)
        check('keep_wallpaper: 壁纸 pid 文件不变、日志无"停止壁纸"',
              read_pid_file(WP_PID) == wp_pid_before
              and '停止壁纸' not in tail,
              'pid %s -> %s' % (wp_pid_before, read_pid_file(WP_PID)))

        # B4 stop 路径：壁纸一起停
        proc2, url2, st2 = start_panel()
        if not url2:
            check('stop_wallpaper: 面板第二次起得来', False, '没等到服务地址')
            return
        try:
            stop_log_base = log_size()
            http('POST', url2 + 'api/config', {'panel_exit_action': 'stop_wallpaper'})
            r = http('POST', url2 + 'api/quit', timeout=6)
            ok = wait_for(lambda: proc2.poll() is not None, 20, label='面板进程退出')
            check('stop_wallpaper: 应答 stopped=True', r.get('stopped') is True, r)
            check('stop_wallpaper: 面板退出', ok)
            check('stop_wallpaper: 壁纸被停（日志 + pid 文件清掉）',
                  wait_log(stop_log_base, '停止壁纸', 10)
                  and read_pid_file(WP_PID) == 0,
                  'pid=%s' % read_pid_file(WP_PID))
        finally:
            if proc2.poll() is None:
                kill_tree(proc2.pid)

        # B5 规则 B sidecar 链路：伪造 sidecar → seen=false → 确认 → 持久
        now = time.time()
        with open(SYS_WP_JSON, 'w', encoding='utf-8') as f:
            json.dump({'path': r'C://fake//新壁纸.jpg', 'name': '新壁纸.jpg',
                       'prev': 'old.jpg', 'changed_at': now}, f)
        proc3, url3, st3 = start_panel()
        if not url3:
            check('规则B: 面板第三次起得来', False, '没等到服务地址')
            return
        try:
            sw = (st3.get('system_wallpaper') or {})
            check('规则B: state.system_wallpaper 读到 sidecar 且 seen=false',
                  sw.get('name') == '新壁纸.jpg' and sw.get('seen') is False, sw)
            r = http('POST', url3 + 'api/action', {'action': 'syswp-seen'})
            check('规则B: syswp-seen 后 seen=true',
                  r['state']['system_wallpaper']['seen'] is True,
                  r['state']['system_wallpaper'])
        finally:
            if proc3.poll() is None:
                kill_tree(proc3.pid)
        # 重启后 seen 仍为 true（PANEL_SEEN_PATH 持久化）
        proc4, url4, st4 = start_panel()
        if url4:
            sw = (st4.get('system_wallpaper') or {})
            check('规则B: 重启面板后 seen 仍为 true（记忆持久化）',
                  sw.get('seen') is True, sw)
            kill_tree(proc4.pid)
            wait_for(lambda: proc4.poll() is not None, 10)
        else:
            check('规则B: 重启面板后 seen 仍为 true', False, '面板没起来')
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
        globals().pop('PM', None)


# ================================================================ C. 壁纸侧逻辑
def part_c():
    print('\n—— C. 壁纸侧规则 B 探测（时间轴造假） ——')
    wm = load_module('wallpaper.pyw', 'wallpaper_probe')
    globals()['WM'] = wm

    check('read_system_wallpaper 真调 SPI 拿到非空路径',
          bool(wm.read_system_wallpaper()), wm.read_system_wallpaper())

    # sidecar 写到临时文件，别污染真 sidecar
    tmp_side = os.path.join(BASE, '_probe_syswp.json')
    wm.SYS_WP_PATH = tmp_side
    reports = []

    def fake_report(p, prev):
        # 和真版一致：报告的同时更新 _SYSWP['reported']
        reports.append((p, prev))
        wm._SYSWP['reported'] = p

    wm.report_system_wallpaper = fake_report

    # 重置探测状态，造假时间轴
    real_time = time.time
    clock = {'t': 1000.0}
    time.time = lambda: clock['t']
    try:
        def poll():
            wm.poll_system_wallpaper()

        # 基线
        wm.read_system_wallpaper = lambda: 'A.jpg'
        poll()
        check('首次调用只记基线不报', reports == [], reports)

        # 单次变化 → 立即报
        clock['t'] += 5               # 越过 SYS_WP_POLL_S 节流窗口
        wm.read_system_wallpaper = lambda: 'B.jpg'
        poll()
        check('单次变化立即报（不等沉淀期）',
              reports == [('B.jpg', 'A.jpg')], reports)

        # 风暴：30s 内变满 4 次（每次必须是不同路径，同值不算变化）。
        # 到达阈值之前的每次变化都正常报，第 4 次触发判定 → 之后的静默
        for i in (2, 3, 4):
            wm.read_system_wallpaper = (lambda i: lambda: 'C%d.jpg' % i)(i)
            clock['t'] += 5
            poll()          # 第 2、3 次 → 报；第 4 次 → 风暴，不报
        check('风暴判定：达到 4 次/30s 后转为静默',
              len(reports) == 3 and wm._SYSWP['suppress'],
              (len(reports), wm._SYSWP['suppress']))

        # 风暴后仍静默（哪怕又变了）
        wm.read_system_wallpaper = lambda: 'D.jpg'
        clock['t'] += 3
        poll()
        check('风暴窗口内仍静默', len(reports) == 3, len(reports))

        # 稳定 ≥20s → 恢复提示，报最后那个稳定值一次
        clock['t'] += wm.SYS_WP_SETTLE_S + 1
        poll()          # 同一路径 → 记 stable_since
        clock['t'] += wm.SYS_WP_SETTLE_S + 1
        poll()          # 达到沉淀期 → 报 D.jpg
        check('稳定超过沉淀期后报一次最后稳定值',
              len(reports) == 4 and reports[-1][0] == 'D.jpg', reports)
    finally:
        time.time = real_time
        if os.path.exists(tmp_side):
            os.remove(tmp_side)


# ================================================================ D. 真换一次壁纸
SPI_GET = 0x0073
SPI_SET = 0x0075


def spi_get():
    buf = ctypes.create_unicode_buffer(520)
    if not ctypes.windll.user32.SystemParametersInfoW(SPI_GET, len(buf), buf, 0):
        return ''
    return buf.value


def spi_set(path):
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SET, 0, path, 0))          # fWinIni=0：不广播，静默设置


def part_d():
    print('\n—— D. 端到端：真换一次系统壁纸再换回来 ——')
    wm = load_module('wallpaper.pyw', 'wallpaper_probe2')
    # 起一个新代码壁纸进程（B 段停掉了旧的那个）
    old = read_pid_file(WP_PID)
    if old:
        print('   （壁纸已在跑，先停掉换成新代码进程）')
        kill_tree(old)
        try:
            os.remove(WP_PID)
        except OSError:
            pass
        time.sleep(1.5)
    base = log_size()
    proc = spawn_detached('wallpaper.pyw')
    # 用日志判据，不用 pid_alive —— pid 可能被立刻复用（本轮实测踩过）
    ok = wait_log(base, '启动完成', 30)
    check('新代码壁纸进程起来并登记 pid', ok, 'proc pid=%d' % proc.pid)
    if not ok:
        return

    cur = spi_get()
    if not cur:
        check('读当前系统壁纸路径', False, 'SPI_GET 失败')
        return
    check('读当前系统壁纸路径', True, os.path.basename(cur))

    # 找一张"和当前不一样的"现成图
    target = None
    for cand in (r'C:\Windows\Web\Wallpaper\ThemeC\img30.jpg',
                 r'C:\Windows\Web\Wallpaper\ThemeC\img28.jpg',
                 os.path.join(BASE, 'images', 'out.jpg')):
        if cand and os.path.exists(cand) and cand.lower() != cur.lower():
            target = cand
            break
    if not target:
        check('找到一张不同的现成图做测试目标', False, '没找到，跳过 D 段')
        return

    if not spi_set(target):
        # 本机实测：从脚本环境里 SPI_SET 一律静默失败（err=0），无论 fWinIni
        # 给什么、沙箱内外都一样 —— 这是测试环境的限制，不是探测代码的
        # 问题（SPI_GET 正常、探测逻辑 C 段已验证）。真实验证请用户在
        # 「设置 → 个性化 → 背景」里换一次壁纸，看面板提示条亮不亮。
        skip(3, '系统壁纸真实变化（本环境 SPI_SET 静默失败）',
             '请手动在系统设置里换一次壁纸，看面板提示条亮不亮')
        return

    try:
        if os.path.exists(SYS_WP_JSON):
            os.remove(SYS_WP_JSON)
        check('壁纸进程探测到变化并写 sidecar（面板会亮提示条）',
              wait_for(
                  lambda: _sidecar_path() == target, 12, label='sidecar 报新路径'),
              _sidecar_path())
        # 立即换回来
        check('换回原壁纸', spi_set(cur), cur)
        ok = wait_for(
            lambda: _sidecar_path() == cur, 12, label='sidecar 报回原路径')
        check('换回后 sidecar 再次更新（连续 2 次变化 < 风暴阈值，不误判）', ok,
              _sidecar_path())
    finally:
        if cur:
            spi_set(cur)               # 兜底：无论中途怎么失败都设回去


def _sidecar_path():
    try:
        with open(SYS_WP_JSON, 'r', encoding='utf-8') as f:
            return json.load(f).get('path')
    except Exception:
        return None


# ================================================================ main
def main():
    save_state()
    try:
        part_a()
        part_b()
        part_c()
        part_d()
    finally:
        restore_state()

    print('\n========== 结果：%d 通过 / %d 失败 / %d 跳过 =========='
          % (ok_n, bad_n, skip_n))

    # 收尾：把用户的常驻状态恢复 —— 壁纸跑着（新代码），面板停着（用户自己关的）
    if not pid_alive(read_pid_file(WP_PID)):
        spawn_detached('wallpaper.pyw')
        print('（已重新拉起新代码壁纸进程）')
    sys.exit(1 if bad_n else 0)


if __name__ == '__main__':
    main()
