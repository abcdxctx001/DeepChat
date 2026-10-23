$hwnd = [IntPtr]0x160A88
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$cond = [System.Windows.Automation.Condition]::TrueCondition
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
foreach ($el in $all) {
  try {
    $tp = $el.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
    $txt = $tp.DocumentRange.GetText(-1)
    if ($txt) { Write-Output ("TEXT: " + $txt) }
  } catch {}
}
