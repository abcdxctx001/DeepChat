"""DeepChat 控制面板 —— server 与 client **共用**的控制模块。

★ 这不是一个外部小工具，而是产品自带的功能：
    · server（app.exe / app.py）能同时控制「服务端」和「本机代理」
    · client（agent.exe / agent.py）只能控制自己
  两个程序都支持 `--control` 参数打开本面板；run.bat 也走同一条路。

架构约束（见根目录 AGENTS.md）：本模块只是**进程管理器**，不参与
浏览器 ↔ server ↔ agent 之间的任何数据中转，不改变原有架构。

为什么单独一个模块而不是塞进 app.py / agent.py：
  · 两个程序共用同一份实现，避免复制两份后逐渐走样；
  · tkinter/pystray 属于可选能力，服务进程正常跑时根本不会 import 到它们。
"""
import ctypes
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# 匹配 ANSI 转义序列（颜色/光标控制），用于净化日志显示
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')

CREATE_NO_WINDOW = 0x08000000


def _run_flags():
    return CREATE_NO_WINDOW if os.name == 'nt' else 0


def port_open(port, host='127.0.0.1'):
    """端口上是否已经有服务在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


class Service:
    """一个被管理的子进程。

    spec 字典字段：
        key       标识（也用作日志文件名）
        label     界面显示名
        port      监听端口
        env_ports 端口要写进哪些环境变量（server 用 PORT，agent 用 AGENT_PORT）
        argv      启动参数；None/[] 表示「用当前这个 exe 再起一个自己」
        cwd       工作目录
        exe       可执行文件路径（None 表示用当前解释器/当前 exe）
        script    源码模式下的脚本名（exe 不存在时用）
    """

    def __init__(self, spec, log_dir):
        self.key = spec['key']
        self.label = spec['label']
        self.port = spec['port']
        self.env_ports = spec.get('env_ports') or ['PORT']
        self.argv = spec.get('argv') or []
        self.cwd = spec['cwd']
        self.exe = spec.get('exe')
        self.script = spec.get('script')
        self.log_dir = log_dir
        self.proc = None
        self._fh = None

    @property
    def log_path(self):
        return os.path.join(self.log_dir, f'{self.key}.log')

    @property
    def pid_path(self):
        return os.path.join(self.log_dir, f'{self.key}.pid')

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def externally_running(self):
        """端口上有服务、但不是本面板起的。

        ★ 2026-10-19：控制面板的**单实例锁已去掉**，所以"会不会重复拉起服务"
          完全靠这个探测来防 —— 端口已被占用时不再启动，界面上显示"端口已占用"。
        """
        return (not self.running()) and port_open(self.port)

    def _build_cmd(self):
        """决定用什么命令启动。

        ★ 优先「用当前 exe 再起一个自己」：打包后 app.exe / agent.exe 就在手边，
          没必要依赖源码。这样控制面板拉起的服务，和用户直接双击 exe 跑的是同一个。

        ★ 源码模式用 **pythonw.exe**（不是 python.exe）：python.exe 是控制台程序，
          Windows 必然给它挂一个黑窗口 —— 而"不要黑窗口"正是控制面板存在的理由。
          没找到 pythonw 才退回 python。
        """
        if self.exe and os.path.isfile(self.exe):
            return [self.exe] + list(self.argv), self.cwd
        if self.script:
            script = os.path.join(self.cwd, self.script)
            if os.path.isfile(script):
                py = sys.executable or 'python'
                try:
                    cand = os.path.join(os.path.dirname(py), 'pythonw.exe')
                    if os.path.isfile(cand):
                        py = cand
                except Exception:
                    pass
                # ★ 顺序必须是 [解释器, 脚本, 参数…]。
                #   写成 [解释器, 参数…, 脚本] 会被 Python 当成解释器选项 ——
                #   实测报 "unknown option --serve"、退出码 2（面板点启动直接失败）。
                return [py, script] + list(self.argv), self.cwd
        return None, None

    def start(self):
        if self.running():
            return True, '已在运行'
        if self.externally_running():
            return True, f'端口 {self.port} 已被占用（非本面板启动）'
        cmd, cwd = self._build_cmd()
        if not cmd:
            return False, '找不到可执行文件（既没有 exe 也没有源码）'
        os.makedirs(self.log_dir, exist_ok=True)
        env = dict(os.environ)
        for v in self.env_ports:
            env[v] = str(self.port)
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        try:
            # 日志覆盖写：追加会把上一次（甚至别的端口）的旧输出留在文件里，
            # 实测面板上出现过「卡片写端口 5000、日志却是 5003」的误导。
            self._fh = open(self.log_path, 'w', encoding='utf-8', errors='replace')
            self._fh.write(f'===== {time.strftime("%Y-%m-%d %H:%M:%S")} 启动 {self.label} '
                           f'（端口 {self.port}）=====\n')
            self._fh.flush()
            self.proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=self._fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=_run_flags())
            with open(self.pid_path, 'w', encoding='utf-8') as f:
                f.write(str(self.proc.pid))
        except Exception as e:
            import traceback
            return False, f'启动失败: {e}\n{traceback.format_exc()}'
        # 等服务真正起来。
        # ★ 源码模式（pythonw 起 app.py）实测需要 2~4 秒：要导入 flask 全家桶、
        #   初始化模板等。慢机器/杀软实时扫描下可能更久，所以给到 45 秒。
        #   （实测曾出现 20 秒上限不够、被误报成"端口尚未就绪"的情况。）
        for _ in range(90):
            time.sleep(0.5)
            if port_open(self.port):
                return True, '已启动'
            if self.proc.poll() is not None:
                return False, f'进程已退出（返回码 {self.proc.returncode}），请看日志'
        return True, '已启动（端口尚未就绪，可能仍在初始化）'

    def stop(self):
        """停止服务。

        ★ PyInstaller 的 **onefile** exe 启动后会再 fork 一个子进程跑真正的逻辑
          （父进程只是引导器）。只结束父进程会**留下孤儿子进程继续占着端口** ——
          表现为"点了停止但端口还在、再启动就报端口被占用"。
          所以必须连整棵进程树一起停。
        """
        if not self.running():
            if self.externally_running():
                return False, '该服务不是本面板启动的，未做处理'
            self._cleanup_orphans()
            return True, '本来就没在运行'
        pid = self.proc.pid
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
            else:
                self.proc.terminate()
            for _ in range(20):
                time.sleep(0.25)
                if self.proc.poll() is not None:
                    break
        except Exception as e:
            return False, f'停止失败: {e}'
        finally:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None
            self.proc = None
        for _ in range(20):
            if not port_open(self.port):
                break
            time.sleep(0.25)
        self._cleanup_orphans()
        return True, '已停止'

    def _cleanup_orphans(self):
        """清掉上次崩溃/强杀留下的 pid 记录与孤儿进程。"""
        try:
            if not os.path.isfile(self.pid_path):
                return
            with open(self.pid_path, encoding='utf-8') as f:
                old = int((f.read() or '0').strip() or 0)
            if old > 0 and old != os.getpid() and os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(old), '/T', '/F'],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        finally:
            try:
                os.remove(self.pid_path)
            except Exception:
                pass

    def tail(self, lines=80):
        """读日志尾部（**只读文件末尾一小段**，不读整个文件）。

        ① 只读尾部：早先 `open().read()` 读整个文件，server 日志可长到 2 MB 以上，
           实测单次 370ms、每 2 秒在主线程跑一次（占 18.5%），表现为窗口拖动卡顿。
           现在按行数估算字节数用 seek 从末尾读，实测 3ms。

        ② 剥 ANSI 转义码：Flask 的日志带颜色控制符，tkinter Text 不渲染颜色，
           只会满屏乱码。

        ③ 编码自适应：日志可能是 UTF-8 也可能是本地编码（中文 Windows 上 GBK）。
           实测两种都出现过，所以两种都解一遍，取坏字符更少的那个。
        """
        try:
            size = os.path.getsize(self.log_path)
        except Exception:
            return '(暂无日志)'
        want = min(max(lines, 1) * 240, 256 * 1024)
        start = max(0, size - want)
        try:
            with open(self.log_path, 'rb') as f:
                if start:
                    f.seek(start)
                raw = f.read()
        except Exception:
            return '(暂无日志)'
        text = ANSI_RE.sub('', self._decode_log(raw))
        out = text.splitlines(keepends=True)[-lines:]
        if start and out:
            out = out[1:]          # 首行可能是半行
        return ''.join(out)

    @staticmethod
    def _decode_log(raw):
        cands = []
        for enc in ('utf-8', 'gbk'):
            try:
                s = raw.decode(enc)
            except UnicodeDecodeError:
                s = raw.decode(enc, 'replace')
            bad = s.count('\ufffd') + sum(1 for ch in s if ord(ch) < 32 and ch not in '\r\n\t')
            cands.append((bad, s))
        cands.sort(key=lambda t: t[0])
        return cands[0][1]


# ============================================================
#  图形界面
# ============================================================

def can_show_gui():
    """能否显示图形界面（tkinter 可用且不是无显示环境）。"""
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def autostart_services(services, start, say=None):
    """把**还没在跑**的服务拉起来，返回 (启动的 key 列表, 说明文字)。

    放在模块级（而不是面板类里）有两个原因：一是不依赖 tkinter，能被单元测试直接
    调用；二是 server / client 两边行为一致。

    三种情况分开处理，避免重复启动：
      · 本面板已经拉起、进程还活着 -> 不动
      · 端口上已有服务（端口已占用） -> 不动，只在状态栏说明
      · 都没有                     -> 启动（通过传入的 start 回调）
    """
    todo, notes = [], []
    for key, svc in services.items():
        try:
            if svc.running():
                notes.append(f'{svc.label} 运行中')
            elif svc.externally_running():
                notes.append(f'{svc.label} 端口已占用（端口 {svc.port}）')
            else:
                todo.append(key)
        except Exception as e:
            notes.append(f'{svc.label} 状态检查失败：{e}')
    for key in todo:
        start(key)
    if say:
        if notes:
            say('；'.join(notes))
        elif todo:
            say('正在自动启动服务 ...')
    return todo, notes


def run_control_panel(services_spec, title='DeepChat 控制面板',
                      state_dir=None, open_url=None, single_instance=True,
                      autostart=True):
    """打开控制面板。services_spec 是 Service 规格列表。

    server 传入两项（服务端 + 本机代理），client 只传自己那一项。

    state_dir：放 logs/ 的目录。
      ★ 必须由调用方显式给出：打包成 onefile exe 后本模块位于 PyInstaller 的
        临时解压目录（_MEIPASS），用 __file__ 推导会把日志写进临时目录、退出即丢。
        调用方传 BASE_DIR（exe 所在目录 / 源码目录）才对。

    autostart：面板显示出来后，自动把**还没在跑**的服务拉起来。
      ★ 默认开着：用户双击 run.bat / exe 的期望就是"马上能用"，不该还要再点一下
        「启动」。已经在跑的（本面板启动的，或端口上已有外部实例）不动，
        避免重复拉起把端口占成两份。
    """
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    # ★ 2026-10-19（按用户要求）：**去掉单实例锁** —— 允许同时打开多个控制面板。
    #   防重复拉起服务不再依赖互斥体，而是靠**端口探测**（port_open /
    #   Service.externally_running）：端口已被占用时不会再次启动该服务，
    #   状态里显示"端口已占用"。
    #   （single_instance 参数保留仅为兼容既有调用方签名，不再产生拦截效果。）
    _ = single_instance

    # 图标就在本模块旁边。
    # ★ 本文件是"唯一源码"，打包/使用时会同步成 server\control.py 与 client\control.py，
    #   图标（icon.ico / icon.png / tray.png）也跟着一起放，所以不再依赖
    #   项目根目录下某个额外的 launcher\ 目录 —— 那样会把 client 单独分发时搞坏
    #   （实测：只复制 client 目录时控制面板报 "No module named 'launcher'"）。
    # 图标统一放在 static\\ 下（两端目录结构一致，根目录不再散落文件）。
    mod_dir = os.path.dirname(os.path.abspath(__file__))
    _icon_dirs = [os.path.join(mod_dir, 'static'), mod_dir]
    if state_dir:
        _icon_dirs.append(os.path.join(state_dir, 'static'))

    def _icon_file(name):
        for _d in _icon_dirs:
            _p = os.path.join(_d, name)
            if os.path.isfile(_p):
                return _p
        return None

    log_dir = os.path.join(state_dir or mod_dir, 'logs')

    # ★ 就绪信号：GUI 真正显示出来之后写一个标记文件。
    #   为什么需要它：exe 是无控制台的（--noconsole），双击时 Windows 不会有任何
    #   反馈，用户要等好几秒才看到窗口，很容易以为"打开失败了"。
    #   run.bat 会留在控制台里显示"正在启动…"，轮询到这个标记再消失 ——
    #   这样"黑窗口消失"就等于"GUI 已经出来了"，不会让人误判。
    ready_file = os.path.join(log_dir, 'panel_ready')

    def _signal_ready():
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(ready_file, 'w', encoding='utf-8') as f:
                f.write(f'{os.getpid()} {time.time()}\n')
        except Exception:
            pass
        # ★ 2026-10-23：正常退出时删掉该标记。
        #   否则进程关掉后标记还留着，run.bat 会拿这个**已被回收的 PID**
        #   去 tasklist 查 —— 命中的可能是完全无关的进程（实测成了 svchost），
        #   于是误判"面板已开着"直接退出，表现为 run.bat 打不开。
        try:
            import atexit

            def _drop_ready():
                try:
                    os.remove(ready_file)
                except Exception:
                    pass

            atexit.register(_drop_ready)
        except Exception:
            pass

    class Panel(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(title)
            self.geometry('780x580')
            self.minsize(680, 500)
            self._icon()
            self._tray = None
            self._closing_to_tray = False
            self._log_sig = {}
            self._log_text = {}
            # ★ 2026-10-19：日志按文件偏移增量读取 + Text 行数上限（治卡）
            self._log_off = {}
            self._ui_q = queue.Queue()      # 工作线程 → 主线程 的回调队列（Tk 不可跨线程）
            self._row_sig = {}
            self._last_geom = None
            self._autostart = autostart
            self._autostarted = False
            self.services = {s['key']: Service(s, log_dir) for s in services_spec}
            self._build(open_url)
            self._refresh()
            self.protocol('WM_DELETE_WINDOW', self.on_close)
            # 窗口已经构建完成 —— 但要等它真正映射到屏幕才算"看得见"，
            # 所以再等一拍（见 run() 里的 after(…, _signal_ready)）。
            try:
                self.update_idletasks()
                self.deiconify()
                self.lift()
            except Exception:
                pass
            # 自动拉起服务：放在窗口显示之后，让界面先出来，不至于"点了没反应"。
            if self._autostart:
                self.after(200, self._autostart_services)

        # ---------- 外观 ----------
        def _icon(self):
            for name in ('icon.ico', 'icon.png'):
                p = _icon_file(name)
                if os.path.isfile(p):
                    try:
                        if p.endswith('.ico'):
                            self.iconbitmap(p)
                        else:
                            self._img = tk.PhotoImage(file=p)
                            self.iconphoto(True, self._img)
                        return
                    except Exception:
                        pass

        def _build(self, open_url):
            self._open_url = open_url
            style = ttk.Style(self)
            try:
                style.theme_use('vista')
            except Exception:
                pass

            head = ttk.Frame(self, padding=(14, 12, 14, 6))
            head.pack(fill='x')
            ttk.Label(head, text='DeepChat', font=('Microsoft YaHei UI', 15, 'bold')).pack(side='left')
            ttk.Label(head, text='  本地 AI 代理控制面板',
                      foreground='#6b7280').pack(side='left', pady=(4, 0))

            body = ttk.Frame(self, padding=(14, 4, 14, 8))
            body.pack(fill='x')
            self.rows = {}
            for key, svc in self.services.items():
                card = ttk.LabelFrame(body, text=svc.label, padding=(10, 8))
                card.pack(fill='x', pady=4)
                dot = tk.Canvas(card, width=12, height=12, highlightthickness=0)
                dot.pack(side='left', padx=(0, 8))
                did = dot.create_oval(1, 1, 11, 11, fill='#9ca3af', outline='')
                info = ttk.Label(card, text='', width=32, anchor='w')
                info.pack(side='left')
                bstop = ttk.Button(card, text='停止', width=8,
                                   command=lambda k=key: self.do_stop(k))
                bstop.pack(side='right', padx=3)
                bstart = ttk.Button(card, text='启动', width=8,
                                    command=lambda k=key: self.do_start(k))
                bstart.pack(side='right', padx=3)
                self.rows[key] = dict(dot=dot, did=did, info=info, start=bstart, stop=bstop)

            act = ttk.Frame(self, padding=(14, 0, 14, 8))
            act.pack(fill='x')
            if open_url:
                ttk.Button(act, text='🌐 打开界面', command=self.open_ui).pack(side='left', padx=(0, 6))
            ttk.Button(act, text='▶ 全部启动', command=self.start_all).pack(side='left', padx=6)
            ttk.Button(act, text='■ 全部停止', command=self.stop_all).pack(side='left', padx=6)
            ttk.Button(act, text='📂 打开日志目录', command=self.open_logs).pack(side='left', padx=6)
            ttk.Button(act, text='🗕 最小化到托盘', command=self.hide_to_tray).pack(side='right')

            box = ttk.LabelFrame(self, text='运行日志', padding=(6, 4))
            box.pack(fill='both', expand=True, padx=14, pady=(0, 6))
            self.log = scrolledtext.ScrolledText(box, height=14, wrap='none',
                                                 font=('Consolas', 9), background='#0f172a',
                                                 foreground='#d1d5db', insertbackground='#d1d5db')
            self.log.pack(fill='both', expand=True)

            pick = ttk.Frame(self, padding=(14, 0, 14, 4))
            pick.pack(fill='x')
            ttk.Label(pick, text='日志来源：').pack(side='left')
            self.log_src = tk.StringVar(value=next(iter(self.services)))
            for k, svc in self.services.items():
                ttk.Radiobutton(pick, text=svc.label, value=k, variable=self.log_src,
                                command=self._reload_log).pack(side='left', padx=4)
            self.autoscroll = tk.BooleanVar(value=True)
            ttk.Checkbutton(pick, text='自动刷新', variable=self.autoscroll).pack(side='right')

            self.status = ttk.Label(self, text='就绪', padding=(14, 4, 14, 10), foreground='#374151')
            self.status.pack(fill='x')

        # ---------- 行为 ----------
        def say(self, msg):
            try:
                self.status.config(text=f'{time.strftime("%H:%M:%S")}  {msg}')
            except Exception:
                pass

        def do_start(self, key):
            svc = self.services[key]
            self.say(f'正在启动 {svc.label} ...')
            def _work():
                r = svc.start()
                # ★ 只入队，不碰 Tk
                self._ui_q.put((svc.label, r[1]))
            threading.Thread(target=_work, daemon=True).start()

        def do_stop(self, key):
            svc = self.services[key]
            self.say(f'正在停止 {svc.label} ...')
            def _work():
                r = svc.stop()
                # ★ 只入队，不碰 Tk（跨线程调 after 会卡死主循环）
                self._ui_q.put((svc.label, r[1]))
            threading.Thread(target=_work, daemon=True).start()

        def start_all(self):
            for k in self.services:
                self.do_start(k)

        def _autostart_services(self):
            """面板一出来就把服务拉起来（用户双击的期望就是"马上能用"）。

            实际逻辑在模块级的 autostart_services()，这里只做一次性保护。
            """
            if self._autostarted:
                return
            self._autostarted = True
            autostart_services(self.services, self.do_start, self.say)

        def stop_all(self):
            for k in self.services:
                self.do_stop(k)

        def open_ui(self):
            url = self._open_url
            if isinstance(url, str):
                webbrowser.open(url)
            elif callable(url):
                webbrowser.open(url())

        def open_logs(self):
            os.makedirs(log_dir, exist_ok=True)
            try:
                os.startfile(log_dir)
            except Exception:
                pass

        def _reload_log(self, force=False):
            if not force and not self.autoscroll.get():
                return
            svc = self.services.get(self.log_src.get())
            if not svc:
                return
            # ★ 2026-10-19（治"奇卡"）：改为按**文件偏移增量读取**。
            #   旧做法取 tail()（最后 N 行）再与上次文本比较前缀 ——
            #   日志一旦超过 N 行，窗口起点每次都在变，startswith 恒 False，
            #   于是每 1.5 秒整块 delete+insert，越用越卡。
            try:
                size = os.path.getsize(svc.log_path)
            except Exception:
                size = 0
            off = self._log_off.get(svc.key, -1)
            try:
                at_bottom = self.log.yview()[1] >= 0.999
            except Exception:
                at_bottom = True
            # 增量路径：偏移有效且文件只增长
            if (not force) and off >= 0 and size > off:
                raw, chunk = b'', ''
                try:
                    with open(svc.log_path, 'rb') as f:
                        f.seek(off)
                        raw = f.read(min(size - off, 512 * 1024))
                    chunk = ANSI_RE.sub('', svc._decode_log(raw))
                except Exception:
                    raw, chunk = b'', ''
                self._log_off[svc.key] = off + len(raw)
                if chunk:
                    self.log.insert('end', chunk)
                    self._trim_log()
                    if at_bottom:
                        self.log.see('end')
                return
            if (not force) and off >= 0 and size == off:
                return                      # 无新内容
            # 全量路径：首次、切换来源、force、或文件被截断
            text = svc.tail()
            self._log_off[svc.key] = size
            self._log_sig[svc.key] = (size, 0)
            self._log_text[svc.key] = text
            # 走到这里说明是"首次 / 切源 / force / 文件被截断" → 整体重建一次
            self.log.delete('1.0', 'end')
            self.log.insert('1.0', text)
            self._trim_log()
            if at_bottom or force:
                self.log.see('end')

        _MAX_LOG_LINES = 3000      # Text 里最多保留的行数（超出裁掉头部）

        def _trim_log(self):
            """限制日志控件行数，避免无限增长导致插入/滚动越来越慢。"""
            try:
                last = int(self.log.index('end-1c').split('.')[0])
                if last > self._MAX_LOG_LINES:
                    self.log.delete('1.0', f'{last - self._MAX_LOG_LINES + 1}.0')
            except Exception:
                pass

        def _drain_ui_q(self):
            """主线程里消费工作线程投递的结果（替代跨线程 self.after）。

                ★ 一次把队列清空：stop_all 会一次入队多条（每个服务一条），
                  只处理一条会让其余结果延后一拍才显示。
            """
            got = False
            for _ in range(20):
                try:
                    label, msg = self._ui_q.get_nowait()
                except queue.Empty:
                    break
                got = True
                try:
                    self.say(f'{label}：{msg}')
                except Exception:
                    pass
            return got

        def _refresh(self, force_log=False):
            # ★ 先处理工作线程（启动/停止）的结果，全部在主线程执行
            if self._drain_ui_q():
                force_log = True
            # ★ 正在拖动 / 缩放窗口时跳过刷新。
            #   Windows 在拖动窗口期间会进入模态循环，此时任何主线程工作都会
            #   直接表现为"窗口跟不上鼠标"。状态晚 1.5 秒更新没人会察觉，
            #   但拖动卡顿非常明显 —— 所以宁可这一拍什么都不做。
            if not force_log:
                try:
                    if self._dragging():
                        self.after(400, self._refresh)
                        return
                except Exception:
                    pass
            for key, svc in self.services.items():
                row = self.rows[key]
                if svc.running():
                    color, text = '#22c55e', f'运行中  pid={svc.proc.pid}  端口 {svc.port}'
                elif svc.externally_running():
                    color, text = '#f59e0b', f'端口已占用  端口 {svc.port}'
                else:
                    color, text = '#9ca3af', f'未运行  端口 {svc.port}'
                # ★ 只在状态真的变了才碰控件。
                #   tkinter 每次 config()/state()/itemconfig() 都会触发布局与重绘，
                #   而状态绝大多数时间是不变的 —— 无条件设置等于每 1.5 秒白刷一遍
                #   整个窗口，拖动时就会顿。
                sig = (color, text,
                       bool(svc.running() or svc.externally_running()),
                       bool(svc.running()))
                if self._row_sig.get(key) != sig:
                    self._row_sig[key] = sig
                    row['dot'].itemconfig(row['did'], fill=color)
                    row['info'].config(text=text)
                    row['start'].state(['disabled'] if sig[2] else ['!disabled'])
                    row['stop'].state(['!disabled'] if sig[3] else ['disabled'])
            self._reload_log(force=force_log)
            if not self._closing_to_tray:
                self.after(1500, self._refresh)

        def _dragging(self):
            """窗口是否正在被拖动 / 缩放。

            做法：比较两次采样的窗口几何。拖动时位置在变，静止时不变。
            （比绑 <Configure> 更稳：缩放窗口的 <Configure> 会连续触发，
              容易和刷新逻辑互相打架。）
            """
            try:
                g = (self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height())
            except Exception:
                return False
            prev = self._last_geom
            self._last_geom = g
            return prev is not None and prev != g

        # ---------- 托盘 ----------
        def hide_to_tray(self):
            self.withdraw()
            self._start_tray()

        def _start_tray(self):
            if self._tray is not None:
                return
            try:
                import pystray
                from pystray import Menu, MenuItem
            except Exception as e:
                self.say(f'托盘不可用（缺 pystray）：{e}')
                self.deiconify()
                return
            tray_png = _icon_file('tray.png')
            icon_img = None
            try:
                from PIL import Image
                icon_img = Image.open(tray_png)
            except Exception:
                pass

            def show(icon, item):
                icon.stop()
                self._tray = None
                self.after(0, self.deiconify)

            def quit_all(icon, item):
                icon.stop()
                self._tray = None
                self.after(0, self._quit_with_services)

            items = [MenuItem('打开控制面板', show, default=True), Menu.SEPARATOR]
            for key, svc in self.services.items():
                items.append(MenuItem(
                    f'启动/停止 {svc.label}',
                    (lambda k: (lambda i, it: (self.do_start(k) if not self.services[k].running()
                                               else self.do_stop(k))))(key)))
            items += [Menu.SEPARATOR,
                      MenuItem('打开日志目录', lambda i, it: self.open_logs()),
                      Menu.SEPARATOR,
                      MenuItem('退出（并停止服务）', quit_all)]
            self._tray = pystray.Icon('DeepChat', icon_img, title, Menu(*items))
            threading.Thread(target=self._tray.run, daemon=True).start()
            self.say('已最小化到托盘（双击托盘图标可重新打开）')

        def _quit_with_services(self):
            for svc in self.services.values():
                if svc.running():
                    svc.stop()
            self.destroy()

        def on_close(self):
            running = [s.label for s in self.services.values() if s.running()]
            if not running:
                self.destroy()
                return
            ans = messagebox.askyesnocancel(
                '关闭 DeepChat 控制面板',
                '服务仍在运行中。\n\n'
                '「是」= 最小化到系统托盘（服务继续跑）\n'
                '「否」= 连同服务一起退出\n'
                '「取消」= 什么都不做')
            if ans is None:
                return
            if ans:
                self.hide_to_tray()
            else:
                self._quit_with_services()

    _panel = Panel()
    # ★ 等窗口真正画出来再发就绪信号（不是"构造完"就算）──
    #   run.bat 一收到信号就关控制台，早发会让用户又看到"黑窗消失但 GUI 还没出来"。
    _panel.after(120, _signal_ready)
    _panel.mainloop()
    return 0
