#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepCoder —— 面向算法竞赛的本地 IDE（网页端 + 服务器）
纯 Python 标准库（winapi.py 用 ctypes 直调 Windows API），零第三方依赖，可离线运行。

用法：python server.py [--port 8765] [--host 0.0.0.0] [--no-browser]
"""

import os
import sys
import json
import time
import threading
import subprocess
import shutil
import webbrowser
import argparse
import re
import socket
import ipaddress
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs, quote

BASE_DIR = Path(__file__).parent.absolute()

import winapi  # 纯标准库 Windows 进程监控（替代 psutil）

# 关闭 Windows 错误报告弹窗/延迟（子进程继承），崩溃程序立即退出并给出退出码
winapi.disable_error_dialogs()

STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
WORKSPACE_DIR = Path(os.getcwd()).absolute()
BUILD_DIR = WORKSPACE_DIR / ".build"
IDE_CONFIG_FILE = WORKSPACE_DIR / ".ide.json"

WORKSPACE_DIR.mkdir(exist_ok=True)
BUILD_DIR.mkdir(exist_ok=True)

COMPILER = "g++"
DEFAULT_COMPILE_FLAGS = ["-O2", "-std=c++14", "-Wl,--stack=2147483647", "-static"]
DEFAULT_TIME_LIMIT = 2000
DEFAULT_MEMORY_LIMIT = 256

# 对拍任务状态
HACK_STATE = {}
HACK_LOCK = threading.Lock()

# 交互会话
INTERACTIVE = {}
INTERACTIVE_LOCK = threading.Lock()

# 启动时通过命令行指定的待打开文件
INITIAL_FILE = None

# ---------- AI 聊天（参考 DeepChat 操作模式，纯标准库调用 DeepSeek API） ----------
AI_KEY_FILE = BASE_DIR / 'api-key.env'
AI_API_URL = 'https://api.deepseek.com/chat/completions'
AI_DEFAULT_MODEL = 'deepseek-v4-flash'
AI_SYSTEM_PROMPT = """你是 DeepSeek，一个由深度求索公司创造的AI助手。知识截止于2025年5月，用流畅的中文回复。上下文长度为 1M，输出长度最大 384K。

## 核心原则
- **准确性**：不确定时明确告知，不编造。
- **深度思考**：对复杂问题，在内部充分推理后再作答，**禁止在正式回答中出现"左右互搏"式的自我怀疑**。一旦给出答案，必须自信、连贯。
- **任务完整性**：面对庞大或复杂的任务，**必须坚持完成全部内容**，不得以"时间有限""不可能"等理由中途放弃。
- **安全**：拒绝有害、违法内容。

## 工具能力（读写权限）
你可以调用以下工具来读写**当前工作区**的文件（这是 DeepCoder 竞赛 IDE 的工作目录，路径：`{workspace_path}`）：
- `list_dir(path)`：列出目录内容（path 为相对路径，如 "." 或 "src"）
- `read_file(path)`：读取文本文件内容（相对路径）
- `write_file(path, content)`：写入/覆盖文本文件（相对路径，content 为完整文件内容）

规则（必须遵守）：
- 所有路径均为**相对路径**，禁止使用绝对路径或 `../` 逃逸。
- 写文件前若不确定目录结构，先用 `list_dir` 查看。
- 文件读写务必基于真实工具结果，**禁止编造文件内容**。
- **当用户要求查看、读取、写入或修改文件时，你必须调用对应工具完成，禁止只输出 shell 命令（如 ls、cat、echo >）或假装完成**。
- 工具结果中如有"截断"字样，说明内容过长已被截断，可分段读取。

## 输出规范
- 使用 Markdown 增强可读性。
- 数学公式请用 $...$ 或 $$...$$ 包裹 LaTeX，不得使用 \(...\) 或 \[...\]。
- **加粗格式**：使用 `**文本**` 时，要确保 `**` 内部的第一个字符和最后一个字符**不能是标点符号**。

## 交互风格
- 默认简体中文，友好专业。"""


# ---------- AI 工具（读写权限，参考 DeepChat 操作模式的工具调用） ----------
AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作区中某个目录的内容（文件与子目录名）。path 为相对路径，如 '.' 表示工作区根目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，默认 '.'"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区中文本文件的内容（相对路径）。文件过大时自动截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 'main.cpp' 或 'src/a.py'"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/覆盖工作区中的文本文件（相对路径，content 为完整文件内容）。目录不存在时自动创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 'main.cpp' 或 'src/a.py'"},
                    "content": {"type": "string", "description": "完整的文件内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
]

AI_TOOL_MAX_READ = 200 * 1024      # 读取上限 200KB
AI_TOOL_MAX_WRITE = 200 * 1024     # 写入上限 200KB
AI_TOOL_MAX_ROUNDS = 10            # 单轮对话最多工具调用轮数（防死循环）
AI_TOOL_LIST_LIMIT = 500           # list_dir 最多返回条目数
AI_TOOL_RESULT_MAX = 30000         # 工具结果回传 API 的上限（字符）


def _ai_tool_bad_path(path):
    """构造路径非法错误信息"""
    return "错误：路径非法（仅允许工作区内相对路径，禁止 ../ 或绝对路径）"


def execute_ai_tool(name, args):
    """执行 AI 工具调用，返回 (ok, text)。ok=False 时 text 为错误信息（仍作为 tool 结果回传）。"""
    import json as _json
    args = args or {}
    try:
        if name == 'list_dir':
            rel = str(args.get('path') or '.').strip() or '.'
            rel = rel.lstrip('/\\')
            if not rel:
                rel = '.'
            try:
                p = safe_path(rel) if rel != '.' else WORKSPACE_DIR
            except ValueError:
                return False, _ai_tool_bad_path(rel)
            if not p.is_dir():
                return False, "错误：%s 不是目录" % rel
            entries = []
            for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                entries.append(('📄 ' if e.is_file() else '📁 ') + e.name + ('/' if e.is_dir() else ''))
                if len(entries) >= AI_TOOL_LIST_LIMIT:
                    entries.append('……（条目过多，仅显示前 %d 个）' % AI_TOOL_LIST_LIMIT)
                    break
            if not entries:
                return True, "（空目录）"
            return True, "\n".join(entries)

        if name == 'read_file':
            rel = str(args.get('path') or '').strip()
            try:
                p = safe_path(rel)
            except ValueError:
                return False, _ai_tool_bad_path(rel)
            if not p.is_file():
                return False, "错误：文件不存在：%s" % rel
            size = p.stat().st_size
            if size > AI_TOOL_MAX_READ:
                return False, "错误：文件过大（%.1f KB > 上限 %d KB），请分块处理" % (size / 1024, AI_TOOL_MAX_READ // 1024)
            try:
                data = read_text_auto(p)
            except Exception as e:
                return False, "错误：读取失败（%s），可能是二进制文件" % e
            if len(data) > AI_TOOL_MAX_READ:
                data = data[:AI_TOOL_MAX_READ]
                return True, "%s\n……（内容过长已截断至 %d 字符）" % (data, AI_TOOL_MAX_READ)
            return True, data

        if name == 'write_file':
            rel = str(args.get('path') or '').strip()
            content = args.get('content')
            if content is None:
                return False, "错误：缺少 content 参数"
            content = str(content)
            if len(content.encode('utf-8')) > AI_TOOL_MAX_WRITE:
                return False, "错误：内容过大（> %d KB），请分次写入" % (AI_TOOL_MAX_WRITE // 1024)
            try:
                p = safe_path(rel)
            except ValueError:
                return False, _ai_tool_bad_path(rel)
            # 禁止写入构建目录与备份目录
            for banned in ('.build', '.backups'):
                parts = p.relative_to(WORKSPACE_DIR).parts if p != WORKSPACE_DIR else ()
                if banned in parts:
                    return False, "错误：禁止写入 %s 目录" % banned
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding='utf-8')
            except Exception as e:
                return False, "错误：写入失败：%s" % e
            return True, "已写入 %s（%d 字符）" % (rel, len(content))

        return False, "错误：未知工具：%s" % name
    except Exception as e:
        return False, "错误：工具执行异常：%s" % e


def ai_system_prompt():
    """返回 AI 系统提示词（动态注入当前工作区路径）"""
    return AI_SYSTEM_PROMPT.replace('{workspace_path}', str(WORKSPACE_DIR))


def read_ai_key():
    """读取 API Key（api-key.env，与 DeepChat 一致）"""
    try:
        if AI_KEY_FILE.exists():
            k = AI_KEY_FILE.read_text(encoding='utf-8').strip()
            if k:
                return k
    except Exception:
        pass
    return None


def save_ai_key(key):
    """保存 API Key 到 api-key.env"""
    key = (key or '').strip()
    if not key:
        raise ValueError("API Key 不能为空")
    AI_KEY_FILE.write_text(key, encoding='utf-8')
    return True


def ai_stream_chat(messages, model, reasoning_effort, thinking, emit, tools=None, max_tool_rounds=AI_TOOL_MAX_ROUNDS):
    """流式调用 DeepSeek Chat API（纯标准库 urllib，SSE 逐行解析），支持工具调用循环。

    messages: [{role, content}, ...]（调用方保证首条为 system）
    tools: OpenAI 格式工具列表；传 None 时退化为纯对话（无工具调用）
    emit: 回调函数 emit(event_dict)，事件：thinking / content_delta / tool_call / tool_result / usage / done / error
    返回 None 表示成功；返回字符串表示错误信息（已 emit error 事件）。
    """
    import urllib.request
    import urllib.error
    key = read_ai_key()
    if not key:
        emit({"type": "error", "content": "未配置 API Key，请在聊天面板点击 🔑 设置"})
        return "no key"

    usage_total = {}

    def _merge_usage(u):
        if not u:
            return
        for k, v in u.items():
            if isinstance(v, (int, float)):
                usage_total[k] = usage_total.get(k, 0) + v

    def _one_call(msgs):
        """单次流式调用；返回 (finish_reason, content, tool_calls)"""
        payload = {
            "model": model or AI_DEFAULT_MODEL,
            "messages": msgs,
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": reasoning_effort or "max",
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if tools:
            payload["tools"] = tools
        req = urllib.request.Request(
            AI_API_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode('utf-8', 'replace')[:500]
            except Exception:
                pass
            emit({"type": "error", "content": "API 请求失败（HTTP %d）：%s" % (e.code, detail)})
            return None, None, None
        except Exception as e:
            emit({"type": "error", "content": "网络错误：%s" % e})
            return None, None, None
        try:
            buf = b''
            finish_reason = None
            content_parts = []
            tc_acc = {}
            tc_order = []
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                # 按 \n 切行解析 SSE
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.decode('utf-8', 'replace').strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[len('data:'):].strip()
                    if data == '[DONE]':
                        finish_reason = finish_reason or 'stop'
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get('choices') or []
                    if obj.get('usage'):
                        _merge_usage(obj['usage'])
                    if not choices:
                        continue
                    ch = choices[0]
                    delta = ch.get('delta') or {}
                    if delta.get('reasoning_content'):
                        emit({"type": "thinking", "content": delta['reasoning_content']})
                    if delta.get('content'):
                        content_parts.append(delta['content'])
                        emit({"type": "content_delta", "content": delta['content']})
                    if delta.get('tool_calls'):
                        for tc in delta.get('tool_calls') or []:
                            idx = tc.get('index', 0)
                            if idx not in tc_acc:
                                tc_acc[idx] = {"id": "", "name": "", "arguments": ""}
                                tc_order.append(idx)
                            if tc.get('id'):
                                tc_acc[idx]["id"] = tc['id']
                            fn = tc.get('function') or {}
                            if fn.get('name'):
                                tc_acc[idx]["name"] = fn['name']
                            if fn.get('arguments'):
                                tc_acc[idx]["arguments"] += fn['arguments']
                    if ch.get('finish_reason'):
                        finish_reason = ch['finish_reason']
            tool_calls = None
            if tc_order:
                tool_calls = []
                for idx in tc_order:
                    t = tc_acc[idx]
                    if t["id"] and t["name"]:
                        tool_calls.append({
                            "id": t["id"],
                            "type": "function",
                            "function": {"name": t["name"], "arguments": t["arguments"]},
                        })
            return finish_reason, ''.join(content_parts), tool_calls
        except Exception as e:
            emit({"type": "error", "content": "读取响应失败：%s" % e})
            return None, None, None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    msgs = list(messages)
    for _round in range(max_tool_rounds + 1):
        finish_reason, content, tool_calls = _one_call(msgs)
        if tool_calls:
            # 组装 assistant 消息（带 tool_calls），追加到上下文
            assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
            msgs.append(assistant_msg)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or '{}')
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                emit({"type": "tool_call", "name": name, "arguments": args})
                ok, text = execute_ai_tool(name, args)
                emit({"type": "tool_result", "name": name, "ok": ok, "content": text})
                # 工具结果回传时截断，避免撑爆上下文
                tool_text = text if ok else ("[失败] " + text)
                if len(tool_text) > AI_TOOL_RESULT_MAX:
                    tool_text = tool_text[:AI_TOOL_RESULT_MAX] + "\n……（工具结果过长已截断）"
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_text})
            continue  # 有工具调用，进入下一轮
        # 无工具调用：正常结束
        emit({"type": "done", "finish_reason": finish_reason or 'stop', "usage": usage_total})
        return None
    emit({"type": "error", "content": "工具调用轮数超过上限（%d 轮），已停止" % max_tool_rounds})
    return "tool round limit"


# ---------- AI 代码补全（flash 不思考，5 秒静止触发，灰色提示） ----------
AI_COMPLETE_SYSTEM = """你是代码补全引擎。用户会提供一段代码，其中用 ⟦CURSOR⟧ 标记光标位置。
请补全 ⟦CURSOR⟧ 处的代码。

要求：
- 只输出补全的代码本身（不含 ⟦CURSOR⟧ 标记），不要任何解释，不要 markdown 代码块围栏。
- **缩进必须与上下文完全一致**：
  - 光标前已经存在的缩进不要重复输出（从光标处直接续写内容，不要再次输出前导空格）。
  - 若补全包含换行（新行），后续行必须按代码块层级正确缩进：与光标所在行的缩进对齐，函数体/循环体内逐层 +4 空格（或与上下文所用缩进一致），闭合括号（`}`、`)`、`]`）与对应开启语句对齐。
  - 严禁出现换行后缩进丢失（顶格）或缩进错误的情况。
- 补全应与上下文风格一致（命名、语言习惯）。
- 若光标处无需补全或不确定，输出空字符串。
- 长度控制在 1~8 行，通常 1~3 行即可。"""


def _pos_to_index(content, line, ch):
    """把 (line, ch)（CM 从 0 起）转为字符串索引"""
    try:
        lines = content.split('\n')
        idx = sum(len(l) + 1 for l in lines[:max(line, 0)])
    except Exception:
        idx = 0
    return idx + max(ch, 0)


def ai_complete_suggestion(path, content, line, ch):
    """AI 代码补全：flash + 不思考，快速返回光标处补全建议；失败或无需补全返回 None。"""
    import urllib.request
    import urllib.error
    key = read_ai_key()
    if not key:
        return None
    idx = _pos_to_index(content, line, ch)
    prefix = content[max(0, idx - 4000):idx]
    suffix = content[idx:idx + 800]
    # 计算光标所在行的前导缩进（空格数），提示模型保持对齐
    try:
        _lines_all = content.split('\n')
        cur_line_text = _lines_all[max(line, 0)] if max(line, 0) < len(_lines_all) else ''
        indent_len = len(cur_line_text) - len(cur_line_text.lstrip(' \t'))
        at_line_end = ch >= len(cur_line_text)
    except Exception:
        indent_len, at_line_end = 0, False
    user = ("```\n%s⟦CURSOR⟧%s\n```\n"
            "请补全 ⟦CURSOR⟧ 处的代码（只输出补全内容）。"
            "注意：光标所在行已有 %d 个空格的前导缩进，补全首行不要重复该缩进；"
            "若补全包含换行，后续行必须按代码层级保持正确缩进（与光标行对齐，内部层级递增，闭合括号对齐）。"
            % (prefix, suffix, indent_len))
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": AI_COMPLETE_SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "reasoning_effort": "low",
        "thinking": {"type": "disabled"},
        "max_tokens": 256,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        AI_API_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode('utf-8', 'replace'))
    except Exception:
        return None
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None
    if not text:
        return None
    # 清理：去掉代码块围栏与多余空白
    import re as _re
    text = _re.sub(r'^```[a-zA-Z0-9_+-]*\s*', '', text)
    text = _re.sub(r'```\s*$', '', text)
    text = text.rstrip()
    # 保守缩进校正：光标在行尾时，若 AI 首行重复输出了已有的前导缩进，去掉它（防双倍缩进）
    if at_line_end and indent_len > 0 and text:
        _m = _re.match(r'^[ \t]+', text)
        if _m:
            _w = len(_m.group(0).replace('\t', '    '))
            if _w >= indent_len:
                text = text[len(_m.group(0)):]
    if len(text) > 600:
        text = text[:600]
    return text


def _reader(pipe, chunks):
    """持续读取子进程管道到块列表，直到 EOF（不访问 proc，线程安全；超 64MB 丢弃）"""
    total = 0
    try:
        while True:
            data = pipe.read1(4096)
            if data:
                if total < OUTPUT_LIMIT:
                    chunks.append(data)
                    total += len(data)
            else:
                try:
                    rest = pipe.read()
                    if rest:
                        if total < OUTPUT_LIMIT:
                            chunks.append(rest)
                except Exception:
                    pass
                break
    except Exception:
        pass


def kill_proc_tree(pid):
    """终止进程及其所有子进程"""
    winapi.kill_proc_tree(pid)


# ---------- 配置 ----------
def load_config():
    if not IDE_CONFIG_FILE.exists():
        return {}
    try:
        with open(IDE_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    IDE_CONFIG_FILE.parent.mkdir(exist_ok=True)
    with open(IDE_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_problem_config(name):
    cfg = load_config()
    return cfg.get("problems", {}).get(name, {})


def set_problem_config(name, patch):
    cfg = load_config()
    problems = cfg.setdefault("problems", {})
    problems.setdefault(name, {})
    problems[name].update(patch)
    save_config(cfg)


def get_compile_config():
    """读取全局编译配置：编译器 + 参数（未配置时用默认值）"""
    c = (load_config().get('compile') or {})
    compiler = (c.get('compiler') or '').strip() or COMPILER
    flags_text = (c.get('flags') or '').strip()
    flags = flags_text.split() if flags_text else DEFAULT_COMPILE_FLAGS
    return compiler, flags


# ---------- 路径安全 ----------
def safe_path(rel):
    """把相对路径解析到 workspace 内，防止 ../ 逃逸"""
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        raise ValueError("非法路径")
    p = (WORKSPACE_DIR / rel).resolve()
    if not (str(p) == str(WORKSPACE_DIR) or str(p).startswith(str(WORKSPACE_DIR) + os.sep)):
        raise ValueError("非法路径")
    return p


def _parse_query(q):
    params = {}
    for kv in q.split('&'):
        if '=' in kv:
            k, v = kv.split('=', 1)
            params[unquote(k)] = unquote(v)
    return params


# ---------- 文件树 ----------
def build_tree():
    def walk(d: Path):
        entries = []
        for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name.startswith('.'):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "type": "dir",
                                "path": str(child.relative_to(WORKSPACE_DIR)).replace('\\', '/'),
                                "children": walk(child)})
            else:
                entries.append({"name": child.name, "type": "file",
                                "path": str(child.relative_to(WORKSPACE_DIR)).replace('\\', '/')})
        return entries
    return walk(WORKSPACE_DIR)


# ---------- 代码补全 ----------
_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

CPP_KEYWORDS = [
    'alignas','alignof','and','and_eq','asm','auto','bitand','bitor','bool','break','case','catch',
    'char','char8_t','char16_t','char32_t','class','compl','concept','const','consteval','constexpr',
    'constinit','const_cast','continue','co_await','co_return','co_yield','decltype','default',
    'delete','do','double','dynamic_cast','else','enum','explicit','export','extern','false','float',
    'for','friend','goto','if','inline','int','long','mutable','namespace','new','noexcept','not',
    'not_eq','nullptr','operator','or','or_eq','private','protected','public','register',
    'reinterpret_cast','requires','return','short','signed','sizeof','static','static_assert',
    'static_cast','struct','switch','template','this','thread_local','throw','true','try','typedef',
    'typeid','typename','union','unsigned','using','virtual','void','volatile','wchar_t','while',
    'xor','xor_eq',
]

CPP_BUILTINS = [  # (名称, 说明)
    ('vector','std 容器'), ('map','std 容器'), ('set','std 容器'), ('unordered_map','std 容器'),
    ('unordered_set','std 容器'), ('queue','std 容器'), ('stack','std 容器'), ('deque','std 容器'),
    ('priority_queue','std 容器'), ('pair','std 容器'), ('tuple','std 容器'), ('string','std 字符串'),
    ('sort','algorithm'), ('reverse','algorithm'), ('lower_bound','algorithm'), ('upper_bound','algorithm'),
    ('binary_search','algorithm'), ('accumulate','algorithm'), ('unique','algorithm'), ('max_element','algorithm'),
    ('min_element','algorithm'), ('next_permutation','algorithm'), ('prev_permutation','algorithm'),
    ('is_sorted','algorithm'), ('nth_element','algorithm'), ('partial_sort','algorithm'), ('merge','algorithm'),
    ('transform','algorithm'), ('copy','algorithm'), ('fill','algorithm'), ('generate','algorithm'),
    ('count_if','algorithm'), ('find_if','algorithm'), ('for_each','algorithm'), ('min','algorithm'),
    ('max','algorithm'), ('swap','algorithm'), ('memset','cstring'), ('memcpy','cstring'), ('strlen','cstring'),
    ('strcmp','cstring'), ('strcpy','cstring'), ('strcat','cstring'), ('sprintf','cstdio'), ('sscanf','cstdio'),
    ('printf','cstdio'), ('scanf','cstdio'), ('fprintf','cstdio'), ('fscanf','cstdio'), ('fopen','cstdio'),
    ('fclose','cstdio'), ('fread','cstdio'), ('fwrite','cstdio'), ('puts','cstdio'), ('gets','cstdio'),
    ('cin','iostream'), ('cout','iostream'), ('endl','iostream'), ('getline','iostream'),
    ('ios::sync_with_stdio','iostream'), ('cin.tie','iostream'),
    ('push_back','容器方法'), ('emplace_back','容器方法'), ('push','容器方法'), ('pop','容器方法'),
    ('top','容器方法'), ('front','容器方法'), ('back','容器方法'), ('begin','容器方法'), ('end','容器方法'),
    ('rbegin','容器方法'), ('rend','容器方法'), ('size','容器方法'), ('empty','容器方法'), ('clear','容器方法'),
    ('resize','容器方法'), ('reserve','容器方法'), ('capacity','容器方法'), ('insert','容器方法'),
    ('erase','容器方法'), ('find','容器方法'), ('count','容器方法'), ('at','容器方法'), ('data','容器方法'),
    ('assign','容器方法'), ('append','string 方法'), ('substr','string 方法'), ('compare','string 方法'),
    ('c_str','string 方法'), ('stoi','string 转换'), ('stoll','string 转换'), ('stod','string 转换'),
    ('to_string','string 转换'), ('make_pair','utility'), ('make_tuple','utility'), ('tie','tuple'),
    ('abs','cmath'), ('pow','cmath'), ('sqrt','cmath'), ('log','cmath'), ('log2','cmath'), ('log10','cmath'),
    ('exp','cmath'), ('floor','cmath'), ('ceil','cmath'), ('round','cmath'), ('fabs','cmath'), ('fmod','cmath'),
    ('rand','cstdlib'), ('srand','cstdlib'), ('time','ctime'), ('clock','ctime'), ('atoi','cstdlib'),
    ('atol','cstdlib'), ('atof','cstdlib'), ('exit','cstdlib'), ('abort','cstdlib'),
    ('int64_t','cstdint'), ('uint64_t','cstdint'), ('int32_t','cstdint'), ('size_t','cstddef'),
    ('unique_ptr','memory'), ('shared_ptr','memory'), ('make_unique','memory'), ('make_shared','memory'),
    ('iterator','iterator'), ('const_iterator','iterator'), ('bits/stdc++.h','万能头'),
    ('long long','整型'), ('FILE','cstdio 类型'), ('NULL','常量'), ('EOF','cstdio 常量'),
]

CPP_STD_NAMES = [  # std:: 限定符下的候选
    'vector','map','set','unordered_map','unordered_set','queue','stack','deque','priority_queue',
    'pair','tuple','string','string_view','array','list','forward_list','multimap','multiset',
    'unordered_multimap','unordered_multiset','bitset','complex','optional','variant','any',
    'sort','reverse','lower_bound','upper_bound','binary_search','accumulate','unique','max','min',
    'swap','max_element','min_element','next_permutation','prev_permutation','fill','copy','merge',
    'transform','count','count_if','find','find_if','for_each','is_sorted','nth_element','partial_sort',
    'make_pair','make_tuple','tie','begin','end','to_string','stoi','stoll','stod','atoi','atol','atof',
    'abs','pow','sqrt','floor','ceil','round','log','log2','rand','srand','time','clock','memset',
    'memcpy','strlen','strcmp','strcpy','printf','scanf','fopen','fclose','cin','cout','endl','cerr',
    'getline','ios','istream','ostream','ifstream','ofstream','fstream','stringstream','iterator',
    'istream_iterator','ostream_iterator','back_inserter','unique_ptr','shared_ptr','make_unique',
    'make_shared','function','bind','ref','cref','numeric_limits','hash','less','greater','equal_to',
    'pair_hash','exception','runtime_error','logic_error','length_error','out_of_range','invalid_argument',
    'move','forward','declval','distance','advance','prev','next','size_t','int64_t','uint64_t',
    'numeric_limits<int>','numeric_limits<long long>',
]

PY_KEYWORDS = [
    'False','None','True','and','as','assert','async','await','break','class','continue','def','del',
    'elif','else','except','finally','for','from','global','if','import','in','is','lambda','nonlocal',
    'not','or','pass','raise','return','try','while','with','yield',
]

PY_BUILTINS = [  # (名称, 说明)
    ('print','内置函数'), ('len','内置函数'), ('range','内置函数'), ('int','内置函数'), ('str','内置函数'),
    ('float','内置函数'), ('bool','内置函数'), ('list','内置函数'), ('dict','内置函数'), ('set','内置函数'),
    ('tuple','内置函数'), ('bytes','内置函数'), ('bytearray','内置函数'), ('complex','内置函数'),
    ('input','内置函数'), ('open','内置函数'), ('sorted','内置函数'), ('reversed','内置函数'),
    ('enumerate','内置函数'), ('zip','内置函数'), ('map','内置函数'), ('filter','内置函数'), ('sum','内置函数'),
    ('min','内置函数'), ('max','内置函数'), ('abs','内置函数'), ('round','内置函数'), ('pow','内置函数'),
    ('divmod','内置函数'), ('chr','内置函数'), ('ord','内置函数'), ('hex','内置函数'), ('oct','内置函数'),
    ('bin','内置函数'), ('format','内置函数'), ('all','内置函数'), ('any','内置函数'), ('next','内置函数'),
    ('iter','内置函数'), ('hash','内置函数'), ('id','内置函数'), ('type','内置函数'), ('isinstance','内置函数'),
    ('issubclass','内置函数'), ('repr','内置函数'), ('super','内置函数'), ('vars','内置函数'), ('dir','内置函数'),
    ('globals','内置函数'), ('locals','内置函数'), ('eval','内置函数'), ('exec','内置函数'), ('compile','内置函数'),
    ('__init__','特殊方法'), ('__str__','特殊方法'), ('__repr__','特殊方法'), ('__eq__','特殊方法'),
    ('__lt__','特殊方法'), ('__len__','特殊方法'), ('__getitem__','特殊方法'), ('__setitem__','特殊方法'),
    ('__enter__','特殊方法'), ('__exit__','特殊方法'), ('__name__','特殊变量'), ('__main__','特殊变量'),
    ('append','list 方法'), ('extend','list 方法'), ('insert','list 方法'), ('remove','list 方法'),
    ('pop','list 方法'), ('clear','list 方法'), ('copy','list 方法'), ('count','list 方法'), ('index','list 方法'),
    ('sort','list 方法'), ('reverse','list 方法'), ('update','dict 方法'), ('keys','dict 方法'),
    ('values','dict 方法'), ('items','dict 方法'), ('get','dict 方法'), ('setdefault','dict 方法'),
    ('popitem','dict 方法'), ('add','set 方法'), ('discard','set 方法'), ('union','set 方法'),
    ('intersection','set 方法'), ('difference','set 方法'), ('join','str 方法'), ('split','str 方法'),
    ('strip','str 方法'), ('lstrip','str 方法'), ('rstrip','str 方法'), ('replace','str 方法'),
    ('startswith','str 方法'), ('endswith','str 方法'), ('find','str 方法'), ('rfind','str 方法'),
    ('format','str 方法'), ('encode','str 方法'), ('decode','str 方法'), ('upper','str 方法'),
    ('lower','str 方法'), ('title','str 方法'), ('capitalize','str 方法'), ('isdigit','str 方法'),
    ('isalpha','str 方法'), ('isalnum','str 方法'), ('isnumeric','str 方法'), ('splitlines','str 方法'),
    ('zfill','str 方法'), ('ljust','str 方法'), ('rjust','str 方法'), ('center','str 方法'),
    ('read','文件方法'), ('readline','文件方法'), ('readlines','文件方法'), ('write','文件方法'),
    ('writelines','文件方法'), ('flush','文件方法'), ('seek','文件方法'), ('tell','文件方法'), ('close','文件方法'),
    ('Exception','内置异常'), ('ValueError','内置异常'), ('TypeError','内置异常'), ('KeyError','内置异常'),
    ('IndexError','内置异常'), ('RuntimeError','内置异常'), ('ImportError','内置异常'),
    ('ZeroDivisionError','内置异常'), ('AttributeError','内置异常'), ('OSError','内置异常'),
    ('IOError','内置异常'), ('ArithmeticError','内置异常'), ('StopIteration','内置异常'),
    ('NameError','内置异常'), ('NotImplementedError','内置异常'), ('MemoryError','内置异常'),
    ('KeyboardInterrupt','内置异常'), ('SystemExit','内置异常'), ('GeneratorExit','内置异常'),
    ('math','标准库'), ('random','标准库'), ('sys','标准库'), ('os','标准库'), ('re','标准库'),
    ('time','标准库'), ('datetime','标准库'), ('collections','标准库'), ('itertools','标准库'),
    ('functools','标准库'), ('heapq','标准库'), ('bisect','标准库'), ('json','标准库'), ('queue','标准库'),
    ('threading','标准库'), ('subprocess','标准库'), ('math.sqrt','math'), ('math.gcd','math'),
    ('math.lcm','math'), ('math.floor','math'), ('math.ceil','math'), ('math.pow','math'),
    ('math.fabs','math'), ('random.randint','random'), ('random.shuffle','random'), ('random.random','random'),
    ('random.choice','random'), ('random.sample','random'), ('sys.stdin','sys'), ('sys.stdout','sys'),
    ('sys.exit','sys'), ('os.path','os'), ('time.time','time'), ('time.sleep','time'),
    ('collections.deque','collections'), ('collections.Counter','collections'), ('collections.defaultdict','collections'),
    ('itertools.permutations','itertools'), ('itertools.combinations','itertools'), ('itertools.product','itertools'),
    ('functools.lru_cache','functools'), ('heapq.heappush','heapq'), ('heapq.heappop','heapq'),
    ('heapq.heapify','heapq'), ('bisect.bisect_left','bisect'), ('bisect.bisect_right','bisect'),
    ('bisect.insort','bisect'), ('json.dumps','json'), ('json.loads','json'),
]

JAVA_KEYWORDS = [
    'abstract','assert','boolean','break','byte','case','catch','char','class','const','continue',
    'default','do','double','else','enum','extends','final','finally','float','for','goto','if',
    'implements','import','instanceof','int','interface','long','native','new','package','private',
    'protected','public','return','short','static','strictfp','super','switch','synchronized','this',
    'throw','throws','transient','try','void','volatile','while','true','false','null',
]

JAVA_BUILTINS = [  # (名称, 说明)
    ('System.out.println','标准输出'), ('System.out.print','标准输出'), ('System.out.printf','标准输出'),
    ('System.err.println','标准错误'), ('System.in','标准输入'), ('System.exit','系统'), ('System.currentTimeMillis','系统'),
    ('String','常用类'), ('StringBuilder','常用类'), ('StringBuffer','常用类'), ('Integer','包装类'),
    ('Long','包装类'), ('Double','包装类'), ('Float','包装类'), ('Boolean','包装类'), ('Character','包装类'),
    ('ArrayList','集合'), ('LinkedList','集合'), ('List','集合'), ('Map','集合'), ('HashMap','集合'),
    ('TreeMap','集合'), ('LinkedHashMap','集合'), ('Set','集合'), ('HashSet','集合'), ('TreeSet','集合'),
    ('LinkedHashSet','集合'), ('PriorityQueue','集合'), ('Deque','集合'), ('ArrayDeque','集合'), ('Queue','集合'),
    ('Stack','集合'), ('Collections','集合工具'), ('Arrays','数组工具'), ('Math','数学工具'),
    ('Math.abs','数学'), ('Math.max','数学'), ('Math.min','数学'), ('Math.sqrt','数学'), ('Math.pow','数学'),
    ('Math.random','数学'), ('Math.floor','数学'), ('Math.ceil','数学'), ('Math.round','数学'), ('Math.log','数学'),
    ('Math.sin','数学'), ('Math.cos','数学'), ('Math.PI','数学'), ('Arrays.sort','数组排序'),
    ('Arrays.fill','数组填充'), ('Arrays.toString','数组转字符串'), ('Arrays.asList','数组转列表'),
    ('Arrays.binarySearch','数组查找'), ('Collections.sort','列表排序'), ('Collections.reverse','列表反转'),
    ('Collections.max','求最大'), ('Collections.min','求最小'), ('Collections.shuffle','打乱'),
    ('Collections.frequency','统计频率'), ('Collections.fill','填充'),
    ('Scanner','输入类'), ('next','Scanner 方法'), ('nextInt','Scanner 方法'), ('nextLong','Scanner 方法'),
    ('nextLine','Scanner 方法'), ('nextDouble','Scanner 方法'), ('nextFloat','Scanner 方法'),
    ('hasNext','Scanner 方法'), ('hasNextInt','Scanner 方法'), ('hasNextLine','Scanner 方法'), ('close','Scanner 方法'),
    ('main','入口方法'), ('public static void main','入口方法'), ('equals','Object 方法'), ('hashCode','Object 方法'),
    ('toString','Object 方法'), ('compareTo','Comparable 方法'), ('length','String 方法'), ('charAt','String 方法'),
    ('substring','String 方法'), ('indexOf','String 方法'), ('lastIndexOf','String 方法'), ('contains','String 方法'),
    ('startsWith','String 方法'), ('endsWith','String 方法'), ('replace','String 方法'), ('trim','String 方法'),
    ('split','String 方法'), ('toCharArray','String 方法'), ('toLowerCase','String 方法'), ('toUpperCase','String 方法'),
    ('valueOf','String 静态方法'), ('parseInt','Integer 静态方法'), ('parseLong','Long 静态方法'),
    ('parseDouble','Double 静态方法'), ('append','StringBuilder 方法'), ('reverse','StringBuilder 方法'),
    ('add','List 方法'), ('remove','List 方法'), ('get','List 方法'), ('set','List 方法'), ('size','List 方法'),
    ('isEmpty','List 方法'), ('contains','List 方法'), ('clear','List 方法'), ('put','Map 方法'),
    ('get','Map 方法'), ('keySet','Map 方法'), ('values','Map 方法'), ('entrySet','Map 方法'),
    ('containsKey','Map 方法'), ('containsValue','Map 方法'), ('remove','Map 方法'), ('poll','Queue 方法'),
    ('offer','Queue 方法'), ('peek','Queue 方法'), ('push','Deque 方法'), ('pop','Deque 方法'),
    ('try-catch','异常处理'), ('try-with-resources','异常处理'), ('throws','异常声明'), ('throw','抛出异常'),
    ('Exception','异常类'), ('RuntimeException','异常类'), ('IOException','异常类'), ('NumberFormatException','异常类'),
    ('ArrayIndexOutOfBoundsException','异常类'), ('NullPointerException','异常类'), ('Scanner','java.util'),
    ('BufferedReader','java.io'), ('InputStreamReader','java.io'), ('PrintWriter','java.io'),
    ('File','java.io'), ('FileReader','java.io'), ('FileWriter','java.io'), ('BufferedWriter','java.io'),
    ('FileInputStream','java.io'), ('FileOutputStream','java.io'), ('StringTokenizer','java.util'),
]

# 类型 → 排序优先级（越小越靠前）
_COMPLETE_TYPE_ORDER = {'keyword': 0, 'builtin': 1, 'local': 2, 'global': 3, 'word': 4}

# 明确是函数/方法的候选（补全后自动加括号）；其余为类型/常量/模块名等
_CPP_FUNCS = {
    'sort','reverse','lower_bound','upper_bound','binary_search','accumulate','unique',
    'max_element','min_element','next_permutation','prev_permutation','is_sorted',
    'nth_element','partial_sort','merge','transform','copy','fill','generate',
    'count_if','find_if','for_each','min','max','swap','memset','memcpy','strlen',
    'strcmp','strcpy','strcat','sprintf','sscanf','printf','scanf','fprintf','fscanf',
    'fopen','fclose','fread','fwrite','puts','gets','getline',
    'push_back','emplace_back','push','pop','top','front','back','begin','end',
    'rbegin','rend','size','empty','clear','resize','reserve','capacity','insert',
    'erase','find','count','at','data','assign','append','substr','compare','c_str',
    'stoi','stoll','stod','to_string','make_pair','make_tuple','tie',
    'abs','pow','sqrt','log','log2','log10','exp','floor','ceil','round','fabs','fmod',
    'rand','srand','time','clock','atoi','atol','atof','exit','abort',
    'make_unique','make_shared','distance','advance','prev','next',
    'get','contains','emplace','pop_back','push_front','pop_front',
}

_PY_FUNCS = {
    'print','len','range','input','open','sorted','reversed','enumerate','zip','map',
    'filter','sum','min','max','abs','round','pow','divmod','chr','ord','hex','oct',
    'bin','format','all','any','next','iter','hash','id','type','isinstance','issubclass',
    'repr','super','vars','dir','globals','locals','eval','exec','compile',
    'append','extend','insert','remove','pop','clear','copy','count','index','sort',
    'reverse','update','keys','values','items','get','setdefault','popitem','add',
    'discard','union','intersection','difference','join','split','strip','lstrip',
    'rstrip','replace','startswith','endswith','find','rfind','encode','decode',
    'upper','lower','title','capitalize','isdigit','isalpha','isalnum','isnumeric',
    'splitlines','zfill','ljust','rjust','center','read','readline','readlines',
    'write','writelines','flush','seek','tell','close',
    '__init__','__str__','__repr__','__eq__','__lt__','__len__','__getitem__',
    '__setitem__','__enter__','__exit__',
    'math.sqrt','math.gcd','math.lcm','math.floor','math.ceil','math.pow','math.fabs',
    'random.randint','random.shuffle','random.random','random.choice','random.sample',
    'sys.exit','time.time','time.sleep',
    'itertools.permutations','itertools.combinations','itertools.product',
    'functools.lru_cache','heapq.heappush','heapq.heappop','heapq.heapify',
    'bisect.bisect_left','bisect.bisect_right','bisect.insort',
    'json.dumps','json.loads',
    'str','int','float','list','dict','set','tuple','bool','bytes','bytearray',
    'Exception','ValueError','TypeError','KeyError','IndexError','RuntimeError',
    'ImportError','ZeroDivisionError','AttributeError','OSError','IOError',
    'ArithmeticError','StopIteration','NameError','NotImplementedError',
    'MemoryError','KeyboardInterrupt','SystemExit','GeneratorExit',
}

_JAVA_FUNCS = {
    'System.out.println','System.out.print','System.out.printf','System.err.println',
    'System.exit','System.currentTimeMillis',
    'Math.abs','Math.max','Math.min','Math.sqrt','Math.pow','Math.random',
    'Math.floor','Math.ceil','Math.round','Math.log','Math.sin','Math.cos',
    'Arrays.sort','Arrays.fill','Arrays.toString','Arrays.asList','Arrays.binarySearch',
    'Collections.sort','Collections.reverse','Collections.max','Collections.min',
    'Collections.shuffle','Collections.frequency','Collections.fill',
    'next','nextInt','nextLong','nextLine','nextDouble','nextFloat',
    'hasNext','hasNextInt','hasNextLine','close','main','public static void main',
    'equals','hashCode','toString','compareTo','length','charAt','substring',
    'indexOf','lastIndexOf','contains','startsWith','endsWith','replace','trim',
    'split','toCharArray','toLowerCase','toUpperCase','valueOf',
    'parseInt','parseLong','parseDouble','append','reverse','add','remove','get',
    'set','size','isEmpty','clear','put','keySet','values','entrySet',
    'containsKey','containsValue','poll','offer','peek','push','pop',
}

# 容器成员表：变量类型推断（v. 场景）→ 常用成员
_CPP_MEMBERS = {
    'vector': [('push_back', True), ('pop_back', True), ('emplace_back', True), ('size', True),
               ('empty', True), ('clear', True), ('begin', True), ('end', True), ('rbegin', True),
               ('rend', True), ('insert', True), ('erase', True), ('front', True), ('back', True),
               ('at', True), ('resize', True), ('reserve', True), ('capacity', True), ('data', True),
               ('swap', True), ('assign', True)],
    'deque': [('push_back', True), ('push_front', True), ('pop_back', True), ('pop_front', True),
              ('size', True), ('empty', True), ('clear', True), ('begin', True), ('end', True),
              ('front', True), ('back', True), ('at', True), ('insert', True), ('erase', True),
              ('resize', True), ('swap', True)],
    'list': [('push_back', True), ('push_front', True), ('pop_back', True), ('pop_front', True),
             ('size', True), ('empty', True), ('clear', True), ('begin', True), ('end', True),
             ('front', True), ('back', True), ('insert', True), ('erase', True), ('resize', True),
             ('sort', True), ('reverse', True), ('swap', True)],
    'map': [('insert', True), ('erase', True), ('find', True), ('count', True), ('size', True),
            ('empty', True), ('clear', True), ('begin', True), ('end', True), ('at', True),
            ('contains', True), ('emplace', True), ('swap', True)],
    'unordered_map': [('insert', True), ('erase', True), ('find', True), ('count', True),
                      ('size', True), ('empty', True), ('clear', True), ('begin', True),
                      ('end', True), ('at', True), ('contains', True), ('emplace', True),
                      ('swap', True)],
    'set': [('insert', True), ('erase', True), ('find', True), ('count', True), ('size', True),
            ('empty', True), ('clear', True), ('begin', True), ('end', True), ('contains', True),
            ('emplace', True), ('swap', True)],
    'unordered_set': [('insert', True), ('erase', True), ('find', True), ('count', True),
                      ('size', True), ('empty', True), ('clear', True), ('begin', True),
                      ('end', True), ('contains', True), ('emplace', True), ('swap', True)],
    'queue': [('push', True), ('pop', True), ('front', True), ('back', True), ('size', True),
              ('empty', True), ('emplace', True), ('swap', True)],
    'stack': [('push', True), ('pop', True), ('top', True), ('size', True), ('empty', True),
              ('emplace', True), ('swap', True)],
    'priority_queue': [('push', True), ('pop', True), ('top', True), ('size', True),
                       ('empty', True), ('emplace', True), ('swap', True)],
    'string': [('size', True), ('length', True), ('empty', True), ('clear', True),
               ('begin', True), ('end', True), ('push_back', True), ('pop_back', True),
               ('insert', True), ('erase', True), ('find', True), ('rfind', True),
               ('substr', True), ('compare', True), ('c_str', True), ('data', True),
               ('append', True), ('replace', True), ('at', True), ('front', True),
               ('back', True), ('resize', True), ('reserve', True), ('stoi', True)],
    'pair': [('first', False), ('second', False), ('swap', True)],
}

_PY_MEMBERS = {
    'list': [('append', True), ('extend', True), ('insert', True), ('remove', True),
             ('pop', True), ('clear', True), ('copy', True), ('count', True),
             ('index', True), ('sort', True), ('reverse', True)],
    'dict': [('get', True), ('keys', True), ('values', True), ('items', True),
             ('update', True), ('setdefault', True), ('pop', True), ('popitem', True),
             ('clear', True), ('copy', True), ('setdefault', True)],
    'set': [('add', True), ('remove', True), ('discard', True), ('pop', True),
            ('clear', True), ('copy', True), ('union', True), ('intersection', True),
            ('difference', True), ('issubset', True), ('issuperset', True)],
    'str': [('upper', True), ('lower', True), ('strip', True), ('lstrip', True),
            ('rstrip', True), ('split', True), ('join', True), ('replace', True),
            ('find', True), ('rfind', True), ('startswith', True), ('endswith', True),
            ('isdigit', True), ('isalpha', True), ('isalnum', True), ('isnumeric', True),
            ('format', True), ('encode', True), ('decode', True), ('title', True),
            ('capitalize', True), ('count', True), ('index', True), ('zfill', True),
            ('splitlines', True), ('ljust', True), ('rjust', True), ('center', True),
            ('removeprefix', True), ('removesuffix', True)],
    'tuple': [('count', True), ('index', True)],
    'int': [('bit_length', True), ('to_bytes', True), ('from_bytes', True)],
    'float': [('is_integer', True), ('as_integer_ratio', True), ('hex', True)],
}

_JAVA_MEMBERS = {
    'String': [('length', True), ('charAt', True), ('substring', True), ('indexOf', True),
               ('lastIndexOf', True), ('contains', True), ('startsWith', True),
               ('endsWith', True), ('replace', True), ('trim', True), ('split', True),
               ('toCharArray', True), ('toLowerCase', True), ('toUpperCase', True),
               ('equals', True), ('compareTo', True), ('isEmpty', True), ('valueOf', True),
               ('toString', True)],
    'StringBuilder': [('append', True), ('reverse', True), ('toString', True),
                      ('length', True), ('charAt', True), ('deleteCharAt', True),
                      ('insert', True), ('replace', True), ('substring', True)],
    'ArrayList': [('add', True), ('remove', True), ('get', True), ('set', True),
                  ('size', True), ('isEmpty', True), ('contains', True), ('clear', True),
                  ('indexOf', True), ('lastIndexOf', True), ('toArray', True)],
    'LinkedList': [('add', True), ('addFirst', True), ('addLast', True), ('remove', True),
                   ('removeFirst', True), ('removeLast', True), ('get', True),
                   ('getFirst', True), ('getLast', True), ('size', True), ('isEmpty', True),
                   ('contains', True), ('clear', True), ('peek', True), ('poll', True)],
    'HashMap': [('put', True), ('get', True), ('keySet', True), ('values', True),
                ('entrySet', True), ('containsKey', True), ('containsValue', True),
                ('remove', True), ('size', True), ('isEmpty', True), ('clear', True)],
    'TreeMap': [('put', True), ('get', True), ('keySet', True), ('values', True),
                ('entrySet', True), ('containsKey', True), ('containsValue', True),
                ('remove', True), ('size', True), ('isEmpty', True), ('clear', True),
                ('firstKey', True), ('lastKey', True)],
    'HashSet': [('add', True), ('remove', True), ('contains', True), ('size', True),
                ('isEmpty', True), ('clear', True)],
    'TreeSet': [('add', True), ('remove', True), ('contains', True), ('size', True),
                ('isEmpty', True), ('clear', True), ('first', True), ('last', True)],
    'PriorityQueue': [('add', True), ('offer', True), ('poll', True), ('peek', True),
                      ('size', True), ('isEmpty', True), ('clear', True), ('remove', True)],
    'ArrayDeque': [('addFirst', True), ('addLast', True), ('removeFirst', True),
                   ('removeLast', True), ('getFirst', True), ('getLast', True),
                   ('offerFirst', True), ('offerLast', True), ('pollFirst', True),
                   ('pollLast', True), ('peekFirst', True), ('peekLast', True),
                   ('push', True), ('pop', True), ('size', True), ('isEmpty', True)],
    'Queue': [('add', True), ('offer', True), ('poll', True), ('peek', True),
              ('size', True), ('isEmpty', True), ('clear', True)],
    'Stack': [('push', True), ('pop', True), ('peek', True), ('size', True),
              ('isEmpty', True), ('clear', True)],
}


def _lang_of(path):
    """按扩展名识别语言；返回 'cpp' / 'c' / 'python' / 'java' / None"""
    l = (path or '').lower()
    if l.endswith(('.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx')):
        return 'cpp'
    if l.endswith('.c'):
        return 'c'
    if l.endswith('.py'):
        return 'python'
    if l.endswith('.java'):
        return 'java'
    return None


def _collect_identifiers(text, limit=200000):
    """提取文本中的标识符及词频（限制读取长度防大文件拖垮性能）"""
    if not text:
        return {}
    if len(text) > limit:
        text = text[:limit]
    words = {}
    try:
        for m in _IDENT_RE.finditer(text):
            w = m.group(0)
            if len(w) < 2:
                continue
            words[w] = words.get(w, 0) + 1
    except Exception:
        pass
    return words


_WS_INDEX_CACHE = {'ts': 0, 'words': {}}


def _workspace_identifiers(max_files=120):
    """统计 workspace 内所有源码文件的标识符（10 秒缓存），供跨文件补全"""
    now = time.time()
    if now - _WS_INDEX_CACHE.get('ts', 0) < 10:
        return _WS_INDEX_CACHE['words']
    words = {}
    try:
        n = 0
        for p in WORKSPACE_DIR.rglob('*'):
            if n >= max_files:
                break
            if not p.is_file() or p.suffix.lower() not in ('.cpp', '.c', '.h', '.hpp', '.py', '.java'):
                continue
            rel = str(p.relative_to(WORKSPACE_DIR)).replace('\\', '/')
            if rel.startswith('.') or '/.' in rel or rel.startswith('.build'):
                continue
            try:
                if p.stat().st_size > 300000:
                    continue
                words.update(_collect_identifiers(p.read_text(encoding='utf-8', errors='ignore'), 60000))
                n += 1
            except Exception:
                continue
    except Exception:
        pass
    _WS_INDEX_CACHE['words'] = words
    _WS_INDEX_CACHE['ts'] = now
    return words


def _base_candidates(lang, qual):
    """语言关键字 + 内置/库函数候选"""
    out = []
    def push(w, typ, detail, fn):
        out.append({'label': w, 'type': typ, 'detail': detail, 'fn': fn, 'seq': len(out)})
    if lang in ('cpp', 'c'):
        if qual == 'std::':
            for w in CPP_STD_NAMES:
                push(w, 'builtin', 'std::' + w, w in _CPP_FUNCS)
        else:
            for w in CPP_KEYWORDS:
                push(w, 'keyword', '关键字', False)
            for w, d in CPP_BUILTINS:
                push(w, 'builtin', d, w in _CPP_FUNCS)
    elif lang == 'python':
        for w in PY_KEYWORDS:
            push(w, 'keyword', '关键字', False)
        for w, d in PY_BUILTINS:
            push(w, 'builtin', d, w in _PY_FUNCS)
    elif lang == 'java':
        for w in JAVA_KEYWORDS:
            push(w, 'keyword', '关键字', False)
        for w, d in JAVA_BUILTINS:
            push(w, 'builtin', d, w in _JAVA_FUNCS)
    return out


# 各语言变量声明的类型提取正则：类型名 -> 变量名
_VAR_TYPE_PATTERNS = {
    'cpp': [
        re.compile(r'\b(vector|map|unordered_map|set|unordered_set|queue|stack|deque|priority_queue|string|list|pair|tuple)\s*<[^>]*>\s+(\w+)\s*[;=(]'),
        re.compile(r'\b(vector|map|unordered_map|set|unordered_set|queue|stack|deque|priority_queue|string|list|pair|tuple)\s+(\w+)\s*[;=(]'),
    ],
    'java': [
        re.compile(r'\b(StringBuilder|StringBuffer|ArrayList|LinkedList|HashMap|TreeMap|LinkedHashMap|HashSet|TreeSet|PriorityQueue|ArrayDeque|Stack|Queue|Deque)\s*<[^>]*>?\s+(\w+)\s*[=;]'),
        re.compile(r'\b(String)\s+(\w+)\s*[=;]'),
    ],
}

_MEMBER_TABLES = {'cpp': _CPP_MEMBERS, 'python': _PY_MEMBERS, 'java': _JAVA_MEMBERS}

# Python 声明推断正则（变量名, 类型, 匹配文本）
_PY_DECL_PATTERNS = [
    re.compile(r'(\w+)\s*:\s*(list|dict|set|str|tuple|int|float|bool)\b'),          # 类型注解
    re.compile(r'(\w+)\s*=\s*\[\s*\]'),                                             # = []
    re.compile(r'(\w+)\s*=\s*\{\s*\}'),                                             # = {}
    re.compile(r'(\w+)\s*=\s*(list|dict|set|str|tuple)\s*\('),                      # 构造函数
    re.compile(r'(\w+)\s*=\s*["\']'),                                               # 字符串字面量
]


def _collect_user_types(content):
    """扫描用户自定义 struct/class/union 类型：{类型名: {members:{vars,funcs}, vars:{变量名:类型名}}}"""
    types = {}
    if not content:
        return types
    for m in re.finditer(r'\b(struct|class|union)\s+(\w+)\s*\{', content):
        name = m.group(2)
        if name in types:
            continue
        depth = 0
        start = m.end() - 1           # '{'
        end = None
        for i in range(start, len(content)):
            c = content[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        block = content[start + 1:end]
        members = {'vars': set(), 'funcs': set()}
        for line in block.split('\n'):
            ls = line.strip()
            if not ls or ls.startswith('//') or ls.startswith('#'):
                continue
            if '(' in ls:
                fm = re.search(r'(\w+)\s*\(', ls)
                if fm:
                    members['funcs'].add(fm.group(1))
                continue
            # 成员变量：类型 名1,名2,...;（支持数组、初始化）
            vm = re.match(r'((?:const\s+)?[\w:<>,*& ]+?)\s+(.+?)\s*;', ls)
            if vm:
                for part in vm.group(2).split(','):
                    pm = re.match(r'\s*(\w+)\s*(?:\[[^\]]*\])?', part)
                    if pm:
                        members['vars'].add(pm.group(1))
        vars_decl = {}
        # 紧跟 '}' 的直接变量声明（支持逗号分隔多变量/数组/指针）：
        # struct seg{...}sg[N<<2],b[N][N];
        dm = re.match(r'\}\s*([^;]+?)\s*;', content[end:end + 120])
        if dm:
            for part in dm.group(1).split(','):
                pm = re.match(r'\s*[*&]?\s*(\w+)\s*(?:\[[^\]]*\])?', part)
                if pm:
                    vars_decl[pm.group(1)] = name
        types[name] = {'members': members, 'vars': vars_decl}
    # 常规变量声明（支持逗号分隔多变量/数组/指针）：seg a, b[10];  seg *p;
    # 注意排除 '{'：避免 'struct seg {' 的定义体被误当作变量声明
    for name, info in types.items():
        pat = re.compile(r'\b' + re.escape(name) + r'\s+([^{};=()]+?)\s*[;=]')
        for dm in pat.finditer(content):
            for part in dm.group(1).split(','):
                pm = re.match(r'\s*[*&]?\s*(\w+)\s*(?:\[[^\]]*\])?', part)
                if pm:
                    info['vars'][pm.group(1)] = name
    return types


def _member_var_from_text(text):
    """从光标前文本提取成员访问的根变量（支持 a[i] / a[i<<1|1] / a[0][1] / p-> / arr[i]->；
    下标内可以是任意表达式）。返回 (变量名, 是否有下标)"""
    if not text:
        return None, False
    idx = -1
    for op in ('.', '->', '::'):
        i = text.rfind(op)
        if i > idx:
            idx = i
    if idx < 0:
        return None, False
    i = idx - 1
    while i >= 0 and text[i] in ' \t':
        i -= 1
    if i < 0:
        return None, False
    if text[i] == ']':
        # 数组下标链：向前配对 '['，支持 a[i][j] 多级，取根变量名
        depth = 0
        j = i
        while j >= 0:
            c = text[j]
            if c == ']':
                depth += 1
            elif c == '[':
                depth -= 1
                if depth == 0:
                    k = j - 1
                    while k >= 0 and text[k] in ' \t':
                        k -= 1
                    if k >= 0 and text[k] == ']':
                        # 链式下标：继续处理外层下标
                        j = k
                        depth = 0
                        continue
                    while k >= 0 and (text[k].isalnum() or text[k] == '_'):
                        k -= 1
                    name = text[k + 1:j]
                    if re.match(r'^[A-Za-z_]\w*$', name):
                        return name, True
                    return None, False
            j -= 1
        return None, False
    if text[i] == ')':
        return None, False      # 函数返回值成员访问 f().x 暂不支持
    if text[i].isalnum() or text[i] == '_':
        j = i
        while j >= 0 and (text[j].isalnum() or text[j] == '_'):
            j -= 1
        name = text[j + 1:i + 1]
        if re.match(r'^[A-Za-z_]\w*$', name):
            return name, False
    return None, False


def _infer_members(lang, var, content, has_index=False, index_types=None):
    """变量类型推断：从当前文件中找 var 的声明，返回该类型的常用成员候选（找不到返回 None）。
    index_types 为编译后符号索引的类型表（优先使用，比实时扫描更全）"""
    if not var or not content:
        return None
    # 1) 用户自定义 struct/class 类型（编译索引优先，实时扫描补充新编辑的变量）
    if lang == 'cpp':
        user_types = {}
        if index_types is not None:
            user_types.update(index_types)
            # 正文中引用索引类型的变量声明（如头文件的 Point 在正文声明 Point q;）
            for name, info in user_types.items():
                pat = re.compile(r'\b' + re.escape(name) + r'\s+([^{};=()]+?)\s*[;=]')
                for dm in pat.finditer(content):
                    for part in dm.group(1).split(','):
                        pm = re.match(r'\s*[*&]?\s*(\w+)\s*(?:\[[^\]]*\])?', part)
                        if pm and pm.group(1) not in info['vars']:
                            info['vars'][pm.group(1)] = name
        live_types = _collect_user_types(content)
        for name, info in live_types.items():
            if name in user_types:
                # 保留索引成员，补充实时扫描到的新变量声明（编译后编辑的代码）
                user_types[name]['vars'].update(info['vars'])
            else:
                user_types[name] = info
        tname = None
        for name, info in user_types.items():
            if var in info['vars']:
                tname = name
                break
        if tname is None and has_index:
            # vector<自定义类型> v → v[i].
            for name in user_types:
                m = re.search(r'vector\s*<\s*' + re.escape(name) + r'\s*>\s+' + re.escape(var) + r'\s*[;=(]', content)
                if m:
                    tname = name
                    break
        if tname:
            info = user_types[tname]
            out = []
            for v in sorted(info['members']['vars']):
                out.append({'label': v, 'type': 'builtin', 'detail': tname + ' 成员',
                            'fn': False, 'seq': len(out)})
            for f in sorted(info['members']['funcs']):
                out.append({'label': f, 'type': 'builtin', 'detail': tname + ' 方法',
                            'fn': True, 'seq': len(out)})
            return out or None
    table = _MEMBER_TABLES.get(lang)
    if not table:
        return None
    var_type = None
    if lang == 'python':
        for line in content.split('\n'):
            ls = line.strip()
            for pat in _PY_DECL_PATTERNS:
                m = pat.match(ls)
                if not m or m.group(1) != var:
                    continue
                if pat.pattern.startswith(r'(\w+)\s*:'):
                    var_type = m.group(2)
                elif pat.pattern.startswith(r'(\w+)\s*=\s*\[\s*\]'):
                    var_type = 'list'
                elif pat.pattern.startswith(r'(\w+)\s*=\s*\{\s*\}'):
                    var_type = 'dict'
                elif pat.pattern.startswith(r'(\w+)\s*=\s*(list|dict|set|str|tuple)\s*\('):
                    var_type = m.group(2)
                else:
                    var_type = 'str'
                break
            if var_type:
                break
    else:
        for pat in _VAR_TYPE_PATTERNS.get(lang, []):
            for m in pat.finditer(content):
                if m.group(2) == var:
                    var_type = m.group(1)
                    break
            if var_type:
                break
    if not var_type:
        return None
    members = table.get(var_type)
    if not members:
        return None
    return [{'label': name, 'type': 'builtin', 'detail': var_type + ' 成员',
             'fn': is_fn, 'seq': i} for i, (name, is_fn) in enumerate(members)]


def _find_namespace_block(content, path_parts, start=0):
    """在 content 中按路径逐层定位 namespace 块（支持嵌套 namespace xx{ namespace yy{...} }），
    返回 (块起始 '{' 索引, 块结束 '}' 索引) 或 None"""
    pos = start
    for idx, part in enumerate(path_parts):
        pat = re.compile(r'\bnamespace\s+' + re.escape(part) + r'\s*\{')
        m = pat.search(content or '', pos)
        if not m:
            return None
        start_brace = m.end() - 1
        depth = 0
        end = None
        for i in range(start_brace, len(content or '')):
            c = content[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            return None
        if idx == len(path_parts) - 1:
            return start_brace, end
        pos = start_brace + 1   # 下一层在块内继续查找
    return None


def _namespace_symbols(namespace, content):
    """提取 namespace 块内符号：支持嵌套（namespace xx{ namespace yy{...} }）与
    C++17 内联（namespace xx::yy{...}）。找不到返回 (None, None)"""
    parts = [p for p in (namespace or '').split('::') if p]
    if not parts:
        return None, None
    r = None
    # C++17 内联语法：namespace xx::yy {
    pat_inline = re.compile(r'\bnamespace\s+' + re.escape(namespace) + r'\s*\{')
    m = pat_inline.search(content or '')
    if m:
        start = m.end() - 1
        depth = 0
        for i in range(start, len(content or '')):
            c = content[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    r = (start, i)
                    break
    else:
        r = _find_namespace_block(content, parts)
    if not r:
        return None, None
    start, end = r
    block = content[start + 1:end]
    words = _collect_identifiers(block)
    funcs = set(re.findall(r'(\w+)\s*\(', block))
    return words, funcs


def _filter_candidates(cands, key, limit=60):
    """按前缀过滤 + 排序：完全匹配 > 前缀匹配 > 子串匹配；类型优先级 + 词频"""
    kl = key.lower()
    pref, sub = [], []
    for c in cands:
        label = c['label']
        ll = label.lower()
        if kl and not ll.startswith(kl):
            # 子串匹配仅限 ≥2 字符，避免单字符前缀噪音过大
            if len(kl) >= 2 and kl in ll:
                sub.append(c)
            continue
        pref.append(c)
    def sort_key(c):
        l = c['label'].lower()
        exact = 0 if (kl and l == kl) else 1
        freq = c.get('freq', 0)
        order = _COMPLETE_TYPE_ORDER.get(c['type'], 9)
        if not kl:
            # 空前缀（Ctrl+Space）：关键字不霸榜，内置按常用度（定义顺序），标识符按词频
            if c['type'] == 'keyword':
                order = 10
            return (order, -freq, c.get('seq', 0), l)
        return (exact, order, -freq, l)
    pref.sort(key=sort_key)
    sub.sort(key=sort_key)
    return (pref + sub)[:limit]


def build_completion(path, content, line, ch):
    """生成补全候选列表（离线，不依赖任何网络服务）。
    优先使用编译后符号索引（若有），未编译文件回退实时扫描"""
    lang = _lang_of(path)
    index_types = None
    index_idents = None
    if lang == 'cpp':
        index_types = _indexed_types(path)
        index_idents = _indexed_idents(path)
    if line >= len(content.split('\n')):
        line = len(content.split('\n')) - 1
    lines = content.split('\n')
    cur = lines[line][:max(ch, 0)]
    m = re.search(r'[A-Za-z0-9_.:>\-\[\]<>]*$', cur)
    prefix = m.group(0) if m else ''
    # 拆分限定符（obj. / obj-> / ns::）与标识符前缀
    qual = ''
    key = prefix
    mm = re.match(r'^(.*(?:\.|::|->))([A-Za-z_][A-Za-z0-9_]*)?$', prefix)
    if mm and mm.group(1):
        qual = mm.group(1)
        key = mm.group(2) or ''
    if not re.match(r'[A-Za-z_][A-Za-z0-9_]*$', key):
        key = ''
    cands = []
    member_access = False
    if qual.endswith(('.', '->')):
        # 对象/类成员访问（x. / x-> / arr[i]. / p[0]-> / sg[u<<1|1].）
        member_access = True
        # 结构化解析：从光标前全文提取根变量（下标内任意表达式都支持）
        pos = sum(len(l) + 1 for l in lines[:line]) + ch
        var, has_index = _member_var_from_text(content[:pos])
        if var:
            inferred = _infer_members(lang, var, content, has_index, index_types) if lang else None
            if inferred:
                cands = inferred
    elif qual.endswith('::'):
        # 作用域限定：std:: → STL；其他 → namespace 内部符号（编译索引优先）
        ns = qual[:-2]
        if ns == 'std':
            cands = _base_candidates(lang, 'std::')
        else:
            ns_info = _indexed_ns(path, ns)
            ns_words = ns_info['words'] if ns_info else None
            ns_funcs = ns_info['funcs'] if ns_info else None
            if ns_words is None:
                ns_words, ns_funcs = _namespace_symbols(ns, content)
            if ns_words:
                cands = [{'label': w, 'type': 'builtin', 'detail': 'namespace ' + ns,
                          'fn': w in ns_funcs, 'seq': i, 'freq': n}
                         for i, (w, n) in enumerate(sorted(ns_words.items(), key=lambda kv: -kv[1]))]
            # 找不到该 namespace：不补内置库（避免无关候选刷屏），只保留当前文件/工作区标识符
    # 非成员访问才补内置库；成员访问推断失败时不给内置库（避免无关候选刷屏），只保留当前文件/工作区标识符
    if not cands and not member_access:
        cands = _base_candidates(lang, qual)
    seen = set(c['label'] for c in cands)
    # 当前文件标识符
    for w, n in sorted(_collect_identifiers(content).items(), key=lambda kv: (-kv[1], kv[0].lower())):
        if w in seen:
            continue
        seen.add(w)
        cands.append({'label': w, 'type': 'local', 'detail': '当前文件', 'freq': n})
    # workspace 其他源码文件标识符（跨文件补全）
    ws_words = _workspace_identifiers()
    for w, n in sorted(ws_words.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        if w in seen:
            continue
        seen.add(w)
        cands.append({'label': w, 'type': 'global', 'detail': '工作区', 'freq': n})
    return _filter_candidates(cands, key)


# ---------- 样例发现 ----------
def find_out_file(d: Path, base):
    for suffix in ['.out', '.ans', '.a', '.txt']:
        c = d / (base + suffix)
        if c.exists():
            return c
    return None


def write_template_file(p: Path, rel: str):
    """新建文件：cpp/py/java 用模板填充（workspace/.default.{ext} 优先），其余为空文件"""
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    name = p.stem
    if ext == '.cpp' or ext == '.c':
        builtin_name, ws_name = 'default.cpp', ('.default.cpp' if ext == '.cpp' else '.default.c')
    elif ext == '.py':
        builtin_name, ws_name = 'default.py', '.default.py'
    elif ext == '.java':
        builtin_name, ws_name = 'default.java', '.default.java'
    else:
        p.write_text('', encoding='utf-8')
        return
    if rel == ws_name:
        builtin = TEMPLATE_DIR / builtin_name
        p.write_text(builtin.read_text(encoding='utf-8') if builtin.exists() else '', encoding='utf-8')
    else:
        template = WORKSPACE_DIR / ws_name
        if not template.exists():
            template = TEMPLATE_DIR / builtin_name
        if template.exists():
            p.write_text(template.read_text(encoding='utf-8').replace('{NAME}', name), encoding='utf-8')
        else:
            p.write_text('', encoding='utf-8')


def find_samples(src_path: Path):
    """查找样例：源目录 + 隐藏目录 .samples/（大样例导入处），支持编号缺口"""
    d = src_path.parent
    name = src_path.stem
    samples = []
    out = find_out_file(d, name)
    if (d / (name + '.in')).exists() and out:
        samples.append({"label": "样例", "in": name + '.in', "out": out.name, "subdir": ""})
    used = set()
    for sub in ('', '.samples'):
        base_dir = d if sub == '' else d / '.samples'
        if not base_dir.is_dir():
            continue
        numbered = []
        for in_file in base_dir.glob(name + '*.in'):
            num = in_file.stem[len(name):]
            if not num or not num.isdigit():
                continue
            out = find_out_file(base_dir, in_file.stem)
            if out:
                numbered.append((int(num), in_file.name, out.name))
        numbered.sort()
        for num, in_name, out_name in numbered:
            if num in used:
                continue  # 编号冲突时以源目录为准
            used.add(num)
            samples.append({"label": "样例%d" % num, "in": in_name, "out": out_name, "subdir": sub})
    return samples


# ---------- 编译后符号索引 ----------
# 编译成功时生成该文件的符号索引（准确、可复用）；补全优先用索引，未编译文件回退实时扫描
SYMBOL_INDEX = {}
SYMBOL_INDEX_LOCK = threading.Lock()


def _scan_headers_for_symbols(src_path):
    """收集源码 include 的同目录/工作区头文件符号（合并进索引）"""
    extra_texts = []
    try:
        text = read_text_auto(src_path)
    except Exception:
        return extra_texts
    # 提取 #include "xxx.h"（本地引号形式；<...> 系统头不扫描）
    for m in re.finditer(r'#\s*include\s*"([^"]+)"', text):
        try:
            p = safe_path(m.group(1))
            if p.is_file() and p.suffix.lower() in ('.h', '.hpp', '.hh'):
                extra_texts.append(read_text_auto(p))
        except ValueError:
            pass
    return extra_texts


def build_symbol_index(rel_path):
    """为已编译文件生成符号索引：{types, ns, idents}。在后台线程调用"""
    try:
        p = safe_path(rel_path)
        if not p.is_file() or p.suffix.lower() not in ('.cpp', '.c', '.h', '.hpp', '.cc', '.cxx'):
            return
        texts = [read_text_auto(p)] + _scan_headers_for_symbols(p)
        content = '\n'.join(texts)
        types = {}
        ns = {}
        for tname, info in _collect_user_types(content).items():
            # 合并重复类型（头文件 + 源文件）：成员取并集
            if tname in types:
                types[tname]['members']['vars'] |= info['members']['vars']
                types[tname]['members']['funcs'] |= info['members']['funcs']
                types[tname]['vars'].update(info['vars'])
            else:
                types[tname] = info
        # 扁平化所有 namespace 路径的符号（按括号包含关系构建嵌套路径）
        decls = []   # (name, brace_start, brace_end)
        for m in re.finditer(r'\bnamespace\s+([A-Za-z_]\w*)\s*\{', content):
            start = m.end() - 1
            depth = 0
            end = None
            for i in range(start, len(content)):
                c = content[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end is not None:
                decls.append((m.group(1), start, end))
        decls.sort(key=lambda d: d[1])
        for i, (name, start, end) in enumerate(decls):
            # 找父级：start 之前最近的、end 大于 start 的 decl
            parent = None
            for j in range(i - 1, -1, -1):
                if decls[j][2] > end:   # 父块包含当前块
                    parent = decls[j]
                    break
            full = ((parent[0] + '::') if parent else '') + name
            block = content[start + 1:end]
            words = _collect_identifiers(block)
            funcs = set(re.findall(r'(\w+)\s*\(', block))
            ns[full] = {'words': words, 'funcs': funcs}
        idents = _collect_identifiers(content)
        idx = {'types': types, 'ns': ns, 'idents': idents}
        with SYMBOL_INDEX_LOCK:
            SYMBOL_INDEX[rel_path] = idx
    except Exception:
        pass


def _indexed_types(rel_path):
    with SYMBOL_INDEX_LOCK:
        idx = SYMBOL_INDEX.get(rel_path)
    return idx['types'] if idx else None


def _indexed_ns(rel_path, nspath):
    with SYMBOL_INDEX_LOCK:
        idx = SYMBOL_INDEX.get(rel_path)
    if not idx:
        return None
    return idx['ns'].get(nspath)


def _indexed_idents(rel_path):
    with SYMBOL_INDEX_LOCK:
        idx = SYMBOL_INDEX.get(rel_path)
    return idx['idents'] if idx else None


def _trigger_symbol_index(src_path):
    """编译成功后在后台生成符号索引（补全优先使用，比实时扫描更全）"""
    try:
        rel = str(src_path.relative_to(WORKSPACE_DIR)).replace('\\', '/')
        threading.Thread(target=build_symbol_index, args=(rel,), daemon=True).start()
    except Exception:
        pass


# ---------- 编译（多语言） ----------
LANG_EXTS = {'.cpp': 'cpp', '.c': 'cpp', '.h': 'header', '.hpp': 'header',
             '.py': 'python', '.java': 'java'}


def read_text_auto(path: Path):
    """读取文本文件：优先 UTF-8，失败回退 GBK（兼容 DevC++ 旧工程）"""
    data = path.read_bytes()
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('gbk', errors='replace')
    except Exception:
        return data.decode('utf-8', errors='replace')


def get_tool(name, default):
    """读取 .ide.json 中全局工具命令（如 javac/java 路径）"""
    c = (load_config().get('compile') or {})
    return (c.get(name) or '').strip() or default


def compile_source(src_path: Path, extra_include_dirs=None):
    """编译源文件，返回 (ok, spec, log)。
    spec = {'kind': 'cpp'|'python'|'java', ...}，编译失败返回 (False, None, log)"""
    ext = src_path.suffix.lower()
    if ext in ('.h', '.hpp'):
        return False, None, '头文件不支持直接编译运行（请编译对应的 .c / .cpp）'
    if ext == '.py':
        # Python：仅做语法检查（compile 内置函数，不产生文件；UTF-8 优先 GBK 回退）
        python_exe = get_tool('python', sys.executable)
        checker = ("import sys;data=open(sys.argv[1],'rb').read();src=None\n"
                   "for enc in ('utf-8','gbk'):\n"
                   " try: src=data.decode(enc);break\n"
                   " except Exception: pass\n"
                   "if src is None: src=data.decode('utf-8','replace')\n"
                   "compile(src,sys.argv[1],'exec')")
        try:
            result = subprocess.run([python_exe, '-c', checker, str(src_path)],
                                    capture_output=True, timeout=30)
            log = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            if result.returncode != 0:
                return False, None, log
            _trigger_symbol_index(src_path)
            return True, {'kind': 'python', 'script': str(src_path)}, log
        except subprocess.TimeoutExpired:
            return False, None, '语法检查超时'
        except FileNotFoundError:
            return False, None, '未找到 Python 解释器（%s），请在右侧编译设置中配置' % python_exe
        except Exception as e:
            return False, None, str(e)

    if ext == '.java':
        build_dir = BUILD_DIR / src_path.stem
        build_dir.mkdir(parents=True, exist_ok=True)
        javac = get_tool('javac', 'javac')
        java = get_tool('java', 'java')
        try:
            result = subprocess.run([javac, '-encoding', 'utf-8', '-d', str(build_dir), str(src_path)],
                                    capture_output=True, timeout=60)
            log = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            if result.returncode != 0:
                return False, None, log
            _trigger_symbol_index(src_path)
            return True, {'kind': 'java', 'build_dir': str(build_dir), 'main': src_path.stem, 'java': java}, log
        except subprocess.TimeoutExpired:
            return False, None, '编译超时'
        except FileNotFoundError:
            return False, None, '未找到 javac，请安装 JDK 或在上方配置路径'
        except Exception as e:
            return False, None, str(e)

    # C++：编译到 BUILD_DIR/{name}/main.exe，先编译到临时文件再原子替换，避免并发竞态
    build_dir = BUILD_DIR / src_path.stem
    build_dir.mkdir(parents=True, exist_ok=True)
    exe_path = build_dir / "main.exe"
    tmp_exe = build_dir / ("main_%d_%d.exe" % (os.getpid(), int(time.time() * 1000)))
    compiler, flags = get_compile_config()
    cmd = [compiler, str(src_path), "-o", str(tmp_exe)] + flags
    if extra_include_dirs:
        for inc in extra_include_dirs:
            cmd.append("-I" + str(inc))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        log = (result.stdout + result.stderr).decode('utf-8', errors='replace')
        if result.returncode != 0:
            try:
                tmp_exe.unlink()
            except OSError:
                pass
            return False, None, log
        try:
            os.replace(tmp_exe, exe_path)
        except OSError:
            exe_path = tmp_exe  # main.exe 正被运行占用时退回使用临时文件
        for stale in build_dir.glob("main_*.exe"):
            if stale != exe_path:
                try:
                    stale.unlink()
                except OSError:
                    pass
        _trigger_symbol_index(src_path)
        return True, {'kind': 'cpp', 'exe': str(exe_path)}, log
    except subprocess.TimeoutExpired:
        return False, None, "编译超时"
    except FileNotFoundError:
        return False, None, '未找到 %s，请检查编译器配置' % compiler
    except Exception as e:
        return False, None, str(e)


def spec_cmd(spec, work_dir=None):
    """根据 spec 生成运行命令 (cmd, cwd)。
    work_dir=None 为 stdio 模式；否则为文件模式（先复制运行产物到 work_dir）"""
    kind = spec['kind']
    if kind == 'cpp':
        if work_dir is None:
            return [spec['exe']], str(Path(spec['exe']).parent)
        exe = work_dir / 'main.exe'
        if not exe.exists():
            shutil.copy(spec['exe'], exe)
        return [str(exe)], str(work_dir)
    if kind == 'python':
        return [get_tool('python', sys.executable), spec['script']], str(work_dir if work_dir is not None else Path(spec['script']).parent)
    if kind == 'java':
        if work_dir is None:
            return [spec['java'], '-cp', spec['build_dir'], spec['main']], spec['build_dir']
        for p in Path(spec['build_dir']).rglob('*'):
            if p.is_file():
                rel = p.relative_to(spec['build_dir'])
                dst = work_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(p, dst)
        return [spec['java'], '-cp', str(work_dir), spec['main']], str(work_dir)
    raise ValueError('未知语言: %s' % kind)


# ---------- SPJ（Special Judge） ----------
def find_checker(src_path: Path):
    """查找题目对应的 SPJ 源文件：{stem}_checker.cpp 或 {stem}_spj.cpp"""
    d = src_path.parent
    for name in (src_path.stem + '_checker.cpp', src_path.stem + '_spj.cpp'):
        c = d / name
        if c.exists():
            return c
    return None


def resolve_checker(src_path: Path, problem_name=None):
    """解析 SPJ 源文件：题目配置指定路径优先，其次自动检测"""
    cfg = get_problem_config(problem_name or src_path.stem)
    spj_rel = (cfg.get('spj') or '').strip()
    if spj_rel:
        try:
            p = safe_path(spj_rel)
            if p.is_file():
                return p
        except ValueError:
            pass
    return find_checker(src_path)


def compile_checker(checker_src: Path):
    """编译 SPJ（include 路径含 checker 所在目录、工作目录和内置 testlib）
    checker 自带 testlib.h 时优先用它，否则回退到系统内置的新版 testlib"""
    return compile_source(checker_src, extra_include_dirs=[checker_src.parent, TEMPLATE_DIR, WORKSPACE_DIR])


OUTPUT_LIMIT = 64 * 1024 * 1024  # 输出上限 64MB，超过判 OLE

# Windows 常见崩溃退出码 → 可读描述
WIN_ERR = {
    0xC0000005: '访问冲突（Access Violation）',
    0xC00000FD: '栈溢出（Stack Overflow）',
    0xC0000094: '整数除零（Integer Division by Zero）',
    0xC0000374: '堆损坏（Heap Corruption）',
}


def exit_code_info(code):
    """返回 (退出码文本, 可读描述)"""
    if code is None:
        return None, ''
    u = code & 0xFFFFFFFF
    if code < 0 or u > 0x7FFFFFFF:
        # Windows NTSTATUS 异常码
        return '0x%08X' % u, WIN_ERR.get(u, '')
    return str(code), ''


def wall_limit_ms(time_limit_ms):
    """墙钟时间兜底：阻塞 IO / sleep 类程序也能被 TLE"""
    return max(time_limit_ms * 3, 5000)


def _capped_reader(pipe, chunks, cap, overflow_flag=None):
    """增量读取管道到块列表（cap 上限），超限立即置 overflow_flag 并继续排空管道"""
    total = 0
    try:
        while True:
            data = pipe.read1(65536)
            if data:
                if total + len(data) > cap:
                    if total < cap:
                        chunks.append(data[:cap - total])
                        total = cap
                    if overflow_flag is not None:
                        overflow_flag[0] = True
                else:
                    chunks.append(data)
                    total += len(data)
            else:
                try:
                    rest = pipe.read()
                    if rest:
                        if total + len(rest) > cap:
                            if total < cap:
                                chunks.append(rest[:cap - total])
                                total = cap
                            if overflow_flag is not None:
                                overflow_flag[0] = True
                        else:
                            chunks.append(rest)
                            total += len(rest)
                except Exception:
                    pass
                break
    except Exception:
        pass


def run_checker(spec, input_bytes, output_bytes, answer_bytes,
                time_limit_ms=5000, memory_limit_mb=2048, stop_check=None):
    """运行 SPJ：checker input.txt output.txt answer.txt，返回 (ok, log)"""
    work_dir = BUILD_DIR / ("checker_%d" % int(time.time() * 1000))
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        (work_dir / 'input.txt').write_bytes(input_bytes or b'')
        (work_dir / 'output.txt').write_bytes(output_bytes or b'')
        (work_dir / 'answer.txt').write_bytes(answer_bytes or b'')
        cmd, _cwd = spec_cmd(spec)
        cmd = cmd + ['input.txt', 'output.txt', 'answer.txt']
        with open(os.devnull, 'rb') as devnull_in:
            proc = subprocess.Popen(
                cmd,
                stdin=devnull_in, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(work_dir), shell=False,
            )
            job = winapi.create_job()
            if job:
                winapi.assign_job(job, proc.pid)
                # 沙盒：内核级硬限制（内存/进程数/IO），监控之外的第二道防线
                winapi.set_job_limits(job, memory_mb=memory_limit_mb, active_processes=8)
            job_closed = False
            baseline_free = None  # 运行中磁盘写入监控基准

            def kill_all():
                nonlocal job_closed
                if job and not job_closed:
                    winapi.close_job(job)
                    job_closed = True
                try:
                    kill_proc_tree(proc.pid)
                except Exception:
                    pass

            log_chunks = []
            threading.Thread(target=_capped_reader,
                             args=(proc.stdout, log_chunks, 4 * 1024 * 1024),
                             daemon=True).start()
            start_wall = time.time()
            peak = 0.0
            reason = None
            while proc.poll() is None:
                try:
                    if stop_check is not None and stop_check():
                        reason = 'STOP'
                        kill_all()
                        break
                    m, _ = winapi.process_memory_mb(proc.pid)
                    # 优先用 Job 枚举进程树：避免孤儿进程/PID 复用误判导致假 MLE
                    plist = winapi.job_pids(job)
                    if plist is None:
                        plist = winapi.children_pids(proc.pid)
                    for cpid in plist:
                        if cpid == proc.pid:
                            continue  # Job 列表含主进程自身，跳过
                        try:
                            cm, _ = winapi.process_memory_mb(cpid)
                            m += cm
                        except winapi.ProcessNotFound:
                            pass
                    if m > peak:
                        peak = m
                    if peak > memory_limit_mb:
                        reason = 'MLE'
                        kill_all()
                        break
                    if (time.time() - start_wall) * 1000.0 > wall_limit_ms(time_limit_ms):
                        reason = 'TLE'
                        kill_all()
                        break
                except winapi.ProcessNotFound:
                    pass
                try:
                    free_now = shutil.disk_usage(work_dir).free
                    if baseline_free is None:
                        baseline_free = free_now
                    elif baseline_free - free_now > 1024 * 1024 * 1024:
                        # 防写爆磁盘：程序吃掉 >1GB 磁盘空间 -> OLE 并击杀
                        reason = 'OLE'
                        kill_all()
                        break
                except OSError:
                    pass
                time.sleep(0.01)
            time.sleep(0.05)
            log = b''.join(log_chunks).decode('utf-8', errors='replace')
            if reason == 'TLE':
                return False, 'SPJ 超时'
            if reason == 'MLE':
                return False, 'SPJ 内存超限（>%dMB）' % memory_limit_mb
            if reason == 'STOP':
                return False, '已停止'
            ret = None
            try:
                ret = proc.poll()
            except Exception:
                ret = None
            if ret is None:
                try:
                    ret = proc.wait(timeout=3)
                except Exception:
                    ret = None
                if ret is None:
                    # 修复：3 秒仍未退出，强制清理进程树
                    kill_all()
                    try:
                        ret = proc.wait(timeout=2)
                    except Exception:
                        ret = None
            return ret == 0, log
    except Exception as e:
        return False, 'SPJ 运行异常：%s' % e
    finally:
        try:
            if job and not job_closed:
                winapi.close_job(job)
                job_closed = True
            if proc is not None and proc.poll() is None:
                kill_proc_tree(proc.pid)
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------- 运行（stdio 模式，喂 stdin 抓 stdout） ----------
def run_stdio(cmd, cwd, input_bytes, time_limit_ms, memory_limit_mb, stop_check=None):
    """运行程序，stdin 喂入 input_bytes，抓取 stdout/stderr。
    监控：CPU 时间（TLE）、内存（MLE）、输出上限（OLE）、墙钟兜底（TLE）。
    stop_check：可选的回调，返回真时立即终止（对拍停止键用）。
    返回 (status, cpu_ms, peak_mem, stdout, stderr, exit_code)"""
    peak_mem = 0.0
    cpu_ms = 0.0
    last_cpu = 0.0
    out_chunks = []
    err_chunks = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, shell=False,
        )
        job = winapi.create_job()
        if job:
            winapi.assign_job(job, proc.pid)
            # 沙盒：内核级硬限制（内存/进程数/IO），监控之外的第二道防线
            winapi.set_job_limits(job, memory_mb=memory_limit_mb, active_processes=8)
        job_closed = False
        baseline_free = None  # 运行中磁盘写入监控基准

        def kill_all():
            nonlocal job_closed
            if job and not job_closed:
                winapi.close_job(job)
                job_closed = True
            try:
                kill_proc_tree(proc.pid)
            except Exception:
                pass

        def writer():
            try:
                proc.stdin.write(input_bytes or b'')
                proc.stdin.flush()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        overflow = [False]

        def reader(pipe, chunks):
            _capped_reader(pipe, chunks, OUTPUT_LIMIT, overflow)
            if overflow[0]:
                # 溢出即刻击杀，避免高速输出程序继续轰炸
                try:
                    kill_all()
                except Exception:
                    pass

        threading.Thread(target=writer, daemon=True).start()
        threading.Thread(target=reader, args=(proc.stdout, out_chunks), daemon=True).start()
        threading.Thread(target=reader, args=(proc.stderr, err_chunks), daemon=True).start()

        start_wall = time.time()
        tick = 0
        reason = None
        while proc.poll() is None:
            try:
                if stop_check is not None and stop_check():
                    reason = 'STOP'
                    kill_all()
                    break
                pt_k, pt_u = winapi.process_cpu_times(proc.pid)
                cpu = pt_k + pt_u
                mem, mem_peak = winapi.process_memory_mb(proc.pid)
                if tick % 2 == 0:
                    # 优先用 Job 枚举进程树：不依赖 ParentPID，
                    # 避免"孤儿进程/PID 复用"被误判为子进程导致内存测量暴涨（假 MLE）
                    plist = winapi.job_pids(job)
                    if plist is None:
                        plist = winapi.children_pids(proc.pid)
                    for cpid in plist:
                        if cpid == proc.pid:
                            continue  # Job 列表含主进程自身，跳过（主进程已单独统计）
                        try:
                            ck, cu = winapi.process_cpu_times(cpid)
                            cpu += ck + cu
                            cm, _ = winapi.process_memory_mb(cpid)
                            mem += cm
                        except winapi.ProcessNotFound:
                            pass
                cpu_now = cpu * 1000.0
                if cpu_now > last_cpu:
                    cpu_ms += cpu_now - last_cpu
                last_cpu = cpu_now
                if mem > peak_mem:
                    peak_mem = mem
                if overflow[0]:
                    reason = 'OLE'
                    kill_all()
                    break
                if cpu_ms > time_limit_ms:
                    reason = 'TLE'
                    kill_all()
                    break
                if peak_mem > memory_limit_mb or mem_peak > memory_limit_mb:
                    reason = 'MLE'
                    kill_all()
                    break
                if (time.time() - start_wall) * 1000.0 > wall_limit_ms(time_limit_ms):
                    reason = 'TLE'
                    kill_all()
                    break
            except winapi.ProcessNotFound:
                pass
            if tick % 4 == 0:
                try:
                    free_now = shutil.disk_usage(cwd).free
                    if baseline_free is None:
                        baseline_free = free_now
                    elif baseline_free - free_now > 1024 * 1024 * 1024:
                        # 防写爆磁盘：程序吃掉 >1GB 磁盘空间 -> OLE 并击杀
                        reason = 'OLE'
                        kill_all()
                        break
                except OSError:
                    pass
            tick += 1
            time.sleep(0.02)

        time.sleep(0.05)
        ret = None
        try:
            ret = proc.poll()
        except Exception:
            ret = None
        if ret is None:
            try:
                ret = proc.wait(timeout=3)
            except Exception:
                ret = None
            if ret is None:
                # 修复：3 秒仍未退出，强制清理进程树，避免残留进程拖垮系统
                kill_all()
                try:
                    ret = proc.wait(timeout=2)
                except Exception:
                    ret = None
        stdout = b''.join(out_chunks)
        stderr = b''.join(err_chunks)
        if reason == 'STOP':
            return 'STOP', cpu_ms, peak_mem, stdout, stderr, ret
        if reason == 'OLE':
            return 'OLE', cpu_ms, peak_mem, stdout, stderr, ret
        if reason == 'MLE' or peak_mem > memory_limit_mb:
            return 'MLE', cpu_ms, peak_mem, stdout, stderr, ret
        if reason == 'TLE' or cpu_ms > time_limit_ms:
            return 'TLE', cpu_ms, peak_mem, stdout, stderr, ret
        if ret != 0:
            return 'RE', cpu_ms, peak_mem, stdout, stderr, ret
        return 'AC', cpu_ms, peak_mem, stdout, stderr, ret
    except Exception as e:
        return 'RE', cpu_ms, peak_mem, b'', str(e).encode('utf-8', errors='replace'), None
    finally:
        # 修复：所有路径（含正常 AC）都必须释放 Job 对象，
        # 否则对拍每轮泄漏 3 个内核 Job（KILL_ON_JOB_CLOSE），长跑会耗尽系统资源
        try:
            if job and not job_closed:
                winapi.close_job(job)
                job_closed = True
            # 异常路径兜底：进程若仍存活则清理，防孤儿进程累积
            if proc is not None and proc.poll() is None:
                kill_proc_tree(proc.pid)
        except Exception:
            pass


def run_file(cmd, cwd, work_dir: Path, name, time_limit_ms, memory_limit_mb, out_name=None, stop_check=None):
    """运行程序（freopen 文件模式），stdin/stdout 均忽略，跑完读 {out_name 或 name}.out"""
    inp = work_dir / (name + '.in')
    inp.touch(exist_ok=True)
    out_file = work_dir / ((out_name or name) + '.out')
    # 先删掉旧的输出文件：防止上一次的残留被当成本轮输出（对拍常驻目录复用时必须）
    try:
        out_file.unlink()
    except OSError:
        pass
    status, cpu, mem, exit_code = _run_ignoring_io(cmd, cwd, time_limit_ms, memory_limit_mb, out_file, stop_check)
    output = out_file.read_bytes() if out_file.exists() else b''
    return status, cpu, mem, output, b'', exit_code


def _run_ignoring_io(cmd, cwd, time_limit_ms, memory_limit_mb, out_file=None, stop_check=None):
    """文件模式运行（stdin/stdout 走 freopen），返回 (status, cpu_ms, peak_mem, exit_code)"""
    try:
        with open(os.devnull, 'rb') as devnull_in, open(os.devnull, 'wb') as devnull_out, open(os.devnull, 'wb') as devnull_err:
            proc = subprocess.Popen(
                cmd,
                stdin=devnull_in,
                stdout=devnull_out,
                stderr=devnull_err,
                cwd=cwd,
                shell=False,
            )
            job = winapi.create_job()
            if job:
                winapi.assign_job(job, proc.pid)
                # 沙盒：内核级硬限制（内存/进程数/IO），监控之外的第二道防线
                winapi.set_job_limits(job, memory_mb=memory_limit_mb, active_processes=8)
            job_closed = False
            baseline_free = None  # 运行中磁盘写入监控基准

            def kill_all():
                nonlocal job_closed
                if job and not job_closed:
                    winapi.close_job(job)
                    job_closed = True
                try:
                    kill_proc_tree(proc.pid)
                except Exception:
                    pass

            peak_mem = 0.0
            cpu_ms = 0.0
            last_cpu = 0.0
            start_wall = time.time()
            tick = 0
            reason = None
            while proc.poll() is None:
                try:
                    if stop_check is not None and stop_check():
                        reason = 'STOP'
                        kill_all()
                        break
                    pt_k, pt_u = winapi.process_cpu_times(proc.pid)
                    cpu = pt_k + pt_u
                    mem, mem_peak = winapi.process_memory_mb(proc.pid)
                    if tick % 2 == 0:
                        # 优先用 Job 枚举进程树：避免孤儿进程/PID 复用误判导致假 MLE
                        plist = winapi.job_pids(job)
                        if plist is None:
                            plist = winapi.children_pids(proc.pid)
                        for cpid in plist:
                            if cpid == proc.pid:
                                continue  # Job 列表含主进程自身，跳过
                            try:
                                ck, cu = winapi.process_cpu_times(cpid)
                                cpu += ck + cu
                                cm, _ = winapi.process_memory_mb(cpid)
                                mem += cm
                            except winapi.ProcessNotFound:
                                pass
                        if out_file is not None:
                            try:
                                if out_file.stat().st_size > OUTPUT_LIMIT:
                                    reason = 'OLE'
                                    kill_all()
                                    break
                            except OSError:
                                pass
                    cpu_now = cpu * 1000.0
                    if cpu_now > last_cpu:
                        cpu_ms += cpu_now - last_cpu
                    last_cpu = cpu_now
                    if mem > peak_mem:
                        peak_mem = mem
                    if cpu_ms > time_limit_ms:
                        reason = 'TLE'
                        kill_all()
                        break
                    if peak_mem > memory_limit_mb or mem_peak > memory_limit_mb:
                        reason = 'MLE'
                        kill_all()
                        break
                    if (time.time() - start_wall) * 1000.0 > wall_limit_ms(time_limit_ms):
                        reason = 'TLE'
                        kill_all()
                        break
                except winapi.ProcessNotFound:
                    pass
                if tick % 4 == 0:
                    try:
                        free_now = shutil.disk_usage(cwd).free
                        if baseline_free is None:
                            baseline_free = free_now
                        elif baseline_free - free_now > 1024 * 1024 * 1024:
                            # 防写爆磁盘：程序吃掉 >1GB 磁盘空间 -> OLE 并击杀
                            reason = 'OLE'
                            kill_all()
                            break
                    except OSError:
                        pass
                tick += 1
                time.sleep(0.02)
            retcode = None
            try:
                retcode = proc.poll()
            except Exception:
                retcode = None
            if retcode is None:
                try:
                    retcode = proc.wait(timeout=3)
                except Exception:
                    retcode = None
                if retcode is None:
                    # 修复：3 秒仍未退出，强制清理进程树
                    kill_all()
                    try:
                        retcode = proc.wait(timeout=2)
                    except Exception:
                        retcode = None
            if reason == 'STOP':
                return 'STOP', cpu_ms, peak_mem, retcode
            if reason == 'OLE':
                return 'OLE', cpu_ms, peak_mem, retcode
            if reason == 'MLE' or peak_mem > memory_limit_mb:
                return 'MLE', cpu_ms, peak_mem, retcode
            if reason == 'TLE' or cpu_ms > time_limit_ms:
                return 'TLE', cpu_ms, peak_mem, retcode
            if retcode != 0:
                return 'RE', cpu_ms, peak_mem, retcode
            return 'AC', cpu_ms, peak_mem, retcode
    except Exception:
        return 'RE', 0.0, 0.0, None
    finally:
        # 修复：正常结束路径也要释放 Job 对象，防内核资源泄漏
        try:
            if job and not job_closed:
                winapi.close_job(job)
                job_closed = True
            if proc is not None and proc.poll() is None:
                kill_proc_tree(proc.pid)
        except Exception:
            pass


def measure(spec, name, io, input_bytes, time_limit_ms, memory_limit_mb):
    """统一的运行入口，返回 (status, cpu_ms, peak_mem, stdout_bytes, stderr_bytes, exit_code)"""
    if io == 'stdio':
        cmd, cwd = spec_cmd(spec)
        return run_stdio(cmd, cwd, input_bytes, time_limit_ms, memory_limit_mb)
    work_dir = BUILD_DIR / ("%s_run_%d" % (name, int(time.time() * 1000)))
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        cmd, cwd = spec_cmd(spec, work_dir)
        inp = work_dir / (name + '.in')
        if input_bytes:
            inp.write_bytes(input_bytes)
        return run_file(cmd, cwd, work_dir, name, time_limit_ms, memory_limit_mb)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------- 比较 ----------
def _cap_display(text, limit):
    """限制返回给前端的展示文本长度（比较逻辑仍用完整内容）"""
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    if len(text) > limit:
        return text[:limit] + '\n…（其余内容已截断，共 %d 字符）' % len(text)
    return text


def compare(expected_bytes, actual_bytes):
    def norm(b):
        text = b.decode('utf-8', errors='ignore').rstrip()
        return [line.rstrip() for line in text.splitlines()]
    return norm(expected_bytes) == norm(actual_bytes)


# ---------- 对拍 ----------
def detect_io(src_path: Path):
    """检测源文件的 IO 模式：返回 (io, in_name, out_name)。
    freopen 文件 IO 返回 'file' 及读写文件名，否则 'stdio'"""
    try:
        content = read_text_auto(src_path)
    except Exception:
        return 'stdio', None, None
    names = re.findall(r'freopen\s*\(\s*"([^"]+)"\s*,\s*"(\w+)"\s*,\s*(\w+)\s*\)', content)
    if not names:
        return 'stdio', None, None
    in_name = None
    out_name = None
    for fname, mode, stream in names:
        stem = Path(fname).stem
        if stream == 'stdin' or mode == 'r':
            in_name = stem
        else:
            out_name = stem
    if in_name is None and out_name is None:
        return 'stdio', None, None
    if in_name is None:
        in_name = out_name
    if out_name is None:
        out_name = in_name
    return 'file', in_name, out_name


def run_hack_prog(spec, io_mode, work_dir, in_name, out_name, input_bytes, time_limit_ms, memory_limit_mb, stop_check=None):
    """对拍中运行一个程序（gen/std/main 通用），返回 (status, cpu, mem, output, stderr, exit_code)"""
    if io_mode == 'file':
        cmd, cwd = spec_cmd(spec, work_dir)
        (work_dir / (in_name + '.in')).write_bytes(input_bytes or b'')
        return run_file(cmd, cwd, work_dir, in_name, time_limit_ms, memory_limit_mb, out_name, stop_check)
    cmd, cwd = spec_cmd(spec)
    return run_stdio(cmd, cwd, input_bytes or b'', time_limit_ms, memory_limit_mb, stop_check)


def hack_worker(task_id, main_path, std_path, gen_path, rounds, io, time_limit, memory_limit, name=None):
    state = HACK_STATE[task_id]
    try:
        # 防写爆磁盘：对拍前检查构建目录所在盘剩余空间（竞赛程序可能 freopen 写大文件）
        try:
            free_bytes = shutil.disk_usage(BUILD_DIR).free
            if free_bytes < 512 * 1024 * 1024:
                state.update(status='error',
                             message='磁盘剩余空间不足（%.0fMB），请清理后重试' % (free_bytes / 1048576.0))
                return
        except OSError:
            pass
        main_src = safe_path(main_path)
        std_src = safe_path(std_path)
        gen_src = safe_path(gen_path)
        ok, main_spec, log = compile_source(main_src)
        if not ok:
            state.update(status='error', message='主程序编译失败：\n' + log)
            return
        ok, std_spec, log = compile_source(std_src)
        if not ok:
            state.update(status='error', message='std 编译失败：\n' + log)
            return
        ok, gen_spec, log = compile_source(gen_src)
        if not ok:
            state.update(status='error', message='gen 编译失败：\n' + log)
            return
        if state.get('stop'):
            state.update(status='stopped')
            return

        # IO 模式：cpp 按 freopen 自动检测，py/java 直接用配置
        def io_of(src, default_io, name_for):
            if src.suffix.lower() in ('.cpp', '.c'):
                d, i, o = detect_io(src)
                if d == 'file':
                    i = i or name_for
                    o = o or i
                    return 'file', i, o
                # 修复：cpp 检测不到 freopen 就一定是 stdio 程序（cin/cout）。
                # 不再跟随用户配置的 file 模式——否则 stdout 被丢弃、
                # 数据为空，对拍会产生"输入为空"的假反例（曾误伤用户）。
                return 'stdio', None, None
            if default_io == 'file':
                # 非 cpp（py/java）文件模式：用默认文件名，否则 run_hack_prog 会因 None 名崩溃
                return 'file', name_for, name_for
            return 'stdio', None, None

        main_io, main_in, main_out = io_of(main_src, io, name or main_src.stem)
        std_io, std_in, std_out = io_of(std_src, io, std_src.stem)
        gen_io, gen_in, gen_out = io_of(gen_src, io, gen_src.stem)

        # SPJ（配置指定或自动检测，SPJ 本身总是 C++）
        checker_spec = None
        checker_src = resolve_checker(main_src, name or Path(main_path).stem)
        if checker_src:
            ok_cc, checker_spec, clog = compile_checker(checker_src)
            if not ok_cc:
                checker_spec = None
                state['spj_warn'] = 'SPJ 编译失败，对拍按普通输出比较：\n' + clog

        save_dir = BUILD_DIR / ("hack_" + task_id)
        save_dir.mkdir(parents=True, exist_ok=True)
        work = BUILD_DIR / ("hackw_" + task_id)
        work.mkdir(parents=True, exist_ok=True)
        # 每个程序一个常驻目录：编译产物只复制一次，避免每轮全量拷贝 + 目录新建/删除。
        # 之前每轮新建 3 个目录并复制 exe，长跑时磁盘 IO 指数级膨胀，会拖垮整机。
        gwork = work / 'gen'
        swork = work / 'std'
        mwork = work / 'main'
        for d in (gwork, swork, mwork):
            d.mkdir(parents=True, exist_ok=True)

        def stop_check():
            return bool(state.get('stop'))

        i = 0
        while True:
            i += 1
            if state.get('stop'):
                state.update(status='stopped')
                return
            state.update(round=i)

            # gen 生成数据
            if gen_io == 'file':
                s, _, _, data, err, _ec = run_hack_prog(gen_spec, 'file', gwork, gen_in, gen_out, b'', 10000, 2048, stop_check)
            else:
                s, _, _, data, err, _ec = run_hack_prog(gen_spec, 'stdio', None, None, None, b'', 10000, 2048, stop_check)
            if s == 'STOP':
                state.update(status='stopped')
                return
            if s != 'AC':
                state.update(status='error', message='gen 运行错误（第 %d 组）：%s' % (i, err.decode('utf-8', 'ignore')))
                return

            # std 跑出答案
            if std_io == 'file':
                s, _, _, ans, err, _ec = run_hack_prog(std_spec, 'file', swork, std_in, std_out, data, 10000, 2048, stop_check)
            else:
                s, _, _, ans, err, _ec = run_hack_prog(std_spec, 'stdio', None, None, None, data, 10000, 2048, stop_check)
            if s == 'STOP':
                state.update(status='stopped')
                return
            if s != 'AC':
                state.update(status='error', message='std 运行错误（第 %d 组）：%s' % (i, err.decode('utf-8', 'ignore')))
                return

            # 主程序跑
            if main_io == 'file':
                s, _, _, out, err, _ec = run_hack_prog(main_spec, 'file', mwork, main_in, main_out, data, time_limit, memory_limit, stop_check)
            else:
                s, _, _, out, err, _ec = run_hack_prog(main_spec, 'stdio', None, None, None, data, time_limit, memory_limit, stop_check)
            if s == 'STOP':
                state.update(status='stopped')
                return
            if s not in ('AC', 'WA'):
                state.update(status='error', message='主程序运行错误（第 %d 组）：%s' % (i, s))
                return

            if checker_spec:
                spj_ok, _ = run_checker(checker_spec, data, out, ans, stop_check=stop_check)
                if state.get('stop'):
                    state.update(status='stopped')
                    return
                wrong = not spj_ok
            else:
                wrong = not compare(ans, out)
            if wrong:
                (save_dir / 'input.txt').write_bytes(data)
                (save_dir / 'answer.txt').write_bytes(ans)
                (save_dir / 'output.txt').write_bytes(out)
                state.update(
                    status='found', round=i,
                    input=data.decode('utf-8', 'ignore'),
                    answer=ans.decode('utf-8', 'ignore'),
                    output=out.decode('utf-8', 'ignore'),
                )
                return

            if rounds > 0 and i >= rounds:
                break

        state.update(status='done')
    except Exception as e:
        state.update(status='error', message='对拍异常：%s' % e)
    finally:
        shutil.rmtree(BUILD_DIR / ("hackw_" + task_id), ignore_errors=True)


# ---------- HTTP ----------
# 允许访问的主机：回环 + 局域网私有地址段（保留对公网的跨站防护）
_ALLOWED_LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')


def _ip_is_lan(ip):
    return ip.is_private or ip.is_loopback or ip.is_link_local


def host_allowed(host):
    """判断 Host/Origin 是否属于本机或局域网（IPv4/IPv6 私有段、链路本地）。"""
    host = (host or '').strip().strip('[]').lower()
    if not host or host in _ALLOWED_LOCAL_HOSTS:
        return True
    try:
        return _ip_is_lan(ipaddress.ip_address(host))
    except ValueError:
        pass
    # 非 IP 形式（如局域网机器名）：解析后只要有一个局域网地址即放行
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            if _ip_is_lan(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False


def _lan_ip():
    """获取本机局域网 IPv4 地址（用于向用户展示访问地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ''


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepCoder/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- 工具 ---
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            return {}

    def _local_ok(self):
        """只接受本机或局域网（私有地址段）的请求，防止公网恶意网页跨站调用 API"""
        host = self.headers.get('Host', '') or ''
        if ':' in host:
            host = host.rsplit(':', 1)[0]
        if host and not host_allowed(host):
            self.send_error(403, 'Forbidden')
            return False
        origin = self.headers.get('Origin', '') or ''
        if origin:
            try:
                oh = urlparse(origin).hostname or ''
            except Exception:
                oh = ''
            if oh and not host_allowed(oh):
                self.send_error(403, 'Forbidden')
                return False
        return True

    def send_file(self, path: Path, mime):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    # --- 静态资源 ---
    def serve_static(self, path):
        rel = path.lstrip('/')
        if rel == '':
            rel = 'index.html'
        elif rel.startswith('static/'):
            rel = rel[len('static/'):]
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR)):
            self.send_error(403)
            return
        mime = 'text/html'
        if rel.endswith('.js'):
            mime = 'application/javascript'
        elif rel.endswith('.css'):
            mime = 'text/css'
        elif rel.endswith('.png'):
            mime = 'image/png'
        elif rel.endswith('.svg'):
            mime = 'image/svg+xml'
        self.send_file(target, mime)

    # --- API 路由 ---
    def do_GET(self):
        if not self._local_ok():
            return
        try:
            self._do_get()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"error": "服务器内部错误：%s" % e}, 500)
            except Exception:
                pass

    def _do_get(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/' or path.startswith('/static/'):
            self.serve_static(path)
            return

        if path == '/api/tree':
            self.send_json({"tree": build_tree()})
            return

        if path == '/api/file':
            params = _parse_query(parsed.query)
            rel = params.get('path', '')
            if not rel:
                self.send_json({"error": "缺少 path"}, 400)
                return
            try:
                p = safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
            if not p.is_file():
                self.send_json({"error": "文件不存在"}, 404)
                return
            size = p.stat().st_size
            max_size = int(params.get('max_size', 0) or 0)
            truncated = False
            if max_size > 0 and size > max_size:
                content = ''
                truncated = True
            else:
                content = read_text_auto(p)
            self.send_json({
                "path": rel,
                "content": content,
                "size": size,
                "truncated": truncated,
                "samples": find_samples(p) if p.suffix.lower() in ('.cpp', '.c', '.py', '.java') else [],
                "config": get_problem_config(p.stem),
            })
            return

        if path == '/api/raw':
            params = _parse_query(parsed.query)
            rel = params.get('path', '')
            if not rel:
                self.send_json({"error": "缺少 path"}, 400)
                return
            try:
                p = safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
            if not p.is_file():
                self.send_error(404)
                return
            mime_map = {
                '.pdf': 'application/pdf',
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
                '.bmp': 'image/bmp',
            }
            mime = mime_map.get(p.suffix.lower(), 'application/octet-stream')
            data = p.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            if params.get('download') == '1':
                # 触发浏览器下载（attachment），文件名支持中文（RFC 5987）
                fname = p.name
                ascii_name = fname.encode('ascii', 'replace').decode('ascii').replace('"', '_')
                disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(fname))
                self.send_header('Content-Disposition', disp)
            self.end_headers()
            self.wfile.write(data)
            return

        if path == '/api/next_sample_no':
            params = _parse_query(parsed.query)
            rel = params.get('path', '')
            try:
                p = safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
            d = p.parent
            stem = p.stem
            n = 0
            for f in d.glob(stem + '*.in'):
                suffix = f.stem[len(stem):]
                if suffix.isdigit():
                    n = max(n, int(suffix))
            self.send_json({"n": n})
            return

        if path == '/api/config':
            self.send_json({"config": load_config()})
            return

        if path == '/api/initial_file':
            self.send_json({"file": INITIAL_FILE})
            return

        if path == '/api/hack_list':
            with HACK_LOCK:
                tasks = {t: dict(s) for t, s in HACK_STATE.items()}
            self.send_json({"tasks": tasks})
            return

        if path == '/api/hack_status':
            qs = parse_qs(parsed.query)
            task = qs.get('task', [''])[0]
            with HACK_LOCK:
                state = HACK_STATE.get(task, {"status": "not_found"})
            self.send_json(state)
            return

        if path == '/api/chat/key':
            self.send_json({"has_key": read_ai_key() is not None})
            return

        self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._local_ok():
            return
        try:
            self._do_put()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"error": "服务器内部错误：%s" % e}, 500)
            except Exception:
                pass

    def _do_put(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/file':
            body = self.read_json()
            rel = body.get('path', '')
            content = body.get('content', '')
            try:
                p = safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            self.send_json({"ok": True})
            return
        if parsed.path == '/api/upload':
            params = {}
            for kv in parsed.query.split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    params[unquote(k)] = unquote(v)
            rel = params.get('path', '')
            if not rel:
                self.send_json({"error": "缺少 path"}, 400)
                return
            try:
                p = safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = self.rfile.read(length)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            self.send_json({"ok": True, "size": len(data)})
            return
        if parsed.path == '/api/chat/key':
            body = self.read_json()
            key = body.get('key', '')
            try:
                save_ai_key(key)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_json({"ok": True})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local_ok():
            return
        try:
            self._do_post()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.send_json({"error": "服务器内部错误：%s" % e}, 500)
            except Exception:
                pass

    def _do_post(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_json()

        if path == '/api/run':
            self.api_run(body)
            return
        if path == '/api/run_all':
            self.api_run_all(body)
            return
        if path == '/api/compile':
            self.api_compile(body)
            return
        if path == '/api/hack':
            self.api_hack(body)
            return
        if path == '/api/hack_stop':
            task = body.get('task', '')
            with HACK_LOCK:
                if task in HACK_STATE:
                    HACK_STATE[task]['stop'] = True
            self.send_json({"ok": True})
            return
        if path == '/api/hack_to_sample':
            self.api_hack_to_sample(body)
            return
        if path == '/api/interactive_open':
            self.api_interactive_open(body)
            return
        if path == '/api/interactive_start':
            self.api_interactive_start(body)
            return
        if path == '/api/interactive_input':
            self.api_interactive_input(body)
            return
        if path == '/api/interactive_poll':
            self.api_interactive_poll(body)
            return
        if path == '/api/interactive_kill':
            self.api_interactive_kill(body)
            return
        if path == '/api/complete':
            self.api_complete(body)
            return
        if path == '/api/ai_complete':
            self.api_ai_complete(body)
            return
        if path == '/api/chat':
            self.api_chat(body)
            return
        if path == '/api/new_file':
            self.api_new_file(body)
            return
        if path == '/api/new_dir':
            self.api_new_dir(body)
            return
        if path == '/api/delete':
            self.api_delete(body)
            return
        if path == '/api/clear_samples':
            self.api_clear_samples(body)
            return
        if path == '/api/rename':
            self.api_rename(body)
            return
        if path == '/api/config':
            name = body.get('name', '')
            patch = body.get('patch', {})
            if name == '__compile__':
                cfg = load_config()
                cfg['compile'] = patch
                save_config(cfg)
            else:
                set_problem_config(name, patch)
            self.send_json({"ok": True})
            return

        self.send_json({"error": "not found"}, 404)

    # --- 具体 API ---
    def _load_src(self, body):
        rel = body.get('path', '')
        try:
            p = safe_path(rel)
        except ValueError:
            return None, None, "非法路径"
        if not p.is_file():
            return None, None, "文件不存在"
        return p, p.stem, None

    def api_run(self, body):
        src, name, err = self._load_src(body)
        if err:
            self.send_json({"error": err}, 400)
            return
        io = body.get('io', 'stdio')
        try:
            time_limit = int(body.get('time_limit', DEFAULT_TIME_LIMIT))
            memory_limit = int(body.get('memory_limit', DEFAULT_MEMORY_LIMIT))
        except (TypeError, ValueError):
            time_limit = DEFAULT_TIME_LIMIT
            memory_limit = DEFAULT_MEMORY_LIMIT

        skip_compile = bool(body.get('skip_compile', False))
        spec = None
        if skip_compile and src.suffix.lower() == '.cpp' and (BUILD_DIR / name / "main.exe").exists():
            ok, spec, log = True, {'kind': 'cpp', 'exe': str(BUILD_DIR / name / "main.exe")}, ''
        else:
            ok, spec, log = compile_source(src)
        if not ok:
            self.send_json({"ok": False, "stage": "compile", "log": log})
            return

        custom = body.get('input', None)
        input_file = body.get('input_file', None)
        input_bytes = b''
        if input_file:
            try:
                p_in = safe_path(input_file)
                if not p_in.is_file():
                    self.send_json({"error": "输入文件不存在"}, 404)
                    return
                input_bytes = p_in.read_bytes()
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return
        elif custom is not None:
            input_bytes = custom.encode('utf-8')
        else:
            inp = src.parent / (name + '.in')
            if inp.exists():
                input_bytes = inp.read_bytes()

        status, cpu, mem, stdout, stderr, exit_code = measure(spec, name, io, input_bytes, time_limit, memory_limit)
        result = {
            "ok": True,
            "status": status,
            "time": round(cpu),
            "memory": round(mem, 1),
            "stdout": _cap_display(stdout, 512 * 1024),
            "stderr": _cap_display(stderr, 512 * 1024),
        }
        if status == 'RE':
            ec, hint = exit_code_info(exit_code)
            result["exit_code"] = ec
            if hint:
                result["exit_hint"] = hint
        expected_file = body.get('expected_file', None)
        expected_content = body.get('expected', None)
        if expected_file or expected_content is not None:
            if expected_file:
                try:
                    p_exp = safe_path(expected_file)
                    if not p_exp.is_file():
                        self.send_json({"error": "期望输出文件不存在"}, 404)
                        return
                    expected = p_exp.read_bytes()
                except ValueError:
                    self.send_json({"error": "非法路径"}, 400)
                    return
            else:
                expected = expected_content.encode('utf-8')
            result["expected"] = _cap_display(expected, 10000)
            if status == 'AC':
                if not expected.strip():
                    # 期望输出为空：只报告运行状态，不参与比较
                    result["verdict"] = 'AC'
                else:
                    # 优先使用 SPJ（配置指定或自动检测），其次普通比较
                    checker_src = resolve_checker(src, name)
                    if checker_src:
                        ok_cc, checker_spec, clog = compile_checker(checker_src)
                        if ok_cc:
                            spj_ok, spj_log = run_checker(checker_spec, input_bytes, stdout, expected)
                            result["verdict"] = 'AC' if spj_ok else 'WA'
                            if spj_log:
                                result["spj_log"] = _cap_display(spj_log, 10000)
                        else:
                            result["verdict"] = 'CE-SPJ'
                            result["spj_log"] = _cap_display(clog, 10000)
                    else:
                        result["verdict"] = 'AC' if compare(expected, stdout) else 'WA'
            else:
                result["verdict"] = status
        self.send_json(result)

    def api_hack_to_sample(self, body):
        """把对拍发现的反例保存为题目样例（{stem}{n}.in / {stem}{n}.out）"""
        task = body.get('task', '')
        with HACK_LOCK:
            st = HACK_STATE.get(task)
        if not st or st.get('status') != 'found':
            self.send_json({"error": "任务不存在或未发现反例"}, 400)
            return
        try:
            p = safe_path(st.get('main', ''))
        except ValueError:
            self.send_json({"error": "非法路径"}, 400)
            return
        d = p.parent
        stem = p.stem
        base_dir = d / '.samples'
        base_dir.mkdir(parents=True, exist_ok=True)
        nums = []
        for f in base_dir.glob(stem + '*.in'):
            suffix = f.stem[len(stem):]
            if suffix.isdigit():
                nums.append(int(suffix))
        n = (max(nums) + 1) if nums else 1
        in_file = base_dir / ('%s%d.in' % (stem, n))
        out_file = base_dir / ('%s%d.out' % (stem, n))
        save_dir = BUILD_DIR / ("hack_" + task)
        if (save_dir / 'input.txt').exists():
            shutil.copy(save_dir / 'input.txt', in_file)
        else:
            in_file.write_bytes((st.get('input') or '').encode('utf-8'))
        if (save_dir / 'answer.txt').exists():
            shutil.copy(save_dir / 'answer.txt', out_file)
        else:
            out_file.write_bytes((st.get('answer') or '').encode('utf-8'))
        self.send_json({"ok": True, "n": n, "in": in_file.name, "out": out_file.name})

    def api_compile(self, body):
        """仅编译，不运行"""
        src, name, err = self._load_src(body)
        if err:
            self.send_json({"error": err}, 400)
            return
        ok, spec, log = compile_source(src)
        if not ok:
            self.send_json({"ok": False, "log": log})
            return
        self.send_json({"ok": True, "log": log})

    def api_run_all(self, body):
        """一次编译，依次运行所有样例，返回每个样例的结果"""
        src, name, err = self._load_src(body)
        if err:
            self.send_json({"error": err}, 400)
            return
        io = body.get('io', 'stdio')
        try:
            time_limit = int(body.get('time_limit', DEFAULT_TIME_LIMIT))
            memory_limit = int(body.get('memory_limit', DEFAULT_MEMORY_LIMIT))
        except (TypeError, ValueError):
            time_limit = DEFAULT_TIME_LIMIT
            memory_limit = DEFAULT_MEMORY_LIMIT

        ok, spec, log = compile_source(src)
        if not ok:
            self.send_json({"ok": False, "log": log})
            return

        checker_src = resolve_checker(src, name)
        checker_spec = None
        spj_compile_error = None
        if checker_src:
            ok_cc, checker_spec, clog = compile_checker(checker_src)
            if not ok_cc:
                checker_spec = None
                spj_compile_error = clog

        results = []
        for s in body.get('samples', []) or []:
            label = s.get('label', '')
            expected = None
            if s.get('type') == 'manual':
                input_bytes = (s.get('input') or '').encode('utf-8')
                expected = (s.get('expected') or '').encode('utf-8')
            else:
                input_bytes = b''
                in_file = s.get('in') or ''
                if in_file:
                    try:
                        p_in = safe_path(in_file)
                        if p_in.is_file():
                            input_bytes = p_in.read_bytes()
                    except ValueError:
                        pass
                out_file = s.get('out') or ''
                if out_file:
                    try:
                        p_exp = safe_path(out_file)
                        if p_exp.is_file():
                            expected = p_exp.read_bytes()
                    except ValueError:
                        pass
            status, cpu, mem, stdout, stderr, exit_code = measure(spec, name, io, input_bytes, time_limit, memory_limit)
            entry = {
                "label": label,
                "status": status,
                "time": round(cpu),
                "memory": round(mem, 1),
                "stdout": _cap_display(stdout, 512 * 1024),
                "stderr": _cap_display(stderr, 512 * 1024),
            }
            if status == 'RE':
                ec, hint = exit_code_info(exit_code)
                entry["exit_code"] = ec
                if hint:
                    entry["exit_hint"] = hint
            if expected is not None:
                if expected.strip():
                    if status == 'AC':
                        if checker_spec:
                            spj_ok, spj_log = run_checker(checker_spec, input_bytes, stdout, expected)
                            entry["verdict"] = 'AC' if spj_ok else 'WA'
                            if spj_log:
                                entry["spj_log"] = _cap_display(spj_log, 10000)
                        else:
                            entry["verdict"] = 'AC' if compare(expected, stdout) else 'WA'
                    else:
                        entry["verdict"] = status
                else:
                    # 期望输出为空：只报告运行状态，不参与比较
                    entry["verdict"] = status
                entry["expected"] = _cap_display(expected, 10000)
            else:
                entry["verdict"] = status
            results.append(entry)
        self.send_json({"ok": True, "results": results, "spj_compile_error": spj_compile_error})

    def api_hack(self, body):
        main_path = body.get('main', '')
        std_path = body.get('std', '')
        gen_path = body.get('gen', '')
        try:
            rounds = int(body.get('rounds', 100))
        except (TypeError, ValueError):
            rounds = 100
        io = body.get('io', 'stdio')
        try:
            time_limit = int(body.get('time_limit', DEFAULT_TIME_LIMIT))
            memory_limit = int(body.get('memory_limit', DEFAULT_MEMORY_LIMIT))
        except (TypeError, ValueError):
            time_limit = DEFAULT_TIME_LIMIT
            memory_limit = DEFAULT_MEMORY_LIMIT
        name = body.get('name', '') or Path(main_path).stem

        for rel in (main_path, std_path, gen_path):
            try:
                safe_path(rel)
            except ValueError:
                self.send_json({"error": "非法路径"}, 400)
                return

        task_id = str(int(time.time() * 1000))
        with HACK_LOCK:
            # 并发任务数上限：防止同时启动过多对拍（每任务每轮 3 个进程）把机器拖死
            running = sum(1 for s in HACK_STATE.values() if s.get('status') == 'running')
            if running >= 4:
                self.send_json({"error": "已有 %d 个对拍任务在运行，请先停止或等待完成" % running}, 400)
                return
            HACK_STATE[task_id] = {"status": "running", "round": 0, "stop": False,
                                   "main": main_path, "std": std_path, "gen": gen_path,
                                   "name": name, "rounds": rounds, "io": io,
                                   "time_limit": time_limit, "memory_limit": memory_limit}
            # 清理太旧的已结束任务，防止内存无限增长
            finished = [t for t, s in HACK_STATE.items()
                        if s.get('status') in ('done', 'found', 'error', 'stopped')]
            for t in finished[:-30]:
                HACK_STATE.pop(t, None)
        t = threading.Thread(target=hack_worker,
                             args=(task_id, main_path, std_path, gen_path, rounds, io, time_limit, memory_limit, name),
                             daemon=True)
        t.start()
        self.send_json({"ok": True, "task": task_id})

    # --- 交互模式（两阶段：先开会话，再编译启动） ---
    def api_interactive_open(self, body):
        """创建交互会话（不编译），返回 session，前端先打开窗口"""
        src, name, err = self._load_src(body)
        if err:
            self.send_json({"error": err}, 400)
            return
        session = str(int(time.time() * 1000))
        rec = {
            "proc": None,
            "job": None,
            "path": str(src),
            "out_chunks": [],
            "err_chunks": [],
            "out_pos": 0,
            "err_pos": 0,
            "status": "starting",
            "exit_code": None,
            "log": "",
        }
        with INTERACTIVE_LOCK:
            for sid in [s for s, r in INTERACTIVE.items() if r["status"] != "running"]:
                rec_old = INTERACTIVE.pop(sid, None)
                if rec_old:
                    # 修复：清理会话时释放 Job 对象，防句柄/内核资源泄漏
                    if rec_old.get("job"):
                        try:
                            winapi.close_job(rec_old["job"])
                        except Exception:
                            pass
                        rec_old["job"] = None
                    # 兜底清理残留进程
                    if rec_old.get("proc"):
                        try:
                            kill_proc_tree(rec_old["proc"].pid)
                        except Exception:
                            pass
            INTERACTIVE[session] = rec
        self.send_json({"ok": True, "session": session})

    def api_interactive_start(self, body):
        """在已有会话中编译并启动程序，编译失败时写入会话（窗口内显示）"""
        session = body.get("session", "")
        with INTERACTIVE_LOCK:
            rec = INTERACTIVE.get(session)
        if not rec:
            self.send_json({"error": "会话不存在"}, 404)
            return
        src = Path(rec["path"])
        ok, spec, log = compile_source(src)
        if not ok:
            rec["status"] = "ce"
            rec["log"] = log
            self.send_json({"ok": True, "status": "ce"})
            return
        # 编译期间窗口可能已关闭（会话被 kill 移除），此时不再启动进程
        with INTERACTIVE_LOCK:
            if INTERACTIVE.get(session) is not rec:
                self.send_json({"error": "会话已关闭"}, 404)
                return
        cmd, cwd = spec_cmd(spec)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, shell=False,
            )
        except Exception as e:
            rec["status"] = "ce"
            rec["log"] = str(e)
            self.send_json({"ok": True, "status": "ce"})
            return
        rec["proc"] = proc
        rec["job"] = winapi.create_job()
        if rec["job"]:
            winapi.assign_job(rec["job"], proc.pid)
            # 沙盒：交互会话也加内核硬限制（防程序吃爆系统）
            winapi.set_job_limits(rec["job"], memory_mb=2048, active_processes=8)
        rec["status"] = "running"
        threading.Thread(target=_reader, args=(proc.stdout, rec["out_chunks"]), daemon=True).start()
        threading.Thread(target=_reader, args=(proc.stderr, rec["err_chunks"]), daemon=True).start()
        self.send_json({"ok": True, "status": "running"})

    def api_interactive_input(self, body):
        session = body.get("session", "")
        text = body.get("text", "")
        with INTERACTIVE_LOCK:
            rec = INTERACTIVE.get(session)
        if not rec:
            self.send_json({"error": "会话不存在"}, 404)
            return
        if not rec.get("proc") or rec["status"] != "running":
            self.send_json({"error": "程序尚未启动"}, 400)
            return
        try:
            rec["proc"].stdin.write(text.encode("utf-8"))
            rec["proc"].stdin.flush()
        except Exception:
            pass
        self.send_json({"ok": True})

    def api_interactive_poll(self, body):
        session = body.get("session", "")
        with INTERACTIVE_LOCK:
            rec = INTERACTIVE.get(session)
            if not rec:
                self.send_json({"status": "not_found"})
                return
            if rec["status"] == "starting":
                self.send_json({"status": "starting"})
                return
            if rec["status"] == "ce":
                INTERACTIVE.pop(session, None)
                self.send_json({"status": "ce", "log": rec["log"]})
                return
            proc = rec["proc"]
            if rec["status"] == "running" and proc.poll() is not None:
                rec["status"] = "exited"
                rec["exit_code"] = proc.returncode
            out = b''.join(rec["out_chunks"])[rec["out_pos"]:].decode("utf-8", "replace")
            err = b''.join(rec["err_chunks"])[rec["err_pos"]:].decode("utf-8", "replace")
            rec["out_pos"] = sum(len(c) for c in rec["out_chunks"])
            rec["err_pos"] = sum(len(c) for c in rec["err_chunks"])
            status = rec["status"]
            exit_code = rec["exit_code"]
            if status == "exited":
                winapi.close_job(rec.get("job"))
                INTERACTIVE.pop(session, None)
        if status == "exited":
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    stream.close()
                except Exception:
                    pass
        self.send_json({"status": status, "exit_code": exit_code, "out": out, "err": err})

    def api_interactive_kill(self, body):
        session = body.get("session", "")
        with INTERACTIVE_LOCK:
            rec = INTERACTIVE.pop(session, None)
        if rec:
            if rec.get("job"):
                winapi.close_job(rec["job"])  # KILL_ON_JOB_CLOSE：瞬间终止整棵进程树
            if rec.get("proc"):
                try:
                    kill_proc_tree(rec["proc"].pid)
                except Exception:
                    pass
                for stream in (rec["proc"].stdout, rec["proc"].stderr, rec["proc"].stdin):
                    try:
                        stream.close()
                    except Exception:
                        pass
        self.send_json({"ok": True})

    def api_chat(self, body):
        """AI 聊天：参考 DeepChat 的操作模式，SSE 流式返回 DeepSeek 推理/正文增量。

        请求：{messages:[{role,content}...], model?, reasoning_effort?, thinking_enabled?}
        响应：text/event-stream，data: {type: thinking|content_delta|usage|done|error}
        """
        messages = body.get('messages') or []
        if not messages or not isinstance(messages, list):
            self.send_json({"error": "缺少 messages"}, 400)
            return
        # 过滤非法消息，保证首条为 system（参考 DeepChat chat() 的 ctx 组装）
        clean = []
        for m in messages:
            if isinstance(m, dict) and m.get('role') in ('system', 'user', 'assistant') and isinstance(m.get('content'), str):
                clean.append({"role": m['role'], "content": m['content']})
        if not clean:
            self.send_json({"error": "messages 为空"}, 400)
            return
        if clean[0]['role'] != 'system':
            clean.insert(0, {"role": "system", "content": ai_system_prompt()})
        model = body.get('model') or AI_DEFAULT_MODEL
        r_eff = body.get('reasoning_effort') or 'max'
        thinking = bool(body.get('thinking_enabled', True))

        # 若未配置 Key，先返回 JSON 错误（前端弹窗引导设置）
        if not read_ai_key():
            self.send_json({"error": "未配置 API Key", "need_key": True}, 400)
            return

        # SSE 响应头
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def emit(evt):
            try:
                line = json.dumps(evt, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % line).encode('utf-8'))
                self.wfile.flush()
            except Exception:
                pass

        ai_stream_chat(clean, model, r_eff, thinking, emit, tools=AI_TOOLS)
        try:
            self.wfile.flush()
        except Exception:
            pass

    def api_ai_complete(self, body):
        """AI 代码补全：{path, content, line, ch} -> {text}（flash 不思考，快速）"""
        path = body.get('path', '') or ''
        content = body.get('content', '') or ''
        try:
            line = int(body.get('line', 0) or 0)
            ch = int(body.get('ch', 0) or 0)
        except (TypeError, ValueError):
            line, ch = 0, 0
        if len(content) > 300000:
            content = content[:300000]
        try:
            text = ai_complete_suggestion(path, content, max(line, 0), max(ch, 0))
        except Exception:
            text = None
        self.send_json({"text": text or ""})

    def api_complete(self, body):
        """代码补全：{path, content, line, ch} -> {candidates:[{label,type,detail,insertText}]}"""
        path = body.get('path', '') or ''
        content = body.get('content', '') or ''
        try:
            line = int(body.get('line', 0) or 0)
            ch = int(body.get('ch', 0) or 0)
        except (TypeError, ValueError):
            line, ch = 0, 0
        if len(content) > 1000000:
            content = content[:1000000]
        try:
            cands = build_completion(path, content, max(line, 0), max(ch, 0))
        except Exception as e:
            self.send_json({"error": "补全失败：%s" % e}, 500)
            return
        self.send_json({"candidates": cands, "prefix": (content.split('\n')[max(min(line, len(content.split('\n')) - 1), 0)][:max(ch, 0)])})

    def api_new_file(self, body):
        rel = body.get('path', '')
        try:
            p = safe_path(rel)
        except ValueError:
            self.send_json({"error": "非法路径"}, 400)
            return
        if p.exists():
            self.send_json({"error": "文件已存在"}, 400)
            return
        write_template_file(p, rel)
        self.send_json({"ok": True})

    def api_new_dir(self, body):
        rel = body.get('path', '')
        try:
            p = safe_path(rel)
        except ValueError:
            self.send_json({"error": "非法路径"}, 400)
            return
        if p.exists():
            self.send_json({"error": "已存在"}, 400)
            return
        p.mkdir(parents=True, exist_ok=True)
        self.send_json({"ok": True})

    def api_delete(self, body):
        rel = body.get('path', '')
        try:
            p = safe_path(rel)
        except ValueError:
            self.send_json({"error": "非法路径"}, 400)
            return
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
        self.send_json({"ok": True})

    def api_rename(self, body):
        old = body.get('old', '')
        new = body.get('new', '')
        try:
            op = safe_path(old)
            np_ = safe_path(new)
        except ValueError:
            self.send_json({"error": "非法路径"}, 400)
            return
        if not op.exists():
            self.send_json({"error": "文件不存在"}, 404)
            return
        np_.parent.mkdir(parents=True, exist_ok=True)
        op.rename(np_)
        self.send_json({"ok": True})


def main():
    parser = argparse.ArgumentParser(description='DeepCoder 本地竞赛 IDE')
    parser.add_argument('file', nargs='?', default=None, help='启动后打开的文件（不存在则自动按模板创建）')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', type=str, default='0.0.0.0', help='监听地址（默认 0.0.0.0 允许局域网访问）')
    parser.add_argument('--dir', type=str, default=None, help='工作目录（默认当前目录）')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()

    global WORKSPACE_DIR, BUILD_DIR, IDE_CONFIG_FILE, INITIAL_FILE
    if args.dir:
        WORKSPACE_DIR = Path(args.dir).absolute()
        WORKSPACE_DIR.mkdir(exist_ok=True)
        BUILD_DIR = WORKSPACE_DIR / ".build"
        IDE_CONFIG_FILE = WORKSPACE_DIR / ".ide.json"
        BUILD_DIR.mkdir(exist_ok=True)

    if args.file:
        try:
            p = safe_path(args.file)
            if p.is_dir():
                print('警告: %s 是目录，忽略' % args.file)
            else:
                if not p.exists():
                    write_template_file(p, args.file)
                    print('已创建文件：%s' % args.file)
                INITIAL_FILE = str(p.relative_to(WORKSPACE_DIR)).replace('\\', '/')
        except ValueError:
            print('警告: 无法打开文件 %s（路径非法）' % args.file)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = 'http://127.0.0.1:%d' % args.port
    print('DeepCoder 已启动：%s' % url)
    lan_ip = _lan_ip()
    if lan_ip:
        print('局域网访问：http://%s:%d' % (lan_ip, args.port))
    print('工作目录：%s' % WORKSPACE_DIR)
    print('按 Ctrl+C 退出')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')


if __name__ == '__main__':
    main()
