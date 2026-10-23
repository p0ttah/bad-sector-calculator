/*
 * Bad Sector Calculator - launcher
 *
 * Starts the bundled Python (app\runtime\pythonw.exe) on app\bad_sector_calculator.pyw,
 * passing through any file dropped onto the .exe. No console window.
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>

#define TITLE L"Bad Sector Calculator"

static int file_exists(const wchar_t *path)
{
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, LPWSTR args, int show)
{
    static wchar_t dir[32768], python[32768], script[32768], cmd[32768];
    DWORD n = GetModuleFileNameW(NULL, dir, 32768);
    if (n == 0 || n >= 32768) {
        MessageBoxW(NULL, L"Couldn't work out where the app is installed.", TITLE, MB_ICONERROR);
        return 1;
    }
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    swprintf(python, 32768, L"%ls\\app\\runtime\\pythonw.exe", dir);
    swprintf(script, 32768, L"%ls\\app\\bad_sector_calculator.pyw", dir);

    if (!file_exists(python) || !file_exists(script)) {
        MessageBoxW(NULL,
            L"The app's files are missing.\n\n"
            L"Keep the \"app\" folder in the same folder as \"Bad Sector Calculator.exe\" - "
            L"copy the whole \"Bad Sector Calculator\" folder, not just the .exe.",
            TITLE, MB_ICONERROR);
        return 1;
    }

    swprintf(cmd, 32768, L"\"%ls\" -I \"%ls\" %ls", python, script, args ? args : L"");

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(python, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        wchar_t msg[512];
        swprintf(msg, 512, L"Couldn't start the app (Windows error %lu).\n\n"
                           L"If your organisation blocks programs run from the Desktop, ask IT "
                           L"to allow this folder.", GetLastError());
        MessageBoxW(NULL, msg, TITLE, MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
