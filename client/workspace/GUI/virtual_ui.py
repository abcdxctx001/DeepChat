# -*- coding: utf-8 -*-
"""
virtual_ui.py — 虚拟桌面 + 屏幕 OCR + 模拟键盘鼠标（后台操作，不抢占用户输入）

核心设计
========
1. 虚拟桌面：在 WinSta0 下用 CreateDesktopEx 创建隐藏桌面（如 "AgentDesk"）。
   目标程序通过 STARTUPINFO.lpDesktop 启动到该桌面 —— 用户在真实桌面上完全无感知，
   不抢占鼠标光标与键盘焦点。
2. 输入注入：专用工作线程 SetThreadDesktop 到虚拟桌面后，向目标窗口
   PostMessage 发送 WM_KEYDOWN/WM_CHAR/WM_LBUTTONDOWN/WM_MOUSEMOVE 等消息。
   支持单击/双击/右键/滚轮/拖拽/单键/热键组合/文本输入。
3. 屏幕截图：隐藏桌面的窗口没有显示表面，GDI 直截（PrintWindow/GetWindowDC）
   拿不到完整内容。因此把 capture_dll/capture_helper.dll 通过
   CreateRemoteThread + LoadLibraryA 注入目标进程，在进程内
   SendMessageTimeout(WM_PRINT) 让窗口把内容画到本地 HDC（离屏绘制），
   保存 BMP 后再读取。对不响应 WM_PRINT 的窗口自动回退 PrintWindow /
   GetWindowDC。
4. OCR：优先 winsdk（Windows.Media.Ocr，中文/英文），不可用时回退
   ocr.ps1（PowerShell + WinRT）。
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# Win32 绑定（完整 argtypes/restype，避免 64 位句柄截断）
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HANDLE = wt.HANDLE

user32.CreateDesktopExW.restype = HANDLE
user32.CreateDesktopExW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
user32.CloseDesktop.argtypes = [HANDLE]
user32.CloseDesktop.restype = wt.BOOL
user32.OpenDesktopW.restype = HANDLE
user32.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
user32.SetThreadDesktop.argtypes = [HANDLE]
user32.SetThreadDesktop.restype = wt.BOOL
user32.GetThreadDesktop.restype = HANDLE
user32.GetThreadDesktop.argtypes = [wt.DWORD]
user32.EnumWindows.argtypes = [ctypes.c_void_p, LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.EnumChildWindows.argtypes = [wt.HWND, ctypes.c_void_p, LPARAM]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.SendMessageW.restype = LRESULT
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = HANDLE
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = HANDLE
user32.ReleaseDC.argtypes = [wt.HWND, HANDLE]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, HANDLE, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.FillRect.argtypes = [HANDLE, ctypes.POINTER(wt.RECT), HANDLE]
user32.FillRect.restype = ctypes.c_int
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wt.HWND]
user32.UpdateWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND

# ★ 2026-10-13（drag 修复需要）：子窗口定位与坐标换算、鼠标捕获
try:
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ClientToScreen.restype = wt.BOOL
    user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ScreenToClient.restype = wt.BOOL
    user32.SetCapture.argtypes = [wt.HWND]
    user32.SetCapture.restype = wt.HWND
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = wt.BOOL
except Exception:
    pass
user32.SetActiveWindow.argtypes = [wt.HWND]
user32.SetActiveWindow.restype = wt.HWND
user32.GetUserObjectInformationW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
user32.GetUserObjectInformationW.restype = wt.BOOL
user32.GetProcessWindowStation.restype = HANDLE
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]

kernel32.CreateProcessW.restype = wt.BOOL
kernel32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetCurrentProcessId.restype = wt.DWORD
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.TerminateProcess.argtypes = [HANDLE, wt.UINT]
kernel32.TerminateProcess.restype = wt.BOOL
kernel32.WaitForSingleObject.argtypes = [HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CreateRemoteThread.restype = HANDLE
kernel32.CreateRemoteThread.argtypes = [HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
kernel32.GetModuleHandleA.restype = wt.HMODULE
kernel32.GetModuleHandleA.argtypes = [ctypes.c_char_p]
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]

gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateCompatibleDC.restype = HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.SelectObject.restype = HANDLE
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [HANDLE]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.BitBlt.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HANDLE, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [HANDLE, HANDLE, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = HANDLE

# 消息常量
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_CLOSE = 0x0010

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_SHIFT = 0x0004
MK_CONTROL = 0x0008
MK_MBUTTON = 0x0010

PROCESS_ALL_ACCESS = 0x001F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _now():
    return time.strftime('%H:%M:%S')


def _log(msg):
    print(f"[virtual_ui {_now()}] {msg}", flush=True)


# ★ 2026-10-13：SendInput 所需的结构（供 hotkey 等"系统级组合键注入"使用）
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

try:
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wt.UINT
except Exception:
    pass


def _make_key_input(vk, up=False, extended=False):
    """构造一条键盘 INPUT。"""
    flags = 0
    if up:
        flags |= KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(int(vk), 0, flags, 0, None)
    return inp


class VirtualUI:
    """虚拟桌面管理器：单工作线程串行执行虚拟桌面相关操作。"""

    DESKTOP_NAME = 'AgentDesk'
    DEFAULT_SCREENSHOT_DIR = None  # 由 set_paths 设置

    def __init__(self, workspace_dir, capture_dll_path, ocr_ps1_path):
        self.workspace = workspace_dir
        self.capture_dll = capture_dll_path
        self.ocr_ps1 = ocr_ps1_path
        self.capture_dir = os.path.join(workspace_dir, 'capture')
        self.screenshot_dir = os.path.join(workspace_dir, 'screenshots')
        os.makedirs(self.capture_dir, exist_ok=True)
        os.makedirs(self.screenshot_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._queue = []
        self._cond = threading.Condition(self._req_lock)
        self._result = {}
        self._desk_handle = None
        self._desk_attached = False
        self._original_desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())  # 真实桌面（主线程）
        self._launched = []          # [(pid, hProcess)]
        self._target_hwnd = None
        self._real_target_hwnd = None  # 真实桌面模式的目标窗口
        self._injected_pids = set()
        self._worker = threading.Thread(target=self._run_worker, daemon=True, name='vdesk-worker')
        self._worker.start()
        self._log_file = os.path.join(self.capture_dir, 'vui.log')

    # ---------------- 工作线程 ----------------
    def _run_worker(self):
        # 工作线程常驻：进入虚拟桌面上下文（若桌面已创建）
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                job = self._queue.pop(0)
            fn = job['fn']
            try:
                # 根据任务类型切换工作线程桌面上下文
                if job.get('real'):
                    self._in_real_desktop()
                else:
                    self._in_desktop()
                r = fn()
                self._result[job['id']] = {'ok': True, 'result': r}
            except Exception as e:
                _log(f"工作线程任务异常: {e}\n{traceback.format_exc()}")
                self._result[job['id']] = {'ok': False, 'error': str(e)}

    def _submit(self, fn, timeout=60, real=False):
        with self._lock:
            job_id = int(time.time() * 1000)
            self._result.pop(job_id, None)
            with self._cond:
                self._queue.append({'id': job_id, 'fn': fn, 'real': real})
                self._cond.notify()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if job_id in self._result:
                    item = self._result.pop(job_id)
                    if item['ok']:
                        return item['result']
                    return {'error': item.get('error', '未知错误')}
            time.sleep(0.02)
        raise TimeoutError('虚拟桌面操作超时')

    def _in_desktop(self):
        """工作线程切换到虚拟桌面（仅首次 SetThreadDesktop，避免反复切换导致
        窗口枚举/消息投递不稳定）。"""
        if self._desk_handle and not self._desk_attached:
            h = user32.SetThreadDesktop(self._desk_handle)
            if not h:
                err = ctypes.get_last_error()
                _log(f"SetThreadDesktop 失败 err={err}")
                return False
            self._desk_attached = True
            cur = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
            buf = ctypes.create_unicode_buffer(64)
            user32.GetUserObjectInformationW(cur, 2, buf, 64, None)
            _log(f"工作线程桌面已绑定: {buf.value}")
        return True

    def _in_real_desktop(self):
        """工作线程切回真实桌面（用户当前桌面）。"""
        if self._desk_attached:
            h = user32.SetThreadDesktop(self._original_desktop)
            if not h:
                _log(f"SetThreadDesktop(真实桌面) 失败 err={ctypes.get_last_error()}")
                return False
            self._desk_attached = False
            _log("工作线程桌面已切回真实桌面")
        return True

    # ---------------- 桌面管理 ----------------
    def create_desktop(self):
        def _do():
            if self._desk_handle:
                return {'already': True, 'name': self.DESKTOP_NAME}
            h = user32.CreateDesktopExW(self.DESKTOP_NAME, None, None, 0, 0x10000000, None, 0, None)
            if not h:
                return {'error': f'CreateDesktopEx 失败 err={ctypes.get_last_error()}'}
            self._desk_handle = h
            self._in_desktop()
            return {'created': True, 'name': self.DESKTOP_NAME}
        return self._submit(_do)

    def close_desktop(self, kill_processes=True):
        def _do():
            if kill_processes:
                for pid, hproc in list(self._launched):
                    try:
                        kernel32.TerminateProcess(hproc, 0)
                    except Exception:
                        pass
                    kernel32.CloseHandle(hproc)
                self._launched.clear()
            if self._desk_handle:
                # 先切回真实桌面，再关闭句柄
                self._in_real_desktop()
                user32.CloseDesktop(self._desk_handle)
                self._desk_handle = None
            self._desk_attached = False
            self._target_hwnd = None
            self._injected_pids.clear()
            return {'closed': True}
        return self._submit(_do)

    def get_info(self):
        def _do():
            return {
                'desktop': self.DESKTOP_NAME if self._desk_handle else None,
                'target_hwnd': self._target_hwnd,
                'launched': [p for p, _ in self._launched],
                'injected': list(self._injected_pids),
            }
        return self._submit(_do)

    # ---------------- 进程启动 ----------------
    def launch(self, command):
        """在虚拟桌面启动程序。command 为完整命令行（字符串）。"""
        def _do():
            if not self._desk_handle:
                return {'error': '虚拟桌面未创建，请先 #vdesk create'}
            class STARTUPINFO(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                            ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                            ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                            ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wt.HANDLE),
                            ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]
            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                            ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]
            si = STARTUPINFO()
            si.cb = ctypes.sizeof(STARTUPINFO)
            si.lpDesktop = self.DESKTOP_NAME
            pi = PROCESS_INFORMATION()
            cmd_buf = ctypes.create_unicode_buffer(command)
            ok = kernel32.CreateProcessW(None, cmd_buf, None, None, False,
                                         0x00000008, None, self.workspace,
                                         ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                return {'error': f'CreateProcess 失败 err={ctypes.get_last_error()}: {command}'}
            self._launched.append((pi.dwProcessId, pi.hProcess))
            kernel32.CloseHandle(pi.hThread)
            # 注入截图 DLL
            inject = self._inject_dll(pi.dwProcessId)
            return {'pid': pi.dwProcessId, 'inject': inject}
        return self._submit(_do, timeout=30)

    # ---------------- DLL 注入 ----------------
    def _inject_dll(self, pid):
        if not os.path.exists(self.capture_dll):
            return 'capture_helper.dll 不存在，截图不可用'
        if pid in self._injected_pids:
            return 'already-injected'
        hproc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not hproc:
            return f'OpenProcess 失败 err={ctypes.get_last_error()}'
        try:
            dll_bytes = self.capture_dll.encode('ascii') + b'\x00'
            mem = kernel32.VirtualAllocEx(hproc, None, len(dll_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
            if not mem:
                return f'VirtualAllocEx 失败 err={ctypes.get_last_error()}'
            written = ctypes.c_size_t()
            if not kernel32.WriteProcessMemory(hproc, mem, dll_bytes, len(dll_bytes), ctypes.byref(written)):
                return f'WriteProcessMemory 失败 err={ctypes.get_last_error()}'
            loadlib = kernel32.GetProcAddress(kernel32.GetModuleHandleA(b'kernel32.dll'), b'LoadLibraryA')
            tid = wt.DWORD()
            hthread = kernel32.CreateRemoteThread(hproc, None, 0, loadlib, mem, 0, ctypes.byref(tid))
            if not hthread:
                return f'CreateRemoteThread 失败 err={ctypes.get_last_error()}'
            kernel32.WaitForSingleObject(hthread, 8000)
            kernel32.CloseHandle(hthread)
            self._injected_pids.add(pid)
            return 'ok'
        finally:
            kernel32.CloseHandle(hproc)

    # ---------------- 截图（DLL 注入 + WM_PRINT） ----------------
    def screenshot(self, scope='window', hwnd=None, out_name=None):
        """scope: window(截目标窗口) | desktop(全虚拟桌面，尽力而为)。返回 dict。"""
        def _do():
            if not self._desk_handle:
                return {'error': '虚拟桌面未创建'}
            self._in_desktop()
            target = hwnd or self._target_hwnd
            if scope == 'window':
                if not target or not user32.IsWindow(target):
                    return {'error': '目标窗口无效，请先 #vdesk list / #vdesk target'}
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
                # 确保注入
                inj = self._inject_dll(pid.value)
                if isinstance(inj, str) and inj not in ('ok', 'already-injected'):
                    return {'error': f'DLL 注入失败: {inj}'}
                out_bmp = os.path.join(self.screenshot_dir, out_name or f'shot_{int(time.time())}.bmp')
                return self._request_capture(target, out_bmp)
            else:
                # 桌面级：尽力而为（BitBlt 虚拟桌面 DC）
                out_png = os.path.join(self.screenshot_dir, out_name or f'desk_{int(time.time())}.png')
                img = self._bitblt_desktop()
                if img is None:
                    return {'error': '虚拟桌面 BitBlt 失败（非活动桌面限制），建议使用窗口截图 --window'}
                try:
                    from PIL import Image
                    img.save(out_png)
                    return {'path': out_png, 'size': img.size, 'method': 'bitblt-desktop'}
                except Exception as e:
                    return {'error': f'保存失败: {e}'}
        return self._submit(_do, timeout=90)

    def _request_capture(self, hwnd, out_bmp):
        cmd_file = os.path.join(self.capture_dir, 'capture_cmd.txt')
        done_file = os.path.join(self.capture_dir, 'capture_done.txt')
        for p in (cmd_file, done_file):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        try:
            with open(cmd_file, 'w') as f:
                f.write(f'{int(hwnd):X}|{out_bmp}\n')
        except Exception as e:
            return {'error': f'写命令失败: {e}'}
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.exists(done_file):
                try:
                    with open(done_file, 'r') as f:
                        res = f.read().strip()
                    os.remove(done_file)
                except Exception:
                    res = 'err:read-done'
                if res.startswith('ok:'):
                    parts = res.split(':')
                    method = parts[1]
                    png = out_bmp.replace('.bmp', '.png')
                    try:
                        from PIL import Image
                        Image.open(out_bmp).save(png)
                    except Exception:
                        pass
                    return {'path': png, 'bmp': out_bmp, 'size': (int(parts[3]), int(parts[4])), 'method': method, 'note': res}
                return {'error': f'截图失败: {res}'}
            time.sleep(0.15)
        return {'error': '截图超时（DLL 未响应）'}

    def _bitblt_desktop(self):
        """尽力而为的虚拟桌面 BitBlt（多数情况下内容不全）。"""
        try:
            from PIL import Image
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            hdc_scr = user32.GetDC(None)
            if not hdc_scr:
                return None
            hdc = gdi32.CreateCompatibleDC(hdc_scr)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_scr, w, h)
            old = gdi32.SelectObject(hdc, hbmp)
            rc = wt.RECT(0, 0, w, h)
            user32.FillRect(hdc, ctypes.byref(rc), gdi32.GetStockObject(4))
            ok = gdi32.BitBlt(hdc, 0, 0, w, h, hdc_scr, 0, 0, 0x00CC0020)
            user32.ReleaseDC(None, hdc_scr)
            if not ok:
                gdi32.SelectObject(hdc, old)
                gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc)
                return None
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer('RGB', (w, h), buf, 'raw', 'BGRX', 0, 1)
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc)
            # 全黑判定
            nz = sum(1 for p in img.getdata() if p != (0, 0, 0))
            if nz < w * h // 200:
                return None
            return img
        except Exception:
            return None

    # ---------------- 窗口枚举 ----------------
    def list_windows(self):
        def _do():
            if not self._desk_handle:
                return {'error': '虚拟桌面未创建'}
            self._in_desktop()
            found = []

            @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, LPARAM)
            def cb(hwnd, lparam):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append({
                        'hwnd': int(hwnd),
                        'pid': int(pid.value),
                        'class': cls.value,
                        'title': buf.value,
                        'rect': [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                    })
                return True
            user32.EnumWindows(cb, 0)
            return {'windows': found}
        return self._submit(_do)

    def set_target(self, hwnd):
        def _do():
            if not hwnd or not user32.IsWindow(hwnd):
                return {'error': f'hwnd {hwnd} 无效'}
            self._target_hwnd = int(hwnd)
            return {'target_hwnd': int(hwnd)}
        return self._submit(_do)

    def activate(self, hwnd=None):
        """在虚拟桌面内激活/聚焦目标窗口（不抢用户桌面焦点）。"""
        def _do():
            self._in_desktop()
            h = hwnd or self._target_hwnd
            if not h or not user32.IsWindow(h):
                return {'error': '目标窗口无效'}
            user32.ShowWindow(h, 1)
            user32.SetForegroundWindow(h)
            user32.SetActiveWindow(h)
            user32.SetFocus(h)
            return {'activated': int(h)}
        return self._submit(_do)


    # ---------------- 真实桌面模式（--real） ----------------
    # 在用户的真实桌面（当前桌面）上枚举窗口 / 截图 / 注入输入。
    # 与虚拟桌面互斥：工作线程会根据任务 real 标志自动切换桌面上下文。

    def list_windows_real(self):
        """枚举真实桌面（用户当前桌面）的顶层可见窗口。"""
        def _do():
            found = []

            @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, LPARAM)
            def cb(hwnd, lparam):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    cls = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls, 128)
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    found.append({
                        'hwnd': int(hwnd),
                        'pid': int(pid.value),
                        'class': cls.value,
                        'title': buf.value,
                        'rect': [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                    })
                return True
            user32.EnumWindows(cb, 0)
            return {'windows': found, 'mode': 'real'}
        return self._submit(_do, real=True)

    def set_target_real(self, hwnd):
        def _do():
            if not hwnd or not user32.IsWindow(hwnd):
                return {'error': f'hwnd {hwnd} 无效'}
            self._real_target_hwnd = int(hwnd)
            return {'target_hwnd': int(hwnd), 'mode': 'real'}
        return self._submit(_do, real=True)

    def activate_real(self, hwnd=None):
        """在真实桌面激活/聚焦目标窗口。"""
        def _do():
            h = hwnd or self._real_target_hwnd
            if not h or not user32.IsWindow(h):
                return {'error': '目标窗口无效'}
            user32.ShowWindow(h, 1)
            user32.SetForegroundWindow(h)
            user32.SetActiveWindow(h)
            user32.SetFocus(h)
            return {'activated': int(h), 'mode': 'real'}
        return self._submit(_do, real=True)

    def launch_real(self, command):
        """在真实桌面（默认桌面）启动程序，返回 PID。"""
        def _do():
            class STARTUPINFO(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                            ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
                            ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                            ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wt.HANDLE),
                            ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]
            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                            ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]
            si = STARTUPINFO()
            si.cb = ctypes.sizeof(STARTUPINFO)
            pi = PROCESS_INFORMATION()
            cmd_buf = ctypes.create_unicode_buffer(command)
            ok = kernel32.CreateProcessW(None, cmd_buf, None, None, False,
                                         0x00000008, None, self.workspace,
                                         ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                return {'error': f'CreateProcess 失败 err={ctypes.get_last_error()}: {command}'}
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
            return {'pid': pi.dwProcessId, 'mode': 'real'}
        return self._submit(_do, timeout=30, real=True)

    def _printwindow_capture(self, hwnd):
        """用 PrintWindow 截取真实桌面窗口（有显示表面，无需 DLL 注入）。返回 PIL Image 或 None。"""
        try:
            from PIL import Image
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = max(1, rect.right - rect.left)
            h = max(1, rect.bottom - rect.top)
            hdc_scr = user32.GetDC(None)
            if not hdc_scr:
                return None
            hdc = gdi32.CreateCompatibleDC(hdc_scr)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_scr, w, h)
            old = gdi32.SelectObject(hdc, hbmp)
            ok = user32.PrintWindow(hwnd, hdc, 2)  # PW_RENDERFULLCONTENT
            if not ok:
                ok = user32.PrintWindow(hwnd, hdc, 0)
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
            img = Image.frombuffer('RGB', (w, h), buf, 'raw', 'BGRX', 0, 1)
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc)
            user32.ReleaseDC(None, hdc_scr)
            if not ok:
                return None
            # 全黑判定（PrintWindow 对某些窗口返回 ok 但内容全黑）
            small = img.resize((max(1, w // 8), max(1, h // 8)))
            nz = sum(1 for p in small.getdata() if p != (0, 0, 0))
            if nz < (small.size[0] * small.size[1]) // 50:
                return None
            return img
        except Exception:
            return None

    def screenshot_real(self, scope='window', hwnd=None, out_name=None):
        """真实桌面截图：scope=window 截目标窗口（PrintWindow），desktop 截整个真实桌面（BitBlt）。"""
        def _do():
            target = hwnd or self._real_target_hwnd
            if scope == 'window':
                if not target or not user32.IsWindow(target):
                    return {'error': '目标窗口无效，请先 #vdesk --real list / #vdesk --real target <hwnd>'}
                img = self._printwindow_capture(target)
                if img is None:
                    return {'error': 'PrintWindow 截图失败（窗口可能不支持离屏渲染）'}
                out_bmp = os.path.join(self.screenshot_dir, out_name or f'real_win_{int(time.time())}.bmp')
                png = out_bmp.replace('.bmp', '.png')
                try:
                    img.save(out_bmp)
                    img.save(png)
                    return {'path': png, 'bmp': out_bmp, 'size': img.size, 'method': 'printwindow-real'}
                except Exception as e:
                    return {'error': f'保存失败: {e}'}
            else:
                out_png = os.path.join(self.screenshot_dir, out_name or f'real_desk_{int(time.time())}.png')
                img = self._bitblt_desktop()
                if img is None:
                    return {'error': '真实桌面截图失败（BitBlt 返回空/全黑）'}
                try:
                    img.save(out_png)
                    return {'path': out_png, 'size': img.size, 'method': 'bitblt-real'}
                except Exception as e:
                    return {'error': f'保存失败: {e}'}
        return self._submit(_do, timeout=60, real=True)

    def inject_real(self, action, **kw):
        """向真实桌面目标窗口注入输入（复用 _do_* 消息级注入实现）。"""
        def _do():
            hwnd = kw.get('hwnd') or self._real_target_hwnd
            if not hwnd or not user32.IsWindow(hwnd):
                return {'error': '目标窗口无效，请先 #vdesk --real target <hwnd>'}
            handler = getattr(self, f'_do_{action}', None)
            if handler is None:
                return {'error': f'不支持的输入动作: {action}'}
            r = handler(hwnd, kw)
            if isinstance(r, dict):
                r['mode'] = 'real'
            return r
        return self._submit(_do, timeout=120, real=True)

    def shot_ocr_real(self, scope='window', lang=None):
        """真实桌面：一步完成截图 + OCR，返回文本与坐标。"""
        shot = self.screenshot_real(scope=scope)
        if 'error' in shot:
            return shot
        ocr = self.ocr(shot['path'], lang)
        if 'error' in ocr:
            return {'shot': shot, 'ocr_error': ocr['error']}
        mark_path = shot['path'].replace('.png', '_marked.png')
        try:
            from PIL import Image, ImageDraw
            img = Image.open(shot['path']).convert('RGB')
            d = ImageDraw.Draw(img)
            for w in ocr['words']:
                d.rectangle([w['x'], w['y'], w['x'] + w['w'], w['y'] + w['h']], outline=(255, 0, 0), width=2)
            img.save(mark_path)
        except Exception:
            mark_path = None
        return {'shot': shot, 'ocr': ocr, 'marked': mark_path}

    def get_info_real(self):
        def _do():
            return {'mode': 'real', 'target_hwnd': self._real_target_hwnd,
                    'desktop': '真实桌面（用户当前桌面）'}
        return self._submit(_do, real=True)

    def close_real(self):
        def _do():
            self._real_target_hwnd = None
            return {'closed': True, 'mode': 'real'}
        return self._submit(_do, real=True)

    # ---------------- 输入注入 ----------------
    def _client_coords(self, hwnd, x, y):
        """把屏幕坐标转成目标窗口客户区坐标。"""
        pt = wt.POINT(int(x), int(y))
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def inject(self, action, **kw):
        def _do():
            if not self._desk_handle:
                return {'error': '虚拟桌面未创建'}
            self._in_desktop()
            hwnd = kw.get('hwnd') or self._target_hwnd
            if not hwnd or not user32.IsWindow(hwnd):
                return {'error': '目标窗口无效，请先 #vdesk target <hwnd>'}
            handler = getattr(self, f'_do_{action}', None)
            if handler is None:
                return {'error': f'不支持的输入动作: {action}'}
            return handler(hwnd, kw)
        return self._submit(_do, timeout=120)

    def _lp(self, x, y):
        return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)

    def _do_click(self, hwnd, kw):
        x, y = int(kw['x']), int(kw['y'])
        button = kw.get('button', 'left')
        double = kw.get('double', False)
        screen = kw.get('screen', False)
        if screen:
            x, y = self._client_coords(hwnd, x, y)
        down = {0: WM_LBUTTONDOWN, 1: WM_RBUTTONDOWN, 2: WM_MBUTTONDOWN}[{'left': 0, 'right': 1, 'middle': 2}[button]]
        up = down + 1
        mk = {0: MK_LBUTTON, 1: MK_RBUTTON, 2: MK_MBUTTON}[{'left': 0, 'right': 1, 'middle': 2}[button]]
        lp = self._lp(x, y)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
        if double:
            user32.PostMessageW(hwnd, down, mk, lp)
            user32.PostMessageW(hwnd, up, 0, lp)
            user32.PostMessageW(hwnd, down, mk, lp)
            user32.PostMessageW(hwnd, up, 0, lp)
        else:
            user32.PostMessageW(hwnd, down, mk, lp)
            user32.PostMessageW(hwnd, up, 0, lp)
        return {'action': 'click', 'button': button, 'x': x, 'y': y, 'double': double}

    def _do_move(self, hwnd, kw):
        x, y = int(kw['x']), int(kw['y'])
        if kw.get('screen', False):
            x, y = self._client_coords(hwnd, x, y)
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, self._lp(x, y))
        return {'action': 'move', 'x': x, 'y': y}

    def _do_drag(self, hwnd, kw):
        x1, y1 = int(kw['x1']), int(kw['y1'])
        x2, y2 = int(kw['x2']), int(kw['y2'])
        steps = int(kw.get('steps', 15))
        button = kw.get('button', 'left')
        if kw.get('screen', False):
            x1, y1 = self._client_coords(hwnd, x1, y1)
            x2, y2 = self._client_coords(hwnd, x2, y2)
        idx = {'left': 0, 'right': 1, 'middle': 2}[button]
        down = {0: WM_LBUTTONDOWN, 1: WM_RBUTTONDOWN, 2: WM_MBUTTONDOWN}[idx]
        up = down + 1
        mk = {0: MK_LBUTTON, 1: MK_RBUTTON, 2: MK_MBUTTON}[idx]

        # ★ 2026-10-13（修「drag 无选区」）：
        #   ① 文本选择由**编辑子控件**处理 —— 原来的消息都发给了顶层窗口，控件收不到；
        #   ② 真实拖动时系统会把鼠标**捕获**给按下的那个窗口（SetCapture），
        #      此后移动消息才归它；PostMessage 没有这层语义，控件认为指针已移出、
        #      于是不做选择。
        #   这里：找到起始点所在的子控件 → 坐标换算到它的客户区 →
        #         down 前 SetCapture、up 后 ReleaseCapture → 用 SendMessage 同步投递。
        target = int(hwnd)
        try:
            # 起始点的屏幕坐标 → 该点下的子窗口
            pt = wt.POINT(int(x1), int(y1))
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            child = user32.WindowFromPoint(pt)
            if child and user32.IsWindow(child):
                target = int(child)
            else:
                _ec = self._find_edit_child(hwnd)
                if _ec:
                    target = int(_ec)
            if target != int(hwnd):
                # 把坐标从顶层窗口客户区换算到子控件客户区
                p1 = wt.POINT(int(x1), int(y1))
                p2 = wt.POINT(int(x2), int(y2))
                user32.ClientToScreen(hwnd, ctypes.byref(p1))
                user32.ClientToScreen(hwnd, ctypes.byref(p2))
                user32.ScreenToClient(target, ctypes.byref(p1))
                user32.ScreenToClient(target, ctypes.byref(p2))
                x1, y1, x2, y2 = p1.x, p1.y, p2.x, p2.y
        except Exception:
            target = int(hwnd)

        def _send(msg, wparam, lparam):
            try:
                user32.SendMessageW(target, msg, wparam, lparam)
            except Exception:
                user32.PostMessageW(target, msg, wparam, lparam)

        _send(WM_MOUSEMOVE, 0, self._lp(x1, y1))
        user32.SetCapture(target)          # ★ 关键：让拖动期间的移动消息都归它
        _send(down, mk, self._lp(x1, y1))
        steps = max(1, min(steps, 200))
        for i in range(1, steps + 1):
            t = i / steps
            cx = int(x1 + (x2 - x1) * t)
            cy = int(y1 + (y2 - y1) * t)
            _send(WM_MOUSEMOVE, mk, self._lp(cx, cy))
            time.sleep(0.01)
        _send(WM_MOUSEMOVE, mk, self._lp(x2, y2))
        time.sleep(0.05)
        _send(up, 0, self._lp(x2, y2))
        try:
            user32.ReleaseCapture()
        except Exception:
            pass
        return {'action': 'drag', 'from': (x1, y1), 'to': (x2, y2),
                'steps': steps, 'target': hex(target), 'child': target != int(hwnd)}

    def _do_wheel(self, hwnd, kw):
        delta = int(kw.get('delta', 120))
        x, y = int(kw.get('x', 10)), int(kw.get('y', 10))
        if kw.get('screen', False):
            x, y = self._client_coords(hwnd, x, y)
        lp = ((delta & 0xFFFF) << 16) | self._lp(x, y) & 0xFFFF
        lp = ((delta & 0xFFFF) << 16) | (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)
        user32.PostMessageW(hwnd, WM_MOUSEWHEEL, 0, lp)
        return {'action': 'wheel', 'delta': delta, 'x': x, 'y': y}

    # ---------- 键盘 ----------
    _VK = {
        'enter': 0x0D, 'return': 0x0D, 'esc': 0x1B, 'escape': 0x1B, 'tab': 0x09,
        'space': 0x20, 'backspace': 0x08, 'delete': 0x2E, 'del': 0x2E,
        'insert': 0x2D, 'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
        'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
        'capslock': 0x14, 'numlock': 0x90, 'scrolllock': 0x91,
        'printscreen': 0x2C, 'pause': 0x13, 'win': 0x5B, 'lwin': 0x5B, 'rwin': 0x5C,
        'ctrl': 0x11, 'control': 0x11, 'lctrl': 0xA2, 'rctrl': 0xA3,
        'alt': 0x12, 'lalt': 0xA4, 'ralt': 0xA5,
        'shift': 0x10, 'lshift': 0xA0, 'rshift': 0xA1,
        'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
        'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
        '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
        '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
        'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46, 'g': 0x47,
        'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E,
        'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55,
        'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A,
        ';': 0xBA, '=': 0xBB, ',': 0xBC, '-': 0xBD, '.': 0xBE, '/': 0xBF,
        '`': 0xC0, '[': 0xDB, '\\': 0xDC, ']': 0xDD, "'": 0xDE,
        'numpad0': 0x60, 'numpad1': 0x61, 'numpad2': 0x62, 'numpad3': 0x63, 'numpad4': 0x64,
        'numpad5': 0x65, 'numpad6': 0x66, 'numpad7': 0x67, 'numpad8': 0x68, 'numpad9': 0x69,
        'multiply': 0x6A, 'add': 0x6B, 'subtract': 0x6D, 'decimal': 0x6E, 'divide': 0x6F,
    }
    _EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x90}

    def _key_lparam(self, vk, up=False, extended=False):
        scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        lparam = 1 | (scan << 16)
        if extended:
            lparam |= 0x01000000
        if up:
            lparam |= 0xC0000000  # bit30 transition, bit31 up
        else:
            lparam |= 0x00000001  # repeat count 1
        return lparam

    # ★ 2026-10-13（修「key=enter 不换行 / 按键注入对编辑控件无效」）：
    #   PostMessage 投递的 WM_KEYDOWN **不会**经过消息循环的 TranslateMessage，
    #   因此永远不会产生 WM_CHAR —— 而编辑控件插入文本靠的正是 WM_CHAR
    #   （_do_type 就是用 WM_CHAR，所以文字输入一直正常）。
    #   这里给出"按键 → 它应产生的字符"，供 _post_key 补发 WM_CHAR。
    _VK_CHAR = {
        0x0D: '\r',   # enter
        0x08: '\x08', # backspace
        0x09: '\t',   # tab
        0x1B: '\x1b', # esc
        0x20: ' ',    # space
    }

    def _post_key(self, hwnd, vk, up=False, extended=False):
        msg = WM_KEYUP if up else WM_KEYDOWN
        lp = self._key_lparam(vk, up, extended)
        user32.PostMessageW(hwnd, msg, vk, lp)

    def _post_char(self, hwnd, ch):
        """补发 WM_CHAR —— 编辑控件靠它插入字符。"""
        try:
            user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
        except Exception:
            pass


    # ★ 2026-10-13（修 hotkey 组合键失效）：
    #   PostMessage 只能在消息里带"主键"，**修饰键的按下状态是系统级的、不在消息里** ——
    #   所以 ctrl+a 到编辑控件手里就退化成普通 'a'。
    #   组合键必须用 SendInput 做系统级注入（并先把目标窗口激活）。
    def _send_key(self, vk, up=False, extended=False):
        """系统级注入一次按键（SendInput）。"""
        try:
            _inp = _make_key_input(vk, up, extended)
            n = user32.SendInput(1, ctypes.byref(_inp), ctypes.sizeof(_inp))
            return bool(n)
        except Exception:
            return False

    def _maybe_post_char(self, hwnd, vk, shift=False):
        """只对"能产生字符"的键补发 WM_CHAR。
        方向键/F1-F12/修饰键等不产生字符 → 不发（否则会给文档里打错东西）。
        ★ 目标必须是 **Edit 子控件**（与 _do_type 一致）：WM_KEYDOWN 顶层窗口会转交，
          但 WM_CHAR 不会 —— 发给顶层窗口等于石沉大海（我第一版就是这么失败的）。"""
        target = self._find_edit_child(hwnd) or hwnd
        ch = self._VK_CHAR.get(vk)
        if ch is None and 0x30 <= vk <= 0x5A:      # 0-9 / A-Z
            ch = chr(vk) if not shift else chr(vk)
        if ch is None:
            return None
        if ch == '\r':
            # ★ 只发 \r —— Windows 里 Enter 产生的 WM_CHAR 就是 \r，
            #   换行由编辑控件自己处理。多发一个 \n 会在某些控件里变成两个换行。
            self._post_char(target, '\r')
        else:
            self._post_char(target, ch)
        return ch

    def _do_key(self, hwnd, kw):
        name = str(kw.get('key', '')).lower()
        vk = self._VK.get(name)
        if vk is None:
            if len(name) == 1:
                vk = ord(name.upper()) if name.isalpha() else ord(name)
            else:
                return {'error': f'未知按键: {name}'}
        ext = vk in self._EXTENDED
        self._post_key(hwnd, vk, False, ext)
        # ★ 补发 WM_CHAR（编辑控件靠它插入字符）——见 _VK_CHAR 的说明
        self._maybe_post_char(hwnd, vk)
        time.sleep(0.02)
        self._post_key(hwnd, vk, True, ext)
        return {'action': 'key', 'key': name, 'vk': vk}

    # ★ 2026-10-13（修组合键）：虚拟桌面上 SendInput 不可用（返回 0，因为它要求
    #   线程处于输入桌面），而 PostMessage 又带不了修饰键状态 —— 所以常见组合键
    #   改用**等价的消息级实现**（直接作用在编辑控件上，确定有效）。
    EM_SETSEL = 0x00B1
    EM_SCROLLCARET = 0x00B7
    EM_UNDO = 0x00C7
    WM_COPY = 0x0301
    WM_PASTE = 0x0302
    WM_CUT = 0x0300
    WM_CLEAR = 0x0303

    def _hotkey_equiv(self, hwnd, combo):
        """用等价消息实现常见组合键；无法实现时返回 None。"""
        try:
            target = self._find_edit_child(hwnd) or hwnd
            if combo in ('ctrl+a', 'ctrl+shift+a'):
                # 全选（编辑控件的标准做法）
                user32.SendMessageW(target, self.EM_SETSEL, 0, -1)
                return 'EM_SETSEL(全选)'
            if combo == 'ctrl+c':
                user32.SendMessageW(target, self.WM_COPY, 0, 0)
                return 'WM_COPY'
            if combo == 'ctrl+v':
                user32.SendMessageW(target, self.WM_PASTE, 0, 0)
                return 'WM_PASTE'
            if combo == 'ctrl+x':
                user32.SendMessageW(target, self.WM_CUT, 0, 0)
                return 'WM_CUT'
            if combo == 'ctrl+z':
                user32.SendMessageW(target, self.EM_UNDO, 0, 0)
                return 'EM_UNDO'
            if combo in ('ctrl+end', 'ctrl+home'):
                pos = -1 if combo == 'ctrl+end' else 0
                user32.SendMessageW(target, self.EM_SETSEL, pos, pos)
                user32.SendMessageW(target, self.EM_SCROLLCARET, 0, 0)
                return 'EM_SETSEL(定位)'
        except Exception:
            return None
        return None

    def _do_hotkey(self, hwnd, kw):
        combo = str(kw.get('hotkey', '')).lower().replace(' ', '')
        parts = [p for p in re.split(r'[+ ]', combo) if p]
        if not parts:
            return {'error': '热键格式错误，示例: ctrl+c'}
        mods = []
        main = None
        for p in parts:
            if p in ('ctrl', 'control', 'lctrl', 'rctrl'):
                mods.append(('ctrl', self._VK[p]))
            elif p in ('alt', 'lalt', 'ralt'):
                mods.append(('alt', self._VK[p]))
            elif p in ('shift', 'lshift', 'rshift'):
                mods.append(('shift', self._VK[p]))
            elif p in ('win', 'lwin', 'rwin'):
                mods.append(('win', self._VK[p]))
            else:
                main = p
        if main is None:
            return {'error': '缺少主键，示例: ctrl+c'}
        vk = self._VK.get(main)
        if vk is None:
            if len(main) == 1:
                vk = ord(main.upper()) if main.isalpha() else ord(main)
            else:
                return {'error': f'未知按键: {main}'}
        # 按下修饰键（顺序：ctrl, alt, shift, win）
        order = {'ctrl': 0, 'alt': 1, 'shift': 2, 'win': 3}
        mods.sort(key=lambda m: order[m[0]])
        # ★ 2026-10-13：优先用**等价消息**实现（虚拟桌面上 SendInput 不可用，
        #   而 PostMessage 带不了修饰键状态 —— 详见 _hotkey_equiv 的说明）。
        _eq = self._hotkey_equiv(hwnd, '+'.join(parts))
        if _eq:
            return {'action': 'hotkey', 'combo': '+'.join(parts),
                    'mods': [m for m, _ in mods], 'main': main, 'via': _eq}
        # 未覆盖的组合：退回原来的注入方式。
        #   ⚠️ 注意：**必须内联**做激活，不能调 self.activate() ——
        #      本方法是在工作线程里执行的（由 _submit 调度），
        #      而 activate() 内部还会 self._submit(...) → 工作线程等自己 → 死锁超时
        #      （实测：inject('hotkey') 超时后返回上一次任务的结果，
        #        看起来像"返回值不对"，其实是死锁）。
        try:
            user32.ShowWindow(hwnd, 1)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
            time.sleep(0.08)
        except Exception:
            pass
        _ok = True
        for _, mvk in mods:
            _ok = self._send_key(mvk, False, mvk in self._EXTENDED) and _ok
        _ok = self._send_key(vk, False, vk in self._EXTENDED) and _ok
        time.sleep(0.03)
        _ok = self._send_key(vk, True, vk in self._EXTENDED) and _ok
        for _, mvk in reversed(mods):
            _ok = self._send_key(mvk, True, mvk in self._EXTENDED) and _ok
        if not _ok:
            # SendInput 不可用 → 退回原来的 PostMessage 路径（至少比什么都不做好）
            for _, mvk in mods:
                self._post_key(hwnd, mvk, False, mvk in self._EXTENDED)
            self._post_key(hwnd, vk, False, vk in self._EXTENDED)
            time.sleep(0.03)
            self._post_key(hwnd, vk, True, vk in self._EXTENDED)
            for _, mvk in reversed(mods):
                self._post_key(hwnd, mvk, True, mvk in self._EXTENDED)
            return {'action': 'hotkey', 'combo': '+'.join(parts),
                    'mods': [m for m, _ in mods], 'main': main, 'via': 'postmessage(降级)'}
        return {'action': 'hotkey', 'combo': '+'.join(parts),
                'mods': [m for m, _ in mods], 'main': main, 'via': 'sendinput'}

    def _do_type(self, hwnd, kw):
        text = kw.get('text', '')
        if not text:
            return {'error': '缺少文本'}
        # 若目标窗口有 Edit 子窗口，优先投递到 Edit（模拟真实输入位置）
        target = self._find_edit_child(hwnd) or hwnd
        for ch in text:
            code = ord(ch)
            # 尝试 WM_CHAR 直接投递 UTF-16 码元
            user32.PostMessageW(target, WM_CHAR, code, 0)
            time.sleep(0.005)
        return {'action': 'type', 'chars': len(text), 'text': text[:50]}

    def _do_settext(self, hwnd, kw):
        text = kw.get('text', '')
        edit = self._find_edit_child(hwnd)
        if not edit:
            return {'error': '目标窗口没有 Edit 子控件，无法直接设置文本'}
        buf = ctypes.create_unicode_buffer(text)
        user32.SendMessageW(edit, 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)  # WM_SETTEXT
        return {'action': 'settext', 'chars': len(text), 'text': text[:50]}

    def _find_edit_child(self, hwnd):
        """递归查找第一个 Edit 类子窗口（在工作线程桌面上下文调用）。"""
        found = [None]

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, LPARAM)
        def cb(child, lparam):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(child, cls, 128)
            if 'Edit' in cls.value or 'RichEdit' in cls.value:
                found[0] = int(child)
                return False
            return True
        user32.EnumChildWindows(hwnd, cb, 0)
        return found[0]

    # ---------------- OCR ----------------
    def ocr(self, image_path, lang=None):
        """OCR 识别图片，返回词级结果 [{text,x,y,w,h,line}]。"""
        if not os.path.exists(image_path):
            return {'error': f'图片不存在: {image_path}'}
        try:
            import asyncio
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.graphics.imaging import BitmapDecoder
            from winsdk.windows.storage import StorageFile, FileAccessMode
            from winsdk.windows.globalization import Language

            async def _do_ocr(path, want_lang):
                engine = None
                if want_lang:
                    try:
                        engine = OcrEngine.try_create_from_language(Language(want_lang))
                    except Exception:
                        engine = None
                if engine is None:
                    engine = OcrEngine.try_create_from_user_profile_languages()
                if engine is None:
                    return {'error': '无法创建 OCR 引擎（系统无语言包）'}
                langs = [l.language_tag for l in OcrEngine.available_recognizer_languages]
                file = await StorageFile.get_file_from_path_async(path)
                stream = await file.open_async(FileAccessMode.READ)
                decoder = await BitmapDecoder.create_async(stream)
                bitmap = await decoder.get_software_bitmap_async()
                result = await engine.recognize_async(bitmap)
                words = []
                for line in result.lines:
                    for w in line.words:
                        r = w.bounding_rect
                        words.append({
                            'text': w.text,
                            'x': int(r.x), 'y': int(r.y),
                            'w': int(r.width), 'h': int(r.height),
                            'cx': int(r.x + r.width / 2), 'cy': int(r.y + r.height / 2),
                            'line': line.text,
                        })
                return {'words': words, 'lang': engine.recognizer_language.language_tag,
                        'available': langs}
            return asyncio.run(_do_ocr(image_path, lang))
        except ImportError:
            return self._ocr_powershell(image_path, lang)
        except Exception as e:
            # winsdk 失败回退 PowerShell
            try:
                return self._ocr_powershell(image_path, lang)
            except Exception:
                return {'error': f'OCR 失败: {e}'}

    def _ocr_powershell(self, image_path, lang=None):
        """PowerShell + WinRT OCR 回退方案。"""
        cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', self.ocr_ps1, '-ImagePath', image_path]
        if lang:
            cmd += ['-Lang', lang]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        raw = proc.stdout
        out = None
        for enc in ('utf-8', 'gb18030', 'gbk'):
            try:
                out = raw.decode(enc)
                if '\ufffd' not in out or enc == 'gbk':
                    break
            except UnicodeDecodeError:
                continue
        if out is None:
            out = raw.decode('utf-8', errors='replace')
        lines = [l for l in out.splitlines() if l.strip().startswith('[') or l.strip().startswith('{')]
        try:
            data = json.loads('\n'.join(lines))
            if isinstance(data, list):
                words = [{'text': d['text'], 'x': d['x'], 'y': d['y'], 'w': d['w'], 'h': d['h'],
                          'cx': d['x'] + d['w'] // 2, 'cy': d['y'] + d['h'] // 2, 'line': d.get('line', d['text'])}
                         for d in data]
                return {'words': words, 'lang': lang or 'auto', 'available': []}
        except Exception:
            pass
        return {'error': f'PowerShell OCR 失败: {out[-500:]}'}

    # ---------------- 便捷：截图 + OCR ----------------
    def shot_ocr(self, scope='window', lang=None):
        """一步完成截图 + OCR，返回文本与坐标。"""
        shot = self.screenshot(scope=scope)
        if 'error' in shot:
            return shot
        ocr = self.ocr(shot['path'], lang)
        if 'error' in ocr:
            return {'shot': shot, 'ocr_error': ocr['error']}
        # 生成标注图
        mark_path = shot['path'].replace('.png', '_marked.png')
        try:
            from PIL import Image, ImageDraw
            img = Image.open(shot['path']).convert('RGB')
            d = ImageDraw.Draw(img)
            for w in ocr['words']:
                d.rectangle([w['x'], w['y'], w['x'] + w['w'], w['y'] + w['h']], outline=(255, 0, 0), width=2)
            img.save(mark_path)
        except Exception:
            mark_path = None
        return {'shot': shot, 'ocr': ocr, 'marked': mark_path}

    # ---------------- 工具函数 ----------------
    def parse_int(self, s):
        try:
            return int(str(s), 16) if str(s).lower().startswith('0x') else int(str(s))
        except Exception:
            return None


# 全局单例（由 agent.py 初始化）
_instance = None


def init(workspace_dir=None, capture_dll_path=None, ocr_ps1_path=None):
    global _instance
    if _instance is None:
        # 打包为 exe 运行时，以 exe 所在目录为基准（sys.executable）；
        # 源码运行时以本文件所在目录为基准。
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        if workspace_dir is None:
            workspace_dir = os.path.join(base_dir, 'workspace')
        if capture_dll_path is None:
            capture_dll_path = os.path.join(base_dir, 'capture_dll', 'capture_helper.dll')
        if ocr_ps1_path is None:
            ocr_ps1_path = os.path.join(base_dir, 'ocr.ps1')
        _instance = VirtualUI(workspace_dir, capture_dll_path, ocr_ps1_path)
    return _instance


def get():
    if _instance is None:
        return init()
    return _instance


# ---------------------------------------------------------------------------
# 命令解析：#vdesk / #ocr / #input
# ---------------------------------------------------------------------------
def split_args(s):
    """按空格拆分，支持双引号/单引号包裹。"""
    out = []
    for groups in re.findall(r'"([^"]*)"|\'([^\']*)\'|(\S+)', s):
        out.append(next(g for g in groups if g))
    return out


def parse_flags(args):
    """解析 --flag 与 --key=value，返回 (位置参数列表, 选项字典)。"""
    positional = []
    opts = {}
    for a in args:
        if a.startswith('--'):
            if '=' in a:
                k, _, v = a[2:].partition('=')
                opts[k] = v
            else:
                opts[a[2:]] = True
        else:
            positional.append(a)
    return positional, opts


def handle_vdesk(cmd):
    """处理 #vdesk 子命令。加 --real 表示操作真实桌面（用户当前桌面）。"""
    vui = get()
    args = split_args(cmd[len('#vdesk'):].strip())
    real = False
    if '--real' in args:
        real = True
        args = [a for a in args if a != '--real']
    if not args:
        base = ("用法:\n"
                "  #vdesk create                    创建隐藏虚拟桌面\n"
                "  #vdesk launch <命令行>          在虚拟桌面启动程序\n"
                "  #vdesk list                      列出虚拟桌面窗口\n"
                "  #vdesk target <hwnd>             设置目标窗口\n"
                "  #vdesk shot [--window|--desktop] 截图（默认窗口）\n"
                "  #vdesk info                      查看状态\n"
                "  #vdesk close                     关闭虚拟桌面\n"
                "  #vdesk --real list               列出【真实桌面】窗口（用户当前桌面）\n"
                "  #vdesk --real target <hwnd>      设置真实桌面目标窗口\n"
                "  #vdesk --real launch <命令>      在真实桌面启动程序\n"
                "  #vdesk --real shot [--window|--desktop]  真实桌面截图\n"
                "  #vdesk --real info / close       真实桌面状态 / 清除目标")
        return base
    sub = args[0].lower()
    if sub == 'create':
        if real:
            return "真实桌面模式无需创建桌面（操作的就是用户当前桌面）。可用 #vdesk --real list 查看窗口。"
        r = vui.create_desktop()
        return json.dumps(r, ensure_ascii=False)
    if sub == 'launch':
        command = ' '.join(args[1:])
        if not command:
            return "用法: #vdesk [--real] launch <命令行>"
        if real:
            r = vui.launch_real(command)
            if 'pid' in r:
                return f"已在真实桌面启动，PID={r['pid']}\n请用 #vdesk --real list 查看窗口并 #vdesk --real target <hwnd> 选定目标窗口。"
            return json.dumps(r, ensure_ascii=False)
        r = vui.launch(command)
        if 'pid' in r:
            return f"已在虚拟桌面启动，PID={r['pid']}，注入截图DLL: {r.get('inject', '?')}\n请用 #vdesk list 查看窗口并 #vdesk target <hwnd> 选定目标窗口。"
        return json.dumps(r, ensure_ascii=False)
    if sub == 'list':
        if real:
            r = vui.list_windows_real()
        else:
            r = vui.list_windows()
        if 'error' in r:
            return r['error']
        wins = r['windows']
        tag = '真实桌面' if real else '虚拟桌面'
        if not wins:
            return f"{tag}没有可见窗口。"
        lines = [f"窗口列表（{tag}）:", f"{'hwnd':<12}{'pid':<8}{'class':<28}{'title'}"]
        for w in wins:
            lines.append(f"0x{w['hwnd']:X}  {w['pid']:<8}{w['class']:<28}{w['title'][:60]}")
        lines.append(f"提示: 用 #vdesk {'--real ' if real else ''}target 0x<hwnd> 选定目标窗口")
        return '\n'.join(lines)
    if sub == 'target':
        if len(args) < 2:
            return "用法: #vdesk [--real] target <hwnd>（hwnd 可用 0x 前缀十六进制）"
        # ★ 2026-10-13（修 500）：这里原来调 `vui.parse_int(...)` ——
        #   那个方法**不存在**，于是 `#vdesk target` 一律 AttributeError → 500，
        #   目标窗口永远设不上，后续所有 #input 都报"目标窗口无效"。
        #   改为 int(x, 0)：同时接受十进制与 0x 十六进制；非法输入给可读提示。
        try:
            hwnd = int(str(args[1]).strip(), 0)
        except Exception:
            return f"hwnd 格式错误: {args[1]!r}（可用 0x 前缀十六进制，或十进制数字）"
        r = vui.set_target_real(hwnd) if real else vui.set_target(hwnd)
        return json.dumps(r, ensure_ascii=False)
    if sub in ('shot', 'screenshot'):
        _, opts = parse_flags(args[1:])
        scope = 'window' if opts.get('window') else ('desktop' if opts.get('desktop') else 'window')
        r = vui.screenshot_real(scope=scope) if real else vui.screenshot(scope=scope)
        if 'path' in r:
            return f"截图保存: {r['path']} 尺寸={r['size']} 方法={r.get('method', '')}"
        return json.dumps(r, ensure_ascii=False)
    if sub == 'info':
        r = vui.get_info_real() if real else vui.get_info()
        return json.dumps(r, ensure_ascii=False, indent=1)
    if sub in ('close', 'destroy', 'stop'):
        r = vui.close_real() if real else vui.close_desktop()
        return json.dumps(r, ensure_ascii=False)
    return f"未知子命令: {sub}\n{handle_vdesk('#vdesk')}"


def handle_ocr(cmd):
    """处理 #ocr 命令：截图 + OCR。加 --real 表示真实桌面。"""
    vui = get()
    rest = cmd[len('#ocr'):].strip()
    args = split_args(rest)
    real = '--real' in args
    args = [a for a in args if a != '--real']
    _, opts = parse_flags(args)
    scope = 'window' if opts.get('window') else ('desktop' if opts.get('desktop') else 'window')
    lang = opts.get('lang') or opts.get('language') or None
    r = vui.shot_ocr_real(scope=scope, lang=lang) if real else vui.shot_ocr(scope=scope, lang=lang)
    if 'error' in r:
        return r['error']
    ocr = r.get('ocr', {})
    if 'error' in ocr:
        return f"截图成功但 OCR 失败: {ocr['error']}\n截图: {r['shot']['path']}"
    words = ocr.get('words', [])
    shot = r['shot']
    lines_out = [f"截图: {shot['path']}（尺寸 {shot['size']}，方法 {shot.get('method', '')}）",
                 f"OCR 语言: {ocr.get('lang', '?')} | 识别到 {len(words)} 个词",
                 ""]
    # 按行聚合
    line_map = {}
    for w in words:
        key = w['line']
        if key not in line_map:
            line_map[key] = []
        line_map[key].append(w)
    idx = 1
    for line_text, ws in line_map.items():
        min_x = min(w['x'] for w in ws)
        min_y = min(w['y'] for w in ws)
        max_x = max(w['x'] + w['w'] for w in ws)
        max_y = max(w['y'] + w['h'] for w in ws)
        cx = (min_x + max_x) // 2
        cy = (min_y + max_y) // 2
        lines_out.append(f"{idx}. \"{line_text}\"  区域=({min_x},{min_y},{max_x - min_x}x{max_y - min_y}) 中心=({cx},{cy})")
        idx += 1
    lines_out.append("")
    lines_out.append(f"标注图: {r.get('marked', '无')}")
    return '\n'.join(lines_out)


def handle_input(cmd):
    """处理 #input 命令：模拟键盘鼠标。加 --real 表示操作真实桌面目标窗口。"""
    vui = get()
    rest = cmd[len('#input'):].strip()
    parts = split_args(rest)
    real = '--real' in parts
    parts = [p for p in parts if p != '--real']
    if not parts:
        return ("用法:\n"
                "  #input [--real] click <x> <y> [--button=left|right|middle] [--double] [--screen]\n"
                "  #input [--real] move <x> <y> [--screen]\n"
                "  #input [--real] drag <x1> <y1> <x2> <y2> [--steps=15] [--button=left] [--screen]\n"
                "  #input [--real] wheel <delta> [--x=.. --y=..] [--screen]\n"
                "  #input [--real] key <名字>            enter/tab/esc/f1..f12/a..z/0..9/...\n"
                "  #input [--real] hotkey <组合>         ctrl+c / ctrl+shift+s / alt+f4 / win+d\n"
                "  #input [--real] type <文本>           输入文本（支持中文）\n"
                "  #input [--real] activate              激活目标窗口\n"
                "坐标默认是目标窗口客户区坐标（与 #ocr --window 截图一致）；加 --screen 表示屏幕坐标。\n"
                "加 --real 时操作的是真实桌面（用户当前桌面）的窗口。")
    action = parts[0].lower()
    args, opts = parse_flags(parts[1:])

    def num(i):
        return vui.parse_int(args[i]) if i < len(args) else None

    if action == 'click':
        x, y = num(0), num(1)
        if x is None or y is None:
            return "用法: #input click <x> <y> [--button=left|right|middle] [--double] [--screen]"
        r = (vui.inject_real if real else vui.inject)('click', x=x, y=y, button=opts.get('button', 'left'),
                       double=bool(opts.get('double')), screen=bool(opts.get('screen')))
        return json.dumps(r, ensure_ascii=False)
    if action == 'move':
        x, y = num(0), num(1)
        if x is None or y is None:
            return "用法: #input move <x> <y> [--screen]"
        r = (vui.inject_real if real else vui.inject)('move', x=x, y=y, screen=bool(opts.get('screen')))
        return json.dumps(r, ensure_ascii=False)
    if action == 'drag':
        x1, y1, x2, y2 = num(0), num(1), num(2), num(3)
        if None in (x1, y1, x2, y2):
            return "用法: #input drag <x1> <y1> <x2> <y2> [--steps=15] [--button=left] [--screen]"
        r = (vui.inject_real if real else vui.inject)('drag', x1=x1, y1=y1, x2=x2, y2=y2,
                       steps=int(opts.get('steps', 15)), button=opts.get('button', 'left'),
                       screen=bool(opts.get('screen')))
        return json.dumps(r, ensure_ascii=False)
    if action == 'wheel':
        d = num(0)
        if d is None:
            return "用法: #input wheel <delta> [--x=.. --y=..] [--screen]"
        r = (vui.inject_real if real else vui.inject)('wheel', delta=d, x=opts.get('x', 10), y=opts.get('y', 10),
                       screen=bool(opts.get('screen')))
        return json.dumps(r, ensure_ascii=False)
    if action == 'key':
        if len(args) < 1:
            return "用法: #input key <名字>"
        r = (vui.inject_real if real else vui.inject)('key', key=args[0])
        return json.dumps(r, ensure_ascii=False)
    if action == 'hotkey':
        if len(args) < 1:
            return "用法: #input hotkey ctrl+c"
        r = (vui.inject_real if real else vui.inject)('hotkey', hotkey=args[0])
        return json.dumps(r, ensure_ascii=False)
    if action in ('type', 'text'):
        if len(args) < 1:
            return "用法: #input type <文本>"
        r = (vui.inject_real if real else vui.inject)('type', text=args[0])
        return json.dumps(r, ensure_ascii=False)
    if action == 'settext':
        if len(args) < 1:
            return "用法: #input settext <文本>（直接写入目标窗口的 Edit 控件）"
        r = (vui.inject_real if real else vui.inject)('settext', text=args[0])
        return json.dumps(r, ensure_ascii=False)
    if action == 'activate':
        r = vui.activate_real() if real else vui.activate()
        return json.dumps(r, ensure_ascii=False)
    return f"未知输入动作: {action}\n{handle_input('#input')}"


def handle_command(cmd):
    """agent.py /execute 的命令分发入口。返回 (输出文本, 返回码)。"""
    cmd = cmd.strip()
    if cmd.startswith('#vdesk'):
        return handle_vdesk(cmd), 0
    if cmd.startswith('#ocr'):
        return handle_ocr(cmd), 0
    if cmd.startswith('#input'):
        return handle_input(cmd), 0
    return None, None
