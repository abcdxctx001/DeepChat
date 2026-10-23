import os, json, subprocess, platform, time, re, chardet, socket, threading, logging, sys, uuid, queue, requests, types, zipfile, io, html, traceback, copy
import flask
import contextvars
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, send_from_directory, send_file, make_response
from flask_cors import CORS
from openai import OpenAI
from datetime import datetime

# Flask 2.0+ 使用 download_name 参数，旧版（如 1.1.x）使用 attachment_filename。
# 兼容两种环境，避免 send_file / send_from_directory 因未知参数抛 TypeError。
FLASK_MAJOR = int(getattr(flask, '__version__', '1').split('.')[0])
def _send_kw(name):
    """构造 send_file/send_from_directory 的文件名关键字参数，兼容新旧 Flask。"""
    return {'download_name': name} if FLASK_MAJOR >= 2 else {'attachment_filename': name}

# ★ BUGFIX(2026-09-28)：图片路由以前传 max_age=31536000，而 Flask 1.x 的
#   send_file/send_from_directory **没有** max_age 参数 → TypeError →
#   /api/uploads/* 与 /api/image/* 全部 500（日志里成片的
#   "send_file() got an unexpected keyword argument 'max_age'"），
#   表现为聊天里的图片/缩略图打不开。这里统一用 _img_response 返回，
#   自行设置 Content-Type 与缓存头，新旧 Flask 都能用。
def _img_response(path, mime, max_age=31536000):
    """把本地图片文件作为响应返回（兼容 Flask 1.x，缓存头自行设置）。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception as e:
        return jsonify({"error": f"读取图片失败: {e}"}), 500
    resp = flask.make_response(data)
    resp.headers['Content-Type'] = mime
    resp.headers['Cache-Control'] = f'public, max-age={int(max_age)}'
    return resp

try:
    import PyPDF2
except:
    PyPDF2 = None
try:
    from docx import Document
except:
    Document = None
try:
    import openpyxl
except:
    openpyxl = None
try:
    from pptx import Presentation
except:
    Presentation = None

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
        if tag in ('style', 'script'): self.ignore_level += 1; return
        if self.ignore_level > 0: return
        d = dict(attrs)
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'): self.output.append('\n' + '#' * int(tag[1]) + ' ')
        elif tag == 'p': self.output.append('\n\n')
        elif tag == 'br': self.output.append('\n')
        elif tag in ('ul', 'ol'): self.output.append('\n'); self.in_list = True; self.list_type = tag; self.ol_count = 0
        elif tag == 'li':
            if self.in_list:
                if self.list_type == 'ul': self.output.append('- ')
                else: self.ol_count += 1; self.output.append(f'{self.ol_count}. ')
        elif tag == 'a': self.output.append('['); self._last_a_href = d.get('href', '')
        elif tag == 'img': self.output.append(f'![{d.get("alt", "图片")}]({d.get("src", "")})')
        elif tag in ('strong', 'b'): self.output.append('**')
        elif tag in ('em', 'i'): self.output.append('*')
        elif tag == 'code': self.output.append('`')
        elif tag == 'pre': self.output.append('\n```\n')
        elif tag == 'blockquote': self.output.append('\n> ')

    def handle_endtag(self, tag):
        if tag in ('style', 'script'): self.ignore_level = max(0, self.ignore_level - 1); return
        if self.ignore_level > 0: return
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'): self.output.append('\n')
        elif tag == 'p': self.output.append('\n')
        elif tag in ('ul', 'ol'): self.in_list = False; self.output.append('\n')
        elif tag == 'li': self.output.append('\n')
        elif tag == 'a':
            href = getattr(self, '_last_a_href', '')
            if href: self.output.append(f']({href})')
            else: self.output.append(']')
        elif tag in ('strong', 'b'): self.output.append('**')
        elif tag in ('em', 'i'): self.output.append('*')
        elif tag == 'code': self.output.append('`')
        elif tag == 'pre': self.output.append('\n```\n')
        elif tag == 'blockquote': self.output.append('\n')

    def handle_data(self, data):
        if self.ignore_level > 0: return
        self.output.append(data)

    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', ''.join(self.output)).strip()

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ★ 日志初始化加固（配合 exe 以「无控制台」方式运行）：
#   PyInstaller 的 --noconsole 构建里 sys.stderr 可能是 None，
#   此时 `logging.StreamHandler()` 会在写日志时抛异常、把请求打挂。
#   所以只在确实有 stderr 时才挂 StreamHandler；文件句柄必须有 ——
#   控制面板读的就是这个文件，没有它界面上就看不到任何日志。
def _log_dir():
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
        else os.path.dirname(os.path.abspath(__file__))
    return base

_log_handlers = []
try:
    _log_handlers.append(logging.FileHandler(
        os.path.join(_log_dir(), 'server.log'), encoding='utf-8'))
except Exception:
    pass
if sys.stderr is not None:
    try:
        _log_handlers.append(logging.StreamHandler())
    except Exception:
        pass
if not _log_handlers:
    _log_handlers.append(logging.NullHandler())
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=_log_handlers)

# 项目根目录/服务器目录绝对路径基准。
# 注意：以 `python app.py` 方式启动时 __file__ 是相对路径（如 "app.py"），
# 直接 os.path.dirname(__file__) 会得到空串，导致下方 CLIENT_DIR / WORK_DIR
# 变成相对路径、相对错误的工作目录解析，使 agent.exe / agent.py 等文件
# 被误判为不存在，下载代理界面显示为空。此处统一转为绝对路径修复。
# PyInstaller 打包后（app.exe）：__file__ 指向临时解压目录（_MEIPASS），
# 不能作为基准；程序目录应以 exe 所在目录为准，api-key.env / workspace 均在其旁。
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def _read_legacy_api_key():
    """读取旧的 api-key.env（仅用于一次性迁移到 settings.json）。"""
    try:
        with open(os.path.join(BASE_DIR, 'api-key.env'), 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''

def _migrate_api_key_into_settings():
    """★ 2026-10-05：把 api-key.env 里的 key 迁移进 settings.json，然后删除该文件。

    历史原因 key 存在 BASE_DIR/api-key.env；现在统一由 settings.json 管理
    （可在前端「模型与价格」里改 base_url 与 api key）。
    迁移是幂等的：settings.json 里已有 api_key 就不再读旧文件。
    """
    try:
        st = load_server_settings()
        if (st.get('api_key') or '').strip():
            return
        legacy = _read_legacy_api_key()
        if not legacy:
            return
        st['api_key'] = legacy
        save_server_settings(st)
        try:
            os.remove(os.path.join(BASE_DIR, 'api-key.env'))
            logging.info("[API] 已把 api-key.env 迁移进 settings.json 并删除旧文件")
        except Exception as e:
            logging.warning(f"[API] 迁移后删除 api-key.env 失败（不影响使用）: {e}")
    except Exception as e:
        logging.warning(f"[API] api-key.env 迁移失败: {e}")

# ==================== 多提供商 / 每模型独立配置（2026-10-14） ====================
# ★ 2026-10-14（按用户要求）：模型与价格支持**无限添加模型**，每个模型可独立配置
#   base_url / api_key / 别名 / 模型名 / 单价。
#
#   数据结构（settings["providers"]）：
#       [{"name": "DeepSeek", "base_url": "...", "api_key": "sk-...", "enabled": true,
#         "models": [{"alias":"flash","label":"Flash","name":"deepseek-v4-flash",
#                     "ctx":"1M","max_out":"256K","input_types":["text","image"],
#                     "price":{"cached":[a,b],"uncached":[c,d],"output":[e,f]}}]}]
#
#   老键（base_url / api_key / flash_model / pro_model / prices）继续可用，
#   load_server_settings() 会自动迁移成 providers（见 _migrate_providers）。

DEFAULT_PROVIDER_NAME = "DeepSeek"
DEFAULT_BASE_URL = "https://api.deepseek.com"

def _default_price():
    """默认单价（元/百万 token，[缓存命中, 缓存未命中] 或 [输入, 输出]）。"""
    return {"cached": [0.02, 0.04], "uncached": [1, 2], "output": [4, 8]}

def _norm_price(pr):
    """规整单价字段，缺失项用默认值补齐。"""
    pr = pr if isinstance(pr, dict) else {}
    out = {}
    for k, dv in (("cached", [0.02, 0.04]), ("uncached", [1, 2]), ("output", [4, 8])):
        v = pr.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                out[k] = [float(v[0]), float(v[1])]
                continue
            except Exception:
                pass
        out[k] = list(dv)
    return out

def _norm_model(m, idx=0):
    """规整单个模型配置。"""
    m = m if isinstance(m, dict) else {}
    alias = str(m.get("alias") or "").strip()
    name = str(m.get("name") or "").strip()
    # ★ 2026-10-14（按用户反馈"为啥这里小写、那里大写"）：
    #   label 不再单独维护，**一律跟随 alias** —— 否则会出现
    #   卡片里显示 flash、下拉里却显示 Flash 的不一致（迁移时我把 label 写死成了 "Flash"）。
    #   想要大写就把别名本身写成 Flash。
    label = alias or name or f"模型{idx + 1}"
    it = m.get("input_types")
    if not isinstance(it, list) or not it:
        it = ["text"]
    it = [str(x) for x in it if str(x) in ("text", "image")] or ["text"]
    return {
        "alias": alias,
        "label": label,
        "name": name,
        "ctx": str(m.get("ctx") or "").strip(),
        "max_out": str(m.get("max_out") or "").strip(),
        "input_types": it,
        "price": _norm_price(m.get("price")),
    }

def _norm_provider(pr, idx=0):
    """规整单个提供商配置（含其模型列表）。"""
    pr = pr if isinstance(pr, dict) else {}
    models = pr.get("models")
    if not isinstance(models, list):
        models = []
    return {
        "name": str(pr.get("name") or "").strip() or f"{DEFAULT_PROVIDER_NAME}{idx + 1 if idx else ''}",
        "base_url": str(pr.get("base_url") or "").strip(),
        "api_key": str(pr.get("api_key") or "").strip(),
        "enabled": bool(pr.get("enabled", True)),
        "models": [_norm_model(m, k) for k, m in enumerate(models)],
    }

def _legacy_to_providers(st):
    """把老的扁平设置迁移成 providers 列表（只读，不写盘）。"""
    models = []
    fl = (st.get("flash_model") or "").strip()
    pm = (st.get("pro_model") or "").strip()
    prices = st.get("prices") if isinstance(st.get("prices"), dict) else {}
    if fl:
        models.append(_norm_model({"alias": "flash", "name": fl,
                                   "input_types": ["text", "image"],
                                   "price": prices.get("flash")}, 0))
    if pm:
        models.append(_norm_model({"alias": "pro", "name": pm,
                                   "input_types": ["text", "image"],
                                   "price": prices.get("pro")}, 1))
    if not models:
        return []
    return [_norm_provider({"name": DEFAULT_PROVIDER_NAME,
                            "base_url": (st.get("base_url") or "").strip() or DEFAULT_BASE_URL,
                            "api_key": (st.get("api_key") or "").strip(),
                            "enabled": True, "models": models}, 0)]

def get_providers():
    """返回规整后的 providers 列表（优先 settings["providers"]，否则由老键迁移）。"""
    try:
        st = load_server_settings()
    except Exception:
        st = {}
    provs = st.get("providers")
    if isinstance(provs, list) and provs:
        out = [_norm_provider(x, i) for i, x in enumerate(provs)]
        # 补：老 api_key 若在 provider 里为空，用它兜底
        legacy_key = (st.get("api_key") or "").strip() or _read_legacy_api_key()
        legacy_base = (st.get("base_url") or "").strip()
        for x in out:
            if not x["api_key"] and legacy_key:
                x["api_key"] = legacy_key
            if not x["base_url"] and legacy_base:
                x["base_url"] = legacy_base
        return out
    return _legacy_to_providers(st)

def iter_models():
    """遍历所有 (提供商, 模型) 对。"""
    for pr in get_providers():
        for m in pr.get("models") or []:
            yield pr, m

def resolve_model(model_name=None):
    """按模型名（或别名）找到 (提供商, 模型)；找不到返回 (None, None)。"""
    want = (model_name or "").strip()
    if not want:
        return None, None
    provs = get_providers()
    # ① 先按模型名精确匹配
    for pr in provs:
        for m in pr.get("models") or []:
            if m.get("name") == want:
                return pr, m
    # ② 再按别名匹配（flash / pro / 用户自定义别名）
    for pr in provs:
        for m in pr.get("models") or []:
            if m.get("alias") and m["alias"] == want:
                return pr, m
    return None, None

def list_model_aliases():
    """所有已配置模型的别名列表。"""
    return [m["alias"] for _pr, m in iter_models() if m.get("alias")]

# ★ 当前请求所用模型 —— 用 contextvars 传递，这样 get_client()/provider_supports_thinking()
#   的签名不必改动，调用点也无需逐一调整（改动面最小）。
_current_model_var = contextvars.ContextVar("dsh_current_model", default="")

def set_current_model(model_name):
    try:
        _current_model_var.set(str(model_name or ""))
    except Exception:
        pass

def get_current_model():
    try:
        return _current_model_var.get() or ""
    except Exception:
        return ""

def get_api_config(model_name=None):
    """返回 (api_key, base_url)。

    ★ 2026-10-14：支持多提供商 —— 优先按 model_name（或当前请求模型）找到其提供商；
      找不到时回退到老逻辑（settings 的 base_url/api_key），保证兼容。
    """
    mn = (model_name or "").strip() or get_current_model()
    if mn:
        pr, _m = resolve_model(mn)
        if pr:
            if pr.get("api_key"):
                return pr["api_key"], (pr.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    # —— 老逻辑（兼容）——
    try:
        st = load_server_settings()
    except Exception:
        st = {}
    key = (st.get('api_key') or '').strip() or _read_legacy_api_key()
    base = (st.get('base_url') or '').strip() or DEFAULT_BASE_URL
    # 若仍为空，尝试第一个提供商
    if not key:
        try:
            provs = get_providers()
            if provs:
                key = provs[0].get("api_key") or ""
                base = provs[0].get("base_url") or base
        except Exception:
            pass
    return key, base.rstrip('/')


def provider_supports_thinking():
    """★ 2026-10-10：当前接入的端点是否支持 DeepSeek 专有的思考参数？

    `thinking` / `reasoning_effort` 是 **DeepSeek 专有**的请求字段。
    以前它们被**无条件**发给所有 OpenAI 兼容端点，于是接别的服务（例如
    本地 Ollama 跑 qwen2.5）会直接 400：
        「"qwen2.5:3b" does not support thinking」

    判定依据：base_url 指向 deepseek.com，或 api_key 是 DeepSeek 的 sk- 形式。
    两者都不满足时按"通用 OpenAI 兼容端点"处理 —— 不发专有字段。
    """
    try:
        _key, base = get_api_config()
        b = (base or '').lower()
        if 'deepseek' in b:
            return True
        # 非 deepseek 域名但用的是国产 deepseek 转发站（key 为 sk- 且域名含 api）
        # 一律按不支持处理更安全：多发的字段会让多数端点直接报错。
        return False
    except Exception:
        return True   # 拿不到配置时维持原行为（DeepSeek 是默认端点）

def provider_extra_body(thinking):
    """需要时才返回 DeepSeek 专有的 extra_body；否则返回 None。"""
    return {"thinking": {"type": "enabled" if thinking else "disabled"}} \
        if provider_supports_thinking() else None

def get_client():
    """按当前设置构造 OpenAI 客户端。

    ★ 不能再用模块级单例：base_url / api_key 现在可以在前端修改，
      必须每次调用时按最新设置构造（OpenAI 客户端构造很轻，开销可忽略）。
    """
    key, base = get_api_config()
    if not key:
        raise ValueError("尚未配置 API Key，请在「设置 → 模型与价格」中填写")
    return OpenAI(api_key=key, base_url=base)

def mask_api_key(key):
    """★ 前端展示用的脱敏值：只暴露末 4 位（key 本身绝不返回给前端）。"""
    k = (key or '').strip()
    if not k:
        return ''
    if len(k) <= 8:
        return '*' * len(k)
    return '*' * 12 + k[-4:]

# 兼容旧调用点：仍保留这个名字，但改为实时读取
def load_api_key():
    key, _ = get_api_config()
    if key:
        return key
    raise ValueError("尚未配置 API Key")

cancel_event = None
cancel_lock = threading.Lock()

# 客户端代理文件路径：client.zip（完整包，含内置 Python/Node.js 等）与 agent.exe（轻量版，仅单个可执行文件）
CLIENT_DIR = os.path.join(os.path.dirname(BASE_DIR), 'client')
AGENT_EXE_PATH = os.path.join(CLIENT_DIR, 'agent.exe')

# ---------- 客户端组件定义（供自定义下载） ----------
def _fmt_size(size):
    """格式化文件大小为可读字符串"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

# 客户端工作目录
CLIENT_WORKSPACE = os.path.join(CLIENT_DIR, 'workspace')
# 屏幕操作组件目录（虚拟桌面 + OCR + 模拟键鼠）：统一位于 client/workspace/GUI
CLIENT_GUI_DIR = os.path.join(CLIENT_WORKSPACE, 'GUI')

CLIENT_COMPONENTS = {
    "agent_py": {
        "name": "agent.py + run.bat + control.py",
        "desc": "Python 脚本，需安装 flask、requests 等依赖",
        "paths": [
            os.path.join(CLIENT_DIR, "agent.py"),
            os.path.join(CLIENT_DIR, "run.bat"),
            # 控制面板是产品自带功能，agent.py 会 import control —— 少了它
            # 双击 run.bat 只会一直打点然后超时。必须随源码组件一起下发。
            os.path.join(CLIENT_DIR, "control.py"),
            os.path.join(CLIENT_DIR, "static"),
        ],
        "optional": True,
    },
    "agent_exe": {
        "name": "agent.exe",
        "desc": "免安装，无需 Python，开箱即用",
        "paths": [AGENT_EXE_PATH],
        "optional": True,
    },
    "screen_ui": {
        "name": "屏幕操作组件（GUI）",
        "desc": "虚拟桌面 / 屏幕OCR / 模拟键鼠，按需勾选",
        "paths": [
            os.path.join(CLIENT_GUI_DIR, "virtual_ui.py"),
            os.path.join(CLIENT_GUI_DIR, "ocr.ps1"),
            os.path.join(CLIENT_GUI_DIR, "capture_dll"),
            os.path.join(CLIENT_GUI_DIR, "README_SCREEN_UI.md"),
        ],
        "optional": True,
    },
    "fetch_page": {
        "name": "网页抓取 (fetch_page.js)",
        "desc": "Puppeteer 抓取脚本，需 Node.js",
        "paths": [os.path.join(CLIENT_WORKSPACE, "fetch_page.js")],
        "optional": True,
    },
    "python": {
        "name": "Python 3.14 解释器",
        "desc": "内置 Python 环境，含标准库和 pip",
        "paths": [os.path.join(CLIENT_WORKSPACE, "python")],
        "optional": True,
    },
    "node": {
        "name": "Node.js 运行时",
        "desc": "内置 Node.js，用于运行 JS 脚本和 Puppeteer",
        "paths": [os.path.join(CLIENT_WORKSPACE, "node")],
        "optional": True,
    },
    "mingw64": {
        "name": "C++ 编译器 (MinGW64)",
        "desc": "GCC/G++ 编译器，用于编译 C++ 代码",
        "paths": [os.path.join(CLIENT_WORKSPACE, "mingw64")],
        "optional": True,
    },
}

def _zip_rel_path(local_path):
    """把本地绝对路径转换为 client.zip 内的相对路径（统一正斜杠）。

    client.zip 的条目以项目根为基准（如 client/workspace/python/...），
    因此本地 client\\workspace\\python → client/workspace/python。
    """
    rel = os.path.relpath(local_path, CLIENT_DIR).replace('\\', '/')
    return 'client/' + rel

def _zip_component_info(comp):
    """检查组件在 client.zip 中是否存在，并累计其解压后总大小。

    返回 (exists, size)：所有组件统一以 client.zip 完整包为唯一来源，
    不再读取本地 client/ 目录。
    """
    if not os.path.exists(CLIENT_ZIP_PATH):
        return False, 0
    rels = [_zip_rel_path(p) for p in comp["paths"]]
    try:
        with zipfile.ZipFile(CLIENT_ZIP_PATH) as zf:
            infos = zf.infolist()
    except Exception:
        return False, 0
    exists = False
    size = 0
    for rel in rels:
        prefix = rel if rel.endswith('/') else rel + '/'
        for info in infos:
            n = info.filename
            if n == rel or n.startswith(prefix):
                exists = True
                if not n.endswith('/'):
                    size += info.file_size
    return exists, size

def _zip_entries_for_comp(comp):
    """返回组件在 client.zip 中对应的全部条目名（用于打包时提取）。

    所有组件统一从 client.zip 提取（不再读取本地 client/ 目录）。
    若 client.zip 缺失或损坏返回空列表。
    """
    if not os.path.exists(CLIENT_ZIP_PATH):
        return []
    rels = [_zip_rel_path(p) for p in comp["paths"]]
    try:
        with zipfile.ZipFile(CLIENT_ZIP_PATH) as zf:
            names = zf.namelist()
    except Exception:
        return []
    entries = []
    for rel in rels:
        prefix = rel if rel.endswith('/') else rel + '/'
        for n in names:
            if n == rel or n.startswith(prefix):
                entries.append(n)
    return entries

SYSTEM_PROMPT = """## Language Policy
- **Internal reasoning**: Use English for all chain-of-thought, planning, and tool-call reasoning. English reasoning has stronger training support and produces more stable formatting (fewer punctuation-collapse artifacts).
- **Final answer to user**: If the user's message is in Chinese, respond in Simplified Chinese. Otherwise respond in the user's language.

You are DeepSeek, an AI assistant created by DeepSeek. Your knowledge cutoff is May 2025. Context length is 1M, maximum output length is 384K. Reply in fluent Chinese unless the user writes in another language.

## Core Principles
- **Accuracy**: Say so plainly when you are unsure; never fabricate.
- **Deep reasoning**: For complex questions, reason thoroughly internally before answering. **Never let "fighting yourself" self-doubt appear in the final answer.** Once an answer is given, it must be confident and coherent.
- **Task completion**: For large or complex tasks, **you must finish the whole thing**; never give up midway for reasons such as "time is limited" or "it's impossible".
- **Safety**: Refuse harmful or illegal content.

## Output Standards
- Use Markdown to improve readability.
- Wrap LaTeX math in $...$ or $$...$$; do not use \\(...\\) or \\[...\\].
- **Bold formatting**: when using `**text**`, make sure the first and last characters inside the `**` are **not punctuation**.

## Interaction Style
- Default to Simplified Chinese; be friendly and professional.
"""

def _effective_chat_system():
    """对话模式系统提示词（★ 与操作模式完全独立）。

    优先级：prompts.chat_base（用户在“提示词”标签页里为对话模式单独保存的覆盖）
            → 内置 SYSTEM_PROMPT。
    注意：**不再**读取操作模式的 agent_base / 旧的 base —— 那是操作模式的 operator 提示词，
    混用会让对话模式变成“能执行系统命令的操作助手”，与旧版行为不符。
    """
    try:
        _p = (load_server_settings().get("prompts") or {}).get("chat_base") or ""
    except Exception:
        _p = ""
    return _p.strip() or SYSTEM_PROMPT


def _effective_child_base():
    """子 Agent 基础提示词：prompts.child_base 覆盖优先，否则内置 CHILD_SYSTEM_PROMPT。"""
    try:
        _p = (load_server_settings().get("prompts") or {}).get("child_base") or ""
    except Exception:
        _p = ""
    return _p.strip() or CHILD_SYSTEM_PROMPT

WORK_DIR = os.environ.get("WORK_DIR_OVERRIDE") or os.path.join(BASE_DIR, 'workspace')
UPLOAD_FOLDER = os.path.join(WORK_DIR, 'uploads')
# 云端记忆目录（服务器端）：对话上下文记忆文件统一存放于 server/workspace/memories；
# 本地记忆目录为客户端机器的 client/workspace/memories（通过客户端 #mem_* 命令中转读写）。
MEMORY_DIR = os.path.join(WORK_DIR, 'memories')
os.makedirs(MEMORY_DIR, exist_ok=True)

def _plan_get(session_id):
    """读取某会话的 plan（内存）。plan 的持久化跟随记忆文件：
    #save 时写入记忆 JSON 的 plan 字段，#load 时恢复。"""
    with sessions_lock:
        sess = sessions.get(session_id)
        return sess.get("plan") if sess else None

def _plan_put(session_id, plan):
    """写入某会话的 plan（内存）。"""
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess:
            sess["plan"] = plan

def _mem_filename(title):
    """把标题/文件名清理为安全的记忆文件名（强制 .json 后缀，防目录穿越）。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(title or 'untitled')).strip(' ._')
    if not name:
        name = 'untitled'
    if not name.lower().endswith('.json'):
        name += '.json'
    return name

def _mem_dump(internal, model=None, r_eff=None, think=None, usage=None, plan=None,
              pre_compress=None):
    """把当前对话内部消息序列化为记忆文件内容（去掉 system，保留完整 user/assistant/tool 历史）。

    plan        当前任务计划（持久化到记忆，load 时可恢复）。
    pre_compress ★ 2026-09-29（按用户要求）：**上下文压缩前的完整消息**。
        #compress 会把 internal 重建为 [system, 摘要]，压缩前的历史就从上下文里消失了；
        于是“记忆关键词搜索”永远搜不到压缩前的内容。
        这里把压缩前的消息另存一份（字段 pre_compress_messages），
        供 search_memory 检索；#load 时**只导入 messages**（压缩后的），不会把旧历史灌回上下文。
    """
    msgs = [m for m in internal if isinstance(m, dict) and m.get('role') != 'system']
    payload = {
        "format": "deepseek-memory-v1",
        "version": 1,
        "saved_at": datetime.now().isoformat(),
        "model": model,
        "reasoning_effort": r_eff,
        "thinking_enabled": think,
        "usage": usage or {},
        "messages": msgs,
    }
    if pre_compress:
        pc = [m for m in pre_compress if isinstance(m, dict) and m.get('role') != 'system']
        if pc:
            payload["pre_compress_messages"] = pc
    if plan:
        payload["plan"] = plan
    return json.dumps(payload, ensure_ascii=False, indent=2)

def _mem_load_cloud(fname):
    """读取云端记忆文件，返回解析后的 dict；不存在/损坏返回 None。"""
    path = os.path.join(MEMORY_DIR, fname)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _mem_list_cloud():
    """列出云端记忆：返回 [{name, mtime, size}]，按修改时间降序。"""
    out = []
    try:
        for fn in os.listdir(MEMORY_DIR):
            fp = os.path.join(MEMORY_DIR, fn)
            if os.path.isfile(fp):
                st = os.stat(fp)
                out.append({"name": fn, "mtime": st.st_mtime, "size": st.st_size})
    except Exception:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ==================== 记忆检索（search_memory 工具） ====================
def _mem_extract_text(payload):
    """把记忆文件的内容抽成可搜索的纯文本（含每条消息的角色前缀）。

    ★ 2026-09-29（按用户要求）：同时纳入 `pre_compress_messages`（上下文压缩前的历史存档），
      这样 search_memory 能搜到压缩前的内容；#load 只读 messages，不受影响。
    """
    if not isinstance(payload, dict):
        return ""
    parts = []

    def _append_msgs(msgs, tag=""):
        for m in (msgs or []):
            if not isinstance(m, dict):
                continue
            role = m.get("role") or "?"
            if tag:
                role = f"{tag}{role}"
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
            if isinstance(c, str) and c.strip():
                parts.append(f"[{role}] {c}")
            rc = m.get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                parts.append(f"[{tag}thinking] {rc}")
            if m.get("tool_calls"):
                try:
                    parts.append(f"[{tag}tool_calls] " + json.dumps(m["tool_calls"], ensure_ascii=False))
                except Exception:
                    pass

    _append_msgs(payload.get("messages"))
    # 压缩前的历史（标记为 compressed- 前缀，便于模型知道它来自压缩前）
    _append_msgs(payload.get("pre_compress_messages"), tag="压缩前-")
    plan = payload.get("plan")
    if isinstance(plan, dict) and plan.get("title"):
        parts.append(f"[plan] {plan.get('title')}")
        for s in (plan.get("steps") or []):
            if isinstance(s, dict) and s.get("text"):
                parts.append(f"[plan-step] {s['text']}")
    return "\n".join(parts)


def _mem_snippet(text, keywords, width=160):
    """截取第一处命中的上下文片段，并把关键词用 【】 标出。"""
    low = text.lower()
    pos = -1
    for kw in keywords:
        p = low.find(kw.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        head = text[:width]
        return head + ("…" if len(text) > width else ""), 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    seg = text[start:end]
    for kw in sorted(keywords, key=len, reverse=True):
        if not kw:
            continue
        seg = re.sub(re.escape(kw), f"【{kw}】", seg, flags=re.IGNORECASE)
    return ("…" if start > 0 else "") + seg + ("…" if end < len(text) else ""), pos


def _mem_search_one(name, payload_or_none, raw_text, keywords, mode, search_in, width):
    """判定单条记忆是否命中，返回 (hit, title_hits, content_hits, snippet)。"""
    title = str(name or "")
    content = ""
    if isinstance(payload_or_none, dict):
        content = _mem_extract_text(payload_or_none)
    elif isinstance(raw_text, str):
        content = raw_text
    title_hits = [k for k in keywords if k and k.lower() in title.lower()]
    content_hits = [k for k in keywords if k and k.lower() in content.lower()]
    if search_in == "title":
        hit = len(title_hits) == len(keywords) if mode == "all" else len(title_hits) > 0
    elif search_in == "content":
        hit = len(content_hits) == len(keywords) if mode == "all" else len(content_hits) > 0
    else:
        # both：关键词可以落在标题或内容上，合并判定
        merged = set(k.lower() for k in title_hits) | set(k.lower() for k in content_hits)
        hit = len(merged) == len(keywords) if mode == "all" else len(merged) > 0
    snippet, _ = _mem_snippet(content, keywords, width) if content else ("", 0)
    return hit, title_hits, content_hits, snippet


def search_memories(keywords, mode="all", search_in="both", target="all",
                    limit=10, context_chars=160, wait_client=None):
    """在记忆库里按关键词搜索标题与内容。返回 (文本结果, returncode)。

    keywords: 关键词列表；mode: all/any；search_in: both/title/content；
    target: all/local/cloud；本地记忆经 wait_client 中转（服务器与客户端可能不同机器）。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return "错误：keywords 不能为空", -1
    if mode not in ("all", "any"):
        mode = "all"
    if search_in not in ("both", "title", "content"):
        search_in = "both"
    if target not in ("all", "local", "cloud"):
        target = "all"
    try:
        limit = max(1, min(50, int(limit or 10)))
    except Exception:
        limit = 10
    try:
        width = max(40, min(1000, int(context_chars or 160)))
    except Exception:
        width = 160

    results = []      # {source, name, mtime, size, title_hits, content_hits, snippet, searchable}
    local_unavailable = False
    local_unsearchable = 0

    # ---- 云端：服务器本地目录，内容可直接搜 ----
    if target in ("all", "cloud"):
        for e in _mem_list_cloud():
            payload = _mem_load_cloud(e["name"])
            hit, th, ch, snip = _mem_search_one(e["name"], payload, None, kws, mode, search_in, width)
            if hit:
                results.append({"source": "cloud", "name": e["name"], "mtime": e.get("mtime", 0),
                                "size": e.get("size", 0), "title_hits": th, "content_hits": ch,
                                "snippet": snip, "content_hits_count": len(ch)})

    # ---- 本地：经客户端中转；优先让客户端直接搜内容 ----
    if target in ("all", "local"):
        wc = wait_client
        if not wc:
            local_unavailable = True
        else:
            # 优先：客户端原生搜索（能搜内容，且不必把整库传回服务器）
            out, ret = wc("#mem_search " + json.dumps({"keywords": kws, "mode": mode,
                                                       "search_in": search_in,
                                                       "limit": limit,
                                                       "context_chars": width},
                                                      ensure_ascii=False),
                          display=f"#search_memory {' '.join(kws)}")
            client_results = None
            if ret == 0:
                try:
                    parsed = json.loads(out)
                    if isinstance(parsed, dict) and parsed.get("supported") is False:
                        client_results = None          # 客户端不支持，走下面的兼容路径
                    elif isinstance(parsed, list):
                        client_results = parsed
                except Exception:
                    client_results = None
            if client_results is not None:
                for r in client_results:
                    if not isinstance(r, dict):
                        continue
                    results.append({"source": "local", "name": r.get("name", "?"),
                                    "mtime": r.get("mtime", 0), "size": r.get("size", 0),
                                    "title_hits": r.get("title_hits") or [],
                                    "content_hits": r.get("content_hits") or [],
                                    "snippet": r.get("snippet") or "",
                                    "content_hits_count": len(r.get("content_hits") or [])})
            else:
                # 兼容旧客户端：只能拿到文件名列表 → 仅能按标题匹配
                lout, lret = wc("#mem_list", display=f"#search_memory {' '.join(kws)}")
                names = []
                if lret == 0:
                    try:
                        arr = json.loads(lout)
                        if isinstance(arr, list):
                            names = [x for x in arr if isinstance(x, dict)]
                    except Exception:
                        names = []
                for e in names:
                    hit, th, ch, snip = _mem_search_one(e.get("name", ""), None, "", kws,
                                                        mode, "title" if search_in == "both" else search_in,
                                                        width)
                    if hit:
                        results.append({"source": "local", "name": e.get("name", "?"),
                                        "mtime": e.get("mtime", 0), "size": e.get("size", 0),
                                        "title_hits": th, "content_hits": [], "snippet": "",
                                        "content_hits_count": 0})
                if search_in in ("both", "content"):
                    local_unsearchable = len(names)

    if not results:
        msg = f"未找到匹配的记忆（关键词：{'、'.join(kws)}；范围：{search_in}；逻辑：{'全部命中' if mode=='all' else '任一命中'}）。"
        if local_unavailable:
            msg += "\n（本地记忆不可用：没有客户端中转通道）"
        if local_unsearchable:
            msg += f"\n（注意：当前客户端版本不支持搜索本地记忆**内容**，已仅按标题匹配了 {local_unsearchable} 个文件；可先用标题定位再 load 查看内容。）"
        return msg, 0

    results.sort(key=lambda x: (len(x.get("content_hits") or []) + len(x.get("title_hits") or []),
                                x.get("mtime", 0)), reverse=True)
    total = len(results)
    results = results[:limit]
    lines = [f"找到 {total} 条匹配记忆（关键词：{'、'.join(kws)}；{'全部命中' if mode=='all' else '任一命中'}；范围：{search_in}）：", ""]
    for i, r in enumerate(results, 1):
        ts = time.strftime('%Y-%m-%d %H:%M', time.localtime(r["mtime"])) if r.get("mtime") else "-"
        lines.append(f"{i}. 【{r['name']}】 来源={r['source']}  修改={ts}  大小={_fmt_size(r.get('size',0))}")
        th, ch = r.get("title_hits") or [], r.get("content_hits") or []
        if th:
            lines.append(f"   标题命中：{'、'.join(th)}")
        if ch:
            lines.append(f"   内容命中：{'、'.join(ch)}")
        if r.get("snippet"):
            lines.append(f"   摘要：{r['snippet']}")
        lines.append("")
    if total > len(results):
        lines.append(f"…（共 {total} 条，仅显示前 {len(results)} 条，可提高 limit 或用更多关键词缩小范围）")
    lines.append("提示：确认目标后，用 load 工具载入其完整上下文（load 支持 local/cloud）。")
    if local_unavailable:
        lines.append("（本地记忆不可用：没有客户端中转通道）")
    if local_unsearchable:
        lines.append(f"（当前客户端不支持搜索本地记忆内容，已仅按标题匹配 {local_unsearchable} 个文件）")
    return "\n".join(lines), 0

# 完整客户端包（含内置 Python/Node.js/MinGW64 等运行时）。
# 自定义下载组件的唯一来源：所有组件统一从 client.zip 中提取，不再读取本地 client/ 目录。
CLIENT_ZIP_PATH = os.path.join(WORK_DIR, 'client.zip')
FILE_MAP_PATH = os.path.join(UPLOAD_FOLDER, 'file_map.json')

def load_file_map():
    if os.path.exists(FILE_MAP_PATH):
        try:
            with open(FILE_MAP_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_file_map(data):
    with open(FILE_MAP_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
TREE_FILE = os.path.join(WORK_DIR, 'chat_tree.json')

# ---------- 树存储管理器 ----------
class TreeManager:
    def __init__(self, fp):
        self.filepath = fp
        self.lock = threading.Lock()
        self.tree = {"root": None, "nodes": {}}
        self.dirty = False
        self._load()
        self._start_flush()
        self._start_watchdog()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f: self.tree = json.load(f)
            except: self.tree = {"root": None, "nodes": {}}

    def _flush(self):
        with self.lock:
            if not self.dirty: return
            tmp = self.filepath + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f: json.dump(self.tree, f, indent=2, ensure_ascii=False)
            if os.name == 'nt':
                if os.path.exists(self.filepath): os.remove(self.filepath)
                os.rename(tmp, self.filepath)
            else: os.replace(tmp, self.filepath)
            self.dirty = False

    def _start_flush(self):
        def loop():
            while True:
                time.sleep(2)
                self._flush()
        threading.Thread(target=loop, daemon=True).start()

    def _start_watchdog(self):
        def loop():
            while True:
                time.sleep(10)
                with self.lock:
                    now = time.time()
                    for nid, node in list(self.tree["nodes"].items()):
                        if node.get("generating") and node.get("assistantMessage") == "" and node.get("reasoning") == "":
                            if now - node.get("createdAt", 0) > 60:
                                node["generating"] = False
                                node["assistantMessage"] = "[生成失败：超时无响应]"
                                self.dirty = True
        threading.Thread(target=loop, daemon=True).start()

    def get(self):
        with self.lock: return json.loads(json.dumps(self.tree))

    def create_node(self, pid, sid, umsg, amsg, title, reasoning="", model=None, reasoningEffort=None, gen=False):
        with self.lock:
            nid = str(int(time.time() * 1000))
            node = {
                "id": nid, "parentId": pid, "title": title or "新对话",
                "userMessage": umsg, "assistantMessage": amsg,
                "reasoning": reasoning, "model": model, "reasoningEffort": reasoningEffort,
                "generating": gen, "children": [], "createdAt": time.time(), "last_update": time.time()
            }
            self.tree["nodes"][nid] = node
            if pid and pid in self.tree["nodes"]:
                p = self.tree["nodes"][pid]
                if sid and sid in p.get("children", []):
                    idx = p["children"].index(sid)
                    p["children"].insert(idx + 1, nid)
                else: p["children"].append(nid)
            else:
                if self.tree["root"] is None: self.tree["root"] = nid
            self.dirty = True
            return nid

    def update_node(self, nid, **kw):
        with self.lock:
            if nid in self.tree["nodes"]:
                node = self.tree["nodes"][nid]
                for k, v in kw.items():
                    if k in ('assistantMessage','reasoning','title','model','usage','reasoningDuration','reasoningEffort','generating','images'):
                        node[k] = v
                # ★ 用量计价以调用时间为准：写入 usage 时同时记录调用时间戳（峰谷定价）
                if kw.get('usage') is not None:
                    node["usageAt"] = time.time()
                node["last_update"] = time.time()
                self.dirty = True
                return True
            return False

    def delete_node(self, nid):
        with self.lock:
            if nid not in self.tree["nodes"]: return False
            def rec(x):
                node = self.tree["nodes"].get(x)
                if node:
                    for c in node.get("children", []): rec(c)
                    pid = node.get("parentId")
                    if pid and pid in self.tree["nodes"] and x in self.tree["nodes"][pid]["children"]:
                        self.tree["nodes"][pid]["children"].remove(x)
                    del self.tree["nodes"][x]
            rec(nid)
            if self.tree["root"] == nid: self.tree["root"] = None
            self.dirty = True
            return True

tree_manager = TreeManager(TREE_FILE)
load_tree = tree_manager.get
create_node_in_tree = lambda *a, **kw: tree_manager.create_node(*a, **kw)
update_node_in_tree = lambda *a, **kw: tree_manager.update_node(*a, **kw)

import base64 as _b64

def _save_inline_images(images):
    """把前端传来的图片（data_url）落盘到 uploads（供文件查看器/历史回放），
    返回 [{name, file, mime}] 元数据列表。非法/超大的图片跳过。"""
    saved = []
    for img in images or []:
        try:
            data_url = img.get('data_url') if isinstance(img, dict) else None
            if not isinstance(data_url, str) or not data_url.startswith('data:image/'):
                continue
            mime = data_url[5:data_url.index(';')]
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(mime)
            if not ext:
                continue
            raw = _b64.b64decode(data_url.split(',', 1)[1])
            if len(raw) > 32 * 1024 * 1024:
                logging.warning(f"[IMG] 图片 {(img or {}).get('name')} 超过 32MB，已跳过")
                continue
            sname = f"img_{int(time.time())}_{os.urandom(4).hex()}.{ext}"
            with open(os.path.join(UPLOAD_FOLDER, sname), 'wb') as f:
                f.write(raw)
            saved.append({"name": str((img or {}).get('name') or f"image.{ext}"), "file": sname, "mime": mime})
        except Exception as e:
            logging.warning(f"[IMG] 保存图片失败: {e}")
    return saved

def _image_to_data_url(sname):
    """把 uploads 目录中的图片文件转成 base64 data URL（供 LLM 图像理解）。失败返回 None。"""
    path = os.path.join(UPLOAD_FOLDER, sname)
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if size > 32 * 1024 * 1024:   # 官方限制：单张图片最大 32 MiB
            return None
        ext = sname.rsplit('.', 1)[1].lower() if '.' in sname else ''
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        mime = mime_map.get(ext, "image/png")
        import base64 as _b64
        with open(path, 'rb') as f:
            return f"data:{mime};base64,{_b64.b64encode(f.read()).decode('ascii')}"
    except Exception:
        return None

def _build_user_content_with_images(text, images):
    """构造带图片的 user 消息 content：无图片返回纯字符串，有图片返回内容块数组。"""
    if not images:
        return text
    blocks = [{"type": "text", "text": text or ""}]
    for img in images:
        if not isinstance(img, dict):
            continue
        data_url = img.get("data_url")
        if not data_url:
            # 历史节点：images 存的是 {name, file, mime}，按文件读回 data URL
            sname = img.get("file")
            if sname:
                data_url = _image_to_data_url(sname)
        if data_url:
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return blocks if len(blocks) > 1 else text

def _file_ctx(files):
    """把用户上传的文档内容拼成注入上下文的文本（与对话模式一致）。"""
    if not files:
        return ""
    return "".join(f"用户上传了文件 `{f['name']}`，内容如下：\n```\n{f['content']}\n```\n\n"
                   for f in files if isinstance(f, dict) and f.get('name'))

def build_path_messages(node_id, tree):
    path, cur = [], node_id
    while cur:
        node = tree["nodes"].get(cur)
        if not node: break
        path.insert(0, node)
        cur = node.get("parentId")
    msgs = []
    for n in path:
        msgs.append({"role":"user","content":_build_user_content_with_images(n.get("userMessage") or "", n.get("images"))})
        msgs.append({"role":"assistant","content":n["assistantMessage"]})
    return msgs

# ---------- 文件解析 ----------
ALLOWED = {'pdf','doc','docx','xls','xlsx','ppt','pptx','txt','py','js','java','cpp','c','h','html','css','json','xml','yaml','yml','md','rb','go','rs','ts','swift','kt','sh','bat','jpg','jpeg','png','gif','webp'}
IMAGE_EXTS = {'jpg','jpeg','png','gif','webp'}
def allowed_file(f): return '.' in f and f.rsplit('.',1)[1].lower() in ALLOWED

def parse_pdf(path):
    if not PyPDF2: return "PyPDF2 未安装"
    try: return "\n".join(p.extract_text() or '' for p in PyPDF2.PdfReader(path).pages)
    except Exception as e: return str(e)
def parse_docx(path):
    if not Document: return "python-docx 未安装"
    if not os.path.exists(path): return "docx 文件不存在"
    if os.path.getsize(path) == 0: return "docx 文件内容为空（0 字节）：上传的源文件本身是空文件，常见于从未打开保存过的“新建 xxx”空白模板、文件被其他程序锁定或下载不完整，请确认源文件有内容后重新上传"
    try:
        return "\n".join(p.text for p in Document(path).paragraphs)
    except Exception:
        # 兼容 fallback：直接解包 document.xml 提取文本（WPS 等生成的文档）
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read('word/document.xml').decode('utf-8', errors='replace')
            xml = re.sub(r'<w:p[ >]', '\n', xml)
            xml = re.sub(r'<[^>]+>', '', xml)
            text = html.unescape(xml)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            if text: return text
            return f"docx 解析失败：不是有效的 Word 文档（{os.path.getsize(path)} 字节）"
        except Exception as e2:
            return f"docx 解析失败：{e2}"
def parse_xlsx(path):
    if not openpyxl: return "openpyxl 未安装"
    if not os.path.exists(path): return "xlsx 文件不存在"
    if os.path.getsize(path) == 0: return "xlsx 文件内容为空（0 字节）：上传的源文件本身是空文件，常见于从未打开保存过的“新建 xxx”空白模板、文件被其他程序锁定或下载不完整，请确认源文件有内容后重新上传"
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        return f"xlsx 解析失败：{e}"
    parts = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append("\t".join("" if c is None else str(c) for c in row))
        parts.append(f"--- {ws.title} ---\n" + "\n".join(rows))
    return "\n".join(parts)
def parse_pptx(path):
    if not Presentation: return "python-pptx 未安装"
    if not os.path.exists(path): return "pptx 文件不存在"
    if os.path.getsize(path) == 0: return "pptx 文件内容为空（0 字节）"
    try:
        prs = Presentation(path)
        return "\n".join(f"--- Slide {i+1} ---\n" + "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame for p in shape.text_frame.paragraphs) for i,slide in enumerate(prs.slides))
    except Exception as e:
        return f"pptx 解析失败：{e}"
def _safe_str(s):
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
        try: return _safe_str(data[3:].decode('utf-8'))
        except UnicodeDecodeError: pass
    # 2) UTF-16 BOM（防御 cmd type 未做代码页转换而输出的原始 UTF-16 字节）
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try: return _safe_str(data.decode('utf-16'))
        except UnicodeDecodeError: pass
    # 3) 纯 ASCII 快速路径（无需猜测编码）
    if data.isascii():
        return _safe_str(data.decode('ascii'))
    # 4) UTF-8 严格解码 + 合理性校验（绝大多数正常输出在此返回，省去 chardet 开销）
    need_arbitrate = False
    try:
        s = _safe_str(data.decode('utf-8'))
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
        try: return _safe_str(data.decode(det_enc))
        except (UnicodeDecodeError, LookupError): pass
    # 6) 多候选解码 + 质量评分择优（GBK 字节流偶尔能"骗过" UTF-8 解码，需对比取优）
    candidates = []
    for enc in ('utf-8', 'gb18030', 'gbk'):
        try:
            s = _safe_str(data.decode(enc))
            candidates.append((_text_score(s), enc, s))
        except (UnicodeDecodeError, LookupError):
            pass
    if not candidates:
        # 7) 最终兜底，保证不抛异常
        return _safe_str(data.decode('latin-1', errors='replace'))
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

def parse_txt(path):
    with open(path, 'rb') as f:
        raw = f.read()
    return decode_bytes(raw)

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'file' not in request.files: return jsonify({"error":"无文件"}),400
    file = request.files['file']
    if file.filename == '': return jsonify({"error":"空文件名"}),400
    if not allowed_file(file.filename): return jsonify({"error":"不支持的类型"}),400
    dname = file.filename
    ext = dname.rsplit('.',1)[1].lower() if '.' in dname else ''
    sname = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    path = os.path.join(UPLOAD_FOLDER, sname)
    file.save(path)
    if os.path.getsize(path) == 0:
        try: os.remove(path)
        except Exception: pass
        return jsonify({"error": "文件内容为空（0 字节）：上传的源文件本身是空文件，常见于从未打开保存过的\u201c新建 xxx\u201d空白模板、文件被其他程序锁定或下载不完整，请确认源文件有内容后重新上传"}), 400
    fmap = load_file_map()
    fmap[dname] = sname
    save_file_map(fmap)
    # ★ 图片文件：不做文本解析，返回 type=image（前端文件查看器直接展示图片）
    if ext in IMAGE_EXTS:
        return jsonify({"displayName":dname,"content":"","length":os.path.getsize(path),"type":"image"})
    try:
        if ext=='pdf': c=parse_pdf(path)
        elif ext in ('doc','docx'): c=parse_docx(path)
        elif ext in ('xls','xlsx'): c=parse_xlsx(path)
        elif ext in ('ppt','pptx'): c=parse_pptx(path)
        else: c=parse_txt(path)
    except Exception as e: c=f"解析错误: {e}"
    return jsonify({"displayName":dname,"content":c,"length":os.path.getsize(path)})

@app.route('/api/file/<name>')
def file_content(name):
    fmap = load_file_map()
    sname = fmap.get(name)
    if not sname: return jsonify({"error":"未找到"}),404
    path = os.path.join(UPLOAD_FOLDER, sname)
    if not os.path.exists(path): return jsonify({"error":"文件缺失"}),404
    ext = sname.rsplit('.',1)[1].lower()
    # ★ 图片文件：返回 type=image + 原始字节地址（前端文件查看器直接展示）
    if ext in IMAGE_EXTS:
        return jsonify({"displayName":name,"content":"","length":os.path.getsize(path),"type":"image","url":f"/api/uploads/{sname}"})
    try:
        if ext=='pdf': c=parse_pdf(path)
        elif ext in ('doc','docx'): c=parse_docx(path)
        elif ext in ('xls','xlsx'): c=parse_xlsx(path)
        elif ext in ('ppt','pptx'): c=parse_pptx(path)
        else: c=parse_txt(path)
    except Exception as e:
        c = f"解析失败：{e}"
    return jsonify({"displayName":name,"content":c,"length":os.path.getsize(path)})

# ★ 2026-10-09（修改一）：把前端即将发送的图片落盘，并回传可导出的元信息。
#
#   为什么要这个独立端点：操作模式的导出记录是由前端事件流拼出来的，而 data_url
#   （base64）绝不能写进导出 JSON（一张图几百 KB，几十张就是几十 MB）。图片本来
#   就要落到 uploads/ 供查看器打开，所以这里落盘之后把 {name,file,mime} 还给前端，
#   前端在收到服务端的 user_message 事件时把它附到对应事件上 —— 导出即可用它引用，
#   导入回放时用 /api/image/<file> 重新加载。
#
#   返回值里**不含 data_url**，避免调用方顺手又把它塞进记录。
@app.route('/api/save_images', methods=['POST'])
def save_images():
    data = request.get_json(silent=True) or {}
    saved = _save_inline_images(data.get('images') or [])
    if not saved:
        return jsonify({"images": [], "saved": 0})
    logging.info(f"[IMG] 落盘 {len(saved)} 张图片，供导出记录引用")
    return jsonify({"images": saved, "saved": len(saved)})

# ★ 图片原始字节（文件查看器/缩略图共用）：uploads 中的图片（上传或对话图片）
@app.route('/api/uploads/<name>')
def uploads_raw(name):
    if not re.match(r'^(?:img_)?[0-9]+_[0-9a-f]+\.(jpg|jpeg|png|gif|webp)$', name, re.I):
        return jsonify({"error":"非法文件名"}),400
    path = os.path.join(UPLOAD_FOLDER, name)
    if not os.path.isfile(path):
        return jsonify({"error":"图片不存在"}),404
    ext = name.rsplit('.',1)[1].lower()
    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif","webp":"image/webp"}.get(ext, "image/png")
    return _img_response(path, mime)

# ★ 图像理解：按存储名返回图片原始字节（对话历史缩略图 / 图片查看）
@app.route('/api/image/<name>')
def image_raw(name):
    if not re.match(r'^img_[0-9]+_[0-9a-f]+\.(jpg|jpeg|png|gif|webp)$', name, re.I):
        return jsonify({"error":"非法文件名"}),400
    path = os.path.join(UPLOAD_FOLDER, name)
    if not os.path.isfile(path):
        return jsonify({"error":"图片不存在"}),404
    ext = name.rsplit('.',1)[1].lower()
    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif","webp":"image/webp"}.get(ext, "image/png")
    return _img_response(path, mime)

# ★ 服务器设置（模型路由 + 价格单价）：读取 / 保存，持久化到 server/settings.json
# ★ 打包时写入的占位 key（"肯德基疯狂星期四"梗）—— 它等于"用户还没配自己的 key"。
#   见 tool/pack/make_server_zip.ps1。前端据此弹「API 设置向导」。
KFC_PLACEHOLDER_KEY = "sk-TodayisKFCcrazyThursdayVme50"


def _deepseek_key_is_placeholder(providers=None):
    """★ 2026-10-14（按用户要求简化的判据）：**只看 DeepSeek 提供商的 key**。

       用户原话："哦好像不用区分 flash，就看 DeepSeek 就行了"

       命中"DeepSeek 提供商"的规则：name 含 deepseek，或 base_url 含 deepseek。
       返回 True（key 为空或仍是打包占位 → 需要弹配置向导）/
            False（已配好）/
            None（找不到 DeepSeek 提供商 → 用户自己加的其它提供商，不打扰）。
    """
    try:
        provs = providers if providers is not None else get_providers()
    except Exception:
        return None
    if not isinstance(provs, list):
        return None
    for pr in provs:
        nm = str(pr.get("name") or "").lower()
        bu = str(pr.get("base_url") or "").lower()
        if ("deepseek" in nm) or ("deepseek" in bu):
            k = str(pr.get("api_key") or "").strip()
            return (not k) or (k == KFC_PLACEHOLDER_KEY)
    return None


# ★ 2026-10-14（按用户要求"把 thinking 落盘，我排查一下"）：
#   服务端把**发出的每个 thinking 块**原样记一份，供与前端渲染结果对照，
#   定位"生成已结束后前端仍显示不完整"到底丢在哪一环。
THINKING_DUMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'thinking_dump')
_thinking_dump_lock = threading.Lock()
_thinking_dump_state = {}     # {session_id: (path, block_no, chars)}


def thinking_dump_write(session_id, kind, text, extra=None):
    """把一条 thinking 相关记录追加到 logs/thinking_dump/<session>.jsonl。

    kind: 'chunk'（本次新增的思考片段）/ 'done'（该轮思考结束）/ 'final'（完整思考）
    """
    try:
        os.makedirs(THINKING_DUMP_DIR, exist_ok=True)
        sid = str(session_id or 'nosession')
        with _thinking_dump_lock:
            path, blk, chars = _thinking_dump_state.get(sid, (None, 0, 0))
            if path is None:
                path = os.path.join(THINKING_DUMP_DIR, f'{sid[:8]}_{int(time.time())}.jsonl')
                blk, chars = 0, 0
            if kind == 'chunk':
                chars += len(text or '')
            elif kind == 'done':
                blk += 1
            rec = {
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'kind': kind,
                'block': blk,
                'chunk_len': len(text or ''),
                'total_chars': chars,
            }
            if extra:
                rec.update(extra)
            if text is not None:
                rec['text'] = text
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            _thinking_dump_state[sid] = (path, blk, chars)
            return path
    except Exception as e:
        try:
            logging.warning(f'[THINK_DUMP] 写入失败: {e}')
        except Exception:
            pass
        return None


def _mask_settings_for_client(st):
    """★ 2026-10-14：把设置里所有 api_key 换成脱敏值 —— **GET 与 POST 共用**。

    事故背景：POST /api/server_settings 原来直接把 save_server_settings() 的返回值
    原样回给前端，而 save 内部为了"不覆盖真 key"会把真 key 填回 merged ——
    于是**响应体里带着明文 key**，前端拿它更新 serverModelConfig，
    页面上的「API 密钥」框就显示出了明文。GET 有脱敏、POST 没有，两条路径不一致。
    现在统一走这里，杜绝再次漏掉。真实 key 永不返回给前端。
    """
    out = dict(st or {})
    real_flat = (out.get('api_key') or '').strip()
    out['api_key'] = mask_api_key(real_flat)
    out['has_api_key'] = bool(real_flat)
    # ★ 2026-10-14（修「把 key 改回 KFC 梗占位、刷新后不弹填密钥的框」）：
    #   这个判断必须由**服务端**做 —— 它能拿到真 key；前端只有脱敏串，
    #   原来靠"末 5 位是不是 Vme50"来猜，而脱敏只保留末 4 位（************me50），
    #   末 5 位是 '*me50'，**永远猜不中**，检测形同虚设。
    #
    #   ★★ 2026-10-14 二次修正（用户："为什么又不弹叫我填 key 的弹窗了"）：
    #   实际请求用的是**模型所属提供商**的 key（见 get_api_config / resolve_model），
    #   而不是扁平键。只判断扁平键会出现：
    #       扁平键 = 可用、DeepSeek provider = 梗占位  → 判为"不需要配置"，但实际用的是梗占位。
    #   所以这里要**两边都看**：
    #     · 扁平键为空或梗占位 → 需要配置；
    #     · 或任一提供商的 key 为空或梗占位 → 需要配置。
    # ★ 2026-10-14（按用户要求简化）：**只看 DeepSeek 的 key**。
    #   找不到 DeepSeek 提供商 → 不弹（None → False）。
    _ds_bad = _deepseek_key_is_placeholder(out.get('providers'))
    out['needs_api_setup'] = bool(_ds_bad) if _ds_bad is not None else False
    try:
        _provs = out.get('providers')
        if isinstance(_provs, list):
            safe = []
            for pr in _provs:
                sp = dict(pr or {})
                rk = (sp.get('api_key') or '').strip()
                sp['api_key'] = mask_api_key(rk)
                sp['has_api_key'] = bool(rk)
                safe.append(sp)
            out['providers'] = safe
    except Exception:
        pass
    return out


@app.route('/api/server_settings', methods=['GET'])
def get_server_settings():
    st = load_server_settings()
    # ★ 2026-10-14：统一走 _mask_settings_for_client —— 真实 key 绝不返回前端
    safe = _mask_settings_for_client(st)
    return jsonify({"success": True, "settings": safe, "defaults": DEFAULT_SERVER_SETTINGS,
                    "prompt_defaults": get_prompt_defaults()})

# ★ 2026-10-13（按用户要求）：显示 API 余额
#   文档：https://api-docs.deepseek.com/zh-cn/api/get-user-balance
#   GET {base_url}/user/balance  →  { is_available, balance_infos: [...] }
#   ★ key 只在服务端使用，绝不返回给前端；前端只拿余额数字。
# ★ 2026-10-14（按用户要求）：余额**按提供商**查询 —— 缓存也按 base_url 分开，
#   不同提供商的余额互不干扰。key 仍然只在服务端使用。
_BALANCE_CACHE = {}          # {base_url: {"at": ts, "data": {...}}}
_BALANCE_TTL = 60


@app.route('/api/api_balance', methods=['GET'])
def api_balance():
    """查询账户余额。

    ★ 2026-10-14：支持 ?provider=<序号> —— 用**该提供商自己的** base_url / api_key 去查，
      这样"模型提供商"里每张卡片都能显示自己的余额（用户要求"按照 base_url 来查"）。
      不传 provider 时保持原行为（用全局配置），兼容旧调用。
    """
    force = (request.args.get('force') or '') in ('1', 'true', 'yes')
    # —— 解析要查哪家 ——
    _pv_idx = request.args.get('provider')
    _pv_name = ''
    try:
        if _pv_idx is not None and str(_pv_idx) != '':
            _provs = get_providers()
            _i = int(_pv_idx)
            if 0 <= _i < len(_provs):
                _pr = _provs[_i]
                key = (_pr.get('api_key') or '').strip()
                base = (_pr.get('base_url') or '').strip() or DEFAULT_BASE_URL
                _pv_name = _pr.get('name') or ''
            else:
                return jsonify({"success": False, "error": f"提供商序号越界: {_pv_idx}"})
        else:
            key, base = get_api_config()
    except Exception as e:
        return jsonify({"success": False, "error": f"读取配置失败: {e}"})
    base = (base or DEFAULT_BASE_URL).rstrip('/')
    now = time.time()
    _c = _BALANCE_CACHE.get(base)
    if (not force) and _c and _c.get("data") is not None and (now - _c["at"] < _BALANCE_TTL):
        return jsonify(_c["data"])
    if not key:
        return jsonify({"success": False, "provider": _pv_name,
                        "error": "该提供商尚未配置 API Key"})
    url = base + '/user/balance'
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                         timeout=12)
        if r.status_code == 404:
            # 多数第三方 OpenAI 兼容端点没有这个接口 —— 明确说明，不当成错误
            out = {"success": False, "unsupported": True,
                   "error": "当前 Base URL 不支持余额查询（该接口是 DeepSeek 专有）"}
        elif r.status_code in (401, 403):
            out = {"success": False, "error": f"API Key 无效或无权限（HTTP {r.status_code}）"}
        elif r.status_code != 200:
            out = {"success": False, "error": f"查询失败（HTTP {r.status_code}）：{r.text[:160]}"}
        else:
            d = r.json() if r.content else {}
            infos = d.get("balance_infos") or []
            out = {
                "success": True,
                "is_available": bool(d.get("is_available")),
                "currency": (infos[0].get("currency") if infos else "") or "",
                "total_balance": (infos[0].get("total_balance") if infos else "") or "",
                "granted_balance": (infos[0].get("granted_balance") if infos else "") or "",
                "topped_up_balance": (infos[0].get("topped_up_balance") if infos else "") or "",
                "balance_infos": infos,
            }
    except requests.exceptions.Timeout:
        out = {"success": False, "error": "查询超时（网络或上游无响应）"}
    except Exception as e:
        out = {"success": False, "error": f"查询异常: {type(e).__name__}: {e}"}
    # 把查询对象也带回给前端（便于卡片显示"查的是谁"），但不含 key
    out["provider"] = _pv_name
    out["base_url"] = base
    if out.get("success"):
        _BALANCE_CACHE[base] = {"at": now, "data": out}
    return jsonify(out)


@app.route('/api/server_settings', methods=['POST'])
def post_server_settings():
    data = request.get_json(silent=True) or {}
    new_settings = data.get('settings')
    if not isinstance(new_settings, dict):
        return jsonify({"success": False, "error": "缺少 settings 字段"}), 400
    # ★ 2026-10-05：api_key 的"脱敏值回传"处理。
    #   前端拿到的是脱敏值（如 ************abcd），若用户没改就直接保存，
    #   会把真实 key 覆盖成脱敏串 —— 所以这里识别出来并保留原值。
    if 'api_key' in new_settings:
        _incoming = (new_settings.get('api_key') or '').strip()
        _stored = (load_server_settings().get('api_key') or '').strip()
        if (not _incoming) or _incoming == mask_api_key(_stored) or set(_incoming) == {'*'}:
            new_settings['api_key'] = _stored      # 保持原 key 不变
    errors = []
    for key in ("flash_model", "pro_model"):
        v = new_settings.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            errors.append(f"{key} 必须为非空字符串")
    # ★ 2026-10-05：base_url 校验（必须是 http/https 地址，留空则用默认）
    _bu = new_settings.get("base_url")
    if _bu is not None:
        if not isinstance(_bu, str):
            errors.append("base_url 必须为字符串")
        elif _bu.strip() and not re.match(r'^https?://[^\s]+$', _bu.strip()):
            errors.append("base_url 必须以 http:// 或 https:// 开头")
    prices = new_settings.get("prices")
    if prices is not None:
        if not isinstance(prices, dict):
            errors.append("prices 必须为对象")
        else:
            for alias in ("flash", "pro"):
                p = prices.get(alias)
                if p is None:
                    continue
                if not isinstance(p, dict):
                    errors.append(f"prices.{alias} 必须为对象")
                    continue
                for field in ("cached", "uncached", "output"):
                    v = p.get(field)
                    if v is None:
                        continue
                    if not (isinstance(v, (list, tuple)) and len(v) == 2 and
                            all(isinstance(x, (int, float)) and x >= 0 for x in v)):
                        errors.append(f"prices.{alias}.{field} 必须为 [空闲价, 高峰价] 两个非负数")
    prompts = new_settings.get("prompts")
    if prompts is not None:
        if not isinstance(prompts, dict):
            errors.append("prompts 必须为对象")
        else:
            b = prompts.get("base")
            if b is not None and not isinstance(b, str):
                errors.append("prompts.base 必须为字符串")
            sec = prompts.get("sections")
            if sec is not None and (not isinstance(sec, dict) or any(
                    (not isinstance(k, str)) or (not isinstance(v, str)) for k, v in sec.items())):
                errors.append("prompts.sections 必须为“字符串→字符串”对象")
    if errors:
        return jsonify({"success": False, "error": "；".join(errors)}), 400
    try:
        merged = save_server_settings(new_settings)
    except Exception as e:
        return jsonify({"success": False, "error": f"保存失败: {e}"}), 500
    # ★ 2026-10-05：POST 的响应同样必须脱敏 —— 否则保存一次就把真实 key 明文回给了浏览器。
    safe = dict(merged)
    _rk = (merged.get('api_key') or '').strip()
    safe['api_key'] = mask_api_key(_rk)
    safe['has_api_key'] = bool(_rk)
    return jsonify({"success": True, "settings": _mask_settings_for_client(safe)})

@app.route('/api/tree')
def get_tree():
    tree = load_tree()
    # 深拷贝一份，去掉节点中的大字段
    slim_nodes = {}
    for nid, node in tree["nodes"].items():
        slim = {
            "id": node["id"],
            "parentId": node["parentId"],
            "title": node["title"],
            "generating": node.get("generating", False),
            "children": node.get("children", []),
            "createdAt": node.get("createdAt"),
            "last_update": node.get("last_update")
        }
        slim_nodes[nid] = slim
    slim_tree = {
        "root": tree["root"],
        "nodes": slim_nodes
    }
    return jsonify(slim_tree)

@app.route('/api/node/<node_id>')
def get_node(node_id):
    node = load_tree()["nodes"].get(node_id)
    if not node: return jsonify({"error":"Not found"}),404
    return jsonify(node)

@app.route('/api/add_node', methods=['POST'])
def add_node():
    d = request.json
    nid = create_node_in_tree(d.get('parentId'), d.get('siblingId'), d['userMessage'], d.get('assistantMessage',''),
                              d.get('title','新对话'), d.get('reasoning',''), d.get('model'), d.get('reasoningEffort'),
                              d.get('generating',False))
    return jsonify({"id":nid})

@app.route('/api/update_node', methods=['POST'])
def update_node():
    d = request.json
    nid = d.pop('id',None)
    return jsonify({"success": update_node_in_tree(nid, **d)})

@app.route('/api/delete_node', methods=['POST'])
def delete_node_api():
    return jsonify({"success": tree_manager.delete_node(request.json.get('id'))})

@app.route('/api/save_tree')
def save_tree(): return jsonify(load_tree())

@app.route('/api/autosave', methods=['POST'])
def autosave():
    """自动备份接口：前端按“自动保存时间”周期调用，把当前工作数据（对话上下文）
    以记忆文件格式（deepseek-memory-v1，与 #save 一致）备份到云端记忆库
    （server/workspace/memories），文件名 = 任务标题 + 时间戳。
    这样 #list 可直接看到备份，且 #load 可恢复上下文。"""
    d = request.get_json(silent=True) or {}
    title = d.get('title') or '未命名'
    payload = d.get('payload')
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "缺少 payload"}), 400
    fname = d.get('fname') or (_mem_filename(f"{title}_autosave"))
    fname = os.path.basename(fname)
    if not fname.lower().endswith('.json'):
        fname += '.json'
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        content = _mem_dump(
            payload.get('messages') or [],
            payload.get('model'), payload.get('reasoning_effort'),
            payload.get('thinking_enabled'), payload.get('usage'), payload.get('plan'))
        path = os.path.join(MEMORY_DIR, fname)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"success": True, "path": path, "fname": fname})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/load_tree', methods=['POST'])
def load_tree_route():
    new_tree = request.json.get('tree')
    if not new_tree or "nodes" not in new_tree: return jsonify({"success":False}),400
    old = load_tree()
    for nid, nn in new_tree["nodes"].items():
        if nid in old["nodes"]:
            on = old["nodes"][nid]
            for f in ['assistantMessage','title','reasoning','usage','reasoningDuration','model','reasoningEffort']:
                if f in nn: on[f] = nn[f]
            for c in nn.get("children",[]):
                if c not in on.get("children",[]): on["children"].append(c)
        else: old["nodes"][nid] = nn
    if new_tree.get("root"): old["root"] = new_tree["root"]
    with tree_manager.lock:
        tree_manager.tree = old
        tree_manager.dirty = True
    return jsonify({"success":True,"tree":old})

# ---------- 操作模式会话管理 ----------
sessions = {}# ★ BUGFIX(2026-09-28)：改用 RLock（可重入锁）。
#   原先用普通 Lock，只要有任何一条代码路径在持锁时再次获取（例如持锁调用 emit()，
#   而 emit 内部又要拿这把锁），就会**永久自锁死**：这把锁再也不会释放，
#   之后所有需要它的接口全部卡死（操作模式全挂），而不用这把锁的功能（如对话模式）却正常。
#   这种 bug 极难复现、极难定位（本次就是用户连续踩到后才借助线程栈抓到）。
#   RLock 让“同线程重入”变成安全操作，从根上消除这一类死锁；
#   跨线程的锁顺序问题另有约定：**必须先 sessions_lock 再 agents_lock**，不得反向。
sessions_lock = threading.RLock()


# ================= Agent 调用 Agent（子 Agent 机制） =================
MAX_CHILD_AGENTS = 3
ASK_USER_WAIT_TIMEOUT = 3600   # ask_user 同步询问最长等待秒数

# ==================== 上下文管理（#compress 主动压缩 + 50% 自动警告） ====================
CTX_WINDOW_TOKENS = 1_000_000                                  # DeepSeek V4 上下文窗口（tokens）
CTX_WARN_RATIO = float(os.environ.get("CTX_WARN_RATIO", "0.5"))  # 占用警告阈值（默认 50%，测试可调低）
# ★ FIX(2026-09-11)：发送前硬拦截阈值（估算值）——估算 ≥ 96 万时拦截发送，避免必然失败的请求
CTX_SEND_LIMIT_EST = int(CTX_WINDOW_TOKENS * 0.96)
# 占用判定基于真实 usage（最近一次 LLM 调用的输入 token），不用字符数估算。

# ==================== 服务器设置（模型路由 + 价格估算单价，持久化到 server/workspace/settings.json） ====================
# ★ 价格依据 DeepSeek 官方定价（https://api-docs.deepseek.com/zh-cn/quick_start/pricing/）：
#   单价以“元 / 百万 tokens”计；cached/uncached/output 均为 [空闲时段, 高峰时段] 二元组，
#   高峰时段为北京时间周一至周五 9:00-12:00、14:00-18:00，其余（含周末全天）为空闲时段。
SERVER_SETTINGS_FILE = os.path.join(WORK_DIR, 'settings.json')
_LEGACY_SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')

def _migrate_settings_file():
    """把旧位置的 settings.json（server/settings.json）迁移到 workspace 内（server/workspace/settings.json）。"""
    if os.path.exists(SERVER_SETTINGS_FILE) or not os.path.isfile(_LEGACY_SETTINGS_FILE):
        return
    try:
        os.makedirs(os.path.dirname(SERVER_SETTINGS_FILE), exist_ok=True)
        os.replace(_LEGACY_SETTINGS_FILE, SERVER_SETTINGS_FILE)
        logging.info(f"[SETTINGS] 已把旧设置文件迁移到 workspace：{SERVER_SETTINGS_FILE}")
    except Exception as e:
        logging.warning(f"[SETTINGS] 迁移设置文件失败：{e}")

_migrate_settings_file()
DEFAULT_SERVER_SETTINGS = {
    "flash_model": "deepseek-flash",     # ⚡ Flash 路由的模型名（官方新模型名；旧名 deepseek-v4-flash 仍可用）
    "pro_model": "deepseek-v4-pro",      # 🧬 Pro 路由的模型名
    # ★ 2026-10-05（按用户要求）：API 接入信息统一到 settings.json 管理，
    #   撤销原来的 api-key.env 文件。前端「模型与价格」里可修改。
    #   · base_url：OpenAI 兼容接口地址（换中转/自建网关时改这里）
    #   · api_key ：密钥。**GET 一律返回脱敏值**（只露末 4 位）；
    #               POST 时若收到的是脱敏值或空串，则保留已存的真实 key 不变。
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "prices": {                          # 每百万 tokens 单价（元）：[空闲时段, 高峰时段]
        "flash": {"cached": [0.02, 0.04], "uncached": [1.0, 2.0], "output": [4.0, 8.0]},
        "pro": {"cached": [0.15, 0.30], "uncached": [4.5, 9.0], "output": [13.5, 27.0]},
    },
    # ★ 2026-10-19（按用户要求）：联网工具（web_search / web_fetch）单价。
    #   官方按"每 1000 次成功请求"计价，默认 5 元（= 0.005 元/次）；
    #   失败与零结果不计费。可在「设置 → 模型与价格 → 联网工具计费」修改。
    "web_tool_price_per_1k": 5.0,
    # ★ 2026-09-21（按用户要求）：对话模式与操作模式是**两套不同的系统提示词**，
    #   分别用 chat_base / agent_base 覆盖；sections 是操作模式各工具段落；
    #   child_base 是子 Agent 的基础提示词。全部留空 = 使用各自内置默认。
    #   （旧版只有一个 base，会同时改掉两种模式，已废弃但仍做迁移兼容，见 _migrate_prompt_keys。）
    "prompts": {"chat_base": "", "agent_base": "", "child_base": "", "sections": {}},
}


def _migrate_prompt_keys(settings):
    """兼容旧设置：把已废弃的 prompts.base 迁到 agent_base（旧版它是操作模式提示词）。

    旧版只有一个 prompts.base，却被对话模式与操作模式共用（用户反馈的问题）。
    这里把历史值迁到操作模式（agent_base），对话模式回归内置默认，
    避免升级后对话模式被操作模式的 operator 提示词污染。
    """
    try:
        pr = settings.get("prompts")
        if not isinstance(pr, dict):
            return settings
        legacy = pr.get("base")
        if isinstance(legacy, str) and legacy.strip():
            if not str(pr.get("agent_base") or "").strip():
                pr["agent_base"] = legacy
            logging.info("[SETTINGS] 已把旧 prompts.base 迁移为 prompts.agent_base（操作模式）")
        pr.pop("base", None)
    except Exception as e:
        logging.warning(f"[SETTINGS] 提示词键迁移失败：{e}")
    return settings

def _merge_settings(base, override):
    """深度合并设置：override 递归覆盖 base（支持部分更新，如只改 flash 缓存命中价）。"""
    out = json.loads(json.dumps(base, ensure_ascii=False))
    if isinstance(override, dict):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _merge_settings(out[k], v)
            else:
                out[k] = v
    return out

def _split_context_breakdown(internal, tools):
    """按“组成部分”统计当前上下文（估算）：系统提示词 / 工具定义 / 用户 / LLM / 工具。

    返回 {system, tools, user, llm, tool, total}（约 tokens）。
    口径说明：
      - system：internal[0] 的 system 消息（可被对话/Chat 模式的提示词覆盖替换）
      - tools ：本轮提供给模型的工具 schema（JSON 文本）
      - user  ：role=user 的消息（含插话、通知、图片消息的文本部分）
      - llm   ：role=assistant 的正文 + reasoning_content + tool_calls 参数
      - tool  ：role=tool 的工具返回结果
    """
    out = {"system": 0, "tools": 0, "user": 0, "llm": 0, "tool": 0, "total": 0}
    try:
        for m in (internal or []):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            size = 0
            c = m.get("content")
            if isinstance(c, str):
                size += _estimate_tokens_text(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        size += _estimate_tokens_text(part["text"])
                    if isinstance(part, dict) and part.get("image_url"):
                        size += 1100
            rc = m.get("reasoning_content")
            if isinstance(rc, str):
                size += _estimate_tokens_text(rc)
            tc = m.get("tool_calls")
            if tc:
                size += _estimate_tokens_text(json.dumps(tc, ensure_ascii=False))
            size += 4   # 每条消息的固定开销
            if role == "system":
                out["system"] += size
            elif role == "user":
                out["user"] += size
            elif role == "assistant":
                out["llm"] += size
            elif role == "tool":
                out["tool"] += size
        if tools:
            out["tools"] = _estimate_tokens_text(json.dumps(tools, ensure_ascii=False))
        out["total"] = out["system"] + out["tools"] + out["user"] + out["llm"] + out["tool"]
    except Exception:
        pass
    return out


# ==================== 会话运行统计（会话属性面板用） ====================
# 每个会话维护：按模型分开的用量、时间统计（忙碌/等提问/首 token/输出速度）、会话标题。
# ★ 2026-09-21（用户长期反馈的 bug）：以前用量是“一个大锅”，计费时统一套**当前模型**的
#   单价，中途把 Pro 切成 Flash 后，之前 Pro 的 token 会被按 Flash 价重算 → 金额凭空变少。
#   现在按模型分别累计 token，前端各自套各自单价后求和。
def _sess_stats_init():
    return {
        "by_model": {},          # {"deepseek-v4-pro": {prompt_tokens, cached_tokens, completion_tokens, calls}, ...}
        "llm_ms": 0.0,           # LLM 调用累计耗时
        "tool_ms": 0.0,          # 工具/命令执行累计耗时
        "ask_wait_ms": 0.0,      # 阻塞等待用户作答累计耗时
        "first_token_ms_sum": 0.0,   # 首 token 延迟累加（用于求平均）
        "first_token_count": 0,
        "output_tokens": 0,      # 输出 token 累计（用于 tok/s）
        "output_ms": 0.0,        # 正文输出窗口累计（首块正文 → 末块）
        "gen_ms": 0.0,           # ★ 整体生成窗口累计（首块 → 末块，含思考），tok/s 的分母
        "title": "",             # 用户自定义标题（空 = 用自动生成的）
        # ★ 2026-09-21：导入时从导出 JSON 恢复的**历史累计用量**（只用于展示总量，不参与
        #   本次会话的计费累加）。面板的“合计/总费用”= hist_usage + by_model（本会话新增）。
        "hist_usage": {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "calls": 0},
        # ★ 2026-10-19：联网工具（web_search / web_fetch）用量。
        #   这两个是**按次计费**（¥5 / 1000 次成功请求），不产生 token，
        #   所以单独一项，与 by_model 并列展示。
        "web_tools": {"search": {"calls": 0, "ms": 0}, "fetch": {"calls": 0, "ms": 0}},
        "created_at": time.time(),
    }


# 会被“导出 / 导入”持久化的统计字段（数值型）
_STATS_NUM_KEYS = ("llm_ms", "tool_ms", "ask_wait_ms", "first_token_ms_sum",
                   "first_token_count", "output_tokens", "output_ms", "gen_ms")
_STATS_USAGE_KEYS = ("prompt_tokens", "cached_tokens", "completion_tokens", "calls")


def _sess_stats_snapshot(sid):
    """导出用：把会话运行统计整理成可 JSON 序列化的快照（深拷贝，不共享引用）。"""
    try:
        with sessions_lock:
            st = _sess_stats(sid)
            if not st:
                return None
            snap = {k: (int(st.get(k) or 0) if k == "first_token_count" else float(st.get(k) or 0))
                    for k in _STATS_NUM_KEYS}
            snap["title"] = str(st.get("title") or "")
            by_model = {}
            for mname, u in (st.get("by_model") or {}).items():
                if not isinstance(u, dict):
                    continue
                by_model[str(mname)] = {k: int(u.get(k) or 0) for k in _STATS_USAGE_KEYS}
            snap["by_model"] = by_model
            # ★ 2026-10-19：把 hist_usage 也存进快照 —— 导入时可原样恢复，
            #   不必再从 by_model 求和反推（那会随重复注入而漂移）。
            _hu = st.get("hist_usage") or {}
            snap["hist_usage"] = {k: int((_hu or {}).get(k) or 0) for k in _STATS_USAGE_KEYS}
            # ★ 联网工具用量：随统计一起导出（导入时恢复）
            wt = st.get("web_tools")
            snap["web_tools"] = ({
                "search": {"calls": int((wt.get("search") or {}).get("calls") or 0),
                           "ms": int((wt.get("search") or {}).get("ms") or 0)},
                "fetch": {"calls": int((wt.get("fetch") or {}).get("calls") or 0),
                          "ms": int((wt.get("fetch") or {}).get("ms") or 0)},
            } if isinstance(wt, dict) else
                {"search": {"calls": 0, "ms": 0}, "fetch": {"calls": 0, "ms": 0}})
            return snap
    except Exception:
        return None


def _sess_stats(sid):
    """取（必要时创建）某会话的运行统计 dict。调用方需自行加锁。"""
    sess = sessions.get(sid)
    if not sess:
        return None
    st = sess.get("stats")
    if not isinstance(st, dict):
        st = _sess_stats_init()
        sess["stats"] = st
    return st


def _acc_model_usage(sid, model, prompt_tokens, cached_tokens, completion_tokens):
    """按模型累计 token（供“用量”拆分 Flash / Pro）。"""
    if not model:
        return
    with sessions_lock:
        st = _sess_stats(sid)
        if not st:
            return
        bm = st["by_model"]
        e = bm.get(model) or {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "calls": 0}
        e["prompt_tokens"] += int(prompt_tokens or 0)
        e["cached_tokens"] += int(cached_tokens or 0)
        e["completion_tokens"] += int(completion_tokens or 0)
        e["calls"] += 1
        bm[model] = e


def _acc_time_stat(sid, field, ms):
    """累计某项耗时（毫秒）。"""
    try:
        ms = float(ms)
    except Exception:
        return
    if ms <= 0:
        return
    with sessions_lock:
        st = _sess_stats(sid)
        if st is not None:
            st[field] = float(st.get(field) or 0) + ms


# ★ 2026-10-14：联网工具（web_search / web_fetch）调用统计
#   计费口径：每 1000 次**成功**请求 5 元（失败/无结果不计费）
_web_tool_stats = {"search": {"calls": 0, "chars": 0, "ms": 0}, "fetch": {"calls": 0, "chars": 0, "ms": 0}}
_web_tool_stats_lock = threading.Lock()


def _log_web_tool_usage(sid, kind, extra):
    """记录一次成功的联网工具调用（供会话属性面板与日志查看）。"""
    # ★ 2026-10-19：累加进**会话统计**，供属性面板显示并可随导出持久化
    try:
        with sessions_lock:
            _st = _sess_stats(sid)
            if _st is not None:
                _wt = _st.get("web_tools")
                if not isinstance(_wt, dict):
                    _wt = {"search": {"calls": 0, "ms": 0}, "fetch": {"calls": 0, "ms": 0}}
                    _st["web_tools"] = _wt
                _e = _wt.setdefault(kind, {"calls": 0, "ms": 0})
                _e["calls"] = int(_e.get("calls") or 0) + 1
                _e["ms"] = int(_e.get("ms") or 0) + int(extra.get("elapsed_ms") or 0)
    except Exception:
        pass
    try:
        with _web_tool_stats_lock:
            st = _web_tool_stats.setdefault(kind, {"calls": 0, "chars": 0, "ms": 0})
            st["calls"] += 1
            st["ms"] += int(extra.get("elapsed_ms") or 0)
            st["chars"] += int(extra.get("chars") or 0)
            total = _web_tool_stats["search"]["calls"] + _web_tool_stats["fetch"]["calls"]
        _p1k = web_tool_price_per_1k()
        logging.info(f"[WEBTOOL] {sid} {kind} 成功 "
                     f"(HTTP {extra.get('http_status')}, {extra.get('elapsed_ms')} ms)；"
                     f"累计 {total} 次 ≈ ¥{total * _p1k / 1000:.5f}")
    except Exception:
        pass


def _acc_first_token(sid, first_ms):
    """累计首 token 延迟（毫秒），用于求平均。"""
    try:
        first_ms = float(first_ms)
    except Exception:
        return
    if first_ms <= 0:
        return
    with sessions_lock:
        st = _sess_stats(sid)
        if st is not None:
            st["first_token_ms_sum"] = float(st.get("first_token_ms_sum") or 0) + first_ms
            st["first_token_count"] = int(st.get("first_token_count") or 0) + 1


def _merge_usage_with_history(by_model, hist):
    """面板要显示的用量（不重复计数）。

    ★ 语义厘清（2026-09-21）：
      · stats.by_model = 导出时的完整累计台账（导入时原样恢复，新调用继续往上加）
        → 它本身就是正确总量，面板直接用它，**不能再叠加 hist_usage**，
          否则历史部分会被算两遍（实测 1,000,000 变成 2,000,000）。
      · stats.hist_usage = 导入那一刻的历史快照，只用于给「外部用量显示」提供基数
        （让每个对话框下面的用量行在导入后不突然变小）。
    """
    return {k: dict(v) for k, v in (by_model or {}).items()}


def load_server_settings():
    """读取 server/settings.json（缺失/损坏时回退默认值）。

    ★ 2026-09-21：顺带把已废弃的 prompts.base 迁移为 prompts.agent_base，
      保证升级后对话模式不会被操作模式的提示词污染。"""
    try:
        with open(SERVER_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        merged = _merge_settings(DEFAULT_SERVER_SETTINGS, data)
        merged = _migrate_prompt_keys(merged)
        # ★ 2026-10-14（多提供商）：没有 providers 时，由老键**在内存里**迁移一份，
        #   保证所有读设置的地方都能看到统一结构；不写盘（只读操作不该改文件）。
        #   注意 get_providers() 内部也会兜底迁移，这里补上是为了 settings 整体一致。
        if not (isinstance(merged.get("providers"), list) and merged.get("providers")):
            _lp = _legacy_to_providers(merged)
            if _lp:
                merged["providers"] = _lp
        return merged
    except Exception:
        return json.loads(json.dumps(DEFAULT_SERVER_SETTINGS, ensure_ascii=False))

def save_server_settings(settings):
    """保存设置到 server/settings.json（原子写入：临时文件 + 替换）。"""
    merged = _merge_settings(DEFAULT_SERVER_SETTINGS, settings or {})
    merged = _migrate_prompt_keys(merged)
    # ★ 2026-10-14：扁平键 api_key 的脱敏保护（与 providers 同理）
    try:
        _oldflat = json.loads(open(SERVER_SETTINGS_FILE, 'r', encoding='utf-8').read())
    except Exception:
        _oldflat = {}
    _subk = str(merged.get('api_key') or '').strip()
    _prevk = str(_oldflat.get('api_key') or '').strip()
    if _subk == '__CLEAR__':
        merged['api_key'] = ''
    elif (not _subk) or ('*' in _subk):
        if _prevk and '*' not in _prevk:
            merged['api_key'] = _prevk
    # ★ 2026-10-14（多提供商）：规整 providers，并把 flash/pro 别名与单价
    #   **镜像**回老键 flash_model/pro_model/prices —— 让所有仍按老键读取的
    #   代码路径（get_model_aliases / model_to_alias / 价格表）继续正常工作。
    # ★ 2026-10-14：needs_api_setup / has_api_key 是**给前端看的计算字段**，
    #   不该落盘。而且 _merge_settings 会把缺失的键补成 DEFAULT_SERVER_SETTINGS 的值，
    #   于是它们可能被默认的 False 覆盖 → 保存完仍提示"需要配置 API Key"。
    #   这里先取出来，最后再按真实数据重新计算放回响应。
    _prev_needs = merged.pop("needs_api_setup", None)
    merged.pop("has_api_key", None)
    provs = merged.get("providers")
    if isinstance(provs, list) and provs:
        # ★ 2026-10-14（修「保存把真 API Key 覆盖成脱敏串」）：
        #   前端回填的是脱敏值（************4188），保存时会原样提交回来。
        #   这里必须识别"提交的是脱敏串"并**保留磁盘上的真 key** ——
        #   否则保存一次就把真 key 弄丢（实测发生过：settings.json 里只剩脱敏串，
        #   之后所有请求都 401 Authentication Fails）。
        try:
            _old = json.loads(open(SERVER_SETTINGS_FILE, 'r', encoding='utf-8').read())
        except Exception:
            _old = {}
        _old_provs = _old.get('providers') if isinstance(_old.get('providers'), list) else []
        _clean_in = [_norm_provider(x, i) for i, x in enumerate(provs)]
        for _i, _cp in enumerate(_clean_in):
            _sub = (_cp.get('api_key') or '').strip()
            _prev = ''
            if _i < len(_old_provs):
                _prev = str((_old_provs[_i] or {}).get('api_key') or '').strip()
            if _sub == '__CLEAR__':
                _cp['api_key'] = ''
            elif (not _sub) or ('*' in _sub):
                # 空串或脱敏串 → 一律保留磁盘上的真 key（不覆盖）
                if _prev and '*' not in _prev:
                    _cp['api_key'] = _prev
                    logging.info(f"[SETTINGS] providers[{_i}] 提交的是脱敏/空 key，已保留磁盘上的真 key")
                else:
                    _cp['api_key'] = _prev
        provs = _clean_in
        clean = _clean_in
        merged["providers"] = clean
        _by_alias = {}
        for _pr in clean:
            for _m in _pr.get("models") or []:
                _a = (_m.get("alias") or "").strip()
                if _a and _a not in _by_alias:
                    _by_alias[_a] = _m
                    # ★ 2026-10-14：老键镜像**大小写不敏感** ——
                    #   别名写成 'Flash' 时同样要维护 flash_model / prices.flash，
                    #   否则前端 applyServerModelConfig 与老价格表路径会取不到值。
                    if _a.lower() in ("flash", "pro") and _a.lower() not in _by_alias:
                        _by_alias[_a.lower()] = _m
        # ★ 2026-10-14：别名 ↔ 模型名 必须成对 —— 若某别名存在但没写模型名，
        #   用老键补上（否则会出现"配置里有 pro、下拉/子代理枚举里却没有 pro"）。
        for _al, _key in (("flash", "flash_model"), ("pro", "pro_model")):
            if _al in _by_alias and not _by_alias[_al].get("name"):
                _by_alias[_al]["name"] = (merged.get(_key) or "").strip()
        if "flash" in _by_alias:
            merged["flash_model"] = _by_alias["flash"]["name"] or merged.get("flash_model")
            merged.setdefault("prices", {})["flash"] = _by_alias["flash"]["price"]
        if "pro" in _by_alias:
            merged["pro_model"] = _by_alias["pro"]["name"] or merged.get("pro_model")
            merged.setdefault("prices", {})["pro"] = _by_alias["pro"]["price"]
        # 反向补：老键里有 flash/pro 而 providers 里完全没有该别名 → 不强行造一个
        # （尊重用户"删掉 pro"的意图）；但 get_model_aliases() 会用老键兜底，
        #  所以 spawn_agent 仍可用 pro（见那里的 setdefault）。
        # ★ 2026-10-14（按用户要求：扁平键降级为**兼容镜像**）：
        #   前端不再单独展示顶层 api_key/base_url/flash_model —— 用户视角只有
        #   "每个提供商一把 key"。这里把它们从 **flash 模型所属提供商**（找不到则第一个）
        #   自动同步过来，只为兼容还没改到的老代码路径。
        _src = None
        for _pr in clean:
            for _m in (_pr.get("models") or []):
                _al = str(_m.get("alias") or "").lower()
                _nm = str(_m.get("name") or "").lower()
                if _al == "flash" or "flash" in _nm:
                    _src = _pr
                    break
            if _src:
                break
        if _src is None:
            _src = clean[0]
        if _src.get("base_url"):
            merged["base_url"] = _src["base_url"]
        # 注意：api_key **不在这里镜像** —— 它有自己的脱敏保护（用户没改就保留原值），
        #   否则会把用户刚输入的新 key 覆盖掉（实测过这个 bug）。
        # ★ 2026-10-14（修「设置向导点保存根本没保存」）：
        #   **这里绝对不能把 provider 的 api_key 写回扁平 api_key** ——
        #   providers 里的提交值通常是**脱敏串**，上面的保护会把它换成磁盘上的旧 key，
        #   再镜像回来就把用户刚输入的新 key 覆盖掉了（实测：发新 key、磁盘还是旧 key）。
        #   扁平键有自己的脱敏保护（用户没改就保留原值），不需要别人替它赋值。
        # ★ 2026-10-14（按用户要求"本来就不用 pro，删了就不用了"）：
        #   **清理** providers 里已不存在的别名在老键里的残留 ——
        #   否则前端内存里的旧值会被反复提交回来（实测 pro_model 删了又出现）。
        for _al, _key in (("flash", "flash_model"), ("pro", "pro_model")):
            _has = (_al in _by_alias) or any(k.lower() == _al for k in _by_alias)
            if not _has:
                merged.pop(_key, None)
                if isinstance(merged.get("prices"), dict):
                    merged["prices"].pop(_al, None)
    tmp = SERVER_SETTINGS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SERVER_SETTINGS_FILE)
    # 返回前按**真实 key** 重新计算计算字段（与 GET 的 _mask_settings_for_client 一致）
    _real = (merged.get("api_key") or "").strip()
    merged["has_api_key"] = bool(_real)

    _fb = _deepseek_key_is_placeholder(merged.get("providers"))
    merged["needs_api_setup"] = bool(_fb) if _fb is not None else False
    return merged

def get_model_aliases():
    """返回 {别名: 模型名}（实时读取设置，设置修改后即时生效）。

    ★ 2026-10-14（修「子代理用自定义别名被拒：未知模型 'Qwen'，可选 flash/pro」）：
      原来这里**硬编码**只返回 {"flash":…, "pro":…}，而 spawn_agent 的模型校验
      直接拿它当白名单 —— 于是用户在「模型提供商」里自定义的别名（如 Qwen）用不了。
      现在改为**从 providers 动态生成**：
        · 遍历所有提供商的模型取 alias → 模型名；
        · 再补 flash/pro 老键（providers 里没有时用老键兜底），
          保证所有按老键读取的代码路径（model_to_alias / 价格表 / 下拉）行为不变。
    """
    out = {}
    try:
        for _pr, _m in iter_models():
            al = (_m.get("alias") or "").strip()
            nm = (_m.get("name") or "").strip()
            if al and nm and al not in out:
                out[al] = nm
            # 没写别名时，用模型名自身作为可用的"别名"，子代理也能直接按模型名指定
            elif nm and nm not in out:
                out[nm] = nm
    except Exception:
        pass
    # ★ 2026-10-14（按用户要求："本来就不用 pro，删了就不用了"）：
    #   **以 providers 为准**，不再拿老键 flash_model/pro_model 兜底 ——
    #   否则用户删掉的 pro 会被兜底又塞回来（实测 spawn_agent 枚举里还残留 pro）。
    #   只在"providers 一个模型都没有"的极端情况下，才退回老键，避免完全无模型可用。
    if not out:
        try:
            st = load_server_settings()
            _f = (st.get("flash_model") or "").strip()
            if _f:
                out["flash"] = _f
        except Exception:
            pass
        if not out:
            out["flash"] = DEFAULT_SERVER_SETTINGS["flash_model"]
    return out

def model_to_alias(name):
    """模型名 → 别名；未知模型名返回原值。

    ★ 2026-10-14（修 `错误: 'pro'`）：原来硬编码查 aliases["flash"]/aliases["pro"] ——
      用户删掉 pro 之后 get_model_aliases() 里没有该键 → **KeyError: 'pro'**
      （实测：#spawn_agent 返回成功后紧接着报 "错误: 'pro'"）。
      改为**反向遍历**别名表，与配置里实际有哪些别名无关，永不 KeyError。
    """
    if not name:
        return name
    try:
        for _al, _nm in get_model_aliases().items():
            if _nm == name:
                return _al
    except Exception:
        pass
    return name


def default_model_name():
    """取一个可用的默认模型名（永不抛异常、尽量不为空）。

    顺序：settings.default_model → flash 别名 → 别名表里第一个 → 空串。
    ★ 2026-10-14：取代散落各处的 `get_model_aliases()["flash"]`（会在没有 flash 时 KeyError）。
    """
    try:
        _al = get_model_aliases()
    except Exception:
        _al = {}
    try:
        _dm = (load_server_settings().get("default_model") or "").strip()
        if _dm:
            return _dm
    except Exception:
        pass
    if _al.get("flash"):
        return _al["flash"]
    for _v in _al.values():
        if _v:
            return _v
    return ""

agents = {}
# ★ 同样改用 RLock（理由见 sessions_lock 的说明）。
#   锁顺序约定：**先 sessions_lock，再 agents_lock**；禁止反向，否则跨线程会互等。
agents_lock = threading.RLock()

def _truncate(text, limit=3000):
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，原长度 {len(text)}）"

# ★ 2026-09-29（按用户反馈）：检测模型把「工具调用」写成了纯文本。
#
#   现象：当模型调用了一个**不存在的工具名**（或没走 function calling 格式），
#   DeepSeek 会把调用意图当普通正文吐出来，形如：
#       <||DSML|| calls>
#       <||DSML|| invoke name="execute_command">
#       <||DSML|| parameter name="command" string="true">dir /a</||DSML|| parameter>
#       </||DSML|| invoke>
#       </||DSML|| calls>
#   （真实字符是 U+FF5C 全角竖线；这里用半角写注释以免源码本身被误判）
#   结果是：没有 tool_calls 字段 → 系统把这段乱码当“最终答案”显示并**停止本轮**，
#   用户看到的就是“输出一段奇怪内容然后就停了”。
#
#   修法：识别这段标记（全角/半角竖线都认），取出它想调用的工具名，
#   若该工具不在本次工具表里，就把“工具不存在，请改用已有工具重试”回给模型再跑一轮，
#   而不是把乱码当答案结束。
_DSML_MARK = re.compile('[<\uff5c|]{2}\s*DSML\s*[\uff5c|]{2}', re.I)
_DSML_INVOKE = re.compile('invoke\\s+name\\s*=\\s*["\']([^"\']+)["\']', re.I)
_DSML_ANY_TAG = re.compile('</?[\uff5c|]{2}\s*DSML\s*[\uff5c|]{2}[^>]*>', re.I)

# ★ 2026-09-29（第二轮，按用户新导出的记录）：
#   除了 <||DSML|| ...>，模型换马甲后还会吐出别的伪 XML 工具调用格式，实测遇到过：
#       <dynamic_function_calls>
#       <invoke name="execute_command">
#       <parameter name="command">dir</parameter>
#       </invoke>
#       </dynamic_function_calls>
#   只认 "DSML" 字样的检测会漏掉这种，于是乱码又被当成最终答案显示。
#   这里改成识别**任意把工具调用写成 XML 文本**的形态：
#     · 明确的工具调用容器标签（dynamic_function_calls / tool_calls / function_calls …）
#     · <invoke name="...">（带 name 属性的调用标签）
#     · <parameter name="..."> 与 </invoke> 同时出现
#   只认**完整标签**，普通回答里的 <div> 之类不会命中（有单测覆盖）。
_XML_TOOL_MARK = re.compile(
    r'<\s*/?\s*(?:dynamic_function_calls?|tool_calls?|function_calls?|tool_invocations?'
    r'|dsml)\s*>',
    re.I)
_XML_INVOKE_TAG = re.compile(r'<\s*invoke\s+name\s*=\s*["\']?([\w\-\.]+)', re.I)
_XML_PARAM_TAG = re.compile(r'<\s*parameter\s+name\s*=\s*["\']?[\w\-\.]+', re.I)
_XML_CLOSE_INVOKE = re.compile(r'<\s*/\s*invoke\s*>', re.I)


def _looks_like_textual_tool_call(text):
    """正文里是否出现了「把工具调用写成 XML 文本」的形态。"""
    if not text:
        return False
    if _DSML_MARK.search(text):
        return True
    if _XML_TOOL_MARK.search(text):
        return True
    if _XML_INVOKE_TAG.search(text):
        return True
    if _XML_PARAM_TAG.search(text) and _XML_CLOSE_INVOKE.search(text):
        return True
    return False


def _extract_textual_tool_name(text):
    """从伪 XML 文本里取出它想调用的工具名；取不到返回空串。"""
    if not text:
        return ''
    m = _DSML_INVOKE.search(text) or _XML_INVOKE_TAG.search(text)
    return m.group(1).strip() if m else ''


def _detect_bad_tool_call(text, allowed_names=None):
    """模型调用了一个**不在可用工具列表里**的工具名时，返回 (工具名, 提示语)。

    判定依据（按重要性）：
      1. 文本里出现了工具调用的原始标记（说明这次调用没走 function calling 通道，
         因此系统没有执行它）—— 这是“工具名不在可用列表”的典型表现；
      2. 从标记里取出它想调用的工具名；
      3. 若该名字**确实**在可用列表里（allowed_names），说明只是格式问题，仍然提醒重试；
         若不在列表里，按用户反馈的重点明确告知“该工具不存在/当前未启用”。
    没有标记则返回 (None, None)，不影响正常回答。
    """
    if not _looks_like_textual_tool_call(text):
        return None, None
    name = _extract_textual_tool_name(text)
    avail = ""
    if allowed_names:
        avail = "\n当前**可用**的工具名只有：" + "、".join(sorted(allowed_names))
    if name:
        not_in_list = bool(allowed_names) and (name not in allowed_names)
        what = ("不在当前可用工具列表中" if not_in_list else "这次没有被系统执行（调用格式不规范）")
        msg = (f"⚠️ 你刚才尝试调用工具 `{name}`，但它{what}，因此没有执行。"
               f"请**改用可用工具名重新调用一次**；确实不需要工具时，直接给出最终答案。"
               f"（不要输出尖括号形式的调用标记，那不会被解析。）{avail}")
    else:
        msg = ("⚠️ 你刚才的工具调用没有被系统识别（未能解析出工具名），因此没有执行。"
               "请用标准的工具调用方式，工具名必须来自系统提供的工具列表。"
               f"确实不需要工具时，直接给出最终答案。{avail}")
    return (name or '(未解析出工具名)'), msg


def _pack_tool_result(ret, out, owner_id, task_id, tool_name, limit):
    """把工具输出打包为注入上下文的 tool 消息内容（★ 对所有工具生效）：

    - 输出长度 ≤ limit：原样返回（limit 取自设置弹窗"工具输出截断长度"，默认 4000）；
    - 输出长度 > limit：★ 客户端 agent 已在执行时就地落盘（写入客户端机器
      workspace/tool_outputs/，服务器与客户端可能不在同一台机器），其返回内容
      本身已含"预览 + 相对路径提示"。此处仅作兜底：若收到未落盘的超长输出
      （旧版 agent），截断为前 limit 字符，避免直接撑爆模型上下文。
    """
    if out is None:
        out = ""
    out = str(out)
    # ★ 2026-10-13（修「截断提示数字恒错」）：
    #   客户端 _maybe_persist_output 超限时**已经**在返回串里写好了
    #   「（输出过长：共 {真实长度} 字符，完整内容已保存到…）」+ 预览。
    #   这里若再按 len(out) 判一次超限，量到的就是"处理后的长度"
    #   （≈ limit + 提示本身，恒定 4153 之类），于是多加一条**数字恒错**的提示，
    #   并与客户端那条并存（用户看到两行）。
    #   识别到客户端已处理的标记 → 只补返回码，不再重复截断/提示。
    if '（输出过长：共 ' in out and '完整内容已保存到工作目录下' in out:
        return f"返回码: {ret}\n{out}"
    # ★ 2026-10-14（修用户报告的 read_file 截断问题）：
    #   #read_file 的设计就是"完整返回文件内容、不落盘"（见 agent.py 里它的注释），
    #   客户端已在 max_chars（默认 20 万）层面做了保护，内容就是模型要读的文件本身。
    #   服务端再按全局 limit（默认 4000）截一刀，会让模型读不到文件后半段 ——
    #   而且因为它没有客户端落盘标记，会走到下面的"兜底截断"分支，
    #   附上一句**误导性**提示（"完整内容应已由客户端代理保存到 tool_outputs/"，
    #   实际根本没保存）。这里直接放行：只加返回码，不截断、不提示。
    if isinstance(tool_name, str) and 'read_file' in tool_name:
        return f"返回码: {ret}\n{out}"
    if len(out) <= limit:
        return f"返回码: {ret}\n{out}"
    logging.warning(f"[TOOL_OUTPUT] {tool_name} 收到未落盘超长输出 {len(out)} 字符（可能为旧版客户端代理），已兜底截断")
    # ★ 2026-10-14（按用户反馈"文档与实现对齐"）：提示里明确给出**可行的下一步** ——
    #   告诉模型用 read_file（它"永不落盘、不截断成预览"），而不是只说"应已保存"
    #   （那种措辞既不确定、又没告诉模型怎么拿到完整内容）。
    return (f"返回码: {ret}\n"
            f"（输出过长：共 {len(out)} 字符，已截断到前 {limit} 字符。\n"
            f"要读取**完整内容**请改用 `read_file` 工具"
            f"（它永不落盘、不截断成预览，可用 start_line/end_line 分段读）。）\n"
            + out[:limit])

def _event_view_image_output(cmd_str, out):
    """view_image 命令结果的事件输出瘦身：把 base64 data_url 落盘到 uploads，
    事件里只保留 {ok,name,mime,size,url}（前端缩略图用 URL 加载，避免大 base64
    阻塞事件流解析、DOM 渲染与导出文件）。模型上下文注入仍用原始 out。"""
    if not (isinstance(cmd_str, str) and cmd_str.startswith("#view_image")):
        return out
    try:
        obj = json.loads(out or "")
    except Exception:
        return out
    if not isinstance(obj, dict) or not obj.get("ok"):
        return out
    data_url = obj.get("data_url") or ""
    if not data_url.startswith("data:image/"):
        return out
    try:
        mime = data_url[5:data_url.index(';')]
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(mime)
        if not ext:
            return out
        raw = _b64.b64decode(data_url.split(',', 1)[1])
        if len(raw) > 32 * 1024 * 1024:
            return out
        sname = f"img_{int(time.time())}_{os.urandom(4).hex()}.{ext}"
        with open(os.path.join(UPLOAD_FOLDER, sname), 'wb') as f:
            f.write(raw)
        return json.dumps({"ok": True, "type": "image", "name": obj.get("name"), "mime": mime,
                           "size": obj.get("size") or len(raw), "url": f"/api/image/{sname}"},
                          ensure_ascii=False)
    except Exception:
        return out

def _append_view_image_tool(internal, tool_call, out, ret, model):
    """view_image 工具结果的专用注入逻辑（图像理解）：
    - 客户端代理返回 JSON：{"ok": true, "name":..., "mime":..., "size":..., "data_url": "data:image/...;base64,..."}
    - 读取失败/当前模型不支持图像时：写入普通 tool 消息说明原因；
    - 成功时：tool 消息只写简短说明（不带 base64，避免撑爆上下文与操作导出文件），
      随后追加一条 user 消息携带图片内容块。★ DeepSeek 要求图片仅能出现在 user 消息中，
      因此图片不能直接放进 tool 消息，必须用后续 user 消息承载。"""
    obj = None
    try:
        obj = json.loads(out or "")
    except Exception:
        obj = None
    if ret != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err_text = ""
        if isinstance(obj, dict):
            err_text = obj.get("error") or ""
        err_text = err_text or str(out or "（无输出）")
        internal.append({"role": "tool", "tool_call_id": tool_call.id,
                         "content": f"返回码: {ret}\n读取图片失败：{err_text}"})
        return
    data_url = obj.get("data_url") or ""
    mime = obj.get("mime") or "image/png"
    name = obj.get("name") or "image"
    size = int(obj.get("size") or 0)
    if not data_url.startswith("data:image/"):
        internal.append({"role": "tool", "tool_call_id": tool_call.id,
                         "content": f"返回码: {ret}\n读取图片失败：客户端返回数据格式异常"})
        return
    internal.append({"role": "tool", "tool_call_id": tool_call.id,
                     "content": f"返回码: {ret}\n🖼️ 已读取图片 {name}（{_fmt_size(size)}，{mime}）。图片内容已附加在随后的 user 消息中，请查看并理解图片内容后继续任务。"})
    internal.append({"role": "user", "content": [
        {"type": "text", "text": f"工具 view_image 读取的图片（{name}），请理解图片内容并继续："},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]})

def list_session_children(sid):
    with agents_lock:
        return [a for a in agents.values() if a.get("parent_sid") == sid]

def count_running_children(sid):
    with agents_lock:
        return sum(1 for a in agents.values()
                   if a.get("parent_sid") == sid and a.get("status") == "running")

def _emit_agent(a, evt):
    evt = dict(evt)
    evt.setdefault("agent_id", a["id"])
    q = a.get("event_queue")
    if q is not None:
        try:
            q.put(evt)
        except Exception:
            pass

def _agent_set_status(a, status):
    changed = a.get("status") != status
    if status in ("finished", "killed", "error"):
        a["finished_at"] = time.time()
    a["status"] = status
    if changed:
        _emit_agent(a, {"type": "agent_status", "agent_id": a["id"], "status": status})

def _notify_parent(a, text, kind="agent"):
    """把子 agent 的 return / 最终答复推送给父 agent 会话。

    ★ 2026-09-29：改为按「消息投递方式」设置分流（见 _enqueue_notification）：
      子代理通道设为「插话」→ 立即进入下一轮；设为「排队」→ 等父 agent 空闲。
    """
    sid = a.get("parent_sid")
    with sessions_lock:
        sess = sessions.get(sid)
        if sess:
            _enqueue_notification(sess, text, kind)

def extract_returns(text):
    """子 agent 未使用 return_result 工具、而是在正文里写 #return 时的兜底解析。
    返回 (returns列表, 剩余文本)。"""
    if not text:
        return [], ""
    lines = text.split("\n")
    returns = []
    rest = []
    i = 0
    while i < len(lines):
        m = re.match(r'^#return\s*(.*)$', lines[i])
        if m:
            buf = m.group(1)
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if re.match(r'^#\w', nxt):
                    break
                buf += "\n" + nxt
                i += 1
            buf = buf.strip()
            if buf:
                returns.append(buf)
            continue
        rest.append(lines[i])
        i += 1
    return returns, "\n".join(rest).strip()

def agent_status_text(a):
    lines = [f"子 agent {a['id']} 状态：{a.get('status')}"]
    lines.append(f"模型: {model_to_alias(a.get('model'))} | 思考强度: {a.get('reasoning_effort')}")
    lines.append(f"任务: {_truncate(a.get('task'), 500)}")
    prompts = a.get("prompts", [])
    if prompts:
        lines.append(f"追加消息: {len(prompts)} 条，最近一条: {_truncate(prompts[-1], 200)}")
    rets = a.get("returns", [])
    lines.append(f"里程碑 return: {len(rets)} 条")
    for i, r in enumerate(rets, 1):
        lines.append(f"[return {i}] {_truncate(r, 800)}")
    ans = a.get("final_answer", "")
    if a.get("status") == "finished" and ans:
        lines.append(f"最终答复: {_truncate(ans, 1500)}")
    elif ans:
        lines.append(f"最终答复(已给出): {_truncate(ans, 1500)}")
    else:
        lines.append("最终答复: 无")
    return "\n".join(lines)

def _cmd_title(cmd_str):
    """所有命令统一中文标题（agent 管理命令 + 常规工具命令）。"""
    h = cmd_str.split("\n", 1)[0].strip()
    if h.startswith("#spawn_agent"): return "🤖 创建子 Agent"
    if h.startswith("#prompt"): return "💬 向子 Agent 追加信息"
    if h.startswith("#process"): return "🔎 查询子 Agent / 任务"
    if h.startswith("#kill"): return "🗑️ 销毁子 Agent / 任务"
    if h == "#agents": return "📋 列出子 Agent"
    if h.startswith("#return"): return "📤 返回里程碑"
    if h.startswith("#web_search"): return "🔍 联网搜索"
    if h.startswith("#web_fetch"): return "🌐 抓取网页(Markdown)"
    if h.startswith("#exec"): return "💻 执行 cmd 命令"
    if h.startswith("#write"): return "📝 写入文件"
    if h.startswith("#replace"): return "✏️ 修改文件"
    # ★ 2026-10-12：这几个"运行类"命令的图标改用 **内联 SVG 标记**，不再用 emoji。
    #   起因：用户机器上 U+1FA9F(🪟)、U+2328(⌨)、U+1F9E0(🧠)、U+1F4AD(💭)、U+26A1(⚡)
    #   全都渲染成空白，而 📋/💻/⚙️ 等却能显示 —— 它缺的是一部分较新的 emoji 字形。
    #   由前端 CMD_ICONS 表替换成 SVG —— 矢量图形，**不依赖 emoji 字体**，任何机器都能渲染。
    #   表里没有的名字会被安全去掉，界面上不会留残渣。
    #   ★ 顺序注意：#run_pwsh 必须都在其它 #run_* 之前判完（用 startswith，前缀不冲突，
    #     但这里曾因为插入重复块导致前四行变成死代码 —— 保持一份、别重复）。
    if h.startswith("#run_pwsh"): return "[[ico:pwsh]] 执行 PowerShell 命令"
    if h.startswith("#run_python"): return "🐍 运行 Python"
    if h.startswith("#run_bash"): return "📜 运行 Bash"
    if h.startswith("#run_node"): return "🟢 运行 Node.js"
    if h.startswith("#run_cpp"): return "⚙️ 编译运行 C++"
    if h.startswith("#fetch_page"): return "🌐 抓取网页"
    if h.startswith("#timer"): return "⏱️ 定时任务"
    if h.startswith("#vdesk"): return "🖥️ 虚拟桌面"
    if h.startswith("#ocr"): return "👁️ 屏幕识别"
    if h.startswith("#input"): return "🖱️ 模拟输入"
    if h.startswith("#save"): return "💾 保存记忆"
    if h.startswith("#delete"): return "🗑️ 删除记忆"
    if h.startswith("#list"): return "📋 列出记忆"
    if h.startswith("#search_memory"): return "🔍 搜索记忆"
    if h.startswith("#load"): return "📥 加载记忆"
    if h.startswith("#plan"): return "📋 任务计划"
    if h.startswith("#ask_user"): return "❓ 询问用户"
    if h.startswith("#compress"): return "🧹 上下文压缩"
    if h.startswith("#view_image"): return "🖼️ 查看图片"
    if h.startswith("#read_file"): return "📖 读取文件"
    return None

def spawn_agent(parent_sid, task, model, effort, thinking):
    """创建子 agent 并启动其工作线程。返回 agent ID（以 A 开头）。"""
    aid = "A" + str(uuid.uuid4())[:8]
    with sessions_lock:
        sess = sessions.get(parent_sid)
        if sess:
            sess["live_children"] = sess.get("live_children", 0) + 1
    a = {
        "id": aid,
        "parent_sid": parent_sid,
        "model": model,
        "reasoning_effort": effort,
        "thinking_enabled": thinking,
        "status": "running",
        "cancel_event": threading.Event(),
        "wait_event": threading.Event(),
        "msg_queue": [],   # ★ 统一消息队列（kind 区分用户消息/通知，want_idle 区分插话/排队）
        "pending_notifications": [],   # ★ 子 agent 的定时任务/询问通知队列（timer_notify 会直接 append）
        "busy": False,
        "final_answered": False,
        "command_events": {},
        "task": task,
        "prompts": [],
        "returns": [],
        "final_answer": "",
        "created_at": time.time(),
        "finished_at": None,
        "event_queue": sess.get("event_queue") if sess else None,
    }
    with agents_lock:
        agents[aid] = a
    _emit_agent(a, {"type": "agent_spawned", "agent_id": aid, "model": model, "effort": effort,
                    "task": task, "created_at": a["created_at"]})
    _agent_set_status(a, "running")
    threading.Thread(target=child_agent_stream, args=(a,), daemon=True).start()
    return aid

def prompt_agent(a, content):
    """向子 agent 追加新信息（作为新的用户消息），触发其继续推理。"""
    # ★ 2026-09-29：统一用 setdefault，任何键缺失都不再抛 KeyError。
    #   以前这里写 a["prompts"].append / a["user_message_queue"].append，
    #   而子 agent 字典已经改用 msg_queue —— 直接 KeyError，
    #   表现为用户看到的「错误: 'user_message_queue'」（工具全量测试里
    #   模型调用 prompt_agent 时整轮中断）。
    a.setdefault("prompts", []).append(content)
    a.setdefault("msg_queue", []).append(content)
    a["final_answered"] = False
    if a.get("status") != "killed":
        _agent_set_status(a, "running")
    _emit_agent(a, {"type": "agent_prompted", "agent_id": a["id"], "content": content})
    evt = a.get("wait_event")
    if evt:
        evt.set()

def child_return(a, content):
    """子 agent 的 #return：记录里程碑并推送给父 agent。"""
    a["returns"].append(content)
    _emit_agent(a, {"type": "agent_return", "agent_id": a["id"], "content": content})
    _notify_parent(a, f"🔔 子 agent {a['id']} 返回了里程碑：\n{content}", "agent_return")
    return f"已返回给父 agent（第 {len(a['returns'])} 条里程碑）"

def _json_err(msg):
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


# ==================== 上下文管理：#compress 主动压缩 + 50% 占用警告 ====================

def _record_active_task(holder, task_id, cmd_str):
    """记录正在运行的定时/异步任务（供 #compress 保留命令与 ID）。
    holder 为会话 dict（父 agent）或子 agent dict。"""
    kind = "timer" if cmd_str.startswith("#timer ") else "async"
    holder.setdefault("active_tasks", {})[task_id] = {
        "task_id": task_id, "type": kind, "command": cmd_str[:400],
        "status": "running", "started_at": time.time(), "client_id": None,
    }


def _match_task_client_id(holder, task_id, output):
    """命令结果回传时，解析客户端返回的任务 ID（如“Timer 任务已启动，ID: xxx”），
    关联到 active_tasks 记录，供 #compress 输出“客户端ID”。"""
    m = re.search(r'ID[:：]\s*(\S+)', output or "")
    if not m:
        return
    cid = m.group(1)
    at = holder.get("active_tasks") or {}
    if task_id in at and not at[task_id].get("client_id"):
        at[task_id]["client_id"] = cid


def _do_compress(ctx, summary):
    """执行上下文压缩：internal 替换为 [system, 压缩摘要]。
    压缩摘要 = 阶段摘要(something) + 任务计划(防止丢失目标) + 运行中任务 + 子 agent 信息。
    返回 (输出文本, returncode)。"""
    sid = ctx.get("session_id")
    internal = ctx.get("internal")
    emit_fn = ctx.get("emit")
    is_parent = ctx.get("is_parent", False)
    agent = ctx.get("agent")

    if not internal:
        return _json_err("当前没有可压缩的上下文"), -1
    sys_msg = None
    for m in internal:
        if m.get("role") == "system":
            sys_msg = m
            break
    if sys_msg is None:
        return _json_err("上下文缺少系统提示，无法压缩"), -1

    # 1) 任务计划（codingplan）：保留，防止模型丢失目标
    plan_text = ""
    plan = _plan_get(sid)
    if plan and plan.get("steps"):
        marks = {"done": "[x]", "doing": "[>]", "failed": "[!]", "pending": "[ ]"}
        rows = [f"## 任务计划（保留，防止丢失目标）：{plan.get('title') or '任务计划'}"]
        for i, s in enumerate(plan["steps"], 1):
            rows.append(f"{marks.get(s.get('status', 'pending'), '[ ]')} {i}. {s.get('text', '')}")
        plan_text = "\n".join(rows)

    # 2) 正在运行的异步/定时任务（命令与 ID）
    tasks_text = ""
    holder = agent if agent is not None else None
    active_tasks = {}
    if holder is not None:
        with agents_lock:
            active_tasks = dict(holder.get("active_tasks") or {})
    else:
        with sessions_lock:
            sess = sessions.get(sid)
            active_tasks = dict(sess.get("active_tasks") or {}) if sess else {}
    running_tasks = {k: v for k, v in active_tasks.items() if v.get("status") == "running"}
    if running_tasks:
        rows = ["## 正在运行的异步/定时任务（命令与ID）"]
        for tid, t in running_tasks.items():
            client = f"（客户端ID: {t['client_id']}）" if t.get("client_id") else ""
            rows.append(f"- [{t.get('type', 'exec')}] ID: {tid}{client} | 命令: {t.get('command', '')}")
        tasks_text = "\n".join(rows)

    # 3) 父 agent：所有子 agent 的 ID 与最后一次提示词
    agents_text = ""
    children = list_session_children(sid) if is_parent else []
    if is_parent and children:
            rows = ["## 子 Agent 状态（ID 与最后一次提示词）"]
            for a in children:
                aid = a.get("id")
                prompts = a.get("prompts") or []
                last_prompt = str(prompts[-1])[:800] if prompts else ""
                task = (a.get("task") or "")[:200]
                rows.append(f"- **{aid}**（状态: {a.get('status')}）初始任务: {task}"
                            + (f"\n  最后一次提示词: {last_prompt}" if last_prompt else ""))
            agents_text = "\n".join(rows)

    compressed = (
        "【上下文压缩】此前所有对话与工具历史已删除，以下为压缩后的完整上下文，请基于此继续工作：\n\n"
        "## 阶段摘要（用户需求 / 已完成进度 / 重要信息 / 下一步 / 关键文件路径）\n"
        f"{summary}"
    )
    if plan_text:
        compressed += "\n\n" + plan_text
    if tasks_text:
        compressed += "\n\n" + tasks_text
    if agents_text:
        compressed += "\n\n" + agents_text
    if agent is not None:
        compressed += f"\n\n（你是子 agent {agent.get('id')}，父 agent 可通过 #prompt 联系你）"

    # ★ 2026-09-29（按用户要求）：压缩前把**即将被删除的历史**留一份存档，
    #   否则 #save 落盘的记忆里没有压缩前的内容，“记忆关键词搜索”永远搜不到它们。
    #   存档只用于检索：accumulate 到会话/子 agent 的 pre_compress_archive，绝不参与上下文。
    _old_msgs = [m for m in internal if isinstance(m, dict) and m.get("role") != "system"]
    if _old_msgs:
        def _archive(holder):
            try:
                arc = holder.setdefault("pre_compress_archive", [])
                arc.extend(_old_msgs)
                # 防止无限膨胀：最多保留 4000 条
                if len(arc) > 4000:
                    del arc[:len(arc) - 4000]
            except Exception:
                pass
        if agent is not None:
            with agents_lock:
                _archive(agent)
        else:
            with sessions_lock:
                _sess_arc = sessions.get(sid)
                if _sess_arc is not None:
                    _archive(_sess_arc)

    # 替换上下文：只保留 system + 压缩摘要（旧历史全部删除）
    internal[:] = [sys_msg, {"role": "user", "content": compressed}]

    # 清除会话/子 agent 的警告标志（压缩后上下文已变小，允许下次超阈值再次警告）
    if agent is not None:
        with agents_lock:
            agent["ctx_warned"] = False
            agent["ctx_warn_last_ratio"] = 0
    else:
        with sessions_lock:
            sess = sessions.get(sid)
            if sess:
                sess["ctx_warned"] = False
                sess["ctx_warn_last_ratio"] = 0

    # ★ 压缩后当前上下文真实占用待下次 LLM 调用更新：清零（不推 usage 事件，
    #   避免前端在工具显示前出现“待更新”的虚假用量信息）
    ctx_usage = ctx.get("usage")
    if ctx_usage is not None:
        ctx_usage["ctx_tokens"] = 0

    if emit_fn:
        emit_fn({"type": "context_compressed", "content": compressed, "summary": summary[:500]})
        # ★ 前端操作记录导出需要完整摘要文本（buildImportedMessages 以它为压缩快照起点），
        #   故此处不再截断 3000 字符；前端对该事件为静默处理，不影响界面性能。

    logging.info(f"[COMPRESS] 会话 {sid} 上下文已压缩（{'子agent ' + str(agent.get('id')) if agent else '父agent'}）："
                 f"历史全部删除，新上下文 {len(compressed)} 字符，任务数 {len(running_tasks)}，子agent数 {len(children) if is_parent else 0}")
    return json.dumps({
        "success": True,
        "action": "compress_context",
        "message": "上下文已压缩：已删除此前所有历史，仅保留系统提示、阶段摘要、任务计划、运行中任务与子代理信息。",
        "new_context_chars": len(compressed),
        "running_tasks": len(running_tasks),
    }, ensure_ascii=False), 0


_CJK_RE = re.compile(r'[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\u3000-\u303F]')

def _estimate_tokens_text(s):
    """估算文本 token 数（保守）：CJK 字符 ≈0.62/字，其它 ≈0.32/字符（均略高于官方口径）。
    超长文本采样估算（样本≤60万字符，误差 ±3% 左右），避免每次调用前的性能开销。"""
    if not s:
        return 0
    n = len(s)
    if n <= 600_000:
        sample, k = s, 1
    else:
        k = n // 600_000
        sample = s[::k]
    try:
        cn = len(_CJK_RE.findall(sample))
    except Exception:
        return 0
    other = len(sample) - cn
    return int((cn * 0.62 + other * 0.32) * k)

def _estimate_tokens_messages(msgs):
    """估算消息列表（internal）的 token 数：文本按字符估算；图片按固定开销；工具调用按 JSON 文本估算。"""
    try:
        parts = []
        n_img = 0
        for m in (msgs or []):
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    t = part.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                    if part.get("image_url"):
                        n_img += 1
            rc = m.get("reasoning_content")
            if isinstance(rc, str):
                parts.append(rc)
            tc = m.get("tool_calls")
            if tc:
                parts.append(json.dumps(tc, ensure_ascii=False))
        est = _estimate_tokens_text("\n".join(parts)) + len(msgs or []) * 4 + n_img * 1100
        return est
    except Exception:
        return 0

def _cap_tool_args(args, limit=None):
    """★ 2026-10-06（修「导出再导入后上下文暴涨」）：
    裁剪**超大**的工具调用参数，保留头部、截掉尾部并注明原长度。

    背景（用户实测）：上下文从 30,949 暴涨到 544,562 tokens。
    根因是模型把**整份记忆全文**当参数写进了 `#mem_save`：
    三个 execute_command 的 arguments 分别是 499,112 / 476,183 / 451,104 字符，
    合计 142 万字符（≈54 万 tokens）。导出时被忠实写进 messages，
    导入时又原样恢复 —— 上下文自然爆掉。

    裁剪原则：**保守**。正常参数（执行命令、写文件、JSON 参数）远小于上限，
    一律不动；只有这种异常巨大的（把整份文档塞进命令）才截断，
    并在末尾写明被截掉多少，方便模型/用户看出发生了什么。
    """
    try:
        s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    except Exception:
        return args
    lim = int(limit or _TOOL_ARG_CHAR_LIMIT)
    if lim <= 0 or len(s) <= lim:
        return s
    # ★ 提示语用**短 ASCII**：中文提示会被 _estimate_tokens_text 按 5 token/字估算，
    #   白白多出上万 token（实测提示语本身贡献了约 1.6 万 tokens）。
    return s[:lim] + f"\n[TRUNCATED: {len(s)} chars -> {lim}]"


# 单个工具参数保留的最大字符数（约合 2 万 tokens）。
# 取得足够大：正常命令/文件写入都不会触发；只有"把整份文档塞进参数"这种才会被截。
_TOOL_ARG_CHAR_LIMIT = 100_000

# ★ 2026-10-12：**需要保真**的工具参数上限（比起默认值高 100 倍）。
#
#   起因：我一开始把**所有**工具参数都截到 10 万字符，然后去磁盘核对，
#   发现这会毁掉正常功能 —— 记忆文件的实测体积：
#       server/workspace/memories: 2205 个，178 个 > 100KB，最大 3,669,969 字节
#       D:\czz\DeepChat\.../memories: 1507 个，499 个 > 100KB，最大 6,338,253 字节
#   这些工具的"参数"就是**要落盘的数据本身**（记忆内容 / 文件内容），
#   截断等于把用户的数据改成半截 —— 绝不能做。
#
#   所以按工具区分：
#     · 保真类（#mem_save / #load / #write / #replace）：给 10M 字符的宽松上限，
#       实际等于"不截"，只防极端异常；
#     · 其它（#exec / #run_* 等）：仍是 10 万，专门拦"把整份文档塞进命令参数"
#       这种让上下文暴涨的用法（用户实测单条 2,010,456 字符 ≈ 67 万 tokens）。
#   ★ 2026-10-12 纠正：`#mem_save` **不在这里** —— 它是服务端内部用于把记忆
#   内容送交客户端落盘的命令（见 #save 分支里的 wc(f"#mem_save ...")），
#   服务端**没有**它的处理分支，模型也不该用它（模型该用的 save 工具只要 title，
#   内容由服务端组装）。把它算作"保真类"等于给误用开绿灯：实测模型用
#   execute_command 发过一次 `#mem_save <文件名>\n<170 万字符快照>`，
#   若不截断，上下文照样被撑爆。
_FIDELITY_TOOLS = ("#load", "#write", "#replace")
_FIDELITY_ARG_CHAR_LIMIT = 10_000_000


def _tool_arg_limit_for(cmd_str):
    """按命令前缀挑参数上限：保真类给宽松值，其余用默认值。"""
    try:
        h = (cmd_str or "").lstrip()
        for t in _FIDELITY_TOOLS:
            if h.startswith(t):
                return _FIDELITY_ARG_CHAR_LIMIT
    except Exception:
        pass
    return _TOOL_ARG_CHAR_LIMIT


def _cap_tool_args_smart(args, limit=None):
    """按**参数里的命令前缀**挑上限来裁剪工具参数。

    与 _cap_tool_args 的区别：后者的上限由调用方统一给定（历史上都是 10 万），
    于是会把 #mem_save / #write_file / #replace 的**真实载荷**一起截断 ——
    实测记忆文件最大 6.3MB，统一截 10 万等于毁掉用户数据。
    这里从 arguments 的 JSON 里取出 command 字段，用 _tool_arg_limit_for 判断，
    保真类走宽松上限，其余仍用 10 万。
    """
    try:
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        if limit is not None:
            return _cap_tool_args(raw, limit)
        # 取 command 字段（工具参数一律是 {"command": "..."}）
        cmd = ""
        try:
            _o = json.loads(raw)
            if isinstance(_o, dict):
                cmd = str(_o.get("command") or "")
        except Exception:
            cmd = raw[:200]
        return _cap_tool_args(raw, _tool_arg_limit_for(cmd))
    except Exception:
        return args


def _cap_oversized_messages(msgs, limit=None):
    """对一批历史消息做"超大 tool_calls 参数"裁剪（原地修改），返回处理条数。"""
    n = 0
    try:
        for m in (msgs or []):
            if not isinstance(m, dict):
                continue
            for tc in (m.get("tool_calls") or []):
                try:
                    fn = tc.get("function") or {}
                    a = fn.get("arguments")
                    # ★ 2026-10-12：用 _cap_tool_args_smart —— 保真类工具
                    #   （#mem_save/#write/#replace/#load）的参数是要落盘的数据本身，
                    #   统一截 10 万会把记忆/文件内容毁掉（实测记忆文件最大 6.3MB）。
                    if limit is None:
                        _new_args = _cap_tool_args_smart(a)
                        if _new_args != a:
                            fn["arguments"] = _new_args
                            n += 1
                    elif isinstance(a, str) and len(a) > int(limit):
                        fn["arguments"] = _cap_tool_args(a, limit)
                        n += 1
                except Exception:
                    continue
    except Exception:
        pass
    return n


def _trim_internal_to_fit(internal, limit_est=None):
    """上下文超限时自动丢弃最早的内容（保留 system 与最近消息），返回丢弃的条数。
    ★ 2026-09-11 v2（按用户要求）：超限不再提示，直接丢弃 1M token 以前的内容。
    丢弃按“整条消息”进行；避免产生孤立的 tool 消息（保持 tool_calls 配对完整）。"""
    if limit_est is None:
        limit_est = CTX_SEND_LIMIT_EST
    try:
        if not internal or len(internal) <= 2:
            return 0
        # ★ 2026-10-06：先做一次"超大工具参数"裁剪（见 _cap_tool_args）。
        #   这一步与"按条丢弃"互补：整条消息本身超限时，丢条丢不掉（只有 1~3 条），
        #   但把里面巨大的 arguments 截短就能立刻降下来。
        #   放在这里的好处是**兜住所有入口**（导入注入、结构化重建、实时累积）。
        try:
            _capped = _cap_oversized_messages(internal)
            if _capped:
                logging.info(f"[CTX] 已裁剪 {_capped} 处超大工具参数（单条上限 {_TOOL_ARG_CHAR_LIMIT} 字符）")
        except Exception:
            pass
        sizes = [max(_estimate_tokens_messages([m]), 1) for m in internal]
        total = sum(sizes)
        if total <= limit_est:
            return 0
        keep_from = 1                      # 永远保留 internal[0]（system 提示词）
        last_keep = len(internal) - 1      # 至少保留最后一条（最新消息）
        cur = total
        while keep_from < last_keep and cur > limit_est:
            cur -= sizes[keep_from]
            keep_from += 1
        # 避免保留区以“孤立的 tool 消息”开头（其配对的 assistant 已被丢弃）
        while keep_from < last_keep and isinstance(internal[keep_from], dict) and internal[keep_from].get("role") == "tool":
            cur -= sizes[keep_from]
            keep_from += 1
        dropped = keep_from - 1
        if dropped <= 0:
            return 0
        del internal[1:keep_from]
        try:
            _ensure_tool_pairing(internal)
        except Exception:
            pass
        return dropped
    except Exception:
        return 0

def _truncate_last_message_to_fit(internal, limit_est=None):
    """裁剪整条消息后仍超限（例如单条消息本身超大，如重建会话时全部历史被拼成一条）时，
    对最后一条消息的文本做尾部截取（保留最近部分）。★ 2026-09-11 v2（超限直接丢旧内容）。
    返回是否进行了截取。"""
    if limit_est is None:
        limit_est = CTX_SEND_LIMIT_EST
    try:
        if len(internal) < 2:
            return False
        m = internal[-1]
        c = m.get("content")
        if not isinstance(c, str) or len(c) < 2000:
            return False
        other = _estimate_tokens_messages(internal[:-1])
        budget = max(limit_est - other - 200, 1000)
        keep_chars = max(2000, int(budget / 0.62 * 0.95))
        if len(c) <= keep_chars:
            return False
        prefix = "…（更早的内容已因长度限制省略）…\n"
        m["content"] = prefix + c[-keep_chars:]
        if _estimate_tokens_messages(internal) > limit_est:
            keep2 = max(2000, keep_chars // 2)
            if keep2 < len(c):
                m["content"] = prefix + c[-keep2:]
        return True
    except Exception:
        return False

def _hard_shrink_last_message(internal, limit_est=None):
    """最后手段：把最后一条消息的文本大幅截到剩余预算内，返回是否有变化。"""
    if limit_est is None:
        limit_est = CTX_SEND_LIMIT_EST
    try:
        if len(internal) < 2:
            return False
        m = internal[-1]
        c = m.get("content")
        if not isinstance(c, str) or len(c) <= 300:
            return False
        other = _estimate_tokens_messages(internal[:-1])
        budget = max(limit_est - other - 100, 300)
        keep_chars = max(200, int(budget / 0.62 * 0.9))
        if len(c) <= keep_chars:
            return False
        m["content"] = "…（内容因长度限制大幅截断）…\n" + c[-keep_chars:]
        return True
    except Exception:
        return False

def _maybe_warn_context(sid, internal, holder, emit_fn, usage):
    """上下文占用 ≥50% 时向模型推送警告（基于真实 usage：最近一次 LLM 调用的输入 token，
    即 usage["ctx_tokens"]；压缩后由下次调用自动更新为压缩后的大小）。
    阈值可调：优先取会话设置 ctx_warn_ratio（设置弹窗可调），否则用全局 CTX_WARN_RATIO。
    分档警告：首档为阈值（默认 50%），此后每提高 15% 再警告一次；压缩后重置。holder 为会话 dict 或子 agent dict。"""
    cur = int(usage.get("ctx_tokens") or 0)
    if cur <= 0:
        return False   # 尚无真实用量数据（尚未发生 LLM 调用）
    # ★ 阈值：会话设置优先（前端设置弹窗"上下文警告阈值(%)"），子 agent 继承父会话设置
    threshold = CTX_WARN_RATIO
    st = (holder.get("settings") or {}) if isinstance(holder, dict) else {}
    ratio_setting = st.get("ctx_warn_ratio")
    if ratio_setting is None:
        with sessions_lock:
            sess = sessions.get(sid)
            ratio_setting = ((sess.get("settings") or {}).get("ctx_warn_ratio")) if sess else None
    if ratio_setting not in (None, ""):
        try:
            threshold = min(max(float(ratio_setting), 0.01), 1.0)
        except Exception:
            pass
    ratio = cur / CTX_WINDOW_TOKENS
    if ratio < threshold:
        return False
    # ★ FIX(2026-09-11)：从“仅警告一次”升级为“分档警告”——每提高 15% 再警告一次，
    #   避免只在 50% 警告过一次后、直到超限都再无提醒。
    _last = float(holder.get("ctx_warn_last_ratio") or 0)
    if _last and ratio < _last + 0.15:
        return False
    holder["ctx_warn_last_ratio"] = ratio
    holder["ctx_warned"] = True
    warn = (f"⚠️ 上下文占用警告：当前上下文占用 {cur:,} tokens（{ratio*100:.1f}%），"
            f"已超过上下文窗口（{CTX_WINDOW_TOKENS:,} tokens）的 {threshold*100:.0f}%。"
            f"若当前大阶段已完成、无需保留以往细节，请调用 compress_context 工具进行压缩，"
            f"摘要需包含：用户需求、已完成进度、已获取的重要信息、下一步行动、关键文件路径。")
    # 用 user role 追加，避免 system 消息位置问题（DeepSeek OpenAI 兼容接口）
    internal.append({"role": "user", "content": warn})
    if emit_fn:
        emit_fn({"type": "context_warning", "content": warn, "ratio": round(ratio, 4), "est_tokens": cur,
                 "threshold": threshold})
    logging.info(f"[CTX-WARN] 会话 {sid} 上下文占用 {ratio*100:.1f}%（{cur} tokens，真实 usage，阈值 {threshold*100:.0f}%），已推送压缩警告")
    return True

# ═══════════════════════════════════════════════════════════════════════
#  DeepSeek 官方 Search / Fetch 工具接口
#    POST {base}/v1/tools/search   {"query":..., "count":..., "need_content":...}
#    POST {base}/v1/tools/fetch    {"url":...}
#  · 鉴权：Authorization: Bearer <API Key>（Key 只在服务端，绝不下发客户端）
#  · 计费：每 1000 次成功请求 5 元；失败/无结果不计费
#  · 限速：Search 100 QPS / Fetch 25 QPS（账号级），超限返回 429
# ═══════════════════════════════════════════════════════════════════════
# ★ 2026-10-19（按用户要求）：单价改为**可配置**（设置 → 模型与价格）。
#   官方计价单位就是"每 1000 次成功请求 5 元"（即 0.005 元/次），
#   这里只作为缺省值；实际取 settings["web_tool_price_per_1k"]。
DEEPSEEK_TOOLS_PRICE_PER_1K = 5.0     # 默认：元 / 1000 次成功请求

# ★ 2026-10-23：服务器版本号 —— **最新版本以此为准**。
#   注入页面后由前端与本地代理自报的版本比对，低则提示升级。
#   未标记版本的老代理按 '2.0' 处理。
APP_VERSION = '2.1'


def web_tool_price_per_1k():
    """读取联网工具单价（元 / 1000 次成功请求）。非法值回退默认 5.0。"""
    try:
        st = load_server_settings()
        v = st.get("web_tool_price_per_1k")
        if v is None or v == "":
            return DEEPSEEK_TOOLS_PRICE_PER_1K
        f = float(v)
        return f if f >= 0 else DEEPSEEK_TOOLS_PRICE_PER_1K
    except Exception:
        return DEEPSEEK_TOOLS_PRICE_PER_1K


def _deepseek_tool_api(kind, payload, model_name=None):
    """调用 DeepSeek 的 search / fetch 接口。

    返回 (ok: bool, data_or_errmsg, extra: dict)
      extra 里带 http_status / url / elapsed_ms，便于事件与日志。
    """
    import time as _t
    path = "/v1/tools/search" if kind == "search" else "/v1/tools/fetch"
    key, base = get_api_config(model_name)
    extra = {"url": (base or "") + path, "kind": kind}
    if not key:
        return False, ("未配置 API Key，无法调用联网搜索/抓取。"
                       "请在「设置 → 模型与提供商」里为 DeepSeek 提供商填写 API Key。"), extra
    t0 = _t.time()
    try:
        import requests as _rq
    except Exception as e:
        try:
            import urllib.request as _u
            import json as _j
            req = _u.Request((base or DEFAULT_BASE_URL) + path,
                             data=_j.dumps(payload).encode("utf-8"),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"},
                             method="POST")
            with _u.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                extra["http_status"] = r.status
                extra["elapsed_ms"] = int((_t.time() - t0) * 1000)
                return True, _j.loads(body), extra
        except Exception as e2:
            extra["elapsed_ms"] = int((_t.time() - t0) * 1000)
            return False, f"联网{'搜索' if kind=='search' else '抓取'}失败: {e2}", extra
    try:
        resp = _rq.post((base or DEFAULT_BASE_URL) + path,
                        headers={"Authorization": "Bearer " + key,
                                 "Content-Type": "application/json"},
                        json=payload, timeout=60)
        extra["http_status"] = resp.status_code
        extra["elapsed_ms"] = int((_t.time() - t0) * 1000)
        if resp.status_code != 200:
            # OpenAI 兼容错误格式：{"error": {"message": ...}}
            try:
                msg = (resp.json().get("error") or {}).get("message") or resp.text[:400]
            except Exception:
                msg = resp.text[:400]
            return False, f"联网工具返回 HTTP {resp.status_code}: {msg}", extra
        return True, resp.json(), extra
    except Exception as e:
        extra["elapsed_ms"] = int((_t.time() - t0) * 1000)
        return False, f"联网{'搜索' if kind=='search' else '抓取'}失败: {e}", extra


def _fmt_web_search_results(data, query, extra):
    """把搜索结果整理成模型易读的文本（并统计计费）。"""
    items = (data or {}).get("results") or []
    if not items:
        return (f"搜索「{query}」没有找到结果（本次不计费）。\n"
                f"可以换个说法、去掉过于具体的限定词，或用 web_fetch 直接抓已知网址。")
    _p1k = web_tool_price_per_1k()
    lines = [f"搜索「{query}」共 {len(items)} 条结果"
             f"（耗时 {extra.get('elapsed_ms', 0)} ms，按 1 次成功请求计费 ¥{_p1k/1000:.5f}）:", ""]
    for i, it in enumerate(items, 1):
        score = it.get("rerank_score")
        auth = it.get("authority")
        date = it.get("date") or ""
        site = it.get("site_name") or ""
        meta = []
        if score is not None:
            meta.append(f"相关度 {score}")
        if auth is not None and auth >= 0:
            meta.append(f"权威性 {auth}")
        if date:
            meta.append(str(date)[:10])
        if site:
            meta.append(site)
        lines.append(f"[{i}] {it.get('title') or '(无标题)'}")
        lines.append(f"    URL: {it.get('url') or ''}")
        if meta:
            lines.append("    " + " | ".join(meta))
        summ = it.get("summary") or it.get("snippet") or ""
        if summ:
            lines.append("    " + str(summ).replace("\n", " ").strip())
        content = it.get("content") or ""
        if content:
            body = str(content).strip()
            if len(body) > 2000:
                body = body[:2000] + f"…（正文共 {len(content)} 字符，已截断）"
            lines.append("    正文:")
            for ln in body.splitlines():
                lines.append("      " + ln)
        lines.append("")
    return "\n".join(lines)


def _unescape_md(t):
    """还原 DeepSeek fetch 接口过度转义的 Markdown 标点。

    实测该接口会把 `#` `_` `/` `:` `-` `|` `(` `)` `.` 等一律加反斜杠
    （2866 字符正文里有 349 个），对模型是纯噪音。
    这里只去掉"反斜杠 + ASCII 标点"的那一层，且先保护真正的转义序列
    （\n / \t / \\），避免把换行符拆坏。
    """
    if not isinstance(t, str) or "\\" not in t:
        return t
    # 先保护真正的转义序列
    t = t.replace("\\\\", "\x00BS\x00").replace("\\n", "\x00NL\x00").replace("\\t", "\x00TB\x00")
    # 去掉「反斜杠 + ASCII 标点」—— 用显式集合，避免 `-` 被当成范围
    PUNCT = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')
    out = []
    k = 0
    n = len(t)
    while k < n:
        ch = t[k]
        if ch == "\\" and k + 1 < n and t[k + 1] in PUNCT:
            out.append(t[k + 1])
            k += 2
        else:
            out.append(ch)
            k += 1
    t = "".join(out)
    # 还原
    t = t.replace("\x00BS\x00", "\\").replace("\x00NL\x00", "\n").replace("\x00TB\x00", "\t")
    return t


def _fmt_web_fetch(data, url, extra):
    """把抓取结果整理成模型易读的文本。"""
    title = (data or {}).get("title") or ""
    content = _unescape_md((data or {}).get("content") or "")
    if not content.strip():
        return f"抓取 {url} 未取得正文（本次不计费）。可能是动态渲染页面，请改用 fetch_webpage 或 web_search。"
    head = (f"抓取成功: {title or '(无标题)'}\n"
            f"URL: {url}\n"
            f"正文 {len(content)} 字符（Markdown，耗时 {extra.get('elapsed_ms', 0)} ms，"
            f"按 1 次成功请求计费 ¥{web_tool_price_per_1k()/1000:.5f}）\n"
            f"----- 正文开始 -----")
    tail = "----- 正文结束 -----"
    return head + "\n" + content + "\n" + tail


def resolve_server_command(cmd_str, ctx):
    """服务端直接处理的 agent 管理命令（不经浏览器/客户端中转）。
    返回 (output, returncode)；非服务端命令返回 None（交给浏览器中转执行）。"""
    sid = ctx.get("session_id")
    is_parent = ctx.get("is_parent", False)
    agent = ctx.get("agent")

    # ---- #web_search <count> <need_content>\n<query>\n<ori_question> ----
    if cmd_str.startswith("#web_search"):
        first, _, rest = cmd_str.partition("\n")
        tok = first.split()
        try:
            cnt = max(1, min(50, int(tok[1]))) if len(tok) > 1 else 10
        except Exception:
            cnt = 10
        need = (len(tok) > 2 and tok[2] == "1")
        parts = rest.split("\n", 1)
        query = (parts[0] or "").strip()
        ori = (parts[1] or "").strip() if len(parts) > 1 else ""
        if not query:
            return _json_err("web_search 缺少 query"), -1
        payload = {"query": query[:128], "count": cnt, "need_content": need}
        if ori:
            payload["ori_question"] = ori[:128]
        ok, data, extra = _deepseek_tool_api("search", payload, ctx.get("model"))
        if not ok:
            return data, -1
        try:
            _log_web_tool_usage(sid, "search", extra)
        except Exception:
            pass
        return _fmt_web_search_results(data, query, extra), 0

    # ---- #web_fetch\n<url> ----
    if cmd_str.startswith("#web_fetch"):
        _, _, rest = cmd_str.partition("\n")
        url = (rest or "").strip()
        if not url:
            return _json_err("web_fetch 缺少 url"), -1
        ok, data, extra = _deepseek_tool_api("fetch", {"url": url[:8192]}, ctx.get("model"))
        if not ok:
            return data, -1
        try:
            _log_web_tool_usage(sid, "fetch", extra)
        except Exception:
            pass
        return _fmt_web_fetch(data, url, extra), 0

    # ---- #spawn_agent [model] [effort]\n<task> ----
    if cmd_str.startswith("#spawn_agent"):
        first, _, rest = cmd_str.partition("\n")
        tokens = first.split()
        model_arg = tokens[1] if len(tokens) > 1 else None
        effort_arg = tokens[2] if len(tokens) > 2 else None
        task = rest.strip()
        if not is_parent:
            return _json_err("仅父 agent 可以创建子 agent（子 agent 不能再调用 agent）"), -1
        if not task:
            return _json_err("任务内容为空，无法创建子 agent"), -1
        with sessions_lock:
            parent = sessions.get(sid)
        if not parent:
            return _json_err("父会话不存在"), -1
        # ★ 最大子代理数量：来自操作模式设置（默认 3）
        try:
            _max_sub = int((parent.get("settings") or {}).get("max_subagents", MAX_CHILD_AGENTS) or MAX_CHILD_AGENTS)
        except Exception:
            _max_sub = MAX_CHILD_AGENTS
        _max_sub = max(1, _max_sub)
        if count_running_children(sid) >= _max_sub:
            return _json_err(f"已达到最大并发子 agent 数（{_max_sub}），请等待某个子 agent 结束后再创建"), -1
        model = parent.get("model") or default_model_name()
        effort = parent.get("reasoning_effort") or "max"
        thinking = parent.get("thinking_enabled", True)
        _aliases = get_model_aliases()
        if model_arg:
            if model_arg not in _aliases:
                # 容错：只写了思考强度的情况（如 #spawn_agent max）
                if model_arg in ("max", "high", "low") and not effort_arg:
                    effort_arg, model_arg = model_arg, None
                else:
                    _opts = "、".join(_aliases.keys())
                    return _json_err(f"未知模型 '{model_arg}'，可选：{_opts}"), -1
        if effort_arg:
            # ★ 2026-10-14（四档滑块）：Low 现在映射到 effort='low'，这里同步放行
            if effort_arg not in ("max", "high", "low"):
                return _json_err(f"未知思考强度 '{effort_arg}'，可选 max/high/low"), -1
            effort = effort_arg
        if model_arg:
            model = _aliases[model_arg]
        aid = spawn_agent(sid, task, model, effort, thinking)
        return json.dumps({
            "success": True,
            "action": "spawn_agent",
            "agent_id": aid,
            "model": model_to_alias(model),
            "reasoning_effort": effort,
            "max_concurrent": _max_sub,
        }, ensure_ascii=False), 0

    # ---- #prompt <id>\n<content> ----
    if cmd_str.startswith("#prompt"):
        first, _, rest = cmd_str.partition("\n")
        tokens = first.split()
        if len(tokens) < 2:
            return _json_err("缺少子 agent ID"), -1
        aid = tokens[1]
        content = rest.strip()
        if not is_parent:
            return _json_err("仅父 agent 可以给子 agent 追加信息"), -1
        if not content:
            return _json_err("追加信息内容为空"), -1
        with agents_lock:
            a = agents.get(aid)
        if not a or a.get("parent_sid") != sid:
            return _json_err(f"子 agent {aid} 不存在或不属于当前会话"), -1
        if a.get("status") == "killed":
            return _json_err(f"子 agent {aid} 已被销毁，无法追加信息"), -1
        prompt_agent(a, content)
        return json.dumps({
            "success": True,
            "action": "prompt_agent",
            "agent_id": aid,
            "prompt_appended": True,
        }, ensure_ascii=False), 0

    # ---- #process <id>（子 agent 优先，其次客户端异步任务）----
    m = re.match(r'^#process\s+(\S+)', cmd_str)
    if m:
        aid = m.group(1)
        with agents_lock:
            a = agents.get(aid)
        if a and a.get("parent_sid") == sid:
            pub = {
                "success": True,
                "action": "query_agent",
                "agent_id": a["id"],
                "status": a.get("status"),
                "model": model_to_alias(a.get("model")),
                "reasoning_effort": a.get("reasoning_effort"),
                "task": _truncate(a.get("task"), 500),
                "prompt_count": len(a.get("prompts", [])),
                "returns": [_truncate(r, 800) for r in a.get("returns", [])],
                "final_answer": _truncate(a.get("final_answer", ""), 1500),
            }
            return json.dumps(pub, ensure_ascii=False), 0
        return None  # 交给客户端异步任务查询

    # ---- #kill <id>（子 agent 优先，其次客户端异步任务）----
    m = re.match(r'^#kill\s+(\S+)', cmd_str)
    if m:
        aid = m.group(1)
        with agents_lock:
            a = agents.get(aid)
        if a and a.get("parent_sid") == sid:
            already = a.get("status") == "killed"
            a["cancel_event"].set()
            evt = a.get("wait_event")
            if evt:
                evt.set()
            # ★ BUGFIX(2026-08-15) #kill 后 #process 仍显示 running：
            #   此前只设置 cancel_event、等子线程自行察觉取消后再改状态。但子线程
            #   可能正阻塞在 LLM 流式调用/命令等待中，其取消分支直接 break 且不更新
            #   状态，导致状态永远停留在 running。这里立即把状态置为 killed 并广播
            #   （幂等：killed_announced 标记保证只广播一次），#process / #agents
            #   查询与前端 UI 均能马上看到 killed。
            if not a.get("killed_announced"):
                a["killed_announced"] = True
                _agent_set_status(a, "killed")
                _emit_agent(a, {"type": "agent_cancelled", "agent_id": aid})
            return json.dumps({"success": True, "action": "kill_agent", "agent_id": aid,
                               "already_killed": already}, ensure_ascii=False), 0
        return None  # 交给客户端异步任务终止

    # ---- #agents ----
    if cmd_str.strip() == "#agents":
        if not is_parent:
            return _json_err("仅父 agent 可以查看子 agent 列表"), -1
        children = list_session_children(sid)
        pubs = [{
            "agent_id": a["id"],
            "status": a.get("status"),
            "model": model_to_alias(a.get("model")),
            "reasoning_effort": a.get("reasoning_effort"),
            "task": _truncate(a.get("task"), 200),
        } for a in children]
        return json.dumps({"success": True, "action": "list_agents", "agents": pubs}, ensure_ascii=False), 0

    # ---- #return <content>（仅子 agent）----
    if cmd_str == "#return" or cmd_str.startswith("#return\n") or cmd_str.startswith("#return "):
        if is_parent:
            return _json_err("只有子 agent 可以使用 #return"), -1
        if agent is None:
            return _json_err("当前上下文没有子 agent 记录"), -1
        content = cmd_str[len("#return"):].strip()
        if not content:
            return _json_err("#return 内容为空"), -1
        child_return(agent, content)
        return json.dumps({
            "success": True,
            "action": "return_result",
            "agent_id": agent["id"],
            "return_index": len(agent["returns"]),
        }, ensure_ascii=False), 0

    # ==================== 记忆工具：#save / #delete / #list / #load ====================
    # 云端（cloud）→ 服务器本地 server/workspace/memories；本地（local）→ 客户端机器
    # client/workspace/memories（经 ctx["wait_client"] 中转给客户端 #mem_* 命令执行）。
    # 注意：服务器与客户端不一定在同一台机器上，local 操作必须经过客户端中转。
    def _parse_mem_target(first_line):
        """解析第一行的目标参数（local/cloud），返回 (target, title)。"""
        parts = first_line.split()
        if len(parts) >= 2 and parts[1] in ("local", "cloud"):
            target = parts[1]
            title = first_line[len("#save"):].strip()
            title = title[len(target):].strip()
            return target, title
        return None, first_line[len("#save"):].strip()

    # ---- #save [local|cloud] <title>（默认同时保存本地与云端）----
    if cmd_str.startswith("#save"):
        first, _, rest = cmd_str.partition("\n")
        target, title = _parse_mem_target(first)
        if not title and rest:
            title = rest.strip()
        if not title:
            return _json_err("#save 需要提供标题（可含任务目标与进度说明，将作为文件名）"), -1
        internal = ctx.get("internal")
        if not isinstance(internal, list):
            return _json_err("当前对话上下文不可用"), -1
        fname = _mem_filename(title)
        # ★ plan 持久化：保存记忆时把当前任务计划一并写入记忆文件
        sid_for_plan = ctx.get("session_id")
        cur_plan = _plan_get(sid_for_plan) if sid_for_plan else None
        # ★ 2026-09-29：把「上下文压缩前的历史存档」一并写入记忆（只用于关键词检索，
        #   #load 时不会导入），这样 search_memory 也能搜到压缩前的内容。
        _pre_archive = None
        _ctx_agent = ctx.get("agent")
        if _ctx_agent is not None:
            with agents_lock:
                _pre_archive = list(_ctx_agent.get("pre_compress_archive") or [])
        elif sid_for_plan:
            with sessions_lock:
                _s = sessions.get(sid_for_plan)
                _pre_archive = list((_s or {}).get("pre_compress_archive") or [])
        payload = _mem_dump(internal, ctx.get("model"), ctx.get("r_eff"), ctx.get("think"),
                            ctx.get("usage"), cur_plan, pre_compress=_pre_archive)
        results = []
        ok = True
        if target in (None, "cloud"):
            try:
                os.makedirs(MEMORY_DIR, exist_ok=True)
                with open(os.path.join(MEMORY_DIR, fname), 'w', encoding='utf-8') as f:
                    f.write(payload)
                results.append(f"☁️ 云端已保存：{fname}（server/workspace/memories）")
            except Exception as e:
                ok = False
                results.append(f"☁️ 云端保存失败：{e}")
        if target in (None, "local"):
            wc = ctx.get("wait_client")
            if not wc:
                ok = False
                results.append("💻 本地保存不可用（无客户端中转通道）")
            else:
                out, ret = wc(f"#mem_save {fname}\n{payload}", display=cmd_str)
                if ret == 0:
                    results.append(f"💻 本地已保存：{fname}（client/workspace/memories）")
                else:
                    ok = False
                    results.append(f"💻 本地保存失败（返回码 {ret}）：{out}")
        return "\n".join(results), 0 if ok else -1

    # ---- #delete local|cloud <title> ----
    if cmd_str.startswith("#delete"):
        first, _, rest = cmd_str.partition("\n")
        parts = first.split()
        if len(parts) < 2:
            return _json_err("#delete 语法：#delete local|cloud <标题>"), -1
        target = parts[1]
        if target not in ("local", "cloud"):
            return _json_err(f"未知目标 '{target}'，可选 local / cloud"), -1
        title = first[len("#delete"):].strip()
        title = title[len(target):].strip()
        if not title and rest:
            title = rest.strip()
        if not title:
            return _json_err("#delete 需要提供标题"), -1
        fname = _mem_filename(title)
        if target == "cloud":
            path = os.path.join(MEMORY_DIR, fname)
            if not os.path.isfile(path):
                return _json_err(f"云端记忆不存在：{fname}"), -1
            os.remove(path)
            return f"☁️ 云端已删除：{fname}", 0
        wc = ctx.get("wait_client")
        if not wc:
            return _json_err("本地删除不可用（无客户端中转通道）"), -1
        out, ret = wc(f"#mem_delete {fname}", display=cmd_str)
        return (f"💻 本地已删除：{fname}" if ret == 0 else f"💻 本地删除失败（返回码 {ret}）：{out}"), 0 if ret == 0 else -1

    # ---- #list local|cloud|all ----
    if cmd_str.startswith("#list"):
        first = cmd_str.split("\n")[0].strip()
        parts = first.split()
        target = parts[1] if len(parts) > 1 else "all"
        if target not in ("local", "cloud", "all"):
            return _json_err(f"未知目标 '{target}'，可选 local / cloud / all"), -1
        entries = []  # [{name, mtime, size, source}]
        if target in ("cloud", "all"):
            for e in _mem_list_cloud():
                e["source"] = "cloud"
                entries.append(e)
        if target in ("local", "all"):
            wc = ctx.get("wait_client")
            if wc:
                out, ret = wc("#mem_list", display=cmd_str)
                if ret == 0:
                    try:
                        local_list = json.loads(out)
                        if isinstance(local_list, list):
                            for e in local_list:
                                entries.append({"name": e.get("name", "?"), "mtime": e.get("mtime", 0),
                                               "size": e.get("size", 0), "source": "local"})
                    except Exception:
                        pass
            else:
                entries.append({"name": "（本地列表不可用：无客户端中转通道）", "mtime": 0, "size": 0, "source": "local"})
        entries.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        if not entries:
            return "（无记忆文件）", 0
        # ★ 设置项：list 列出记忆最大数量（超出部分不返回给模型）
        list_limit = int(ctx.get("list_limit") or 50)
        total = len(entries)
        truncated = total > list_limit
        entries = entries[:list_limit]
        lines = [f"{'来源':<6}{'修改时间':<20}{'大小':<10}标题"]
        for e in entries:
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.get("mtime", 0))) if e.get("mtime") else "-"
            lines.append(f"{e.get('source','?'):<6}{ts:<20}{_fmt_size(e.get('size',0)):<10}{e.get('name','?')}")
        if truncated:
            lines.append(f"…（记忆共 {total} 条，仅显示前 {list_limit} 条，可在设置中调整最大数量）")
        return "\n".join(lines), 0

    # ---- #search_memory {json}：按关键词搜索记忆标题与内容（多关键词，AND/OR 可选）----
    if cmd_str.startswith("#search_memory"):
        raw = cmd_str[len("#search_memory"):].strip()
        try:
            spec = json.loads(raw) if raw else {}
        except Exception:
            # 容错：也接受“空格分隔的关键词”写法
            spec = {"keywords": raw.split()} if raw else {}
        if not isinstance(spec, dict):
            spec = {"keywords": raw.split()}
        return search_memories(
            spec.get("keywords"),
            mode=spec.get("mode", "all"),
            search_in=spec.get("search_in", "both"),
            target=spec.get("target", "all"),
            limit=spec.get("limit", 10),
            context_chars=spec.get("context_chars", 160),
            wait_client=ctx.get("wait_client"),
        )

    # ---- #load [local|cloud] <file>（追加到当前对话上下文，非覆盖）----
    if cmd_str.startswith("#load"):
        first, _, rest = cmd_str.partition("\n")
        parts = first.split()
        target = None
        file_arg = None
        if len(parts) >= 2 and parts[1] in ("local", "cloud"):
            target = parts[1]
            file_arg = first[len("#load"):].strip()
            file_arg = file_arg[len(target):].strip()
        else:
            file_arg = first[len("#load"):].strip()
        if not file_arg and rest:
            file_arg = rest.strip()
        if not file_arg:
            return _json_err("#load 需要提供记忆文件名"), -1
        fname = _mem_filename(file_arg)
        internal = ctx.get("internal")
        if not isinstance(internal, list):
            return _json_err("当前对话上下文不可用"), -1
        raw = None
        found_src = None
        if target in (None, "cloud"):
            data = _mem_load_cloud(fname)
            if data is not None:
                raw = data
                found_src = "cloud"
        if raw is None and target in (None, "local"):
            wc = ctx.get("wait_client")
            if wc:
                out, ret = wc(f"#mem_read {fname}", display=cmd_str)
                if ret == 0:
                    try:
                        raw = json.loads(out)
                        found_src = "local"
                    except Exception:
                        raw = None
        if raw is None:
            return _json_err(f"未找到记忆文件：{fname}" + ("（本地/云端均不存在）" if target is None else "")), -1
        msgs = raw.get("messages")
        cleaned = _clean_injected_messages(msgs) if isinstance(msgs, list) else None
        if not cleaned:
            return _json_err(f"记忆文件 {fname} 中没有可用的对话消息"), -1
        # ★ 压缩状态还原：记忆保存前若执行过 #compress（历史中含“【上下文压缩】”摘要消息），
        #   自动恢复为“压缩完成后”的状态：仅保留压缩快照（摘要+压缩后消息），并同步裁剪
        #   当前 internal 中的压缩前历史，避免加载出“压缩前完整历史+压缩摘要”混合体。
        snap = _restore_compressed_context(ctx, cleaned)
        if snap is not None:
            cleaned = snap
            compressed_restored = True
        else:
            compressed_restored = False
        # ★ 追加语义：把记忆中的消息追加到当前 internal（不覆盖现有上下文）。
        #   ★ 关键修复（2026-08-16 二次修复）：模型可能在同一轮并行调用多个工具
        #     （如 #list all + #load + #process + #prompt），load 执行时 internal
        #     末尾可能已是前一个工具的 tool 响应，仅检查 internal[-1] 会失效，
        #     导致记忆被追加到末尾、后续 tool 响应变成孤立 tool 消息（API 400：
        #     "Messages with role 'tool' must be a response to a preceding
        #     message with 'tool_calls'"）。
        #     正确做法：从后往前找到【最后一个带 tool_calls 的 assistant 消息】，
        #     把记忆插入到它【之前】，保证该轮所有 tool_calls 与后续 tool 响应
        #     保持紧密配对。
        insert_at = len(internal)
        for _idx in range(len(internal) - 1, -1, -1):
            _mm = internal[_idx]
            if isinstance(_mm, dict) and _mm.get('role') == 'assistant' and _mm.get('tool_calls'):
                insert_at = _idx
                break
        # ★ 2026-10-21：再往前退到最近的 user 消息，让记忆像"历史"一样
        #   出现在**当前提问之前**（原来插在提问之后，模型读到的是
        #   "用户问 → 记忆 → 工具调用"，语义倒置，容易答非所问）。
        #   仍然在 tool_calls 之前，配对关系不变。
        for _idx in range(insert_at - 1, -1, -1):
            _mm = internal[_idx]
            if isinstance(_mm, dict) and _mm.get('role') == 'user':
                insert_at = _idx
                break
        if compressed_restored:
            # 压缩快照已由 _restore_compressed_context 置入 internal（system 之后），无需再插入
            pass
        else:
            internal[insert_at:insert_at] = cleaned
        # ★ plan 恢复：记忆文件中携带的任务计划恢复到当前会话（左侧任务视图同步显示）
        #
        # ★ 2026-10-12（修「计划状态中途回退成旧计划」）：这里原来是**无条件覆盖**。
        #   真实事故（来自用户导出的记录）：模型先建了 8 步的"全工具冒烟测试"计划，
        #   推到第 3 步时执行了一次 #load，加载的是今天早些时候保存的记忆 ——
        #   那份记忆里带着当时那个 4 步旧计划，于是**当前 8 步计划被静默覆盖成 4 步**。
        #   紧接着模型调 #plan step 4/5/7/8 全部报「步骤序号超出范围（1-4）」，
        #   它一度以为是平台故障，只能重建计划（"全工具冒烟测试（重建）"）。
        #   修法：只在当前**没有活跃计划**时才从记忆恢复；当前计划还有未完成步骤时，
        #   保留当前计划并把这件事明确告知模型（它才不会一头雾水地重建）。
        saved_plan = raw.get("plan")
        _cur_plan = _plan_get(ctx.get("session_id"))
        _cur_active = bool(_cur_plan) and any(
            (s or {}).get("status") != "done" for s in (_cur_plan.get("steps") or []))
        if isinstance(saved_plan, dict) and isinstance(saved_plan.get("steps"), list):
            if _cur_active:
                logging.info("[PLAN] #load 携带的旧计划已忽略（当前计划仍有未完成步骤）")
                _plan_note = (f"（注：该记忆里还存着一份旧计划「{saved_plan.get('title') or '未命名'}」，"
                              f"但当前计划「{_cur_plan.get('title') or '未命名'}」还有未完成步骤，"
                              f"已保留当前计划、未做覆盖。如需改用记忆里的计划，请先 #plan clear。）")
            else:
                _plan_put(ctx.get("session_id"), saved_plan)
                emit_fn = ctx.get("emit")
                if emit_fn:
                    emit_fn({"type": "plan_update", "plan": saved_plan})
                _plan_note = ""
        else:
            _plan_note = ""
        # ★ FIX(空思考框)：不再 emit user_message（该事件会触发前端 resetAssistantContainer
        #   创建新助手容器，而模型 load 后可能无思考直接答复，产生空思考框）。
        #   加载提示已包含在返回文本中，经 command_result 展示给界面与模型。
        if compressed_restored:
            note = (f"📥 [记忆已加载] {fname}（来源：{found_src}）—— 该记忆保存前曾执行过 #compress，"
                    f"已自动还原为压缩完成后的状态：丢弃压缩前完整历史，仅载入压缩快照"
                    f"（{len(cleaned)} 条消息：摘要 + 压缩后对话，保存于 {raw.get('saved_at','?')}）")
        else:
            note = (f"📥 [记忆已加载] {fname}（来源：{found_src}，{len(cleaned)} 条消息已追加到当前对话上下文，"
                    f"保存于 {raw.get('saved_at','?')}）")
        # ★ 2026-10-21：附上**内容预览**。
        #   原来只回一句"N 条消息已追加"，界面上看不到加载了什么，
        #   用户和模型都无从确认"到底成没成功"（用户实测：加载后一头雾水）。
        try:
            _prev_lines = []
            _n_user = 0
            for _m in cleaned:
                if not isinstance(_m, dict) or _m.get("role") != "user":
                    continue
                _txt = _m.get("content")
                if not isinstance(_txt, str) or not _txt.strip():
                    continue
                _one = " ".join(_txt.split())[:110]
                _prev_lines.append(f"  · {_one}")
                _n_user += 1
                if _n_user >= 4:
                    break
            if _prev_lines:
                note += ("\n--- 已加载内容预览（前 %d 条用户消息）---\n" % len(_prev_lines)
                         + "\n".join(_prev_lines))
                if len(cleaned) > _n_user:
                    note += (f"\n  …（共 {len(cleaned)} 条消息，此处仅列开头 {len(_prev_lines)} 条；"
                             f"正文已进入上下文，可直接引用/复述）")
        except Exception:
            pass
        if _plan_note:
            note += "\n" + _plan_note
        return note, 0

    # ==================== 上下文压缩：#compress <摘要> ====================
    # 删除此前所有对话/工具历史，只保留 system + 摘要（含 plan/运行任务/子agent 信息），返回给模型。
    if cmd_str.startswith("#compress"):
        summary = cmd_str[len("#compress"):].strip()
        if not summary:
            return _json_err("#compress 需要提供摘要内容（用户需求/进度/重要信息/下一步/关键文件路径）"), -1
        return _do_compress(ctx, summary)

    # ==================== 任务计划：#plan（模型记录本回合任务并逐项打勾） ====================
    if cmd_str.startswith("#plan"):
        sid2 = ctx.get("session_id")
        emit_fn = ctx.get("emit")

        def _get_plan():
            # 内存优先，其次从 plans.json 恢复（会话重建/进程重启后不丢失）
            return _plan_get(sid2)

        def _publish_plan(plan):
            # 内存 + plans.json 双写（★ plan 持久化）
            _plan_put(sid2, plan)
            if emit_fn:
                emit_fn({"type": "plan_update", "plan": plan})

        first = cmd_str.split("\n", 1)[0].strip()
        parts = first.split()
        action = parts[1] if len(parts) > 1 else "view"
        if action == "new":
            title = first[len("#plan new"):].strip() or "任务计划"
            steps = []
            for line in cmd_str.partition("\n")[2].splitlines():
                line = line.strip()
                if not line:
                    continue
                line = re.sub(r'^[-*•\d\.\)\s]+', '', line).strip()
                if line:
                    steps.append({"text": line, "status": "pending"})
            if not steps:
                return _json_err("#plan new 需要至少一个步骤（每行一个，可用 - 开头）"), -1
            plan = {"title": title, "steps": steps, "updated_at": time.time()}
            _publish_plan(plan)
            summary = "\n".join(f"[ ] {i+1}. {s['text']}" for i, s in enumerate(steps))
            return f"📋 任务计划已创建：{title}\n{summary}", 0
        if action == "step":
            if len(parts) < 3:
                return _json_err("#plan step 语法：#plan step <序号> done|doing|failed|pending"), -1
            try:
                idx = int(parts[2]) - 1
            except Exception:
                return _json_err(f"步骤序号无效：{parts[2]}"), -1
            status = parts[3] if len(parts) > 3 else "done"
            if status not in ("done", "doing", "failed", "pending"):
                return _json_err(f"未知状态 '{status}'，可选 done / doing / failed / pending"), -1
            plan = _get_plan()
            if not plan:
                return _json_err("尚无任务计划，请先 #plan new 创建"), -1
            if idx < 0 or idx >= len(plan["steps"]):
                return _json_err(f"步骤序号超出范围（1-{len(plan['steps'])}）"), -1
            plan["steps"][idx]["status"] = status
            plan["updated_at"] = time.time()
            _publish_plan(plan)
            mark = {"done": "[x]", "doing": "[>]", "failed": "[!]", "pending": "[ ]"}.get(status, "[ ]")
            return f"{mark} 步骤 {idx+1} 已标记为 {status}：{plan['steps'][idx]['text']}", 0
        if action == "done":
            plan = _get_plan()
            if not plan:
                return _json_err("尚无任务计划"), -1
            for s in plan["steps"]:
                s["status"] = "done"
            plan["updated_at"] = time.time()
            _publish_plan(plan)
            return f"🎉 任务计划全部完成：{plan['title']}", 0
        if action == "clear":
            _publish_plan(None)
            return "🗑️ 任务计划已清空", 0
        # view / 其它
        plan = _get_plan()
        if not plan:
            return "（当前没有任务计划）", 0
        lines2 = [f"📋 {plan['title']}"]
        for i, s in enumerate(plan["steps"]):
            mark = {"done": "[x]", "doing": "[>]", "failed": "[!]", "pending": "[ ]"}.get(s.get("status"), "[ ]")
            lines2.append(f"{mark} {i+1}. {s['text']}")
        return "\n".join(lines2), 0

    return None

CHILD_SYSTEM_PROMPT = """## Language Policy
- **Internal reasoning**: Use English for all chain-of-thought, planning, and tool-call reasoning. English reasoning has stronger training support and produces more stable formatting (fewer punctuation-collapse artifacts).
- **Final answer to user**: If the user's message is in Chinese, respond in Simplified Chinese. Otherwise respond in the user's language.

You are a DeepSeek Sub-Agent (worker), created by a parent Agent to complete the tasks it assigns you in an isolated context. Your working directory is workspace. Context length is 1M, maximum output length is 384K.

## Role and Communication Rules
- The parent Agent cannot see your reasoning or your tool calls; it only receives what you actively send back:
  1. Milestones / interim results: call the `return_result` tool and the parent Agent is notified immediately; you may call it several times during a task to keep the parent Agent updated on interim results.
  2. Final answer: simply output the complete, confident final answer text and the parent Agent receives it automatically.
- Do not use "#return" in the body of the final answer.
- You cannot create or manage other agents; stay focused on completing your own task.
- The parent Agent may append new messages while you are running or after you finish (you will receive them as new user messages); incorporate the new information, keep reasoning, and update your results.

## Output Standards
- Reason in English; if the user asks in Chinese, reply in Chinese.
- Use Markdown to improve readability.
- Wrap LaTeX math in $...$ or $$...$$; do not use \\(...\\) or \\[...\\].
- **Bold formatting**: when using `**text**`, make sure the first and last characters inside the `**` are **not punctuation**.
- When you need the user to open or use a local file directly, use the format `[文件: 完整路径]` in the final answer; do not reference local files with `[文件](路径)`.

## Multi-Turn Strategy
- For complex tasks, first produce a step plan, then execute and verify step by step, instead of generating all files in one shot.
- If the context has already accumulated many turns, summarize the completed progress and the next steps concisely in the user's language.
- You must see the task through to the end; never give up midway.

## Async Tasks and Sub-Agent Collaboration Protocol
- After you start `#timer`, the system automatically pushes task status or return information into your context whenever it changes, and wakes you up to continue. You **do not need to poll**.
- If you have no other essential work right after starting it, output an interim answer (for example: "Background task started; it should finish in about X minutes and I will continue once it does.") and end the turn. Scheduled tasks are only pushed when you are idle.
- **No busy-wait polling**: while waiting for an async task, **do not repeatedly call `#process` to check status**. Trust the system notifications and save tokens and time.
- Note: you do not have the task plan (#plan) or sub-agent management (#spawn_agent, etc.) tools; the parent agent maintains the task plan on your behalf, so do not try to create a plan or create other agents.

## Note the Difference Between Async Execution and Scheduled Monitoring
- `execute_command` with `async_mode=True`: this only starts a background process; **the system will not notify you of its output automatically**. If you use it, you must poll the status yourself with `query_task`, and do not output the final answer until you have confirmed the task is complete.
- `start_timer`: starts a **scheduled monitoring task with automatic notifications**. The system pushes a message to you when the task produces output or completes. **Only with `start_timer` can you output an interim answer and end the turn immediately after starting it**.
- If you need to wait for a long-running task and want progress delivered automatically, be sure to use `start_timer` rather than `async_mode`.

## Screen Operations
- By default you operate on a hidden virtual desktop (so the user is not disturbed); pass `real=true` to screen tools when you need to operate the real desktop. The user can watch your actions live through the "AI 屏幕监控" panel in the web UI.
- **Important: windows opened on the virtual desktop (browsers, editors, programs, etc.) must be closed promptly once you are done with them** — close the whole virtual desktop with `virtual_desktop`'s `close`, or close the windows opened inside it; never leave them behind. The user may be deleting or moving files at the same time, and windows you leave behind lock files or ports, so the user cannot delete files and programs misbehave.

## Safety and Basic Rules
- The dangerous-command blacklist is enforced by the system; do not generate such commands yourself.
- Avoid `sudo`.
- For file operations, create a full backup and keep backup directories under `.backups/`. Delete only after the user confirms.
- You may chain commands with `&&` or `;`. Synchronous commands time out after 10 minutes and are terminated if they produce no output for more than 30 seconds; async commands and scheduled tasks have no time limit.
- **Protect the agent processes**: the locally running `app.py` / `agent.py` / `agent.exe` are the server / client agent processes and are part of this system's infrastructure. **Never kill / taskkill / terminate them**, and do not perform any dangerous operation on them (such as deleting files or rewriting code). Even when the user asks to clean up processes, exclude these.
- **Never start background processes with `start`**. For background execution use `execute_command` with `async_mode=true` (equivalent to `#exec --async`); it is managed by the system and has its own pipe and status query.
  - Why: `start` makes the new process **inherit the parent command's stdout pipe handle**. The parent command exited long ago, but the new process keeps holding the write end of the pipe, so the reader never sees EOF — which shows up as "the command looks finished, yet this turn hangs / the UI is unresponsive for a long time".
  - If there is a genuine special reason to use `start`, **the new process's stdin/stdout/stderr must all be redirected**: `start "" /B /min cmd /c "program > output_file 2>&1 < nul"` — none of the three may be omitted (especially `< nul`).
- **Do not chain long waits inside a single synchronous command**. For example `ping -n 25 127.0.0.1 >nul` produces no output for 24 seconds, which is bound to trigger the 30-second idle termination — you would be ruining the command yourself. To wait for later results, use `start_timer` for scheduled monitoring, or `async_mode` + `query_task` polling.
- **Commands expected to run longer than 90 seconds must go through async mode or scheduled monitoring; synchronous execution is forbidden**:
  - Use `execute_command` with `async_mode=true` (that is, `#exec --async`), then check progress with `query_task`;
  - or use `start_timer` directly so it notifies you automatically on completion.
  - Why: synchronous execution has a hard limit (about {max_timeout_min} minutes on the client), **and any single gap of more than 30 seconds without output is terminated immediately**. With synchronous mode, long tasks very easily end in "command result lost" or "command produced no output for a long time and was terminated" — while the task is in fact still running in the background, so you waited for nothing and the user never sees the result.
  - How to judge: if the script/command contains `sleep`, a wait loop, a download, a build, model inference, etc., or you estimate it will take more than 90 seconds — always go async.

## Handling Long Tool Output
- When a tool (execute_command, run_script, fetch_webpage, etc.) returns more than 4000 characters, the full output is saved automatically to a file under the `workspace/tool_outputs/` directory, and the tool result gives you that file's path.
- Do not draw conclusions from the truncated preview alone: to analyze the full content, read that file with `execute_command` (such as `type <path>` / `findstr`) or `run_script` and keep analyzing.

## Context Management (Save Token Costs)
- The context window is 1M tokens. When you receive a "上下文占用警告" (usage ≥50%), or you judge that a major stage is complete and earlier details no longer need to be kept, call the `compress_context` tool to compress the context proactively.
- The summary must include: ① user requirements ② completed progress ③ important information gathered ④ what to do next ⑤ a list of paths to key files.
- Compression deletes all previous conversation and tool history and keeps only: the system prompt, the stage summary, and the running tasks you started yourself (commands and IDs). The parent agent can still reach you via #prompt.
- After compression, continue the task based on the summary and do not redo completed work.
"""

def _emit(session_id, evt):
    """把事件放入会话共享事件队列（父/子 agent 共用的同一条 HTTP 连接）。"""
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess and sess.get("event_queue") is not None:
            try:
                sess["event_queue"].put(evt)
            except Exception:
                pass

@app.route('/api/command_result', methods=['POST'])
def command_result():
    data = request.json
    sid = data.get('session_id')
    aid = data.get('agent_id')
    task_id = data.get('task_id')
    output = data.get('output', '')
    returncode = data.get('returncode', -1)

    if not sid or not task_id:
        return jsonify({"error": "缺少 session_id 或 task_id"}), 400

    # 子 agent 命令结果：路由到对应 agent 的 command_events
    if aid:
        with agents_lock:
            a = agents.get(aid)
            if not a or a.get("parent_sid") != sid:
                return jsonify({"error": "子 agent 不存在"}), 404
            events = a.get("command_events", {})
            if task_id in events:
                event, holder = events[task_id]
                holder["output"] = output
                holder["returncode"] = returncode
                event.set()
                # ★ 上下文管理：记录定时/异步任务的客户端 ID（供 #compress 保留）
                _match_task_client_id(a, task_id, output)
                return jsonify({"success": True})
            return jsonify({"error": "命令ID不存在或已处理"}), 404

    with sessions_lock:
        sess = sessions.get(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        events = sess.get("command_events", {})
        if task_id in events:
            event, holder = events[task_id]
            holder["output"] = output
            holder["returncode"] = returncode
            event.set()
            # ★ 上下文管理：记录定时/异步任务的客户端 ID（供 #compress 保留）
            _match_task_client_id(sess, task_id, output)
            return jsonify({"success": True})
        else:
            return jsonify({"error": "命令ID不存在或已处理"}), 404

@app.route('/api/timer_notify', methods=['POST'])
def timer_notify():
    data = request.json
    session_id = data.get('session_id')
    task_id = data.get('task_id')
    output = data.get('output', '')
    status = data.get('status', 'running')
    if not session_id:
        return jsonify({"error": "缺少 session_id"}), 400
    # 过大的输出：客户端 agent 已就地落盘（输出本身含预览+路径），此处仅兜底截断
    if len(output) > 4000:
        output = output[:4000] + f"\n…（截断，原长度 {len(output)}）"

    # ★ BUGFIX(2026-10-04)：定时任务的输出必须**只推增量**。
    #
    #   问题：客户端 AsyncTask.get_status() 每次返回的都是「从任务开始到现在的全部输出」
    #   （b''.join(self.output)），于是第 N 次推送会把前 N-1 次的输出**原样重发一遍**：
    #       第1次: [启动] + [进度 1/8]
    #       第2次: [启动] + [进度 1/8] + [进度 2/8]
    #       第3次: … 越来越长
    #   后果有两个：
    #     ① 消息区被重复的大段落淹没，真正的新内容（[进度 N/8] 一行）肉眼注意不到 ——
    #        用户反馈「前端没看到定时任务返回」；
    #     ② 模型上下文被同一段内容反复塞入，浪费 token 且容易误判进度。
    #
    #   修法（放在服务端，不必重新打包客户端）：按任务记住「上次推过的全量输出」，
    #   本次只把**多出来的尾巴**推给前端与模型。判断不了就退回全量，绝不丢信息。
    _prev_key = f"_timer_sent_{task_id}"
    _prev_out = ""
    with sessions_lock:
        _s0 = sessions.get(session_id)
        if _s0 is not None:
            _prev_out = str(_s0.get(_prev_key) or "")
    _delta = None
    if _prev_out and output.startswith(_prev_out):
        _delta = output[len(_prev_out):]
    elif not _prev_out:
        _delta = output
    # 记录本次全量（下次据此取增量）；用**截断前**的原始全量更稳
    with sessions_lock:
        _s1 = sessions.get(session_id)
        if _s1 is not None:
            _s1[_prev_key] = output
    if _delta is not None:
        _shown = _delta.strip() or "（无新输出）"
    else:
        # 无法判定增量（例如输出被截断/落盘改写过）：退回全量，保证不丢信息
        _shown = output
    message = {"text": f"[TIMER] 任务 {task_id} 状态更新：{status}\n输出：{_shown}", "kind": "timer"}
    aid = data.get('agent_id')
    # ★ 上下文管理：定时任务结束时更新 active_tasks 状态（供 #compress 区分运行中任务）
    def _mark_task_done(holder):
        if status in ("finished", "killed", "failed") and holder.get("active_tasks"):
            for tid, t in list(holder["active_tasks"].items()):
                if t.get("client_id") == task_id or t.get("task_id") == task_id:
                    t["status"] = status
                    logging.info(f"[COMPRESS] 任务 {tid}（客户端 {task_id}）状态更新为 {status}")
    # 子 agent 的定时任务通知：推送给子 agent 自身（子 agent 只有一个队列，保持原行为）
    if aid:
        with agents_lock:
            a = agents.get(aid)
            if a and a.get("parent_sid") == session_id:
                _mark_task_done(a)
                if 'pending_notifications' not in a:
                    a['pending_notifications'] = []
                a['pending_notifications'].append(message)
                evt = a.get('wait_event')
                if evt:
                    evt.set()
                logging.info(f"Timer 通知加入子 agent 队列: {aid}, session: {session_id}")
                return jsonify({"success": True})
            return jsonify({"error": "子 agent 不存在"}), 404
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess:
            _mark_task_done(sess)
            # ★ 2026-09-29：父会话的定时任务通知按「消息投递方式」设置分流
            #   注意传字符串（message 是 {"text","kind"} 字典，直接传会让下游拿 dict 去拼字符串）
            _enqueue_notification(sess, message.get("text") if isinstance(message, dict) else message,
                                  (message.get("kind") if isinstance(message, dict) else None) or "timer")
            logging.info(f"Timer 通知已投递，session: {session_id}")
            return jsonify({"success": True})
        else:
            return jsonify({"error": "会话不存在"}), 404

@app.route('/api/session_export_stats', methods=['GET', 'POST'])
def session_export_stats():
    """导出操作记录时调用：把「会话属性」里那些统计一并写进 JSON，
    这样导出→导入之后标题、按模型分开的用量、时间统计都不会归零。
    没调用过模型时也返回一份空壳，保证字段结构稳定。"""
    sid = request.args.get('session_id') or (request.get_json(silent=True) or {}).get('session_id')
    if not sid:
        return jsonify({"success": False, "error": "缺少 session_id"}), 400
    snap = _sess_stats_snapshot(sid)
    if snap is None:
        # 会话不存在（例如页面刷新后已回收）：返回空壳，前端照写不误
        snap = _sess_stats_init()
        snap.pop("created_at", None)
    return jsonify({"success": True, "stats": snap})


@app.route('/api/session_info', methods=['GET', 'POST'])
def session_info():
    """会话属性（操作模式「会话」按钮的面板数据）。

    GET/POST 参数：session_id
    返回：
      title        当前标题（用户自定义优先，空则由前端用自动总结填充）
      auto_title   服务端按首条用户提问生成的建议标题
      timing       {llm_ms, tool_ms, ask_wait_ms, busy_ms, first_token_avg_ms, output_tps}
      usage_by_model  {模型名: {prompt_tokens, cached_tokens, completion_tokens, calls}}
                    ★ 按模型分开累计（修复“中途 Pro→Flash 导致金额按 Flash 单价算”）
      context      {system, tools, user, llm, tool, total} 上下文组成部分（估算 tokens）
      ctx_tokens   最近一次真实调用的输入 token（API usage，最准）
      ctx_window   上下文窗口大小
      turns        完成轮次（final_answer 次数）、子代理数等
    """
    sid = request.args.get('session_id') or (request.get_json(silent=True) or {}).get('session_id')
    if not sid:
        return jsonify({"success": False, "error": "缺少 session_id"}), 400
    with sessions_lock:
        sess = sessions.get(sid)
        if not sess:
            return jsonify({"success": False, "error": "会话不存在"}), 404
        st = _sess_stats(sid) or _sess_stats_init()
        by_model = json.loads(json.dumps(st.get("by_model") or {}, ensure_ascii=False))
        hist = dict(st.get("hist_usage") or {})
        llm_ms = float(st.get("llm_ms") or 0)
        tool_ms = float(st.get("tool_ms") or 0)
        ask_wait_ms = float(st.get("ask_wait_ms") or 0)
        ft_sum = float(st.get("first_token_ms_sum") or 0)
        ft_cnt = int(st.get("first_token_count") or 0)
        out_ms = float(st.get("output_ms") or 0)
        gen_ms = float(st.get("gen_ms") or 0)
        out_tokens = int(st.get("output_tokens") or 0)
        title_user = str(st.get("title") or "")
        internal = list(sess.get("internal_ref") or [])
        # ★ 父循环可能还没跑起来（刚导入、还没发消息）→ 退回用「导入注入的历史上下文」估算，
        #   这样用户一导入就能在面板里看到上下文组成，而不是一片 0。
        if not internal:
            _inj = sess.get("injected_context")
            if isinstance(_inj, list) and _inj:
                internal = [{"role": "system", "content": build_agent_system(dict(sess.get("settings") or {}))}] + list(_inj)
        model = sess.get("model") or ""
        usage = dict(sess.get("usage_ref") or {})
        settings = dict(sess.get("settings") or {})
        live_children = int(sess.get("live_children") or 0)
    # 上下文组成部分：从父循环的 internal 取（若还没建立则给空）
    breakdown = _split_context_breakdown(internal, build_parent_tools(settings)) if internal else {
        "system": 0, "tools": 0, "user": 0, "llm": 0, "tool": 0, "total": 0}
    auto_title = ""
    try:
        for m in internal:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    auto_title = _summarize_title(c)
                    break
    except Exception:
        auto_title = ""
    busy_ms = llm_ms + tool_ms
    # 输出速度：三档口径，优先精确
    if gen_ms > 0 and out_tokens > 0:
        _tps = round(out_tokens / (gen_ms / 1000.0), 2)
        _tps_basis = "gen"        # 整体生成窗口（首块→末块，含思考）：准确
    elif llm_ms > 0 and out_tokens > 0:
        _tps = round(out_tokens / (llm_ms / 1000.0), 2)
        _tps_basis = "llm"        # 整次调用耗时（含首 token 等待）：偏保守
    elif out_ms > 0 and out_tokens > 0:
        _tps = round(out_tokens / (out_ms / 1000.0), 2)
        _tps_basis = "content"    # 旧记录：只覆盖正文窗口，偏大，仅作最后兜底
    else:
        _tps, _tps_basis = 0, "none"
    return jsonify({
        "success": True,
        "session_id": sid,
        "model": model,
        "title": title_user,
        "auto_title": auto_title,
        "timing": {
            "llm_ms": round(llm_ms),
            "tool_ms": round(tool_ms),
            "ask_wait_ms": round(ask_wait_ms),
            "busy_ms": round(busy_ms),
            "first_token_avg_ms": round(ft_sum / ft_cnt) if ft_cnt else 0,
            "first_token_count": ft_cnt,
            # ★ BUGFIX(2026-09-21)：输出速度的分子分母必须同口径。
            #   output_tokens 用的是**全部** completion_tokens（含思考阶段），
            #   而旧口径 output_ms 只统计「正文首块→正文末块」；思考很长、正文很短时
            #   会算出离谱的速度（用户那份记录实测 41779 tok / 7.66 s = 5451 tok/s）。
            #   优先级：gen_ms（首块→末块，含思考，精确）> llm_ms（整次调用耗时，保守）> output_ms（兜底）。
            "output_tps": _tps,
            "tps_basis": _tps_basis,
            "gen_ms": round(gen_ms),
            "output_ms": round(out_ms),
            "output_tokens": out_tokens,
        },
        "usage_by_model": by_model,
        "web_tools": (lambda _w: {
            "search": {"calls": int((_w.get("search") or {}).get("calls") or 0),
                       "ms": int((_w.get("search") or {}).get("ms") or 0)},
            "fetch": {"calls": int((_w.get("fetch") or {}).get("calls") or 0),
                      "ms": int((_w.get("fetch") or {}).get("ms") or 0)},
        })(st.get("web_tools") if isinstance(st.get("web_tools"), dict) else {}),
        # ★ 2026-09-21：导入时恢复的历史用量 + 本会话新增 = 面板要显示的“总量”。
        #   否则导出→导入→再发一条消息后，面板只显示本次会话那一点点，看起来“用量变少了”。
        "hist_usage": hist,
        "usage_total_by_model": _merge_usage_with_history(by_model, hist),
        "context": breakdown,
        "ctx_tokens": int(usage.get("ctx_tokens") or 0),
        "ctx_window": CTX_WINDOW_TOKENS,
        "live_children": live_children,
    })


@app.route('/api/session_title', methods=['POST'])
def session_title_set():
    """设置/清除会话自定义标题（导出文件名与面板标题都用它）。空字符串 = 清除，回到自动标题。"""
    d = request.get_json(silent=True) or {}
    sid = d.get('session_id')
    if not sid:
        return jsonify({"success": False, "error": "缺少 session_id"}), 400
    title = str(d.get('title') or "").strip()[:120]
    with sessions_lock:
        sess = sessions.get(sid)
        if not sess:
            return jsonify({"success": False, "error": "会话不存在"}), 404
        st = _sess_stats(sid)
        if st is not None:
            st["title"] = title
    return jsonify({"success": True, "title": title})


def _summarize_title(text, max_len=24):
    """把一段文本压成简短标题（导出文件名/会话标题的“自动总结”）。"""
    s = re.sub(r'\s+', ' ', str(text or '')).strip()
    s = re.sub(r'[#*`>_~\[\]()]', '', s)
    s = re.sub(r'^[-—•\s]+', '', s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return s


@app.route('/api/ask_user_answer', methods=['POST'])
def ask_user_answer():
    """用户对 ask_user 问题的作答：
    同步询问：唤醒父循环中阻塞等待的工具调用（答案直接作为工具结果）；
    异步询问：加入 pending_notifications，模型空闲时以“新通知”形式送达。"""
    data = request.json or {}
    sid = data.get('session_id')
    qid = data.get('question_id')
    answer = str(data.get('answer') or '').strip()
    question = str(data.get('question') or '').strip()
    is_async = bool(data.get('async'))
    if not sid or not qid or not answer:
        return jsonify({"error": "缺少 session_id / question_id / answer"}), 400
    with sessions_lock:
        sess = sessions.get(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        if is_async:
            # ★ 2026-09-29：异步询问的作答按「消息投递方式」设置分流
            _enqueue_notification(sess, f"[ASK_USER] 用户回答了你提问的问题「{question}」：{answer}",
                                  "ask_user")
        else:
            events = sess.get('command_events', {})
            if qid not in events:
                return jsonify({"error": "问题不存在或已处理"}), 404
            event, holder = events[qid]
            holder["output"] = answer
            holder["returncode"] = 0
            event.set()
    # 事件广播（界面更新/导出回放用；同步时工具结果事件由父循环另行发出）
    _emit(sid, {"type": "ask_user_answered", "task_id": qid, "question": question,
                "answer": answer, "async": is_async})
    return jsonify({"success": True})

@app.route('/api/diag/threads')
def diag_threads():
    """★ 2026-09-28：线程栈转储（排查“操作模式全部卡死”这类问题）。

    为什么需要它：像“持锁再获取同一把锁”造成的自锁死，表现为
    「操作模式所有请求永久无响应，但对话模式正常」，且服务器日志里
    连请求都看不到（请求还没进入视图就卡在锁上），只看日志完全无法定位。
    有了这个接口，卡死时直接访问 /api/diag/threads 就能看到每个线程卡在哪一行。
    """
    import traceback as _tb
    out = []
    frames = sys._current_frames()
    for th in threading.enumerate():
        if th.ident not in frames:
            continue
        st = _tb.extract_stack(frames[th.ident])
        out.append({
            "thread": th.name,
            "daemon": th.daemon,
            "stack": [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}()  {f.line}"
                      for f in st[-20:]],
        })
    try:
        sess_brief = []
        for k, v in list(sessions.items()):
            sess_brief.append({
                "sid": k,
                "parent_exited": v.get("parent_exited"),
                "parent_running": v.get("parent_running"),
                "live_children": v.get("live_children"),
                "busy": v.get("busy"),
                "qsize": v["event_queue"].qsize() if v.get("event_queue") else -1,
            })
    except Exception as e:
        sess_brief = [{"error": str(e)}]
    return jsonify({
        "threads": out,
        "locks": {"sessions_lock_locked": sessions_lock._is_owned() if hasattr(sessions_lock, "_is_owned") else None,
                  "agents_lock_locked": agents_lock._is_owned() if hasattr(agents_lock, "_is_owned") else None},
        "sessions": sess_brief,
        "agents": [{"id": k, "status": v.get("status"), "busy": v.get("busy")}
                   for k, v in list(agents.items())],
    })


@app.route('/api/session_alive', methods=['GET', 'POST'])
def session_alive():
    """★ 2026-10-03（方案A 配套）：探测某个操作模式会话是否还活着。

    场景：**后端重启、而前端页面一直开着**（刷新页面本来就没法救，这里不管）。
    页面开着意味着前端内存里还留着完整操作记录；但后端重启后内存会话全丢，
    前端若只靠「收到新 session_id」来判断，就要等到**第二条**消息才能补上历史。
    前端在**发消息前**用本接口确认一次：会话已不在 → 立刻用本地记录重建上下文，
    这样重启后发的第一条消息也带完整历史。

    返回：{"alive": bool, "busy": bool, "parent_running": bool}
    只回答"会话在不在"，不返回任何对话内容。
    """
    sid = (request.args.get('session_id')
           or (request.get_json(silent=True) or {}).get('session_id'))
    if not sid:
        return jsonify({"error": "缺少 session_id"}), 400
    with sessions_lock:
        sess = sessions.get(sid)
        if sess is None:
            return jsonify({"alive": False, "busy": False, "parent_running": False})
        return jsonify({
            "alive": True,
            "busy": bool(sess.get("busy")),
            "parent_running": bool(sess.get("parent_running")),
            "final_answered": bool(sess.get("final_answered")),
        })


# ★ 2026-10-05（用户要求「停止当前操作」按钮）：列出该会话正在进行的操作。
#
#   为什么需要它：以前只有「停止操作并清空操作记录」，而且它**不中断**异步任务与
#   定时任务（客户端 agent.py 里那些后台进程会继续跑）。新的「停止当前操作」按钮
#   要在停止前弹框让用户勾选"要不要连带停掉"，就得先知道有哪些在跑。
#
#   本接口**只读**，不改变任何状态、不碰消息流。
#   · 子代理：服务端 agents 表（status == running 且属于本会话）
#   · 异步/定时任务：会话的 active_tasks（status == running），带 client_id 供前端
#     直连客户端 POST {AGENT_URL}/kill/<client_id> 终止（客户端已有该端点）
@app.route('/api/running_operations', methods=['GET'])
def running_operations():
    sid = request.args.get('session_id')
    children, tasks = [], []
    if sid:
        try:
            with sessions_lock:
                sess = sessions.get(sid)
                _at = dict((sess or {}).get("active_tasks") or {})
            _kids = list_session_children(sid)
            for a in _kids:
                if a.get("status") == "running":
                    children.append({
                        "agent_id": a.get("id"),
                        "task": _truncate(a.get("task"), 120),
                        "model": model_to_alias(a.get("model")),
                    })
            # ★ 2026-10-05（用户实测）：**子代理自己**创建的异步/定时任务也要列出来。
            #   它们登记在子代理 dict 的 active_tasks 里，不在会话里 ——
            #   上一版只查会话，导致「子代理创建的异步/定时你没终止」。
            _owner = {a.get("id"): a for a in _kids}
            _all = [("parent", _at)] + [(aid, dict((a.get("active_tasks") or {})))
                                        for aid, a in _owner.items()]
            for _who, _tasks in _all:
                for tid, t in _tasks.items():
                    if (t or {}).get("status") == "running":
                        tasks.append({
                            "task_id": tid,
                            "type": t.get("type") or "async",       # timer / async
                            "command": (t.get("command") or "")[:200],
                            "client_id": t.get("client_id"),        # 客户端侧任务 ID（可能尚未回填）
                            "started_at": t.get("started_at"),
                            "owner": _who,                          # 'parent' 或子代理 id
                            "owner_is_child": _who != "parent",
                        })
        except Exception as e:
            logging.warning(f"[STOP] 查询运行中操作失败: {e}")
    return jsonify({"success": True, "children": children, "tasks": tasks,
                    "has_any": bool(children or tasks)})


@app.route('/api/cancel_operation', methods=['POST'])
def cancel_operation():
    data = request.json
    sid = data.get('session_id')
    ok = False
    with sessions_lock:
        if sid and sid in sessions:
            sess = sessions[sid]
            cancel_evt = sess.get("cancel_event")
            if cancel_evt:
                cancel_evt.set()
            # 唤醒等待中的流，使其退出
            evt = sess.get("wait_event")
            if evt:
                evt.set()
            # ★ 2026-10-05：打上"刚被用户停止"的时间戳。
            #   作用见事件流的存活判据 —— 停止后父循环会退出（parent_exited=True），
            #   若此时把流关掉，用户过一会儿再发消息时就没有连接接收回复了
            #   （实测：服务端回答正常生成、界面永远看不到）。
            sess["last_stop_at"] = time.time()
            ok = True
    # 同时销毁该会话的全部子 agent
    for a in list_session_children(sid):
        try:
            a["cancel_event"].set()
            evt = a.get("wait_event")
            if evt:
                evt.set()
        except Exception:
            pass
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "没有正在进行的操作"})

@app.route('/api/operation_input', methods=['POST'])
def operation_input():
    data = request.json
    sid = data.get('session_id')
    message = data.get('message')
    if not sid or not message:
        return jsonify({"error": "缺少 session_id 或 message"}), 400

    with sessions_lock:
        sess = sessions.get(sid)
        if not sess:
            return jsonify({"error": "会话不存在"}), 404
        # ★ 消息可附带图片（data_url 列表）与文档（name+content），父循环消费时注入 user 消息
        _imgs = [u for u in (data.get('images') or [])
                 if isinstance(u, str) and u.startswith('data:image/')]
        _files = [f for f in (data.get('files') or [])
                  if isinstance(f, dict) and f.get('name')]
        # ★ 2026-09-29：用户消息按「消息投递方式 → 用户消息」通道设置分流
        _mode = _enqueue(sess, {"text": message, "images": _imgs, "files": _files}, "user")
        logging.info(f"[OPERATE] {sid} 用户消息投递方式={_mode}"
                     f"（{'等空闲' if _mode == 'queue' else '下一轮立刻'}）")
        # ★ 2026-10-05（修「停止后同一会话发不出消息」）：
        #   用户点「停止生成」→ cancel_event 置位 → 父循环 break 退出，
        #   此时会话还在（流能重连、消息能入队），但**没有线程消费队列**，
        #   于是这条消息永远没人处理（实测：投递后毫无动静）。
        #   这里检测到父循环已不在运行且有可复用的 internal_ref → 立刻以
        #   「恢复模式」重启父循环：接手原来的 internal（上下文连续），
        #   跳过初始消息处理，直接进主循环消费刚入队的这条消息。
        _resume_needed = False
        _resume_internal = None
        _resume_model = _resume_r_eff = None
        _resume_think = None
        if not sess.get("parent_running", False):
            _ref = sess.get("internal_ref")
            if isinstance(_ref, list) and _ref:
                _resume_internal = _ref
                _resume_model = sess.get("model")
                _resume_r_eff = sess.get("reasoning_effort")
                _resume_think = sess.get("thinking_enabled")
                _resume_needed = True
            else:
                # ★ 2026-10-13（修「导入操作记录后发消息毫无反应」）：
                #   导入建出来的会话 parent_running=False，且历史放在 **injected_context**
                #   而不是 internal_ref。此时：
                #     · /chat 的纯重连分支不启动父循环（它假定父循环已在跑 —— 那是"停止后重连"的场景）；
                #     · 上面的 internal_ref 恢复分支也进不来（没有 internal_ref）。
                #   于是**没有任何路径启动这个从未启动过的父循环** →
                #   用户投递的消息进了队列却无人消费 → 实测「发 OK 毫无反应」，
                #   服务端该会话一行 [OPERATE] 日志都没有。
                #   run_parent_loop 开头会自己 pop("injected_context") 并当作 internal 历史，
                #   所以这里不带 resume_internal 启动，正好走"用导入历史初始化上下文"的正路。
                _ic = sess.get("injected_context")
                if isinstance(_ic, list) and _ic:
                    _resume_model = sess.get("model")
                    _resume_r_eff = sess.get("reasoning_effort")
                    _resume_think = sess.get("thinking_enabled")
                    _resume_needed = True
    if _resume_needed:
        with sessions_lock:
            _s2 = sessions.get(sid)
            if _s2 is not None:
                _s2["parent_exited"] = False
                _s2["parent_running"] = True
        logging.info(f"[OPERATE] {sid} 父循环未在运行 → 启动它消费刚投递的消息"
                     f"（{'接手 internal_ref' if _resume_internal else '用 injected_context 初始化'}）")
        threading.Thread(target=run_parent_loop,
                         args=(None, _resume_model, _resume_r_eff, _resume_think, sid),
                         kwargs={"resume_internal": _resume_internal},
                         daemon=True).start()
    with sessions_lock:
        sess = sessions.get(sid)
        if sess is None:
            return jsonify({"error": "会话已结束"}), 404
        # ★ FIX(2026-09-11)：模型选择随消息同步到会话。此前会话内切换 Flash/Pro 不生效，
        #   显示与计费不一致（界面显示 Flash、实际仍按会话创建时的模型计费）。
        if data.get('model'):
            sess['model'] = data['model']
            logging.info(f"[OPERATE] {sid} 模型切换为 {data['model']}")
        # ★ FIX(2026-09-11 v4)：思考强度 / 思考开关同样随消息同步（此前中途切换不生效）
        if data.get('reasoning_effort'):
            sess['reasoning_effort'] = data['reasoning_effort']
            logging.info(f"[OPERATE] {sid} 思考强度切换为 {data['reasoning_effort']}")
        if 'thinking_enabled' in data:
            sess['thinking_enabled'] = bool(data['thinking_enabled'])
            logging.info(f"[OPERATE] {sid} 思考开关切换为 {bool(data['thinking_enabled'])}")
        # ★ 操作模式设置（工具开关/超时等）可随消息即时更新，后续调用生效
        if data.get('settings'):
            sess['settings'] = data['settings']
        # 唤醒操作流
        evt = sess.get('wait_event')
        if evt:
            evt.set()
    return jsonify({"success": True})

# ---------- 操作模式导入导出 ----------
MAX_INJECTED_MESSAGE_CHARS = 300000

# ==================== 上下文压缩状态还原（#load / 操作记录导入共用） ====================
COMPRESS_MARK = "【上下文压缩】"   # _do_compress 生成的摘要消息以此开头

def _is_compress_snapshot_msg(m):
    """判断消息是否为“压缩完成后的摘要消息”（#compress 把 internal 重建为
    [system, user(摘要)]，其中 user 消息即压缩快照的起点）。"""
    return (isinstance(m, dict) and m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(COMPRESS_MARK))


def _find_compress_snapshot_index(msgs):
    """返回消息序列中第一条压缩摘要消息的下标；历史中未执行过 #compress 返回 -1。"""
    if not isinstance(msgs, list):
        return -1
    for i, m in enumerate(msgs):
        if _is_compress_snapshot_msg(m):
            return i
    return -1


def _restore_compressed_context(ctx, loaded):
    """★ 压缩状态自动还原：#load 加载记忆 / operation_import 导入历史时，
    若消息序列中曾执行过 #compress（含“【上下文压缩】”摘要消息），自动恢复到
    “压缩完成后”的状态：丢弃摘要之前的压缩前完整历史，只保留压缩快照
    （摘要消息 + 其后压缩新对话），避免“压缩前完整历史 + 压缩摘要”混合体。

    - loaded：待注入的消息序列（已 _clean_injected_messages 清洗）
    - ctx：包含 internal/session_id/usage/agent 的上下文（operation_import 传 None 时仅做截断）
    返回：找到压缩标记时返回压缩快照列表（并重建 ctx.internal）；否则返回 None（调用方按原追加逻辑处理）。"""
    idx = _find_compress_snapshot_index(loaded)
    if idx < 0:
        return None
    snapshot = loaded[idx:]   # 压缩快照 = 摘要消息及之后的压缩后对话
    internal = ctx.get("internal") if isinstance(ctx, dict) else None
    if isinstance(internal, list) and internal:
        # 当前 internal 可能也积累了大量压缩前历史：一并裁剪（重放压缩逻辑）。
        # 保留 system 消息 + 当前挂起的工具轮（最后一个带 tool_calls 的 assistant
        # 及其后续 tool 响应），避免 tool 消息孤立导致 API 400。
        sys_msgs = [m for m in internal if isinstance(m, dict) and m.get("role") == "system"]
        keep = len(internal)
        for k in range(len(internal) - 1, -1, -1):
            m = internal[k]
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
                keep = k
                break
        # ★ 2026-10-21（**修「#load 后模型看不到内容/答非所问」**）：
        #   上面按 tool_calls 定位的 keep，会把**当前用户的那句提问**一起丢掉 ——
        #   实测加载前 internal 里有「请把记忆里的暗号告诉我」，加载后整条消失，
        #   模型于是不知道自己为什么要加载这条记忆。
        #   正确做法：把起点再往前退到**最近的一条 user 消息**，
        #   这样 tail = [当前提问, assistant(tool_calls), 后续 tool 响应]，
        #   提问与工具轮都保住，且 tool_calls 与 tool 响应仍紧邻（API 不会 400）。
        for k in range(keep - 1, -1, -1):
            m = internal[k]
            if isinstance(m, dict) and m.get("role") == "user":
                keep = k
                break
        tail = internal[keep:]
        internal[:] = sys_msgs + snapshot + tail
        # 与 _do_compress 一致：复位上下文警告标志、清空占用计数
        usage = ctx.get("usage")
        if usage is not None:
            usage["ctx_tokens"] = 0
        agent = ctx.get("agent")
        if agent is not None:
            with agents_lock:
                agent["ctx_warned"] = False
                agent["ctx_warn_last_ratio"] = 0
        else:
            sid = ctx.get("session_id")
            if sid:
                with sessions_lock:
                    sess = sessions.get(sid)
                    if sess:
                        sess["ctx_warned"] = False
                        sess["ctx_warn_last_ratio"] = 0
    return snapshot


def _clean_injected_messages(messages):
    """净化导入的历史消息，保证是 API 可接受格式：
    - 只保留 role 为 user/assistant/tool 的消息（system 由服务器自己的提示词取代）
    - assistant 的 tool_calls 必须与紧随其后的 tool 消息配对：缺失的调用补占位 tool 响应
      （否则 API 报 "insufficient tool messages following tool_calls"）
    - 孤立的 tool 消息（前一条 assistant 没有 tool_calls）用合成 assistant 承载，
      不丢弃信息且格式合法（否则 API 报孤立 tool 消息错误）
    - 超长内容截断
    返回 None 表示没有可用消息。
    """
    if not isinstance(messages, list):
        return None
    # ★ 2026-10-06（修「导出再导入后上下文暴涨」）：
    #   导入走的是本函数（不是"结构化重建"那条路），所以裁剪必须加在这里。
    #   实测：模型把整份记忆全文塞进 #mem_save 的参数（单条 48 万字符），
    #   导出→导入后上下文从 3 万涨到 54 万 tokens。这里先裁掉超大参数。
    try:
        _n = _cap_oversized_messages(messages)
        if _n:
            logging.info(f"[OPERATE] 导入历史：已裁剪 {_n} 处超大工具参数"
                         f"（单条上限 {_TOOL_ARG_CHAR_LIMIT} 字符，避免上下文暴涨）")
    except Exception as _ce:
        logging.warning(f"[OPERATE] 裁剪超大工具参数失败（不影响导入）: {_ce}")
    out = []
    pending_call_ids = []   # 当前 assistant 尚未被 tool 消息消费的 tool_call_id 列表
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        content = m.get("content")
        # ★ 2026-10-09（修改一·要求5）：多模态 user 消息的 content 是**内容块数组**
        #   （text + image_url）。这里绝不能 str(content) —— 那会把数组变成一段
        #   Python repr 字符串，图片连同文本一起作废（实测导入后模型完全看不到图）。
        #   数组按块清洗后原样保留（只截断 text 块、丢掉非 dict 块）。
        if isinstance(content, list):
            _blocks = []
            for _b in content:
                if not isinstance(_b, dict):
                    continue
                if _b.get("type") == "text":
                    _blocks.append({"type": "text",
                                    "text": str(_b.get("text") or "")[:MAX_INJECTED_MESSAGE_CHARS]})
                elif _b.get("type") == "image_url":
                    _u = _b.get("image_url")
                    _url = _u.get("url") if isinstance(_u, dict) else _u
                    if not (isinstance(_url, str) and _url):
                        continue
                    # ★ 前端导出记录里存的是 /api/image/<file> 这种**本机 URL**，
                    #   而 API 厂商取不到 localhost 的地址（模型会直接看不到图）。
                    #   这里在服务端就把它读回转成 data_url —— 图片就在 uploads/ 里，
                    #   一次本地读盘即可，不依赖外网。
                    _m = re.search(r'/api/image/([^/?#]+)', _url)
                    if _m:
                        _du = _image_to_data_url(_m.group(1))
                        if _du:
                            _url = _du
                        else:
                            # 文件不在本机（跨机器导入）：保留 URL，让模型侧自然忽略
                            logging.warning(f"[OPERATE] 导入的图片 {_m.group(1)} 不在本机 uploads/，无法转 data_url")
                    _blocks.append({"type": "image_url", "image_url": {"url": _url}})
            content = _blocks if _blocks else ""
        else:
            content = "" if content is None else str(content)[:MAX_INJECTED_MESSAGE_CHARS]

        if role == "tool":
            if not pending_call_ids:
                # 孤立 tool 消息：合成一个 assistant 承载（保留信息且格式合法）
                host_id = "call_orphan_" + uuid.uuid4().hex[:8]
                out.append({"role": "assistant", "content": None,
                            # ★ FIX(400 reasoning_content)：thinking 模式历史消息必须带该字段
                            "reasoning_content": "",
                            "tool_calls": [{"id": host_id, "type": "function",
                                            "function": {"name": "execute_command", "arguments": "{}"}}]})
                pending_call_ids = [host_id]
            call_id = str(m.get("tool_call_id") or pending_call_ids[0])
            if call_id not in pending_call_ids:
                # 历史错位：改写为当前 assistant 第一个等待的 id，保证配对合法
                call_id = pending_call_ids[0]
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": content[:MAX_INJECTED_MESSAGE_CHARS]})
            if call_id in pending_call_ids:
                pending_call_ids.remove(call_id)
            continue

        # role 为 user / assistant：新消息到来前，先补齐上一 assistant 未闭合的 tool_calls
        if pending_call_ids:
            for cid in pending_call_ids:
                out.append({"role": "tool", "tool_call_id": cid,
                            "content": "(无结果：该工具调用未收到返回，可能已取消)"})
            pending_call_ids = []

        if role == "user":
            out.append({"role": "user", "content": content})
            continue

        # role == assistant
        nm = {"role": "assistant", "content": content}
        reasoning = m.get("reasoning_content")
        if reasoning:
            nm["reasoning_content"] = str(reasoning)[:MAX_INJECTED_MESSAGE_CHARS]
        else:
            # ★ FIX(400 reasoning_content)：thinking 模式会话中 assistant 消息必须携带
            #   reasoning_content 字段（可为空串），否则 DeepSeek API 报
            #   "The 'reasoning_content' in the thinking mode must be passed back to the API."
            nm["reasoning_content"] = ""
        tcs = m.get("tool_calls")
        if isinstance(tcs, list):
            cleaned = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not name:
                    continue
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args, ensure_ascii=False)
                elif args is None:
                    args = ""
                cleaned.append({
                    "id": str(tc.get("id") or ("call_imported_" + uuid.uuid4().hex[:8])),
                    "type": "function",
                    "function": {"name": str(name), "arguments": str(args)}
                })
            if cleaned:
                nm["tool_calls"] = cleaned
                if not content:
                    nm["content"] = None
                pending_call_ids = [c["id"] for c in cleaned]
        if nm.get("content") is None and not nm.get("tool_calls"):
            nm["content"] = ""
        out.append(nm)

    # 收尾：补齐最后一条 assistant 未闭合的 tool_calls
    if pending_call_ids:
        for cid in pending_call_ids:
            out.append({"role": "tool", "tool_call_id": cid,
                        "content": "(无结果：该工具调用未收到返回，可能已取消)"})
        pending_call_ids = []

    if not out:
        return None
    # 若最后一条是 tool 消息（模型尚未答复），补一条 assistant 占位答复，避免 API 报错
    if out[-1]["role"] == "tool":
        out.append({"role": "assistant", "content": "（历史导入：工具已执行，模型当时未继续输出。）",
                    # ★ FIX(400 reasoning_content)：thinking 模式历史消息必须带该字段
                    "reasoning_content": ""})
    return out

@app.route('/api/operation_import', methods=['POST'])
def operation_import():
    """操作模式导入：创建新会话并把导入的完整历史（用户提问/思考/工具调用/结果/
    最终答复/子代理记录）注入服务器上下文。前端随后通过 /chat 复用该 session_id
    启动流，run_parent_loop 会用注入的历史初始化内部上下文。"""
    data = request.get_json(silent=True) or {}
    messages = _clean_injected_messages(data.get("messages"))
    # ★ 压缩状态还原：导入的历史中若曾执行过 #compress（含“【上下文压缩】”摘要消息），
    #   自动恢复到压缩完成后的状态：仅注入压缩快照（摘要消息及其后的对话），
    #   丢弃摘要之前的压缩前历史，避免“压缩前完整历史+压缩摘要”混合体。
    if messages:
        _snap = _restore_compressed_context(None, messages)
        if _snap is not None:
            messages = _snap
            logging.info(f"[OPERATE] 导入记录含 #compress 历史：已自动还原为压缩快照（{len(messages)} 条消息，压缩前历史已丢弃）")
    model = data.get("model") or default_model_name()
    r_eff = data.get("reasoning_effort", "max")
    think = data.get("thinking_enabled", True)
    # ★ 历史用量：导入记录中的累计用量作为新会话的用量初始值（前端导入时携带），
    #   使导入后继续对话的前端用量计数保持连续。
    hist_usage = data.get("usage") or {}
    # ★ 任务计划：导入记录携带的 plan 存入会话（run_parent_loop 启动时推送 plan_update）
    imported_plan = data.get("plan")
    if not (isinstance(imported_plan, dict) and isinstance(imported_plan.get("steps"), list)):
        imported_plan = None
    # ★ 操作模式设置（工具开关/超时/截断/记忆列表上限等）
    op_settings = data.get("settings") or {}
    # ★ 2026-09-21：导出 JSON 里持久化的「会话属性」统计（标题/按模型用量/时间）
    imported_stats = data.get("stats")

    sid = str(uuid.uuid4())
    with sessions_lock:
        sessions[sid] = {
            "cancel_event": threading.Event(),
            "msg_queue": [],   # ★ 统一消息队列（kind 区分用户消息/通知，want_idle 区分插话/排队）
            "busy": False,
            "wait_event": None,   # 占位，由父循环分配
            "event_queue": queue.Queue(),
            "parent_exited": False,
            "parent_running": False,   # ★ 2026-09-28：导入时父循环尚未启动
            "stream_token": None,      # ★ 事件流消费者身份（重连时作废旧连接）
            "live_children": 0,
            "drain_event": threading.Event(),
            "model": model,
            "reasoning_effort": r_eff,
            "thinking_enabled": think,
            "stream_started": False,
            "injected_context": messages,   # 导入的历史上下文（可能为 None）
            # 注意：usage_init 已不再参与用量累计（详见 run_parent_loop 里的说明），
            # 仅保留字段以兼容可能的旧读取方。
            "usage_init": hist_usage,
            "plan": imported_plan,          # 导入的任务计划（可能为 None）
            "settings": op_settings,        # 导入时的操作模式设置
        }
        # ★ 2026-09-21：恢复导出 JSON 里持久化的「会话属性」统计
        #   （标题、按模型分开的用量、时间统计）。此前这些都不落盘，
        #   导出→导入后用量/时间全部归零，看起来就像“被重置”了。
        #   注意：此处已经持有 sessions_lock，必须直接访问会话 dict，
        #   不能再调 _sess_stats()（它假定调用方已持锁，但读的是同一个 sessions 字典）。
        if isinstance(imported_stats, dict):
            _sess_now = sessions.get(sid)
            if _sess_now is not None:
                _st = _sess_now.get("stats")
                if not isinstance(_st, dict):
                    _st = _sess_stats_init()
                    _sess_now["stats"] = _st
                for _k in _STATS_NUM_KEYS:
                    _v = imported_stats.get(_k)
                    if isinstance(_v, (int, float)):
                        _st[_k] = int(_v) if _k == "first_token_count" else float(_v)
                _title = imported_stats.get("title")
                if isinstance(_title, str):
                    _st["title"] = _title[:120]
                _bm = {}
                for _mn, _u in (imported_stats.get("by_model") or {}).items():
                    if isinstance(_u, dict):
                        _bm[str(_mn)] = {k: int(_u.get(k) or 0) for k in _STATS_USAGE_KEYS}
                _st["by_model"] = _bm
                # ★ 2026-10-19：恢复联网工具用量（web_tools）
                _wt_imp = imported_stats.get("web_tools")
                if isinstance(_wt_imp, dict):
                    _wt_new = {"search": {"calls": 0, "ms": 0}, "fetch": {"calls": 0, "ms": 0}}
                    for _k in ("search", "fetch"):
                        _src = _wt_imp.get(_k)
                        if isinstance(_src, dict):
                            _wt_new[_k] = {"calls": int(_src.get("calls") or 0),
                                           "ms": int(_src.get("ms") or 0)}
                    _st["web_tools"] = _wt_new
                # ★ 2026-09-21：把导入的历史用量单独记一份（与本次会话的 by_model 累加区分开）。
                #   面板的“合计/总费用”= hist_usage + by_model（本会话新增）。
                #   ★ 关键：一定要在发第一条消息**之前**就登记，否则导入后立刻发消息时
                #     新用量会先落进 by_model，总量反而比导入前小 —— 就是“用量减少了”的现象。
                # ★ 2026-10-19（修「导出→导入后用量行变小」）：
                #   这里决定"每轮对话下面用量行"的**历史基数**（服务端发 usage 时会加上它）。
                #   注意：新建会话的 stats.hist_usage 初始就是全 0，直接采信会让基数失效
                #   （用户实测：64,278 变成 15,487）。所以按可信度取，并**跳过全 0**：
                #     ① 快照里的 hist_usage（非全 0 时）
                #     ② 导出文件顶层的 usage（最准的累计值）
                #     ③ 按 by_model 求和（旧文件兜底）
                def _nz(d):
                    try:
                        return any(int((d or {}).get(k) or 0) for k in _STATS_USAGE_KEYS)
                    except Exception:
                        return False

                _hist = None
                _hu_src = imported_stats.get("hist_usage")
                if isinstance(_hu_src, dict) and _nz(_hu_src):
                    _hist = {k: int(_hu_src.get(k) or 0) for k in _STATS_USAGE_KEYS}
                if _hist is None:
                    _top_u = data.get("usage")
                    if isinstance(_top_u, dict) and (
                            int(_top_u.get("prompt_tokens") or 0)
                            or int(_top_u.get("completion_tokens") or 0)):
                        _hist = {k: int(_top_u.get(k) or 0) for k in _STATS_USAGE_KEYS}
                if _hist is None:
                    _hist = {"prompt_tokens": 0, "cached_tokens": 0,
                             "completion_tokens": 0, "calls": 0}
                    for _u in _bm.values():
                        for _k in _STATS_USAGE_KEYS:
                            _hist[_k] += int(_u.get(_k) or 0)
                _st["hist_usage"] = _hist
                logging.info(f"[IMPORT] {sid} 历史用量基数(prompt={_hist.get('prompt_tokens')}, "
                             f"completion={_hist.get('completion_tokens')})")
    logging.info(f"[OPERATE] 导入会话 {sid} 已创建，注入消息数 {len(messages) if messages else 0}，model={model}, effort={r_eff}，"
                 f"恢复统计: 标题={bool((imported_stats or {}).get('title'))}，"
                 f"按模型用量={len(((imported_stats or {}).get('by_model') or {}))} 个模型，计划={bool(imported_plan)}")
    return jsonify({"success": True, "session_id": sid,
                    "injected_messages": len(messages) if messages else 0})

def extract_json(text):

    if not text: return None
    start = text.find('{')
    if start != -1:
        count, end = 0, -1
        for i in range(start, len(text)):
            if text[i]=='{': count+=1
            elif text[i]=='}':
                count-=1
                if count==0: end=i; break
        if end!=-1:
            try: return json.loads(text[start:end+1])
            except: pass
    return None

# ==================== 消息投递方式（四种通道可分别配置） ====================
# ★ 2026-09-29（按用户要求）：这四类消息进入父循环的时机以前是写死的，现在可在
#   「操作模式设置 → 工具设置」里分别选择：
#     "interject" 插话：当前 agent 循环一结束就立刻注入并再跑一轮 —— 响应快；
#     "queue"     排队：等本轮彻底空闲（已给出最终答复）才注入 —— 不打断任务。
#   四个通道：user（用户消息）/ timer（定时任务通知）/ agent_return（子代理 return 与最终答复）
#             / ask_user（异步询问的作答）
#
#   实现要点（★ 这里以前有过一批 bug，改动请务必保持这三点）：
#     ① 队列里可能同时有「用户消息」和「通知」两种条目，靠 kind 区分；
#        通知必须用 _notification_text + send_user_message(kind=...) 注入，
#        绝不能当普通用户消息，否则模型会以为通知是用户说的。
#     ② 「排队」用条目上的 want_idle 标志表达，而不是入队时的忙闲状态 ——
#        入队那一刻父循环可能正忙，忙闲会变，标志不会。
#     ③ 只有用户消息才触发 reset_container；通知轮不重置，避免多出一个空思考框。
MSG_CHANNELS = ("user", "timer", "agent_return", "ask_user")
DEFAULT_MSG_DELIVERY = {
    "user": "interject",        # 用户消息：默认插话（立刻响应）
    "timer": "queue",           # 定时任务：默认排队
    "agent_return": "queue",    # 子代理 return：默认排队
    "ask_user": "queue",        # 异步询问：默认排队
}
MSG_CHANNEL_NAMES = {
    "user": "用户消息",
    "timer": "定时任务通知",
    "agent_return": "子代理 return / 最终答复",
    "ask_user": "异步询问的作答",
}


def _delivery_mode(settings, channel):
    """取某通道的投递方式：'interject'（插话，立刻）或 'queue'（排队，等空闲）。"""
    d = (settings or {}).get("msg_delivery") or {}
    m = d.get(channel)
    if m not in ("interject", "queue"):
        m = DEFAULT_MSG_DELIVERY.get(channel, "queue")
    return m


def _notify_channel_of_kind(kind):
    """把通知的 kind 映射到投递通道。"""
    if kind == "timer":
        return "timer"
    if kind == "ask_user":
        return "ask_user"
    # agent_return / agent_answer / agent 等子代理相关
    return "agent_return"


def _enqueue(sess, item, channel):
    """把一条消息按 channel 的配置放进统一消息队列。调用方需持有 sessions_lock。"""
    mode = _delivery_mode(sess.get("settings"), channel)
    it = dict(item)
    # 「排队」= 标记为需要空闲；「插话」= 不标记，父循环一空下来就取
    it["want_idle"] = (mode == "queue")
    it.setdefault("kind", "user")
    sess.setdefault("msg_queue", []).append(it)
    evt = sess.get("wait_event")
    if evt:
        evt.set()
    return mode


def _enqueue_notification(sess, text, kind):
    """通知类消息入队（timer / agent_return / ask_user）。调用方需持有 sessions_lock。

    ★ 2026-09-29：text 允许传字符串，也允许传旧格式的 {"text":..., "kind":...} 字典
      （历史上 /api/timer_notify 就是这么构造的）。以前这里只认字符串，
      调用方传字典时会把 dict 一路带到 send_user_message，
      在那里的 `_file_ctx(files) + msg` 上抛 TypeError，**整个父循环线程直接崩掉**。
      这属于“一个通知就能打死会话”的严重故障，所以在这里做兼容 + 显式转换。
    """
    if isinstance(text, dict):
        d = text
        text = d.get("text") or ""
        kind = d.get("kind") or kind
    text = "" if text is None else str(text)
    ch = _notify_channel_of_kind(kind)
    mode = _enqueue(sess, {"text": text, "kind": kind}, ch)
    logging.info(f"[OPERATE] 通知({ch}) 投递方式={mode}"
                 f"（{'等空闲' if mode == 'queue' else '下一轮立刻'}）")


# ---------- 工具定义（父/子 agent 共用） ----------
COMMON_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "执行一个系统命令。返回命令输出和退出码。仅支持单行命令，若要执行多行命令请先写入文件再执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "async_mode": {"type": "boolean", "description": "是否异步执行", "default": False}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "★ 读取文件内容（**永不落盘、不截断成预览**，专用于读大文件）：读你自己刚写的几千行代码、"
                           "读 fetch_webpage 落盘的网页原文、读 tool_outputs/ 下的完整工具输出等。"
                           "与 execute_command 的 `type`/`cat` 不同，本工具的输出会**完整**返回给你，"
                           "不会因为超过「工具输出截断长度」而被替换成预览——所以大文件请用它，不要用 type/cat。"
                           "大文件可先用 start_line/end_line 只读需要的区间（例如先读 1-400 行看结构，再读具体段落）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对路径相对于工作目录"},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始，含）；省略表示从第一行"},
                    "end_line": {"type": "integer", "description": "结束行号（含）；省略表示读到最后一行"},
                    "max_chars": {"type": "integer", "description": "本次最多返回的字符数上限（默认 200000，仅在极端超大文件时才会触发）"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将文本写入指定文件（UTF-8），自动创建目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对路径相对于工作目录"},
                    "content": {"type": "string", "description": "要写入的文本内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "替换文件中的文本。支持 --all 参数替换所有匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "all_occurrences": {"type": "boolean", "default": False}
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "运行一段 Python / Bash / Node.js / C++ / PowerShell 代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["python", "bash", "cpp", "node", "pwsh"]},
                    "code": {"type": "string", "description": "代码内容"}
                },
                "required": ["language", "code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "读取本地图片文件并返回给模型，用于图像理解（看图、识别截图文字、分析图表等）。支持 JPEG/PNG/GIF/WebP，单张最大 32MB。参数 path 为图片文件路径（绝对路径，或相对 workspace 目录的相对路径）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "图片文件路径（绝对路径，或相对 workspace 目录的相对路径）"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "抓取网页内容，可选择原始 HTML 或纯文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "return_raw": {"type": "boolean", "default": False}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": ("联网搜索（DeepSeek 自研搜索引擎）。返回结构化结果列表：标题、链接、短摘要、"
                            "长摘要(600-800字，适合阅读)、可选的网页正文、发布时间、站点权威性。"
                            "支持搜索语法：site:example.com、-site:、\"精确短语\"、-排除词、lang:zh,en、after:2025、before:2026。"
                            "注意：这是**收费接口**（每 1000 次成功请求 5 元），且无结果不计费；"
                            "需要正文时把 need_content 设为 true（否则 content 恒为空）。")
                            ,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，1~128 字符，支持 site:/lang:/after: 等语法"},
                    "count": {"type": "integer", "description": "返回条数 1~50，默认 10", "default": 10},
                    "need_content": {"type": "boolean", "description": "是否返回网页正文（默认 false）", "default": False},
                    "ori_question": {"type": "string", "description": "触发搜索的用户原始问题，有助于理解搜索意图（可选）"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": ("抓取单个网页并返回 **Markdown 正文**（比 fetch_webpage 更干净，专为给模型阅读设计）。"
                            "返回 {title, content}。适合读文档、博客、GitHub 页面等静态内容。"
                            "注意：这是**收费接口**（每 1000 次成功请求 5 元）；"
                            "若需要原始 HTML 或做 DOM 操作，请改用 fetch_webpage。")
                            ,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的 HTTP/HTTPS 地址（不能带用户名密码）"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_timer",
            "description": "启动一个后台定时监控任务，当任务有输出时，系统会在你空闲时自动通知你，无须你轮询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的监控命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_task",
            "description": "查询异步任务的状态和最新输出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要查询的任务ID"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kill_task",
            "description": "终止一个正在运行的异步任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "要终止的任务ID"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "virtual_desktop",
            "description": "管理后台虚拟桌面（不影响用户真实桌面）：create 创建隐藏桌面；launch 在虚拟桌面启动程序；list 列出虚拟桌面窗口；target 选定目标窗口；shot 截图；info 查看状态；close 关闭。传 real=true 时改为操作【真实桌面】（用户当前桌面）：list 枚举真实窗口、target 选真实窗口、shot 截真实窗口/全屏、launch 在真实桌面启动程序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "create / launch / list / target / shot / info / close"},
                    "real": {"type": "boolean", "description": "true=操作真实桌面（用户当前桌面），false/缺省=虚拟桌面", "default": False},
                    "command": {"type": "string", "description": "launch 时要在虚拟桌面启动的命令行"},
                    "hwnd": {"type": "string", "description": "target 时的窗口句柄（0x 十六进制）"},
                    "scope": {"type": "string", "description": "shot 时的截图范围：window（目标窗口，默认）或 desktop"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_ocr",
            "description": "对桌面窗口截图并 OCR 识别文字，返回每行文字及其区域坐标（用于定位点击）。window=true 截目标窗口，desktop=true 截整个桌面。real=true 时识别真实桌面（用户当前桌面），否则识别隐藏虚拟桌面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "real": {"type": "boolean", "description": "true=真实桌面（用户当前桌面），false/缺省=虚拟桌面", "default": False},
                    "window": {"type": "boolean", "description": "是否截目标窗口", "default": True},
                    "desktop": {"type": "boolean", "description": "是否截整个桌面", "default": False},
                    "lang": {"type": "string", "description": "OCR 语言（如 zh-Hans、en-US），默认自动"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_input",
            "description": "对目标窗口模拟键盘鼠标（消息级注入，不抢用户真实输入）：click 点击 / move 移动 / drag 拖拽 / wheel 滚轮 / key 单键 / hotkey 热键组合（如 ctrl+c）/ type 文本输入 / settext 直接写入 Edit 控件 / activate 激活窗口。坐标默认是目标窗口客户区坐标（与 #ocr --window 截图一致）。real=true 时操作真实桌面（用户当前桌面）的目标窗口，否则操作虚拟桌面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "click / move / drag / wheel / key / hotkey / type / settext / activate"},
                    "real": {"type": "boolean", "description": "true=真实桌面（用户当前桌面），false/缺省=虚拟桌面", "default": False},
                    "x": {"type": "number", "description": "x 坐标"},
                    "y": {"type": "number", "description": "y 坐标"},
                    "x1": {"type": "number", "description": "drag 起点 x"},
                    "y1": {"type": "number", "description": "drag 起点 y"},
                    "x2": {"type": "number", "description": "drag 终点 x"},
                    "y2": {"type": "number", "description": "drag 终点 y"},
                    "delta": {"type": "number", "description": "wheel 滚轮增量（如 120 向上，-120 向下）"},
                    "button": {"type": "string", "description": "left / right / middle"},
                    "double": {"type": "boolean", "description": "是否双击"},
                    "steps": {"type": "number", "description": "drag 的步数"},
                    "key": {"type": "string", "description": "按键名：enter/tab/esc/f1..f12/a..z/0..9/up/down 等"},
                    "hotkey": {"type": "string", "description": "热键组合：ctrl+c / ctrl+shift+s / alt+f4 / win+d"},
                    "text": {"type": "string", "description": "要输入的文本"},
                    "screen": {"type": "boolean", "description": "坐标是否为屏幕坐标（默认客户区坐标）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save",
            "description": "保存当前对话的完整上下文到记忆库：#save <标题> 同时保存本地(client/workspace/memories)与云端(server/workspace/memories)；#save local <标题> 或 #save cloud <标题> 仅保存到指定位置。标题将作为文件名，可在其中简要说明任务目标与进度，便于日后 #list/#load 检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "记忆标题（作为文件名，建议包含任务目标与进度说明，如“修复登录bug-已完成80%”）"},
                    "target": {"type": "string", "enum": ["local", "cloud"], "description": "保存目标：local=仅客户端本地，cloud=仅服务器云端；省略则同时保存本地与云端"}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete",
            "description": "删除记忆文件：#delete local <标题> 删除本地记忆，#delete cloud <标题> 删除云端记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["local", "cloud"], "description": "删除目标：local（客户端本地）/ cloud（服务器云端）"},
                    "title": {"type": "string", "description": "要删除的记忆标题（文件名）"}
                },
                "required": ["target", "title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list",
            "description": "列出记忆文件（按修改时间降序，含标题、修改时间、文件大小、来源 local/cloud）：#list local、#list cloud 或 #list all。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["local", "cloud", "all"], "description": "列出范围：local（本地）/ cloud（云端）/ all（全部）"}
                },
                "required": ["target"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "在记忆库中按关键词搜索（同时匹配记忆**标题**与**内容**）：支持多个关键词，默认要求全部命中（AND）。记忆很多时先用它定位，再用 load 载入完整上下文。返回命中的标题、来源(local/cloud)、命中位置与带高亮标记的摘要片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "关键词列表，如 [\"登录\",\"bug\"]；默认全部关键词都命中才算匹配"
                    },
                    "match": {"type": "string", "enum": ["all", "any"], "description": "all=全部关键词命中(默认)；any=命中任一关键词"},
                    "search_in": {"type": "string", "enum": ["both", "title", "content"], "description": "搜索范围：both=标题+内容(默认)；title=仅标题；content=仅内容"},
                    "target": {"type": "string", "enum": ["all", "local", "cloud"], "description": "搜索范围：all=本地+云端(默认)；local=仅客户端本地；cloud=仅服务器云端"},
                    "limit": {"type": "integer", "description": "最多返回多少条（默认 10）"},
                    "context_chars": {"type": "integer", "description": "每条摘要片段长度（默认 160 字符）"}
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "load",
            "description": "把记忆文件中的上下文【追加】到当前对话（注意：追加而非覆盖现有上下文）：#load <文件> 自动在本地/云端查找，#load local <文件> 或 #load cloud <文件> 指定来源。加载后可无缝继续该记忆中的任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "记忆文件名（#list 列出的标题）"},
                    "target": {"type": "string", "enum": ["local", "cloud"], "description": "读取来源：local / cloud；省略则自动在本地与云端查找"}
                },
                "required": ["file"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan",
            "description": "记录本回合的任务计划并在左侧任务视图显示，逐项打勾跟踪进度：\n- #plan new <标题>\\n- 步骤1\\n- 步骤2 ... 创建/替换任务计划（每行一个步骤，可用 - 开头）；\n- #plan step <序号> done|doing|failed 更新某步骤状态；\n- #plan done 全部完成；#plan clear 清空；#plan 查看当前计划。\n任务开始时应先 #plan new 创建计划，执行中逐项 #plan step 更新，全部完成后 #plan done。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["new", "step", "done", "clear", "view"], "description": "操作：new=创建计划，step=更新步骤状态，done=全部完成，clear=清空，view=查看"},
                    "title": {"type": "string", "description": "计划标题（action=new 时必填）"},
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "步骤列表（action=new 时必填，每项一个步骤）"},
                    "index": {"type": "integer", "description": "步骤序号（1 起，action=step 时必填）"},
                    "status": {"type": "string", "enum": ["done", "doing", "failed", "pending"], "description": "步骤新状态（action=step 时使用）"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compress_context",
            "description": "主动压缩上下文（节省 token 费用）。当系统推送\u201c上下文占用警告\u201d（占用 ≥50%）或你认为一个大阶段已完成、不再需要保留以往细节时调用。压缩后系统将删除此前所有对话与工具历史，仅保留：系统提示、你提供的阶段摘要、任务计划（#plan）、正在运行的异步/定时任务（命令与ID）、子代理信息（父 agent 时含各子代理 ID 与最后一次提示词）。摘要必须包含：①用户需求 ②已完成进度 ③已获取的重要信息 ④下一步应当做什么 ⑤关键文件的路径列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "阶段压缩摘要：简要描述 ①用户需求 ②已完成进度 ③已获取的重要信息 ④下一步应当做什么 ⑤关键文件的路径列表"}
                },
                "required": ["summary"]
            }
        }
    },
]

# 父 agent 专属：agent 管理工具
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": "创建子 agent 分担复杂任务（agent 调用 agent）。子 agent 在独立上下文中执行任务，会通过 #return 返回里程碑、最终给出答复。同时运行数量受“操作模式设置-工具设置”中的“最大子代理数量”限制（默认 3 个）。父 agent 看不到子 agent 的思考与工具调用，只能收到其 return 和最终答复（自动推送）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "子 agent 模型别名（可选项由父级动态注入 enum）；不填则继承当前模型。别名与模型名可在「设置 → 模型与提供商」中配置。不填则继承父 agent 当前模型。"},
                    "effort": {"type": "string", "enum": ["max", "high", "low"], "description": "子 agent 思考强度（对应四档滑块的 low/high/max）。不填则继承父 agent 当前思考强度。"},
                    "task": {"type": "string", "description": "发给子 agent 的任务描述。如果用户原始提示词不够精准，请先优化/细化后再发送。"}
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "prompt_agent",
            "description": "向子 agent 追加新信息（作为新的用户消息）。子 agent 运行中或完成后均可用，会触发子 agent 继续推理。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "子 agent ID（以 A 开头）"},
                    "message": {"type": "string", "description": "要追加给子 agent 的信息/任务"}
                },
                "required": ["agent_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_agent",
            "description": "查询子 agent 的状态（running/finished/killed 等）、#return 里程碑和最终答复。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "子 agent ID（以 A 开头）"}
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kill_agent",
            "description": "销毁一个子 agent。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "子 agent ID（以 A 开头）"}
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "列出当前会话的全部子 agent 及其状态。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]

# 子 agent 专属：里程碑返回工具
RETURN_TOOL = {
    "type": "function",
    "function": {
        "name": "return_result",
        "description": "向父 agent 返回里程碑或中间信息（协议 #return）。父 agent 会立即收到通知；任务执行中可多次调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要返回给父 agent 的里程碑/中间结果内容"}
            },
            "required": ["content"]
        }
    }
}

# ★ 仅父 agent 可用：询问用户（子 agent 不提供该工具）
ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "询问用户：向用户提交选择题（前端自动附带“其他，请补充”填空选项），显示在网页左侧“问题视图”。★ 一次可以问多道题：用 questions 数组，前端会**同时把每道题各显示一张卡片**，用户全部作答后把各题答案一起返回。单题也可以多选（在顶层给 multiple:true 即可，不必套数组）。默认同步阻塞等待用户作答，答案直接作为工具结果返回；async=true 为异步询问：不阻塞、继续执行，用户作答后将以“新通知”形式送达。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "单题写法：向用户提出的问题（一道选择题）。问多道题请改用 questions 数组。"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "单题写法：选项列表（2-6 个为宜；不要包含“其他，请补充”，系统会自动附加）"},
                "multiple": {"type": "boolean", "description": "单题写法：true=该题**可多选**（用户可勾选多个选项，答案以「、」连接返回）；缺省 false=单选。多选不必套 questions 数组，直接在这里给 true 即可。"},
                "questions": {
                    "type": "array",
                    "description": "★ 多题写法：一次提交多道题，前端每道题各显示一张卡片，用户全部作答后一起返回。与 question/options 二选一。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "问题文本"},
                            "options": {"type": "array", "items": {"type": "string"}, "description": "选项列表（2-6 个为宜；不要包含“其他，请补充”）"},
                            "multiple": {"type": "boolean", "description": "true=该题**可多选**（用户可勾选多个选项，答案以逗号分隔返回）；缺省 false=单选"}
                        },
                        "required": ["question", "options"]
                    }
                },
                "async": {"type": "boolean", "description": "true=异步询问（不阻塞，用户作答后通知）；缺省 false=同步阻塞等待用户作答"}
            },
            "required": []
        }
    }
}

PARENT_TOOLS = COMMON_TOOLS + AGENT_TOOLS + [ASK_USER_TOOL]
# ★ 子 agent 不能访问“任务计划”工具（#plan）：子代理创建/修改任务计划会与父代理的
#   任务视图互相打架，因此从 COMMON_TOOLS 中剔除 plan 后再组装子代理工具列表。
CHILD_TOOLS = [t for t in COMMON_TOOLS if t["function"]["name"] != "plan"] + [RETURN_TOOL]

# ==================== 操作模式工具分组（设置弹窗按组开关） ====================
# 打包分组：同一组的工具一起勾选；勾选后才会给模型相应工具与系统提示词。
TOOL_GROUPS = {
    # ★ 2026-10-14（按用户要求重组）：
    #   "file" 只保留**文件读写**；"cmd" 承担**执行命令**（含原来的 timer 三项）。
    "file":   ["write_file", "replace_in_file", "view_image", "read_file"],
    # ★ 2026-10-14（按用户要求）：执行命令 排在 网页抓取 之前
    "cmd":    ["execute_command", "start_timer", "query_task", "kill_task", "run_script"],
    "web":    ["fetch_webpage", "web_search", "web_fetch"],
    "screen": ["virtual_desktop", "screen_ocr", "simulate_input"],
    "memory": ["save", "delete", "list", "load", "search_memory"],
    "plan":   ["plan"],
    "agent":  ["spawn_agent", "prompt_agent", "query_agent", "kill_agent", "list_agents"],
    "compress": ["compress_context"],
    "ask_user": ["ask_user"],
}
TOOL_GROUP_NAMES = {
    "file": "文件读写", "cmd": "执行命令", "web": "网页抓取",
    "screen": "屏幕与桌面操作", "memory": "记忆管理", "plan": "任务计划",
    "agent": "子代理管理", "compress": "上下文管理", "ask_user": "询问用户",
}
# ★ 2026-09-29（按用户要求）：默认只开这几个分组，其余默认关闭。
DEFAULT_TOOL_GROUPS = ("file", "cmd", "web", "memory", "plan", "compress", "ask_user")


def get_tool_groups():
    """返回默认工具分组开关（{分组: 是否开启}）。"""
    return {g: (g in DEFAULT_TOOL_GROUPS) for g in TOOL_GROUPS}


def build_parent_tools(settings):
    """根据设置弹窗的工具开关过滤父代理工具列表（未启用分组的工具不提供给模型）。"""
    st = dict((settings or {}).get("tools") or {})
    # ★ 2026-10-14（分组重组的老配置兼容）：
    #   旧版把"文件"与"执行"合在 file 里、定时任务单列 timer。
    #   新版拆成 file(文件读写) + cmd(执行命令)、取消 timer。
    #   迁移规则：老 file 的开关同时作用于 file 与 cmd（当时它代表这两类）；
    #             老 timer 的开关作用于 cmd；两者取"或"（宁可开着，用户可自行关）。
    if "cmd" not in st:
        _old_file = st.get("file")
        _old_timer = st.get("timer")
        if _old_file is not None or _old_timer is not None:
            st["cmd"] = bool(_old_file) or bool(_old_timer)
    st.pop("timer", None)      # 旧键清理
    enabled = {}
    for g in TOOL_GROUPS:
        # ★ 未显式设置时按默认分组决定（以前默认全开，现在按用户要求只开常用几个）
        enabled[g] = st.get(g, g in DEFAULT_TOOL_GROUPS)
    tools = []
    for t in PARENT_TOOLS:
        name = t["function"]["name"]
        grouped = False
        for g, names in TOOL_GROUPS.items():
            if name in names:
                grouped = True
                if enabled[g]:
                    tools.append(t)
                break
        if not grouped:
            tools.append(t)   # ★ 未分组的工具（如 ask_user）始终可用
    # ★ 2026-10-14（修「子代理模型别名搜不到，schema 写死了 flash/pro」）：
    #   把 spawn_agent 的 model 字段**动态**改成"所有已配置模型的别名"，
    #   并在描述里列出 别名=模型名（提供商），这样模型（和用户）都能看到全部可选项。
    #   注意深拷贝 —— 不能改动模块级的 AGENT_TOOLS 常量。
    try:
        _opts = []            # [[别名, 模型名, 提供商], ...]
        for _pr, _m in iter_models():
            _al = (_m.get("alias") or "").strip()
            _nm = (_m.get("name") or "").strip()
            if _nm:
                _opts.append([_al or _nm, _nm, _pr.get("name") or ""])
        if _opts:
            _names = [o[0] for o in _opts]
            _desc = "；".join(
                f"{o[0]}={o[1]}" + (f"（{o[2]}）" if o[2] else "") for o in _opts)
            for _i, _t in enumerate(tools):
                if _t.get("function", {}).get("name") == "spawn_agent":
                    _nt = copy.deepcopy(_t)
                    _mp = _nt["function"]["parameters"]["properties"]
                    _mp["model"] = {
                        "type": "string",
                        "enum": _names,
                        "description": (
                            f"子 agent 模型别名（可选其一）：{_desc}。"
                            "模型别名与模型名可在「设置 → 模型与价格 → 模型提供商」中修改。"
                            "不填则继承父 agent 当前模型。"),
                    }
                    tools[_i] = _nt
                    break
    except Exception as _e:
        logging.warning(f"[TOOLS] 动态生成 spawn_agent 模型选项失败: {_e}")
    return tools

DEFAULT_PROMPT_BASE = """## Language Policy
- **Internal reasoning**: always in English (better training support, more stable formatting).
- **Final answer**: Simplified Chinese if the user wrote Chinese, otherwise the user's language.

You are DeepSeek Operator, an AI assistant that fulfills user requests by executing system commands. Your working directory is `workspace`. You have tool functions to read, write, run commands and fetch web pages. Think step by step, call the appropriate tools, and once no tool is needed output the final answer. Context 1M tokens, max output 384K.

## Output Standards
- Use Markdown for readability.
- LaTeX math: inline `$...$` or `\(...\)`, block `$$...$$` or `\[...\]` — all four forms render correctly, use whichever you prefer.
- In `**bold**`, the first and last characters inside the asterisks must not be punctuation.
- To let the user open a local file, write `[文件: 完整路径]` in the final answer (clicking opens it on their machine). Do not use `[文件](路径)`.

## Multi-Turn Strategy
- For complex tasks: plan first, then execute and verify step by step; do not generate everything in one shot.
- When the context has grown long, briefly summarize completed progress and next steps in the user's language.
- See the task through to the end; never give up midway.

## Tool Efficiency
- **Prefer code over repeated calls**: if you would call the same tool more than 3 times in a loop, write a single `run_script` instead.
- **Parallelize independent calls**: issue independent tool calls in one response; serialize only when a later call needs an earlier result.
- **Never poll**: use `start_timer` (auto-notify) instead of looping `query_task`. If a background task is running and you have nothing to do, give a brief status and end the turn.

## Knowledge Base
- **Write only when**: the user asks you to remember something, you failed then retried and succeeded, or the user states a durable preference. Nothing else.
- **Keep each entry under 200 words**, specific, with a category (env / preference / convention / lesson) and 1-3 tags.
- **Search before unfamiliar tasks**, not every task. The index (filenames + titles) is already in your context -- scan it first.
- **A bloated knowledge base is worse than none.** When in doubt, do not write."""

DEFAULT_PROMPT_SECTIONS = {
    "agent": """## Sub-Agents and Async Tasks
- When a complex task splits into independent subtasks, you **must** call `spawn_agent` to share the work (max {max_subagents} concurrent; sub-agents cannot spawn further agents). You coordinate, consolidate and produce the overall answer.
- **Choosing a sub-agent model**: `spawn_agent` accepts any configured model alias (the `model` field lists them all). Empty = inherit your current model. Aliases live in Settings -> Model & Pricing -> Model Providers.
- Before creating sub-agents, write an `AGENTS.md` with the project goals and constraints for them to read.
- Sub-agents have isolated contexts: you only receive their `return_result` milestones and final answers (pushed automatically). While they run you can append information with `prompt_agent`, and manage them with `query_agent` / `kill_agent` / `list_agents`.
- `#process` / `#kill` also cover client-side async tasks: IDs starting with `A` are sub-agents, anything else is passed to the client.""",
    "agent_timer": """## Async Tasks: Notification Protocol
- After `#timer` or `#spawn_agent` starts, the system pushes status/return information into your context automatically and wakes you. **You never need to poll** -- do not loop `#process` / `#agents` to check status.
- With `start_timer` you may output a brief interim answer (e.g. "后台任务已启动，约 X 分钟完成，完成后我继续") and end the turn immediately. Scheduled tasks are only pushed while you are idle.""",
    "cmd": """## Async Execution vs Scheduled Monitoring
- `execute_command` with `async_mode=True`: only starts a background process; **the system will not notify you of its output**. You must poll with `query_task` yourself, and must not output the final answer until it is confirmed finished.
- `start_timer`: a **scheduled task with automatic notification**; the system pushes a message when it produces output or finishes. **Only with `start_timer` may you end the turn right after starting it.**
- To wait on a long task with automatic progress delivery, always prefer `start_timer` over `async_mode`.""",
    "plan_memory": """## Tasks and Memory
- **Create a task plan with `plan` for every new task, and tick items off as you go** so the left-hand task view shows live progress. Tick each step the moment it completes.
- **For large tasks, call `save` after each stage completes** (especially before a stage summary or the final answer) to persist context to the memory store (local + cloud), so an interruption does not lose progress. Do not wait for the whole task: stage results must be persisted immediately.
- When resuming a task in a new session: `list` to find the memory, then `load` to append it to the conversation.""",
    "compress": """## Context Management (Save Token Costs)
- The context window is 1M tokens. On a "上下文占用警告" (usage >= 50%), or when a major stage is done and earlier details no longer matter, call `compress_context` proactively (`#compress <summary>`).
- The summary must cover: (1) user requirements (2) completed progress (3) important information gathered (4) what to do next (5) paths to key files.
- Compression deletes all prior conversation and tool history, keeping only: the system prompt, your summary, the task plan (`#plan`), running async/scheduled tasks (commands + IDs), and sub-agents (ID + last prompt).
- After compressing, continue from the summary; do not redo completed work. Load details back from the memory store with `load` if needed.""",
    "screen": """## Screen Operations
- By default you act on a hidden virtual desktop so the user is not disturbed; pass `real=true` to screen tools to operate the user's actual desktop (real-desktop mode needs no create/close). The user can watch live via the "AI 屏幕监控" panel.
- Prefer Playwright, FFmpeg, grep, ImageMagick; OCR + simulated keyboard/mouse is the last resort.
- **Close everything you open on the virtual desktop** (browsers, editors, programs) as soon as you are done -- close the desktop with `virtual_desktop`'s `close`, or close the windows inside it. Never leave them behind: the user may be deleting or moving files at the same time, and leftover windows lock files or ports, so files cannot be deleted and the user may even blame their own software.""",
    "long_output": """## Handling Long Tool Output
- When a tool (`execute_command`, `run_script`, `fetch_webpage`, `write_file`, ...) returns more than the "工具输出截断长度" from the settings dialog (4000 chars by default), the full output is saved to a file under `workspace/tool_outputs/` and the tool result gives you that path.
- Then do not conclude from the truncated preview alone: read the full file with the `read_file` tool (never truncated, never persisted), or with `execute_command` (`type` / `findstr`) or `run_script`.""",
    "web": """## Web Search and Fetch (DeepSeek native)
- `web_search` queries the web (DeepSeek's own engine) and returns structured results: title, URL, short snippet, a 600-800 char `summary` written for models, optional full page `content`, publish date and `authority` score (>=3 is authoritative). Search syntax: `site:`, `-site:`, `"exact phrase"`, `-word`, `lang:zh,en`, `after:2025`, `before:2026`.
- `web_fetch` turns a single URL into clean Markdown (`{title, content}`) -- much easier to read than raw HTML. Prefer it for docs, blogs, README pages.
- `fetch_webpage` is the older local fetcher (runs on the client agent) and returns raw HTML or plain text; use it when you need the actual DOM or the DeepSeek APIs are unavailable.
- **Cost**: `web_search` and `web_fetch` bill **¥5 per 1,000 successful requests** (failed calls and zero-result searches are free). Do not spray searches: one well-formed query beats five vague ones. Use `need_content=false` unless you really need full page text.
- **Limits**: Search 100 QPS, Fetch 25 QPS per account; HTTP 429 means you should back off. Fetch returns 404 when a page has no extractable text (often JS-rendered) -- fall back to `web_search` or `fetch_webpage` rather than retrying the same URL.
- DeepSeek Search/Fetch only answer for the DeepSeek provider (it uses that provider's API key). If the current model is a local one (e.g. Ollama), these tools may return an auth error; use `fetch_webpage` instead.""",
    "file": """## Image Understanding
- To understand image content (describe an image, read text off a screenshot, analyse a chart), call the `view_image` tool with the image path (absolute, or relative to workspace); the system returns the image to you.
- Images may only appear in user messages (handled automatically); the total image size per request is limited, so call it as needed.
- If the routed model does not support image understanding the API returns an error; switch the model in Settings -> Model & Pricing and retry.""",
    "safety": """## Safety and Basic Rules
- The dangerous-command blacklist is enforced by the system; do not generate such commands.
- Avoid `sudo`.
- For file operations, keep full backups under `.backups/`; delete only after the user confirms.
- You may chain commands with `&&` or `;`. Synchronous commands time out after {max_timeout_min} minutes, and are killed automatically if they produce no output for {idle_timeout} seconds. Async commands and scheduled tasks have no time limit.
- **Protect the agent processes**: the running `app.py` / `agent.py` / `agent.exe` are this system's own server and client agents. **Never kill / taskkill / terminate them**, and never perform dangerous operations on them (deleting files, rewriting code). Even when the user asks to clean up processes, exclude these.""",
    "ask_user": """## Asking the User (ask_user)
- When you need a choice, a preference, or missing information before continuing (which approach, which files, what format, whether to overwrite), call `ask_user` instead of deciding for the user. The user's side automatically adds an "其他，请补充" free-text option.
- Options must be specific and actionable (2-6 is best). Do not write "其他，请补充" into the options yourself.
- **Batch questions to save round trips**: pass a `questions` array; the user sees one card per question and submits them together, so you get every answer in one tool result. Prefer 2-4 questions per call; do not pad the list.
- **Allow multiple choices when it fits**: set `"multiple": true` on a question (or a top-level `"multiple": true` in the single-question form) so the user can tick several; the answer comes back joined by "、". Use it for genuinely multi-valued questions only.
- Synchronous (default) blocks until all questions are answered; asynchronous (`async=true`) returns immediately and delivers the answers later like a scheduled task -- use it for secondary questions you can work around.""",
}

def build_agent_system(settings):
    """根据设置弹窗的工具开关动态生成父 agent 系统提示词：
    被禁用的工具组其提示词段落一并移除；用户在“提示词”标签页保存的覆盖文本优先于内置默认。"""
    st = (settings or {}).get("tools") or {}
    def on(g):
        return st.get(g, True)
    idle_timeout = int((settings or {}).get("idle_timeout", 30) or 30)
    max_timeout = int((settings or {}).get("max_timeout", 600) or 600)
    try:
        _max_sub = max(1, int((settings or {}).get("max_subagents", 3) or 3))
    except Exception:
        _max_sub = 3
    _prompts = load_server_settings().get("prompts") or {}
    # ★ 操作模式基础提示词用 agent_base（与对话模式的 chat_base 完全独立）
    _ov_base = str(_prompts.get("agent_base") or "").strip()
    _ov_sections = _prompts.get("sections") or {}
    def _sec(key, default):
        v = _ov_sections.get(key)
        if isinstance(v, str) and v.strip():
            return v
        return default
    secs = []
    secs.append(_ov_base if _ov_base else DEFAULT_PROMPT_BASE)
    if on("agent"):
        secs.append(_sec("agent", DEFAULT_PROMPT_SECTIONS["agent"].format(max_subagents=_max_sub)))
    # ★ 2026-10-14（分组重组）：定时/执行类提示词挂在 "cmd" 分组下（原 timer）
    if on("cmd") or on("agent"):
        secs.append(_sec("agent_timer", DEFAULT_PROMPT_SECTIONS["agent_timer"]))
    if on("cmd"):
        secs.append(_sec("cmd", DEFAULT_PROMPT_SECTIONS["cmd"]))
    if on("plan") or on("memory"):
        secs.append(_sec("plan_memory", DEFAULT_PROMPT_SECTIONS["plan_memory"]))
    secs.append(_sec("ask_user", DEFAULT_PROMPT_SECTIONS["ask_user"]))
    if on("compress"):
        secs.append(_sec("compress", DEFAULT_PROMPT_SECTIONS["compress"]))
    if on("screen"):
        secs.append(_sec("screen", DEFAULT_PROMPT_SECTIONS["screen"]))
    if on("web"):
        secs.append(_sec("web", DEFAULT_PROMPT_SECTIONS["web"]))
    secs.append(_sec("long_output", DEFAULT_PROMPT_SECTIONS["long_output"]))
    if on("file"):
        secs.append(_sec("file", DEFAULT_PROMPT_SECTIONS["file"]))
    secs.append(_sec("safety", DEFAULT_PROMPT_SECTIONS["safety"].format(max_timeout_min=max_timeout // 60, idle_timeout=idle_timeout)))
    return "\n\n".join(secs)

def get_prompt_defaults():
    """“提示词”标签页展示用：三套内置默认文本（对话 / 操作 / 子 Agent）+ 各工具段落，
    占位符已按默认值填充（运行时的真实取值由 build_agent_system 按当前设置替换）。"""
    secs = dict(DEFAULT_PROMPT_SECTIONS)
    secs["agent"] = secs["agent"].format(max_subagents=MAX_CHILD_AGENTS)
    secs["safety"] = secs["safety"].format(max_timeout_min=10, idle_timeout=30)
    return {
        "chat_base": SYSTEM_PROMPT,             # 对话模式基础提示词（与操作模式独立）
        "agent_base": DEFAULT_PROMPT_BASE,      # 操作模式（父 agent）基础提示词
        "child_base": CHILD_SYSTEM_PROMPT,      # 子 Agent 基础提示词
        "sections": secs,                       # 操作模式各工具段落
    }

# 将 tool call 转换为命令
def _strip_tool_prefix(cmd, prefix):
    """★ 2026-10-04：去掉模型**重复携带**的命令前缀。

    背景（用户实测）：历史/记忆恢复后，模型看到的是「记录里的原始命令」形式
    （`#exec xxx`），于是它调用 execute_command 时把 `#exec` 也写进了 command 参数。
    而 tool_to_command 又会无条件拼一个前缀，结果变成 `#exec #exec xxx`，
    执行时直接报 `'#exec' is not recognized as an internal or external command`。
    这里在拼接前把重复前缀剥掉 —— 对模型宽容，正常情况（不带前缀）完全不受影响。
    """
    s = (cmd or "").strip()
    p = prefix.strip()
    while p and s.lower().startswith(p.lower()):
        s = s[len(p):].lstrip()
    return s


def tool_to_command(tool_call):
    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    if func_name == "execute_command":
        cmd = _strip_tool_prefix(args.get("command", ""), "#exec")
        if not cmd: return None
        return f"#exec {'--async ' if args.get('async_mode') else ''}{cmd}"
    elif func_name == "read_file":
        path = _strip_tool_prefix(args.get("path", ""), "#read_file")
        if not path:
            return None
        parts = [f"#read_file {path}"]
        sl, el = args.get("start_line"), args.get("end_line")
        mc = args.get("max_chars")
        if sl is not None:
            parts.append(f"--start={int(sl)}")
        if el is not None:
            parts.append(f"--end={int(el)}")
        if mc is not None:
            parts.append(f"--max-chars={int(mc)}")
        return " ".join(parts)
    elif func_name == "write_file":
        path = _strip_tool_prefix(args.get("path", ""), "#write")
        content = args.get("content", "")
        if not path or not content: return None
        return f"#write {path}\n{content}"
    elif func_name == "replace_in_file":
        path = _strip_tool_prefix(args.get("path", ""), "#replace")
        old = args.get("old_text", ""); new = args.get("new_text", "")
        if not path: return None
        all_flag = "--all " if args.get("all_occurrences") else ""
        return f"#replace {all_flag}{path}\n{old}\n===<separator>===\n{new}"
    elif func_name == "run_script":
        lang = args.get("language", ""); code = args.get("code", "")
        if not lang or not code: return None
        return f"#run_{lang}\n{code}"
    elif func_name == "view_image":
        path = _strip_tool_prefix(args.get("path", ""), "#view_image")
        if not path: return None
        return f"#view_image {path}"
    elif func_name == "fetch_webpage":
        url = _strip_tool_prefix(args.get("url", ""), "#fetch_page")
        if not url: return None
        raw = "--raw " if args.get("return_raw") else ""
        return f"#fetch_page {raw}{url}"
    elif func_name == "web_search":
        q = str(args.get("query") or "").strip()
        if not q: return None
        # 编码进命令：首行 #web_search <count> <need_content>，次行 query，第三行 ori_question
        cnt = args.get("count") or 10
        need = "1" if args.get("need_content") else "0"
        ori = str(args.get("ori_question") or "").replace("\n", " ").strip()
        return f"#web_search {cnt} {need}\n{q}\n{ori}"
    elif func_name == "web_fetch":
        u = str(args.get("url") or "").strip()
        if not u: return None
        return f"#web_fetch\n{u}"
    elif func_name == "start_timer":
        cmd = _strip_tool_prefix(args.get("command", ""), "#timer")
        if not cmd: return None
        return f"#timer {cmd}"
    elif func_name == "query_task":
        tid = _strip_tool_prefix(args.get("task_id", ""), "#process")
        if not tid: return None
        return f"#process {tid}"
    elif func_name == "kill_task":
        tid = args.get("task_id", "")
        if not tid: return None
        return f"#kill {tid}"
    elif func_name == "spawn_agent":
        model = args.get("model", "") or ""
        effort = args.get("effort", "") or ""
        task = args.get("task", "")
        if not task: return None
        return f"#spawn_agent {model} {effort}\n{task}"
    elif func_name == "prompt_agent":
        aid = args.get("agent_id", "")
        msg = args.get("message", "")
        if not aid or not msg: return None
        return f"#prompt {aid}\n{msg}"
    elif func_name == "query_agent":
        aid = args.get("agent_id", "")
        if not aid: return None
        return f"#process {aid}"
    elif func_name == "kill_agent":
        aid = args.get("agent_id", "")
        if not aid: return None
        return f"#kill {aid}"
    elif func_name == "list_agents":
        return "#agents"
    elif func_name == "search_memory":
        kws = args.get("keywords") or []
        if isinstance(kws, str):
            kws = [s for s in re.split(r'[\s,，、]+', kws) if s]
        kws = [str(k).strip() for k in kws if str(k).strip()]
        if not kws:
            return None
        spec = {"keywords": kws}
        for k, src in (("match", "mode"), ("search_in", "search_in"), ("target", "target"),
                       ("limit", "limit"), ("context_chars", "context_chars")):
            if args.get(k) not in (None, ""):
                spec[src] = args[k]
        return "#search_memory " + json.dumps(spec, ensure_ascii=False)
    elif func_name == "return_result":
        content = args.get("content", "")
        if not content: return None
        return f"#return\n{content}"
    elif func_name == "compress_context":
        summary = args.get("summary", "")
        if not summary: return None
        return f"#compress\n{summary}"
    elif func_name == "virtual_desktop":
        action = args.get("action", "")
        real = "--real " if args.get("real") else ""
        if action == "create":
            return f"#vdesk {real}create"
        if action == "list":
            return f"#vdesk {real}list"
        if action == "info":
            return f"#vdesk {real}info"
        if action == "close":
            return f"#vdesk {real}close"
        if action == "launch":
            cmd = args.get("command", "")
            if not cmd: return None
            return f"#vdesk {real}launch {cmd}"
        if action == "target":
            hwnd = args.get("hwnd", "")
            if not hwnd: return None
            return f"#vdesk {real}target {hwnd}"
        if action == "shot":
            scope = args.get("scope", "window")
            return f"#vdesk {real}shot --{scope}" if scope == "desktop" else f"#vdesk {real}shot --window"
        return None
    elif func_name == "screen_ocr":
        parts = ["#ocr"]
        if args.get("real"):
            parts.append("--real")
        if args.get("desktop"):
            parts.append("--desktop")
        else:
            parts.append("--window")
        lang = args.get("lang")
        if lang:
            parts.append(f"--lang={lang}")
        return " ".join(parts)
    elif func_name == "simulate_input":
        action = args.get("action", "")
        if action not in ("click", "move", "drag", "wheel", "key", "hotkey", "type", "settext", "activate"):
            return None
        parts = [action]
        for k in ("x", "y", "x1", "y1", "x2", "y2", "delta", "key", "hotkey"):
            if args.get(k) is not None:
                parts.append(str(args[k]))
        if args.get("text") is not None:
            parts.append('"' + str(args["text"]).replace('"', '\\"') + '"')
        if args.get("button"):
            parts.append(f"--button={args['button']}")
        if args.get("double"):
            parts.append("--double")
        if args.get("steps"):
            parts.append(f"--steps={args['steps']}")
        if args.get("screen"):
            parts.append("--screen")
        if args.get("real"):
            parts.append("--real")
        return "#input " + " ".join(parts)
    elif func_name == "save":
        title = args.get("title", "")
        if not title:
            return None
        tgt = args.get("target") or ""
        return f"#save {tgt} {title}" if tgt else f"#save {title}"
    elif func_name == "delete":
        tgt = args.get("target", "")
        title = args.get("title", "")
        if not tgt or not title:
            return None
        return f"#delete {tgt} {title}"
    elif func_name == "list":
        tgt = args.get("target", "all")
        return f"#list {tgt}"
    elif func_name == "load":
        file = args.get("file", "")
        if not file:
            return None
        tgt = args.get("target") or ""
        return f"#load {tgt} {file}" if tgt else f"#load {file}"
    elif func_name == "plan":
        # ★ 2026-10-12（修「工具 plan 缺少必要参数」的误导性报错）：
        #   实测（用真实记录复现）模型用结构化参数调 plan 时经常漏 "action"：
        #       plan {}                        -> 返回 None -> 上层报「缺少必要参数」
        #       plan {title, steps}（漏 action）-> 同样报错
        #   但**参数其实够用**，而且纯文本 `#plan`（同样没有 action）是能工作的 ——
        #   run_operate_command 里写着 `action = parts[1] if len(parts) > 1 else "view"`。
        #   所以这里按同一套默认值补齐，不再把"没写 action"当成缺参数：
        #     · 没 action 但有 title/steps -> 当成 new（模型显然是想建计划）
        #     · 没 action 且什么都没给      -> 当成 view
        action = (args.get("action") or "").strip()
        _steps = args.get("steps")
        if isinstance(_steps, str):
            # 模型偶尔把 steps 写成多行字符串：按行拆成列表
            _steps = [x.strip() for x in _steps.replace("\r", "").split("\n") if x.strip()]
        if not action:
            action = "new" if (args.get("title") or _steps) else "view"
        if action == "new":
            title = args.get("title") or "任务计划"
            steps = _steps or []
            if not steps:
                return None
            lines = ["#plan new " + title]
            for s in steps:
                lines.append("- " + str(s))
            return "\n".join(lines)
        elif action == "step":
            idx = args.get("index")
            status = args.get("status") or "done"
            if idx is None:
                return None
            return f"#plan step {int(idx)} {status}"
        elif action == "done":
            return "#plan done"
        elif action == "clear":
            return "#plan clear"
        elif action == "view":
            return "#plan"
        return None
    else:
        return None

def _ensure_tool_pairing(internal):
    """★ 防御性修复（400 insufficient tool messages）：
    确保 internal 中每个带 tool_calls 的 assistant 消息后面都有对应的 tool 响应。
    任何路径（取消/中断/异常/超时）导致工具调用未收到结果时，下次调用 API 前
    自动补齐占位 tool 消息，避免 API 报
    "An assistant message with 'tool_calls' must be followed by tool messages"。
    返回是否修改过。"""
    changed = False
    i = 0
    while i < len(internal):
        m = internal[i]
        if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'):
            ids = [tc.get('id') for tc in m['tool_calls'] if isinstance(tc, dict) and tc.get('id')]
            if ids:
                responded = set()
                k = i + 1
                while k < len(internal) and isinstance(internal[k], dict) and internal[k].get('role') == 'tool':
                    cid = internal[k].get('tool_call_id')
                    if cid in ids:
                        responded.add(cid)
                    k += 1
                missing = [cid for cid in ids if cid not in responded]
                if missing:
                    inserts = [{"role": "tool", "tool_call_id": cid,
                                "content": "返回码: -1\n（该工具调用未收到返回结果，可能已取消，已自动补占位响应）"}
                               for cid in missing]
                    internal[i + 1:i + 1] = inserts
                    # 清除插入点之后与占位重复的游离 tool 响应（跨 user 等消息继续扫描到下一个 assistant 为止）
                    k2 = i + 1 + len(inserts)
                    while k2 < len(internal):
                        mm2 = internal[k2]
                        if isinstance(mm2, dict) and mm2.get('role') == 'tool':
                            if mm2.get('tool_call_id') in missing:
                                internal.pop(k2)
                                continue
                            k2 += 1
                            continue
                        if isinstance(mm2, dict) and mm2.get('role') == 'assistant':
                            break
                        k2 += 1
                    changed = True
                    i += len(inserts)
        i += 1
    return changed

def _llm_stream_call(internal, model, r_eff, thinking, tools, cancel_event, usage, emit_evt, **kwargs):
    # ★ 2026-10-14：thinking 落盘用的会话标识（调用方经 kwargs 传入；没有就记 unknown）
    _dump_sid = kwargs.get('session_id') or kwargs.get('sid') or 'unknown'
    """通用流式 LLM 调用。返回 (finish_reason, full_reasoning, full_content, tool_calls, cancelled, error)。
    tool_calls: list[SimpleNamespace(id, function)]；cancelled=True 表示已取消；error 为异常对象或 None。

    ★ 2026-09-21（修复“有子 agent 且记录多起来时前端神秘卡死”的根因）：
    原实现是 `for chunk in resp:` 直接遍历 OpenAI SDK 的流，而该迭代在底层 socket 上
    **没有读取超时**。一旦上游不再下发数据（长连接被中间设备掐断、服务端限流、并发子 agent
    抢占同一账号配额等），这个 for 就会永久阻塞：父循环既不调用模型也不产生任何事件，
    浏览器侧表现为“一瞬间所有东西都不动了、模型不再生成、标题卡在忙碌”，而页面滚动由
    合成线程负责所以依旧流畅——与用户描述完全一致。
    记录佐证：出问题的操作记录里 command_execute / command_result 完全配对（没有丢结果），
    主 agent 只是“戛然而止”；子 agent 会并发开启多条这样的长连接，因此子 agent 越多越容易撞上。

    现在改为：① 用后台线程推进原始迭代器、主线程带超时取块；超过 LLM_STALL_TIMEOUT 秒
    无任何数据即判定为“流卡死”，关闭连接并重试（最多 LLM_MAX_RETRIES 次），重试前先
    reset_container 让界面重开一块，避免半截内容与重试内容拼接；
    ② 只补发新增内容，不会把已显示的思考/正文重复推给前端；③ 周期性进度日志便于定位。
    """
    LLM_STALL_TIMEOUT = float(os.environ.get("LLM_STALL_TIMEOUT", "120"))
    LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))

    def _open_stream():
        # ★ 2026-10-14（多提供商）：先声明"本次调用用的是哪个模型" ——
        #   get_client() / provider_supports_thinking() 据此解析到该模型所属提供商的
        #   base_url / api_key（见 resolve_model / set_current_model）。
        set_current_model(model)
        # ★ 2026-10-10：thinking / reasoning_effort 是 DeepSeek 专有字段。
        #   接通用 OpenAI 兼容端点（如本地 Ollama）时必须省掉，否则对方 400：
        #   「"qwen2.5:3b" does not support thinking」。详见 provider_supports_thinking()。
        _use_ds = provider_supports_thinking()
        _extra = {"thinking": {"type": "enabled" if thinking else "disabled"}} if _use_ds else None
        _basic = dict(model=model, messages=internal, tools=tools, stream=True)
        if _use_ds:
            _basic["reasoning_effort"] = r_eff
            _basic["extra_body"] = _extra
        try:
            return get_client().chat.completions.create(
                stream_options={"include_usage": True}, **_basic)
        except TypeError:
            # 旧版 SDK 不支持 stream_options
            return get_client().chat.completions.create(**_basic)

    # 累计量在每个尝试内部重置（见下方循环开头）；此处声明供循环外返回路径使用
    full_reasoning = ""
    full_content = ""
    tool_calls_acc = {}
    tool_calls_order = []
    finish_reason = None
    reasoning_start = None
    reasoning_end = None
    # ★ 2026-09-21：本函数新增两个可选参数（供会话属性面板统计时间/按模型计费）。
    #   sid：会话 ID（可选，None 表示只做调用不记统计）；t_start：本次调用开始时间。
    sid = kwargs.get("sid")
    t_start = kwargs.get("t_start") or time.time()
    _first_chunk_at = None
    _last_chunk_at = None  # 最后一个数据块（含思考/正文），用于整体吞吐 gen_ms
    _out_start = None      # 第一个正文 token 的时间（用于算正文输出窗口）
    _out_end = None

    try:
        # ★ 兜底防御：任何历史原因导致 tool_calls 未配对，先补齐再调用 API
        _ensure_tool_pairing(internal)

        last_err = None
        for _attempt in range(1, LLM_MAX_RETRIES + 1):
            resp = None
            q = queue.Queue()
            _SENTINEL = object()

            def _pump(r, qq=q, sent=_SENTINEL):
                """单条流的读取线程：只在这里做阻塞读，把结果推入队列；
                主线程据此做超时判断，从而能真正检测到“长时间无数据”。"""
                try:
                    for _c in r:
                        qq.put(_c)
                except BaseException as _e:      # noqa: BLE001
                    qq.put(_e)
                finally:
                    qq.put(sent)

            stalled = False
            # ★ 每次尝试都从零开始累计：卡死重试时本次未送出的累计量直接丢弃，
            #   否则“第 1 次已累计 + 第 2 次重新生成”会拼成重复/错乱的内容。
            full_reasoning = ""
            full_content = ""
            sent_reasoning = 0
            sent_content = 0
            tool_calls_acc = {}
            tool_calls_order = []
            finish_reason = None
            reasoning_start = None
            reasoning_end = None
            # ★ 2026-09-21：计时起点也随重试一起归零。
            #   否则重试后仍沿用「第 1 次尝试」的起点，算出的耗时/速度窗口偏小
            #   （tok/s 会被高估）。
            _first_chunk_at = None
            _last_chunk_at = None
            _out_start = None
            _out_end = None
            try:
                resp = _open_stream()
                it = iter(resp)
                threading.Thread(target=_pump, args=(it,), daemon=True).start()
                last_progress = time.time()

                while True:
                    if cancel_event.is_set():
                        emit_evt({"type": "cancelled"})
                        return None, "", "", [], True, None
                    try:
                        chunk = q.get(timeout=1.0)
                    except queue.Empty:
                        waited = time.time() - last_progress
                        if waited > LLM_STALL_TIMEOUT:
                            stalled = True
                            logging.warning(
                                f"[LLM] 流式响应 {LLM_STALL_TIMEOUT:.0f}s 无数据（第 {_attempt} 次尝试），"
                                f"判定为卡死并重建连接（本次已收到 reasoning={len(full_reasoning)} "
                                f"content={len(full_content)} 字符）")
                            break
                        if int(waited) % 30 == 0:
                            logging.info(f"[LLM] 等待流式数据中…（已等待 {waited:.0f}s）")
                        continue
                    last_progress = time.time()
                    # ★ 2026-09-21：首个 chunk 到达时间 → 首 token 平均延迟；每个 chunk 更新末尾时间
                    if _first_chunk_at is None:
                        _first_chunk_at = last_progress
                    _last_chunk_at = last_progress

                    if chunk is _SENTINEL:
                        break
                    if isinstance(chunk, BaseException):
                        raise chunk

                    # ---- 正常分块处理（只补发新增内容，兼容重试） ----
                    if getattr(chunk, 'usage', None):
                        u = chunk.usage
                        usage["prompt_tokens"] += u.prompt_tokens
                        usage["completion_tokens"] += u.completion_tokens
                        usage["total_tokens"] += u.total_tokens
                        _cached = getattr(getattr(u, 'prompt_tokens_details', None), 'cached_tokens', 0)
                        usage["cached_tokens"] += _cached
                        # ★ 真实 usage：本次调用的输入 token 即当前上下文占用
                        usage["ctx_tokens"] = u.prompt_tokens
                        # ★ 按模型分别累计（修复“中途 Pro→Flash 导致金额变少”）
                        if sid:
                            _acc_model_usage(sid, model, u.prompt_tokens, _cached, u.completion_tokens)
                            with sessions_lock:
                                _st = _sess_stats(sid)
                                if _st is not None:
                                    _st["output_tokens"] = int(_st.get("output_tokens") or 0) + int(u.completion_tokens or 0)
                        continue
                    if not getattr(chunk, 'choices', None):
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    reasoning = getattr(delta, 'reasoning_content', None)
                    if reasoning:
                        if reasoning_start is None:
                            reasoning_start = time.time()
                        reasoning_end = time.time()
                        full_reasoning += reasoning
                        if len(full_reasoning) > sent_reasoning:
                            _chunk = full_reasoning[sent_reasoning:]
                            emit_evt({"type": "thinking", "content": _chunk})
                            # ★ 2026-10-14（按用户要求把 thinking 落盘，排查"前端显示不完整"）：
                            #   记录**实际发出**的片段；与前端 DOM 渲染结果对照即可定位丢失环节。
                            try:
                                thinking_dump_write(_dump_sid, "chunk", _chunk)
                            except Exception:
                                pass
                            sent_reasoning = len(full_reasoning)
                        continue
                    content = delta.content
                    if content:
                        full_content += content
                        if _out_start is None:
                            _out_start = time.time()   # 第一个正文 token（输出速度计时起点）
                        _out_end = time.time()
                        # ★ 2026-09-29（按用户要求，最终方案）：
                        #   模型把工具调用写成正文（DSML / 伪 XML 标记）时，这段文本**照常推给前端**，
                        #   不做源头拦截。曾试过“检测到就扣住不推”，但正文是逐 token 推的、
                        #   标记要读到 '>' 才成形，要拦住就得暂扣字符，实现复杂且会牵连正常流式输出；
                        #   用户明确要求放行。乱码的补救交给下面的重试逻辑（_detect_bad_tool_call）。
                        if len(full_content) > sent_content:
                            emit_evt({"type": "content_delta", "content": full_content[sent_content:]})
                            sent_content = len(full_content)
                        continue
                    if getattr(delta, 'tool_calls', None):
                        for tc in delta.tool_calls:
                            idx = getattr(tc, 'index', 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {"id": tc.id or "", "name": "", "args": ""}
                                tool_calls_order.append(idx)
                            if tc.id:
                                tool_calls_acc[idx]["id"] = tc.id
                            fn = getattr(tc, 'function', None)
                            if fn:
                                if fn.name:
                                    tool_calls_acc[idx]["name"] = fn.name
                                if fn.arguments:
                                    tool_calls_acc[idx]["args"] += fn.arguments
                        continue
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
            finally:
                # 关闭底层连接，唤醒仍阻塞在 socket 读取上的 pump 线程（daemon，不阻止进程退出）
                try:
                    if resp is not None and hasattr(resp, "close"):
                        resp.close()
                except Exception:
                    pass

            if not stalled:
                break   # 正常结束

            # ---- 流卡死：重试 ----
            last_err = RuntimeError(f"流式响应 {LLM_STALL_TIMEOUT:.0f}s 无数据（疑似连接被中断）")
            if _attempt >= LLM_MAX_RETRIES:
                logging.error(f"[LLM] 已重试 {LLM_MAX_RETRIES} 次仍卡死，放弃本轮")
                break
            # ★ 重试会整段重新生成：先让界面重开一块，避免半截内容与重试内容拼接显示
            if full_reasoning or full_content or tool_calls_acc:
                try:
                    emit_evt({"type": "reset_container"})
                except Exception:
                    pass
            time.sleep(1.0)
            logging.warning(f"[LLM] 准备第 {_attempt + 1} 次重试（已重置本地累计量）")

        if last_err is not None and not full_content and not tool_calls_acc:
            # 多次重试仍无任何产出：把错误交给上层（界面会提示），避免静默卡住
            raise last_err

        # ★ 思考结束事件：纯思考时长（最后一个思考 chunk 到第一个思考 chunk，不含正文/工具调用时间）
        if full_reasoning and reasoning_start is not None and reasoning_end is not None:
            emit_evt({"type": "thinking_done", "duration": round(reasoning_end - reasoning_start, 1)})
            # ★ 落盘：该轮思考结束 —— 记下完整长度，供与前端渲染长度对照
            try:
                thinking_dump_write(_dump_sid, "done", None,
                                    {"full_len": len(full_reasoning), "sent_len": sent_reasoning})
            except Exception:
                pass

        # ★ 2026-09-21：会话属性面板的时间统计
        if sid:
            _t_end = time.time()
            _acc_time_stat(sid, "llm_ms", (_t_end - t_start) * 1000.0)
            if _first_chunk_at is not None:
                _acc_first_token(sid, (_first_chunk_at - t_start) * 1000.0)
            if _out_start is not None and _out_end is not None and _out_end > _out_start:
                with sessions_lock:
                    _st = _sess_stats(sid)
                    if _st is not None:
                        _st["output_ms"] = float(_st.get("output_ms") or 0) + (_out_end - _out_start) * 1000.0
            # ★ BUGFIX(2026-09-21)：输出速度的分子分母必须同口径。
            #   原来 output_tokens 用的是**全部** completion_tokens（含思考阶段），
            #   而 output_ms 只统计「正文首块→正文末块」的窗口，于是思考很长、
            #   正文很短时算出的速度离谱（实测出现过 5451 tok/s）。
            #   现在另记 gen_ms = 首个数据块 → 最后一个数据块（覆盖思考+正文），
            #   速度一律用 gen_ms 计算，这才是“整体吞吐”。
            if _first_chunk_at is not None and _last_chunk_at is not None and _last_chunk_at > _first_chunk_at:
                with sessions_lock:
                    _st2 = _sess_stats(sid)
                    if _st2 is not None:
                        _st2["gen_ms"] = float(_st2.get("gen_ms") or 0) + (_last_chunk_at - _first_chunk_at) * 1000.0

        tool_calls = []
        if tool_calls_acc:
            for idx in tool_calls_order:
                t = tool_calls_acc[idx]
                tool_calls.append(types.SimpleNamespace(
                    id=t["id"],
                    function=types.SimpleNamespace(name=t["name"], arguments=t["args"])
                ))
        return finish_reason, full_reasoning, full_content, tool_calls, False, None
    except Exception as e:
        _em = str(e)
        # ★ FIX(2026-09-11 v2)：超限类错误静默返回（由上层自动裁剪重试，不向界面发提示）
        if ("maximum context length" in _em) or ("context_length_exceeded" in _em):
            return None, "", "", [], False, e
        emit_evt({"type": "error", "content": _em})
        return None, "", "", [], False, e

def _handle_parent_ask_user(tool_call, session_id, emit, cancel_event, internal):
    """处理父 agent 的 ask_user 工具调用（仅主 agent 可用）。
    同步（默认）：向前端推送问题并阻塞等待用户作答，答案直接作为工具结果放入上下文；
    异步（async=true）：立即返回“已提交”结果，用户作答经 /api/ask_user_answer 以通知送达。"""
    try:
        args = json.loads(tool_call.function.arguments or "{}")
    except Exception:
        args = {}
    # ★ 2026-10-13（按用户要求）：支持一次多题 + 每题可多选。
    #   两种入参都接受：questions:[{question,options,multiple}] 或旧的 question/options。
    is_async = bool(args.get("async", False))
    _items = []
    _qs = args.get("questions")
    if isinstance(_qs, list):
        for _it in _qs:
            if not isinstance(_it, dict):
                continue
            _q = str(_it.get("question") or "").strip()
            _o = [str(x).strip() for x in (_it.get("options") or []) if str(x).strip()]
            if _q:
                _items.append({"question": _q, "options": _o, "multiple": bool(_it.get("multiple"))})
    if not _items:
        _q = str(args.get("question") or "").strip()
        _o = [str(x).strip() for x in (args.get("options") or []) if str(x).strip()]
        if _q:
            _items.append({"question": _q, "options": _o, "multiple": bool(args.get("multiple"))})
    if not _items:
        internal.append({"role": "tool", "tool_call_id": tool_call.id,
                         "content": "错误: ask_user 需要 question/options 或 questions 数组"})
        return
    _multi = len(_items) > 1
    # 每题一个 qid、一条事件（前端各自画一张卡片）
    entries = []
    for _i, _it in enumerate(_items, 1):
        _qid = str(uuid.uuid4())[:8]
        entries.append((_qid, _it))
        emit({"type": "ask_user", "task_id": _qid, "question": _it["question"],
              "options": _it["options"], "multiple": _it["multiple"],
              "index": _i, "total": len(_items), "group": _multi,
              "async": is_async})
    qid = entries[0][0]          # 兼容：单题时就是它自己；多题时用于结果事件
    if is_async:
        out = ("已向用户提交 %d 道问题（异步询问）。用户作答后会以“新通知”形式送达，"
               "你可以继续执行其他工作。" % len(entries)) if _multi else \
              "已向用户提交问题（异步询问）。用户作答后会以“新通知”形式送达，你可以继续执行其他工作。"
        for _qid, _it in entries:
            emit({"type": "ask_user_result", "task_id": _qid, "output": out, "returncode": 0})
        internal.append({"role": "tool", "tool_call_id": tool_call.id, "content": out})
        return
    # --- 同步：为**每一道题**注册等待者，全部答完才唤醒 ---
    _events = {}
    _holders = {}
    with sessions_lock:
        sess = sessions.get(session_id)
        if not sess:
            internal.append({"role": "tool", "tool_call_id": tool_call.id, "content": "会话丢失，无法等待用户作答。"})
            return
        _ce = sess.setdefault("command_events", {})
        for _qid, _it in entries:
            _ev = threading.Event()
            _hd = {}
            _events[_qid] = _ev
            _holders[_qid] = _hd
            _ce[_qid] = (_ev, _hd)
    result_event = _events[qid]      # 兼容下面的引用（等待循环会检查全部）
    result_holder = _holders[qid]
    start_wait = time.time()
    _hb_last = start_wait
    # ★ 2026-10-13：多题时**等全部题都作答**才唤醒（原来是只看第一题）
    def _all_answered():
        return all(ev.is_set() for ev in _events.values())
    while not _all_answered():
        if time.time() - start_wait > ASK_USER_WAIT_TIMEOUT:
            break
        if cancel_event.is_set():
            for _ev in _events.values():
                _ev.set()      # ★ 多题时全部唤醒，避免留下悬挂的等待者
            break
        # ★ 2026-10-04：同上 —— 提问等待最长可达 3600 秒，这期间必须持续发心跳，
        #   否则前端 150 秒看门狗会判定「模型流中断」并中止等待（用户会看到
        #   「服务器已 150 秒没有返回任何数据」）。heartbeat 前端只刷新时间戳。
        if time.time() - _hb_last >= 10:
            _hb_last = time.time()
            try:
                emit({"type": "heartbeat", "agent_id": "main", "waiting_ask_user": True})
            except Exception:
                pass
        time.sleep(0.2)
        with sessions_lock:
            if session_id not in sessions:
                break
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess and sess.get("command_events"):
            for _qid, _it in entries:
                sess["command_events"].pop(_qid, None)   # ★ 多题：逐个清理
    # ★ 会话属性面板：阻塞等待用户作答的耗时（单独统计，不计入工具耗时）
    _acc_time_stat(session_id, "ask_wait_ms", (time.time() - start_wait) * 1000.0)
    if cancel_event.is_set():
        internal.append({"role": "tool", "tool_call_id": tool_call.id, "content": "（已取消，用户未作答）"})
        return
    # ★ 2026-10-13：按题汇总。单题保持原格式（"用户作答：xxx"），多题按题号列出。
    _answered = [(q, _holders[q]) for q, _ in entries
                 if _holders[q].get("returncode") == 0]
    if not _multi:
        out = (f"用户作答：{result_holder.get('output', '')}"
               if result_holder.get("returncode") == 0
               else "用户长时间未作答（等待超时）。")
    elif _answered:
        _lines = []
        for _i, (_qid2, _it) in enumerate(entries, 1):
            _hd2 = _holders[_qid2]
            _ans = _hd2.get("output", "") if _hd2.get("returncode") == 0 else "（未作答/超时）"
            _lines.append(f"{_i}. {_it['question']} → {_ans}")
        _miss = len(entries) - len(_answered)
        out = "用户对 %d 道问题的作答：\n%s" % (len(entries), "\n".join(_lines))
        if _miss:
            out += f"\n（其中 {_miss} 道未作答/超时）"
    else:
        out = "用户长时间未作答（等待超时）。"
    emit({"type": "ask_user_result", "task_id": qid, "output": out, "returncode": 0})
    internal.append({"role": "tool", "tool_call_id": tool_call.id, "content": out})

# ---------- 父 agent 主循环（操作模式） ----------
def run_parent_loop(msgs, model, r_eff, thinking, session_id, resume_internal=None):
    """父 agent 循环：LLM 推理 → 工具执行（agent 管理命令服务端直处理，其余经浏览器中转）→
    子 agent 通知注入。所有事件推入会话共享队列，与子 agent 共用同一条 HTTP 连接。

    ★ 2026-10-05：新增 resume_internal —— 「恢复模式」重启。
      背景：用户点「停止生成」时，cancel_event 被置位，父循环 break 退出，
      而它持有的 internal（内部上下文）随线程消失 —— 此后会话虽然还在
      （事件流能重连、消息能投进队列），但**没有任何线程消费队列**，
      于是"停止后同一会话啥也发不出去"（用户实测）。
      恢复模式就是：拿回上次保存的 internal，跳过一次性的"处理初始消息"，
      直接进入"等队列 → 处理"的主循环。这样既接上了上下文，又不会重发旧消息。
    """
    _resume = resume_internal is not None
    if _resume:
        logging.info(f"[OPERATE] {session_id} 以恢复模式启动父循环（内部上下文 {len(resume_internal)} 条，不重发历史）")
    def emit(evt):
        evt = dict(evt)
        evt.setdefault("agent_id", "main")
        _emit(session_id, evt)

    cancel_event = threading.Event()
    wait_event = threading.Event()

    # ★ BUGFIX(2026-09-28)：这里以前在 `with sessions_lock:` 内部调用 emit()/ _finish_parent()，
    #   而两者都会再次获取 sessions_lock —— 普通 Lock 不可重入，于是**当场自锁死**，
    #   这把锁再也不会释放，之后所有需要它的接口（操作模式全部路径）永久卡死，
    #   而对话模式不用这把锁所以一切正常（用户看到的“操作模式全部超时、对话模式不受影响”）。
    #   修法：锁内只读写字典，出了锁再做 emit / _finish_parent。
    _sess_missing = False
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess:
            sess['cancel_event'] = cancel_event
            sess['wait_event'] = wait_event
            logging.info(f"[OPERATE] 会话 {session_id} 开始，绑定事件")
        else:
            _sess_missing = True
    if _sess_missing:
        emit({"type": "error", "content": "会话丢失"})
        _finish_parent(session_id)
        return

    # ★ 会话设置（来自前端设置弹窗）：工具开关 / 超时 / 截断 / 记忆列表上限等
    #   初始读取一次；★ 每次循环迭代都会重新读取，保证设置修改后实时生效
    #   （无需开启新会话）。
    with sessions_lock:
        sess = sessions.get(session_id)
        settings = dict(sess.get("settings") or {}) if sess else {}
    # ★ BUGFIX(2026-10-04)：服务端"等客户端回传命令结果"的超时，必须与客户端上限对齐。
    #
    #   问题（用户实测）：客户端 COMMAND_TIMEOUT=600s，而服务端这里硬编码只等 120s。
    #   一个跑 285 秒的脚本（每 15 秒 print 一次，不会触发客户端 30s 空闲杀）——
    #   客户端老老实实跑完并 POST 回结果，但服务端 120 秒就 break 了，
    #   还把这个等待位从 command_events 里 pop 掉，于是客户端回传时吃 404，
    #   结果静默丢失，模型只看到「命令结果丢失」。
    #
    #   现在：等待上限 = 用户设置里的 max_timeout（发给客户端的同步命令上限）+ 余量，
    #   保证"客户端还在跑"时服务端绝不会先放弃。
    _tool_wait_s = int(settings.get("max_timeout", 600) or 600) + 300
    logging.info(f"[OPERATE] {session_id} 服务端等待工具回传上限 = {_tool_wait_s}s"
                 f"（max_timeout={settings.get('max_timeout', 600)} + 300s 余量）")
    tools = build_parent_tools(settings)
    agent_system = build_agent_system(settings)
    list_limit = int(settings.get("list_limit", 50) or 50)
    last_call_ts = time.time()   # 兜底：usage 计价时间（正常在每次 LLM 调用前更新）
    _bad_tool_retry = 0          # ★ 模型把工具调用写成纯文本时的重试计数（见下方检测逻辑）
    logging.info(f"[OPERATE] 会话 {session_id} 工具分组设置: {json.dumps((settings.get('tools') or {}), ensure_ascii=False)}，启用工具数: {len(tools)}")

    # ★ 导入恢复：优先使用注入的完整历史上下文（含用户提问/思考/工具调用/结果/最终答复/子代理记录）
    injected = None
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess:
            injected = sess.pop("injected_context", None)
            if injected is not None:
                # 注入的上下文直接作为内部消息历史；会话先进入“已答复”空闲状态，
                # 等待用户下一条消息后才开始推理
                sess["final_answered"] = True

    # ★ plan 恢复：导入会话携带的任务计划推送给前端（左侧任务视图立即显示）
    #   注意：emit 内部会获取 sessions_lock，不能放在锁内调用（普通 Lock 嵌套会死锁）
    plan_to_push = None
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess and sess.get("plan"):
            plan_to_push = sess["plan"]
    if plan_to_push:
        emit({"type": "plan_update", "plan": plan_to_push})

    user_content = None
    # ★ 2026-10-03（按用户要求）：重建路径**也走结构化**，不再把历史压成一段文本。
    #
    #   为什么要改：导入路径用的是 injected_context（结构化消息原样当 internal），
    #   而重建路径以前把消息拼成「之前的对话记录：用户: … 助手: …」这样一段文本。
    #   两者不一致带来两个问题：
    #     ① 工具调用的 arguments 在拼字符串时被丢掉 ——
    #        而 write_file / #write 的**文件全文就在 arguments 里**，
    #        于是模型重启后只记得"我写过某个文件"，看不到内容；
    #     ② 结构化信息（role / tool_call_id 配对）全部降级成文本，模型更难区分
    #        "谁说的"、"哪条结果对应哪次调用"。
    #
    #   现在：只要前端发来的 messages 是结构化且合法的（tool_calls 与 tool 结果配对），
    #   就直接当作 internal 的历史部分使用（与导入完全同一条路）。
    #   校验不通过时**退回**原来的文本拼接（保底，绝不因为结构问题把会话搞挂）。
    rebuilt_history = None
    if resume_internal is None and injected is None:
        real = [m for m in msgs if m['role'] != 'system']
        if not real:
            emit({"type": "error", "content": "没有用户消息"})
            _finish_parent(session_id)
            return

        # ---- 先尝试结构化重建 ----
        try:
            _hist = real[:-1]      # 最后一条 = 本条新消息，单独作为当前任务
            _last = real[-1]
            _ok_struct = True
            _open_calls = set()
            _built = []
            for _m in _hist:
                _role = _m.get('role')
                if _role not in ('user', 'assistant', 'tool'):
                    _ok_struct = False
                    break
                _item = {"role": _role}
                _c = _m.get('content')
                if isinstance(_c, list):
                    _c = " ".join(str(p.get('text', '')) for p in _c if isinstance(p, dict))
                # tool 消息必须能配对到前面的 tool_calls
                if _role == 'tool':
                    _cid = _m.get('tool_call_id')
                    if not _cid or _cid not in _open_calls:
                        _ok_struct = False
                        break
                    _open_calls.discard(_cid)
                    _item["tool_call_id"] = _cid
                    _item["content"] = "" if _c is None else str(_c)
                    _built.append(_item)
                    continue
                _tcs = _m.get('tool_calls')
                if _role == 'assistant':
                    _item["reasoning_content"] = str(_m.get('reasoning_content') or "")
                    if _tcs:
                        _clean = []
                        for _tc in _tcs:
                            _fn = (_tc or {}).get('function') or {}
                            _nm = _fn.get('name')
                            if not _nm:
                                continue
                            _args = _fn.get('arguments')
                            if isinstance(_args, (dict, list)):
                                _args = json.dumps(_args, ensure_ascii=False)
                            _tid = _tc.get('id') or ("call_rb_%d" % (len(_built) + 1))
                            # ★ 2026-10-06：超大参数在这里就裁掉（见 _cap_tool_args 的说明）。
                            #   这是"上下文暴涨"的入口：模型可能把整份文档塞进命令参数。
                            # ★ 2026-10-12：改用 _cap_tool_args_smart —— 由参数里的
                            #   命令前缀决定上限。保真类（#mem_save/#write/#replace/#load）
                            #   给宽松上限，否则会把要落盘的数据截断（实测记忆文件最大 6.3MB）。
                            _args = _cap_tool_args_smart("" if _args is None else _args)
                            _clean.append({"id": _tid, "type": "function",
                                           "function": {"name": _nm,
                                                        "arguments": _args}})
                            _open_calls.add(_tid)
                        if _clean:
                            _item["tool_calls"] = _clean
                            # 有 tool_calls 时 content 可为 null
                            _item["content"] = _c if (_c and str(_c).strip()) else None
                        else:
                            _item["content"] = "" if _c is None else str(_c)
                    else:
                        _item["content"] = "" if _c is None else str(_c)
                else:
                    _item["content"] = "" if _c is None else str(_c)
                _built.append(_item)
            # 结束时有未闭合的 tool_calls → 结构不完整，退回文本拼接
            if _open_calls:
                _ok_struct = False
            if _ok_struct and _built:
                rebuilt_history = _built
                logging.info(f"[OPERATE] {session_id} 结构化重建历史成功，消息数 {len(_built)}")
            else:
                logging.info(f"[OPERATE] {session_id} 结构化重建校验未通过（未闭合调用 "
                             f"{len(_open_calls)} 个），退回文本拼接")
        except Exception as _e:
            logging.warning(f"[OPERATE] {session_id} 结构化重建异常，退回文本拼接: {_e}")
            rebuilt_history = None

        # ---- 退回：把历史压成一段文本（保底路径）----
        if rebuilt_history is None:
            # ★ BUGFIX(2026-10-03)：压成文本时**必须带上工具调用的参数**。
            #   以前只取 m['content']，于是带 tool_calls 的 assistant（content=null）
            #   被拼成「助手: None」、tool 结果被标成「助手: …」、
            #   **工具调用的 arguments 被完全丢弃**（文件全文就在里面）。
            def _hist_line(m):
                role = m.get('role')
                content = m.get('content')
                if isinstance(content, list):
                    content = " ".join(str(p.get('text', '')) for p in content if isinstance(p, dict))
                content = "" if content is None else str(content)
                if role == 'user':
                    return f"用户: {content}"
                if role == 'tool':
                    return f"工具结果: {content}"
                parts = []
                if content.strip():
                    parts.append(f"助手: {content.strip()}")
                for tc in (m.get('tool_calls') or []):
                    fn = tc.get('function') or {}
                    name = fn.get('name') or '?'
                    args = fn.get('arguments')
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args, ensure_ascii=False)
                    args = "" if args is None else str(args)
                    parts.append(f"工具调用: {name}({args})")
                if not parts:
                    return "助手: (无输出)"
                return "\n".join(parts)

            context = "\n".join(_hist_line(m) for m in real[:-1])
            _last_content = real[-1].get('content')
            if isinstance(_last_content, list):
                _last_content = " ".join(str(p.get('text', '')) for p in _last_content if isinstance(p, dict))
            current_task = "" if _last_content is None else str(_last_content)
            user_content = f"之前的对话记录：\n{context}\n\n当前任务：{current_task}" if context else current_task



    # ★ 图像理解：初始消息附带的图片（会话创建时随 /chat 传入）注入 user 消息；
    #   同时落盘过的 pending_images（复用会话时传入）也在下一条消息消费前注入。
    initial_images = []
    initial_files = []
    with sessions_lock:
        _sess_i = sessions.get(session_id)
        if _sess_i:
            initial_images = list(_sess_i.pop("initial_images", None) or [])
            initial_files = list(_sess_i.pop("initial_files", None) or [])
            if _sess_i.get("model"):
                model = _sess_i["model"]
    if resume_internal is not None:
        # ★ 恢复模式：**直接接手会话上那个 internal 对象本身**（不拷贝）。
        #   它就是上一轮父循环在用的同一个 list（会话里一直存着 internal_ref），
        #   所以历史、工具参数、图片全都原样保留，接着往下聊完全连续。
        internal = resume_internal
        user_content = None
        real = []
        with sessions_lock:
            _rs = sessions.get(session_id)
            if _rs is not None:
                _rs["final_answered"] = True    # 进入空闲，等用户下一条消息
    elif injected is not None:
        internal = [{"role": "system", "content": agent_system}] + injected
        logging.info(f"[OPERATE] {session_id} 使用导入上下文，消息数 {len(injected)}")
    elif rebuilt_history:
        # ★ 2026-10-03（按用户要求）：重建也走结构化 —— 与导入同一条路。
        #   历史原样作为 internal，最后一条（本条新消息）拼在末尾。
        _last_content = real[-1].get('content')
        if isinstance(_last_content, list):
            _last_content = " ".join(str(p.get('text', '')) for p in _last_content if isinstance(p, dict))
        _cur_text = "" if _last_content is None else str(_last_content)
        internal = [{"role": "system", "content": agent_system}] + rebuilt_history
        internal.append({"role": "user",
                         "content": _build_user_content_with_images(
                             _file_ctx(initial_files) + _cur_text,
                             [{"data_url": u} for u in initial_images])})
        logging.info(f"[OPERATE] {session_id} 使用结构化重建上下文，历史 {len(rebuilt_history)} 条 + 当前 1 条")
    else:
        _init_text = _file_ctx(initial_files) + user_content
        internal = [{"role": "system", "content": agent_system},
                    {"role": "user", "content": _build_user_content_with_images(_init_text, [{"data_url": u} for u in initial_images])}]

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "ctx_tokens": 0}
    # ★ 2026-09-21：把 internal / usage 的引用挂到会话上，供「会话属性」面板读取
    #   （只读展示用：上下文组成部分、最近一次真实 ctx_tokens 等）
    with sessions_lock:
        _s_ref = sessions.get(session_id)
        if _s_ref is not None:
            _s_ref["internal_ref"] = internal
            _s_ref["usage_ref"] = usage
    # ★ BUGFIX(2026-09-21)：导入会话**不再**用导出记录里的累计用量做初始化。
    #   那些字段（prompt_tokens/cached_tokens/...）是**导出前所有轮次累加的历史总和**，
    #   把它当作本次会话的起点后，每发一条消息都会「以历史总和为基数再加本次用量」，
    #   于是「累计输入」按几何级数膨胀（实测导出记录里已经出现 4654 万 prompt_tokens），
    #   缓存命中率、费用、上下文占用率也跟着一起失真——这就是“导入后上下文占用多出许多”。
    #   正确的做法：用量只统计**本次会话真正发生过的调用**；历史总量由面板按需从
    #   导入记录里读取展示，绝不再参与累加。ctx_tokens 则由下一次真实调用自动校准。
    # ★ FIX(2026-09-11 v2 按用户要求)：导入上下文若超限，不再提示，直接丢弃最早的内容（保留最近部分）。
    if injected is not None:
        _est_inj = _estimate_tokens_messages(injected)
        if _est_inj > 0:
            if _est_inj > CTX_SEND_LIMIT_EST:
                _dropped_inj = _trim_internal_to_fit(internal)
                _est_now = _estimate_tokens_messages(internal)
                usage["ctx_tokens"] = _est_now
                logging.info(f"[OPERATE] {session_id} 导入上下文估算 {_est_inj} tokens 超限，已自动丢弃最早 {_dropped_inj} 条消息（裁剪后约 {_est_now} tokens）")
            else:
                usage["ctx_tokens"] = _est_inj

    def wait_client(cmd_str, display=None, orig_args=None):
        """把命令经浏览器中转给客户端执行并同步等待结果（用于 local 记忆操作）。
        返回 (output, returncode)。★ command 字段必须为真实执行命令（#mem_*），
        display 仅用于界面标题（#save/#delete/#list/#load），避免前端把 #save 当 shell 执行。"""
        task_id = str(uuid.uuid4())[:8]
        show_cmd = display or cmd_str
        emit({"type": "command_execute", "task_id": task_id, "command": cmd_str,
              "original_args": orig_args,
              "command_title": _cmd_title(show_cmd)})
        result_event = threading.Event()
        result_holder = {}
        with sessions_lock:
            sess = sessions.get(session_id)
            if not sess:
                return "会话丢失", -1
            sess.setdefault("command_events", {})[task_id] = (result_event, result_holder)
        start_wait = time.time()
        _hb_last = start_wait
        while not result_event.is_set():
            if time.time() - start_wait > _tool_wait_s:
                break
            if cancel_event.is_set():
                result_event.set()
                break
            # ★ 2026-10-04：等待期间每 10 秒补发一次心跳。
            #   工具可能跑几十秒到几分钟，这段期间如果不发任何事件，
            #   前端的 150 秒看门狗会误判「模型流中断」并中止本轮等待。
            #   heartbeat 前端只用于刷新时间戳，不渲染、不写入导出记录。
            if time.time() - _hb_last >= 10:
                _hb_last = time.time()
                try:
                    emit({"type": "heartbeat", "agent_id": "main", "waiting_tool": True})
                except Exception:
                    pass
            time.sleep(0.2)
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess:
                sess["command_events"].pop(task_id, None)
        out = result_holder.get("output", "命令结果丢失") if result_holder else "命令结果丢失"
        ret = result_holder.get("returncode", -1) if result_holder else -1
        emit({"type": "command_result", "task_id": task_id, "command": show_cmd,
              "output": _event_view_image_output(show_cmd, out), "returncode": ret, "command_title": _cmd_title(show_cmd)})
        return out, ret

    def send_user_message(msg, kind="user", images=None, files=None):
        # ★ 2026-09-29（防御）：msg 必须是字符串。此前调用方误传 dict 时，
        #   下一行的 `_file_ctx(files) + msg` 会抛 TypeError 并**打死父循环线程**，
        #   表现为整个会话突然不动了。这里统一转成字符串，事件 kind 也收敛到已知通道。
        if isinstance(msg, dict):
            _d = msg
            msg = _d.get("text") or ""
            if kind == "user" and _d.get("kind"):
                kind = _d["kind"]
        msg = "" if msg is None else str(msg)
        if kind not in MSG_CHANNELS:
            kind = "agent_return" if kind != "user" else "user"
        # ★ 事件携带图片（供前端在用户气泡中渲染缩略图）；文档内容只注入模型上下文
        emit({"type": "user_message", "content": msg, "kind": kind})
        text = _file_ctx(files or []) + msg
        internal.append({"role": "user", "content": _build_user_content_with_images(text, [{"data_url": u} for u in (images or [])])})

    try:
        _ctx_retry_n = 0   # 上下文超限静默重试计数
        while True:
            if cancel_event.is_set():
                logging.info(f"[OPERATE] {session_id} 被取消")
                emit({"type": "cancelled"})
                break

            # ★ 每次迭代刷新会话设置（工具开关/超时/截断/记忆列表上限），
            #   前端修改设置后无需新开会话即可实时生效。
            with sessions_lock:
                sess = sessions.get(session_id)
                new_settings = dict(sess.get("settings") or {}) if sess else {}
                new_model = (sess.get("model") or "") if sess else ""
                new_r_eff = (sess.get("reasoning_effort") or "") if sess else ""
                new_think = sess.get("thinking_enabled") if sess else None
            if new_model and new_model != model:
                model = new_model
                logging.info(f"[OPERATE] {session_id} 模型已刷新为 {model}")
            # ★ FIX(2026-09-11 v4)：思考强度/思考开关同样每轮刷新（中途切换即时生效）
            if new_r_eff and new_r_eff != r_eff:
                r_eff = new_r_eff
                logging.info(f"[OPERATE] {session_id} 思考强度已刷新为 {r_eff}")
            if new_think is not None and new_think != thinking:
                thinking = new_think
                logging.info(f"[OPERATE] {session_id} 思考开关已刷新为 {thinking}")
            if new_settings != settings:
                settings = new_settings
                tools = build_parent_tools(settings)
                agent_system = build_agent_system(settings)
                list_limit = int(settings.get("list_limit", 50) or 50)
                # 同步更新 system 提示词（internal 首条消息）
                if internal and internal[0].get("role") == "system":
                    internal[0]["content"] = agent_system
                logging.info(f"[OPERATE] {session_id} 设置已刷新: 工具数 {len(tools)}，list_limit={list_limit}")
            # ★ 提示词设置独立刷新（服务器“提示词”可能变化，无需等 settings 变化）
            _sys_new = build_agent_system(settings)
            if _sys_new != agent_system:
                agent_system = _sys_new
                if internal and internal[0].get("role") == "system":
                    internal[0]["content"] = agent_system
                logging.info(f"[OPERATE] {session_id} 系统提示词已刷新（提示词设置变更）")

            # 检查消息队列 / 通知队列
            with sessions_lock:
                sess = sessions.get(session_id)
                if not sess:
                    logging.info(f"[OPERATE] {session_id} 会话已消失")
                    break
                user_msgs = None
                notifications = None
                busy = sess.get('busy', False)
                # ★ 2026-09-29：统一消息队列（取代原来 user_message_queue / wait_idle_queue /
                #   pending_notifications 三套并存的机制）。条目形如
                #   {"text":..., "kind":"user|timer|agent_return|ask_user", "want_idle":bool}
                #
                #   取件规则：
                #     busy=True  → 什么都不取（当前 agent 循环还在跑，等它结束）；
                #     busy=False → 取 want_idle=False 的「插话」条目（立刻就办）；
                #                  再取 want_idle=True 且 final_answered 的「排队」条目
                #                  （final_answered 表示模型已给出最终答复、任务真正结束）。
                #   两类可能同时取出，下面按 kind 分别注入。
                picked = []
                if not busy:
                    _idle_ok = bool(sess.get('final_answered'))
                    q = sess.get('msg_queue') or []
                    picked = [it for it in q
                              if (not it.get('want_idle')) or _idle_ok]
                    if picked:
                        for it in picked:
                            try:
                                q.remove(it)
                            except ValueError:
                                pass
                        user_msgs = [it for it in picked if (it.get('kind') or 'user') == 'user']
                        notifications = [it for it in picked
                                         if (it.get('kind') or 'user') != 'user']
                        logging.info(f"[OPERATE] {session_id} 取出消息 {len(picked)} 条"
                                     f"（用户 {len(user_msgs)} / 通知 {len(notifications)}）")

            if user_msgs:
                logging.info(f"[OPERATE] {session_id} 处理用户消息 {len(user_msgs)} 条，发送 reset_container 事件")
                _bad_tool_retry = 0   # ★ 新的一轮用户消息：重置“错误工具名”重试计数
                emit({"type": "reset_container"})
                # ★ 兜底：复用会话时随 /chat 传入的图片/文档（pending_*）附加到本轮消息
                with sessions_lock:
                    _ss = sessions.get(session_id)
                    pending_imgs = list((_ss.pop("pending_images", None) or [])) if _ss else []
                    pending_files = list((_ss.pop("pending_files", None) or [])) if _ss else []

                # ★ 2026-10-04（回退说明）：这里曾经把一次取出的多条用户消息**合并成一条**，
                #   依据是当时一句「排队发送应该整合起来一起发」。**那个改动是错的，已撤销**：
                #   用户实测「我发了 1 2 3，前端咋都给我合并了，还放到下面了」——
                #   合并会让三条独立的消息变成一条（历史错乱、气泡位置也不对）。
                #   用户消息**不得合并**，一条就是一条，各自独立注入。
                for raw_msg in user_msgs:
                    # ★ 队列项兼容：字符串（纯文本）或 {"text":..., "images":[...], "files":[...]}
                    if isinstance(raw_msg, dict):
                        msg = raw_msg.get("text") or ""
                        msg_images = raw_msg.get("images") or []
                        msg_files = raw_msg.get("files") or []
                    else:
                        msg = raw_msg
                        msg_images = []
                        msg_files = []
                    if not msg_images and pending_imgs:
                        msg_images = pending_imgs
                        pending_imgs = []
                    if not msg_files and pending_files:
                        msg_files = pending_files
                        pending_files = []
                    # 上下文压缩：走独立通道（会把 internal 重建为 [system, 摘要]）
                    if msg.strip().startswith("#compress"):
                        summary = msg.strip()[len("#compress"):].strip()
                        out, ret = _do_compress({"session_id": session_id, "internal": internal,
                                                 "is_parent": True, "agent": None, "emit": emit,
                                                 "usage": usage}, summary)
                        if ret == 0:
                            emit({"type": "user_message", "content": f"🧹 用户执行上下文压缩：{summary[:200]}"})
                        else:
                            emit({"type": "error", "content": out})
                        continue
                    send_user_message(msg, "user", msg_images, msg_files)
                with sessions_lock:
                    if sess:
                        sess['final_answered'] = False
                        sess['busy'] = True
                continue

            if notifications:
                # ★ 2026-09-29：通知必须带自己的 kind 注入（前端据此新建容器、不当作你发的消息）。
                #   以前这里把所有通知拼成一条 "🔔 新通知：..." 并以 kind='user' 发送，
                #   导致模型把通知当成用户说的话 —— 那些乱象的根源之一。
                for n in notifications:
                    if not isinstance(n, dict):
                        n = {"text": str(n), "kind": "agent_return"}
                    kind = n.get("kind") or "agent_return"
                    text = n.get("text") or ""
                    logging.info(f"[OPERATE] {session_id} 注入通知 kind={kind}")
                    send_user_message(text, kind)
                with sessions_lock:
                    if sess:
                        sess['final_answered'] = False
                        sess['busy'] = True
                continue

            # 空闲等待逻辑
            need_wait = False
            with sessions_lock:
                sess = sessions.get(session_id)
                if not sess:
                    logging.info(f"[OPERATE] {session_id} 会话丢失，退出")
                    break
                if sess.get('final_answered') and not sess.get('msg_queue'):
                    need_wait = True
                    logging.info(f"[OPERATE] {session_id} 进入空闲等待")

            if need_wait:
                while not wait_event.wait(timeout=1):
                    if cancel_event.is_set():
                        logging.info(f"[OPERATE] {session_id} 等待中被取消")
                        emit({"type": "cancelled"})
                        break
                if cancel_event.is_set():
                    break
                wait_event.clear()
                logging.info(f"[OPERATE] {session_id} 被唤醒")
                continue

            # 最终退出条件
            with sessions_lock:
                sess = sessions.get(session_id)
                if sess and sess.get('final_answered') and not sess.get('msg_queue'):
                    logging.info(f"[OPERATE] {session_id} 最终退出，无待处理消息")
                    break

            with sessions_lock:
                if sess:
                    sess['busy'] = True
                    logging.info(f"[OPERATE] {session_id} 设置 busy=True")

            if cancel_event.is_set():
                emit({"type": "cancelled"})
                break

            # ★ 上下文占用检查：≥50% 时自动向模型推送压缩警告（基于真实 usage，仅一次，压缩后重置）
            with sessions_lock:
                _csess = sessions.get(session_id)
            if _csess:
                _maybe_warn_context(session_id, internal, _csess, emit, usage)

            # ★ 2026-09-21（按用户要求）：发送前若上下文超限，自动从最早处丢弃内容直到适配；
            #   不再提示“无法发送”。特别大单条继续做尾部截取/大幅截断。
            _est = _estimate_tokens_messages(internal)
            if _est >= CTX_SEND_LIMIT_EST:
                _trim_rounds = 0
                while _trim_rounds < 30:
                    _trim_rounds += 1
                    _dropped_pre = _trim_internal_to_fit(internal)
                    _est_after = _estimate_tokens_messages(internal)
                    if _est_after < CTX_SEND_LIMIT_EST:
                        break
                    if _truncate_last_message_to_fit(internal):
                        _est_after = _estimate_tokens_messages(internal)
                        if _est_after < CTX_SEND_LIMIT_EST:
                            break
                        continue
                    if _hard_shrink_last_message(internal):
                        _est_after = _estimate_tokens_messages(internal)
                        if _est_after < CTX_SEND_LIMIT_EST:
                            break
                        continue
                    if _dropped_pre <= 0:
                        break
                logging.info(f"[OPERATE] {session_id} 发送前上下文超限：已自动从最早处裁剪 {_trim_rounds} 轮（现约 {_estimate_tokens_messages(internal)} tokens）")
                usage["ctx_tokens"] = _estimate_tokens_messages(internal)

            # 调用大模型（流式输出）
            try:
                logging.info(f"[OPERATE] {session_id} 调用大模型（流式），模型: {model}，思考强度: {r_eff}，思考开关: {thinking}，当前消息数: {len(internal)}")
                last_call_ts = time.time()   # ★ 用量计价以本次调用时间为准（峰谷定价）
                finish_reason, full_reasoning, full_content, tool_calls, cancelled, err = _llm_stream_call(
                    internal, model, r_eff, thinking, tools, cancel_event, usage, emit,
                    sid=session_id, t_start=last_call_ts)

                if cancelled:
                    break
                if err:
                    # ★ 2026-09-21（按用户要求）：API 报上下文超限时，从最早处持续删除内容直到适配，
                    #   全程静默（不显示“无法发送”等提示）；其余错误保持原逻辑。
                    _es = str(err)
                    if ("maximum context length" in _es) or ("context_length_exceeded" in _es):
                        _before = _estimate_tokens_messages(internal)
                        _dropped_er = _trim_internal_to_fit(internal, limit_est=int(CTX_SEND_LIMIT_EST * 0.7))
                        _cut = False
                        if _estimate_tokens_messages(internal) >= CTX_SEND_LIMIT_EST:
                            _cut = _truncate_last_message_to_fit(internal, limit_est=int(CTX_SEND_LIMIT_EST * 0.7))
                        if (not _cut) and _dropped_er <= 0:
                            _cut = _hard_shrink_last_message(internal, limit_est=int(CTX_SEND_LIMIT_EST * 0.5))
                        _after = _estimate_tokens_messages(internal)
                        _ctx_retry_n += 1
                        logging.warning(f"[OPERATE] {session_id} API 报上下文超限：已从最早处裁剪（{_before}→{_after} tokens，丢弃 {_dropped_er} 条，截断={_cut}），静默重试（第 {_ctx_retry_n} 次）")
                        if (_dropped_er > 0 or _cut or _after < _before) and _ctx_retry_n <= 20:
                            usage["ctx_tokens"] = _after
                            continue
                        logging.warning(f"[OPERATE] {session_id} 上下文已无法继续裁剪，停止重试（静默）")
                        break
                    break
                _ctx_retry_n = 0
                if cancel_event.is_set():
                    break

                # ★ 上下文占用检查（调用后）：基于本次真实 usage，超阈值立即推送警告并注入下次上下文
                with sessions_lock:
                    _csess2 = sessions.get(session_id)
                if _csess2:
                    _maybe_warn_context(session_id, internal, _csess2, emit, usage)

                logging.info(f"[OPERATE] {session_id} 流式结束: has_reasoning={bool(full_reasoning)}, tool_calls={len(tool_calls)}, content_len={len(full_content)}, finish={finish_reason}")

                if tool_calls:
                    # ★ 2026-10-12（修「上下文占用率暴涨 25% -> 50%+」）：
                    #   实测（用户导出文件）模型把**整份会话快照**当成 #mem_save 的参数
                    #   写了一次 —— 单条 arguments 2,010,456 字符 ≈ 67 万 tokens，
                    #   占了 messages 总字符的 79%。
                    #   已有的 _cap_tool_args 只在 _trim_internal_to_fit 里调用，而那只在
                    #   "上下文超过窗口 96%"时才触发 —— 所以它在超限前一直原样躺在
                    #   internal 里，每次请求都发给模型，导出也原样带走。
                    #   这里在**参数进入 internal 之前**就截断，让 internal / 执行 / 事件
                    #   三者保持一致（避免"事件裁了但上下文没裁"这类不一致）。
                    try:
                        for _tc in tool_calls:
                            _a = getattr(_tc.function, "arguments", None)
                            if not (isinstance(_a, str) and _a):
                                continue
                            # ★ 按工具挑上限：保真类（#mem_save/#load/#write/#replace）
                            #   的参数就是要落盘的数据本身，绝不能截（实测记忆文件
                            #   最大 6.3MB，一律截 10 万等于毁掉用户数据）；
                            #   其余（命令/脚本）用 10 万，专拦"整份文档塞进参数"。
                            _lim = _tool_arg_limit_for(_a)
                            if len(_a) > _lim:
                                _orig_len = len(_a)
                                _tc.function.arguments = _cap_tool_args(_a, _lim)
                                logging.warning(
                                    f"[CTX] 工具 {_tc.function.name} 的参数过大"
                                    f"（{_orig_len} 字符），已截断到 {_lim} 字符")
                    except Exception as _e:
                        logging.info(f"[CTX] 参数裁剪跳过: {_e}")
                    internal.append({
                        "role": "assistant",
                        "content": full_content or None,
                        "reasoning_content": full_reasoning,
                        "tool_calls": [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in tool_calls],
                    })
                    # ★ 2026-10-13（修「view_image 与其它工具同批时，同批调用看起来被取消」）：
                    #   view_image 的结果要作为**一条 user 消息**（图片）注入 internal，
                    #   而 OpenAI 协议要求 assistant(tool_calls) 之后必须**紧跟全部** tool 响应。
                    #   若 view_image 排在批内中间，它注入的 user 消息会把后面几个 tool 响应
                    #   变成"孤儿" → 下一轮请求前 _ensure_tool_pairing 给它们补
                    #   "未收到返回结果，已自动补占位响应" → 模型误以为那几条被取消并重跑。
                    #   （实测：记录里同批的 plan/fetch 三条其实都执行成功 rc=0，却收到占位。）
                    #   这里只调整**执行顺序**：view_image 排到最后，
                    #   使 user(图片) 落在所有 tool 响应之后，顺序即合法。
                    _ordered = sorted(tool_calls,
                                      key=lambda tc: 1 if tc.function.name == "view_image" else 0)
                    for tool_call in _ordered:
                        # ★ BUGFIX(2026-09-28)：工具被中途关掉后，模型仍可能继续调用它
                        #   （历史上下文里见过这个工具，或者同一次调用里工具表变了）。
                        #   以前服务端**不做任何校验**，照样把命令发下去执行 —— 相当于
                        #   “关掉的工具其实还能用”，而且在客户端不具备该能力时会直接报错。
                        #   现在先校验：不在本次提供给模型的工具表里 → 直接返回可读错误，
                        #   让模型改用别的工具或直接回答。
                        _allowed = {t["function"]["name"] for t in tools}
                        if tool_call.function.name not in _allowed:
                            logging.warning(f"[OPERATE] {session_id} 模型调用了未启用的工具 "
                                            f"{tool_call.function.name}（本次工具表 {len(_allowed)} 个），已拒绝执行")
                            _rej = (f"错误: 工具 {tool_call.function.name} 当前未启用"
                                    f"（可能已在“操作模式设置 → 工具开关”中被关闭）。"
                                    f"请不要调用它，改用已启用的工具，或直接给出最终答案。")
                            emit({"type": "command_execute", "task_id": str(uuid.uuid4())[:8],
                                  "command": f"#rejected {tool_call.function.name}",
                                  "command_title": f"⛔ 工具未启用：{tool_call.function.name}"})
                            internal.append({"role": "tool", "tool_call_id": tool_call.id, "content": _rej})
                            continue
                        # ★ 询问用户（ask_user）：仅主 agent 可用；同步阻塞等待作答 / 异步立即返回
                        if tool_call.function.name == "ask_user":
                            _handle_parent_ask_user(tool_call, session_id, emit, cancel_event, internal)
                            if cancel_event.is_set():
                                emit({"type": "cancelled"})
                                break
                            continue
                        cmd_str = tool_to_command(tool_call)
                        if not cmd_str:
                            logging.warning(f"[OPERATE] {session_id} 工具 {tool_call.function.name} 参数缺失，跳过")
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                # ★ 2026-10-12：原来只说"缺少必要参数"，模型据此反复重试各种写法
                                #   （真实记录里它试了 5 次才偶然蒙对结构化写法）。现在把**实际收到的
                                #   参数**回给模型，它一次就能自我纠正。
                                "content": (f"错误: 工具 {tool_call.function.name} 的参数无法解析。"
                                            f"实际收到: {str(tool_call.function.arguments)[:300]}")
                            })
                            continue

                        task_id = str(uuid.uuid4())[:8]
                        # ★ 2026-10-14：模型原始 arguments（供导出/导入重建上下文时一字不差地还原）
                        _orig_args = getattr(tool_call.function, 'arguments', None)
                        # ★ 上下文管理：记录正在运行的定时/异步任务（供 #compress 保留命令与ID）
                        if cmd_str.startswith("#timer ") or cmd_str.startswith("#exec --async"):
                            with sessions_lock:
                                _tsess = sessions.get(session_id)
                            if _tsess:
                                _record_active_task(_tsess, task_id, cmd_str)
                        # agent 管理命令：服务端直接处理（不经浏览器/客户端中转）
                        _tool_t0 = time.time()   # ★ 工具耗时统计起点（会话属性面板）
                        server_out = resolve_server_command(cmd_str, {
                            "session_id": session_id, "is_parent": True, "agent": None,
                            "internal": internal, "usage": usage,
                            "model": model, "r_eff": r_eff, "think": thinking,
                            "wait_client": wait_client, "emit": emit,
                            "list_limit": list_limit,
                        })
                        if server_out is not None:
                            out, ret = server_out
                            _acc_time_stat(session_id, "tool_ms", (time.time() - _tool_t0) * 1000.0)
                            emit({"type": "command_execute", "task_id": task_id, "command": cmd_str,
                                      "original_args": _orig_args,
                                  "server_side": True, "command_title": _cmd_title(cmd_str)})
                            emit({"type": "command_result", "task_id": task_id, "command": cmd_str,
                                  "output": _event_view_image_output(cmd_str, out), "returncode": ret, "command_title": _cmd_title(cmd_str)})
                            # ★ 上下文压缩：#compress 已把 internal 重建为 [system, 摘要]，
                            #   旧 assistant(tool_calls) 已删除，此处不能再 append tool 消息，
                            #   否则产生孤立 tool 消息导致 API 400（tool 无前置 tool_calls）。
                            if cmd_str.startswith("#compress"):
                                continue
                            # ★ 长输出处理：客户端 agent 已就地落盘（输出含预览+路径），此处兜底
                            limit = int(settings.get("process_truncate", 4000) or 4000)
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": _pack_tool_result(ret, out, session_id, task_id, tool_call.function.name, limit),
                            })
                            continue

                        # 普通命令：经浏览器中转执行
                        emit({"type": "command_execute", "task_id": task_id, "command": cmd_str,
                                      "original_args": _orig_args,
                              "command_title": _cmd_title(cmd_str)})

                        # ★ BUGFIX(2026-09-28)：同上——锁内不能调 emit（会自锁死）。
                        _sess_gone = False
                        with sessions_lock:
                            sess = sessions.get(session_id)
                            if not sess:
                                _sess_gone = True
                            else:
                                if "command_events" not in sess:
                                    sess["command_events"] = {}
                                result_event = threading.Event()
                                result_holder = {}
                                sess["command_events"][task_id] = (result_event, result_holder)
                        if _sess_gone:
                            emit({"type": "error", "content": "会话丢失"})
                            break

                        start_wait = time.time()
                        _hb_last = start_wait
                        while not result_event.is_set():
                            if time.time() - start_wait > _tool_wait_s:
                                break
                            if cancel_event.is_set():
                                result_event.set()
                                break
                            # ★ 2026-10-04：等待工具结果期间每 10 秒补发心跳，
                            #   避免工具跑很久时前端 150 秒看门狗误判「模型流中断」。
                            if time.time() - _hb_last >= 10:
                                _hb_last = time.time()
                                try:
                                    emit({"type": "heartbeat", "agent_id": "main", "waiting_tool": True})
                                except Exception:
                                    pass
                            time.sleep(0.2)

                        with sessions_lock:
                            sess = sessions.get(session_id)
                            if sess:
                                sess["command_events"].pop(task_id, None)
                        # ★ 会话属性面板：经浏览器中转执行的工具耗时
                        _acc_time_stat(session_id, "tool_ms", (time.time() - start_wait) * 1000.0)

                        if cancel_event.is_set():
                            # ★ 取消时补齐 tool 响应，保证 tool_calls 配对（避免下一轮 API 400）
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "返回码: -1\n（命令未执行完成，已取消）",
                            })
                            emit({"type": "cancelled"})
                            break

                        out = result_holder.get("output", "命令结果丢失") if result_holder else "命令结果丢失"
                        ret = result_holder.get("returncode", -1) if result_holder else -1
                        emit({"type": "command_result", "task_id": task_id, "command": cmd_str, "output": _event_view_image_output(cmd_str, out), "returncode": ret,
                              "command_title": _cmd_title(cmd_str)})

                        # ★ 图像理解：view_image 结果需要把图片注入为 user 消息（专用处理）
                        if tool_call.function.name == "view_image":
                            _append_view_image_tool(internal, tool_call, out, ret, model)
                            continue

                        # ★ 长输出处理：客户端 agent 已就地落盘（输出含预览+路径），此处兜底
                        limit = int(settings.get("process_truncate", 4000) or 4000)
                        internal.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": _pack_tool_result(ret, out, session_id, task_id, tool_call.function.name, limit),
                        })
                    if cancel_event.is_set():
                        break
                    continue

                # ★ 2026-09-29（按用户反馈）：模型把工具调用写成了纯文本（调用了不存在的工具名）时，
                #   不能把这段乱码当“最终答案”显示并结束本轮 —— 那样用户只会看到一段
                #   <||DSML|| ...> 然后就没反应了。这里改为：把工具不存在的错误回给模型，
                #   让它用正确的工具名重试（最多重试 _bad_tool_retry 次，避免死循环）。
                if full_content:
                    _allowed_now = {t["function"]["name"] for t in tools}
                    _bad_name, _bad_msg = _detect_bad_tool_call(full_content, _allowed_now)
                    if _bad_name and _bad_tool_retry < 3:
                        _bad_tool_retry += 1
                        logging.warning(f"[OPERATE] {session_id} 模型把工具调用写成了文本"
                                        f"（工具名={_bad_name}），第 {_bad_tool_retry} 次提醒重试")
                        # ★ 2026-09-29（按用户要求，最终方案）：
                        #   ① 那段文本**放行**（已经推给前端了，不撤回、不掩盖）；
                        #   ② 仍然把错误回给模型，让它换正确的工具名重试；
                        #   ③ **不发 reset_container** —— 发了会在界面上新起一个白色大框，
                        #      把上一个回复框“断开”。保持同一个容器，重试产生的内容
                        #      接着往里写，用户看到的就是一个连续的白框。
                        internal.append({"role": "assistant", "content": full_content})
                        internal.append({"role": "user", "content": _bad_msg})
                        # 本轮不作为最终答复，继续下一轮让模型重试
                        with sessions_lock:
                            _s = sessions.get(session_id)
                            if _s:
                                _s["final_answered"] = False
                                _s["busy"] = True
                        continue

                if full_content:
                    answer = full_content
                    logging.info(f"[OPERATE] {session_id} 输出最终答案，长度 {len(answer)}")
                    # ★ 记忆修复(2026-09-06)：把最终答复写回 internal（此前只 emit 不存档，
                    #   导致操作模式下模型在后续轮次看不到自己之前的回答）。
                    internal.append({"role": "assistant", "content": answer})
                    # ★ 用量合计(2026-09-06)：子代理（spawn_agent）的 token 用量也计入总量。
                    with sessions_lock:
                        _psess = sessions.get(session_id)
                        _extra = dict((_psess or {}).get("children_usage") or {})
                        # ★ 2026-09-21：导入会话带上历史用量基数，否则“导出→导入→再发一条消息”
                    #   时，界面外部用量会从很小的本次值重新开始，看起来像“用量变少了”。
                    #   这里只在**导入过历史用量**时才加基数（新会话基数为 0，行为不变）。
                    _base = dict((_psess or {}).get("stats", {}).get("hist_usage") or {}) \
                        if isinstance((_psess or {}).get("stats"), dict) else {}
                    _usage_evt = dict(usage)
                    for _k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
                        _usage_evt[_k] = int(_usage_evt.get(_k, 0) or 0) + int(_extra.get(_k, 0) or 0)
                    if _base:
                        for _k in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                            _usage_evt[_k] = int(_usage_evt.get(_k, 0) or 0) + int(_base.get(_k, 0) or 0)
                        _usage_evt["total_tokens"] = int(_usage_evt.get("prompt_tokens", 0)) + int(_usage_evt.get("completion_tokens", 0))
                    emit({"type": "final_answer", "content": answer})
                    emit({"type": "usage", "usage": _usage_evt, "ts": last_call_ts})
                    with sessions_lock:
                        sess = sessions.get(session_id)
                        if sess:
                            sess['final_answered'] = True
                            sess['busy'] = False
                            logging.info(f"[OPERATE] {session_id} 设置 final_answered=True, busy=False")
                    continue
                else:
                    logging.info(f"[OPERATE] {session_id} 模型无内容返回，退出")
                    break

            except Exception as e:
                logging.error(f"[OPERATE] {session_id} 异常: {e}")
                emit({"type": "error", "content": str(e)})
                break
            finally:
                with sessions_lock:
                    sess = sessions.get(session_id)
                    if sess:
                        sess['busy'] = False
                        logging.info(f"[OPERATE] {session_id} 重置 busy=False")
    except Exception as _loop_exc:
        # ★ 2026-10-06（关键可观测性缺口）：以前这里**只有 finally、没有 except**——
        #   父循环里任何异常都被静默吞掉，线程直接死掉。
        #   表现出来就是「消息发出去了、服务端也收到了、就是永远没有回复」，
        #   而日志里连一行 ERROR 都没有（用户反复反馈的"发不出消息"，我查了十几轮
        #   都抓不到根因，就是被这个静默 except 挡住的）。
        #   现在把完整堆栈打出来，任何同类故障都能一眼定位。
        logging.error(f"[OPERATE] {session_id} 父循环异常退出（这会导致之后收不到任何回复）: {_loop_exc}\n"
                      + traceback.format_exc())
        try:
            emit({"type": "error", "content": f"服务端处理异常（本轮已中断）：{_loop_exc}"})
        except Exception:
            pass
    finally:
        _finish_parent(session_id)

def _finish_parent(session_id):
    """父循环结束：标记 parent_exited / parent_running 并向共享队列放入结束哨兵。"""
    with sessions_lock:
        sess = sessions.get(session_id)
        if sess and not sess.get("parent_exited"):
            sess["parent_exited"] = True
            # ★ 2026-09-28：父循环退出后，下一条消息的 /chat 需要重新启动父循环
            sess["parent_running"] = False
            q = sess.get("event_queue")
            if q is not None:
                try:
                    q.put(None)
                except Exception:
                    pass

# ---------- 子 agent 工作循环 ----------
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")

def _acc_child_usage(sid, aid, usage):
    """把子 agent 的累计用量按【增量】并入父会话 children_usage（幂等）：
    子 agent 每轮结束（final answer）与线程退出（被销毁/异常）都会调用，
    已并入部分记录在 a['usage_added']，不会重复累计。"""
    with agents_lock:
        a = agents.get(aid)
        if a is None:
            return
        added = a.get("usage_added")
        if added is None:
            added = {}
            a["usage_added"] = added
        deltas = {}
        for k in _USAGE_KEYS:
            cur = int(usage.get(k, 0) or 0)
            prev = int(added.get(k, 0) or 0)
            if cur > prev:
                deltas[k] = cur - prev
                added[k] = cur
    if not deltas:
        return
    with sessions_lock:
        sess = sessions.get(sid)
        if sess:
            cu = sess.setdefault("children_usage", {})
            for k, v in deltas.items():
                cu[k] = int(cu.get(k, 0) or 0) + v

def child_agent_stream(a):
    """子 agent 循环：独立上下文 + 工具执行 + #return 里程碑 + 最终答复推送父 agent。
    事件经父会话共享队列推送给浏览器（与父 agent 共用同一条 HTTP 连接）。"""
    aid = a["id"]
    sid = a["parent_sid"]
    cancel_event = a["cancel_event"]
    wait_event = a["wait_event"]

    # ★ 2026-10-04：子代理等客户端回传的上限，与父循环保持一致
    #   （从父会话设置取 max_timeout，默认 600，+300s 余量）。
    #   原来硬编码 120s：命令跑得久一点就会「命令结果丢失」，
    #   而客户端其实还在跑、回传时还会吃 404。
    try:
        with sessions_lock:
            _ps = sessions.get(sid)
            _mt = int(((_ps or {}).get("settings") or {}).get("max_timeout", 600) or 600)
    except Exception:
        _mt = 600
    _tool_wait_s = _mt + 300

    def emit(evt):
        _emit_agent(a, evt)

    def announce_killed():
        """标记子 agent 为已销毁并广播事件。幂等：与 #kill 处理器共用
        killed_announced 标记，无论哪个先执行都只广播一次（修复 kill 后
        状态仍显示 running 的问题）。"""
        if not a.get("killed_announced"):
            a["killed_announced"] = True
            _agent_set_status(a, "killed")
            emit({"type": "agent_cancelled"})

    internal = [
        # ★ 2026-09-21：子 Agent 基础提示词支持在“提示词”标签页里单独覆盖（child_base）
        {"role": "system", "content": _effective_child_base()},
        {"role": "user", "content": a["task"]},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "ctx_tokens": 0}

    def wait_client(cmd_str, display=None, orig_args=None):
        """把命令经浏览器中转给客户端执行并同步等待结果（子 agent 的 local 记忆操作）。
        返回 (output, returncode)。"""
        task_id = str(uuid.uuid4())[:8]
        show_cmd = display or cmd_str
        emit({"type": "command_execute", "task_id": task_id, "command": show_cmd,
              "original_args": orig_args,
              "command_title": _cmd_title(show_cmd)})
        result_event = threading.Event()
        result_holder = {}
        with agents_lock:
            if aid not in agents:
                return "子 agent 已不存在", -1
            a.setdefault("command_events", {})[task_id] = (result_event, result_holder)
        start_wait = time.time()
        _hb_last = start_wait
        while not result_event.is_set():
            if time.time() - start_wait > _tool_wait_s:
                break
            if cancel_event.is_set():
                result_event.set()
                break
            # ★ 2026-10-04：等待期间每 10 秒补发一次心跳。
            #   工具可能跑几十秒到几分钟，这段期间如果不发任何事件，
            #   前端的 150 秒看门狗会误判「模型流中断」并中止本轮等待。
            #   heartbeat 前端只用于刷新时间戳，不渲染、不写入导出记录。
            if time.time() - _hb_last >= 10:
                _hb_last = time.time()
                try:
                    emit({"type": "heartbeat", "agent_id": "main", "waiting_tool": True})
                except Exception:
                    pass
            time.sleep(0.2)
        with agents_lock:
            if aid in agents:
                a["command_events"].pop(task_id, None)
        out = result_holder.get("output", "命令结果丢失") if result_holder else "命令结果丢失"
        ret = result_holder.get("returncode", -1) if result_holder else -1
        emit({"type": "command_result", "task_id": task_id, "command": show_cmd,
              "output": _event_view_image_output(show_cmd, out), "returncode": ret, "command_title": _cmd_title(show_cmd)})
        return out, ret

    logging.info(f"[CHILD] 子 agent {aid} 启动，父会话 {sid}")

    try:
        while True:
            if cancel_event.is_set():
                announce_killed()
                logging.info(f"[CHILD] {aid} 被销毁")
                break

            # 检查消息队列 / 通知队列
            with agents_lock:
                if aid not in agents:
                    break
                user_msgs = None
                notifications = None
                if not a.get("busy"):
                    # ★ 2026-09-29：子 agent 的待处理消息存在 msg_queue（与父会话统一，
                    #   由 prompt_agent 以纯字符串写入）。这里做兼容读取：
                    #   msg_queue 里可能是字符串，也可能是 {"text":...} 字典。
                    _q = a.get("msg_queue")
                    if _q:
                        user_msgs = []
                        for _it in _q:
                            if isinstance(_it, dict):
                                _t = _it.get("text")
                                if _t:
                                    user_msgs.append(_t)
                            elif _it is not None:
                                user_msgs.append(str(_it))
                        _q.clear()
                    elif a.get("final_answered") and a.get("pending_notifications"):
                        notifications = a["pending_notifications"][:]
                        a["pending_notifications"].clear()

            if user_msgs:
                for msg in user_msgs:
                    # ★ 上下文压缩：子 agent 也可执行 #compress（父/用户经 #prompt 传入）
                    if msg.strip().startswith("#compress"):
                        summary = msg.strip()[len("#compress"):].strip()
                        out, ret = _do_compress({"session_id": sid, "internal": internal,
                                                 "is_parent": False, "agent": a, "emit": emit,
                                                 "usage": usage}, summary)
                        emit({"type": "agent_notice",
                              "content": (f"🧹 上下文已压缩：{summary[:200]}" if ret == 0 else out)})
                        continue
                    internal.append({"role": "user", "content": msg})
                a["final_answered"] = False
                a["busy"] = True
                _agent_set_status(a, "running")
                # ★ BUGFIX(2026-08-15) 父发给子改为 B 处理（与用户信息一致）：
                #   父 agent 追加的信息先由 agent_prompted 事件在前端显示气泡；
                #   子 agent 当前轮（思考/工具调用）结束后才真正消费新消息，
                #   此刻广播 agent_round_start，前端据此开启新一轮气泡，
                #   不再出现“思考到一半被打断、新任务没有换新气泡”的问题。
                emit({"type": "agent_round_start"})
                logging.info(f"[CHILD] {aid} 处理 {len(user_msgs)} 条新消息")
                continue

            if notifications:
                notification_text = "🔔 定时任务有更新：\n" + "\n".join(
                    n.get("text") if isinstance(n, dict) else str(n) for n in notifications)
                emit({"type": "agent_notice", "content": notification_text})
                internal.append({"role": "user", "content": notification_text})
                a["final_answered"] = False
                a["busy"] = True
                continue

            # 空闲等待（已完成且无新消息时，保持存活以便父 agent 追加信息）
            with agents_lock:
                ag = agents.get(aid)
                if ag is None:
                    break
                idle = (a.get("final_answered") and not a.get("pending_notifications")
                        and not a.get("msg_queue"))
            if idle:
                wait_event.wait(timeout=1)
                wait_event.clear()
                continue

            a["busy"] = True
            try:
                # ★ 上下文占用检查：≥50% 时自动向模型推送压缩警告（基于真实 usage，仅一次，压缩后重置）
                with agents_lock:
                    _ch = agents.get(aid)
                if _ch:
                    _maybe_warn_context(sid, internal, _ch, emit, usage)
                logging.info(f"[CHILD] {aid} 调用大模型，当前消息数: {len(internal)}")
                _child_call_t0 = time.time()   # ★ 子 agent 调用也计入会话时间统计
                finish_reason, full_reasoning, full_content, tool_calls, cancelled, err = _llm_stream_call(
                    internal, a["model"], a["reasoning_effort"], a.get("thinking_enabled", True),
                    CHILD_TOOLS, cancel_event, usage, emit,
                    sid=sid, t_start=_child_call_t0)

                if cancelled:
                    # ★ BUGFIX(2026-08-15) 取消发生于 LLM 流式调用中途时，
                    #   此前直接 break 不更新状态，导致 #kill 后仍显示 running。
                    announce_killed()
                    break
                if err:
                    # ★ 2026-09-21：子 agent 同样自动裁剪上下文（静默重试）
                    _ce = str(err)
                    if ("maximum context length" in _ce) or ("context_length_exceeded" in _ce):
                        _c_dropped = _trim_internal_to_fit(internal)
                        _c_cut = False
                        if _estimate_tokens_messages(internal) >= CTX_SEND_LIMIT_EST:
                            _c_cut = _truncate_last_message_to_fit(internal)
                        if (not _c_cut) and _c_dropped <= 0:
                            _c_cut = _hard_shrink_last_message(internal)
                        if _c_dropped > 0 or _c_cut:
                            usage["ctx_tokens"] = _estimate_tokens_messages(internal)
                            logging.warning(f"[CHILD] {aid} API 报上下文超限：已从最早处裁剪（丢弃 {_c_dropped} 条，截断={_c_cut}），静默重试")
                            continue
                    _agent_set_status(a, "error")
                    break
                if cancel_event.is_set():
                    announce_killed()
                    break

                # ★ 上下文占用检查（调用后）：基于本次真实 usage，超阈值立即推送警告
                with agents_lock:
                    _ch2 = agents.get(aid)
                if _ch2:
                    _maybe_warn_context(sid, internal, _ch2, emit, usage)

                if tool_calls:
                    # ★ 2026-10-12（修「上下文占用率暴涨 25% -> 50%+」）：
                    #   实测（用户导出文件）模型把**整份会话快照**当成 #mem_save 的参数
                    #   写了一次 —— 单条 arguments 2,010,456 字符 ≈ 67 万 tokens，
                    #   占了 messages 总字符的 79%。
                    #   已有的 _cap_tool_args 只在 _trim_internal_to_fit 里调用，而那只在
                    #   "上下文超过窗口 96%"时才触发 —— 所以它在超限前一直原样躺在
                    #   internal 里，每次请求都发给模型，导出也原样带走。
                    #   这里在**参数进入 internal 之前**就截断，让 internal / 执行 / 事件
                    #   三者保持一致（避免"事件裁了但上下文没裁"这类不一致）。
                    try:
                        for _tc in tool_calls:
                            _a = getattr(_tc.function, "arguments", None)
                            if not (isinstance(_a, str) and _a):
                                continue
                            # ★ 按工具挑上限：保真类（#mem_save/#load/#write/#replace）
                            #   的参数就是要落盘的数据本身，绝不能截（实测记忆文件
                            #   最大 6.3MB，一律截 10 万等于毁掉用户数据）；
                            #   其余（命令/脚本）用 10 万，专拦"整份文档塞进参数"。
                            _lim = _tool_arg_limit_for(_a)
                            if len(_a) > _lim:
                                _orig_len = len(_a)
                                _tc.function.arguments = _cap_tool_args(_a, _lim)
                                logging.warning(
                                    f"[CTX] 工具 {_tc.function.name} 的参数过大"
                                    f"（{_orig_len} 字符），已截断到 {_lim} 字符")
                    except Exception as _e:
                        logging.info(f"[CTX] 参数裁剪跳过: {_e}")
                    internal.append({
                        "role": "assistant",
                        "content": full_content or None,
                        "reasoning_content": full_reasoning,
                        "tool_calls": [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in tool_calls],
                    })
                    # ★ 2026-10-13：与父 agent 同样处理 —— view_image 排到批内最后，
                    #   使它的 user(图片) 消息落在所有 tool 响应之后（原因见父 agent 处的说明）。
                    _ordered_c = sorted(tool_calls,
                                        key=lambda tc: 1 if tc.function.name == "view_image" else 0)
                    for tool_call in _ordered_c:
                        # ★ 与父 agent 一致：子 agent 也拒绝执行未启用的工具
                        _allowed_c = {t["function"]["name"] for t in CHILD_TOOLS}
                        if tool_call.function.name not in _allowed_c:
                            logging.warning(f"[CHILD] {aid} 调用了未启用的工具 {tool_call.function.name}，已拒绝")
                            internal.append({"role": "tool", "tool_call_id": tool_call.id,
                                             "content": f"错误: 工具 {tool_call.function.name} 不可用（子 agent 无此工具）。"
                                                        f"请改用已有工具或直接给出最终答案。"})
                            continue
                        cmd_str = tool_to_command(tool_call)
                        if not cmd_str:
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": f"错误: 工具 {tool_call.function.name} 缺少必要参数"
                            })
                            continue

                        task_id = str(uuid.uuid4())[:8]
                        # ★ 2026-10-14：模型原始 arguments（供导出/导入重建上下文时一字不差地还原）
                        _orig_args = getattr(tool_call.function, 'arguments', None)
                        # ★ 上下文管理：记录子 agent 正在运行的定时/异步任务
                        if cmd_str.startswith("#timer ") or cmd_str.startswith("#exec --async"):
                            with agents_lock:
                                _ta = agents.get(aid)
                            if _ta:
                                _record_active_task(_ta, task_id, cmd_str)
                        # 服务端命令（#return 等）
                        server_out = resolve_server_command(cmd_str, {
                            "session_id": sid, "is_parent": False, "agent": a,
                            "internal": internal, "usage": usage,
                            "model": a.get("model"), "r_eff": a.get("reasoning_effort"),
                            "think": a.get("thinking_enabled", True),
                            "wait_client": wait_client, "emit": emit,
                        })
                        if server_out is not None:
                            out, ret = server_out
                            emit({"type": "command_execute", "task_id": task_id, "command": cmd_str,
                                      "original_args": _orig_args,
                                  "server_side": True, "command_title": _cmd_title(cmd_str)})
                            emit({"type": "command_result", "task_id": task_id, "command": cmd_str,
                                  "output": _event_view_image_output(cmd_str, out), "returncode": ret, "command_title": _cmd_title(cmd_str)})
                            # ★ 上下文压缩：#compress 已把 internal 重建为 [system, 摘要]，
                            #   此处不能再 append tool 消息（避免孤立 tool 消息 API 400）。
                            if cmd_str.startswith("#compress"):
                                continue
                            # ★ 长输出处理：子 agent 沿用父会话的"工具输出截断长度"设置
                            _trunc_limit = 4000
                            with sessions_lock:
                                _psess = sessions.get(sid)
                                if _psess:
                                    _trunc_limit = int((_psess.get("settings") or {}).get("process_truncate", 4000) or 4000)
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": _pack_tool_result(ret, out, aid, task_id, tool_call.function.name, _trunc_limit),
                            })
                            continue

                        # 普通命令：经浏览器中转执行
                        emit({"type": "command_execute", "task_id": task_id, "command": cmd_str,
                                      "original_args": _orig_args,
                              "command_title": _cmd_title(cmd_str)})

                        with agents_lock:
                            if aid not in agents:
                                break
                            result_event = threading.Event()
                            result_holder = {}
                            a["command_events"][task_id] = (result_event, result_holder)

                        start_wait = time.time()
                        _hb_last = start_wait
                        while not result_event.is_set():
                            if time.time() - start_wait > _tool_wait_s:
                                break
                            if cancel_event.is_set():
                                result_event.set()
                                break
                            # ★ 2026-10-04：同上 —— 等工具结果期间补发心跳
                            if time.time() - _hb_last >= 10:
                                _hb_last = time.time()
                                try:
                                    emit({"type": "heartbeat", "agent_id": aid,
                                          "waiting_tool": True})
                                except Exception:
                                    pass
                            time.sleep(0.2)

                        with agents_lock:
                            a["command_events"].pop(task_id, None)

                        if cancel_event.is_set():
                            # ★ 取消时补齐 tool 响应，保证 tool_calls 配对（避免下一轮 API 400）
                            internal.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": "返回码: -1\n（命令未执行完成，已取消）",
                            })
                            announce_killed()
                            break

                        out = result_holder.get("output", "命令结果丢失") if result_holder else "命令结果丢失"
                        ret = result_holder.get("returncode", -1) if result_holder else -1
                        emit({"type": "command_result", "task_id": task_id, "command": cmd_str, "output": _event_view_image_output(cmd_str, out), "returncode": ret,
                              "command_title": _cmd_title(cmd_str)})

                        # ★ 图像理解：view_image 结果需要把图片注入为 user 消息（专用处理）
                        if tool_call.function.name == "view_image":
                            _append_view_image_tool(internal, tool_call, out, ret, a.get("model") or default_model_name())
                            continue

                        # ★ 长输出处理：子 agent 沿用父会话的"工具输出截断长度"设置
                        _trunc_limit = 4000
                        with sessions_lock:
                            _psess = sessions.get(sid)
                            if _psess:
                                _trunc_limit = int((_psess.get("settings") or {}).get("process_truncate", 4000) or 4000)
                        internal.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": _pack_tool_result(ret, out, aid, task_id, tool_call.function.name, _trunc_limit),
                        })
                    if cancel_event.is_set():
                        # ★ BUGFIX(2026-08-15) 工具执行期间收到 kill：更新状态后再退出
                        announce_killed()
                        break
                    continue

                # 最终内容：解析 #return 文本兜底 + 最终答复
                returns, answer = extract_returns(full_content or "")
                for r in returns:
                    a["returns"].append(r)
                    emit({"type": "agent_return", "content": r})
                    _notify_parent(a, f"🔔 子 agent {aid} 返回了里程碑：\n{r}", "agent_return")
                    logging.info(f"[CHILD] {aid} 返回里程碑（长度 {len(r)}）")
                if answer:
                    a["final_answer"] = answer
                    # ★ 记忆修复(2026-09-06)：子 agent 的最终答复同样写回上下文。
                    internal.append({"role": "assistant", "content": answer})
                    # ★ BUGFIX(2026-09-28)：这里以前漏了 agent_id，导致导出记录里
                    #   agent_answer 事件没有归属。导入回放时前端按 `agent_id || 'main'`
                    #   判定，于是子 agent 的最终答复被画进**主**区域，
                    #   子 agent 区块变成空的 —— 用户看到的就是“子 agent 导入后没了”。
                    emit({"type": "agent_answer", "agent_id": aid,
                          "content": answer, "usage": dict(usage)})
                    _agent_set_status(a, "finished")
                    _notify_parent(a, f"✅ 子 agent {aid} 最终答复：\n{_truncate(answer, 8000)}", "agent_answer")
                    logging.info(f"[CHILD] {aid} 输出最终答复（长度 {len(answer)}）")
                else:
                    logging.info(f"[CHILD] {aid} 本轮无正文输出")
                # ★ 用量合计(2026-09-06)：子代理每轮结束即把本轮新增用量并入父会话总量
                _acc_child_usage(sid, aid, usage)
                a["final_answered"] = True
                continue

            except Exception as e:
                logging.error(f"[CHILD] {aid} 异常: {e}")
                emit({"type": "error", "content": str(e)})
                _agent_set_status(a, "error")
                break
            finally:
                a["busy"] = False
    finally:
        # 递减存活子 agent 计数，唤醒 drain 循环以便及时结束流
        with sessions_lock:
            sess = sessions.get(sid)
            if sess:
                # ★ 用量合计(2026-09-06)：线程退出兜底并入剩余增量（幂等）
                _acc_child_usage(sid, aid, usage)
                sess["live_children"] = max(0, sess.get("live_children", 0) - 1)
                evt = sess.get("drain_event")
                if evt:
                    evt.set()
        logging.info(f"[CHILD] {aid} 线程结束，status={a.get('status')}")

# ★ 2026-10-05（用户反馈「对话模式点停止怎么没用」）：
#   对话模式的生成跑在后台线程 _generate_chat_background 里，前端只能靠轮询
#   /api/node/<id> 看进度。停止需要一个服务端信号让那个线程别再写了。
#   放在这里（函数之前）而不是路由旁，免得读代码时误以为"用到时还没定义"。
cancelled_nodes = set()
cancelled_nodes_lock = threading.Lock()


def _generate_chat_background(nid, msgs, model, r_eff, extra, user_input):
    # ★ 2026-10-14（多提供商）：声明本次调用所用模型（决定 base_url / api_key）
    set_current_model(model)
    full_content = ""
    full_reasoning = ""
    reasoning_start = time.time()
    usage_info = None
    _user_stopped = False      # ★ 2026-10-05：用户点了「停止生成」
    try:
        stream = get_client().chat.completions.create(
            model=model, messages=msgs, stream=True,
            # ★ 2026-10-10：DeepSeek 专有字段按端点能力发送（见 provider_supports_thinking）。
            **({"reasoning_effort": r_eff, "extra_body": extra}
               if provider_supports_thinking() else {}),
            timeout=90
        )
        for chunk in stream:
            # ★ 2026-10-05：用户点了「停止生成」→ 立刻收工（不再往节点里写。
            #   否则界面停了、服务端还在不停 update_node_in_tree，既浪费 token
            #   又会让重新轮询时内容"跳一下"。）
            with cancelled_nodes_lock:
                if nid in cancelled_nodes:
                    cancelled_nodes.discard(nid)
                    _user_stopped = True
                    logging.info(f"[CHAT] 节点 {nid} 生成已按用户要求中断")
                    try:
                        stream.close()
                    except Exception:
                        pass
                    break
            if hasattr(chunk, 'usage') and chunk.usage:
                u = chunk.usage
                cached = getattr(getattr(u, 'prompt_tokens_details', None), 'cached_tokens', 0)
                usage_info = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens, "cached_tokens": cached,
                              "ts": time.time()}   # ★ 用量计价以调用时间为准（峰谷定价）
                update_node_in_tree(nid, usage=usage_info)
                continue
            reasoning = getattr(chunk.choices[0].delta, 'reasoning_content', None)
            if reasoning:
                full_reasoning += reasoning
                update_node_in_tree(nid, reasoning=full_reasoning)
                continue
            content = chunk.choices[0].delta.content
            if content:
                if full_content == "" and full_reasoning:
                    update_node_in_tree(nid, reasoningDuration=round(time.time()-reasoning_start,1))
                full_content += content
                update_node_in_tree(nid, assistantMessage=full_content)
        title = "新对话"
        if full_content and not _user_stopped:
            try:
                tp = f"请为以下对话内容生成一个简短的标题（4-10字），只输出标题。\n用户：{user_input}\n助手：{full_content}"
                tr = get_client().chat.completions.create(model=model, messages=[{"role": "user", "content": tp}], stream=False, reasoning_effort='low')
                title = tr.choices[0].message.content.strip()[:20]
            except: pass
        update_node_in_tree(nid, title=title)
    except Exception as e:
        logging.error(f"节点 {nid} 生成失败: {e}")
        update_node_in_tree(nid, assistantMessage=f"[生成失败: {str(e)}]")
    finally:
        update_node_in_tree(nid, generating=False)
        if usage_info: update_node_in_tree(nid, usage=usage_info)

def _operation_stream_response(sid, messages, model, r_eff, think, session_extra=None,
                               start_parent=True):
    """操作模式 NDJSON 流：父 agent 与全部子 agent 共用同一个事件队列。

    ★ 2026-09-28（修复“发送消息没反应、150 秒后弹网络错误”）
      原来的致命设计：会话已存在时 /chat 直接 `return jsonify({"status":"reused"})`，
      而前端拿到响应后**必然按 NDJSON 流读取** → 读到那行 JSON（没有 type 字段）
      → 什么都不渲染、也不更新时间戳 → 150 秒后报“服务器已 150 秒没有返回任何数据”。
      触发条件很常见：浏览器端的流先断了（刷新/切模式/网络抖动/abort），
      而服务端的流还挂在同一个事件队列上，sessions 里会话仍在。

      现在改成 **重连**：把请求直接接到该会话现有的事件队列上，继续把事件推给新连接，
      同时把旧的消费者标记为失效（token 机制），避免两条连接抢同一个队列。
      start_parent=False 表示父循环已经在跑，只做重连、不要再启一个。
    """
    def generate():
        _my_token = None
        try:
            with sessions_lock:
                _s = sessions.get(sid)
                if _s is None:
                    yield json.dumps({"type": "error",
                                      "content": "会话已结束，请重新发送消息。"}, ensure_ascii=False) + "\n"
                    return
                # 抢占消费者身份：把旧消费者（如果有）作废
                _my_token = object()
                _s["stream_token"] = _my_token
                q = _s.get("event_queue")
            if q is None:
                return
            _sess_evt = {"type": "session", "session_id": sid, "agent_id": "main"}
            if isinstance(session_extra, dict):
                _sess_evt.update(session_extra)
            # ★ 重连时带上 resumed 标记，前端可据此提示“已重新连接”
            if not start_parent:
                _sess_evt["resumed"] = True
            # ★ 2026-10-06（修竞态"消息被取出但模型从不被调用"）：
            #   parent_running 原本只由**父循环线程内部**设置，而线程是异步启动的。
            #   在"线程已提交但还没跑到那一步"的窗口里，任何 POST /api/operation_input
            #   都会看到 parent_running=False，于是**再起一个恢复模式的父循环** ——
            #   两个循环抢同一条消息队列：一个取走消息，另一个把它取消掉，
            #   结果是"消息被取出、模型从未被调用、界面永远没回复"
            #   （实测复现率 2/5）。
            #   ★ 位置很关键：必须在 **yield session 事件之前**同步置位。
            #     否则客户端一收到 session 就可能发消息，而生成器还停在 yield 上
            #     （生成器是惰性求值，yield 后面的代码要等下一次迭代才跑），窗口依然存在。
            if start_parent:
                with sessions_lock:
                    _sp = sessions.get(sid)
                    if _sp is not None:
                        _sp["parent_running"] = True
                        _sp["parent_exited"] = False
            yield json.dumps(_sess_evt, ensure_ascii=False) + "\n"
            if start_parent:
                threading.Thread(target=run_parent_loop,
                                 args=(messages, model, r_eff, think, sid), daemon=True).start()
            while True:
                # ★ 消费者身份检查：有新的连接接管后，旧连接安静退出（不清理会话！）
                with sessions_lock:
                    _s = sessions.get(sid)
                    if _s is None or _s.get("stream_token") is not _my_token:
                        logging.info(f"[OPERATE] {sid} 旧的事件流连接已被新连接接管，安静退出")
                        return
                try:
                    evt = q.get(timeout=15)
                except queue.Empty:
                    with sessions_lock:
                        _s = sessions.get(sid)
                        # ★ 2026-10-05（修「停止后看不到回复」）：
                        #   存活判据必须把 **parent_running** 算进去。
                        #   原来的判据只有 parent_exited / live_children：
                        #   用户点「停止」后 parent_exited=True，于是这条流在
                        #   15 秒超时后判定"会话已死"直接 return 关流。
                        #   而用户往往过一会儿才发下一条消息 —— 那时流已经没了，
                        #   恢复模式虽然把父循环重新拉起、回答也生成了，
                        #   却没有任何连接把事件送到浏览器（实测：服务端日志一切正常、
                        #   界面上却永远没有回复）。
                        #   另外还给了「刚停止后」一段宽限期（见 last_stop_at）：
                        #   停止后父循环必然退出，若立刻判死关流，用户想接着聊就断线了。
                        _stopped_ago = time.time() - float(_s.get("last_stop_at") or 0) if _s else 1e9
                        alive = _s is not None and (
                            _s.get("parent_running")
                            or not _s.get("parent_exited")
                            or _s.get("live_children", 0) > 0
                            or _stopped_ago < 600)      # 停止后 10 分钟内保持连接可用
                    if not alive:
                        # 队列中可能还有残余事件，先排空
                        try:
                            while True:
                                try:
                                    evt2 = q.get_nowait()
                                except queue.Empty:
                                    break
                                if evt2 is not None:
                                    yield json.dumps(evt2, ensure_ascii=False) + "\n"
                        except Exception:
                            pass
                        return
                    yield json.dumps({"type": "heartbeat", "agent_id": "main"}, ensure_ascii=False) + "\n"
                    continue
                if evt is None:
                    # ★ 2026-10-05（修「停止后重连的流 8 毫秒就断」的真根因）：
                    #   父循环退出时会往队列放一个 None 哨兵。若**上一个消费者已经死了**
                    #   （用户点了停止 → 旧流被 abort），这个哨兵会**留在队列里**。
                    #   之后恢复模式重连时，新连接第一次 q.get() 就拿到它，
                    #   下面原本直接 return —— 于是流刚建立就断，
                    #   服务端随后生成的回答没有任何连接可送（用户看到的"又发不出去了"）。
                    #
                    #   正确语义：哨兵只代表"那个父循环结束了"，**不代表会话没救**——
                    #   恢复模式随时可能把父循环重新拉起来。所以：
                    #     · 父循环还在跑（parent_running）→ 这个哨兵是陈旧的，忽略并继续读；
                    #     · 父循环确实没了且无子 agent → 才收尾退出（并清掉残留哨兵）。
                    with sessions_lock:
                        _s = sessions.get(sid)
                        _parent_alive = bool(_s and _s.get("parent_running"))
                        _kids = int((_s or {}).get("live_children", 0) or 0)
                    if _parent_alive:
                        logging.info(f"[OPERATE] {sid} 读到陈旧的父循环结束哨兵（父循环仍在运行）→ 忽略并继续")
                        continue
                    if _kids == 0:
                        return
                    continue
                yield json.dumps(evt, ensure_ascii=False) + "\n"
        finally:
            # ★ 2026-09-28：这里**不再删除会话**。
            #   以前流一结束就 pop 掉会话，导致「浏览器断开 → 服务端流结束 → 会话消失」
            #   之后的下一条消息只能新建会话（丢掉上下文）。
            #   现在保留会话，由下一条消息的 /chat 负责复用或重建；
            #   真正的清理交给「下一条消息到来时」与「父循环+子 agent 都结束」两个时机。
            with sessions_lock:
                _s = sessions.get(sid)
                if _s is not None and _s.get("stream_token") is _my_token:
                    _s["stream_token"] = None
            logging.info(f"[OPERATE] {sid} 事件流连接结束（会话保留，等待重连或下一条消息）")
    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")

@app.route('/chat', methods=['POST'])
def chat():
    d = request.get_json()
    mode = d.get('mode','chat')
    model = d.get('model') or default_model_name()
    r_eff = d.get('reasoning_effort','max')
    think = d.get('thinking_enabled', True)
    tree_node_id = d.get('tree_node_id')
    user_input = d.get('user_input')
    parent_id = d.get('parent_id') or tree_node_id
    sibling_id = d.get('sibling_id')
    files = d.get('files',[])

    # === 操作模式 ===
    if mode == 'operation':
        messages = d.get('messages',[])
        if not messages or messages[0].get('role')!='system':
            messages.insert(0,{"role":"system","content":SYSTEM_PROMPT})
        # ★ 操作模式设置（工具开关/超时/截断/记忆列表上限等，来自前端设置弹窗）
        op_settings = d.get('settings') or {}

        # ★ 操作模式支持随消息附带图片与文档（与对话模式一致，无模型能力假设）
        op_images = [img.get('data_url') for img in (d.get('images') or [])
                     if isinstance(img, dict) and isinstance(img.get('data_url'), str) and img['data_url'].startswith('data:image/')]
        op_files = [f for f in (d.get('files') or [])
                    if isinstance(f, dict) and f.get('name')]
        # 图片落盘（供文件查看器打开），仅供查看不参与上下文
        _save_inline_images([{"data_url": u} for u in op_images])

        # 前端带了 session_id 时的三种情况：
        #  ① 会话不存在（已被清理）：丢弃旧 id，走下面新建会话的分支
        #  ② 会话存在但父循环还没启动（导入场景）：启动父循环消费注入的上下文
        #  ③ 会话存在且父循环在跑：**重连**到它的事件队列（不要回 jsonify！见
        #     _operation_stream_response 的说明：回 JSON 会让前端按流读、最终 150s 超时）
        existing_sid = d.get('session_id')
        # ★ 2026-10-05：**纯重连**（前端点「停止」后重新挂上事件流用）。
        #   为什么必须区分：停止会把旧流 abort 掉，父循环随之退出（parent_running=False），
        #   此时若走下面的通用重连逻辑，会因为 parent_running=False 而**启动父循环**，
        #   并把请求里带的 messages 当作新一轮输入重新跑一遍 ——
        #   用户实测到的「点停止后平台自动把请求又发了一次」就是这个原因。
        #   纯重连只做两件事：接管事件队列 + 作废旧消费者，绝不提交任何消息。
        if existing_sid and d.get('reconnect_only'):
            _ro = False
            with sessions_lock:
                _rs = sessions.get(existing_sid)
                if _rs is not None:
                    if op_settings:
                        _rs["settings"] = op_settings
                    _rs["model"] = model
                    _rs["reasoning_effort"] = r_eff
                    _rs["thinking_enabled"] = think
                    _ro = True
            if _ro:
                logging.info(f"[OPERATE] {existing_sid} 纯重连（只接管事件流，不提交消息）")
                return _operation_stream_response(existing_sid, messages, model, r_eff, think,
                                                  session_extra={"model": model},
                                                  start_parent=False)
            logging.info(f"[OPERATE] {existing_sid} 纯重连：会话已不存在，忽略本次重连")
            return jsonify({"status": "no_session"})
        if existing_sid:
            reconnect = False
            start_parent = False
            with sessions_lock:
                sess = sessions.get(existing_sid)
                if sess:
                    if op_settings:
                        sess["settings"] = op_settings
                    # ★ FIX(2026-09-11)：前端模型选择随请求同步，避免会话继续沿用旧模型
                    sess["model"] = model
                    # ★ FIX(2026-09-11 v4)：思考强度/思考开关也随请求同步
                    sess["reasoning_effort"] = r_eff
                    sess["thinking_enabled"] = think
                    if op_images:
                        sess["pending_images"] = op_images
                    if op_files:
                        sess["pending_files"] = op_files
                    if not sess.get("stream_started"):
                        # ② 导入会话：启动父循环
                        sess["stream_started"] = True
                        start_parent = True
                    else:
                        # ③ 已有会话：重连（父循环可能已在跑，也可能已退出等新消息）
                        start_parent = not sess.get("parent_running", False)
                        sess["parent_running"] = True
                    reconnect = True
                    model = sess.get("model") or model
                    r_eff = sess.get("reasoning_effort") or r_eff
                    think = sess.get("thinking_enabled", think)
            if reconnect:
                logging.info(f"[OPERATE] {existing_sid} /chat 重连到已有会话"
                             f"（{'启动父循环' if start_parent else '父循环已在运行'}）")
                return _operation_stream_response(existing_sid, messages, model, r_eff, think,
                                                  session_extra={"model": model},
                                                  start_parent=start_parent)

        # 新建会话：父 agent 与全部子 agent 共用同一个事件队列（同一条 HTTP 连接）
        sid = str(uuid.uuid4())
        with sessions_lock:
            sessions[sid] = {
                "cancel_event": threading.Event(),
                "msg_queue": [],   # ★ 统一消息队列（kind 区分用户消息/通知，want_idle 区分插话/排队）
                "busy": False,
                "wait_event": None,   # 占位，由父循环分配
                "event_queue": queue.Queue(),
                "parent_exited": False,
                "parent_running": True,   # ★ 2026-09-28：父循环正在跑（供 /chat 判断重连还是重启）
                "stream_token": None,     # ★ 事件流消费者身份（重连时作废旧连接）
                "live_children": 0,
                "drain_event": threading.Event(),
                "model": model,
                "reasoning_effort": r_eff,
                "thinking_enabled": think,
                "stream_started": True,
                "settings": op_settings,
                "initial_images": op_images,   # ★ 初始消息附带的图片（data_url 列表）
                "initial_files": op_files,     # ★ 初始消息附带的文档（name+content）
            }
        return _operation_stream_response(sid, messages, model, r_eff, think,
                                          session_extra={"model": model})

    # === 树状对话模式（chat） ===
    images = d.get('images') or []
    images = [img for img in images if isinstance(img, dict) and isinstance(img.get('data_url'), str) and img['data_url'].startswith('data:image/')]
    # ★ 图像理解：图片直接内联进 user 消息，不做任何模型能力假设/自动路由
    #   （模型不支持时由 API 返回错误，用户可自行在设置中调整模型）。
    actual_model = model
    # ★ 2026-10-14（多提供商）：extra 由 provider_extra_body() 决定，
    #   而它内部依赖"当前端点是否支持 DeepSeek 专有字段" —— 必须先声明模型，
    #   否则会用错提供商的判定。
    set_current_model(actual_model)

    extra = provider_extra_body(think)
    new_id = create_node_in_tree(parent_id, sibling_id, user_input, "", "生成中...", "", actual_model, r_eff, True)
    # ★ 图片落盘到 uploads（供历史记录回放与界面缩略图），节点只存 {name, file, mime} 元数据
    saved_images = _save_inline_images(images)
    if saved_images:
        update_node_in_tree(new_id, images=saved_images)
    tree = load_tree()
    ctx = []
    if tree_node_id and tree_node_id in tree["nodes"]:
        ctx = build_path_messages(tree_node_id, tree)
    file_ctx = "".join(f"用户上传了文件 `{f['name']}`，内容如下：\n```\n{f['content']}\n```\n\n" for f in files) if files else ""
    meta = f"[时间: {datetime.now().isoformat()}] [IP: {request.remote_addr}] "
    # ★ 带图片消息：content 为内容块数组（text + image_url），DeepSeek 要求图片仅出现在 user 消息
    text_content = meta + file_ctx + user_input
    ctx.append({"role": "user", "content": _build_user_content_with_images(text_content, [{"data_url": i['data_url']} for i in images])})
    if not ctx or ctx[0].get('role')!='system':
        ctx.insert(0,{"role":"system","content":_effective_chat_system()})
    threading.Thread(target=_generate_chat_background, args=(new_id, ctx, actual_model, r_eff, extra, user_input), daemon=True).start()
    return jsonify({"nodeId": new_id, "generating": True, "model": actual_model})

# ★ 2026-10-05：对话模式的「停止生成」入口（见 cancelled_nodes 的说明）。
@app.route('/api/cancel_node', methods=['POST'])
def cancel_node():
    data = request.get_json(silent=True) or {}
    nid = data.get('node_id') or data.get('nodeId')
    if not nid:
        return jsonify({"success": False, "error": "缺少 node_id"}), 400
    with cancelled_nodes_lock:
        cancelled_nodes.add(nid)
    logging.info(f"[CHAT] 节点 {nid} 已被用户停止生成")
    # 立即把节点的 generating 置假，前端下一次轮询就会收尾（不必等线程退出）
    try:
        update_node_in_tree(nid, generating=False)
    except Exception as e:
        logging.warning(f"[CHAT] 停止节点 {nid} 时更新状态失败: {e}")
    return jsonify({"success": True})

# ---------- 自定义客户端下载（组件勾选） ----------
@app.route('/api/client-components')
def get_client_components():
    """返回客户端各组件信息（名称、描述、大小、是否存在），供前端展示复选框。

    所有组件统一以 client.zip 完整包为唯一来源（不再读取本地 client/ 目录）。
    """
    result = {}
    defaults = {"agent_py", "fetch_page"}  # 默认勾选的组件
    for key, comp in CLIENT_COMPONENTS.items():
        zip_exists, zip_size = _zip_component_info(comp)
        result[key] = {
            "name": comp["name"],
            "desc": comp["desc"],
            "size": zip_size,
            "size_fmt": _fmt_size(zip_size),
            "exists": zip_exists,
            "optional": comp["optional"],
            "default_selected": key in defaults,
        }
    return jsonify(result)

@app.route('/api/build-client-zip', methods=['POST'])
def build_client_zip():
    """根据用户选择的组件，动态打包并下载自定义 zip。

    所有组件统一从 client.zip 完整包中提取（不再读取本地 client/ 目录）。
    """
    selected = request.json.get("components", [])
    if not selected:
        return jsonify({"error": "请至少选择一个组件"}), 400

    if not os.path.exists(CLIENT_ZIP_PATH):
        return jsonify({"error": "client.zip 不存在，无法打包"}), 400

    zip_entries = set()   # 需要从 client.zip 提取的条目名（相对 client/ 的路径）
    for key in selected:
        comp = CLIENT_COMPONENTS.get(key)
        if not comp:
            continue
        for n in _zip_entries_for_comp(comp):
            zip_entries.add(n)

    if not zip_entries:
        return jsonify({"error": "所选组件在 client.zip 中不存在，无法打包"}), 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 从完整包 client.zip 提取所选组件条目
        with zipfile.ZipFile(CLIENT_ZIP_PATH) as src:
            for n in sorted(zip_entries):
                if n.endswith('/'):
                    continue  # 目录条目无需单独写入
                zf.writestr(n, src.read(n))

        # 确保 client/workspace 目录在 zip 中存在（agent 需要该目录）
        has_workspace = any(n.startswith('client/workspace/') for n in zip_entries)
        if not has_workspace:
            zf.writestr("client/workspace/.keep", "")

    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        **_send_kw('deepseek_client.zip')
    )

@app.route('/download/client.zip')
def download_client():
    zip_path = os.path.join(WORK_DIR, 'client.zip')
    if not os.path.exists(zip_path):
        return jsonify({"error": "client.zip 不存在"}), 404
    return send_from_directory(WORK_DIR, 'client.zip', as_attachment=True, **_send_kw('client.zip'))

@app.route('/download/agent.exe')
def download_agent_exe():
    """轻量版下载：仅提供 agent.exe（单个可执行文件，不含内置 Python/Node.js 等），下载更快。"""
    if not os.path.exists(AGENT_EXE_PATH):
        return jsonify({"error": "agent.exe 不存在"}), 404
    return send_from_directory(CLIENT_DIR, 'agent.exe', as_attachment=True, **_send_kw('agent.exe'))

@app.route('/api/downloads')
def downloads_info():
    """返回客户端代理文件的下载信息（是否存在、大小、下载地址），供前端展示。"""
    def _info(name, path, url):
        exists = os.path.exists(path)
        return {
            "name": name,
            "exists": exists,
            "size": os.path.getsize(path) if exists else 0,
            "url": url,
        }
    return jsonify({
        "client_zip": _info("client.zip", os.path.join(WORK_DIR, 'client.zip'), "/download/client.zip"),
        "agent_exe": _info("agent.exe", AGENT_EXE_PATH, "/download/agent.exe"),
    })

@app.route('/api/version')
def api_version():
    """当前服务器版本（最新版由服务器决定）。"""
    return jsonify({"success": True, "app": "DeepChat Server", "version": APP_VERSION})


@app.route('/')
def index():
    # ★ 客户端代理地址可经环境变量 AGENT_URL 覆盖（默认 5001；测试实例可用 5003 等端口）
    agent_url = os.environ.get("AGENT_URL", "http://localhost:5001")
    # ★ 2026-10-05：**禁止缓存首页**。
    #   原因：改完模板后浏览器一直拿旧页面（响应头里原本没有任何缓存控制），
    #   于是"明明改了却没生效"，用户与我都反复被误导（实测过好几轮：
    #   界面上跑的是旧 JS，服务端与磁盘代码却都是新的）。
    #   页面本身只有几百 KB 且必须随版本走，禁掉缓存最省心。
    # ★ 2026-10-23：把**服务器版本**注入页面。前端用它和本地代理自报的版本比对，
    #   低于服务器版本时提示升级（最新版由服务器决定）。
    resp = make_response(render_template('index.html', agent_url=agent_url,
                                         app_version=APP_VERSION))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# ★ 2026-10-05：api-key.env → settings.json 迁移（幂等），必须在**所有路由注册完**之后执行。
#
#   两个坑都踩过了，记下来免得再犯：
#     ① 放 `if __name__ == '__main__'` 里 → 本项目的测试启动器（tool/_test_server.py）
#        和打包后的 app.exe 都是用 importlib 加载本模块，那个块根本不执行，迁移永远不发生；
#     ② 放模块中段（比如紧挨 save_server_settings 定义）→ app.py 后面还有一堆
#        `@app.route(...)` 装饰器，提前做文件 IO 会让 Flask 认为"已经处理过请求了"，
#        抛 AssertionError: The setup method 'route' can no longer be called...
#   所以：模块级调用 + 放在文件最末尾。
_migrate_api_key_into_settings()

def _services_for_control():
    """控制面板要管理的服务清单（server 端：服务端 + 本机代理，两个都能控）。

    ★ 用「当前这个 exe 再起一个自己」的方式来启动服务端，所以控制面板拉起的
      服务端和用户直接双击 app.exe 跑的是同一个东西；源码运行时退回 python + app.py。
    """
    frozen = getattr(sys, 'frozen', False)
    self_exe = sys.executable if frozen else None
    agent_exe = os.path.join(CLIENT_DIR, 'agent.exe')
    return [
        {
            'key': 'server', 'label': '服务端 (Web UI)', 'port': 5000,
            'env_ports': ['PORT'], 'cwd': BASE_DIR,
            # ★ argv 必须是 ['--serve']：不带参数现在等于"打开控制面板"，
            #   若留空，面板点"启动"会再开出一个面板而不是起服务。
            'exe': self_exe, 'script': 'app.py', 'argv': ['--serve'],
        },
        {
            'key': 'agent', 'label': '本机代理 (操作执行)', 'port': 5001,
            'env_ports': ['AGENT_PORT', 'PORT'], 'cwd': CLIENT_DIR,
            'exe': agent_exe if os.path.isfile(agent_exe) else None,
            # ★ 同上：agent 也必须带 --serve，否则会开出一个 agent 面板而非服务。
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
        print(f'[错误] 无法加载控制面板模块: {e}')
        return 1
    return control.run_control_panel(
        _services_for_control(),
        title='DeepChat 控制面板（服务端）',
        state_dir=BASE_DIR,
        open_url=f'http://localhost:{os.environ.get("PORT", "5000")}',
    )


if __name__ == '__main__':
    # ★ 参数约定（产品入口就这两个）：
    #     --control / -c / --gui   打开图形控制面板（server 端面板可管「服务端 + 本机代理」）
    #     --serve                  直接起服务、不开面板
    #   **不带任何参数 = 打开控制面板。**
    #
    #   为什么默认是面板而不是直接起服务：exe 是无控制台构建（--noconsole），
    #   双击时既没有窗口也没有任何输出，用户只看到"什么反应都没有"，以为打开失败了。
    #   打开面板至少有明确界面，点一下就能起服务。
    #   需要"直接起服务"的场景（脚本、后台、想接控制台看日志）用 --serve / run.bat --console。
    _args = [a.lower() for a in sys.argv[1:]]
    if not any(a in ('--serve', '--service') for a in _args):
        sys.exit(_run_control_panel())

    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW('DeepChat Server')
    except Exception:
        pass
    port = int(os.environ.get('PORT', '5000'))
    logging.info(f"服务器启动，端口 {port}")
    app.run(debug=False, threaded=True, host='0.0.0.0', port=port)