#include "debug_log.h"

#ifndef NDEBUG  // Ab hier nur im Debug-Build - im Release ist diese Datei effektiv leer

// Muss vor windows.h/winsock2.h gesetzt sein, sonst deklariert MinGW
// GetTickCount64 (erst ab Vista/0x0600) nicht.
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>

#pragma comment(lib, "ws2_32.lib")

static SOCKET g_sock = INVALID_SOCKET;
static CRITICAL_SECTION g_cs;
static BOOL g_cs_ready = FALSE;
static ULONGLONG g_next_reconnect_tick = 0;
static const ULONGLONG RECONNECT_INTERVAL_MS = 2000;

static void TryConnectLocked(void) {
    if (g_sock != INVALID_SOCKET) {
        return;
    }

    ULONGLONG now = GetTickCount64();
    if (now < g_next_reconnect_tick) {
        return; // noch nicht wieder dran, Ziel-App nicht ausbremsen
    }
    g_next_reconnect_tick = now + RECONNECT_INTERVAL_MS;

    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        return;
    }

    // Kurzer Connect-Timeout, damit wir nie lange hängen, falls kein Monitor läuft.
    u_long nonblock = 1;
    ioctlsocket(s, FIONBIO, &nonblock);

    struct sockaddr_in addr;
    ZeroMemory(&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(DBGLOG_PORT);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");

    int rc = connect(s, (struct sockaddr*)&addr, sizeof(addr));
    if (rc == SOCKET_ERROR && WSAGetLastError() != WSAEWOULDBLOCK) {
        closesocket(s);
        return;
    }

    fd_set writeSet, errSet;
    FD_ZERO(&writeSet);
    FD_SET(s, &writeSet);
    FD_ZERO(&errSet);
    FD_SET(s, &errSet);

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 200 * 1000; // 200ms max Wartezeit

    int sel = select(0, NULL, &writeSet, &errSet, &tv);
    if (sel <= 0 || FD_ISSET(s, &errSet)) {
        closesocket(s);
        return;
    }

    // wieder blockierend schalten für die eigentlichen sends (schnell, lokal)
    nonblock = 0;
    ioctlsocket(s, FIONBIO, &nonblock);

    g_sock = s;
}

void DbgLogInit(void) {
    if (!g_cs_ready) {
        InitializeCriticalSection(&g_cs);
        g_cs_ready = TRUE;
    }

    WSADATA wsaData;
    WSAStartup(MAKEWORD(2, 2), &wsaData);
}

void DbgLogShutdown(void) {
    EnterCriticalSection(&g_cs);
    if (g_sock != INVALID_SOCKET) {
        closesocket(g_sock);
        g_sock = INVALID_SOCKET;
    }
    LeaveCriticalSection(&g_cs);

    WSACleanup();
}

void DbgLogWrite(const char* fmt, ...) {
    if (!g_cs_ready) {
        return; // DbgLogInit wurde nicht aufgerufen
    }

    char msg[1024];
    va_list args;
    va_start(args, fmt);
    int len = vsnprintf(msg, sizeof(msg) - 2, fmt, args);
    va_end(args);
    if (len < 0) {
        return;
    }
    if (len > (int)sizeof(msg) - 2) {
        len = (int)sizeof(msg) - 2;
    }
    msg[len++] = '\n';
    msg[len] = '\0';

    EnterCriticalSection(&g_cs);

    TryConnectLocked();

    if (g_sock != INVALID_SOCKET) {
        int sent = send(g_sock, msg, len, 0);
        if (sent == SOCKET_ERROR) {
            // Verbindung verloren (Monitor beendet o.ä.) -> aufräumen, nächstes Mal neu verbinden
            closesocket(g_sock);
            g_sock = INVALID_SOCKET;
        }
    }

    LeaveCriticalSection(&g_cs);
}

#endif // NDEBUG