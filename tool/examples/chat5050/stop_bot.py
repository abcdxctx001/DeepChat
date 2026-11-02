"""结束正在运行的牛哞哞（cowsay_bot.py）进程。

    python stop_bot.py
"""
import os
import sys

try:
    import psutil
except ImportError:
    print("需要 psutil：python -m pip install psutil")
    sys.exit(1)

killed = []
ME = os.getpid()

for proc in psutil.process_iter(["name", "cmdline"]):
    name = (proc.info.get("name") or "").lower()
    args = proc.info.get("cmdline") or []
    cmd = " ".join(args)
    if proc.pid == ME or "-c" in args:
        continue
    if name.startswith("python") and "cowsay_bot.py" in cmd:
        try:
            proc.terminate()
            killed.append(proc.pid)
        except Exception as exc:  # noqa: BLE001
            print(f"终止 {proc.pid} 失败：{exc}")

if killed:
    print("已让这些牛下班：", ", ".join(map(str, killed)))
else:
    print("没有找到正在运行的牛哞哞。")
