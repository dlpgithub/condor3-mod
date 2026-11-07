// debug monitor
//
// Simple standalone tool: listens on 127.0.0.1:DBGLOG_PORT, accepts
// connections from the injected proxy DLL, and prints every line it
// receives with a timestamp. Supports multiple/reconnecting clients
// (one at a time is the common case, but a new connection just
// replaces the old one).

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <time.h>

#pragma comment(lib, "ws2_32.lib")

#ifndef DBGLOG_PORT
#define DBGLOG_PORT 28734
#endif

static void print_timestamp(void) {
    SYSTEMTIME st;
    GetLocalTime(&st);
    printf("[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
}

int main(void) {
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        fprintf(stderr, "WSAStartup failed\n");
        return 1;
    }

    SOCKET listenSock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listenSock == INVALID_SOCKET) {
        fprintf(stderr, "socket() failed: %d\n", WSAGetLastError());
        WSACleanup();
        return 1;
    }

    int reuse = 1;
    setsockopt(listenSock, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));

    struct sockaddr_in addr;
    ZeroMemory(&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(DBGLOG_PORT);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");

    if (bind(listenSock, (struct sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
        fprintf(stderr, "bind() failed: %d (läuft schon ein Monitor?)\n", WSAGetLastError());
        closesocket(listenSock);
        WSACleanup();
        return 1;
    }

    if (listen(listenSock, 1) == SOCKET_ERROR) {
        fprintf(stderr, "listen() failed: %d\n", WSAGetLastError());
        closesocket(listenSock);
        WSACleanup();
        return 1;
    }

    printf("d3dx_proxy debug monitor - listening on 127.0.0.1:%d\n", DBGLOG_PORT);
    printf("Warte auf Verbindung von der Proxy-DLL...\n\n");

    for (;;) {
        struct sockaddr_in clientAddr;
        int clientAddrLen = sizeof(clientAddr);
        SOCKET client = accept(listenSock, (struct sockaddr*)&clientAddr, &clientAddrLen);
        if (client == INVALID_SOCKET) {
            fprintf(stderr, "accept() failed: %d\n", WSAGetLastError());
            break;
        }

        printf("-- Client verbunden --\n");

        char buf[4096];
        char lineBuf[4096];
        size_t lineLen = 0;

        for (;;) {
            int n = recv(client, buf, sizeof(buf), 0);
            if (n <= 0) {
                break; // Verbindung geschlossen oder Fehler
            }

            for (int i = 0; i < n; i++) {
                char c = buf[i];
                if (c == '\n') {
                    lineBuf[lineLen] = '\0';
                    print_timestamp();
                    printf("%s\n", lineBuf);
                    fflush(stdout);
                    lineLen = 0;
                } else if (c != '\r' && lineLen + 1 < sizeof(lineBuf)) {
                    lineBuf[lineLen++] = c;
                }
            }
        }

        printf("-- Client getrennt, warte auf neue Verbindung --\n\n");
        closesocket(client);
    }

    closesocket(listenSock);
    WSACleanup();
    return 0;
}