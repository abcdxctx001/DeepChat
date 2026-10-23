import os, json, subprocess, platform, time, re, chardet, threading, logging, sys, uuid, atexit, signal, shutil
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
# JSON 响应直接输出中文（不转 \uXXXX），避免被误读为乱码
try:
    app.json.ensure_ascii = False
except Exception:
    try:
        app.config['JSON_AS_ASCII'] = False
    except Exception:
        pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ============================================================
#  ★ 控制台加固（配合 exe 以「无控制台」方式运行 / pythonw 启动 GUI）
#
#  以 pythonw.exe 或 PyInstaller 的 --noconsole 构建运行时，sys.stdout /
#  sys.stderr 会是 None —— 此时任何 print() 都会抛 AttributeError 把流程打断，
#  logging 的 StreamHandler 也会在写日志时炸掉。而这两种正是控制面板的启动方式。
#
#  处理办法：
#    ① logging 一定要有**文件**handler，日志才不会丢（控制面板读的就是它）；
#    ② print 统一走 _safe_print，没有控制台就写日志，绝不抛异常。
# ============================================================
def _agent_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

_log_handlers = []
try:
    _log_handlers.append(logging.FileHandler(
        os.path.join(_agent_base_dir(), 'client.log'), encoding='utf-8'))
except Exception:
    pass
if sys.stderr is not None:
    try:
        _log_handlers.append(logging.StreamHandler())
    except Exception:
        pass
if _log_handlers:
    logging.getLogger().handlers = []
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        handlers=_log_handlers)


def _safe_print(*args, **kwargs):
    """没有控制台时也能安全"打印"（改写进日志）。"""
    try:
        if sys.stdout is not None:
            print(*args, **kwargs)
            return
    except Exception:
        pass
    try:
        logging.info(' '.join(str(a) for a in args))
    except Exception:
        pass


# 打包为 exe 运行时，以 exe 所在目录为基准（sys.executable 指向真实 exe 路径）；
# 未打包（源码运行）时，以脚本所在目录为基准。
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(BASE_DIR, 'workspace')
os.makedirs(WORK_DIR, exist_ok=True)
# 超长工具输出落盘目录（客户端机器本地，模型工作目录下，模型可自行读取继续分析）
TOOL_OUTPUT_DIR = os.path.join(WORK_DIR, 'tool_outputs')

# ---- 屏幕操作组件（虚拟桌面 + OCR + 模拟键鼠）位于 workspace/GUI 目录 ----
# virtual_ui.py / ocr.ps1 / capture_dll / README_SCREEN_UI.md 统一放在 GUI 文件夹
# 打包为 exe 时：优先使用 exe 旁的 workspace/GUI（完整包部署，DLL 路径推导一致），
# 缺失时回退到打包内置的 _MEIPASS/GUI（见 agent.spec datas）
if getattr(sys, 'frozen', False):
    GUI_DIR = os.path.join(WORK_DIR, 'GUI')
    if not os.path.isdir(GUI_DIR):
        GUI_DIR = os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), 'GUI')
else:
    GUI_DIR = os.path.join(WORK_DIR, 'GUI')
if os.path.isdir(GUI_DIR):
    sys.path.insert(0, GUI_DIR)
    try:
        import virtual_ui
        # 初始化虚拟桌面模块（OCR/截图/输入模拟），显式指定组件路径（GUI 目录）
        VUI = virtual_ui.init(
            workspace_dir=WORK_DIR,
            capture_dll_path=os.path.join(GUI_DIR, 'capture_dll', 'capture_helper.dll'),
            ocr_ps1_path=os.path.join(GUI_DIR, 'ocr.ps1'),
        )
    except Exception as _vui_err:
        # ★ 2026-10-06：这里以前**没有 try/except** —— virtual_ui.py 里
        #   `from PIL import Image` 一旦失败（PIL 未随包分发/被杀软删除/路径不全），
        #   这个 ImportError 会直接把整个 agent.py 的启动打断，agent 根本起不来。
        #   而下面那句提示只覆盖"目录不存在"这一种情况，报出来的文案
        #   （"屏幕操作组件目录不存在"）与真因（PIL 缺失）对不上，
        #   用户看到的就是莫名其妙的"PIL 缺失/组件缺失"。
        #   现在：把真实异常完整打出来，并降级为"仅屏幕操作不可用"，
        #   不影响 agent 的命令/文件等其余功能。
        virtual_ui = None
        VUI = None
        _safe_print(f"[警告] 屏幕操作组件加载失败（仅屏幕功能不可用，其余功能正常）: {_vui_err}")
        try:
            import traceback as _tb
            _safe_print(_tb.format_exc())
        except Exception:
            pass
else:
    # 组件缺失时给出明确提示（不影响 agent 其他功能）
    virtual_ui = None
    VUI = None
    _safe_print(f"[警告] 屏幕操作组件目录不存在: {GUI_DIR}（virtual_ui.py / ocr.ps1 / capture_dll 应位于 workspace/GUI 下）")

BLACKLIST = ['rm -rf /', 'rm -rf ~', 'del /f /s', 'format', 'mkfs', ':(){ :|:& };:', 'shutdown', 'reboot', 'halt', 'poweroff', 'dd if=', 'chmod 777 /', 'sudo']
# ★ 2026-10-04：把黑名单拆成两类（见 is_safe_command 的说明）：
#   · _BLOCK_SUBSTR：本身已足够特征化的带参数模式，继续用子串匹配
#   · _BLOCK_WORDS ：纯单词型，必须按词边界匹配，否则会误伤
#                    Format-List / reformat / mkdir format_backup 之类
_BLOCK_SUBSTR = ['rm -rf /', 'rm -rf ~', 'del /f /s', 'mkfs',
                 ':(){ :|:& };:', 'dd if=', 'chmod 777 /']
_BLOCK_WORDS = ['shutdown', 'reboot', 'halt', 'poweroff', 'sudo']
# ★ `format` 单独处理：它作为**命令**才危险（格式化磁盘），作为**普通词**很常见
#   （Format-List / format_backup / 路径里叫 format 的目录）。
#   这里只拦两种形态：
#     ① 命令位：句首或 && ; | 之后紧跟 format
#     ② 后接盘符：format C: / format D:
#   其余情况（`... | Format-List`、`dir ...\format`）放行 —— 实测模型正是被
#   PowerShell 的 Format-List 误拦的。
_BLOCK_FORMAT_RE = re.compile(
    r'(^|[&;|]\s*)format(\.exe)?(\s+[A-Za-z]:|\s*$)',
    re.IGNORECASE | re.MULTILINE)
COMMAND_TIMEOUT = 600
IDLE_TIMEOUT = 30
# 隐藏子进程的控制台窗口：Windows 下用 CREATE_NO_WINDOW 让 PowerShell/cmd/node 等
# 子进程不再闪出或停留黑窗；非 Windows 平台该值为 0，传给 creationflags 安全无副作用。
_CREATE_NO_WINDOW = 0x08000000 if platform.system() == 'Windows' else 0

def _no_window_kwargs():
    """★ 2026-10-14（按用户反馈"运行 python/pwsh 的窗口又显示出来了"）：
       给子进程再加一层隐藏保险。

       `_CREATE_NO_WINDOW` 只在"父进程没有控制台"时才确保不新建控制台；
       打包成 windowed 的 agent.exe 或从某些宿主启动时，子进程仍可能闪出窗口。
       叠加 STARTUPINFO(SW_HIDE) 后，**无论父进程有没有控制台**都不会显示窗口
       —— 这是 Windows 上最彻底的做法。
       非 Windows 平台返回空 dict（展开后无副作用）。
    """
    if platform.system() != 'Windows':
        return {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return {'creationflags': _CREATE_NO_WINDOW, 'startupinfo': si}
    except Exception:
        return {'creationflags': _CREATE_NO_WINDOW}

tasks = {}
tasks_lock = threading.Lock()

def safe_string(s):
    return re.sub(r'[\ud800-\udfff]', '', s) if isinstance(s, str) else s

# 正常文本中可能出现的常见希腊字母（科学符号），其余希腊码位视为碰撞产物
_COMMON_GREEK = set('αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ')

# 中文文本中偶尔出现的 Latin-1 符号（°±×÷§©®™· 等），其余 U+00C0-U+00FF
# 码位（ûÿôë 等字母）几乎不会在中文/英文命令输出中出现，出现即为 GBK 碰撞特征
_LATIN1_SAFE = set('°±×÷§©®™·')

def _is_rare_char(o):
    """GBK 字节流被 UTF-8 误解码时的高频产物区块（组合音标/修饰字母/拉丁扩展/西里尔/
    希伯来/阿拉伯等），正常中文/英文命令输出几乎不会出现，出现即为误解码的强信号。"""
    if 0x0370 <= o <= 0x03ff:  # 希腊区：仅常见字母豁免，其余（如 Ϊ ϰ ϵ）视为碰撞
        return chr(o) not in _COMMON_GREEK
    return (0x0100 <= o <= 0x02ff or 0x0300 <= o <= 0x036f
            or 0x0400 <= o <= 0x04ff or 0x0500 <= o <= 0x05ff or 0x0600 <= o <= 0x06ff
            or 0x0900 <= o <= 0x0dff or 0x1e00 <= o <= 0x1eff or 0x2e80 <= o <= 0x2eff
            or 0xfb00 <= o <= 0xfbff)

def _is_light_rare_char(o):
    """弱碰撞特征区块（仅常见希腊字母，正常输出偶尔出现如科学符号 β），轻微处罚。"""
    return 0x0370 <= o <= 0x03ff and chr(o) in _COMMON_GREEK

def _needs_arbitration(s):
    """UTF-8 解码结果含"碰撞特征字符"时，不直接采信，交由多候选评分仲裁。

    覆盖 GBK 中英混合碰撞：GBK 双字节可能恰好构成合法 UTF-8 的
    Latin-1 补充字母（如 路=0xC2B7→'·'、没=0xC3BB→'û'、每=0xC3BF→'ÿ'、
    位=0xCEBB→'λ'）或稀有区字符，导致中英混合文本被误判为 UTF-8 乱码。
    - '·'：中文间隔号（3·15、深·圳）前后是数字/汉字属正常；若 '·' 后跟汉字
      且前一个字符不是汉字/数字，或 '·' 位于末尾且前一个不是汉字/数字，判定为碰撞。
    - 希腊字母：后跟字母/汉字/数字（词内）判定为碰撞；后跟空格/符号/结尾
      （独立的科学符号，如 β=0.5）不触发仲裁。
    """
    n = len(s)
    for i, c in enumerate(s):
        o = ord(c)
        nxt = s[i + 1] if i + 1 < n else ''
        prv = s[i - 1] if i > 0 else ''
        if o == 0x00B7:  # ·
            # 前一个字符不是汉字/数字时：
            #   后跟汉字（路径→·径）、后跟符号/空格（;路=1→;·=1）、或位于末尾（[路→[·）
            #   均判定为碰撞候选；3·15、A·B、深·圳 等正常间隔号不触发
            if not ('\u4e00' <= prv <= '\u9fff' or prv.isdigit()):
                if not nxt or '\u4e00' <= nxt <= '\u9fff' or not (nxt.isalpha() or nxt.isdigit()):
                    return True
            continue
        if 0x00C0 <= o <= 0x00FF and c not in _LATIN1_SAFE:
            return True
        if 0x0370 <= o <= 0x03ff:
            # 词内（后跟字母/汉字/数字）→ 碰撞特征；独立科学符号（后跟空格/符号/结尾）→ 正常
            if nxt and (nxt.isalpha() or '\u4e00' <= nxt <= '\u9fff' or nxt.isdigit()):
                return True
            continue
        if _is_rare_char(o):
            return True
    return False

def _looks_ok(s):
    """UTF-8 解码结果的合理性快速校验：无替换符/控制字符/碰撞特征字符堆叠。"""
    if not s or '\ufffd' in s: return False
    rare = light = 0
    for c in s:
        o = ord(c)
        if o < 32 and c not in '\t\n\r': return False
        if 0xd800 <= o <= 0xdfff: return False
        if o > 127:
            if _is_rare_char(o): rare += 1
            elif _is_light_rare_char(o): light += 1
    # 强特征 0 个、轻特征至多 1 个 → 视为正常（如单个 β / é）
    return rare == 0 and light <= 1

def _text_score(s):
    """评估解码结果合理性，分数越高越像正常文本（用于多候选解码择优）。"""
    if not s: return -10000
    score = 0.0
    score -= s.count('\ufffd') * 100
    rare = light = 0
    for c in s:
        o = ord(c)
        if o < 32 and c not in '\t\n\r': score -= 30          # 控制字符
        elif 0x80 <= o <= 0x9f: score -= 50                   # C1 控制（latin-1 误解码特征）
        elif '\u4e00' <= c <= '\u9fff': score += 8            # 常用汉字
        elif 0x3400 <= o <= 0x4dbf or 0x20000 <= o <= 0x2ffff: score -= 5  # CJK 扩展生僻字
        elif '\u3000' <= c <= '\u303f' or '\uff00' <= c <= '\uffef': score += 3  # 全角标点
        elif 32 <= o < 127: score += 0.5                      # ASCII 可打印
        elif 0x00C0 <= o <= 0x00FF and c not in _LATIN1_SAFE: score -= 10  # Latin-1 补充字母（GBK 碰撞特征）
        elif 0xe000 <= o <= 0xf8ff: score -= 50               # 私有区（GBK 误读 UTF-8 常见）
        elif 0xd800 <= o <= 0xdfff: score -= 100              # 孤立代理区
        elif _is_rare_char(o): rare += 1
        elif _is_light_rare_char(o): light += 1
    score -= rare * 30
    score -= light * 10
    return score

def decode_bytes(data):
    """智能解码命令输出字节流，自适应选择正确的编码方式。

    覆盖场景：UTF-8 / UTF-8 BOM / UTF-16(含无 BOM) / GBK(GB18030) / 纯 ASCII 等。
    Windows 下 cmd 内置命令（如 type）输出通常为 GBK 字节，Python 等程序多为 UTF-8 字节，
    此处不依赖单一编码：BOM 优先 → ASCII 快速路径 → UTF-8 严格解码+合理性校验 →
    chardet 识别无 BOM 的 UTF-16/32 → 多候选解码按文本质量评分择优（GBK 兜底），
    从而避免 GBK 字节流恰好构成合法 UTF-8 序列（碰撞）时被误解码成乱码。"""
    if not data: return ""
    # 1) UTF-8 BOM：剥离后按 utf-8 解码（避免开头残留 \ufeff 乱码字符）
    if data.startswith(b'\xef\xbb\xbf'):
        try: return safe_string(data[3:].decode('utf-8'))
        except UnicodeDecodeError: pass
    # 2) UTF-16 BOM（防御 cmd type 未做代码页转换而输出的原始 UTF-16 字节）
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try: return safe_string(data.decode('utf-16'))
        except UnicodeDecodeError: pass
    # 3) 纯 ASCII 快速路径（无需猜测编码）
    if data.isascii():
        return safe_string(data.decode('ascii'))
    # 4) UTF-8 严格解码 + 合理性校验（绝大多数正常输出在此返回，省去 chardet 开销）
    need_arbitrate = False
    try:
        s = safe_string(data.decode('utf-8'))
        # 含碰撞特征字符（Latin-1 补充字母/希腊字母/稀有区）时不直接采信，
        # 交由多候选评分仲裁，避免 GBK 中英混合字节被误判为 UTF-8 乱码
        if _looks_ok(s) and not _needs_arbitration(s):
            return s
        if _looks_ok(s) and _needs_arbitration(s):
            need_arbitrate = True
    except UnicodeDecodeError:
        pass
    # 5) chardet：仅用于识别无 BOM 的 UTF-16 / UTF-32 及后续加权参考
    det = chardet.detect(data)
    det_enc = (det.get('encoding') or '').lower().replace('_', '-')
    det_conf = det.get('confidence') or 0.0
    if (det_enc.startswith('utf-16') or det_enc.startswith('utf-32')) and det_conf >= 0.2:
        try: return safe_string(data.decode(det_enc))
        except (UnicodeDecodeError, LookupError): pass
    # 6) 多候选解码 + 质量评分择优（GBK 字节流偶尔能"骗过" UTF-8 解码，需对比取优）
    candidates = []
    for enc in ('utf-8', 'gb18030', 'gbk'):
        try:
            s = safe_string(data.decode(enc))
            candidates.append((_text_score(s), enc, s))
        except (UnicodeDecodeError, LookupError):
            pass
    if not candidates:
        # 7) 最终兜底，保证不抛异常
        return safe_string(data.decode('latin-1', errors='replace'))
    # 8) chardet 结果仅作弱参考（置信度加权，避免盲信造成 cp1250 等误判）；
    #    疑似碰撞（need_arbitrate）时跳过加权，交由纯文本质量评分裁决
    if det_conf >= 0.2 and not need_arbitrate:
        if det_enc in ('utf-8', 'utf8'):
            candidates = [(q + det_conf * 25 if e == 'utf-8' else q, e, s) for q, e, s in candidates]
        elif det_enc in ('gb18030', 'gb2312', 'gbk'):
            candidates = [(q + det_conf * 25 if e == 'gb18030' else q, e, s) for q, e, s in candidates]
    # 9) 取最高分；同分时优先 UTF-8（现代默认）
    best = max(candidates, key=lambda c: (c[0], c[1] == 'utf-8'))
    return best[2]

# ★ BUGFIX(2026-10-04)：黑名单原来是**朴素子串匹配**（`b in cmd.lower()`），
#   导致大量无害命令被误拦。实测证据：模型执行
#     ... | Select-Object -First 3 Name,Length,LastWriteTime | Format-List
#   （只是列目录并格式化输出）因为含子串 "format" 被判「禁止的命令」。
#
#   修法：对纯单词型危险命令加**词边界**，不再匹配标识符内部的子串：
#     · Format-List / Get-Item -Path .\format  → 不再误拦
#     · format C:                              → 仍然拦
#   其余带参数的模式（rm -rf / 等）保持子串匹配，它们本身已足够特征化。
_BLOCK_WORD_RE = re.compile(
    r'(?<![A-Za-z0-9_\-])(' + '|'.join(re.escape(w) for w in _BLOCK_WORDS) + r')(?![A-Za-z0-9_\-])',
    re.IGNORECASE)


def is_safe_command(cmd):
    s = cmd or ''
    low = s.lower()
    for b in _BLOCK_SUBSTR:
        if b in low:
            return False
    if _BLOCK_WORD_RE.search(s):
        return False
    if _BLOCK_FORMAT_RE.search(s):
        return False
    return True

# ---------- HTML to Markdown ----------
from html.parser import HTMLParser

class HTMLToTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.output = []
        self.in_list = False
        self.list_type = None
        self.ol_count = 0
        self.ignore_level = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('style','script'): self.ignore_level+=1; return
        if self.ignore_level>0: return
        d=dict(attrs)
        if tag in ('h1','h2','h3','h4','h5','h6'): self.output.append('\n'+'#'*int(tag[1])+' ')
        elif tag=='p': self.output.append('\n\n')
        elif tag=='br': self.output.append('\n')
        elif tag in ('ul','ol'): self.output.append('\n'); self.in_list=True; self.list_type=tag; self.ol_count=0
        elif tag=='li':
            if self.in_list:
                if self.list_type=='ul': self.output.append('- ')
                else: self.ol_count+=1; self.output.append(f'{self.ol_count}. ')
        elif tag=='a': self.output.append('['); self._last_a_href=d.get('href','')
        elif tag=='img': self.output.append(f'![{d.get("alt","图片")}]({d.get("src","")})')
        elif tag in ('strong','b'): self.output.append('**')
        elif tag in ('em','i'): self.output.append('*')
        elif tag=='code': self.output.append('`')
        elif tag=='pre': self.output.append('\n```\n')
        elif tag=='blockquote': self.output.append('\n> ')

    def handle_endtag(self, tag):
        if tag in ('style','script'): self.ignore_level=max(0,self.ignore_level-1); return
        if self.ignore_level>0: return
        if tag in ('h1','h2','h3','h4','h5','h6'): self.output.append('\n')
        elif tag=='p': self.output.append('\n')
        elif tag in ('ul','ol'): self.in_list=False; self.output.append('\n')
        elif tag=='li': self.output.append('\n')
        elif tag=='a':
            href=getattr(self,'_last_a_href','')
            if href: self.output.append(f']({href})')
            else: self.output.append(']')
        elif tag in ('strong','b'): self.output.append('**')
        elif tag in ('em','i'): self.output.append('*')
        elif tag=='code': self.output.append('`')
        elif tag=='pre': self.output.append('\n```\n')
        elif tag=='blockquote': self.output.append('\n')

    def handle_data(self, data):
        if self.ignore_level>0: return
        self.output.append(data)

    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', ''.join(self.output)).strip()

def parse_html_to_markdown(html_content):
    try:
        parser=HTMLToTextParser()
        parser.feed(html_content)
        return parser.get_text()
    except:
        x=re.sub(r'<script[\s\S]*?</script>','',html_content,flags=re.I)
        x=re.sub(r'<style[\s\S]*?</style>','',x,flags=re.I)
        x=re.sub(r'<[^>]+>',' ',x)
        return re.sub(r'\s+',' ',x).strip()

# ---------- Puppeteer ----------
def find_node_path():
    bundled = os.path.join(WORK_DIR, 'node', 'node.exe')
    if os.path.exists(bundled): return bundled
    for p in [r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe",
              fr"C:\Users\{os.getenv('USERNAME')}\AppData\Local\Programs\nodejs\node.exe"]:
        if os.path.exists(p): return p
    for p in os.environ.get("PATH","").split(os.pathsep):
        cand=os.path.join(p,"node.exe")
        if os.path.exists(cand): return cand
    return None
NODE_PATH=find_node_path()

def fetch_with_puppeteer(url):
    if not NODE_PATH: return None,None,"未找到 Node.js"
    script=os.path.join(WORK_DIR,"fetch_page.js")
    if not os.path.exists(script):
        with open(script,'w',encoding='utf-8') as f:
            f.write('''// workspace/fetch_page.js
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const fs = require('fs');
puppeteer.use(StealthPlugin());
// 优先使用系统已安装的 Chrome/Edge，找不到时回退 puppeteer 自带缓存
function findBrowser() {
    const candidates = [
        process.env.PROGRAMFILES + '\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',
        process.env['PROGRAMFILES(X86)'] + '\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',
        process.env.LOCALAPPDATA + '\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',
        'C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',
        'C:\\\\Program Files (x86)\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe',
        process.env.PROGRAMFILES + '\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe',
        process.env['PROGRAMFILES(X86)'] + '\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe',
        'C:\\\\Program Files\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe',
        'C:\\\\Program Files (x86)\\\\Microsoft\\\\Edge\\\\Application\\\\msedge.exe'
    ];
    for (const p of candidates) {
        if (p && fs.existsSync(p)) return p;
    }
    return null;
}
const url = process.argv[2];
if (!url) {
    console.error(JSON.stringify({ error: '缺少URL参数' }));
    process.exit(1);
}
(async () => {
    let browser;
    try {
        const launchOpts = { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'] };
        const browserPath = findBrowser();
        if (browserPath) launchOpts.executablePath = browserPath;
        browser = await puppeteer.launch(launchOpts);
        const page = await browser.newPage();
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36');
        await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
        const html = await page.content();
        const title = await page.title();
        console.log(JSON.stringify({ success: true, title, content: html }));
    } catch (error) {
        console.error(JSON.stringify({ error: error.message }));
        process.exit(1);
    } finally {
        if (browser) await browser.close();
    }
})();''')
    try:
        proc=subprocess.Popen([NODE_PATH, script, url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', cwd=WORK_DIR, env=_child_env(), creationflags=_CREATE_NO_WINDOW)
        out,err=proc.communicate(timeout=90)
        if proc.returncode!=0:
            # 脚本错误（如找不到浏览器）优先返回 stderr 中的 JSON 错误
            return None,None,(err.strip() or out or f"node 退出码 {proc.returncode}")
        if not out.strip():
            return None,None,(err.strip() or "抓取脚本无输出（可能浏览器启动失败）")
        try:
            data=json.loads(out)
        except Exception as je:
            return None,None,f"抓取结果解析失败: {je}；stderr: {err[:500] if err else '无'}"
        content=data.get('content')
        text_content=parse_html_to_markdown(content) if content else None
        return content,text_content,data.get('error')
    except Exception as e: return None,None,str(e)

# ---------- 命令执行 ----------
# 子进程统一环境：Python 子进程输出 UTF-8（避免 GBK 字节流引发编码歧义；
# cmd 内置命令仍输出系统代码页字节，由 decode_bytes 自适应解码兜底）
def _child_env(env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    e.setdefault('PYTHONIOENCODING', 'utf-8')
    return e

def run_command_maybe_multiline(cmd, timeout=COMMAND_TIMEOUT, idle_timeout=IDLE_TIMEOUT):
    """执行命令；**含换行时改用临时 .bat**。

    ★ 2026-10-13（修「多行 #exec 只执行第 1 行」）：
      Windows 上 Popen(cmd, shell=True) 对含换行的命令行**只执行第一行**
      （实测 'echo AAA\necho BBB' → 只输出 AAA）。这是平台行为，不是我们写错。
      多行命令写入临时 .bat（CRLF）再执行，才能真正跑完所有行。
      单行命令走原路径，行为零变化。
    """
    if not isinstance(cmd, str) or ('\n' not in cmd and '\r' not in cmd):
        return execute_with_timeout(cmd, shell=True, timeout=timeout, idle_timeout=idle_timeout)
    # 多行 → 写临时 bat
    tmpdir = os.path.join(WORK_DIR, 'temp_exec')
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except Exception:
        pass
    bat = os.path.join(tmpdir, 'run_%d_%d.bat' % (int(time.time() * 1000), os.getpid() % 1000))
    try:
        # 统一 CRLF；开头 @echo off 避免回显命令行本身
        body = cmd.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        with open(bat, 'w', encoding='utf-8', newline='') as f:
            f.write('@echo off\r\n' + body + '\r\n')
        return execute_with_timeout(bat, shell=False, timeout=timeout, idle_timeout=idle_timeout)
    finally:
        try:
            os.remove(bat)
        except Exception:
            pass


def execute_write_command(full_cmd):
    m=re.match(r'^#write\s+(["\']?)(.*?)\1\s*\n(.*)', full_cmd, re.DOTALL)
    if not m: return "格式错误",-1
    raw_path=m.group(2).strip()
    content=m.group(3)
    if raw_path.startswith('"') and raw_path.endswith('"'): raw_path=raw_path[1:-1]
    elif raw_path.startswith("'") and raw_path.endswith("'"): raw_path=raw_path[1:-1]
    if not raw_path: return "未提供文件路径",-1
    # 修复：相对路径统一放在 workspace 下
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(WORK_DIR, raw_path)
    try:
        dir_name=os.path.dirname(raw_path)
        if dir_name and not os.path.exists(dir_name): os.makedirs(dir_name,exist_ok=True)
        with open(raw_path,'w',encoding='utf-8') as f: f.write(content)
        # ★ 2026-10-13（修用户实测发现的 bug）：
        #   这里原来写的是 `len(content)` —— 那是**字符数**，不是字节数。
        #   含中文时差得很明显：实测"52 字节"其实 83 字节（15 个中文字 ×3 字节）。
        #   改为读**磁盘上的真实大小**，与 #read_file 的"总大小"口径完全一致。
        try:
            _size = os.path.getsize(raw_path)
        except Exception:
            _size = len(content.encode('utf-8'))
        return f"文件写入成功：{raw_path}（大小 {_size} 字节）",0
    except Exception as e: return f"写入失败：{str(e)}",-1

def execute_replace_command(full_cmd):
    """
    解析 #replace [--all] [--separator=<SEP>] <路径>
    旧文本
    分隔符
    新文本
    """
    # 提取第一行（命令头）
    newline_idx = full_cmd.find('\n')
    if newline_idx == -1:
        return "命令格式错误：缺少替换内容", -1
    first_line = full_cmd[:newline_idx].strip()
    body = full_cmd[newline_idx+1:]

    # 解析参数和路径
    pattern = r'^#replace\s+(--all\s+)?(--separator=(\S+)\s+)?(.+?)$'
    m = re.match(pattern, first_line)
    if not m:
        return "命令格式错误：无法解析 #replace 参数", -1
    all_flag = m.group(1) is not None
    sep = m.group(3) if m.group(2) else "===<separator>==="
    path_str = m.group(4).strip()

    # 处理路径两侧的引号
    raw_path = path_str
    if (raw_path.startswith('"') and raw_path.endswith('"')) or \
       (raw_path.startswith("'") and raw_path.endswith("'")):
        raw_path = raw_path[1:-1]
    if not raw_path:
        return "未提供文件路径", -1
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(WORK_DIR, raw_path)

    # 按分隔符分割 body（分隔符必须独占一行）
    try:
        escaped_sep = re.escape(sep)
        # 匹配行首或行尾的分隔符（前后可有换行）
        pattern = r'(?:\r?\n|^)' + escaped_sep + r'(?:\r?\n|$)'
        parts = re.split(pattern, body, maxsplit=1)
        if len(parts) != 2:
            return f"分隔符 '{sep}' 未出现或出现多次，请确保旧文本和新文本之间只包含一个独占一行的分隔符", -1
        old_text = parts[0]
        new_text = parts[1]
    except Exception as e:
        return f"分隔符解析失败: {str(e)}", -1

    # 读取文件
    try:
        with open(raw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return f"文件不存在: {raw_path}", -1
    except Exception as e:
        return f"读取文件失败: {str(e)}", -1

    # 执行替换
    if all_flag:
        count = content.count(old_text)
        if count == 0:
            return f"未找到匹配文本，替换失败", -1
        new_content = content.replace(old_text, new_text)
    else:
        if old_text not in content:
            return f"未找到匹配文本，替换失败", -1
        new_content = content.replace(old_text, new_text, 1)
        count = 1

    # 写回文件
    try:
        with open(raw_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except Exception as e:
        return f"写入文件失败: {str(e)}", -1

    return f"替换成功，共替换 {count} 处", 0

def get_python_cmd():
    """获取用于执行 #run_python 的 Python 解释器命令。
    优先使用 workspace/python/ 下随包分发的解释器；源码运行时直接用当前解释器；
    打包成 exe 后 sys.executable 指向 exe 本身，需从 PATH 查找系统 Python（python / python3 / py）。"""
    bundled = os.path.join(WORK_DIR, 'python', 'python.exe')
    if os.path.exists(bundled):
        return [bundled]
    if not getattr(sys, 'frozen', False):
        return [sys.executable]
    for name in ('python', 'python3', 'py'):
        p = shutil.which(name)
        if p:
            return [p]
    return None

def get_pwsh_cmd():
    """获取用于执行 #run_pwsh 的 PowerShell 解释器命令。

    ★ 优先 PowerShell 7+（pwsh），没有就退回系统自带的 Windows PowerShell 5.1
      （powershell.exe，Win10/11 一定存在）。
      为什么要回退：pwsh 是**单独安装**的，很多机器（含本机）并没有装；
      如果只认 pwsh，这个语言选项在那些机器上会直接报"找不到解释器"，
      等于给了个用不了的功能。5.1 与 7 语法高度兼容，作为回退足够。
    """
    for name in ('pwsh', 'pwsh.exe'):
        p = shutil.which(name)
        if p:
            return [p]
    for cand in (os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                              'PowerShell', '7', 'pwsh.exe'),
                 os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                              'PowerShell', '7', 'pwsh.exe')):
        if os.path.isfile(cand):
            return [cand]
    p = shutil.which('powershell') or shutil.which('powershell.exe')
    if p:
        return [p]
    sysroot = os.environ.get('SystemRoot', r'C:\Windows')
    cand = os.path.join(sysroot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    if os.path.isfile(cand):
        return [cand]
    return None


def execute_run_command(lang,code):
    if len(code)>50000: return "代码过长",-1
    mingw_bin = os.path.join(WORK_DIR, 'mingw64', 'bin')
    bundled_gpp = os.path.join(mingw_bin, 'g++.exe')
    bundled_node = os.path.join(WORK_DIR, 'node', 'node.exe')
    config={
        'python':{'ext':'.py'},
        'cpp':{'ext':'.cpp','compile_cmd':[bundled_gpp if os.path.exists(bundled_gpp) else 'g++','-o','{exe}','{script}'],'run_cmd':['{exe}']},
        'bash':{'ext':'.sh','run_cmd':['bash','{script}']},
        'node':{'ext':'.js','run_cmd':[bundled_node if os.path.exists(bundled_node) else 'node','{script}']},
    }
    if lang not in config and lang != 'pwsh': return f"不支持的语言 #{lang}",-1
    if lang == 'pwsh':
        # ★ -NoProfile：不加载用户 profile，避免别人的自定义配置影响脚本行为。
        #   -ExecutionPolicy Bypass：否则默认策略可能直接拒绝运行脚本文件。
        #   -File + 路径：把代码写进 .ps1 再跑（与其它语言一致）。
        ps = get_pwsh_cmd()
        if ps is None:
            return "未找到 PowerShell 解释器（PATH 中无 pwsh / powershell）", -1
        cfg = {'ext': '.ps1',
               'run_cmd': ps + ['-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', '{script}']}
    else:
        cfg=config[lang]
    if lang == 'python':
        py_cmd = get_python_cmd()
        if py_cmd is None:
            return "打包版未找到系统 Python 解释器（PATH 中无 python/python3/py），无法执行 #run_python", -1
        cfg = {'ext': '.py', 'run_cmd': py_cmd + ['{script}']}
    # 使用内置 MinGW 编译器时，将 bin 目录加入 PATH，供 as/ld/动态库查找
    env = None
    if lang == 'cpp' and os.path.exists(bundled_gpp):
        env = os.environ.copy()
        env['PATH'] = mingw_bin + os.pathsep + env.get('PATH', '')
    script_name=f"temp_runner_{int(time.time())}_{os.urandom(2).hex()}{cfg['ext']}"
    script_path=os.path.join(WORK_DIR,script_name)
    try:
        # ★ PowerShell 脚本必须带 UTF-8 BOM 落盘。
        #   原因是 Windows PowerShell 5.1 用 -File 执行脚本时，若文件没有 BOM，
        #   会按**系统 ANSI 码页**（中文 Windows 上是 GBK）解析 —— 脚本里的中文
        #   会直接乱码甚至语法报错。其它语言（py/js/sh）按 UTF-8 读，不需要 BOM。
        _encoding = 'utf-8-sig' if cfg['ext'] == '.ps1' else 'utf-8'
        with open(script_path,'w',encoding=_encoding) as f: f.write(code)
        exe_path=None
        if 'compile_cmd' in cfg:
            exe_name=script_name.replace(cfg['ext'],'.exe')
            exe_path=os.path.join(WORK_DIR,exe_name)
            compile_cmd=[part.replace('{script}',script_path).replace('{exe}',exe_path) for part in cfg['compile_cmd']]
            proc=subprocess.Popen(compile_cmd,cwd=WORK_DIR,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=_child_env(env),**_no_window_kwargs())
            out,err=proc.communicate(timeout=30)
            if proc.returncode!=0: return f"编译失败:\n{decode_bytes(err)}",proc.returncode
        run_cmd=[part.replace('{script}',script_path).replace('{exe}',exe_path or script_path) for part in cfg['run_cmd']]
        return execute_with_timeout(run_cmd,shell=False,env=env)
    except Exception as e: return f"脚本执行异常: {str(e)}",-1
    finally:
        try:
            if os.path.exists(script_path): os.remove(script_path)
            if exe_path and os.path.exists(exe_path): os.remove(exe_path)
        except: pass

def _maybe_persist_output(out, truncate, tag):
    """★ 超长工具输出就地落盘（客户端机器本地，模型可读）：

    工具（run_script / execute_command / fetch_webpage 等）在客户端 agent 执行，
    输出超过前端设置的"工具输出截断长度"（truncate，默认 4000）时：
    - 完整内容写入 workspace/tool_outputs/<tag>_<时间戳>.txt（模型工作目录下）；
    - 只返回前 truncate 字符预览 + 相对路径提示，模型可用 execute_command /
      run_script 读取该文件继续分析，避免超长输出直接撑爆模型上下文。
    服务器与客户端可能不在同一台机器，因此必须由客户端就地落盘。
    """
    if out is None:
        out = ""
    out = str(out)
    if len(out) <= truncate:
        return out
    try:
        os.makedirs(TOOL_OUTPUT_DIR, exist_ok=True)
        safe_tag = re.sub(r'[^\w\-]', '_', str(tag))[:24] or "tool"
        fname = f"{safe_tag}_{int(time.time() * 1000)}.txt"
        fpath = os.path.join(TOOL_OUTPUT_DIR, fname)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(out)
        rel = f"tool_outputs/{fname}"
        logging.info(f"[TOOL_OUTPUT] {tag} 输出 {len(out)} 字符超限，已就地落盘 {fpath}")
        # ★ 2026-10-19（按用户反馈：模型曾"不知道确切文件名"而自己 ls 去找）：
        #   把**文件名**与**绝对路径**都写清楚，并直接点名 read_file，减少无谓探测。
        return (f"（输出过长：共 {len(out)} 字符，完整内容已保存为 **{fname}**\n"
                f"  相对路径：{rel}\n"
                f"  绝对路径：{fpath}\n"
                f"  读取方式：用 read_file 工具直接读该路径（它不截断、不落盘），"
                f"或用 execute_command 的 type / run_script 读取；"
                f"本消息仅展示前 {truncate} 字符）\n"
                f"--- 前 {truncate} 字符 ---\n{out[:truncate]}")
    except Exception as e:
        logging.warning(f"[TOOL_OUTPUT] 落盘失败: {e}")
        return out[:truncate] + f"\n…（截断，原长度 {len(out)}，落盘失败 {e}）"

def _kill_process_tree(proc):
    """★ 2026-10-04：终止整个进程树，而不是只杀直接子进程。

    为什么必须这样：命令里若出现 `start "x" cmd /c "..."`，新进程会**继承父命令的
    stdout 管道句柄**。父 cmd.exe 退出后，start 起来的进程仍握着管道写端，
    reader 线程的 `pipe.readline()` 永远等不到 EOF → 一直阻塞、本轮卡死。
    只 `proc.kill()` 杀不掉这些孙进程，必须用 taskkill /T 按进程树清理。
    """
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False,
                           creationflags=_CREATE_NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    # 兜底：直接关掉我们这侧的管道读端，让阻塞中的 reader 线程立刻返回
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    try:
        if proc.stderr:
            proc.stderr.close()
    except Exception:
        pass


def execute_with_timeout(cmd,shell=False,timeout=COMMAND_TIMEOUT,env=None,idle_timeout=IDLE_TIMEOUT):
    proc=subprocess.Popen(cmd,shell=shell,cwd=WORK_DIR,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=_child_env(env),**_no_window_kwargs())
    stdout_chunks=[]
    stderr_chunks=[]
    start_time=time.time()
    last_output_time=[start_time]
    def reader(pipe,chunks):
        try:
            for line in iter(pipe.readline,b''):
                if line:
                    chunks.append(line)
                    last_output_time[0]=time.time()
        except: pass
    t_stdout=threading.Thread(target=reader,args=(proc.stdout,stdout_chunks),daemon=True)
    t_stderr=threading.Thread(target=reader,args=(proc.stderr,stderr_chunks),daemon=True)
    t_stdout.start()
    t_stderr.start()
    while proc.poll() is None:
        if time.time()-start_time>timeout:
            # ★ 用进程树终止：否则 start 起来的孙进程会继续握着管道写端
            _kill_process_tree(proc)
            return "命令执行超时",-1
        if time.time()-last_output_time[0]>idle_timeout:
            _kill_process_tree(proc)
            return "命令长时间无输出，已终止",-1
        time.sleep(0.2)
    # ★ 父进程已退出，但可能仍有孙进程握着管道写端（start 场景）→ reader 会一直阻塞。
    #   这里限时收尾；若 2 秒后 reader 仍未结束，说明写端被别人握着，
    #   直接关闭我们这侧的读端把它唤醒，避免线程与句柄泄漏。
    t_stdout.join(timeout=2)
    t_stderr.join(timeout=2)
    if t_stdout.is_alive() or t_stderr.is_alive():
        logging.warning("[EXEC] 命令已结束但输出管道未关闭（疑似 start 起的子进程仍握着写端），强制关闭读端")
        try:
            if proc.stdout: proc.stdout.close()
        except Exception: pass
        try:
            if proc.stderr: proc.stderr.close()
        except Exception: pass
        t_stdout.join(timeout=1)
        t_stderr.join(timeout=1)
    out=decode_bytes(b''.join(stdout_chunks))
    err=decode_bytes(b''.join(stderr_chunks))
    if err: out+="\n[STDERR]\n"+err
    return out,proc.returncode

class AsyncTask:
    def __init__(self, cmd, truncate=4000):
        self.id = str(uuid.uuid4())[:8]
        self.cmd = cmd
        self.truncate = truncate
        self._persisted = False   # ★ 超长输出只落盘一次
        # ★ 2026-10-19（**修 start_timer / async_mode 全部 500**）：
        #   这里原来直接用了 `_run` 和 `self._bat`，但两者**从未被赋值**
        #   → 每次构造 AsyncTask 都抛 NameError → /timer 与 /exec --async 返回 500。
        #   现在按 run_command_maybe_multiline 的同样规则，先判断是否多行：
        #     · 多行 → 写临时 .bat（CRLF）再执行（Windows 上 shell=True 对多行只跑第一行）
        #     · 单行 → 原样执行，行为零变化
        #   _monitor 里已有的"结束时删除 .bat"逻辑继续生效。
        self._bat = None
        _run = cmd
        if isinstance(cmd, str) and ('\n' in cmd or '\r' in cmd):
            try:
                _tmpdir = os.path.join(WORK_DIR, 'temp_exec')
                os.makedirs(_tmpdir, exist_ok=True)
                self._bat = os.path.join(
                    _tmpdir, 'async_%d_%d.bat' % (int(time.time() * 1000), os.getpid() % 1000))
                _body = cmd.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
                with open(self._bat, 'w', encoding='utf-8', newline='') as _f:
                    _f.write('@echo off\r\n' + _body + '\r\n')
                _run = self._bat
            except Exception:
                self._bat = None
                _run = cmd
        self.proc = subprocess.Popen(_run, shell=(self._bat is None), cwd=WORK_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(), **_no_window_kwargs())
        self.output = []
        self.error_output = []
        self.status = "running"
        self.thread_stdout = threading.Thread(target=self._read_output, args=(self.proc.stdout, self.output), daemon=True)
        self.thread_stderr = threading.Thread(target=self._read_output, args=(self.proc.stderr, self.error_output), daemon=True)
        self.thread_stdout.start()
        self.thread_stderr.start()
        self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self.monitor_thread.start()

    def _read_output(self, pipe, chunks):
        try:
            for line in iter(pipe.readline, b''):
                if not line: break
                chunks.append(line)
        except: pass

    def _monitor(self):
        self.thread_stdout.join()
        self.thread_stderr.join()
        self.proc.wait()
        self.status = "finished" if self.proc.returncode == 0 else f"failed({self.proc.returncode})"
        # ★ 2026-10-13：多行命令用的临时 .bat，任务结束即删（避免 temp_exec 里越积越多）
        if getattr(self, '_bat', None):
            try:
                os.remove(self._bat)
            except Exception:
                pass
            self._bat = None

    def get_status(self):
        out = decode_bytes(b''.join(self.output))
        err = decode_bytes(b''.join(self.error_output))
        if err: out += "\n[STDERR]\n" + err
        # ★ 任务结束后超长输出就地落盘（客户端本地），浏览器/服务器/模型只看到预览+路径
        if self.status != "running" and not self._persisted:
            self._persisted = True
            out = _maybe_persist_output(out, self.truncate, "task")
        return {
            "id": self.id,
            "status": self.status,
            "output": out,
            "returncode": self.proc.returncode if self.status != "running" else None
        }

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.status = "killed"

# 退出清理
def cleanup_all_tasks():
    with tasks_lock:
        for task in tasks.values():
            task.kill()
        tasks.clear()
atexit.register(cleanup_all_tasks)
def signal_handler(signum, frame):
    cleanup_all_tasks()
    sys.exit(0)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ---------- 路由 ----------
@app.route('/exec', methods=['POST'])
def execute_exec():
    data = request.get_json()
    if not data or 'command' not in data: return jsonify({"error": "缺少 command"}), 400
    cmd = data['command']
    async_mode = data.get('async', False)
    # ★ 设置项：最大超时 / 同步命令无输出超时（前端设置弹窗传入，未传用默认值）
    timeout = int(data.get('timeout', COMMAND_TIMEOUT) or COMMAND_TIMEOUT)
    idle_timeout = int(data.get('idle_timeout', IDLE_TIMEOUT) or IDLE_TIMEOUT)
    if not is_safe_command(cmd): return jsonify({"output": "禁止的命令", "returncode": -1}), 400
    if async_mode:
        task = AsyncTask(cmd)
        with tasks_lock:
            tasks[task.id] = task
        return jsonify({"task_id": task.id, "message": "异步任务已启动", "output": f"任务ID: {task.id}\n异步命令已开始执行"})
    else:
        out, ret = execute_with_timeout(cmd, shell=True, timeout=timeout, idle_timeout=idle_timeout)
        return jsonify({"output": out, "returncode": ret})

@app.route('/timer', methods=['POST'])
def execute_timer():
    data = request.get_json()
    if not data or 'command' not in data: return jsonify({"error": "缺少 command"}), 400
    cmd = data['command']
    if not is_safe_command(cmd): return jsonify({"output": "禁止的命令", "returncode": -1}), 400
    task = AsyncTask(cmd)
    with tasks_lock:
        tasks[task.id] = task
    return jsonify({"task_id": task.id, "output": f"Timer 任务已启动，ID: {task.id}"})

@app.route('/task/<task_id>', methods=['GET'])
def get_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task: return jsonify({"error": "任务不存在"}), 404
    return jsonify(task.get_status())

@app.route('/kill/<task_id>', methods=['POST'])
def kill_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task: return jsonify({"error": "任务不存在"}), 404
    task.kill()
    return jsonify({"message": f"任务 {task_id} 已终止", "status": task.status})

@app.route('/execute', methods=['POST'])
def execute_legacy():
    data = request.get_json()
    if not data or 'command' not in data:
        return jsonify({"output": "缺少 command", "returncode": -1}), 400

    cmd = data['command']
    # ★ 设置项：最大超时 / 同步命令无输出超时 / process 返回截断长度（前端设置弹窗传入）
    timeout = int(data.get('timeout', COMMAND_TIMEOUT) or COMMAND_TIMEOUT)
    idle_timeout = int(data.get('idle_timeout', IDLE_TIMEOUT) or IDLE_TIMEOUT)
    truncate = int(data.get('truncate', 4000) or 4000)

    # ---- #vdesk / #ocr / #input：虚拟桌面 + 屏幕OCR + 模拟键盘鼠标 ----
    if virtual_ui is not None:
        vui_out, vui_ret = virtual_ui.handle_command(cmd)
    else:
        vui_out, vui_ret = None, None
    if vui_out is not None:
        return jsonify({"output": vui_out, "returncode": vui_ret})

    # ---- #exec ----
    exec_match = re.match(r'^#exec\s+(--async\s+)?(.+)', cmd, re.DOTALL)
    if exec_match:
        async_flag = bool(exec_match.group(1))
        actual_cmd = exec_match.group(2).strip()
        if not is_safe_command(actual_cmd):
            return jsonify({"output": "禁止的命令", "returncode": -1})
        if async_flag:
            task = AsyncTask(actual_cmd, truncate=truncate)
            with tasks_lock:
                tasks[task.id] = task
            return jsonify({"output": f"异步任务已启动，任务ID: {task.id}", "returncode": 0})
        else:
            # ★ 多行命令走临时 bat（见 run_command_maybe_multiline 的说明）
            out, ret = run_command_maybe_multiline(actual_cmd, timeout=timeout, idle_timeout=idle_timeout)
            out = _maybe_persist_output(out, truncate, "exec")
            return jsonify({"output": out, "returncode": ret})

    # ---- #process ----
    process_match = re.match(r'^#process\s+(\S+)', cmd)
    if process_match:
        task_id = process_match.group(1)
        with tasks_lock:
            task = tasks.get(task_id)
        if not task:
            return jsonify({"output": f"任务 {task_id} 不存在", "returncode": -1})
        st = task.get_status()
        out = st.get("output") or ""
        if len(out) > truncate:
            st["output"] = out[:truncate] + f"\n…（截断，原长度 {len(out)}）"
        return jsonify({"output": json.dumps(st, ensure_ascii=False, indent=2), "returncode": 0})

    # ---- #kill ----
    kill_match = re.match(r'^#kill\s+(\S+)', cmd)
    if kill_match:
        task_id = kill_match.group(1)
        with tasks_lock:
            task = tasks.get(task_id)
        if not task:
            return jsonify({"output": f"任务 {task_id} 不存在", "returncode": -1})
        task.kill()
        return jsonify({"output": f"任务 {task_id} 已终止", "returncode": 0})

    # ---- #timer ----
    timer_match = re.match(r'^#timer\s+(.+)', cmd, re.DOTALL)
    if timer_match:
        actual_cmd = timer_match.group(1).strip()
        if not is_safe_command(actual_cmd):
            return jsonify({"output": "禁止的命令", "returncode": -1})
        task = AsyncTask(actual_cmd, truncate=truncate)
        with tasks_lock:
            tasks[task.id] = task
        return jsonify({"output": f"Timer 任务已启动，ID: {task.id}", "returncode": 0})

    # ---- #replace ----
    if cmd.startswith('#replace '):
        out, ret = execute_replace_command(cmd)
        return jsonify({"output": out, "returncode": ret})

    # ---- #write ----
    if cmd.startswith('#write '):
        out, ret = execute_write_command(cmd)
        return jsonify({"output": out, "returncode": ret})

    # ---- #read_file <path> [--start=N] [--end=M] [--max-chars=K] ----
    # ★ 2026-09-29（按用户要求）：专用于读大文件，**绝不走 _maybe_persist_output**。
    #   普通工具（execute_command 的 type/cat、run_script、fetch_page）输出超过
    #   「工具输出截断长度」会被替换成“预览 + 落盘路径”，模型反而看不到完整内容 ——
    #   于是它永远读不到自己刚写的几千行代码、也读不到 fetch_page 落盘的网页原文。
    #   本命令完整返回文件内容（只受调用方给的 max_chars 上限保护，默认 20 万字符），
    #   不落盘、不生成预览。
    if cmd.startswith('#read_file'):
        raw = cmd[len('#read_file'):].strip()
        if not raw:
            return jsonify({"output": "缺少文件路径", "returncode": -1})
        # 解析 --start= / --end= / --max-chars=（路径里可能有空格，所以参数单独抽）
        start_line = end_line = None
        max_chars = 200000
        mcs = re.search(r'--max-chars=(\d+)', raw)
        if mcs:
            try:
                max_chars = max(1000, int(mcs.group(1)))
            except Exception:
                pass
            raw = raw.replace(mcs.group(0), ' ')
        ms = re.search(r'--start=(\d+)', raw)
        if ms:
            try:
                start_line = max(1, int(ms.group(1)))
            except Exception:
                start_line = None
            raw = raw.replace(ms.group(0), ' ')
        me = re.search(r'--end=(\d+)', raw)
        if me:
            try:
                end_line = max(1, int(me.group(1)))
            except Exception:
                end_line = None
            raw = raw.replace(me.group(0), ' ')
        path_arg = raw.strip().strip('"').strip("'").strip()
        if not path_arg:
            return jsonify({"output": "缺少文件路径", "returncode": -1})
        p = os.path.expanduser(os.path.expandvars(path_arg))
        if not os.path.isabs(p):
            p = os.path.join(WORK_DIR, p)
        if not os.path.isfile(p):
            return jsonify({"output": f"文件不存在：{p}", "returncode": -1})
        try:
            size = os.path.getsize(p)
            # 大文件按行切片读取，避免一次性把超大文件读进内存
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            total = len(all_lines)
            s = (start_line or 1)
            e = (end_line or total)
            s = max(1, min(s, total)) if total else 1
            e = max(s, min(e, total))
            body = ''.join(all_lines[s - 1:e])
            truncated = False
            if len(body) > max_chars:
                body = body[:max_chars]
                truncated = True
            head = (f"文件：{p}\n"
                    f"总行数：{total}  总大小：{size} 字节  本次返回第 {s}-{e} 行\n")
            if truncated:
                head += (f"⚠️ 本次内容超过 max_chars={max_chars} 已被截断；"
                         f"请用 start_line/end_line 分段读取剩余部分"
                         f"（下次从第 {s + body.count(chr(10))} 行继续）。\n")
            head += "----- 内容开始 -----\n"
            logging.info(f"[READ_FILE] {p} 行 {s}-{e}/{total}，返回 {len(body)} 字符")
            return jsonify({"output": head + body + "\n----- 内容结束 -----", "returncode": 0})
        except Exception as ex:
            return jsonify({"output": f"读取失败：{ex}", "returncode": -1})

    # ---- #mem_save / #mem_delete / #mem_list / #mem_read：本地记忆库（client/workspace/memories）----
    # 供服务端 #save local / #delete local / #list local / #load local 命令经浏览器中转调用。
    if cmd.startswith('#mem_save '):
        # 格式：#mem_save <文件名>\n<JSON内容>
        nl = cmd.find('\n')
        if nl == -1:
            return jsonify({"output": "格式错误：#mem_save <文件名>\\n<JSON内容>", "returncode": -1})
        fname = os.path.basename(cmd[9:nl].strip())
        content = cmd[nl + 1:]
        if not fname:
            return jsonify({"output": "缺少文件名", "returncode": -1})
        if not fname.lower().endswith('.json'):
            fname += '.json'
        mem_dir = os.path.join(WORK_DIR, 'memories')
        try:
            os.makedirs(mem_dir, exist_ok=True)
            with open(os.path.join(mem_dir, fname), 'w', encoding='utf-8') as f:
                f.write(content)
            return jsonify({"output": f"已保存本地记忆：{fname}", "returncode": 0})
        except Exception as e:
            return jsonify({"output": f"保存失败: {e}", "returncode": -1})

    if cmd.startswith('#mem_delete '):
        fname = os.path.basename(cmd[11:].strip())
        if not fname:
            return jsonify({"output": "缺少文件名", "returncode": -1})
        if not fname.lower().endswith('.json'):
            fname += '.json'
        path = os.path.join(WORK_DIR, 'memories', fname)
        try:
            if os.path.isfile(path):
                os.remove(path)
                return jsonify({"output": f"已删除本地记忆：{fname}", "returncode": 0})
            return jsonify({"output": f"本地记忆不存在：{fname}", "returncode": -1})
        except Exception as e:
            return jsonify({"output": f"删除失败: {e}", "returncode": -1})

    if cmd.startswith('#mem_list'):
        mem_dir = os.path.join(WORK_DIR, 'memories')
        entries = []
        try:
            if os.path.isdir(mem_dir):
                for fn in os.listdir(mem_dir):
                    fp = os.path.join(mem_dir, fn)
                    if os.path.isfile(fp):
                        try:
                            st = os.stat(fp)
                            entries.append({"name": fn, "mtime": st.st_mtime, "size": st.st_size})
                        except Exception:
                            pass
        except Exception as e:
            return jsonify({"output": json.dumps({"error": str(e)}, ensure_ascii=False), "returncode": -1})
        entries.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        return jsonify({"output": json.dumps(entries, ensure_ascii=False), "returncode": 0})

    if cmd.startswith('#mem_search'):
        # ★ 2026-09-21：本地记忆「标题 + 内容」多关键词搜索（供 search_memory 工具使用）。
        #   服务器与客户端可能不在同一台机器，所以内容搜索必须在客户端本地完成，
        #   只把命中的摘要回传，避免把整个记忆库传回服务器。
        raw = cmd[len('#mem_search'):].strip()
        try:
            spec = json.loads(raw) if raw else {}
        except Exception:
            spec = {"keywords": raw.split()}
        if not isinstance(spec, dict):
            spec = {"keywords": raw.split()}
        kws = [str(k).strip() for k in (spec.get('keywords') or []) if str(k).strip()]
        if not kws:
            return jsonify({"output": "缺少关键词", "returncode": -1})
        mode = spec.get('mode') if spec.get('mode') in ('all', 'any') else 'all'
        search_in = spec.get('search_in') if spec.get('search_in') in ('both', 'title', 'content') else 'both'
        try:
            limit = max(1, min(50, int(spec.get('limit') or 10)))
        except Exception:
            limit = 10
        try:
            width = max(40, min(1000, int(spec.get('context_chars') or 160)))
        except Exception:
            width = 160

        def _extract_text(payload):
            if not isinstance(payload, dict):
                return ''
            parts = []
            for m in (payload.get('messages') or []):
                if not isinstance(m, dict):
                    continue
                role = m.get('role') or '?'
                c = m.get('content')
                if isinstance(c, list):
                    c = ' '.join(str(p.get('text', '')) for p in c if isinstance(p, dict))
                if isinstance(c, str) and c.strip():
                    parts.append('[%s] %s' % (role, c))
                rc = m.get('reasoning_content')
                if isinstance(rc, str) and rc.strip():
                    parts.append('[thinking] %s' % rc)
                if m.get('tool_calls'):
                    try:
                        parts.append('[tool_calls] ' + json.dumps(m['tool_calls'], ensure_ascii=False))
                    except Exception:
                        pass
            plan = payload.get('plan')
            if isinstance(plan, dict) and plan.get('title'):
                parts.append('[plan] %s' % plan.get('title'))
                for s in (plan.get('steps') or []):
                    if isinstance(s, dict) and s.get('text'):
                        parts.append('[plan-step] %s' % s['text'])
            return '\n'.join(parts)

        def _snippet(text):
            low = text.lower()
            pos = -1
            for kw in kws:
                p = low.find(kw.lower())
                if p >= 0 and (pos < 0 or p < pos):
                    pos = p
            if pos < 0:
                head = text[:width]
                return head + ('…' if len(text) > width else '')
            start = max(0, pos - width // 3)
            end = min(len(text), start + width)
            seg = text[start:end]
            for kw in sorted(kws, key=len, reverse=True):
                if not kw:
                    continue
                try:
                    seg = re.sub(re.escape(kw), '【%s】' % kw, seg, flags=re.IGNORECASE)
                except Exception:
                    pass
            return ('…' if start > 0 else '') + seg + ('…' if end < len(text) else '')

        mem_dir = os.path.join(WORK_DIR, 'memories')
        out = []
        try:
            names = []
            if os.path.isdir(mem_dir):
                for fn in os.listdir(mem_dir):
                    fp = os.path.join(mem_dir, fn)
                    if os.path.isfile(fp):
                        try:
                            st = os.stat(fp)
                            names.append({"name": fn, "mtime": st.st_mtime, "size": st.st_size, "path": fp})
                        except Exception:
                            pass
            names.sort(key=lambda x: x.get('mtime', 0), reverse=True)
            for e in names:
                name = e['name']
                title_hits = [k for k in kws if k.lower() in name.lower()]
                content = ''
                if search_in in ('both', 'content'):
                    try:
                        with open(e['path'], 'r', encoding='utf-8', errors='replace') as f:
                            payload = json.load(f)
                        content = _extract_text(payload)
                    except Exception:
                        content = ''
                content_hits = [k for k in kws if k.lower() in content.lower()] if content else []
                if search_in == 'title':
                    hit = (len(title_hits) == len(kws)) if mode == 'all' else (len(title_hits) > 0)
                elif search_in == 'content':
                    hit = (len(content_hits) == len(kws)) if mode == 'all' else (len(content_hits) > 0)
                else:
                    merged = set(k.lower() for k in title_hits) | set(k.lower() for k in content_hits)
                    hit = (len(merged) == len(kws)) if mode == 'all' else (len(merged) > 0)
                if not hit:
                    continue
                out.append({"name": name, "mtime": e['mtime'], "size": e['size'],
                            "title_hits": title_hits, "content_hits": content_hits,
                            "snippet": _snippet(content) if content else ""})
        except Exception as ex:
            return jsonify({"output": json.dumps({"error": str(ex)}, ensure_ascii=False), "returncode": -1})
        out.sort(key=lambda x: (len(x.get('content_hits') or []) + len(x.get('title_hits') or []), x.get('mtime', 0)),
                 reverse=True)
        return jsonify({"output": json.dumps(out[:limit], ensure_ascii=False), "returncode": 0})

    if cmd.startswith('#mem_read '):
        fname = os.path.basename(cmd[10:].strip())
        if not fname:
            return jsonify({"output": "缺少文件名", "returncode": -1})
        if not fname.lower().endswith('.json'):
            fname += '.json'
        path = os.path.join(WORK_DIR, 'memories', fname)
        try:
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return jsonify({"output": f.read(), "returncode": 0})
            return jsonify({"output": f"本地记忆不存在：{fname}", "returncode": -1})
        except Exception as e:
            return jsonify({"output": f"读取失败: {e}", "returncode": -1})

    # ---- #run_python / #run_cpp / #run_bash / #run_node ----
    run_match = re.match(r'^#run_(\w+)\s*\n(.*)', cmd, re.DOTALL)
    if run_match:
        lang = run_match.group(1).lower()
        code = run_match.group(2)
        out, ret = execute_run_command(lang, code)
        out = _maybe_persist_output(out, truncate, f"run_{lang}")
        return jsonify({"output": out, "returncode": ret})

    # ---- #view_image <path>：读取本地图片并返回 base64 data URL（供模型图像理解） ----
    if cmd.startswith('#view_image'):
        img_path = cmd[len('#view_image'):].strip().strip('"').strip("'")
        if not img_path:
            return jsonify({"output": json.dumps({"ok": False, "error": "缺少图片路径"}, ensure_ascii=False), "returncode": -1})
        import base64 as _b64
        try:
            p = os.path.expanduser(os.path.expandvars(img_path))
            if not os.path.isabs(p):
                # 相对路径：相对工作目录（client/workspace）
                p = os.path.join(WORK_DIR, p)
            p = os.path.abspath(p)
            if not os.path.isfile(p):
                return jsonify({"output": json.dumps({"ok": False, "error": f"图片文件不存在：{p}"}, ensure_ascii=False), "returncode": -1})
            size = os.path.getsize(p)
            if size == 0:
                return jsonify({"output": json.dumps({"ok": False, "error": "图片文件为空（0 字节）"}, ensure_ascii=False), "returncode": -1})
            if size > 32 * 1024 * 1024:
                return jsonify({"output": json.dumps({"ok": False, "error": f"图片过大（{size} 字节，最大支持 32MB）"}, ensure_ascii=False), "returncode": -1})
            # 按文件实际内容识别格式（官方按内容判断，而非扩展名）
            mime = None
            with open(p, 'rb') as f:
                head = f.read(16)
                data = head + f.read()
            if data[:3] == b'\xff\xd8\xff':
                mime = 'image/jpeg'
            elif data[:8] == b'\x89PNG\r\n\x1a\n':
                mime = 'image/png'
            elif data[:6] in (b'GIF87a', b'GIF89a'):
                mime = 'image/gif'
            elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
                mime = 'image/webp'
            if not mime:
                ext = os.path.splitext(p)[1].lower().lstrip('.')
                ext_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
                mime = ext_map.get(ext)
            if not mime:
                return jsonify({"output": json.dumps({"ok": False, "error": "不支持的图片格式（仅支持 JPEG/PNG/GIF/WebP）"}, ensure_ascii=False), "returncode": -1})
            data_url = f"data:{mime};base64,{_b64.b64encode(data).decode('ascii')}"
            return jsonify({"output": json.dumps({
                "ok": True, "type": "image", "name": os.path.basename(p), "path": p,
                "mime": mime, "size": size, "data_url": data_url,
            }, ensure_ascii=False), "returncode": 0})
        except Exception as e:
            return jsonify({"output": json.dumps({"ok": False, "error": f"读取图片异常：{e}"}, ensure_ascii=False), "returncode": -1})

    # ---- #fetch_page ----
    if cmd.startswith('#fetch_page '):
        url_part = cmd[12:].strip()
        only_raw = '--raw' in url_part or '--only-raw' in url_part
        only_text = '--text' in url_part or '--only-text' in url_part
        url = url_part.replace('--text','').replace('--only-text','').replace('--raw','').replace('--only-raw','').strip()
        try:
            raw_html, text_content, error = fetch_with_puppeteer(url)
        except Exception as e:
            return jsonify({"output": f"抓取异常: {str(e)}", "returncode": -1})

        if error:
            return jsonify({"output": f"抓取失败: {error}", "returncode": -1})

        if only_raw:
            output = raw_html or ""
        else:
            output = text_content or raw_html or ""
        output = _maybe_persist_output(output, truncate, "fetch")
        return jsonify({"output": output, "returncode": 0})

    # ---- 普通命令 ----
    if not is_safe_command(cmd):
        return jsonify({"output": "禁止的命令", "returncode": -1})
    out, ret = execute_with_timeout(cmd, shell=True)
    out = _maybe_persist_output(out, truncate, "exec")
    return jsonify({"output": out, "returncode": ret})

@app.route('/open', methods=['POST'])
def open_file():
    data = request.get_json()
    path = data.get('path', '') if data else ''
    if not path: return jsonify({"error": "缺少 path"}), 400
    if not os.path.exists(path): return jsonify({"error": f"文件不存在: {path}"}), 404
    try:
        if platform.system() == 'Windows': os.startfile(path)
        elif platform.system() == 'Darwin': subprocess.Popen(['open', path], creationflags=_CREATE_NO_WINDOW)
        else: subprocess.Popen(['xdg-open', path], creationflags=_CREATE_NO_WINDOW)
        return jsonify({"success": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/open_folder', methods=['POST'])
def open_folder():
    """在系统文件管理器中打开 path 所在目录；若 path 是文件则定位并选中它。
    供前端文件卡片右键菜单「打开文件所在位置」调用（不经服务器中转）。"""
    data = request.get_json() or {}
    path = (data.get('path') or '').strip().strip('"')
    if not path:
        return jsonify({"error": "缺少 path"}), 400
    # 允许传入尚不存在的文件路径（打开其父目录），因此不强制 exists 校验
    try:
        system = platform.system()
        if system == 'Windows':
            target = os.path.abspath(path)
            if os.path.isdir(target):
                # 目录：直接打开该目录
                subprocess.Popen(['explorer', target], creationflags=_CREATE_NO_WINDOW)
            else:
                parent = os.path.dirname(target)
                if not os.path.isdir(parent):
                    # 父目录也不存在：向上找到最近存在的祖先目录
                    probe = parent
                    while probe and not os.path.isdir(probe):
                        nxt = os.path.dirname(probe)
                        if nxt == probe:
                            break
                        probe = nxt
                    if probe and os.path.isdir(probe):
                        subprocess.Popen(['explorer', probe], creationflags=_CREATE_NO_WINDOW)
                        return jsonify({"success": True, "opened": probe,
                                        "note": f"目标或其父目录不存在，已打开最近的上级目录: {probe}"})
                    return jsonify({"error": f"目录不存在: {parent}"}), 404
                # explorer /select,<文件> 会在资源管理器中定位并选中该文件
                subprocess.Popen(['explorer', '/select,' + target], creationflags=_CREATE_NO_WINDOW)
        elif system == 'Darwin':
            if os.path.isdir(path):
                subprocess.Popen(['open', path], creationflags=_CREATE_NO_WINDOW)
            else:
                subprocess.Popen(['open', '-R', os.path.abspath(path)], creationflags=_CREATE_NO_WINDOW)
        else:
            d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
            subprocess.Popen(['xdg-open', d or '.'], creationflags=_CREATE_NO_WINDOW)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

# ================= 虚拟桌面 + OCR + 模拟输入（REST） =================
@app.route('/vdesk', methods=['POST'])
def vdesk_api():
    data = request.get_json() or {}
    action = data.get('action', '')
    real = '--real ' if data.get('real') else ''
    cmd_map = {
        'create': f"#vdesk {real}create",
        'list': f"#vdesk {real}list",
        'info': f"#vdesk {real}info",
        'close': f"#vdesk {real}close",
        'launch': f"#vdesk {real}launch {data.get('command', '')}",
        'target': f"#vdesk {real}target {data.get('hwnd', '')}",
        'shot': f"#vdesk {real}shot {'--desktop' if data.get('scope') == 'desktop' else '--window'}",
    }
    cmd = cmd_map.get(action)
    if not cmd:
        return jsonify({"error": f"未知 action: {action}，可选: {list(cmd_map)}"}), 400
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    out, ret = virtual_ui.handle_command(cmd)
    return jsonify({"output": out, "returncode": ret})

@app.route('/ocr', methods=['POST'])
def ocr_api():
    data = request.get_json() or {}
    cmd = "#ocr"
    if data.get('real'):
        cmd += " --real"
    if data.get('window'):
        cmd += " --window"
    if data.get('desktop'):
        cmd += " --desktop"
    if data.get('lang'):
        cmd += f" --lang={data['lang']}"
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    out, ret = virtual_ui.handle_command(cmd)
    return jsonify({"output": out, "returncode": ret})

@app.route('/input', methods=['POST'])
def input_api():
    data = request.get_json() or {}
    action = data.get('action', '')
    parts = [action]
    for k in ('x', 'y', 'x1', 'y1', 'x2', 'y2', 'delta', 'key', 'hotkey'):
        if data.get(k) is not None:
            parts.append(str(data[k]))
    if data.get('text') is not None:
        parts.append('"' + str(data['text']).replace('"', '\\"') + '"')
    if data.get('button'):
        parts.append(f"--button={data['button']}")
    if data.get('double'):
        parts.append("--double")
    if data.get('screen'):
        parts.append("--screen")
    if data.get('steps'):
        parts.append(f"--steps={data['steps']}")
    if data.get('real'):
        parts.append("--real")
    cmd = "#input " + " ".join(parts)
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    out, ret = virtual_ui.handle_command(cmd)
    return jsonify({"output": out, "returncode": ret})

@app.route('/vui/shot', methods=['GET'])
def vui_shot():
    """实时预览截图接口：返回 PNG 图片（供前端 <img> 轮询展示 AI 屏幕操作）。

    Query 参数:
      scope: window(默认，目标窗口) | desktop(整个桌面)
      real:  1=真实桌面，0/缺省=虚拟桌面
      no_mark: 1=不画 OCR 标注框（预览默认不加框，更快）
    """
    from flask import send_file, Response
    scope = request.args.get('scope', 'window')
    real = request.args.get('real', '0') == '1'
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    try:
        vui = virtual_ui.get()
        if real:
            r = vui.screenshot_real(scope=scope)
        else:
            r = vui.screenshot(scope=scope)
        if 'error' in r:
            return jsonify({"error": r['error']}), 500
        p = r.get('path')
        if not p or not os.path.exists(p):
            return jsonify({"error": "截图文件不存在"}), 500
        return send_file(p, mimetype='image/png')
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/vui/status', methods=['GET'])
def vui_status():
    if VUI is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    try:
        info = VUI.get_info()
        try:
            info['real'] = VUI.get_info_real()
        except Exception:
            info['real'] = {}
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/vui/screenshot_stats', methods=['GET'])
def vui_screenshot_stats():
    """统计 screenshots 截图缓存目录中的文件数与总大小（不删除），供前端按钮显示。"""
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    try:
        vui = virtual_ui.get()
        shot_dir = getattr(vui, 'screenshot_dir', None)
        if not shot_dir or not os.path.isdir(shot_dir):
            return jsonify({"error": f"截图缓存目录不存在: {shot_dir}"}), 404
        count = 0
        total = 0
        for name in os.listdir(shot_dir):
            p = os.path.join(shot_dir, name)
            try:
                if os.path.isfile(p):
                    count += 1
                    total += os.path.getsize(p)
                elif os.path.isdir(p) and name.lower() not in ('.', '..'):
                    for root, dirs, files in os.walk(p):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                count += 1
                                total += os.path.getsize(fp)
                            except Exception:
                                pass
            except Exception:
                pass
        return jsonify({"success": True, "count": count, "bytes": total, "dir": shot_dir})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/vui/clear_screenshots', methods=['POST'])
def vui_clear_screenshots():
    """清除 screenshots 截图缓存目录中的所有文件（bmp/png/jpg 等），释放磁盘空间。"""
    if virtual_ui is None:
        return jsonify({"error": "屏幕操作组件缺失（workspace/GUI 目录不存在）"}), 500
    try:
        vui = virtual_ui.get()
        shot_dir = getattr(vui, 'screenshot_dir', None)
        if not shot_dir or not os.path.isdir(shot_dir):
            return jsonify({"error": f"截图缓存目录不存在: {shot_dir}"}), 404
        removed = 0
        freed = 0
        for name in os.listdir(shot_dir):
            p = os.path.join(shot_dir, name)
            try:
                if os.path.isfile(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                elif os.path.isdir(p) and name.lower() not in ('.', '..'):
                    for root, dirs, files in os.walk(p, topdown=False):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                freed += os.path.getsize(fp)
                                os.remove(fp)
                            except Exception:
                                pass
                        for dn in dirs:
                            try:
                                os.rmdir(os.path.join(root, dn))
                            except Exception:
                                pass
                    try:
                        os.rmdir(p)
                    except Exception:
                        pass
                    removed += 1
            except Exception:
                pass
        return jsonify({"success": True, "removed": removed, "freed_bytes": freed, "dir": shot_dir})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _services_for_control():
    """控制面板要管理的服务清单（client 端：**只有它自己**）。

    ★ client 是「本机操作代理」，它不该去管服务端 —— 两者甚至可能不在同一台机器上
      （见根目录 AGENTS.md：server 可部署在远程，agent 必须与浏览器同机）。
      所以这里只列出 agent 自己。
    """
    frozen = getattr(sys, 'frozen', False)
    return [
        {
            'key': 'agent', 'label': '本机代理 (操作执行)', 'port': 5001,
            'env_ports': ['AGENT_PORT', 'PORT'], 'cwd': BASE_DIR,
            'exe': sys.executable if frozen else None,
            # ★ argv 必须是 ['--serve']：不带参数现在等于开控制面板，
            #   若留空，面板点启动会再开出一个面板而不是起服务。
            'script': 'agent.py', 'argv': ['--serve'],
        },
    ]


def _run_control_panel():
    """打开产品自带的控制面板（--control）。"""
    # ★ control.py 与本文件同级（打包/使用时会同步过来）。
    #   不放在项目根目录的独立包里 —— 那样单独分发 client 就会缺依赖。
    try:
        sys.path.insert(0, BASE_DIR)
        import control
    except Exception as e:
        _safe_print(f'[错误] 无法加载控制面板模块: {e}')
        return 1
    return control.run_control_panel(
        _services_for_control(),
        title='DeepChat 控制面板（本机代理）',
        state_dir=BASE_DIR,
        open_url=None,      # agent 端不负责打开 Web 界面（那是服务端的事）
    )


if __name__ == '__main__':
    # ★ 参数约定（产品入口就这两个）：
    #     --control / -c / --gui   打开图形控制面板（client 端面板只管 agent 自己）
    #     --serve                  直接起服务、不开面板
    #   **不带任何参数 = 打开控制面板。**
    #
    #   为什么默认是面板：exe 是无控制台构建（--noconsole），双击时既没有窗口
    #   也没有任何输出，用户只看到"点了没反应"，以为打开失败了。
    #   ★ 另外要先解析参数再碰控制台 API —— pythonw / 无控制台时调用
    #     SetConsoleTitleW 会失败甚至弹错误框。
    _args = [a.lower() for a in sys.argv[1:]]
    if not any(a in ('--serve', '--service') for a in _args):
        sys.exit(_run_control_panel())

    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW('DeepChat Agent')
    except Exception:
        pass
    port = int(os.environ.get('AGENT_PORT', '5001'))
    _safe_print(f" * 客户端 agent 启动，端口 {port}")
    # 仅监听回环地址，避免暴露到局域网
    app.run(debug=False, threaded=True, host='127.0.0.1', port=port)