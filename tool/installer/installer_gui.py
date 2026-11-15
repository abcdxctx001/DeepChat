# -*- coding: utf-8 -*-
r"""DeepChat 安装器（GUI）。由 PyInstaller 打成单个 setup.exe，内嵌 payload.zip。

设计
----
· 免管理员：默认装到 %LOCALAPPDATA%\DeepChat，卸载项写 HKCU，不需要提权
· 只装运行必需：server\app.exe、client\agent.exe、client 的 GUI/PIL、settings.json 占位
· 安装后：桌面 + 开始菜单快捷方式（指向 server\run.bat），并写卸载信息
· 附带 uninstall.bat（在安装目录里）
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

APP_NAME = 'DeepChat'
VERSION = '2.3.7'
DEFAULT_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), APP_NAME)

# ★ 2026-11-12（用户要求）：安装目录里已经有 DeepChat 时**只替换程序文件**，
#   下面这些是用户自己的数据，一律不覆盖（不存在时才写入出厂默认）。
USER_DATA_FILES = {
    'server/workspace/chat_tree.json',
    'server/workspace/settings.json',
    'server/workspace/memories.idx',
    'server/workspace/memories.idx.meta',
    'server/workspace/session_stats.json',
}
USER_DATA_DIRS = (
    'server/workspace/memories/',
    'server/workspace/uploads/',
    'server/workspace/logs/',
    'server/workspace/node/',
    'client/workspace/memories/',
    'client/workspace/archive_task/',
    'client/workspace/screenshots/',
    'client/workspace/tool_outputs/',
    'client/workspace/temp_exec/',
    'client/workspace/capture/',
)


def is_user_data(arc):
    """该 payload 条目是不是用户数据（升级安装时必须保留）。"""
    a = (arc or '').replace('\\', '/').lstrip('./')
    if a in USER_DATA_FILES:
        return True
    return any(a.startswith(d) for d in USER_DATA_DIRS)


def existing_install(target):
    """目录里是不是已经有一份 DeepChat。"""
    for rel in (('server', 'app.exe'), ('server', 'app.py'),
                ('server', 'workspace', 'chat_tree.json')):
        if os.path.exists(os.path.join(target, *rel)):
            return True
    return False


def file_in_use(path):
    """程序是否正在运行（被占用）。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, 'ab'):
            return False
    except OSError:
        return True


def payload_path():
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, 'payload.zip')


# ============================================================
#  升级前的「优雅停机」（2026-11-13 用户要求）
#
#  以前升级遇到旧进程在跑，直接报「请在托盘上退出后再装」，全靠用户自己动手。
#  现在：先等它把手头的活干完（对话模式不再生成、操作模式没有进行中的会话），
#  再自动关掉、替换程序文件、装快捷方式、启动新进程。
#  判断依据只读旧服务端自己的接口，不去猜：
#    · /api/diag/threads  → 每个会话的 busy / qsize（操作模式）
#    · /api/tree          → 节点的 generating 标记（对话模式还在生成）
#  接口不通（服务没起、只剩托盘面板）视为空闲 —— 没有服务端就没有活会话。
# ============================================================

def _ps_json(script, timeout=20):
    """跑一段 PowerShell 并解析 JSON；失败返回 None（安装流程不该因此中断）。

    ★ 2026-11-13：必须强制 PowerShell 用 UTF-8 输出。Windows PowerShell 5.1 往管道
      写的是 OEM 代码页（简体中文是 936/GBK），而 Python 3.14 默认按 UTF-8 解码，
      遇到中文路径（用户名是中文很常见）直接 UnicodeDecodeError，进程列表就成了空。
    """
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '[Console]::OutputEncoding=[Text.Encoding]::UTF8; ' + script],
            capture_output=True, timeout=timeout,
            creationflags=0x08000000)
        s = (r.stdout or b'').decode('utf-8', 'replace').strip()
        return json.loads(s) if s else None
    except Exception:
        return None


def find_running(target):
    """安装目录下正在运行的 DeepChat 进程：[(pid, name, path)]。

    按 ExecutablePath 前缀过滤 —— 同一个机器上可能装了不止一份 DeepChat，
    只关这一份，不能按镜像名一刀切。
    """
    root = os.path.abspath(target).rstrip('\\/').lower()
    data = _ps_json(
        "Get-CimInstance Win32_Process -Filter \"Name='app.exe' or Name='agent.exe'\" | "
        "Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress")
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for it in data:
        path = str(it.get('ExecutablePath') or '')
        try:
            if path and os.path.abspath(path).lower().startswith(root):
                out.append((int(it.get('ProcessId')), str(it.get('Name')), path))
        except Exception:
            continue
    # 先服务端、后代理：顺序上更接近正常退出
    out.sort(key=lambda x: 0 if x[1].lower() == 'app.exe' else 1)
    return out


def _http_json(url, timeout=1.2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


_prev_qsize = {}


def busy_snapshot(port=5000):
    """旧服务端还有多少活在跑 → (数量, 说明)。0 表示可以安全停机。"""
    base = 'http://127.0.0.1:%d' % port

    # ① 操作模式：先查这个（响应只有几百字节），忙就不用再拉整棵树
    op_busy = 0
    try:
        d = _http_json(base + '/api/diag/threads')
        for s in (d.get('sessions') or []):
            sid = str(s.get('sid') or '')
            q = int(s.get('qsize') or 0)
            prev = _prev_qsize.get(sid)
            _prev_qsize[sid] = q
            if int(s.get('live_children') or 0) > 0:
                op_busy += 1
            elif s.get('busy') and not s.get('parent_exited'):
                # ★ 2026-11-13：busy 也是**持久化**标记 —— 父循环线程已经退出
                #   （parent_exited=True）时它不会复位，会话就成了"永远在忙"的僵尸。
                #   这种不该拦着安装，否则「无限等待」会一直等下去。
                op_busy += 1
            elif q > 0 and prev is not None and q != prev:
                # ★ 队列里还有东西**并且还在变化**才算忙。客户端断开后事件没人取，
                #   qsize 会永远停在一个大数字上（本机实测卡了 1786 条），
                #   只看 qsize>0 会让安装器永远等不到空闲。
                op_busy += 1
        if op_busy:
            return op_busy, '操作模式 %d 个会话仍在处理' % op_busy
    except Exception:
        pass

    # ② 对话模式：有没有节点还在生成（generating 标记）
    try:
        t = _http_json(base + '/api/tree')
        now = time.time()
        live = []
        for n in (t.get('nodes') or {}).values():
            if not n.get('generating'):
                continue
            # ★ generating 是**持久化**标记：页面被关掉、进程崩溃、请求中断之后
            #   它不会自动复位，于是磁盘上永远留着「正在生成」。硬等它会让安装器
            #   永远等不到"空闲"（本机实测就有 3 个这样的僵尸节点）。
            #   只有最近还在更新 last_update 的才算真的在跑。
            try:
                lu = float(n.get('last_update') or n.get('createdAt') or 0)
            except Exception:
                lu = 0
            if lu and (now - lu) < 180:
                live.append(n)
        if live:
            return len(live), '对话模式 %d 个回答仍在生成' % len(live)
    except Exception:
        pass

    return 0, '空闲'


def _pid_alive(pid):
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                           capture_output=True, timeout=10,
                           creationflags=0x08000000)
        # 用字节比对：tasklist 的输出是本地代码页，解码成 str 反而容易踩编码坑
        return str(pid).encode() in (r.stdout or b'')
    except Exception:
        return False


def stop_processes(procs, log=None):
    """关掉这些进程；返回仍然活着的（正常应为空）。"""
    log = log or (lambda msg, pct=None: None)
    for pid, name, _ in procs:
        log('正在关闭 %s（PID %d）…' % (name, pid))
        subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                       capture_output=True, creationflags=0x08000000)
    time.sleep(1.0)
    rest = []
    for pid, name, path in procs:
        if _pid_alive(pid):
            # 再补一刀（有时第一刀撞上进程正在收尾）
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, creationflags=0x08000000)
            rest.append((pid, name, path))
    if rest:
        time.sleep(0.8)
        rest = [p for p in rest if _pid_alive(p[0])]
    return rest


def is_admin():
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def make_shortcut(lnk_path, target, args='', workdir='', icon=''):
    ps = (
        "$W = New-Object -ComObject WScript.Shell; "
        "$s = $W.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; $s.Arguments = '%s'; "
        "$s.WorkingDirectory = '%s'; $s.IconLocation = '%s'; "
        "$s.Save()"
    ) % (lnk_path, target, args, workdir, icon or target)
    subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                   capture_output=True, creationflags=0x08000000)


def install_payload(zp, target, log=None):
    """把 payload.zip 铺到 target。

    已有安装时：程序文件全部覆盖，用户数据（对话树/设置/记忆/上传/日志）原样保留。
    返回 (written, skipped, upgrade)。
    """
    log = log or (lambda msg, pct=None: None)
    upgrade = existing_install(target)

    if upgrade:
        for rel in (('server', 'app.exe'), ('client', 'agent.exe')):
            if file_in_use(os.path.join(target, *rel)):
                raise RuntimeError(
                    '程序文件仍被占用，无法替换：\n%s\n\n'
                    '通常是还有 DeepChat 进程没退干净（或杀毒软件正在扫描）。\n'
                    '请稍后重试；仍不行就在任务管理器里结束 app.exe / agent.exe。\n'
                    '你现有的对话记录 / 设置 / 记忆不会被改动。'
                    % os.path.join(target, *rel))

    os.makedirs(target, exist_ok=True)
    written = skipped = 0
    with zipfile.ZipFile(zp) as z:
        files = [i for i in z.infolist() if not i.is_dir()]
        total = max(1, len(files))
        for n, info in enumerate(files, 1):
            arc = info.filename
            dest = os.path.join(target, arc.replace('/', os.sep))
            # 已有安装 + 属于用户数据 + 目标已存在 → 保留
            if upgrade and is_user_data(arc) and os.path.exists(dest):
                skipped += 1
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out)
                written += 1
            if n % 20 == 0 or n == total:
                log('正在安装 %d / %d（保留用户数据 %d 项）' % (n, total, skipped),
                    n / total * 85)
    return written, skipped, upgrade


UNINSTALL_BAT = r'''@echo off
chcp 65001 >nul
echo 正在卸载 DeepChat ...
set "DIR=%~dp0"
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\DeepChat" /f >nul 2>nul
if exist "%USERPROFILE%\Desktop\DeepChat.lnk" del /q "%USERPROFILE%\Desktop\DeepChat.lnk"
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\DeepChat.lnk" del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\DeepChat.lnk"
cd /d "%TEMP%"
rmdir /s /q "%DIR%" 2>nul
echo 已卸载。工作区数据（对话树/记忆/设置）已随目录一起删除。
pause
'''


class Installer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('DeepChat %s 安装程序' % VERSION)
        self.geometry('600x400')
        self.resizable(True, False)   # 允许横向拉伸，避免底行按钮被裁掉
        tk.Label(self, text='DeepChat %s' % VERSION, font=('Segoe UI', 16, 'bold')).pack(pady=(16, 2))
        tk.Label(self, text='树状对话 + 操作模式 AI 工作台', fg='#555').pack()

        row = tk.Frame(self)
        row.pack(fill='x', padx=18, pady=12)
        tk.Label(row, text='安装到：').pack(side='left')
        self.path_var = tk.StringVar(value=DEFAULT_DIR)
        tk.Entry(row, textvariable=self.path_var).pack(side='left', fill='x', expand=True, padx=6)
        tk.Button(row, text='浏览…', command=self.browse).pack(side='left')

        opt = tk.Frame(self)
        opt.pack(fill='x', padx=18)
        self.desktop_var = tk.BooleanVar(value=True)
        self.startmenu_var = tk.BooleanVar(value=True)
        self.launch_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt, text='创建桌面快捷方式', variable=self.desktop_var).pack(anchor='w')
        tk.Checkbutton(opt, text='创建开始菜单快捷方式', variable=self.startmenu_var).pack(anchor='w')
        tk.Checkbutton(opt, text='安装完成后启动', variable=self.launch_var).pack(anchor='w')

        self.bar = ttk.Progressbar(self, length=520)
        self.bar.pack(padx=18, pady=(14, 4))
        self.status = tk.Label(self, text='准备就绪', fg='#444', anchor='w')
        self.status.pack(fill='x', padx=20)

        btns = tk.Frame(self)
        btns.pack(fill='x', padx=18, pady=12)
        self.install_btn = tk.Button(btns, text='安装', width=12, command=self.start_install)
        self.install_btn.pack(side='right')
        tk.Button(btns, text='退出', width=8, command=self.destroy).pack(side='right', padx=8)

    def browse(self):
        d = filedialog.askdirectory(initialdir=os.path.dirname(self.path_var.get()) or os.path.expanduser('~'))
        if d:
            self.path_var.set(os.path.join(d, APP_NAME))

    def log(self, msg, pct=None):
        self.status.config(text=msg)
        if pct is not None:
            self.bar['value'] = pct
        self.update_idletasks()

    def start_install(self):
        target = self.path_var.get().strip()
        if target and existing_install(target):
            # ★ 2026-11-12：升级安装，明确告知只换程序、不动数据
            if not messagebox.askyesno(
                    '检测到已有安装',
                    '该目录已存在 DeepChat：\n%s\n\n'
                    '升级安装只会替换程序文件（app.exe / client.zip / 界面等），\n'
                    '你的对话记录、设置、记忆都会原样保留。\n\n继续升级？' % target):
                return
        self.install_btn.config(state='disabled')
        threading.Thread(target=self.do_install, daemon=True).start()

    def _ask_while_busy(self, detail):
        """在工作线程里请主线程弹一个「等待中」窗口，**不超时**。

        ★ 2026-11-13（按用户要求）：以前是"等满 5 分钟再弹 是/否/取消"，
          既不直观又可能在长任务中途强制结束。现在：
            · 窗口一直开着，显示当前在忙什么 + 已等多久；
            · 「⚡ 立刻结束并继续安装」= 用户自己决定不等了；
            · 「✕ 取消安装」= 放弃本次安装；
            · 连接/任务全部结束时窗口**自动关闭**并继续安装（这就是"无限等待"）。

        返回 'idle'（已空闲）/ 'kill'（用户要求立刻结束）/ 'cancel'。
        窗口只在主线程创建，工作线程通过 Event 等待 —— 避免跨线程操作 Tk。
        """
        ev = threading.Event()
        box = {'v': 'cancel'}
        t0 = time.time()

        def build():
            win = tk.Toplevel(self)
            win.title('等待 DeepChat 完成任务')
            win.transient(self)
            win.grab_set()
            win.resizable(False, False)
            tk.Label(win, text='DeepChat 还在工作，正在等它把手头的任务做完…',
                     font=('Segoe UI', 10, 'bold')).pack(padx=22, pady=(16, 6))
            info = tk.Label(win, text=detail, justify='left', fg='#444', wraplength=470)
            info.pack(padx=22, pady=(0, 4))
            tk.Label(win, text='这一步不会超时：连接全部结束后会自动继续安装。',
                     fg='#777', font=('Segoe UI', 9)).pack(padx=22, pady=(0, 2))
            tk.Label(win, text='如果上面是已经关掉页面的旧会话，直接点「立刻结束」即可。',
                     fg='#999', font=('Segoe UI', 9)).pack(padx=22, pady=(0, 10))
            btns = tk.Frame(win)
            btns.pack(padx=22, pady=(0, 16))

            def pick(v):
                if ev.is_set():
                    return
                box['v'] = v
                try:
                    win.destroy()
                except Exception:
                    pass
                ev.set()

            tk.Button(btns, text='⚡ 立刻结束并继续安装', width=22,
                      command=lambda: pick('kill')).pack(side='left', padx=5)
            tk.Button(btns, text='✕ 取消安装', width=12,
                      command=lambda: pick('cancel')).pack(side='left', padx=5)
            win.protocol('WM_DELETE_WINDOW', lambda: pick('cancel'))

            def tick():
                if ev.is_set():
                    return
                _busy, d = busy_snapshot()
                if not _busy:
                    pick('idle')
                    return
                waited = int(time.time() - t0)
                wtxt = ('已等待 %d 秒' % waited) if waited < 60 else \
                       ('已等待 %d 分 %02d 秒' % (waited // 60, waited % 60))
                try:
                    info.config(text='%s\n\n%s' % (d, wtxt))
                except Exception:
                    return
                win.after(1000, tick)

            win.after(300, tick)

        try:
            self.after(0, build)
        except Exception:
            return 'cancel'
        ev.wait()          # 无限等待：空闲、用户点按钮、或窗口被关
        return box['v']

    def _graceful_stop(self, procs):
        """等旧进程把手头的活干完，再关掉它。返回 False = 用户取消/关不掉。

        ★ 2026-11-13（用户："只能写死 5min 后重启，最好给几个按钮"）：
          不再有 5 分钟硬超时。只采样一次：忙就**立刻**弹「等待中」窗口
          （窗口自己会一直等，任务结束自动继续），不忙就直接往下走。

        ★ 2026-11-13（用户："一开就是 20s？"）：原来先静默等 20 秒再弹窗，
          主窗口只剩一行"最多再等 19 秒"的倒计时，既不知道在等什么、也没法立刻结束。
          现在去掉这段：一检测到在忙就弹窗，让用户第一时间看到详情和按钮。
        """
        names = '、'.join('%s(PID %d)' % (p[1], p[0]) for p in procs)
        self.log('检测到 DeepChat 正在运行（%s）…' % names, 2)
        busy, detail = busy_snapshot()
        if busy:
            self.log('等待中：%s（窗口里可随时「立刻结束」）' % detail, 3)
            act = self._ask_while_busy(detail)
            if act == 'cancel':
                self.log('已取消安装', 0)
                return False
            # 'kill' = 用户要求立刻结束；'idle' = 已空闲，直接往下走

        self.log('正在关闭旧进程…', 6)
        rest = stop_processes(procs, self.log)
        if rest:
            messagebox.showwarning(
                '部分进程未能关闭',
                '以下进程仍在运行：\n' +
                '\n'.join('  %s（PID %d）' % (n, p) for p, n, _ in rest) +
                '\n\n请在任务管理器中结束它们后重试。')
            return False
        return True

    def do_install(self):
        try:
            target = self.path_var.get().strip()
            if not target:
                raise RuntimeError('请选择安装目录')
            zp = payload_path()
            if not os.path.exists(zp):
                raise RuntimeError('安装包损坏：缺 payload.zip')

            # ★ 2026-11-13：升级时先优雅停机（等对话生成完 / 操作模式会话结束），
            #   再自动关闭旧进程；用户不必自己去托盘上退出。
            old_closed = False
            if existing_install(target):
                running = find_running(target)
                if running:
                    if not self._graceful_stop(running):
                        self.install_btn.config(state='normal')
                        return
                    old_closed = True

            written, skipped, upgrade = install_payload(zp, target, self.log)

            self.log('写入卸载信息…', 88)
            with open(os.path.join(target, 'uninstall.bat'), 'w', encoding='utf-8') as f:
                f.write(UNINSTALL_BAT)

            run_bat = os.path.join(target, 'server', 'run.bat')
            icon = os.path.join(target, 'server', 'static', 'icon.ico')
            if not os.path.exists(icon):
                icon = os.path.join(target, 'server', 'app.exe')
            self.log('创建快捷方式…', 92)
            if self.desktop_var.get():
                make_shortcut(os.path.join(os.path.expanduser('~'), 'Desktop', 'DeepChat.lnk'),
                              run_bat, '', os.path.dirname(run_bat), icon)
            if self.startmenu_var.get():
                sm = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows',
                                  'Start Menu', 'Programs', 'DeepChat.lnk')
                os.makedirs(os.path.dirname(sm), exist_ok=True)
                make_shortcut(sm, run_bat, '', os.path.dirname(run_bat), icon)

            # HKCU 卸载项（免管理员）
            try:
                import winreg
                k = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                     r'Software\Microsoft\Windows\CurrentVersion\Uninstall\DeepChat')
                for name, val in (('DisplayName', APP_NAME),
                                  ('DisplayVersion', VERSION),
                                  ('InstallLocation', target),
                                  ('UninstallString', '"%s"' % os.path.join(target, 'uninstall.bat')),
                                  ('Publisher', 'DeepChat')):
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
                winreg.SetValueEx(k, 'NoModify', 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, 'NoRepair', 0, winreg.REG_DWORD, 1)
                winreg.CloseKey(k)
            except Exception:
                pass

            self.log('安装完成', 100)
            if self.launch_var.get():
                # ★ 2026-11-08：改用 shell 关联打开 .bat，避免引号/网络路径问题
                try:
                    os.startfile(run_bat)          # type: ignore[attr-defined]
                except Exception:
                    subprocess.Popen('"%s"' % run_bat, cwd=os.path.dirname(run_bat), shell=True)

            if upgrade:
                detail = ('· 更新了 %d 个程序文件\n'
                          '· 保留了 %d 项你的数据（对话记录 / 设置 / 记忆）' % (written, skipped))
                if old_closed:
                    detail += '\n· 已等旧进程空闲后自动关闭并重启'
                head = 'DeepChat 已升级到 %s' % VERSION
            else:
                detail = '· 安装 %d 个文件' % written
                head = 'DeepChat %s 已安装' % VERSION
            messagebox.showinfo('完成', '%s\n\n%s\n%s\n\n启动方式：桌面「DeepChat」快捷方式。'
                                % (head, target, detail))
            self.destroy()
        except Exception as e:
            messagebox.showerror('安装失败', str(e))
            self.install_btn.config(state='normal')


if __name__ == '__main__':
    if not os.path.exists(payload_path()):
        tk.Tk().withdraw()
        messagebox.showerror('安装包损坏', '找不到内置 payload.zip')
        sys.exit(1)
    Installer().mainloop()
