$src = @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NotepadReader {
  [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr hwndParent, IntPtr hwndChildAfter, string lpszClass, string lpszWindow);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, StringBuilder lParam);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
  public static string Read() {
    IntPtr hwnd = new IntPtr(0x160A88);
    IntPtr edit = FindWindowEx(hwnd, IntPtr.Zero, "Edit", null);
    if (edit == IntPtr.Zero) return "NO_EDIT_FOUND";
    int len = (int)SendMessage(edit, 0x000E, IntPtr.Zero, IntPtr.Zero); // WM_GETTEXTLENGTH
    StringBuilder sb = new StringBuilder(len + 1);
    SendMessage(edit, 0x000D, new IntPtr(sb.Capacity), sb); // WM_GETTEXT
    return sb.ToString();
  }
}
"@
Add-Type -TypeDefinition $src
[NotepadReader]::Read()
