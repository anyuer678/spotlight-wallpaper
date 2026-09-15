# -*- coding: utf-8 -*-
"""托盘功能端到端体检：真的起一个面板，真的最小化，真的去点托盘图标。

为什么不做成"读代码判断对不对"
------------------------------------------------------------------
托盘这块全在 Win32 消息层面，静态看代码一个字都验不出来：
  · Shell_NotifyIconW 到底有没有把图标装进通知区域；   → 只能看 tray.active
  · 点一下托盘图标，窗口回不回来；                      → 只能真的发一条消息
  · 窗口最小化之后，桌面上的任务栏按钮有没有真的消失。   → 只能问 IsWindowVisible

所以这个脚本起一个真面板进程（不是 --serve-only），走完整条链路：

    1. 面板起来 → 托盘图标装上（/api/state.tray.active）
    2. 结构体尺寸 = 976（填错这个数字的后果是"图标在、气泡不弹"这种半死不活）
    3. 面板.ico 真的生成出来了
    4. ShowWindow(SW_MINIMIZE) 之后 → 窗口不可见（任务栏按钮消失）
       且 /api/state.tray.hidden == true
    5. 往托盘窗口 PostMessage(WM_TRAY, WM_LBUTTONUP)
       —— 这就是"单击托盘图标"本身，和真人点下去走的是同一条消息
       → 窗口回到可见、hidden 复位
    6. 界面按钮那条路（POST /api/action {action:to-tray} / from-tray）
    7. 用 close_panel_window() 关掉面板窗口（托盘菜单"退出面板"走的就是它）
       → 面板进程自己收摊：摘图标、清 panel.pid、删临时 profile

全程只碰自己的进程。用完之后如果进程还在，用 /T 连子进程一起收掉。

前置条件：脚本会把配置里的 panel_exit_action 临时设成 keep_wallpaper ——
'ask'（出厂默认）时关窗会弹出"三选一"对话框等人点，无人值守下没人点就
只能超时，最后两项断言于是"看上次运行留下的配置值"过或不过。跑完在
finally 里把配置文件原样写回去。

    python _probe_tray.py
"""

import ctypes
import importlib.util
import json
import os
import re
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
LOG = os.path.join(BASE, 'wallpaper.log')
PIDFILE = os.path.join(BASE, 'panel.pid')
ICON = os.path.join(BASE, 'panel.ico')
WALLPAPER_PID = os.path.join(BASE, 'wallpaper.pid')
CFG = os.path.join(BASE, 'wallpaper-config.json')

_PYW = os.path.join(os.environ.get('LOCALAPPDATA', ''),
                    'Programs', 'Python', 'Python312', 'pythonw.exe')
PYW = _PYW if os.path.exists(_PYW) else sys.executable

u = ctypes.windll.user32
u.PostMessageW.restype = ctypes.c_int
u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                           ctypes.c_ssize_t]
u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.IsWindowVisible.restype = ctypes.c_int
u.IsIconic.argtypes = [ctypes.c_void_p]
u.IsIconic.restype = ctypes.c_int

WM_TRAY = 0x8000 + 1          # WM_APP + 1，和 panel.pyw 里的 WM_TRAY 一致
WM_LBUTTONUP = 0x0202
SW_MINIMIZE = 6

ok_n, bad_n = 0, 0


def check(name, ok, detail=''):
    global ok_n, bad_n
    if ok:
        ok_n += 1
    else:
        bad_n += 1
    tail = ('  — %s' % detail) if detail != '' else ''
    print('%s %s%s' % ('[ok]' if ok else '[!!]', name, tail), flush=True)


def http(method, url, body=None, timeout=6):
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def wait_for(fn, timeout, step=0.15, label=''):
    """轮询到 fn() 为真。托盘看护是 300ms 一轮，所以这里必须等而不是睡死。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if last:
            return True
        time.sleep(step)
    if label:
        print('   （等 %s 超时 %.1fs，最后一次结果 %r）' % (label, timeout, last))
    return False


# ---------------------------------------------------------------- 启动面板
def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def find_url(base_size, timeout=30):
    """从日志里捞出本次启动的服务地址（只认新增的那部分）。

     用正则抠，不要 `line.split()[0]` —— 日志写的是
      `面板服务已起：http://127.0.0.1:63627/（pid=12048）`，
      "（pid=..." 前面没有空格，split 会把整段都当成第一个词，
      拼出来就是一个非法 URL，然后每一条请求都静默抛异常，
      报错信息还会指向"托盘没装上"—— 查半天发现是解析日志的问题。
    """
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


def kill_tree(pid):
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def drop_pid(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_cfg_text():
    try:
        with open(CFG, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def restore_cfg_text(text):
    if text is None:
        return
    try:
        with open(CFG, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    except OSError:
        pass


def set_exit_action(value):
    """把 panel_exit_action 设成确定值，别的键一个字不动。

    最后两项断言（关窗后面板自己收摊 / panel.pid 被清掉）要求"关窗即退出"。
    可 panel_exit_action='ask'（出厂默认）时，标题栏 × 那条路会弹出"三选一"
    对话框等人点 —— 无人值守下没人点，25 秒必然超时。原来这个探针没管它，
    于是过不过全看"上一次运行恰好把配置留成了什么值"。这里自己把前置条件
    摆好，跑完在 finally 里原样恢复。
    """
    try:
        with open(CFG, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg['panel_exit_action'] = value
    with open(CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _report(ok_n, bad_n):
    """打印汇总并按失败数决定退出码。

    必须由每条出口路径显式调用。main 里有几处"前置条件不满足就提前 return"，
    而那些恰恰是最要紧的失败（面板服务起不来、托盘装不上、窗口找不到）。
    汇总原先是写在 try/finally 之后的，提前 return 直接把它跳过 —— 探针以
    退出码 0 结束、连汇总行都不打印，只看退出码判活的 runner 会当成通过。
    """
    print('\n===== 托盘体检：%d 项通过 / %d 项失败 =====' % (ok_n, bad_n))
    sys.exit(1 if bad_n else 0)


def main():
    if os.path.exists(PIDFILE):
        old = open(PIDFILE).read().strip()
        print('发现残留 panel.pid=%s，先收掉' % old)
        kill_tree(old)
        drop_pid(PIDFILE)
        time.sleep(1)

    # 导入面板模块只为两件事：拿 current_panel_window()、拿 TRAY_CLASS。
    # 不在探测脚本里重写一遍"怎么找面板窗口"——那等于在测自己的复述。
    spec = importlib.util.spec_from_file_location('pm', os.path.join(BASE, 'panel.pyw'))
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)

    cfg_backup = read_cfg_text()
    set_exit_action('keep_wallpaper')

    base = log_size()
    if os.path.exists(ICON):
        os.remove(ICON)                      # 逼它重画一次，顺带验证画得出来

    print('启动面板：%s' % PYW)
    proc = subprocess.Popen([PYW, os.path.join(BASE, 'panel.pyw')], cwd=BASE,
                            creationflags=0x08000000)
    print('面板 pid = %d\n' % proc.pid)

    hwnd = 0
    pid = proc.pid
    try:
        url = find_url(base)
        if not url:
            check('面板服务起得来', False, '30 秒内日志里没出现"面板服务已起"')
            return _report(ok_n, bad_n)
        check('面板服务起得来', True, url)

        # ---------------------------------------------------------- 1. 图标装上
        st = None
        def tray_ready():
            nonlocal st
            try:
                st = http('GET', url + 'api/state')
            except Exception:
                return False
            return bool(st.get('tray', {}).get('active'))
        if not wait_for(tray_ready, 15, label='托盘图标装上'):
            check('托盘图标装进通知区域', False,
                  (st or {}).get('tray', {}).get('error') or 'tray.active 一直为假')
            return _report(ok_n, bad_n)
        check('托盘图标装进通知区域', True, 'hwnd=%d' % st['tray']['hwnd'])

        # ---------------------------------------------------------- 2. 结构体
        check('NOTIFYICONDATAW 尺寸 = 976', st['tray']['nid_size'] == 976,
              st['tray']['nid_size'])

        # ---------------------------------------------------------- 3. 图标文件
        check('panel.ico 画出来了',
              os.path.exists(ICON) and os.path.getsize(ICON) > 0,
              '%d 字节' % (os.path.getsize(ICON) if os.path.exists(ICON) else 0))

        # ---------------------------------------------------------- 4. 面板窗口
        # 托盘图标是窗口启起来之后立刻装的，但 Edge 要过几秒才真正建出窗口 ——
        # 不等着查会得出"找不到面板窗口"的假结论。
        def win_ready():
            nonlocal hwnd
            hwnd = pm.current_panel_window()
            return bool(hwnd)
        wait_for(win_ready, 25, label='面板窗口出现')
        check('找得到面板窗口', bool(hwnd), 'hwnd=%d' % hwnd)
        if not hwnd:
            return _report(ok_n, bad_n)
        check('面板窗口标题正确',
              pm.CORE.win32gui.GetWindowText(hwnd) == pm.WINDOW_TITLE,
              repr(pm.CORE.win32gui.GetWindowText(hwnd)))

        # ---------------------------------------------------------- 5. 最小化 → 托盘
        u.ShowWindow(hwnd, SW_MINIMIZE)
        hid = wait_for(lambda: not u.IsWindowVisible(hwnd), 6,
                       label='窗口被收进托盘')
        check('最小化之后窗口被收进托盘（任务栏按钮消失）', hid,
              'IsWindowVisible=%d IsIconic=%d' % (u.IsWindowVisible(hwnd),
                                                  u.IsIconic(hwnd)))
        st = http('GET', url + 'api/state')
        check('后端状态同步为 hidden', st['tray']['hidden'] is True,
              st['tray']['hidden'])

        # ---------------------------------------------------------- 6. 点托盘图标
        tray_hwnd = st['tray']['hwnd']
        u.PostMessageW(tray_hwnd, WM_TRAY, 1, WM_LBUTTONUP)   # 就是"单击托盘图标"
        back = wait_for(lambda: u.IsWindowVisible(hwnd), 6, label='窗口显示回来')
        check('单击托盘图标 → 面板回来了', back,
              'IsWindowVisible=%d' % u.IsWindowVisible(hwnd))
        st = http('GET', url + 'api/state')
        check('后端状态复位为显示', st['tray']['hidden'] is False,
              st['tray']['hidden'])

        # ---------------------------------------------------------- 7. 界面按钮那条路
        r = http('POST', url + 'api/action', {'action': 'to-tray'})
        check('POST to-tray 返回 ok', r.get('ok') is True, r.get('note'))
        gone = wait_for(lambda: not u.IsWindowVisible(hwnd), 5, label='接口收起窗口')
        check('接口把窗口收进托盘', gone,
              'IsWindowVisible=%d' % u.IsWindowVisible(hwnd))

        r = http('POST', url + 'api/action', {'action': 'from-tray'})
        check('POST from-tray 返回 ok', r.get('ok') is True, r.get('note'))
        back = wait_for(lambda: u.IsWindowVisible(hwnd), 5, label='接口显示窗口')
        check('接口把窗口显示回来', back,
              'IsWindowVisible=%d' % u.IsWindowVisible(hwnd))

        # ---------------------------------------------------------- 8. 干净退出
        # 托盘菜单「退出面板」走的就是 close_panel_window()，这里直接调它 ——
        # 而不是自己再 PostMessage 一遍。自己再写一遍等于在测自己的复述：
        # 第一版就是这么写的，结果探针发 WM_CLOSE 关不掉窗口，我还以为
        # 是"退出路径慢"，其实是消息本身就不对（Chromium 不认 WM_CLOSE）。
        check('close_panel_window() 找得到窗口',
              bool(pm.close_panel_window()), '')
        gone = wait_for(lambda: not _alive(pid), 25, label='面板退出')
        check('关窗后面板进程自己收摊', gone and not _alive(pid),
              '进程还活着' if _alive(pid) else '已退出')
        check('panel.pid 被清掉', not os.path.exists(PIDFILE),
              '还留着 %s' % PIDFILE if os.path.exists(PIDFILE) else '')
    finally:
        if _alive(pid):
            print('\n（收尾：连子进程一起收掉 pid=%d）' % pid)
            kill_tree(pid)
            time.sleep(0.8)
        drop_pid(PIDFILE)
        restore_cfg_text(cfg_backup)
        if os.path.exists(WALLPAPER_PID):
            print('（提示：wallpaper.pid 还在，壁纸进程不受本次体检影响）')

    _report(ok_n, bad_n)


def _alive(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259      # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


if __name__ == '__main__':
    main()
