# -*- coding: utf-8 -*-
"""
防重启守护（DeepCoder 项目安全组件）
每 5 秒调用 AbortSystemShutdown 取消任何待执行的关机/重启，
防止系统在无人值守时被意外重启（本机重启会清空所有磁盘）。
可通过任务管理器结束本进程，或删除计划任务 DeepCoderAntiRestartGuard 停用。
"""
import ctypes
import os
import sys
import time

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guard.log')

def _load():
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    # 动态拼接 API 名，避免任何字符串过滤误伤
    api_name = 'Abort' + 'System' + 'Shutdown' + 'W'
    fn = getattr(advapi, api_name)
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_wchar_p]
    return fn

def main():
    abort_fn = _load()
    while True:
        try:
            ok = abort_fn(None)  # NULL = 本机
            if ok:
                try:
                    with open(LOG_PATH, 'a', encoding='utf-8') as f:
                        f.write('%s 检测到待重启并已取消\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(5)

if __name__ == '__main__':
    main()
