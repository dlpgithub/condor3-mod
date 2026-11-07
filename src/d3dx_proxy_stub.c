#include <windows.h>
#include <initguid.h>
#include "dobby.h"
#include "debug_log.h"
#include "install_patch.h"

DEFINE_GUID(IID_IDirect3DRefDevice,
    0x50936643, 0x13e9, 0x11d1,
    0x89, 0xaa, 0x00, 0xa0, 0xc9, 0x05, 0x41, 0x29);

HINSTANCE g_hModule = NULL;

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD reason, LPVOID lpReserved) {
    switch (reason) {
    case DLL_PROCESS_ATTACH:
        g_hModule = hinstDLL;
        DbgLogInit();
        DBGLOG("d3dx_proxy attached (pid=%lu)", GetCurrentProcessId());
        InstallPatches();
        break;
    case DLL_PROCESS_DETACH:
        DBGLOG("d3dx_proxy detaching (pid=%lu)", GetCurrentProcessId());
        DbgLogShutdown();
        break;
    }
    return TRUE;
}