// c3h_client.c - 32-bit side of the helper protocol (used by the mod and by tools/c3htest.c). Copy kept in condor3-mod/src.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "c3h_client.h"

int C3h_Start(C3hClient* c, const char* exe, const char* logPath, char* err, int errLen) {
    char name[64], cmd[MAX_PATH * 3]; DWORD pid = GetCurrentProcessId(), t0; STARTUPINFOA si; PROCESS_INFORMATION pi;
    memset(c, 0, sizeof *c);
    _snprintf(name, sizeof name, "Local\\c3h_%lu_shm", (unsigned long)pid);
    c->map = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, C3H_HEADER_SIZE + C3H_ARENA_SIZE, name);
    _snprintf(name, sizeof name, "Local\\c3h_%lu_req", (unsigned long)pid);  c->req = CreateEventA(NULL, FALSE, FALSE, name);
    _snprintf(name, sizeof name, "Local\\c3h_%lu_done", (unsigned long)pid); c->done = CreateEventA(NULL, FALSE, FALSE, name);
    if (!c->map || !c->req || !c->done) { _snprintf(err, errLen, "CreateFileMapping/CreateEvent failed (%lu)", GetLastError()); C3h_Stop(c); return 0; }
    c->hd = (C3hHeader*)MapViewOfFile(c->map, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!c->hd) { _snprintf(err, errLen, "MapViewOfFile failed (%lu)", GetLastError()); C3h_Stop(c); return 0; }
    memset(c->hd, 0, sizeof *c->hd);
    c->hd->magic = C3H_MAGIC; c->hd->version = C3H_VERSION; c->hd->header_size = C3H_HEADER_SIZE; c->hd->arena_size = C3H_ARENA_SIZE;
    c->arena = (unsigned char*)c->hd + C3H_HEADER_SIZE;
    _snprintf(cmd, sizeof cmd, "\"%s\" --pid %lu%s%s%s", exe, (unsigned long)pid, logPath ? " --log \"" : "", logPath ? logPath : "", logPath ? "\"" : "");
    memset(&si, 0, sizeof si); si.cb = sizeof si; memset(&pi, 0, sizeof pi);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) {
        _snprintf(err, errLen, "cannot start %s (%lu)", exe, GetLastError()); C3h_Stop(c); return 0;
    }
    CloseHandle(pi.hThread); c->proc = pi.hProcess;
    for (t0 = GetTickCount(); !c->hd->helper_ready; Sleep(20)) {
        if (WaitForSingleObject(c->proc, 0) == WAIT_OBJECT_0) { DWORD ec = 0; GetExitCodeProcess(c->proc, &ec); _snprintf(err, errLen, "helper exited with %lu", ec); C3h_Stop(c); return 0; }
        if (GetTickCount() - t0 > 15000) { _snprintf(err, errLen, "helper did not become ready"); C3h_Stop(c); return 0; }
    }
    return 1;
}

int C3h_Request(C3hClient* c, unsigned type, int a0, int a1, const char* path, DWORD timeoutMs) {
    DWORD t0 = GetTickCount();
    if (!c->hd) return -100;
    c->hd->req_type = type; c->hd->req_arg[0] = a0; c->hd->req_arg[1] = a1;
    if (path) { strncpy(c->hd->req_path, path, sizeof c->hd->req_path - 1); c->hd->req_path[sizeof c->hd->req_path - 1] = 0; }
    MemoryBarrier();
    c->hd->req_seq++;
    SetEvent(c->req);
    for (;;) {
        HANDLE hs[2]; DWORD w, left;
        if (c->hd->done_seq == c->hd->req_seq) break;
        left = timeoutMs - (GetTickCount() - t0);
        if ((int)left <= 0) return -101;                                   // timeout
        hs[0] = c->done; hs[1] = c->proc;
        w = WaitForMultipleObjects(2, hs, FALSE, left > 500 ? 500 : left);
        if (w == WAIT_OBJECT_0 + 1) return -102;                           // helper died
    }
    MemoryBarrier();
    return c->hd->status;
}

void C3h_Stop(C3hClient* c) {
    if (c->hd && c->proc && WaitForSingleObject(c->proc, 0) == WAIT_TIMEOUT) C3h_Request(c, C3H_REQ_QUIT, 0, 0, NULL, 2000);
    if (c->hd) UnmapViewOfFile(c->hd);
    if (c->map) CloseHandle(c->map);
    if (c->req) CloseHandle(c->req);
    if (c->done) CloseHandle(c->done);
    if (c->proc) CloseHandle(c->proc);
    memset(c, 0, sizeof *c);
}
