Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PW3 {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetDC(IntPtr h);
  [DllImport("user32.dll")] public static extern int ReleaseDC(IntPtr h, IntPtr dc);
  [DllImport("user32.dll")] public static extern bool BitBlt(IntPtr hdc, int x, int y, int w, int h, IntPtr src, int sx, int sy, int rop);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  public struct RECT { public int L, T, R, B; }
}
"@
$dir = "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\screenshots"
$hwnd = [IntPtr]0x160A88
$r = New-Object PW3+RECT
$null = [PW3]::GetWindowRect($hwnd, [ref]$r)
$w = $r.R - $r.L; $h = $r.B - $r.T

# 1) PrintWindow flags=0
$bmp1 = New-Object System.Drawing.Bitmap $w, $h
$g1 = [System.Drawing.Graphics]::FromImage($bmp1)
$hdc1 = $g1.GetHdc()
$ok1 = [PW3]::PrintWindow($hwnd, $hdc1, 0)
$g1.ReleaseHdc($hdc1); $g1.Dispose()
$bmp1.Save("$dir\notepad_pw0.png", [System.Drawing.Imaging.ImageFormat]::Png)
$bmp1.Dispose()

# 2) BitBlt 整个虚拟桌面
$sw = [PW3]::GetSystemMetrics(0); $sh = [PW3]::GetSystemMetrics(1)
$bmp2 = New-Object System.Drawing.Bitmap $sw, $sh
$g2 = [System.Drawing.Graphics]::FromImage($bmp2)
$hdc2 = $g2.GetHdc()
$srcdc = [PW3]::GetDC([IntPtr]::Zero)
$ok2 = [PW3]::BitBlt($hdc2, 0, 0, $sw, $sh, $srcdc, 0, 0, 0x00CC0020)  # SRCCOPY
$null = [PW3]::ReleaseDC([IntPtr]::Zero, $srcdc)
$g2.ReleaseHdc($hdc2); $g2.Dispose()
$bmp2.Save("$dir\vdesk_bb.png", [System.Drawing.Imaging.ImageFormat]::Png)
$bmp2.Dispose()

Set-Content "$dir\pw_info2.txt" "PW0=$ok1 BITBLT=$ok2 screens=${sw}x${sh}"
