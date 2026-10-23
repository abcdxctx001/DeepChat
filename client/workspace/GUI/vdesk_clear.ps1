Add-Type @"
using System;
using System.Runtime.InteropServices;
public class EditClear {
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr p, IntPtr c, string cls, string t);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, int m, IntPtr w, string l);
}
"@
$top = [IntPtr]0x160A88
$edit = [EditClear]::FindWindowEx($top, [IntPtr]::Zero, "Edit", $null)
if ($edit -ne [IntPtr]::Zero) {
  $null = [EditClear]::SendMessage($edit, 0x000C, [IntPtr]::Zero, "")  # WM_SETTEXT ""
  Set-Content "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\cleared.txt" "CLEARED"
} else {
  Set-Content "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\cleared.txt" "NO_EDIT"
}
