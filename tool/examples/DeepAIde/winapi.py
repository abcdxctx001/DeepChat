# -*- coding: utf-8 -*-
"""
winapi.py —— 纯标准库（ctypes）实现的 Windows 进程监控与操作。
替代 psutil 的核心功能：CPU 时间、内存（工作集/峰值）、子进程枚举、进程树击杀。
要求：Windows，Python 3.8+。
"""

import ctypes
import ctypes.wintypes as wt
import sys
import time

if sys.platform != 'win32':
    raise RuntimeError('winapi 仅支持 Windows')


class ProcessNotFound(Exception):
    """进程不存在或无权限访问"""
    pass


kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
try:
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
except OSError:
    psapi = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

# Job 对象：JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE —— 关闭最后一个句柄时瞬间终止整个进程树
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
JobObjectBasicProcessIdList = 3

# Job 硬限制标志（沙盒：内核强制，即使监控被干扰也能兜底）
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_READ_BYTES = 0x00010000
JOB_OBJECT_LIMIT_WRITE_BYTES = 0x00020000


class FILETIME(ctypes.Structure):
    _fields_ = [
        ('dwLowDateTime', wt.DWORD),
        ('dwHighDateTime', wt.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('cb', wt.DWORD),
        ('PageFaultCount', wt.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
    ]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD),
        ('cntUsage', wt.DWORD),
        ('th32ProcessID', wt.DWORD),
        ('th32DefaultHeapID', ctypes.POINTER(wt.ULONG)),
        ('th32ModuleID', wt.DWORD),
        ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD),
        ('pcPriClassBase', wt.LONG),
        ('dwFlags', wt.DWORD),
        ('szExeFile', ctypes.c_char * 260),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', wt.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wt.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wt.DWORD),
        ('SchedulingClass', wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_longlong),
        ('WriteOperationCount', ctypes.c_longlong),
        ('OtherOperationCount', ctypes.c_longlong),
        ('ReadTransferCount', ctypes.c_longlong),
        ('WriteTransferCount', ctypes.c_longlong),
        ('OtherTransferCount', ctypes.c_longlong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    """Job 内进程列表（按需动态扩容）。
    注意：ProcessIdList 元素为 ULONG_PTR（64 位下 8 字节），不是 DWORD。"""
    _fields_ = [
        ('NumberOfAssignedProcesses', wt.DWORD),
        ('NumberOfProcessIdsInList', wt.DWORD),
        ('ProcessIdList', ctypes.c_size_t * 1),
    ]


# ---- 函数签名（防止 64 位句柄被截断） ----
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetProcessTimes.restype = wt.BOOL
kernel32.GetProcessTimes.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Process32First.restype = wt.BOOL
kernel32.Process32First.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.Process32Next.restype = wt.BOOL
kernel32.Process32Next.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.TerminateProcess.restype = wt.BOOL
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.SetErrorMode.restype = wt.UINT
kernel32.SetErrorMode.argtypes = [wt.UINT]
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
kernel32.SetInformationJobObject.restype = wt.BOOL
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.restype = wt.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.QueryInformationJobObject.restype = wt.BOOL
kernel32.QueryInformationJobObject.argtypes = [
    wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
]
if psapi is not None:
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD,
    ]


def _open_process(pid, access):
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        raise ProcessNotFound(pid)
    return h


def disable_error_dialogs():
    """关闭 Windows 错误报告弹窗（子进程继承），崩溃程序立即退出"""
    try:
        kernel32.SetErrorMode(0x8007)  # SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX
    except Exception:
        pass


def process_cpu_times(pid):
    """返回 (user秒, system秒)；进程不存在抛 ProcessNotFound"""
    h = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    try:
        c = FILETIME()
        e = FILETIME()
        k = FILETIME()
        u = FILETIME()
        if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                       ctypes.byref(k), ctypes.byref(u)):
            raise ProcessNotFound(pid)

        def to_s(f):
            return ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7
        return to_s(k), to_s(u)
    finally:
        kernel32.CloseHandle(h)


def process_memory_mb(pid):
    """返回 (工作集MB, 峰值工作集MB)；进程不存在抛 ProcessNotFound"""
    if psapi is None:
        return 0.0, 0.0
    h = _open_process(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            raise ProcessNotFound(pid)
        return pmc.WorkingSetSize / 1048576.0, pmc.PeakWorkingSetSize / 1048576.0
    finally:
        kernel32.CloseHandle(h)


def children_pids(pid):
    """返回 pid 的所有后代进程 id（递归），进程不存在返回空列表"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    kids = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            kids.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    out = []
    stack = [pid]
    while stack:
        cur = stack.pop()
        for c in kids.get(cur, []):
            out.append(c)
            stack.append(c)
    return out


def create_job():
    """创建带 KILL_ON_JOB_CLOSE 的 Job 对象；失败返回 None。
    之后子进程产生的所有后代会自动加入同一 Job，关闭句柄即可瞬间终止整棵进程树。"""
    try:
        h = kernel32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(h, JobObjectExtendedLimitInformation,
                                              ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(h)
            return None
        return h
    except Exception:
        return None


def assign_job(hjob, pid):
    """把进程加入 Job（需进程句柄权限）；失败（如已属于其它 Job）返回 False"""
    if not hjob:
        return False
    try:
        h = _open_process(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE)
    except ProcessNotFound:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(hjob, h))
    finally:
        kernel32.CloseHandle(h)


def set_job_limits(hjob, memory_mb=None, active_processes=None):
    """给 Job 设置内核级硬限制（沙盒增强）。

    与"监控采样后判定"不同，这些限制由内核强制：
    - memory_mb：Job 提交内存上限（超限后内存分配失败，程序必然异常/崩溃）
    - active_processes：Job 内同时活跃的进程数上限（防进程爆炸）
    保留 KILL_ON_JOB_CLOSE 语义。
    注：Windows 的 JOB_OBJECT_LIMIT_READ/WRITE_BYTES（磁盘 IO 字节限制）在
    部分系统上不受支持（ERROR_INVALID_PARAMETER），故未使用；
    磁盘防护由输出文件大小检查（OUTPUT_LIMIT）+ 磁盘剩余空间检查承担。
    返回 False 表示设置失败（调用方可忽略，监控仍兜底）。
    """
    if not hjob:
        return False
    try:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        flags |= JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION  # 崩溃立即终止
        if memory_mb and memory_mb > 0:
            # 注意：JOB_MEMORY 与 PROCESS_MEMORY 不能同时设置（ERROR_INVALID_PARAMETER）
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = int(memory_mb * 1048576)
        if active_processes and active_processes > 0:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = active_processes
        info.BasicLimitInformation.LimitFlags = flags
        ok = kernel32.SetInformationJobObject(
            hjob, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        return bool(ok)
    except Exception:
        return False


def close_job(hjob):
    """关闭 Job 句柄；KILL_ON_JOB_CLOSE 语义下，其中所有进程（含后代）被瞬间终止"""
    if hjob:
        try:
            kernel32.CloseHandle(hjob)
        except Exception:
            pass


def job_pids(hjob):
    """枚举 Job 对象内所有进程 PID（进程树的权威来源）。

    与 children_pids 相比，Job 通过对象引用跟踪进程，不依赖 ParentPID，
    因此不存在"父进程退出后孤儿进程被误判为子进程（PID 复用）"的问题。
    返回 None 表示查询失败（调用方应回退到 children_pids）。
    """
    if not hjob:
        return None
    try:
        # JobObjectBasicProcessIdList 缓冲区不足时返回 ERROR_BAD_LENGTH(24)，
        # 用递增的缓冲区大小重试（对拍进程树一般远小于 64 个）
        for cap in (64, 256, 1024, 4096):
            class PIDLIST(ctypes.Structure):
                _fields_ = [
                    ('NumberOfAssignedProcesses', wt.DWORD),
                    ('NumberOfProcessIdsInList', wt.DWORD),
                    ('ProcessIdList', ctypes.c_size_t * cap),
                ]
            buf = PIDLIST()
            ok = kernel32.QueryInformationJobObject(
                hjob, JobObjectBasicProcessIdList,
                ctypes.byref(buf), ctypes.sizeof(buf), None)
            if ok:
                return list(buf.ProcessIdList[:int(buf.NumberOfProcessIdsInList)])
            err = ctypes.get_last_error()
            if err != 24:  # ERROR_BAD_LENGTH：继续加大缓冲区
                return None
        return None
    except Exception:
        return None


def terminate(pid):
    """终止单个进程"""
    h = _open_process(pid, PROCESS_TERMINATE)
    try:
        kernel32.TerminateProcess(h, 1)
    finally:
        kernel32.CloseHandle(h)


def kill_proc_tree(pid):
    """终止进程及其所有子进程（多次重试，防止击杀瞬间新生的子进程逃逸成孤儿）"""
    for _ in range(6):
        kids = children_pids(pid)
        for c in kids:
            try:
                terminate(c)
            except Exception:
                pass
        try:
            terminate(pid)
        except Exception:
            pass
        if not children_pids(pid):
            return
        time.sleep(0.02)
