$log = "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\vdesk_diag_log.txt"
Set-Content $log "START $(Get-Date -Format 'HH:mm:ss')"
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class VDiag2 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr hWnd, EnumWindowsProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int n);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder sb, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, StringBuilder l);
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr c, string cls, string t);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  public struct RECT { public int L, T, R, B; }
  public static string WinText(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
  public static string WinClass(IntPtr h) { var sb = new StringBuilder(256); GetClassName(h, sb, 256); return sb.ToString(); }
}
"@

$lines = New-Object System.Collections.Generic.List[string]
$cb = {
  param($h, $l)
  $r = New-Object VDiag2+RECT
  [VDiag2]::GetWindowRect($h, [ref]$r) | Out-Null
  $line = "TOP hwnd=0x{0:X} class={1} title=[{2}] rect=({3},{4},{5},{6}) vis={7} ico={8}" -f `
    $h.ToInt64(), [VDiag2]::WinClass($h), [VDiag2]::WinText($h), $r.L, $r.T, $r.R, $r.B, `
    [VDiag2]::IsWindowVisible($h), [VDiag2]::IsIconic($h)
  $script:lines.Add($line)
  return $true
}
$null = [VDiag2]::EnumWindows($cb, [IntPtr]::Zero)

# 检查记事本 Edit 控件
$hwndList = @()
$cb2 = { param($h, $l) $script:hwndList += $h; return $true }
$null = [VDiag2]::EnumWindows($cb2, [IntPtr]::Zero)
foreach ($top in $hwndList) {
  if ([VDiag2]::WinClass($top) -eq "Notepad") {
    $edit = [VDiag2]::FindWindowEx($top, [IntPtr]::Zero, "Edit", $null)
    if ($edit -ne [IntPtr]::Zero) {
      $len = [VDiag2]::SendMessage($edit, 0x000E, [IntPtr]::Zero, [IntPtr]::Zero).ToInt32()
      $buf = New-Object System.Text.StringBuilder ($len + 2)
      $null = [VDiag2]::SendMessage($edit, 0x000D, [IntPtr]($buf.Capacity), $buf)
      $lines.Add("NOTEPAD EDIT len=$len text=[$($buf.ToString())]")
    } else {
      $lines.Add("NOTEPAD no Edit control; children:")
      $cb3 = { param($h, $l) $script:lines.Add("  CHILD class=$([VDiag2]::WinClass($h)) title=[$([VDiag2]::WinText($h))]"); return $true }
      $null = [VDiag2]::EnumChildWindows($top, $cb3, [IntPtr]::Zero)
    }
  }
}

Add-Content $log ($lines -join "`r`n")
Add-Content $log "END"
