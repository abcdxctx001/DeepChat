# -*- coding: utf-8 -*-
"""
DeepChat Headless API（无界面 HTTP 接口）
==========================================
为 DeepChat 项目提供“无头”（headless）调用能力：不带浏览器 UI，直接用 HTTP 调用
DeepChat 的【对话模式】与【操作模式】。

原理
----
本程序相当于一个“无头浏览器”，用 Python 复刻了前端页面 (templates/index.html)
的全部转发逻辑，通过 HTTP 与正在运行的 DeepChat 组件交互：

  ┌───────────────┐   HTTP    ┌────────────────────────┐   HTTP   ┌──────────────────┐
  │  调用方程序    │ ────────▶ │  tool/headless_api      │ ───────▶ │ server/app.py    │
  │ (curl/任何语言)│ ◀──────── │  (本文件, 默认 :5100)    │ ◀─────── │ (对话/操作业务)   │
  └───────────────┘           └───────────┬────────────┘          └──────────────────┘
                                          │ HTTP
                                          ▼
                              ┌────────────────────────┐
                              │ client/agent.py (:5001)│  本机操作代理（执行命令等）
                              └────────────────────────┘

  * 对话模式（chat）  ：转发到 server 的 /chat（mode=chat），轮询节点直到生成完成；
  * 操作模式（operate）：建立 server 的 /chat（mode=operation）NDJSON 事件流，
    在后台复刻浏览器行为——
      - 收到 command_execute 事件 → 转发给本机 agent /execute 执行 → 回传
        /api/command_result；
      - 对 #timer / #exec --async 任务，轮询 agent /task/<id> 并在状态变化时
        回传 /api/timer_notify（唤醒 AI 继续处理）；
      - 汇总 thinking / 正文 / 命令结果 / 最终答复 / 用量 等全部事件。

本程序只“新增文件”，不修改 DeepChat 的其它任何代码。

依赖
----
Python 3.8+；flask、requests（与 DeepChat server 相同，无需额外安装）。

运行
----
    python headless_api.py            # 默认监听 127.0.0.1:5100
    set HEADLESS_PORT=5200 && python headless_api.py

环境变量
--------
    DEEPCHAT_SERVER   DeepChat server 地址，默认 http://127.0.0.1:5000
    DEEPCHAT_AGENT    DeepChat agent  地址，默认 http://127.0.0.1:5001
    HEADLESS_HOST     本 API 监听地址，默认 127.0.0.1（仅本机；如需外部访问设为 0.0.0.0）
    HEADLESS_PORT     本 API 监听端口，默认 5100

接口一览
--------
    GET  /health                              健康检查（server/agent 在线状态）
    POST /api/chat                            对话模式：发送消息，等待/流式获取回复
    GET  /api/chat/node/<node_id>             对话模式：查询节点详情
    GET  /api/chat/tree                       对话模式：查询对话树
    POST /api/operate                         操作模式：新建会话并发送第一条消息
    POST /api/operate/<sid>/message           操作模式：向已有会话追加消息
    POST /api/operate/<sid>/cancel            操作模式：取消（终止）会话
    GET  /api/operate/<sid>/status            操作模式：查询会话状态
    GET  /api/operate/<sid>/events?after=0    操作模式：增量拉取事件记录
    GET  /api/sessions                        列出本 API 管理的全部操作会话
    GET  /                                    接口简介（JSON）

详见同目录 README.md。
"""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid

import requests
from flask import Flask, Response, jsonify, request

# 某些环境（如 Anaconda 遗留的全局变量）中会残留 WERKZEUG_SERVER_FD，
# 导致 werkzeug 误把该值当作已打开的 socket fd 使用而启动失败
# （OSError WinError 10038）。这里主动清除，保证在任意环境下都能正常监听。
os.environ.pop('WERKZEUG_SERVER_FD', None)

# =============================================================================
# 配置
# =============================================================================

SERVER_BASE = os.environ.get('DEEPCHAT_SERVER', 'http://127.0.0.1:5000').rstrip('/')
AGENT_BASE = os.environ.get('DEEPCHAT_AGENT', 'http://127.0.0.1:5001').rstrip('/')
HOST = os.environ.get('HEADLESS_HOST', '127.0.0.1')
PORT = int(os.environ.get('HEADLESS_PORT', '5100'))

# 操作模式默认设置（与前端 index.html 的 DEFAULT_OP_SETTINGS 保持一致）
DEFAULT_OP_SETTINGS = {
    "tools": {"file": True, "web": True, "timer": True, "screen": True,
              "memory": True, "plan": True, "agent": True, "compress": True},
    "idle_timeout": 30, "max_timeout": 600, "autosave_interval": 300,
    "process_refresh": 5, "process_truncate": 4000, "list_limit": 50,
    "expand_reasoning": True, "expand_cmd": True, "ctx_warn_ratio": 0.5,
}

DEFAULT_CHAT_WAIT = 300.0      # 对话模式等待生成完成的默认超时（秒）
DEFAULT_OPERATE_WAIT = 600.0   # 操作模式等待最终答复的默认超时（秒）

EVENT_STR_LIMIT = 65536        # 单条事件中单个字符串字段的存储上限（截断保存）
EVENTS_SOFT_MAX = 50000        # 内存中保留的最大事件条数（超出时丢弃最早的 1/5）

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('headless')

app = Flask(__name__)
try:
    app.config['JSON_AS_ASCII'] = False   # 兼容旧版 Flask（1.x）
except Exception:
    pass

# =============================================================================
# 基础工具
# =============================================================================


def _body():
    """读取请求 JSON（容错）。"""
    return request.get_json(silent=True) or {}


def _json_safe(resp):
    """尽力把响应解析为 JSON，失败返回 None。"""
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(resp.text)
        except Exception:
            return None


def _clip(s, limit=600):
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= limit else s[:limit] + '…（截断，原长度 %d）' % len(s)


def merge_op_settings(overrides):
    """合并用户传入的操作模式设置为默认值。"""
    s = json.loads(json.dumps(DEFAULT_OP_SETTINGS))
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k == 'tools' and isinstance(v, dict):
                s['tools'].update(v)
            elif v is not None:
                s[k] = v
    return s


def check_server(timeout=4):
    try:
        r = requests.get(SERVER_BASE + '/', timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def check_agent(timeout=4):
    try:
        r = requests.get(AGENT_BASE + '/health', timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


# =============================================================================
# 对话模式（chat）
# =============================================================================

# headless 侧的对话会话：session_id -> {'last_node_id', 'created', 'updated'}
chat_sessions = {}
chat_sessions_lock = threading.Lock()


def _chat_session_touch(sid, node_id=None):
    if not sid:
        return
    with chat_sessions_lock:
        s = chat_sessions.setdefault(sid, {'last_node_id': None,
                                           'created': time.time()})
        if node_id:
            s['last_node_id'] = node_id
        s['updated'] = time.time()


def _chat_session_get(sid):
    with chat_sessions_lock:
        s = chat_sessions.get(sid)
        return dict(s) if s else None


def _chat_poll_stream(node_id, timeout):
    """轮询对话节点直到生成完成。

    生成器事件（dict）：
      {'type':'reasoning','content':增量} / {'type':'delta','content':增量}
      / {'type':'done','node':节点} / {'type':'error','message':...}
    """
    deadline = time.time() + max(10.0, float(timeout or DEFAULT_CHAT_WAIT))
    last_reason = ''
    last_ans = ''
    while True:
        node = None
        try:
            r = requests.get(SERVER_BASE + '/api/node/' + node_id, timeout=15)
            if r.status_code == 200:
                node = _json_safe(r)
        except Exception as e:
            log.warning('查询节点 %s 失败: %s', node_id, e)
        if node and isinstance(node, dict) and not node.get('error'):
            reason = node.get('reasoning') or ''
            ans = node.get('assistantMessage') or ''
            if reason != last_reason:
                delta = reason[len(last_reason):] if reason.startswith(last_reason) else reason
                last_reason = reason
                if delta:
                    yield {'type': 'reasoning', 'content': delta}
            if ans != last_ans:
                delta = ans[len(last_ans):] if ans.startswith(last_ans) else ans
                last_ans = ans
                if delta:
                    yield {'type': 'delta', 'content': delta}
            if not node.get('generating'):
                yield {'type': 'done', 'node': node}
                return
        if time.time() > deadline:
            yield {'type': 'error', 'message': '等待生成超时（%.0f 秒）' % timeout,
                   'node_id': node_id}
            return
        time.sleep(0.4)


def chat_send(body, stream=False):
    """处理一次对话模式请求。返回 (payload, status_code) 或流式生成器。"""
    message = body.get('message')
    if not isinstance(message, str) or not message.strip():
        return {'ok': False, 'error': '缺少 message（字符串）'}, 400
    model = body.get('model') or 'deepseek-v4-flash'
    r_eff = body.get('reasoning_effort') or 'max'
    thinking = bool(body.get('thinking', True))
    files = body.get('files') if isinstance(body.get('files'), list) else []
    wait = bool(body.get('wait', True))
    timeout = float(body.get('timeout') or DEFAULT_CHAT_WAIT)

    parent_id = body.get('parent_id') or None
    hsess = body.get('session_id') or None
    if not parent_id and hsess:
        s = _chat_session_get(hsess)
        if s:
            parent_id = s.get('last_node_id')
    if not hsess:
        hsess = 'hl-chat-' + uuid.uuid4().hex[:10]

    payload = {
        'mode': 'chat', 'model': model, 'reasoning_effort': r_eff,
        'thinking_enabled': thinking,
        'tree_node_id': parent_id, 'parent_id': parent_id,
        'user_input': message, 'files': files,
    }
    try:
        r = requests.post(SERVER_BASE + '/chat', json=payload, timeout=60)
    except Exception as e:
        return {'ok': False, 'error': '无法连接 DeepChat server: %s' % e}, 502
    if r.status_code != 200:
        return {'ok': False, 'error': 'server 返回 HTTP %s' % r.status_code,
                'detail': _clip(r.text, 500)}, 502
    data = _json_safe(r) or {}
    node_id = data.get('nodeId')
    if not node_id:
        return {'ok': False, 'error': '创建节点失败', 'detail': _clip(r.text, 500)}, 502
    _chat_session_touch(hsess, node_id)

    if stream:
        def gen():
            yield json.dumps({'type': 'node', 'node_id': node_id,
                              'session_id': hsess, 'parent_id': parent_id},
                             ensure_ascii=False) + '\n'
            final_node = None
            for ev in _chat_poll_stream(node_id, timeout):
                if ev['type'] == 'done':
                    final_node = ev['node']
                yield json.dumps(ev, ensure_ascii=False) + '\n'
            if final_node:
                _chat_session_touch(hsess, node_id)
                yield json.dumps({'type': 'result',
                                  'node_id': node_id, 'session_id': hsess,
                                  'reply': final_node.get('assistantMessage') or '',
                                  'reasoning': final_node.get('reasoning') or '',
                                  'title': final_node.get('title') or '',
                                  'usage': final_node.get('usage') or {}},
                                 ensure_ascii=False) + '\n'
        return Response(gen(), mimetype='application/x-ndjson')

    if not wait:
        return {'ok': True, 'node_id': node_id, 'session_id': hsess,
                'parent_id': parent_id, 'status': 'generating'}, 200

    final_node = None
    for ev in _chat_poll_stream(node_id, timeout):
        if ev['type'] == 'done':
            final_node = ev['node']
        elif ev['type'] == 'error':
            return {'ok': False, 'error': ev.get('message'), 'node_id': node_id,
                    'session_id': hsess}, 504
    if not final_node:
        return {'ok': False, 'error': '未获取到生成结果', 'node_id': node_id,
                'session_id': hsess}, 504
    _chat_session_touch(hsess, node_id)
    return {'ok': True, 'node_id': node_id, 'session_id': hsess,
            'parent_id': parent_id, 'status': 'answered',
            'reply': final_node.get('assistantMessage') or '',
            'reasoning': final_node.get('reasoning') or '',
            'reasoning_duration': final_node.get('reasoningDuration'),
            'title': final_node.get('title') or '',
            'usage': final_node.get('usage') or {}}, 200


# =============================================================================
# 操作模式（operation）
# =============================================================================

class OperationSession(object):
    """一条操作模式会话。

    生命周期：
      1. start()            建立 server NDJSON 事件流（父/子 agent 事件共用该流）；
      2. reader 线程        解析事件：转发命令到 agent、回传结果、累积答案；
      3. poller 线程        轮询 #timer / #exec --async 任务并回传 timer_notify；
      4. send_message()     经 /api/operation_input 追加用户消息（同一会话多轮）；
      5. cancel()/close()   终止会话（服务端随之清理）。
    """

    def __init__(self, model, r_eff, thinking, settings):
        self.id = 'hl-op-' + uuid.uuid4().hex[:10]
        self.model = model
        self.r_eff = r_eff
        self.thinking = thinking
        self.settings = settings

        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)

        self.server_sid = None          # server 侧会话 ID（stream 首个事件给出）
        self.closed = False             # 事件流是否已断开/结束
        self.cancelled = False          # 是否收到取消事件（服务端确认）
        self.cancel_requested = False   # 是否调用过 cancel 接口
        self.last_error = None

        self.turn_done = 0              # 已完成的“轮次”数（final_answer 次数）
        self.expected_turn = 1          # 期待完成的轮次（用于 busy 判定）
        self.answers = []               # 每轮最终答复
        self.round_thinkings = []       # 每轮思考（final_answer 时归档）
        self.round_thinking = ''        # 当前轮思考
        self.round_answer = ''          # 当前轮正文
        self.last_usage = {}
        self.last_usage_ts = 0.0
        self.last_final_ts = 0.0

        self.commands = {}              # task_id -> 命令记录
        self.tasks = {}                 # agent 侧 task_id -> 轮询登记
        self.events = []                # 全部事件（带 seq）
        self._ev_seq = 0

        self.stream_resp = None
        self._stop_flag = threading.Event()
        self._last_event_ts = time.time()

    # ---------------- 事件流 ----------------

    def start(self, first_message):
        """建立事件流并发送第一条消息（经 /chat mode=operation）。"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": first_message},
        ]
        payload = {
            "mode": "operation", "messages": messages,
            "model": self.model, "reasoning_effort": self.r_eff,
            "thinking_enabled": self.thinking, "settings": self.settings,
        }
        self.reader = threading.Thread(target=self._read_stream, args=(payload,),
                                       name='hl-reader-' + self.id, daemon=True)
        self.reader.start()
        self.poller = threading.Thread(target=self._poller,
                                       name='hl-poller-' + self.id, daemon=True)
        self.poller.start()

    def _read_stream(self, payload):
        try:
            resp = requests.post(SERVER_BASE + '/chat', json=payload,
                                 stream=True, timeout=(15, 90))
            self.stream_resp = resp
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if resp.status_code != 200:
                self._set_error('server 返回 HTTP %s: %s'
                                % (resp.status_code, _clip(resp.text, 500)))
                return
            if 'application/json' in ctype:
                data = _json_safe(resp) or {}
                self._handle_event(dict(data, type=data.get('type') or 'json_response'))
                return
            buf = b''
            for chunk in resp.iter_content(chunk_size=8192):
                if self._stop_flag.is_set():
                    break
                if not chunk:
                    continue
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    self._handle_line(line)
            if buf.strip():
                self._handle_line(buf)
        except Exception as e:
            if not self._stop_flag.is_set():
                self._set_error('事件流异常: %s' % e)
        finally:
            self._mark_closed()

    def _handle_line(self, raw):
        try:
            s = raw.decode('utf-8', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            return
        s = s.strip()
        if not s:
            return
        try:
            ev = json.loads(s)
        except Exception:
            self._handle_event({'type': 'raw', 'raw': _clip(s, 2000)})
            return
        if isinstance(ev, dict):
            self._handle_event(ev)

    def _handle_event(self, ev):
        t = ev.get('type')
        spawn_cmd = None
        with self.cond:
            self._append_event(ev)
            self._last_event_ts = time.time()
            if t == 'session':
                sid = ev.get('session_id')
                if sid:
                    self.server_sid = sid
            elif t == 'command_execute':
                if not ev.get('server_side'):
                    spawn_cmd = dict(ev)
            elif t == 'command_result':
                self._update_command(ev)
            elif t == 'thinking':
                self.round_thinking += ev.get('content') or ''
            elif t == 'content_delta':
                self.round_answer += ev.get('content') or ''
            elif t == 'final_answer':
                content = ev.get('content') or ''
                self.answers.append(content)
                self.round_thinkings.append(self.round_thinking)
                self.round_thinking = ''
                self.round_answer = ''
                self.turn_done += 1
                self.last_final_ts = time.time()
                self.cond.notify_all()
            elif t == 'usage':
                u = ev.get('usage')
                if isinstance(u, dict):
                    self.last_usage = u
                self.last_usage_ts = time.time()
                self.cond.notify_all()
            elif t == 'cancelled':
                self.cancelled = True
                self.cond.notify_all()
            elif t == 'error':
                self.last_error = ev.get('content') or '未知错误'
                self.cond.notify_all()
            elif t == 'reset_container':
                self.round_thinking = ''
                self.round_answer = ''
            elif t == 'heartbeat':
                pass
        if spawn_cmd is not None:
            th = threading.Thread(target=self._exec_command, args=(spawn_cmd,),
                                  name='hl-cmd-' + self.id, daemon=True)
            th.start()

    def _append_event(self, ev):
        """把事件追加到记录（调用方须持锁）。字符串字段超长时截断保存。"""
        self._ev_seq += 1
        rec = {'seq': self._ev_seq, 'ts': time.time()}
        for k, v in ev.items():
            if k in ('seq', 'ts'):
                continue
            if isinstance(v, str) and len(v) > EVENT_STR_LIMIT:
                v = v[:EVENT_STR_LIMIT] + '…（已截断，原长度 %d）' % len(v)
            rec[k] = v
        self.events.append(rec)
        if len(self.events) > EVENTS_SOFT_MAX:
            del self.events[:EVENTS_SOFT_MAX // 5]

    def _set_error(self, msg):
        with self.cond:
            self.last_error = msg
            self.cond.notify_all()

    def _mark_closed(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    # ---------------- 命令转发（浏览器逻辑的 Python 版） ----------------

    def _wait_server_sid(self, timeout=10):
        deadline = time.time() + timeout
        with self.cond:
            while not self.server_sid and not self.closed and time.time() < deadline:
                self.cond.wait(0.2)
            return self.server_sid

    def _exec_command(self, ev):
        """执行一条命令：转发 agent /execute → 回传 server /api/command_result。"""
        task_id = ev.get('task_id') or ''
        command = ev.get('command') or ''
        agent_id = ev.get('agent_id')
        is_child = bool(agent_id and agent_id != 'main')
        output, rc = '', -1
        try:
            body = {'command': command}
            if not is_child:
                # 主流程携带设置项（与前端一致）
                body['timeout'] = int(self.settings.get('max_timeout') or 600)
                body['idle_timeout'] = int(self.settings.get('idle_timeout') or 30)
                body['truncate'] = int(self.settings.get('process_truncate') or 4000)
            r = requests.post(AGENT_BASE + '/execute', json=body, timeout=(10, 660))
            if r.status_code == 200:
                j = _json_safe(r) or {}
                output = j.get('output') or ''
                rc = j.get('returncode', -1)
                if rc is None:
                    rc = -1
            else:
                output = '代理返回错误: %s' % r.status_code
        except Exception as e:
            output = '无法连接本地代理: %s' % e

        # 注册定时/异步任务（与前端一致：从输出中提取任务 ID）
        try:
            if command.startswith('#timer') or re.match(r'^#exec\s+--async\b', command):
                m = re.search(r'ID[:：]\s*(\S+)', output)
                if m:
                    self._register_task(m.group(1),
                                        'timer' if command.startswith('#timer') else 'async',
                                        agent_id if is_child else None)
        except Exception as e:
            log.warning('注册任务失败: %s', e)

        # 回传 server
        sid = self._wait_server_sid()
        if sid:
            payload = {'session_id': sid, 'task_id': task_id,
                       'output': output, 'returncode': rc}
            if is_child:
                payload['agent_id'] = agent_id
            try:
                requests.post(SERVER_BASE + '/api/command_result', json=payload, timeout=30)
            except Exception as e:
                log.warning('回传命令结果失败: %s', e)
        # 本地记录
        with self.cond:
            rec = self.commands.get(task_id) or {'task_id': task_id}
            rec.update({'task_id': task_id, 'command': command,
                        'command_title': ev.get('command_title') or '',
                        'output': output, 'returncode': rc,
                        'server_side': False, 'agent_id': agent_id,
                        'ts': time.time()})
            self.commands[task_id] = rec

    def _update_command(self, ev):
        """server 回显的 command_result 事件 → 更新本地命令记录（调用方须持锁）。"""
        task_id = ev.get('task_id') or ''
        if not task_id:
            return
        rec = self.commands.get(task_id) or {'task_id': task_id}
        rec.update({'task_id': task_id,
                    'command': ev.get('command') or rec.get('command') or '',
                    'command_title': ev.get('command_title') or rec.get('command_title') or '',
                    'output': ev.get('output') or '',
                    'returncode': ev.get('returncode', -1),
                    'server_side': bool(ev.get('server_side')) or rec.get('server_side', False),
                    'agent_id': ev.get('agent_id') or rec.get('agent_id'),
                    'ts': time.time()})
        self.commands[task_id] = rec

    # ---------------- 定时/异步任务轮询 ----------------

    def _register_task(self, tid, ttype, agent_id):
        with self.cond:
            t = self.tasks.get(tid)
            if t is None:
                self.tasks[tid] = {
                    'task_id': tid, 'type': ttype, 'agent_id': agent_id,
                    'status': 'running', 'output': '', 'returncode': None,
                    'last_output': '', 'last_status': 'running',
                    'registered_ts': time.time(),
                }
            else:
                t['type'] = ttype
                if agent_id:
                    t['agent_id'] = agent_id

    def _poller(self):
        while True:
            interval = max(1, int(self.settings.get('process_refresh') or 5))
            with self.cond:
                if self.closed or self._stop_flag.is_set():
                    return
            time.sleep(interval)
            with self.cond:
                if self.closed or self._stop_flag.is_set():
                    return
                tasks = list(self.tasks.values())
            for t in tasks:
                try:
                    r = requests.get(AGENT_BASE + '/task/' + t['task_id'], timeout=10)
                    if r.status_code != 200:
                        if r.status_code == 404:
                            with self.cond:
                                self.tasks.pop(t['task_id'], None)
                        continue
                    st = _json_safe(r) or {}
                    new_out = st.get('output') or ''
                    new_status = st.get('status') or ''
                    changed = (new_out != t.get('last_output')
                               or new_status != t.get('last_status'))
                    if changed:
                        # 仅定时任务（#timer）向模型推送通知；异步任务只更新本地记录
                        if t.get('type') == 'timer' and self.server_sid:
                            try:
                                requests.post(SERVER_BASE + '/api/timer_notify', json={
                                    'session_id': self.server_sid,
                                    'agent_id': t.get('agent_id') or None,
                                    'task_id': t['task_id'],
                                    'output': new_out,
                                    'status': new_status,
                                }, timeout=30)
                            except Exception as e:
                                log.warning('回传 timer_notify 失败: %s', e)
                        with self.cond:
                            t['last_output'] = new_out
                            t['last_status'] = new_status
                            t['output'] = new_out
                            if st.get('returncode') is not None:
                                t['returncode'] = st.get('returncode')
                            self._append_event({'type': 'task_update',
                                                'task_id': t['task_id'],
                                                'task_type': t.get('type'),
                                                'status': new_status,
                                                'output': new_out})
                    if new_status and new_status != 'running':
                        with self.cond:
                            self.tasks.pop(t['task_id'], None)
                except Exception:
                    # 网络类错误：保留任务，下轮重试
                    continue

    # ---------------- 对外操作 ----------------

    def send_message(self, message, settings=None):
        """向本会话追加用户消息（经 /api/operation_input）。"""
        sid = self.server_sid
        if not sid:
            sid = self._wait_server_sid(10)
        if not sid:
            return False, '会话尚未就绪（server 未分配 session_id）'
        payload = {'session_id': sid, 'message': message}
        if settings:
            payload['settings'] = merge_op_settings(settings)
            with self.cond:
                self.settings = payload['settings']
        try:
            r = requests.post(SERVER_BASE + '/api/operation_input', json=payload, timeout=30)
        except Exception as e:
            return False, '无法连接 server: %s' % e
        if r.status_code != 200:
            return False, 'server 返回 HTTP %s: %s' % (r.status_code, _clip(r.text, 300))
        with self.cond:
            self.expected_turn = self.turn_done + 1
        return True, None

    def cancel(self):
        """取消（终止）本会话：服务端会结束循环、关闭事件流。"""
        with self.cond:
            self.cancel_requested = True
        sid = self.server_sid
        if sid:
            try:
                requests.post(SERVER_BASE + '/api/cancel_operation',
                              json={'session_id': sid}, timeout=30)
            except Exception as e:
                log.warning('cancel_operation 请求失败: %s', e)
        return True

    def close(self, do_cancel=True, remove=False):
        """关闭会话：取消 + 断开事件流 + 停止后台线程。

        remove=True 时同时从会话注册表移除（彻底释放）。
        """
        if do_cancel and not self.closed:
            self.cancel()
        self._stop_flag.set()
        try:
            if self.stream_resp is not None:
                self.stream_resp.close()
        except Exception:
            pass
        # 等 reader 收尾（最多 5 秒）
        self._mark_closed()
        if remove:
            with op_sessions_lock:
                op_sessions.pop(self.id, None)
        return True

    def wait_response(self, target_turn, timeout):
        """等待第 target_turn 轮 final_answer 完成，返回结果 payload。"""
        deadline = time.time() + max(1.0, float(timeout or DEFAULT_OPERATE_WAIT))
        with self.cond:
            while self.turn_done < target_turn:
                if self.cancelled:
                    return self._payload('cancelled')
                if self.closed:
                    return self._payload('closed')
                rem = deadline - time.time()
                if rem <= 0:
                    return self._payload('timeout')
                self.cond.wait(min(rem, 0.5))
            # final_answer 之后 server 还会补发 usage 事件：最多等 0.8 秒让返回信息完整
            if self.last_usage_ts < self.last_final_ts and not self.closed:
                self.cond.wait(0.8)
            return self._payload('answered')

    def _payload(self, status):
        """组装等待结果（调用方须持锁）。"""
        return {
            'ok': status == 'answered',
            'status': status,
            'session_id': self.id,
            'server_session_id': self.server_sid,
            'turn_done': self.turn_done,
            'answer': self.answers[-1] if self.answers else None,
            'thinking': self.round_thinkings[-1] if self.round_thinkings else None,
            'commands': [self._cmd_view(c) for c in self.commands.values()],
            'usage': dict(self.last_usage),
            'closed': self.closed,
            'cancelled': self.cancelled,
            'cancel_requested': self.cancel_requested,
            'last_error': self.last_error,
        }

    def _cmd_view(self, c):
        return {
            'task_id': c.get('task_id'),
            'command': c.get('command'),
            'command_title': c.get('command_title'),
            'output': c.get('output'),
            'returncode': c.get('returncode'),
            'server_side': c.get('server_side', False),
            'agent_id': c.get('agent_id'),
        }

    def snapshot(self):
        """会话状态快照。"""
        with self.cond:
            busy = (not self.closed) and (self.turn_done < self.expected_turn)
            return {
                'session_id': self.id,
                'server_session_id': self.server_sid,
                'closed': self.closed,
                'cancelled': self.cancelled,
                'cancel_requested': self.cancel_requested,
                'busy': busy,
                'turn_done': self.turn_done,
                'expected_turn': self.expected_turn,
                'answers': list(self.answers),
                'last_answer': self.answers[-1] if self.answers else None,
                'round_thinking': self.round_thinking,
                'last_thinking': self.round_thinkings[-1] if self.round_thinkings else None,
                'usage': dict(self.last_usage),
                'last_error': self.last_error,
                'commands': [self._cmd_view(c) for c in self.commands.values()],
                'tasks': [dict(t) for t in self.tasks.values()],
                'events_count': self._ev_seq,
                'last_event_ts': self._last_event_ts,
            }

    def events_after(self, after_seq, limit=1000):
        with self.cond:
            out = [e for e in self.events if e.get('seq', 0) > after_seq]
            if limit and len(out) > limit:
                out = out[:limit]
            return out, self._ev_seq


# 会话注册表
op_sessions = {}
op_sessions_lock = threading.Lock()


def get_session(sid):
    with op_sessions_lock:
        return op_sessions.get(sid)


def create_operate_session(body):
    """处理 POST /api/operate / .../message 的公共逻辑。"""
    message = body.get('message')
    if not isinstance(message, str) or not message.strip():
        return {'ok': False, 'error': '缺少 message（字符串）'}, 400
    model = body.get('model') or 'deepseek-v4-flash'
    r_eff = body.get('reasoning_effort') or 'max'
    thinking = bool(body.get('thinking', True))
    settings = merge_op_settings(body.get('settings'))
    wait = bool(body.get('wait', True))
    timeout = float(body.get('timeout') or DEFAULT_OPERATE_WAIT)
    require_agent = bool(body.get('require_agent', True))

    if not check_server():
        return {'ok': False, 'error': 'DeepChat server 未连接（%s）' % SERVER_BASE}, 503
    if require_agent and not check_agent():
        return {'ok': False,
                'error': '本地 DeepChat agent 未连接（%s）；操作模式需要 agent 在线，'
                         '可传 "require_agent": false 跳过检查' % AGENT_BASE}, 503

    s = OperationSession(model, r_eff, thinking, settings)
    with op_sessions_lock:
        op_sessions[s.id] = s
    s.start(message)
    # 等待 server 分配 session（最多 15 秒）
    s._wait_server_sid(15)
    if not wait:
        return {'ok': True, 'status': 'running', **s.snapshot()}, 200
    res = s.wait_response(1, timeout)
    return res, 200


def operate_message(sid, body):
    s = get_session(sid)
    if not s:
        return {'ok': False, 'error': '会话不存在: %s' % sid}, 404
    message = body.get('message')
    if not isinstance(message, str) or not message.strip():
        return {'ok': False, 'error': '缺少 message（字符串）'}, 400
    wait = bool(body.get('wait', True))
    timeout = float(body.get('timeout') or DEFAULT_OPERATE_WAIT)

    with s.cond:
        target = s.turn_done + 1
    ok, err = s.send_message(message, body.get('settings'))
    if not ok:
        return {'ok': False, 'error': err}, 502
    if not wait:
        return {'ok': True, 'status': 'running', **s.snapshot()}, 200
    return s.wait_response(target, timeout), 200


# =============================================================================
# HTTP 路由
# =============================================================================


@app.after_request
def _add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp


@app.route('/')
def index():
    return jsonify({
        'name': 'DeepChat Headless API',
        'server': SERVER_BASE,
        'agent': AGENT_BASE,
        'endpoints': {
            'health': 'GET /health',
            'chat': 'POST /api/chat',
            'chat_node': 'GET /api/chat/node/<node_id>',
            'chat_tree': 'GET /api/chat/tree',
            'operate': 'POST /api/operate',
            'operate_message': 'POST /api/operate/<session_id>/message',
            'operate_cancel': 'POST /api/operate/<session_id>/cancel',
            'operate_close': 'POST /api/operate/<session_id>/close',
            'operate_status': 'GET /api/operate/<session_id>/status',
            'operate_events': 'GET /api/operate/<session_id>/events?after=<seq>',
            'sessions': 'GET /api/sessions',
        },
    })


@app.route('/health')
def health():
    server_ok = check_server()
    agent_ok = check_agent()
    with op_sessions_lock:
        sid_list = list(op_sessions.keys())
    return jsonify({
        'status': 'ok' if server_ok else 'degraded',
        'server': {'url': SERVER_BASE, 'connected': server_ok},
        'agent': {'url': AGENT_BASE, 'connected': agent_ok},
        'sessions': sid_list,
    })


@app.route('/api/chat', methods=['POST'])
def api_chat():
    body = _body()
    try:
        stream = bool(body.get('stream', False))
        result = chat_send(body, stream=stream)
        if stream and not isinstance(result, tuple):
            return result
        payload, code = result
        return jsonify(payload), code
    except Exception as e:
        log.exception('chat 异常')
        return jsonify({'ok': False, 'error': '内部错误: %s' % e}), 500


@app.route('/api/chat/node/<node_id>')
def api_chat_node(node_id):
    try:
        r = requests.get(SERVER_BASE + '/api/node/' + node_id, timeout=15)
        return (r.content, r.status_code, {'Content-Type': 'application/json; charset=utf-8'})
    except Exception as e:
        return jsonify({'error': '无法连接 server: %s' % e}), 502


@app.route('/api/chat/tree')
def api_chat_tree():
    try:
        r = requests.get(SERVER_BASE + '/api/tree', timeout=15)
        return (r.content, r.status_code, {'Content-Type': 'application/json; charset=utf-8'})
    except Exception as e:
        return jsonify({'error': '无法连接 server: %s' % e}), 502


@app.route('/api/operate', methods=['POST'])
def api_operate():
    try:
        payload, code = create_operate_session(_body())
        return jsonify(payload), code
    except Exception as e:
        log.exception('operate 异常')
        return jsonify({'ok': False, 'error': '内部错误: %s' % e}), 500


@app.route('/api/operate/<sid>/message', methods=['POST'])
def api_operate_message(sid):
    try:
        payload, code = operate_message(sid, _body())
        return jsonify(payload), code
    except Exception as e:
        log.exception('operate message 异常')
        return jsonify({'ok': False, 'error': '内部错误: %s' % e}), 500


@app.route('/api/operate/<sid>/cancel', methods=['POST'])
def api_operate_cancel(sid):
    s = get_session(sid)
    if not s:
        return jsonify({'ok': False, 'error': '会话不存在: %s' % sid}), 404
    s.close(do_cancel=True, remove=False)
    return jsonify({'ok': True, 'session_id': sid, 'status': 'cancelled'}), 200


@app.route('/api/operate/<sid>/close', methods=['POST'])
def api_operate_close(sid):
    s = get_session(sid)
    if not s:
        return jsonify({'ok': False, 'error': '会话不存在: %s' % sid}), 404
    s.close(do_cancel=True, remove=True)
    return jsonify({'ok': True, 'session_id': sid, 'status': 'closed'}), 200


@app.route('/api/operate/<sid>/status')
def api_operate_status(sid):
    s = get_session(sid)
    if not s:
        return jsonify({'ok': False, 'error': '会话不存在: %s' % sid}), 404
    return jsonify({'ok': True, **s.snapshot()}), 200


@app.route('/api/operate/<sid>/events')
def api_operate_events(sid):
    s = get_session(sid)
    if not s:
        return jsonify({'ok': False, 'error': '会话不存在: %s' % sid}), 404
    try:
        after = int(request.args.get('after', '0') or 0)
    except Exception:
        after = 0
    try:
        limit = int(request.args.get('limit', '1000') or 1000)
    except Exception:
        limit = 1000
    events, last_seq = s.events_after(after, limit)
    return jsonify({'ok': True, 'session_id': sid, 'events': events,
                    'last_seq': last_seq}), 200


@app.route('/api/sessions')
def api_sessions():
    with op_sessions_lock:
        items = []
        for sid, s in op_sessions.items():
            items.append({'session_id': sid,
                          'server_session_id': s.server_sid,
                          'closed': s.closed,
                          'cancelled': s.cancelled,
                          'turn_done': s.turn_done})
    return jsonify({'ok': True, 'sessions': items}), 200


# =============================================================================
# 主入口
# =============================================================================

def _is_port_listening(host, port, timeout=0.6):
    """检测 host:port 上是否已有服务在监听。"""
    import socket as _socket
    target = '127.0.0.1' if host in ('0.0.0.0', '') else host
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((target, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


if __name__ == '__main__':
    print('=' * 66)
    print('  DeepChat Headless API')
    print('  server : %s' % SERVER_BASE)
    print('  agent  : %s' % AGENT_BASE)
    print('  listen : http://%s:%d' % (HOST, PORT))
    print('=' * 66)
    if not check_server():
        print('[WARN] DeepChat server 当前不可达，请确认已启动（server/run.bat 或 app.exe）')
    if not check_agent():
        print('[WARN] DeepChat agent 当前不可达，操作模式将不可用（client/run.bat 或 agent.exe）')
    if _is_port_listening(HOST, PORT):
        print('[ERROR] 端口 %s:%d 已被占用（可能已有一个 headless_api 实例在运行）。' % (HOST, PORT))
        print('        请先停止旧实例，或改用其他端口：set HEADLESS_PORT=5200 && python headless_api.py')
        sys.exit(1)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
