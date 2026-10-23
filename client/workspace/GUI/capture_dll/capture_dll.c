/*
 * capture_dll.c — 注入到目标进程的截图辅助 DLL（最终版）
 *
 * 机制：DLL 被 LoadLibrary 注入目标进程后，DllMain 启动 worker 线程，
 * 轮询命令文件（capture_cmd.txt）。收到 "hwnd|outpath" 后：
 *   1) 顶层 + 递归子窗口 WM_PRINT（进程内 hdc + viewport 偏移）
 *      Edit/RichEdit 控件改用 EM_FORMATRANGE（打印级文本渲染，不依赖显示状态）
 *   2) PrintWindow
 *   3) GetWindowDC + BitBlt
 * 保存 BMP 并写 done 标记；调试日志写 capture_debug.txt。
 *
 * 命令/输出目录推导：DLL 位于 <PROJ>/client/capture_dll/，
 * 命令目录取 <PROJ>/client/workspace/capture/。
 *
 * 编译：gcc -shared -O2 -o capture_helper.dll capture_dll.c -lgdi32
 */
#include <windows.h>
#include <richedit.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

#ifndef SMTO_ERRORONEXIT
#define SMTO_ERRORONEXIT 0x0020
#endif

#define MAX_CAPTURE_DIRS 4
static wchar_t g_cmd_file[MAX_CAPTURE_DIRS][MAX_PATH];
static wchar_t g_done_file[MAX_CAPTURE_DIRS][MAX_PATH];
static wchar_t g_dbg_file[MAX_CAPTURE_DIRS][MAX_PATH];
static int g_dir_count = 0;
static int g_dbg_idx = 0;   /* 当前命令目录索引（dbg 写日志用） */
static int g_done_idx = 0;  /* 当前命令目录索引（写完成标记用） */

static void add_capture_dir(const wchar_t* base) {
    if (g_dir_count >= MAX_CAPTURE_DIRS || base == NULL || base[0] == 0) return;
    swprintf(g_cmd_file[g_dir_count], MAX_PATH, L"%s\\capture\\capture_cmd.txt", base);
    swprintf(g_done_file[g_dir_count], MAX_PATH, L"%s\\capture\\capture_done.txt", base);
    swprintf(g_dbg_file[g_dir_count], MAX_PATH, L"%s\\capture\\capture_debug.txt", base);
    g_dir_count++;
}

static void write_file_text(const wchar_t* path, const char* text) {
    FILE* f = _wfopen(path, L"w");
    if (f) { fputs(text, f); fclose(f); }
}

static void dbg(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (g_dbg_idx < 0 || g_dbg_idx >= g_dir_count) return;
    FILE* f = _wfopen(g_dbg_file[g_dbg_idx], L"a");
    if (f) { fprintf(f, "%s\n", buf); fclose(f); }
}

static void trim(char* s) {
    size_t n = strlen(s);
    while (n > 0 && (s[n-1] == '\n' || s[n-1] == '\r' || s[n-1] == ' ')) s[--n] = 0;
}

static LONG count_nonblack(void* bits, int w, int h) {
    unsigned char* p = (unsigned char*)bits;
    LONG count = 0;
    for (LONG i = 0; i < (LONG)w * h; i++) {
        if (p[0] || p[1] || p[2]) count++;
        p += 4;
    }
    return count;
}

static int save_bmp(void* bits, int w, int h, const char* path) {
    BITMAPFILEHEADER bfh;
    BITMAPINFOHEADER bih;
    memset(&bfh, 0, sizeof(bfh));
    memset(&bih, 0, sizeof(bih));
    bih.biSize = sizeof(BITMAPINFOHEADER);
    bih.biWidth = w;
    bih.biHeight = -h;
    bih.biPlanes = 1;
    bih.biBitCount = 32;
    bih.biCompression = BI_RGB;
    bih.biSizeImage = w * h * 4;
    bfh.bfType = 0x4D42;
    bfh.bfSize = sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER) + bih.biSizeImage;
    bfh.bfOffBits = sizeof(BITMAPFILEHEADER) + sizeof(BITMAPINFOHEADER);
    FILE* f = fopen(path, "wb");
    if (!f) return 0;
    fwrite(&bfh, 1, sizeof(bfh), f);
    fwrite(&bih, 1, sizeof(bih), f);
    fwrite(bits, 1, bih.biSizeImage, f);
    fclose(f);
    return 1;
}

/* ============ 子窗口递归捕获 ============ */
static BOOL CALLBACK child_enum_cb(HWND hwnd, LPARAM lparam);
static HDC g_main_hdc = NULL;   /* do_capture 的主内存 DC，子窗口内容合成到这里 */

/* 对单个窗口（含子窗口）渲染到 tmp（viewport 已偏移） */
static void render_window(HWND win, HDC tmp, DWORD_PTR* pres) {
    wchar_t cls[128] = {0};
    GetClassNameW(win, cls, 128);
    DWORD_PTR res = 0;
    if (wcsstr(cls, L"RichEdit") != NULL) {
        /* RichEdit：EM_FORMATRANGE 打印级渲染 */
        RECT cr;
        GetClientRect(win, &cr);
        FORMATRANGE fr;
        memset(&fr, 0, sizeof(fr));
        fr.hdc = tmp;
        fr.hdcTarget = tmp;
        fr.rc = cr;
        fr.rcPage = cr;
        fr.chrg.cpMin = -1;
        fr.chrg.cpMax = -1;
        SendMessageTimeoutW(win, EM_FORMATRANGE, TRUE, (LPARAM)&fr,
            SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, 5000, &res);
        SendMessageTimeoutW(win, EM_FORMATRANGE, FALSE, 0,
            SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, 3000, &res);
    } else if (wcsstr(cls, L"Edit") != NULL) {
        /* 经典 Edit：WM_GETTEXT + DrawText（不依赖显示状态） */
        RECT cr;
        GetClientRect(win, &cr);
        HBRUSH wb = CreateSolidBrush(RGB(255, 255, 255));
        FillRect(tmp, &cr, wb);
        DeleteObject(wb);
        int len = (int)SendMessageW(win, WM_GETTEXTLENGTH, 0, 0);
        dbg("edit %p gettextlen=%d", win, len);
        if (len > 0) {
            wchar_t* buf = (wchar_t*)malloc(((size_t)len + 1) * sizeof(wchar_t));
            if (buf) {
                LRESULT gt = SendMessageW(win, WM_GETTEXT, len + 1, (LPARAM)buf);
                HFONT font = (HFONT)SendMessageW(win, WM_GETFONT, 0, 0);
                HFONT oldf = (HFONT)SelectObject(tmp, font ? font : (HFONT)GetStockObject(DEFAULT_GUI_FONT));
                SetBkMode(tmp, TRANSPARENT);
                SetTextColor(tmp, RGB(0, 0, 0));
                RECT cr2 = cr;
                int dt = DrawTextW(tmp, buf, -1, &cr2, DT_LEFT | DT_TOP | DT_WORDBREAK | DT_NOPREFIX);
                dbg("edit %p gettext=%ld dt=%d first=%ls", win, (long)gt, dt, buf);
                SelectObject(tmp, oldf);
                free(buf);
            }
        }
        res = 1;
    } else {
        SendMessageTimeoutW(win, WM_PRINT, (WPARAM)tmp,
            PRF_NONCLIENT | PRF_CLIENT | PRF_ERASEBKGND,
            SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, 3000, &res);
    }
    if (pres) *pres = res;
}

/* 把子窗口渲染并合成到主 hdc，然后递归 */
static void capture_one_child(HWND child, HWND top, HDC hdc, int w, int h) {
    RECT pr, tr;
    if (!GetWindowRect(child, &pr)) return;
    if (!GetWindowRect(top, &tr)) return;
    int ox = pr.left - tr.left;
    int oy = pr.top - tr.top;
    HDC tmp = CreateCompatibleDC(hdc);
    HBITMAP bmp = CreateCompatibleBitmap(tmp, w, h);
    HGDIOBJ old = SelectObject(tmp, bmp);
    RECT r2 = {0, 0, w, h};
    FillRect(tmp, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));
    SetViewportOrgEx(tmp, ox, oy, NULL);
    DWORD_PTR res = 0;
    render_window(child, tmp, &res);
    SetViewportOrgEx(tmp, 0, 0, NULL);
    int bw = w - ox; if (bw > w) bw = w;
    int bh = h - oy; if (bh > h) bh = h;
    BitBlt(hdc, ox, oy, bw, bh, tmp, ox, oy, SRCCOPY);
    /* 调试：统计该子窗口渲染后的非零像素 */
    {
        void* dbg_bits = malloc((size_t)w * h * 4);
        if (dbg_bits) {
            BITMAPINFO db;
            memset(&db, 0, sizeof(db));
            db.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
            db.bmiHeader.biWidth = w;
            db.bmiHeader.biHeight = -h;
            db.bmiHeader.biPlanes = 1;
            db.bmiHeader.biBitCount = 32;
            db.bmiHeader.biCompression = BI_RGB;
            GetDIBits(tmp, bmp, 0, h, dbg_bits, &db, DIB_RGB_COLORS);
            wchar_t cls[128] = {0};
            GetClassNameW(child, cls, 128);
            char cls_a[256];
            WideCharToMultiByte(CP_ACP, 0, cls, -1, cls_a, 256, NULL, NULL);
            dbg("child hwnd=%p cls=%s off=(%d,%d) sm=%ld n=%ld", child, cls_a, ox, oy, (long)res, count_nonblack(dbg_bits, w, h));
            free(dbg_bits);
        }
    }
    SelectObject(tmp, old);
    DeleteObject(bmp);
    DeleteDC(tmp);
    EnumChildWindows(child, (WNDENUMPROC)child_enum_cb, (LPARAM)top);
}

static BOOL CALLBACK child_enum_cb(HWND hwnd, LPARAM lparam) {
    HWND top = (HWND)lparam;
    RECT tr;
    if (GetWindowRect(top, &tr)) {
        int w = tr.right - tr.left;
        int h = tr.bottom - tr.top;
        if (g_main_hdc) {
            capture_one_child(hwnd, top, g_main_hdc, w, h);
        }
    }
    return TRUE;
}

static void do_capture(HWND hwnd, const char* path) {
    RECT rc;
    if (!IsWindow(hwnd)) { write_file_text(g_done_file[g_done_idx], "err:invalid-hwnd"); return; }
    if (!GetWindowRect(hwnd, &rc)) { write_file_text(g_done_file[g_done_idx], "err:getrect"); return; }
    int w = rc.right - rc.left;
    int h = rc.bottom - rc.top;
    if (w <= 0 || h <= 0) { write_file_text(g_done_file[g_done_idx], "err:size"); return; }

    HDC hdc = CreateCompatibleDC(NULL);
    HBITMAP hbmp = CreateCompatibleBitmap(hdc, w, h);
    HGDIOBJ old = SelectObject(hdc, hbmp);
    RECT r2 = {0, 0, w, h};
    FillRect(hdc, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));

    BITMAPINFO bmi;
    memset(&bmi, 0, sizeof(bmi));
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = w;
    bmi.bmiHeader.biHeight = -h;
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;
    void* bits = malloc((size_t)w * h * 4);
    if (!bits) { write_file_text(g_done_file[g_done_idx], "err:malloc"); SelectObject(hdc, old); DeleteObject(hbmp); DeleteDC(hdc); return; }

    char result[256];
    LONG n = 0;

    /* 方法1: 顶层 + 子窗口 WM_PRINT / EM_FORMATRANGE */
    FillRect(hdc, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));
    DWORD_PTR smResult = 0;
    render_window(hwnd, hdc, &smResult);
    {
        HDC tmp_top = CreateCompatibleDC(hdc);
        HBITMAP bmp_top = CreateCompatibleBitmap(tmp_top, w, h);
        HGDIOBJ old_top = SelectObject(tmp_top, bmp_top);
        FillRect(tmp_top, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));
        SetViewportOrgEx(tmp_top, 0, 0, NULL);
        DWORD_PTR res2 = 0;
        render_window(hwnd, tmp_top, &res2);
        SetViewportOrgEx(tmp_top, 0, 0, NULL);
        BitBlt(hdc, 0, 0, w, h, tmp_top, 0, 0, SRCCOPY);
        SelectObject(tmp_top, old_top);
        DeleteObject(bmp_top);
        DeleteDC(tmp_top);
    }
    /* 子窗口 */
    g_main_hdc = hdc;
    EnumChildWindows(hwnd, (WNDENUMPROC)child_enum_cb, (LPARAM)hwnd);
    g_main_hdc = NULL;
    GetDIBits(hdc, hbmp, 0, h, bits, &bmi, DIB_RGB_COLORS);
    n = count_nonblack(bits, w, h);
    dbg("top hwnd=%p size=%dx%d sm=%ld n=%ld", hwnd, w, h, (long)smResult, n);
    if (n > (LONG)(w * h) / 100) {
        if (save_bmp(bits, w, h, path)) {
            sprintf(result, "ok:wmprint:%ld:%d:%d:sm=%ld", n, w, h, (long)smResult);
        } else {
            sprintf(result, "err:save-wmprint");
        }
        write_file_text(g_done_file[g_done_idx], result);
        free(bits);
        SelectObject(hdc, old); DeleteObject(hbmp); DeleteDC(hdc);
        return;
    }

    /* 方法2: PrintWindow */
    FillRect(hdc, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));
    BOOL pw = PrintWindow(hwnd, hdc, 0);
    GetDIBits(hdc, hbmp, 0, h, bits, &bmi, DIB_RGB_COLORS);
    n = count_nonblack(bits, w, h);
    if (n > (LONG)(w * h) / 100) {
        if (save_bmp(bits, w, h, path)) {
            sprintf(result, "ok:printwindow:%ld:%d:%d:pw=%d", n, w, h, (int)pw);
        } else {
            sprintf(result, "err:save-pw");
        }
        write_file_text(g_done_file[g_done_idx], result);
        free(bits);
        SelectObject(hdc, old); DeleteObject(hbmp); DeleteDC(hdc);
        return;
    }

    /* 方法3: GetWindowDC + BitBlt */
    FillRect(hdc, &r2, (HBRUSH)GetStockObject(BLACK_BRUSH));
    HDC hdc_win = GetWindowDC(hwnd);
    BOOL bb = FALSE;
    if (hdc_win) {
        bb = BitBlt(hdc, 0, 0, w, h, hdc_win, 0, 0, SRCCOPY);
        ReleaseDC(hwnd, hdc_win);
    }
    GetDIBits(hdc, hbmp, 0, h, bits, &bmi, DIB_RGB_COLORS);
    n = count_nonblack(bits, w, h);
    if (n > (LONG)(w * h) / 100) {
        if (save_bmp(bits, w, h, path)) {
            sprintf(result, "ok:gwdc:%ld:%d:%d:bb=%d", n, w, h, (int)bb);
        } else {
            sprintf(result, "err:save-gwdc");
        }
    } else {
        sprintf(result, "err:blank:wmprint=%ld:pw=%d:gwdc=%d", n, (int)pw, (int)bb);
    }
    write_file_text(g_done_file[g_done_idx], result);
    free(bits);
    SelectObject(hdc, old);
    DeleteObject(hbmp);
    DeleteDC(hdc);
}

static DWORD WINAPI worker(LPVOID param) {
    (void)param;
    for (;;) {
        Sleep(150);
        for (int i = 0; i < g_dir_count; i++) {
            FILE* f = _wfopen(g_cmd_file[i], L"r");
            if (!f) continue;
            char line[2048];
            if (fgets(line, sizeof(line), f)) {
                fclose(f);
                _wremove(g_cmd_file[i]);
                trim(line);
                if (line[0] == 0) continue;
                char* sep = strchr(line, '|');
                if (!sep) { write_file_text(g_done_file[i], "err:format"); continue; }
                *sep = 0;
                HWND hwnd = (HWND)strtoull(line, NULL, 16);
                char* path = sep + 1;
                trim(path);
                g_dbg_idx = i;
                g_done_idx = i;
                do_capture(hwnd, path);
            } else {
                fclose(f);
            }
        }
    }
    return 0;
}

static void derive_paths(HINSTANCE hinst) {
    wchar_t dll_path[MAX_PATH];
    GetModuleFileNameW(hinst, dll_path, MAX_PATH);
    wchar_t* slash = wcsrchr(dll_path, L'\\');
    if (slash) *slash = 0;  /* 去掉 capture_helper.dll */
    slash = wcsrchr(dll_path, L'\\');
    if (slash) *slash = 0;  /* 去掉 capture_dll */
    slash = wcsrchr(dll_path, L'\\');
    if (slash) *slash = 0;  /* 去掉 GUI（新结构：<workspace>/GUI/capture_dll/capture_helper.dll） */
    /* 候选1：DLL 向上推导出的 <workspace>（源码部署 / client.zip 完整包） */
    add_capture_dir(dll_path);
    /* 候选2：exe 所在目录（打包版 agent.exe 旁 workspace/capture） */
    wchar_t exe_path[MAX_PATH];
    if (GetModuleFileNameW(NULL, exe_path, MAX_PATH) > 0) {
        slash = wcsrchr(exe_path, L'\\');
        if (slash) *slash = 0;
        add_capture_dir(exe_path);
    }
    /* 候选3：当前工作目录（notepad 等由 agent 以 workspace 为工作目录启动） */
    wchar_t cwd[MAX_PATH];
    if (GetCurrentDirectoryW(MAX_PATH, cwd) > 0) {
        add_capture_dir(cwd);
    }
    for (int i = 0; i < g_dir_count; i++) {
        _wremove(g_dbg_file[i]);
    }
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        derive_paths(hinst);
        CreateThread(NULL, 0, worker, NULL, 0, NULL);
    }
    return TRUE;
}
