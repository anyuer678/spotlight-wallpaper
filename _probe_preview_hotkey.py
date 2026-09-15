# -*- coding: utf-8 -*-
"""Ctrl+Alt+W（叫出 / 关掉小窗口预览）端到端体检。

这条链路为什么必须端到端测，而不是"读代码看着对"：

  · 热键循环里那个 0x57 到底是不是 W。写错了代码照样跑，只是按了没反应。
  · _apply_action 拿到 'preview' 之后有没有真的去调 _toggle_preview。
    没接上的话它会掉进下面"换图"那条支路 —— 弹一个选文件的框。静默走错路。
  · 预览能不能和壁纸并存。两边共用一把互斥体的话，壁纸跑着时热键永远拉不
    起任何东西，而代码看起来"明明写了 spawn"（旧行为正是如此）。
  · 预览进程会不会把壁纸的 pid 文件顶掉。顶掉的后果最坏：面板的「停止壁纸」
    去杀预览，壁纸反而留在桌面上关不掉。
  · 连按两下会不会叠出两个预览窗口。

键盘不动：不模拟全局按键（GetAsyncKeyState 被替成桩），但走的是真函数、真进程。

  分三段：
    A. 接线（不起进程）：按键映射、动作分发、命令行拼装、标题协议
    B. 真进程：按一下热键 → 预览起来 → 认窗口 → 再按一下 → 它退干净
    C. 落地证据：日志带上 (预览) 标记，pid 文件从头到尾没被动过

预览窗口会真的在屏幕上闪几下 —— 这是它唯一诚实的测法。

    python _probe_preview_hotkey.py
"""

import ctypes
import importlib.util
import os
import subprocess
import sys
import threading
import time
import traceback

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
WP_PID = os.path.join(BASE, 'wallpaper.pid')
LOG = os.path.join(BASE, 'wallpaper.log')

ok_n, bad_n, skip_n = 0, 0, 0
_spawned = []          # 本次探针亲手拉起来的预览进程 pid，收尾时清掉
_saved = {}            # 需要还原的东西（被替换掉的函数等）


def check(name, ok, detail=''):
    global ok_n, bad_n
    if ok:
        ok_n += 1
    else:
        bad_n += 1
    tail = ('  — %s' % (detail,)) if detail != '' else ''
    print('%s %s%s' % ('[ok]' if ok else '[!!]', name, tail), flush=True)


def skip(n, name, detail=''):
    """明说"这几项这次没跑"，把它们从"通过"里摘出来。"""
    global skip_n
    skip_n += n
    tail = ('  — %s' % (detail,)) if detail != '' else ''
    print('[--] 跳过 %d 项：%s%s' % (n, name, tail), flush=True)


def load_module(fname, modname):
    spec = importlib.util.spec_from_file_location(modname,
                                                  os.path.join(BASE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


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
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))      # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == 259                      # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def read_pid_text():
    try:
        with open(WP_PID, 'r') as f:
            return f.read().strip()
    except OSError:
        return None


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def log_since(n):
    try:
        with open(LOG, 'rb') as f:
            f.seek(n)
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def count_preview_windows(W):
    """屏幕上顶着预览标题的窗口有几个。

    不直接复用 find_preview_window()：那个函数返回"第一个"，两个窗口时
    它照样返回一个 —— 而"连按两下会不会叠出两个"正是要测的事。
    """
    import win32gui
    hits = []

    def cb(h, _):
        try:
            if win32gui.GetWindowText(h).startswith(W.PREVIEW_TITLE):
                hits.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return len(hits)


class Shim(object):
    """给热键线程 / 动作分发用的假 self。

    这两个函数只碰 running / _pending / _toggle_preview，用不着一个真窗口 ——
    正因为它们这么干净，才可以在不起壁纸的前提下单独验。
    """
    running = True
    _pending = None

    def _quit(self):
        self.running = False


def main():
    import win32gui
    W = load_module('wallpaper.pyw', 'wl_preview_probe')
    _saved['get_async'] = W.win32api.GetAsyncKeyState
    _saved['pick'] = W.pick_image_file

    # ---------------------------------------------------------- A. 接线
    print('【A】接线（不起进程）')
    VK_CTRL = W.win32con.VK_CONTROL
    VK_ALT = W.win32con.VK_MENU
    VK_W = 0x57

    def fake_keys(pressed):
        def f(vk):
            return -32768 if vk in pressed else 0     # 高位为 1 = 按着
        return f

    def run_loop(pressed, seconds):
        sh = Shim()
        W.win32api.GetAsyncKeyState = fake_keys(pressed)
        t = threading.Thread(target=W.SpotlightWallpaper._hotkey_loop,
                             args=(sh,), daemon=True)
        t.start()
        time.sleep(seconds)
        sh.running = False
        t.join(1.5)
        return sh

    sh = run_loop({VK_CTRL, VK_ALT, VK_W}, 0.7)
    check('按住 Ctrl+Alt+W → 热键线程递出 preview',
          sh._pending == 'preview', '实际 %r' % (sh._pending,))

    sh = run_loop({VK_CTRL, VK_ALT}, 0.7)
    check('反面对照：只按 Ctrl+Alt、没按 W 时什么都不递',
          sh._pending is None, '实际 %r' % (sh._pending,))
    W.win32api.GetAsyncKeyState = _saved['get_async']

    # 分发改道：'preview' 必须走 _toggle_preview，不许掉进换图支路
    hit, picked = [], []
    W.pick_image_file = lambda *a, **k: (picked.append(a), None)[1]
    sh = Shim()
    sh._pending = 'preview'
    sh._toggle_preview = lambda: hit.append(1)
    W.SpotlightWallpaper._apply_action(sh)
    check("_apply_action 把 'preview' 交给了 _toggle_preview",
          bool(hit) and not picked,
          'toggle=%d 换图支路=%d' % (len(hit), len(picked)))
    check('_apply_action 取走动作后清空 _pending',
          sh._pending is None, '实际 %r' % (sh._pending,))
    W.pick_image_file = _saved['pick']

    # 命令行：角色参数与模式参数是两件事
    argv = W._preview_argv()
    line = ' '.join(argv)
    check('命令行里带上了 --window 这个模式', argv[-1] == '--window', line)
    check('命令行没有踩 --window → window.pyw 的坑',
          'window.pyw' not in line
          and (getattr(sys, 'frozen', False) or argv[1].endswith('wallpaper.pyw')),
          line)
    if getattr(sys, 'frozen', False):
        skip(1, '把命令行换成 pythonw（exe 模式没有这一层）')
    else:
        exe = os.path.basename(argv[0]).lower()
        pw = os.path.join(os.path.dirname(argv[0]), 'pythonw.exe')
        check('命令行换成 pythonw（用 python.exe 会闪一个黑框）',
              exe.startswith('pythonw') or not os.path.exists(pw), argv[0])

    check('窗口标题以 PREVIEW_TITLE 开头 —— find_preview_window 的判据',
          W.WINDOW_TITLE.startswith(W.PREVIEW_TITLE), W.WINDOW_TITLE)
    check('预览和壁纸各用一把锁（共用的话热键永远拉不起来）',
          W.MUTEX_PREVIEW != W.MUTEX_WALLPAPER,
          '%s / %s' % (W.MUTEX_WALLPAPER, W.MUTEX_PREVIEW))

    # 起始状态
    if W.preview_running():
        print('   （开始前就有一个预览在跑，先把它关掉再测）')
        W.close_preview_window()
        wait_for(lambda: not W.preview_running(), 10.0, 0.2, '旧预览退出')
    check('开始前：没有预览在跑', not W.preview_running())
    check('开始前：找不到预览窗口', W.find_preview_window() == 0)

    # ---------------------------------------------------------- B. 真进程
    print()
    print('【B】真拉起 / 真关掉（屏幕上会闪几下预览窗口）')
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    # 占住壁纸那把锁 = 让"壁纸正在跑"这件事成真。共用一把锁的实现到这里
    # 就露馅了：下面按热键拉预览会被自己挡回去。
    h_lock = k32.CreateMutexW(None, False, W.MUTEX_WALLPAPER)
    check('先假装壁纸在跑（占住壁纸锁）', bool(h_lock), 'h=%r' % h_lock)

    pid_before = read_pid_text()
    if pid_before is None:
        # 壁纸没在跑 → 连 pid 文件都没有。先放一个哨兵进去：只有文件存在着，
        # "预览退出时顺手把它删了"这一条才真的被测到 —— 本来就没有的文件，
        # 删不删都看不出来。收尾时把这个哨兵收走。
        try:
            _saved['pid_wrote'] = '999999'
            with open(WP_PID, 'w') as f:
                f.write(_saved['pid_wrote'])
            pid_before = _saved['pid_wrote']
            print('   （壁纸没在跑，先放一个哨兵 pid 进 wallpaper.pid）')
        except OSError as e:
            print('   （哨兵写不进去：%r —— 这一项退化成"文件始终不存在"）' % e)
    log_off = log_size()
    sh = Shim()

    W.SpotlightWallpaper._toggle_preview(sh)          # 第一下 = 叫出来
    proc = None
    check('按一下热键：预览进程起来了',
          wait_for(W.preview_running, 25.0, 0.2, '预览进程'))
    check('按一下热键：屏幕上出现了预览窗口',
          wait_for(lambda: W.find_preview_window() != 0, 25.0, 0.2, '预览窗口'))

    hwnd = W.find_preview_window()
    if hwnd:
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        check('窗口标题就是 WINDOW_TITLE', title == W.WINDOW_TITLE, title)
        check('窗口是可见的（不是建出来就藏着的）',
              bool(win32gui.IsWindowVisible(hwnd)))
        check('是 Tk 顶层窗口 —— 说明真按 --window 起来了',
              cls == 'TkTopLevel', cls)
    else:
        skip(3, '预览窗口的属性（窗口没找到）')

    check('预览没去动壁纸的 pid 文件',
          read_pid_text() == pid_before,
          '之前 %r / 现在 %r' % (pid_before, read_pid_text()))

    # 故意再拉一个：它应该被自己的锁挡回去，而不是叠出第二个窗口
    extra = W.spawn_preview()
    if extra:
        _spawned.append(extra.pid)
    time.sleep(3.0)
    check('再拉一个会被预览自己的锁挡住',
          extra is None or not pid_alive(extra.pid),
          'pid=%s' % (getattr(extra, 'pid', None),))
    check('屏幕上仍然只有一个预览窗口',
          count_preview_windows(W) == 1,
          '实际 %d 个' % count_preview_windows(W))

    W.SpotlightWallpaper._toggle_preview(sh)          # 第二下 = 关掉
    check('再按一下热键：预览进程退干净',
          wait_for(lambda: not W.preview_running(), 20.0, 0.2, '预览退出'))
    check('再按一下热键：窗口也从屏幕上消失了',
          wait_for(lambda: W.find_preview_window() == 0, 10.0, 0.2, '窗口消失'))
    check('关掉之后仍然没动壁纸的 pid 文件',
          read_pid_text() == pid_before, '现在 %r' % (read_pid_text(),))

    # ---------------------------------------------------------- C. 证据
    print()
    print('【C】落地证据')
    tail = log_since(log_off)
    check('预览进程的日志带上了 (预览) 标记',
          '(预览) 启动完成' in tail,
          '日志尾部：%r' % tail[-160:])
    check('预览是以 window 模式起来的',
          '模式=window' in tail, '')
    check('壁纸侧留下了"已拉起"的记录',
          '小窗口预览已拉起' in tail, '')
    check('壁纸侧留下了"请它关闭"的记录',
          '请预览窗口关闭' in tail, '')

    if h_lock:
        k32.CloseHandle(ctypes.c_void_p(h_lock))


def cleanup():
    """无论怎么退出，屏幕上都不许留下我们拉起来的预览窗口。"""
    try:
        W = sys.modules.get('wl_preview_probe')
        if W is not None:
            W.close_preview_window()
            wait_for(lambda: not W.preview_running(), 8.0, 0.2, '预览退出')
        # 哨兵是我们自己放进去的，收尾必须收走 —— 别给用户留一个假 pid 文件。
        # 内容对不上（说明真有进程写过它）就不动，那是现场证据。
        if _saved.get('pid_wrote') and read_pid_text() == _saved['pid_wrote']:
            try:
                os.remove(WP_PID)
            except OSError:
                pass
        for p in _spawned:
            if pid_alive(p):
                subprocess.run(['taskkill', '/F', '/PID', str(int(p))],
                               creationflags=0x08000000, close_fds=True,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    # 汇总与收尾都挂在最外层 finally 上：函数里任何一处提前 return / 抛异常
    # 都绕不过它。托盘那份探针把汇总写在 try/finally 之后，被几条提前 return
    # 跳过去过 —— 顶层 finally 是从结构上堵掉这一类假绿。
    try:
        main()
    except Exception:
        traceback.print_exc()
        bad_n += 1
    finally:
        cleanup()
        if _saved.get('get_async') is not None:
            try:
                m = sys.modules.get('wl_preview_probe')
                if m is not None:
                    m.win32api.GetAsyncKeyState = _saved['get_async']
                    m.pick_image_file = _saved['pick']
            except Exception:
                pass
        print()
        print('=' * 62)
        print('预览热键 Ctrl+Alt+W ：%d 通过 / %d 失败 / %d 跳过'
              % (ok_n, bad_n, skip_n))
    sys.exit(1 if bad_n else 0)
