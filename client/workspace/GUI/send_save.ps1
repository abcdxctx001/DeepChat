$src = @"
using System;
using System.Runtime.InteropServices;
public class WinMsg {
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
Add-Type -TypeDefinition $src
# 记事本菜单命令: 文件->另存为 0x1F05, 保存 0x1F04
$hwnd = [IntPtr]0x160A88
[WinMsg]::PostMessage($hwnd, 0x0111, [IntPtr]0x1F05, [IntPtr]::Zero) | Out-Null
Write-Output "WM_COMMAND sent"
