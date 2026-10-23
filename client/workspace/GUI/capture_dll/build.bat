@echo off
rem 重新编译 capture_helper.dll（需要 MinGW gcc）
rem 如果系统没有 gcc，可跳过：仓库已附带编译好的 capture_helper.dll
setlocal
set GCC=C:\mingw64\bin\gcc.exe
if exist "%GCC%" (
    "%GCC%" -shared -O2 -o "%~dp0capture_helper.dll" "%~dp0capture_dll.c" -lgdi32
    echo 编译完成: %~dp0capture_helper.dll
) else (
    echo 未找到 gcc，请安装 MinGW-w64 或将 gcc 加入 PATH 后重试
)
endlocal
pause
