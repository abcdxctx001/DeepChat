$src = @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NPRead {
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr hwndParent, IntPtr hwndChildAfter, string lpszClass, string lpszWindow);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, StringBuilder lParam);
  public static string Read(IntPtr hwnd) {
    IntPtr edit = FindWindowEx(hwnd, IntPtr.Zero, "Edit", null);
    if (edit == IntPtr.Zero) return "NO_EDIT";
    int len = (int)SendMessage(edit, 0x000E, IntPtr.Zero, IntPtr.Zero);
    StringBuilder sb = new StringBuilder(len + 2);
    SendMessage(edit, 0x000D, new IntPtr(sb.Capacity), sb);
    return sb.ToString();
  }
}
"@
Add-Type -TypeDefinition $src
$h = [IntPtr]0x1D09A6
$txt = [NPRead]::Read($h)
Write-Output "===CONTENT START==="
Write-Output $txt
Write-Output "===CONTENT END (length $($txt.Length))==="
