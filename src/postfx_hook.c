// postfx_hook.c - plugs the post-processing chain (postfx_core.c) into Condor 3.
//
// How a Condor frame is organised (found with REA/Ghidra, addresses are IDA/Ghidra addresses, base 0x400000):
//
//   0x6D6BB4  frame function           for every window: bind its buffers (0x77DB00), scene (0x73B59C),
//                                      HUD (0x6E7404), Present (0x77DA20)
//   0x73B59C  scene                    land, sky, airports, shadows, trees, clouds, glider, sun, flares
//   0x6E7404  HUD                      <- we run just before this: the scene is complete, the HUD is not
//                                         drawn yet, so text and instruments are never post-processed
//
//   D3D11 objects are plain globals:   0x7DA0EC ID3D11Device*   0x7DA0F4 ID3D11DeviceContext*
//   swap chain manager:                *(*(0x793998)) -> +8 current index, +0xC array of items
//   item (one per swap chain):         +0x14 IDXGISwapChain  +0x18 RTV  +0x1C depth texture (R24G8_TYPELESS,
//                                      DSV + SRV bind)  +0x20 backbuffer texture (multisampled when MSAA is
//                                      on)  +0x24 DSV  +0x28 depth SRV (multisampled)
//   camera manager:                    *(0x7D96F8); D3DX projection matrix at +0x188 (view at +0x108)
//
// Hotkeys (while the game window has focus):  F8 reload postfx.hlsl   F9 post-processing on/off
//                                              F10 write postfx\capture.bin (frame for offline tuning)
#define COBJMACROS
#include "install_patch.h"
#include "dobby.h"
#include "debug_log.h"
#include "postfx_core.h"

#include <d3d11.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>

#define ADDR_HUD_DRAW     0x006E7404
#define ADDR_DEVICE_VAR   0x007DA0EC
#define ADDR_CONTEXT_VAR  0x007DA0F4
#define ADDR_SWAPMGR_PTR  0x00793998
#define ADDR_CAMMGR_VAR   0x007D96F8

#define MGR_OFF_INDEX     0x08
#define MGR_OFF_ITEMS     0x0C
#define ITEM_OFF_COLOR    0x20
#define ITEM_OFF_DEPTH    0x1C
#define ITEM_OFF_RTV      0x18
#define CAM_OFF_PROJ      0x188

static PfxCore* g_core = NULL;
static int      g_enabled = 1;
static int      g_dead = 0;           // gave up (fatal problem)
static int      g_fails = 0;
static char     g_dir[MAX_PATH];      // <exedir>\postfx
static unsigned g_frames = 0;
static FILE*    g_logf = NULL;

// ---- world depth capture -------------------------------------------------------------------------------
// The engine clears the depth buffer between the world and the cockpit, so at the HUD hook only the cockpit is
// left. We patch ID3D11DeviceContext::ClearDepthStencilView (vtable slot 53) and copy the depth buffer just before
// every clear of the main depth texture; the last copy of a frame is the world (see PfxCore_Run: max(depth, world)).
#define VT_CLEAR_DSV 53
typedef void (STDMETHODCALLTYPE *ClearDsvFn)(ID3D11DeviceContext*, ID3D11DepthStencilView*, UINT, FLOAT, UINT8);
static ClearDsvFn g_origClear = NULL;
static ID3D11Texture2D* g_mainDepth = NULL;   // watched (set by the HUD hook, not owned)
static ID3D11Texture2D* g_worldCopy = NULL;   // owned
static unsigned g_clearsThisFrame = 0, g_copiesThisFrame = 0, g_worldValid = 0;
static int g_clearLogN = 0;

static void STDMETHODCALLTYPE ClearDsvHook(ID3D11DeviceContext* self, ID3D11DepthStencilView* dsv, UINT flags, FLOAT d, UINT8 st) {
    if (dsv && g_mainDepth && g_enabled) {
        ID3D11Resource* res = NULL;
        ID3D11DepthStencilView_GetResource(dsv, &res);
        if (res) {
            int isMain = ((void*)res == (void*)g_mainDepth);
            if (g_logf && g_clearLogN < 60) {
                g_clearLogN++;
                fprintf(g_logf, "postfx: frame %u clear dsv res=%p main=%d flags=%u depth=%g\n", g_frames, (void*)res, isMain, flags, d);
                fflush(g_logf);
            }
            if (isMain && (flags & D3D11_CLEAR_DEPTH)) {
                D3D11_TEXTURE2D_DESC md, wd;
                ID3D11Device* dev = NULL;
                g_clearsThisFrame++;
                ID3D11Texture2D_GetDesc(g_mainDepth, &md);
                if (g_worldCopy) { ID3D11Texture2D_GetDesc(g_worldCopy, &wd); if (wd.Width != md.Width || wd.Height != md.Height || wd.SampleDesc.Count != md.SampleDesc.Count || wd.Format != md.Format) { ID3D11Texture2D_Release(g_worldCopy); g_worldCopy = NULL; } }
                if (!g_worldCopy) {
                    ID3D11DeviceContext_GetDevice(self, &dev);
                    if (dev) {
                        md.BindFlags = D3D11_BIND_SHADER_RESOURCE; md.Usage = D3D11_USAGE_DEFAULT; md.CPUAccessFlags = 0; md.MiscFlags = 0;
                        if (FAILED(ID3D11Device_CreateTexture2D(dev, &md, NULL, &g_worldCopy)) && g_logf) { fprintf(g_logf, "postfx: world depth copy creation failed\n"); fflush(g_logf); }
                        ID3D11Device_Release(dev);
                    }
                }
                if (g_worldCopy) { ID3D11DeviceContext_CopyResource(self, (ID3D11Resource*)g_worldCopy, res); g_copiesThisFrame++; g_worldValid = 1; }
            }
            ID3D11Resource_Release(res);
        }
    }
    g_origClear(self, dsv, flags, d, st);
}

static int g_clearHooked = 0;
static void HookClear(ID3D11DeviceContext* ctx) {
    void** vt; DWORD old;
    if (g_clearHooked || !ctx) return;
    g_clearHooked = 1;
    vt = *(void***)ctx;
    if (!VirtualProtect(&vt[VT_CLEAR_DSV], sizeof(void*), PAGE_READWRITE, &old)) { if (g_logf) { fprintf(g_logf, "postfx: VirtualProtect failed\n"); fflush(g_logf); } return; }
    g_origClear = (ClearDsvFn)vt[VT_CLEAR_DSV];
    vt[VT_CLEAR_DSV] = (void*)ClearDsvHook;
    VirtualProtect(&vt[VT_CLEAR_DSV], sizeof(void*), old, &old);
    if (g_logf) { fprintf(g_logf, "postfx: ClearDepthStencilView hooked (vtable %p, orig %p)\n", (void*)vt, (void*)g_origClear); fflush(g_logf); }
}


static void Diag(const char* fmt, ...) {
    static int n = 0;
    va_list ap;
    if (!g_logf || n >= 40 || (g_frames % 120) != 1) return;
    n++;
    va_start(ap, fmt); vfprintf(g_logf, fmt, ap); va_end(ap);
    fputc('\n', g_logf); fflush(g_logf);
}

static BOOL Rd32(uintptr_t addr, uint32_t* out) {
    if (!addr || IsBadReadPtr((const void*)addr, 4)) return FALSE;
    *out = *(volatile const uint32_t*)addr;
    return TRUE;
}

static void Key(int vk, int* prev, int* edge) {
    int down = (GetAsyncKeyState(vk) & 0x8000) != 0;
    *edge = down && !*prev;
    *prev = down;
}

static int ReadProjection(PfxCamera* cam) {
    uint32_t camMgr = 0;
    const float* m;
    if (!Rd32((uintptr_t)IdaAddrToRuntime(ADDR_CAMMGR_VAR), &camMgr) || !camMgr) { Diag("postfx: camera manager unreadable"); return 0; }
    m = (const float*)(uintptr_t)(camMgr + CAM_OFF_PROJ);
    if (IsBadReadPtr(m, 64)) { Diag("postfx: projection unreadable (cam=%08x)", camMgr); return 0; }
    cam->m00 = m[0]; cam->m11 = m[5]; cam->m22 = m[10]; cam->m32 = m[14];
    Diag("postfx: cam=%08x proj m00=%g m11=%g m22=%g m32=%g", camMgr, m[0], m[5], m[10], m[14]);
    if (!(cam->m00 > 0.05f && cam->m00 < 50.f && cam->m11 > 0.05f && cam->m11 < 50.f)) return 0;
    if (!(fabsf(cam->m32) > 1e-6f) || !(fabsf(cam->m22) < 2.0f)) return 0;   // forward (m32<0) or reversed (m32>0) Z
    return 1;
}

static void HudPre(void* address, DobbyRegisterContext* rc) {
    static int kF8, kF9, kF10;
    int e8, e9, e10;
    uint32_t dev = 0, ctx = 0, p1 = 0, mgr = 0, idx = 0, arr = 0, item = 0, color = 0, depth = 0, rtv = 0;
    PfxCamera cam;
    (void)address; (void)rc;

    if (g_dead) return;
    g_frames++;

    Key(VK_F8, &kF8, &e8); Key(VK_F9, &kF9, &e9); Key(VK_F10, &kF10, &e10);
    if (e9) { g_enabled = !g_enabled; DBGLOG("postfx: %s", g_enabled ? "on" : "off"); if (g_logf) { fprintf(g_logf, "postfx: %s\n", g_enabled ? "on" : "off"); fflush(g_logf); } }
    if (!g_enabled) return;

    if (!Rd32((uintptr_t)IdaAddrToRuntime(ADDR_DEVICE_VAR), &dev) || !dev) { Diag("postfx: no device"); return; }
    if (!Rd32((uintptr_t)IdaAddrToRuntime(ADDR_CONTEXT_VAR), &ctx) || !ctx) { Diag("postfx: no context"); return; }

    if (!Rd32((uintptr_t)IdaAddrToRuntime(ADDR_SWAPMGR_PTR), &p1) || !Rd32(p1, &mgr) || !mgr) { Diag("postfx: no swap manager (p1=%08x)", p1); return; }
    if (!Rd32(mgr + MGR_OFF_INDEX, &idx) || !Rd32(mgr + MGR_OFF_ITEMS, &arr) || !arr) { Diag("postfx: manager layout (mgr=%08x)", mgr); return; }
    if (!Rd32(arr + idx * 4, &item) || !item) { Diag("postfx: no item (arr=%08x idx=%u)", arr, idx); return; }
    if (!Rd32(item + ITEM_OFF_COLOR, &color) || !Rd32(item + ITEM_OFF_DEPTH, &depth) || !Rd32(item + ITEM_OFF_RTV, &rtv)) return;
    Diag("postfx: dev=%08x ctx=%08x item=%08x color=%08x depth=%08x rtv=%08x", dev, ctx, item, color, depth, rtv);
    if (!color || !depth || !rtv) return;
    g_mainDepth = (ID3D11Texture2D*)(uintptr_t)depth;
    HookClear((ID3D11DeviceContext*)(uintptr_t)ctx);
    if (!ReadProjection(&cam)) { Diag("postfx: projection rejected"); return; }
    cam.time = (float)(GetTickCount() % 3600000u) / 1000.0f;

    if (!g_core) {
        char hlsl[MAX_PATH];
        _snprintf(hlsl, sizeof hlsl, "%s\\postfx.hlsl", g_dir);
        g_core = PfxCore_Create((ID3D11Device*)(uintptr_t)dev, hlsl);
        if (!g_core) { g_dead = 1; return; }
    }
    if (e8) PfxCore_RequestReload(g_core);
    if (e10 || g_frames == 600) {      // F10, or automatically once ~10 s after start (diagnostics)
        char path[MAX_PATH];
        _snprintf(path, sizeof path, "%s\\capture.bin", g_dir);
        PfxCore_RequestCapture(g_core, path);
    }

    if (PfxCore_Run(g_core, (ID3D11DeviceContext*)(uintptr_t)ctx, (ID3D11Texture2D*)(uintptr_t)color,
                    (ID3D11RenderTargetView*)(uintptr_t)rtv, (ID3D11Texture2D*)(uintptr_t)depth,
                    (g_worldCopy && g_worldValid) ? g_worldCopy : NULL, &cam)) {
        g_fails = 0;
        if (g_logf && (g_frames % 300) == 2) { fprintf(g_logf, "postfx: frame %u: %u clears, %u world copies\n", g_frames, g_clearsThisFrame, g_copiesThisFrame); fflush(g_logf); }
    } else if (++g_fails > 600) {
        g_dead = 1;     // e.g. unsupported formats: stop trying, the game keeps running unmodified
        if (g_logf) { fprintf(g_logf, "postfx: giving up after repeated failures\n"); fflush(g_logf); }
    }
    g_worldValid = 0; g_clearsThisFrame = 0; g_copiesThisFrame = 0;
}

void InstallPostFX(void) {
    char exe[MAX_PATH], logp[MAX_PATH];
    char* slash;
    void* target;
    int r;

    GetModuleFileNameA(NULL, exe, MAX_PATH);
    slash = strrchr(exe, '\\');
    if (slash) *slash = 0;
    _snprintf(g_dir, sizeof g_dir, "%s\\postfx", exe);
    _snprintf(logp, sizeof logp, "%s\\postfx.log", g_dir);
    g_logf = fopen(logp, "w");
    if (g_logf) PfxSetLog(g_logf);

    target = IdaAddrToRuntime(ADDR_HUD_DRAW);
    r = DobbyInstrument(target, HudPre);
    DBGLOG("postfx: hook on HUD draw (%p) -> %s (%d)", target, r == 0 ? "OK" : "FAILED", r);
    if (g_logf) { fprintf(g_logf, "postfx: hook on HUD draw %p -> %d\n", target, r); fflush(g_logf); }
    (void)r; (void)target;
}
