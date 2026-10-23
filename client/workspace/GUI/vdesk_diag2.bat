@echo off
powershell -NoProfile -Command "Get-Process notepad,calc,mspaint -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,MainWindowTitle,MainWindowHandle | Format-List" > "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\vdesk_diag_log.txt" 2>&1
echo EXITCODE=%ERRORLEVEL% >> "C:\Users\Administrator\Downloads\deepseek_chat_ui\client\workspace\vdesk_diag_log.txt"
