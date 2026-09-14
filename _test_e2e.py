# -*- coding: utf-8 -*-
"""端到端：面板改配置 → 主程序在跑 → 确认它真的热重载了。

API 单测只能证明"文件写对了"，证明不了"壁纸收到了"。这个脚本把主程序真拉起来，
改配置，然后去 wallpaper.log 里找它自己说的话 —— 这条通道断了的话，
面板上一切显示正常，桌面上却纹丝不动，是最难发现的一类故障。

跑法：python _test_e2e.py
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

# 中文 Windows 的控制台默认是 GBK，打 ✓/✗ 会直接 UnicodeEncodeError 把脚本
# 打断 —— 而且断在"第一次打印判定结果"那一行，看起来像测试本身挂了。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'wallpaper-config.json')
PIDF = os.path.join(BASE, 'wallpaper.pid')
LOG = os.path.join(BASE, 'wallpaper.log')
WALL = os.path.join(BASE, 'wallpaper.pyw')

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print('  %s %s%s' % ('✓' if ok else '✗', name,
                         ('   ← ' + str(detail)) if (detail and not ok) else ''))


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


loader = importlib.machinery.SourceFileLoader('panel_mod',
                                              os.path.join(BASE, 'panel.pyw'))
spec = importlib.util.spec_from_loader('panel_mod', loader)
P = importlib.util.module_from_spec(spec)
loader.exec_module(P)

BACKUP = open(CFG, encoding='utf-8').read() if os.path.exists(CFG) else None
pre_pid = read_pid()
we_started = False

# ---------------------------------------------------------------- 1. 起主程序
print('【1】启动壁纸主程序')
if alive(pre_pid):
    print('  已经有一个在跑（pid %d），直接拿它测' % pre_pid)
else:
    exe = sys.executable.replace('python.exe', 'pythonw.exe')
    if not os.path.exists(exe):
        exe = sys.executable
    subprocess.Popen([exe, WALL], cwd=BASE,
                     creationflags=0x08000000 | 0x00000008,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    we_started = True
    print('  已拉起，等它写 pid …')

deadline = time.time() + 25
pid = 0
while time.time() < deadline:
    pid = read_pid()
    if pid and alive(pid):
        break
    time.sleep(0.5)

check('主程序起来了', bool(pid) and alive(pid), 'pid=%r' % pid)
time.sleep(3.0)                      # 等它挂上桌面层、把状态写完
tail = log_since(0)
check('日志里有启动完成记录', '启动完成' in tail, tail[-200:])
check('挂载层级正确（DefView 在它上面）',
      '挂载校验通过' in tail, tail[-300:])

# ---------------------------------------------------------------- 2. 起面板服务
httpd = ThreadingHTTPServer(('127.0.0.1', 0), P.Handler)
httpd.daemon_threads = True
PORT = httpd.server_address[1]
P.PANEL_ORIGIN = 'http://127.0.0.1:%d' % PORT
threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.1},
                 daemon=True).start()


def post(path, body=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(P.PANEL_ORIGIN + path, data=data,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


print('\n【2】面板说壁纸的状态')
st = json.loads(urllib.request.urlopen(P.PANEL_ORIGIN + '/api/state',
                                       timeout=20).read().decode('utf-8'))
w = st['wallpaper']
check('/api/state 认出壁纸在跑', w['alive'] and w['pid'] == pid, w)
check('状态里报告"已挂到桌面层"（枚举 Progman 子窗口得到，不是猜的）',
      w['attached'] is True, w)

# ---------------------------------------------------------------- 3. 改参数
print('\n【3】改参数 → 主程序应当即时套用（不重启进程）')
n0 = log_size()
r = post('/api/config', {'size': 512, 'feather': 62, 'glow': 40})
check('接口回话成功', r.get('ok') is True, r)
time.sleep(2.2)                       # 主程序最长 1.5s 才写一条限流日志
new = log_since(n0)
check('日志出现主程序主动报告的参数变更',
      '控制面板：光圈=512' in new and '柔和=62' in new, repr(new[-400:]))
check('改参数没有重启进程（pid 没变）', read_pid() == pid,
      '%r → %r' % (pid, read_pid()))
check('数值确实落在磁盘上',
      json.load(open(CFG, encoding='utf-8'))['size'] == 512)

# ---------------------------------------------------------------- 4. rev
print('\n【4】“重新载入图片”必须真的让主程序重读图')
rev_before = json.load(open(CFG, encoding='utf-8')).get('rev', 0)
n1 = log_size()
post('/api/reload-images')
time.sleep(2.5)
new = log_since(n1)
check('rev 递增', json.load(open(CFG, encoding='utf-8')).get('rev') == rev_before + 1,
      json.load(open(CFG, encoding='utf-8')).get('rev'))
check('日志出现"图片已更新"（主程序真的重读了，不是只改了数字）',
      '图片已更新' in new, repr(new[-400:]))
check('重读之后图片路径没被换掉', '默认图案' not in new.split('图片已更新')[0][-200:],
      repr(new[-300:]))

# ---------------------------------------------------------------- 5. 越界值
print('\n【5】界面传来越界值时主程序不能崩')
n2 = log_size()
r = post('/api/config', {'fps': 9999, 'follow': -3})
time.sleep(1.8)
new = log_since(n2)
check('被夹取后主程序照常工作', alive(read_pid()), 'pid=%r' % read_pid())
check('日志里没有新的异常',
      'Traceback' not in new and '渲染出错' not in new, repr(new[-300:]))
d = json.load(open(CFG, encoding='utf-8'))
check('磁盘上也是夹取后的值', d['fps'] <= 144 and d['follow'] >= 0.02, d)

# ---------------------------------------------------------------- 收尾
print('\n【6】收尾')
httpd.shutdown()
try:
    if BACKUP is not None:
        with open(CFG, 'w', encoding='utf-8') as f:
            f.write(BACKUP)
    print('  配置已还原')
except Exception as e:
    print('  ! 还原失败 %r' % e)
if we_started and alive(pid):
    subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                   capture_output=True)
    print('  已停掉本次由测试启动的壁纸 pid=%d' % pid)
    try:
        os.remove(PIDF)
    except OSError:
        pass
else:
    print('  壁纸是测试前就在跑的，留着不动')

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
for f in FAIL:
    print('  ✗ ' + f)
print('=' * 60)
sys.exit(1 if FAIL else 0)
