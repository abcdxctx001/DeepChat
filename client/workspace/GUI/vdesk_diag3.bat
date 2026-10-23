@echo off
powershell -NoProfile -Command "Write-Output 'hello from vdesk'; Get-Process notepad -ErrorAction SilentlyContinue | Select-Object Id,ProcessName | Format-List" > "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\vdesk_diag_log.txt" 2>&1
echo EXITCODE=%ERRORLEVEL% >> "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\vdesk_diag_log.txt"
