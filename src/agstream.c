// agstream.c - streams Condor 3 autogen from the 64-bit helper (c3helper.exe) instead of letting the game load whole tiles.
//
// The game normally loads every autogen tile of a 4x4 tile window as one model and keeps each mesh twice (CPU copy + GPU buffer),
// which costs ~1.6 GB of the 32-bit address space for the OrlyDemo landscape (see condor3-helper/docs/AUTOGEN_RE.md).
// With this module:
//   * the game's own autogen loading is switched off (its .oha table is replaced by an empty one before the cache thread runs),
//   * the helper holds the landscape's complete autogen in 64-bit memory, cut into chunks (4 x 4 per tile) with 3 levels of detail,
//   * a stream thread asks the helper for the chunks around the glider (LOD by distance) over shared memory,
//   * the main thread turns each answer into a real game model (same class, same fields as the game's own autogen models),
//     creates its GPU buffers with the game's own function, frees the CPU copy (except for the chunk under the glider, which the
//     game's triangle collision needs) and puts it into the game's autogen list, so the game draws it with its own shaders.
//   * textures come from the helper too (mip levels dropped for far LODs) and are owned by us, not by the game's texture cache.
// D3D work and the game's lists are touched on the main thread only (AgStream_FrameTick, called by the HUD hook of postfx_hook.c
// once per frame, within a time budget); the stream thread does the IPC and builds the CPU side of each model (as the game's own
// cache thread does) and the collision trees.
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdarg.h>
#include "install_patch.h"
#include "dobby.h"
#include "c3h_client.h"

#define G(a) ((uintptr_t)IdaAddrToRuntime(a))
// game functions (Delphi register convention) and globals, see AUTOGEN_RE.md
#define FN_CACHE_EXECUTE   0x006DEF04
#define FN_AG_UPDATE       0x006DF690
#define FN_AG_AFTERTESS    0x006DF568
#define FN_MODEL_CREATE    0x007709BC
#define FN_PART_CREATE     0x007708A8
#define FN_LIST_ADD        0x0042309C
#define FN_LIST_GET        0x004231FC
#define FN_LIST_DELETE     0x004230E8
#define FN_FREE            0x00404D50
#define FN_SETLENGTH       0x00407608
#define FN_MODEL_BOUNDS    0x00770A74
#define FN_MODEL_GPU       0x00771134
#define FN_MODEL_FREEARR   0x00771458
#define FN_MODEL_POS       0x00761014
#define FN_EFFECT          0x00775938
#define FN_SIM_TEARDOWN    0x006D71D0   // end of a flight: "Free TerrainCacheThread" ... "Free world"
#define VAR_WORLD          0x00792FD0
#define VAR_GLIDER         0x00792A64
#define VAR_LSNAME         0x00793584
#define VAR_MODELCLASS     0x006DEDAC
#define VAR_PARTCLASS      0x0076FB40
#define VAR_TI_VERTS       0x004AC838
#define VAR_TI_INDICES     0x004AC808
#define VAR_TI_GROUPS      0x0076FC70
#define VAR_IDENTITY       0x00792BD4
#define VAR_EFFECTMGR      0x00793978
#define VAR_DEVICE         0x007DA0EC
#define VAR_CONTEXT        0x007DA0F4

// ---- Delphi calls ------------------------------------------------------------------------------------------------
static uint32_t DCall(uintptr_t fn, uint32_t a, uint32_t d, uint32_t c) {
    __asm__ volatile("call *%3" : "+a"(a), "+d"(d), "+c"(c) : "r"(fn) : "memory", "cc");
    return a;
}
static uint32_t DCallPush10(uintptr_t fn, uint32_t a, uint32_t d, uint32_t c) {   // pushes 1, then 0 (callee pops)
    __asm__ volatile("pushl $1\n\tpushl $0\n\tcall *%3" : "+a"(a), "+d"(d), "+c"(c) : "r"(fn) : "memory", "cc");
    return a;
}
static void DSetLength(void* arr, uint32_t typeInfo, uint32_t n) {                // _DynArraySetLength(&arr, ti, 1, n); caller pops n
    uint32_t a = (uint32_t)(uintptr_t)arr, d = typeInfo, c = 1;
    uintptr_t fn = G(FN_SETLENGTH);
    __asm__ volatile("pushl %4\n\tcall *%3\n\taddl $4, %%esp" : "+a"(a), "+d"(d), "+c"(c) : "r"(fn), "r"(n) : "memory", "cc");
}
#define RD(addr) (*(volatile uint32_t*)(uintptr_t)(addr))
// Big transient buffers (chunk answers, textures) bypass the C heap: under Wine its 16 MB segments fragment and are never given
// back, which grew the process by ~0.25 MB per streamed chunk. VirtualAlloc/VirtualFree return the memory exactly.
static void* BigAlloc(size_t n) { return VirtualAlloc(NULL, n ? n : 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE); }
static void  BigFree(void* p) { if (p) VirtualFree(p, 0, MEM_RELEASE); }
static int Ok(uintptr_t p, size_t n) { return p && !IsBadReadPtr((void*)p, n); }

// ---- log ----------------------------------------------------------------------------------------------------
static FILE* g_log; static char g_gameDir[MAX_PATH];
static void ALog(const char* fmt, ...) {
    va_list ap; SYSTEMTIME t;
    if (!g_log) return;
    GetLocalTime(&t);
    fprintf(g_log, "%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
    fputc('\n', g_log); fflush(g_log);
}

// ---- frame timing (diagnostics for stutters) ------------------------------------------------------------------
// AgStream_FrameTick runs once per frame, so the time between two calls is the frame time. The work of tick N shows up in the
// frame that ends at tick N+1: a slow frame is logged together with what streaming did in the tick before it.
enum { T_BUILD, T_ARRAYS, T_BOUNDS, T_BUFS, T_TEX, T_FREE, T_TOTAL, T_N };
static const char* kTName[T_N] = { "hook-up", "-", "-", "vb/ib", "tex", "free", "all" };
static double g_tf[T_N]; static unsigned g_fJobs, g_fTex, g_fFree, g_fBytes;      // this tick
static double g_pf[T_N]; static unsigned g_pJobs, g_pTex, g_pFree, g_pBytes;      // previous tick
static unsigned g_slow[4], g_slowOurs[4], g_frameN; static double g_frameMax, g_lastTick;
static volatile LONG g_stNow;              // what it does right now (same bits)
static volatile LONG g_stMask;             // what the stream thread did since the last tick: 1 io, 2 model build, 4 trees, 8 VirtualAlloc
static double g_ppTotal;
static unsigned g_winFrames; static double g_winMs, g_fps;   // frames per second over ~3 s windows                   // streaming time two ticks back
static double Now(void) { static LARGE_INTEGER f; LARGE_INTEGER c; if (!f.QuadPart) QueryPerformanceFrequency(&f); QueryPerformanceCounter(&c); return (double)c.QuadPart * 1000.0 / (double)f.QuadPart; }
#define TSTART double _t0 = Now()
#define TADD(k) do { double _t1 = Now(); g_tf[k] += _t1 - _t0; _t0 = _t1; } while (0)

// ---- config -------------------------------------------------------------------------------------------------
static int   g_enable = 1;
static float g_r[C3H_MAX_LOD] = { 2000.0f, 5000.0f, 12000.0f };   // LOD n is used up to distance g_r[n] (metres, 2D, to the chunk box)
static int   g_jobsPerTick = 1;                                   // (unused since the time budget)
static int   g_lodMode = 2;                                      // 2 = distance per level (below), 1 = screen-space error, 0 = fixed ranges g_r
static float g_tolPx = 2.0f;                                     // mode 1: screen-space error tolerance (pixels)
static float g_range = 15000.0f;                                 // no autogen beyond (metres, 3D from the camera)
static float g_budgetMs = 3.0f;                                  // main-thread streaming work per frame (ms; at least one step)
static int   g_noGpu = 0;                                        // test switch: build models but create no GPU objects, draw nothing
static float g_levelDist[32];                                    // mode 2: export level n used up to this distance (0 = derived)
static float g_blobs = 15000.0f;                                 // mode 2: generated levels only beyond this (tiles with a coarse real level)
static float g_full = 2000.0f, g_blob1 = 5000.0f, g_blob2 = 12000.0f;   // mode 2, landscapes without detail sets
static int   g_cfgMarker;                                        // the file is our documented template
static FILETIME g_cfgTime;
#define CFG_MARKER "# agstream.txt v2"
static void ConfigPath(char* p, size_t n) { _snprintf(p, n, "%s\\postfx\\agstream.txt", g_gameDir); }
static void LoadConfig(void) {
    char p[MAX_PATH], line[256]; FILE* f; WIN32_FILE_ATTRIBUTE_DATA fa;
    ConfigPath(p, sizeof p);
    if (GetFileAttributesExA(p, GetFileExInfoStandard, &fa)) g_cfgTime = fa.ftLastWriteTime;
    f = fopen(p, "r");
    g_cfgMarker = 0;
    if (!f) return;
    while (fgets(line, sizeof line, f)) {
        char k[32]; float v;
        if (!strncmp(line, CFG_MARKER, strlen(CFG_MARKER))) g_cfgMarker = 1;
        if (line[0] == '#' || sscanf(line, "%31s %f", k, &v) != 2) continue;
        if (!_stricmp(k, "enable")) g_enable = (int)v;
        else if ((k[0] == 'L' || k[0] == 'l') && k[1] >= '0' && k[1] <= '9' && atoi(k + 1) < 32) g_levelDist[atoi(k + 1)] = v;
        else if (!_stricmp(k, "blobs")) g_blobs = v;
        else if (!_stricmp(k, "full")) g_full = v;
        else if (!_stricmp(k, "blob1")) g_blob1 = v;
        else if (!_stricmp(k, "blob2")) g_blob2 = v;
        else if (!_stricmp(k, "r0")) g_r[0] = v; else if (!_stricmp(k, "r1")) g_r[1] = v; else if (!_stricmp(k, "r2")) g_r[2] = v;
        else if (!_stricmp(k, "jobs")) g_jobsPerTick = (int)v;
        else if (!_stricmp(k, "nogpu")) g_noGpu = (int)v;
        else if (!_stricmp(k, "lodmode")) g_lodMode = (int)v;
        else if (!_stricmp(k, "tol")) g_tolPx = v;
        else if (!_stricmp(k, "range")) g_range = v;
        else if (!_stricmp(k, "budget")) g_budgetMs = v;
    }
    fclose(f);
}
// export level n: its distance from the file, else twice the distance of level n + 1 (L19 3000 -> L18 6000, L17 12000; L20 1500)
static float LevelDist(int n) {
    float base = g_levelDist[19] > 0 ? g_levelDist[19] : 3000.0f;
    if (n < 1 || n > 30) return base;
    if (g_levelDist[n] > 0) return g_levelDist[n];
    return n <= 19 ? base * (float)(1u << (19 - n)) : base / (float)(1u << (n - 19 > 8 ? 8 : n - 19));
}
// writes the documented settings file (with the values in use) when the file is not our template yet
static void WriteConfigTemplate(void) {
    char p[MAX_PATH]; FILE* f; int n;
    ConfigPath(p, sizeof p);
    f = fopen(p, "w");
    if (!f) return;
    fprintf(f, CFG_MARKER " - autogen streaming (c3helper.exe + d3dx.dll). One \"key value\" per line, # starts a comment.\n"
               "# Changes are used while the game runs (within a second), except 'enable'. Delete the file to get the defaults back.\n\n"
               "enable %d          # 0 = the game loads autogen itself (restart needed)\n\n", g_enable);
    fprintf(f, "# Which detail is shown where. Distances in metres from the camera to the nearest point of a chunk (altitude counts).\n"
               "range %.0f       # no autogen beyond this distance\n", g_range);
    for (n = 20; n >= 17; n--)
        fprintf(f, "L%d %.0f%s\n", n, LevelDist(n), n == 20 ? "         # data of export level 20 up to this distance (when the tile has a coarser level too)"
                                                  : n == 17 ? "         # a tile's coarsest real level stays until 'blobs' anyway" : "");
    fprintf(f, "blobs %.0f       # generated simplified versions only beyond this distance (where a real coarser level exists)\n\n", g_blobs);
    fprintf(f, "# Landscapes without detail sets (old compiler, no Autogen/lods.txt): full detail, then generated 8 m / 24 m / 72 m versions\n"
               "full %.0f\nblob1 %.0f\nblob2 %.0f\n\n", g_full, g_blob1, g_blob2);
    fprintf(f, "# Other\n"
               "budget %.1f        # milliseconds of streaming work per frame on the render thread (textures, 4 MB buffer pieces)\n"
               "lodmode %d         # 2 = distances above, 1 = screen-space error ('tol' pixels), 0 = old fixed ranges r0 r1 r2\n"
               "tol %.1f\n", g_budgetMs, g_lodMode, g_tolPx);
    fclose(f);
    LoadConfig();
}
static void ConfigCheck(void) {                                  // stream thread, about once a second: reload when the file changed
    char p[MAX_PATH]; WIN32_FILE_ATTRIBUTE_DATA fa;
    ConfigPath(p, sizeof p);
    if (!GetFileAttributesExA(p, GetFileExInfoStandard, &fa) || !CompareFileTime(&fa.ftLastWriteTime, &g_cfgTime)) return;
    Sleep(100);                                                  // let the editor finish writing
    LoadConfig();
    ALog("agstream.txt reloaded: lodmode %d, range %.0f, L20 %.0f L19 %.0f L18 %.0f L17 %.0f, blobs %.0f, full %.0f blob1 %.0f blob2 %.0f, budget %.1f",
         g_lodMode, g_range, LevelDist(20), LevelDist(19), LevelDist(18), LevelDist(17), g_blobs, g_full, g_blob1, g_blob2, g_budgetMs);
}

// ---- state ---------------------------------------------------------------------------------------------------
typedef struct { uint32_t key; uint8_t* dds; uint32_t size; } JobTex;
#define JOB_MAX_TEX 64                     // distinct textures one job brings (a chunk with more takes several jobs)
typedef struct Job {
    struct Job* next; int chunk, lod; uint8_t* blob; uint32_t size; int ntex; JobTex tex[JOB_MAX_TEX];
    uint32_t model;                        // game model built by the stream thread (no GPU objects yet)
    int arr;                               // LOD 0 near the glider: keeps its CPU arrays (collision trees need them)
    int stage, texStep; uint32_t vbOff, ibOff; struct ID3D11Buffer* vb; struct ID3D11Buffer* ib;   // main-thread progress
} Job;
typedef struct { ID3D11ShaderResourceView* srv; ID3D11Texture2D* tex; int users; unsigned lastUse; } TexEntry;

static C3hClient     g_cl;
static volatile int  g_state;            // 0 idle, 1 open requested, 2 streaming, 3 flight ending, -1 failed
static char          g_lsDir[MAX_PATH];
static C3hLandscape  g_L;
static C3hChunkInfo* g_info;
static float*        g_box;              // per chunk world AABB 2D: minx, miny, maxx, maxy
static float*        g_zb;               // per chunk altitude range: minz, maxz
static Job*          g_cur;              // main thread: job being staged
static volatile int* g_want;             // stream thread: desired LOD (-1 none)
static volatile int* g_res;              // main thread: resident LOD (-1 none)
static volatile int* g_pend;             // LOD of the queued job (-1 none)
static volatile uint8_t* g_needArr;      // main thread: LOD 0 chunk came near the glider without CPU arrays: load it again with them
static uint32_t*     g_model;            // main thread: game model per chunk
static TexEntry*     g_tex;              // [n_textures * n_lods], main thread
static volatile uint8_t* g_texHave;      // 1 = created (main thread), read by the stream thread
static volatile uint8_t* g_texBad;       // the helper could not deliver it (stream thread): parts are drawn without it, never asked again
static CRITICAL_SECTION g_qlock; static Job* g_qhead; static Job* g_qtail; static volatile int g_qn;
static uint32_t g_lsPtr, g_listPtr;      // landscape object / autogen resident list we stream into
static volatile double g_gx, g_gy, g_gz; // glider position (main thread copies it each tick)
static volatile int g_haveGlider, g_haveCam;
static volatile double g_cx, g_cy, g_cz, g_K = 923.0;   // camera position (world), pixels per radian (viewport height * m11 / 2)
static unsigned g_ticks, g_created, g_freed, g_dropped, g_texMissing, g_emptyChunks; static unsigned long long g_bytesIn;
static double g_ioMs, g_cpuMs;

static uint32_t World(void)  { uintptr_t p = RD(G(VAR_WORLD)); return Ok(p, 4) ? RD(p) : 0; }
static uint32_t Landscape(void) { uint32_t w = World(); return (w && Ok(w + 0x44, 4)) ? RD(w + 0x44) : 0; }
static uint32_t AutoGen(void) { uint32_t w = World(), g; if (!w || !Ok(w + 0x48, 4)) return 0; g = RD(w + 0x48); return (g && Ok(g + 0x20, 4)) ? RD(g + 0x20) : 0; }

// ---- game-side switch-off -------------------------------------------------------------------------------------
// Pre-handler of AutogenCacheThread.Execute (cache thread). Replaces the .oha table by an empty one (the game then loads no autogen)
// and asks the stream thread to open the landscape in the helper.
static uint32_t* g_zeroRows; static uint32_t* g_zeroRow; static uint32_t g_zeroFor;
static void CacheExecutePre(void* address, DobbyRegisterContext* rc) {
    uint32_t L = Landscape(), tx, ty, i; uintptr_t nm; char name[64];
    (void)address; (void)rc;
    if (!g_enable || g_state < 0 || g_state == 3 || !L || !Ok(L + 0x1a4, 8)) return;
    if (RD(L + 0x44) == (uint32_t)(uintptr_t)g_zeroRows && g_zeroFor == L) return;      // already switched off for this landscape
    tx = *(uint16_t*)(uintptr_t)(L + 0x1a4); ty = *(uint16_t*)(uintptr_t)(L + 0x1a6);
    nm = RD(G(VAR_LSNAME)) + 0xdc;
    if (!Ok(nm, 2)) return;
    memcpy(name, (void*)(nm + 1), *(uint8_t*)nm < 63 ? *(uint8_t*)nm : 63); name[*(uint8_t*)nm < 63 ? *(uint8_t*)nm : 63] = 0;
    g_zeroRow = (uint32_t*)calloc(ty + 1, 4); g_zeroRows = (uint32_t*)calloc(tx + 1, 4);     // leaked on purpose (tiny, game may hold it)
    for (i = 0; i < tx; i++) g_zeroRows[i] = (uint32_t)(uintptr_t)g_zeroRow;
    ALog("landscape '%s' %ux%u tiles: game autogen switched off (oha table %08x -> %p)", name, tx, ty, RD(L + 0x44), (void*)g_zeroRows);
    *(volatile uint32_t*)(uintptr_t)(L + 0x44) = (uint32_t)(uintptr_t)g_zeroRows;
    g_zeroFor = L;
    {   char dir[MAX_PATH];
        _snprintf(dir, sizeof dir, "%s\\Landscapes\\%s", g_gameDir, name);
        if (g_state == 2 && !_stricmp(dir, g_lsDir)) return;                    // same landscape again (new flight): data is still open
        strcpy(g_lsDir, dir); }
    g_state = 1;
}

// ---- uploads through our own staging resources -------------------------------------------------------------------
// Under CrossOver (DXVK) every UpdateSubresource / CreateXxx-with-initial-data takes temporary upload memory from DXVK's allocator,
// which was never given back (+0.3..0.6 MB per streamed chunk). We upload through a fixed ring of staging buffers and a small set of
// staging textures instead: Map(WRITE) + memcpy + CopySubresourceRegion / CopyResource. No allocation per upload at all.
#define STAGE_BUF_SIZE (4u << 20)
#define STAGE_BUF_N    8
static ID3D11Buffer* g_stageBuf[STAGE_BUF_N]; static int g_stageNext;
typedef struct { ID3D11Texture2D* t; uint32_t w, h, mips, fmt; unsigned used; } StageTex;
#define STAGE_TEX_N 20                     // 6 per shape for the 3 usual shapes (2048, 1024, 256) + spare
static StageTex g_stageTex[STAGE_TEX_N]; static unsigned g_stageClock;
static ID3D11Device* Dev(void) { return (ID3D11Device*)(uintptr_t)RD(G(VAR_DEVICE)); }
static ID3D11DeviceContext* Ctx(void) { return (ID3D11DeviceContext*)(uintptr_t)RD(G(VAR_CONTEXT)); }
static int g_uploads;                      // uploads this tick (the context is flushed after them)
static unsigned g_stageBusy;               // uploads postponed because every staging resource was still in use by the GPU
// src -> dst[off, off + n), n <= 4 MB. Returns 0 (nothing done) when every staging buffer is still in use by the GPU: try again later.
static int UploadBufferAt(ID3D11Buffer* dst, const uint8_t* src, uint32_t off, uint32_t n) {
    ID3D11DeviceContext* ctx = Ctx(); ID3D11Buffer** ps = NULL; D3D11_MAPPED_SUBRESOURCE ms; D3D11_BOX box = { 0, 0, 0, n, 1, 1 }; int k, mapped = 0;
    for (k = 0; k < STAGE_BUF_N && !mapped; k++) {
        ps = &g_stageBuf[g_stageNext]; g_stageNext = (g_stageNext + 1) % STAGE_BUF_N;
        if (!*ps) {
            D3D11_BUFFER_DESC bd; memset(&bd, 0, sizeof bd);
            bd.ByteWidth = STAGE_BUF_SIZE; bd.Usage = D3D11_USAGE_STAGING; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            if (FAILED(ID3D11Device_CreateBuffer(Dev(), &bd, NULL, ps))) *ps = NULL;
        }
        mapped = *ps && SUCCEEDED(ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)*ps, 0, D3D11_MAP_WRITE, D3D11_MAP_FLAG_DO_NOT_WAIT, &ms));
    }
    if (!mapped) { g_stageBusy++; return 0; }
    g_uploads++;
    {
        memcpy(ms.pData, src, n);
        ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource*)*ps, 0);
        ID3D11DeviceContext_CopySubresourceRegion(ctx, (ID3D11Resource*)dst, 0, off, 0, 0, (ID3D11Resource*)*ps, 0, &box);
    }
    return 1;
}
// blocks: rows of 4x4 blocks for BC formats, rows of pixels for 32-bit
// Mapping a staging texture whose last copy the GPU has not done yet waits for the GPU (measured: ~22 ms, a whole frame, per
// texture). So: up to STAGE_PER_SHAPE staging textures per shape, tried least recently used first with D3D11_MAP_FLAG_DO_NOT_WAIT;
// a new one when all are busy; waiting only when the shape has its maximum.
#define STAGE_PER_SHAPE 6
static unsigned g_stageWaits, g_stageNew;
static int UploadTexture(ID3D11Texture2D* dst, uint32_t w, uint32_t h, uint32_t mips, uint32_t fmt, const D3D11_SUBRESOURCE_DATA* sd) {
    ID3D11DeviceContext* ctx = Ctx(); StageTex* st = NULL; StageTex* cand[STAGE_TEX_N]; int i, k, nc = 0, mapped0 = 0; uint32_t m;
    D3D11_MAPPED_SUBRESOURCE ms0;
    for (i = 0; i < STAGE_TEX_N; i++) if (g_stageTex[i].t && g_stageTex[i].w == w && g_stageTex[i].h == h && g_stageTex[i].mips == mips && g_stageTex[i].fmt == fmt) {
        for (k = nc++; k > 0 && cand[k - 1]->used > g_stageTex[i].used; k--) cand[k] = cand[k - 1];   // sorted, oldest first
        cand[k] = &g_stageTex[i];
    }
    for (k = 0; k < nc && !st; k++) {
        HRESULT hr = ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)cand[k]->t, 0, D3D11_MAP_WRITE, D3D11_MAP_FLAG_DO_NOT_WAIT, &ms0);
        if (SUCCEEDED(hr)) { st = cand[k]; mapped0 = 1; }
    }
    if (!st && nc >= STAGE_PER_SHAPE) { g_stageWaits++; g_stageBusy++; return 0; }   // all busy, no more allowed: later
    if (!st) {                                                                    // a new one, in the least recently used slot
        D3D11_TEXTURE2D_DESC td; int lru = 0;
        for (i = 1; i < STAGE_TEX_N; i++) if (!g_stageTex[i].t || (g_stageTex[lru].t && g_stageTex[i].used < g_stageTex[lru].used)) lru = i;
        st = &g_stageTex[lru];
        if (st->t) ID3D11Texture2D_Release(st->t);
        memset(&td, 0, sizeof td);
        td.Width = w; td.Height = h; td.MipLevels = mips; td.ArraySize = 1; td.Format = (DXGI_FORMAT)fmt; td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_STAGING; td.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        st->t = NULL; st->w = w; st->h = h; st->mips = mips; st->fmt = fmt;
        if (FAILED(ID3D11Device_CreateTexture2D(Dev(), &td, NULL, &st->t))) st->t = NULL;
        g_stageNew++;
    }
    st->used = ++g_stageClock;
    for (m = 0; m < mips; m++) {
        uint32_t mh = h >> m, rows; D3D11_MAPPED_SUBRESOURCE ms; int ok;
        if (!mh) mh = 1;
        rows = (fmt == DXGI_FORMAT_B8G8R8A8_UNORM) ? mh : (mh + 3) / 4;
        if (m == 0 && mapped0) { ms = ms0; ok = 1; }
        else ok = st->t && SUCCEEDED(ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)st->t, m, D3D11_MAP_WRITE, 0, &ms));
        if (ok) {
            uint32_t r;
            for (r = 0; r < rows; r++) memcpy((uint8_t*)ms.pData + (size_t)r * ms.RowPitch, (const uint8_t*)sd[m].pSysMem + (size_t)r * sd[m].SysMemPitch, sd[m].SysMemPitch);
            ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource*)st->t, m);
        } else ID3D11DeviceContext_UpdateSubresource(ctx, (ID3D11Resource*)dst, m, NULL, sd[m].pSysMem, sd[m].SysMemPitch, 0);
    }
    if (st->t) ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource*)dst, (ID3D11Resource*)st->t);
    g_uploads++;
    return 1;
}

// ---- textures ----------------------------------------------------------------------------------------------
// Textures are pooled by (format, size, mips): a texture no chunk uses any more goes into a free pool and is refilled with
// UpdateSubresource for the next one of the same shape. Creating/destroying them instead fragments DXVK's 16 MB memory chunks
// (measured: +100 MB per 400 streamed chunks); the atlas pages have one size per LOD, so the pool is almost always hit.

typedef struct { ID3D11Texture2D* tex; ID3D11ShaderResourceView* srv; uint32_t w, h, mips, fmt, bytes; } PoolTex;
static PoolTex g_pool[96]; static int g_npool; static uint32_t g_poolBytes, g_texCreated, g_texReused, g_texSlowLog;
#define POOL_MAX_BYTES (96u << 20)

typedef struct { uint32_t w, h, mips, fmt, bytes; D3D11_SUBRESOURCE_DATA sd[16]; } DdsInfo;
static int ParseDds(const uint8_t* d, uint32_t n, DdsInfo* di) {
    uint32_t pf, fourcc, bits, i, sw, sh; int bs = 0; size_t off = 128;
    if (n < 128 || memcmp(d, "DDS ", 4)) return 0;
    memcpy(&di->h, d + 12, 4); memcpy(&di->w, d + 16, 4); memcpy(&di->mips, d + 28, 4); memcpy(&pf, d + 80, 4); memcpy(&fourcc, d + 84, 4); memcpy(&bits, d + 88, 4);
    if (di->mips < 1) di->mips = 1; if (di->mips > 16) di->mips = 16;
    if (pf & 4) {
        if (fourcc == 0x31545844) { di->fmt = DXGI_FORMAT_BC1_UNORM; bs = 8; }
        else if (fourcc == 0x33545844) { di->fmt = DXGI_FORMAT_BC2_UNORM; bs = 16; }
        else if (fourcc == 0x35545844) { di->fmt = DXGI_FORMAT_BC3_UNORM; bs = 16; }
        else return 0;
    } else if (bits == 32) di->fmt = DXGI_FORMAT_B8G8R8A8_UNORM; else return 0;
    // Native D3D11 refuses a BC texture whose top level is not a multiple of 4 wide and high (E_INVALIDARG; DXVK accepts it).
    // Native landscapes have such textures (1024x942, 2550x1472, ...; and the helper's coarser LODs halve sizes). The data is whole
    // 4x4 blocks anyway, so the texture is created rounded up to 4: same blocks, same bytes. Lower levels only as long as their
    // block layout stays the same as the file's.
    sw = di->w; sh = di->h;
    if (bs) { di->w = (sw + 3) & ~3u; di->h = (sh + 3) & ~3u; }
    for (i = 0; i < di->mips; i++) {
        uint32_t mw = sw >> i, mh = sh >> i, aw = di->w >> i, ah = di->h >> i; size_t sz;
        if (!mw) mw = 1; if (!mh) mh = 1; if (!aw) aw = 1; if (!ah) ah = 1;
        if (bs && i > 0 && ((mw + 3) / 4 != (aw + 3) / 4 || (mh + 3) / 4 != (ah + 3) / 4)) { di->mips = i; break; }
        sz = bs ? (size_t)((mw + 3) / 4) * ((mh + 3) / 4) * bs : (size_t)mw * mh * 4;
        if (off + sz > n) { di->mips = i; break; }
        di->sd[i].pSysMem = d + off; di->sd[i].SysMemPitch = bs ? ((mw + 3) / 4) * bs : mw * 4; di->sd[i].SysMemSlicePitch = (UINT)sz;
        off += sz;
    }
    di->bytes = (uint32_t)(off - 128);
    return di->mips > 0;
}
static void TexToPool(ID3D11Texture2D* tex, ID3D11ShaderResourceView* srv);
static int g_texBusy;                      // TexAcquire: nothing done, staging busy
static char g_texErr[160];                 // TexAcquire: why it failed (when not busy)
static ID3D11ShaderResourceView* TexAcquire(const uint8_t* d, uint32_t n, ID3D11Texture2D** outTex) {
    DdsInfo di; int i; ID3D11Device* dev = (ID3D11Device*)(uintptr_t)RD(G(VAR_DEVICE));
    ID3D11DeviceContext* ctx = (ID3D11DeviceContext*)(uintptr_t)RD(G(VAR_CONTEXT));
    D3D11_TEXTURE2D_DESC td; ID3D11Texture2D* tex = NULL; ID3D11ShaderResourceView* srv = NULL;
    *outTex = NULL;
    if (!dev || !ctx) { g_texBusy = 1; return NULL; }
    if (!ParseDds(d, n, &di)) { _snprintf(g_texErr, sizeof g_texErr, "not a DDS we can read (%u bytes)", n); return NULL; }
    for (i = g_npool - 1; i >= 0; i--) {
        PoolTex* p = &g_pool[i];
        if (p->w == di.w && p->h == di.h && p->mips == di.mips && p->fmt == di.fmt) {
            double t0 = Now();
            if (!UploadTexture(p->tex, di.w, di.h, di.mips, di.fmt, di.sd)) { g_texBusy = 1; return NULL; }
            if (Now() - t0 > 4 && g_texSlowLog < 30) { g_texSlowLog++; ALog("texture %ux%u: upload %.1f ms (pooled; staging new %u waits %u)", di.w, di.h, Now() - t0, g_stageNew, g_stageWaits); }
            *outTex = p->tex; srv = p->srv;
            g_poolBytes -= p->bytes; g_pool[i] = g_pool[--g_npool]; g_texReused++;
            return srv;
        }
    }
    memset(&td, 0, sizeof td);
    td.Width = di.w; td.Height = di.h; td.MipLevels = di.mips; td.ArraySize = 1; td.Format = (DXGI_FORMAT)di.fmt; td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    {   double t0 = Now(), t1, t2;
        HRESULT hr;
        if (FAILED(hr = ID3D11Device_CreateTexture2D(dev, &td, NULL, &tex))) {
            _snprintf(g_texErr, sizeof g_texErr, "CreateTexture2D %ux%u, %u mips, format %u: %08lx", di.w, di.h, di.mips, di.fmt, (unsigned long)hr); return NULL; }
        t1 = Now();
        if (FAILED(hr = ID3D11Device_CreateShaderResourceView(dev, (ID3D11Resource*)tex, NULL, &srv))) {
            _snprintf(g_texErr, sizeof g_texErr, "CreateShaderResourceView %ux%u format %u: %08lx", di.w, di.h, di.fmt, (unsigned long)hr); ID3D11Texture2D_Release(tex); return NULL; }
        g_texCreated++;
        if (!UploadTexture(tex, di.w, di.h, di.mips, di.fmt, di.sd)) { TexToPool(tex, srv); g_texBusy = 1; return NULL; }   // filled later
        t2 = Now();
        if (Now() - t0 > 4 && g_texSlowLog < 30) { g_texSlowLog++; ALog("texture %ux%u: create %.1f ms, upload %.1f ms, view %.1f ms (staging new %u waits %u)", di.w, di.h, t1 - t0, t2 - t1, Now() - t2, g_stageNew, g_stageWaits); } }
    *outTex = tex;
    return srv;
}
static void TexToPool(ID3D11Texture2D* tex, ID3D11ShaderResourceView* srv) {
    D3D11_TEXTURE2D_DESC td; PoolTex p; DdsInfo di; (void)di;
    ID3D11Texture2D_GetDesc(tex, &td);
    p.tex = tex; p.srv = srv; p.w = td.Width; p.h = td.Height; p.mips = td.MipLevels; p.fmt = td.Format;
    p.bytes = td.Width * td.Height / (td.Format == DXGI_FORMAT_BC1_UNORM ? 2 : td.Format == DXGI_FORMAT_B8G8R8A8_UNORM ? 1 : 1) * (td.Format == DXGI_FORMAT_B8G8R8A8_UNORM ? 4 : 1) * 4 / 3;
    while (g_npool && (g_npool >= (int)(sizeof g_pool / sizeof g_pool[0]) || g_poolBytes + p.bytes > POOL_MAX_BYTES)) {   // drop the oldest
        ID3D11ShaderResourceView_Release(g_pool[0].srv); ID3D11Texture2D_Release(g_pool[0].tex); g_poolBytes -= g_pool[0].bytes;
        memmove(&g_pool[0], &g_pool[1], (size_t)(--g_npool) * sizeof g_pool[0]);
    }
    g_pool[g_npool++] = p; g_poolBytes += p.bytes;
}
static void TexRelease(uint32_t key) {
    TexEntry* t = &g_tex[key];
    if (t->users > 0 && --t->users == 0 && t->srv) { TexToPool(t->tex, t->srv); t->srv = NULL; t->tex = NULL; g_texHave[key] = 0; }
}
// ---- vertex / index buffers ------------------------------------------------------------------------------------
// Same reason as the texture pool: chunk buffers of every possible size fragment DXVK's memory chunks. Buffers are created in size
// classes (4 per doubling) and reused through a free pool. Only the very first chunk model is built by the game's own function
// FUN_00771134; its input layout (identical for all autogen models) is shared by all later ones.
typedef struct { ID3D11Buffer* b; uint32_t bind, size; } PoolBuf;
static PoolBuf g_bpool[256]; static int g_nbpool; static uint32_t g_bpoolBytes, g_bufCreated, g_bufReused;
static ID3D11InputLayout* g_layout;
static ID3D11Buffer* g_newVb; static ID3D11Buffer* g_newIb;         // buffers of the model BuildModel just made
static ID3D11Buffer** g_vb; static ID3D11Buffer** g_ib;              // per chunk, pooled buffers in use (NULL = made by the game)
#define BPOOL_MAX_BYTES (128u << 20)
static uint32_t SizeClass(uint32_t n) {
    uint32_t p = 65536, step;
    if (n <= p) return p;
    while (p < n) p <<= 1;                                       // next power of two
    step = p >> 3;                                               // classes: 5/8, 6/8, 7/8, 8/8 of it
    return (n + step - 1) / step * step;
}
static ID3D11Buffer* BufGet(uint32_t bind, uint32_t bytes) {      // pooled or new, contents undefined
    ID3D11Device* dev = (ID3D11Device*)(uintptr_t)RD(G(VAR_DEVICE)); ID3D11DeviceContext* ctx = (ID3D11DeviceContext*)(uintptr_t)RD(G(VAR_CONTEXT));
    uint32_t sz = SizeClass(bytes); int i; ID3D11Buffer* b = NULL; (void)ctx;
    for (i = g_nbpool - 1; i >= 0; i--) if (g_bpool[i].bind == bind && g_bpool[i].size == sz) {
        b = g_bpool[i].b; g_bpoolBytes -= sz; g_bpool[i] = g_bpool[--g_nbpool]; g_bufReused++;
        break;
    }
    if (!b) {
        D3D11_BUFFER_DESC bd; memset(&bd, 0, sizeof bd);
        bd.ByteWidth = sz; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = bind;
        if (FAILED(ID3D11Device_CreateBuffer(dev, &bd, NULL, &b))) return NULL;
        g_bufCreated++;
    }
    return b;
}
static void BufToPool(ID3D11Buffer* b, uint32_t bind) {
    D3D11_BUFFER_DESC bd; ID3D11Buffer_GetDesc(b, &bd);
    while (g_nbpool && (g_nbpool >= (int)(sizeof g_bpool / sizeof g_bpool[0]) || g_bpoolBytes + bd.ByteWidth > BPOOL_MAX_BYTES)) {
        ID3D11Buffer_Release(g_bpool[0].b); g_bpoolBytes -= g_bpool[0].size;
        memmove(&g_bpool[0], &g_bpool[1], (size_t)(--g_nbpool) * sizeof g_bpool[0]);
    }
    g_bpool[g_nbpool].b = b; g_bpool[g_nbpool].bind = bind; g_bpool[g_nbpool].size = bd.ByteWidth; g_nbpool++; g_bpoolBytes += bd.ByteWidth;
}

static uint32_t TexKey(int tex, int lod) { return (uint32_t)tex * g_L.n_lods + (uint32_t)lod; }

// ---- models ----------------------------------------------------------------------------------------------------
// A chunk model is made in two halves:
//   * stream thread (BuildModelCpu): the game objects (model, parts), the CPU arrays and the bounds, as the game's own cache thread
//     does for its autogen (FUN_00771b98). The bounds function would also build a triangle tree per part for the collision test
//     (FUN_0076fdbc with flag 1, 50-120 ms for a dense chunk): it is skipped here and built later only for the chunks near the glider.
//   * main thread (StageJob / FinishJob), spread over frames by a time budget: textures, vertex and index buffers (4 MB pieces),
//     then the effect, the floating-origin position and the game's autogen list.
#define FN_PART_BOUNDS 0x0076FDBC     // (part, &vertices, indices, char tree)
static uint32_t DCallPush1(uintptr_t fn, uint32_t a, uint32_t d, uint32_t c, uint32_t s) {   // one stack argument (callee pops)
    __asm__ volatile("pushl %4\n\tcall *%3" : "+a"(a), "+d"(d), "+c"(c) : "r"(fn), "r"(s) : "memory", "cc");
    return a;
}
static uint32_t BuildModelCpu(const Job* j) {
    const C3hChunk* h = (const C3hChunk*)j->blob; const C3hPart* p = (const C3hPart*)(h + 1);
    const C3hVertex* v = (const C3hVertex*)(p + h->n_parts); const uint32_t* idx = (const uint32_t*)(v + h->n_verts);
    uint32_t m, i, L = g_lsPtr; float cell, pos[3];
    if (!L) return 0;
    cell = *(float*)(uintptr_t)(L + 0x1b8);
    m = DCallPush10(G(FN_MODEL_CREATE), RD(G(VAR_MODELCLASS)), 1, 0);         // = the game's cache thread call (no D3D yet)
    if (!m) return 0;
    *(uint8_t*)(uintptr_t)(m + 0x9c) = 0;                                       // bounds without the collision trees
    DSetLength((void*)(uintptr_t)(m + 0x58), RD(G(VAR_TI_GROUPS)), 1);
    ((uint32_t*)(uintptr_t)RD(m + 0x58))[0] = 0; ((uint32_t*)(uintptr_t)RD(m + 0x58))[1] = h->n_parts;
    for (i = 0; i < h->n_parts; i++) {
        uint32_t part = DCall(G(FN_PART_CREATE), RD(G(VAR_PARTCLASS)), 1, 0); size_t nl = strlen(p[i].name);
        uint8_t* b = (uint8_t*)(uintptr_t)part;
        if (nl > 31) nl = 31;
        b[0x10] = (uint8_t)nl; memcpy(b + 0x11, p[i].name, nl);
        b[0xa4] = 0;                                                            // no texture name: we assign our own SRV later
        memcpy(b + 0x125, p[i].color, 16);
        memcpy(b + 0x138, &p[i].params[0], 4); memcpy(b + 0x13c, &p[i].params[1], 4); memcpy(b + 0x140, &p[i].params[2], 4);
        *(uint32_t*)(b + 0x1e0) = p[i].first_index;  *(uint32_t*)(b + 0x1e4) = p[i].index_count;
        *(uint32_t*)(b + 0x1e8) = p[i].first_vertex; *(uint32_t*)(b + 0x1ec) = p[i].vertex_count;
        if (p[i].color[3] < 1.0f) { b[0x144] = 1; *(uint8_t*)(uintptr_t)(m + 0xcc) = 1; }
        DCall(G(FN_LIST_ADD), m, part, 0);
    }
    DSetLength((void*)(uintptr_t)(m + 0xe4), RD(G(VAR_TI_VERTS)), h->n_verts);
    memcpy((void*)(uintptr_t)RD(m + 0xe4), v, (size_t)h->n_verts * 32);
    DSetLength((void*)(uintptr_t)(m + 0xe8), RD(G(VAR_TI_INDICES)), h->n_indices);
    memcpy((void*)(uintptr_t)RD(m + 0xe8), idx, (size_t)h->n_indices * 4);
    DCall(G(FN_MODEL_BOUNDS), m, 0, 0);
    *(uint8_t*)(uintptr_t)(m + 0x9c) = 1;
    memcpy((void*)(uintptr_t)(m + 0x5c), (void*)(uintptr_t)RD(G(VAR_IDENTITY)), 64);
    *(int32_t*)(uintptr_t)(m + 0xec) = -1000; *(int32_t*)(uintptr_t)(m + 0xf0) = -1000;   // no collision unless under the glider
    pos[0] = ((float)h->tile_x + 0.5f) * 64.0f * cell; pos[1] = ((float)h->tile_y + 0.5f) * 64.0f * cell; pos[2] = 0;
    memcpy((void*)(uintptr_t)(m + 0xf4), pos, 12);
    *(float*)(uintptr_t)(m + 0xd0) = 0.2f;
    if ((j->lod != 0 || !j->arr) && g_layout) DCall(G(FN_MODEL_FREEARR), m, 0, 0);   // the CPU copy only near the glider (collision)
    return m;
}
// Collision trees (stream thread) for LOD 0 chunks near the glider. The main thread asks (g_treeReq), the stream thread builds
// and records the model it built them for; a model is never freed while its trees are being built (g_treeBusy, under g_treeLock).
static CRITICAL_SECTION g_treeLock; static volatile int g_treeReq = -1, g_treeBusy = -1; static volatile uint32_t g_treeReqModel;
static uint32_t* g_treeFor;                // per chunk: the model whose trees exist (0 = none)
static unsigned g_treesBuilt; static double g_treeMs;
static int TreeBusy(int c) {               // main thread, before freeing chunk c's model: 1 = not now
    int busy;
    EnterCriticalSection(&g_treeLock);
    busy = g_treeBusy == c;
    if (!busy) { if (g_treeReq == c) g_treeReq = -1; g_treeFor[c] = 0; }
    LeaveCriticalSection(&g_treeLock);
    return busy;
}
static void BuildTrees(void) {             // stream thread
    int c; uint32_t m, i, n; double t0;
    EnterCriticalSection(&g_treeLock);
    c = g_treeReq; m = g_treeReqModel; g_treeReq = -1; if (c >= 0) g_treeBusy = c;
    LeaveCriticalSection(&g_treeLock);
    if (c < 0) return;
    t0 = Now(); InterlockedOr(&g_stMask, 4); g_stNow = 4;
    if (RD(m + 0xe4) && RD(m + 0xe8)) {
        n = RD(m + 8);
        for (i = 0; i < n; i++) {
            uint32_t part = RD(RD(m + 4) + 4 * i);
            if (RD(part + 0x1e4) >= 100 && !RD(part + 0x1f0)) DCallPush1(G(FN_PART_BOUNDS), part, m + 0xe4, RD(m + 0xe8), 1);
        }
    }
    g_treeMs += Now() - t0; g_treesBuilt++; g_stNow = 0;
    EnterCriticalSection(&g_treeLock);
    if (RD(m + 0xe4)) g_treeFor[c] = m;
    g_treeBusy = -1;
    LeaveCriticalSection(&g_treeLock);
}

static int ListIndex(uint32_t list, uint32_t item) {
    int n = (int)RD(list + 8), i; uint32_t items = RD(list + 4);
    for (i = 0; i < n; i++) if (RD(items + 4 * i) == item) return i;
    return -1;
}
// Removes chunk c's model from the game's list and frees it. The parts' texture fields are borrowed from our cache (no AddRef),
// so they are cleared first and the game's destructor never touches our SRVs; then our cache references are dropped.
static unsigned g_refLog;
// The game's destructor (with the CPU arrays and collision trees of a LOD 0 chunk: 40-70 ms) runs on the stream thread: the model is
// out of the game's list by then and our buffer/texture references are already back in the pools.
#define FREEQ_N 256
static volatile uint32_t g_freeQ[FREEQ_N]; static volatile LONG g_freeHead, g_freeTail;   // main thread writes head, stream thread tail
static volatile int g_teardown;           // flight end: everything is freed right away on the main thread
static int FreeLater(uint32_t m) {
    if (g_teardown || g_freeHead - g_freeTail >= FREEQ_N) return 0;
    g_freeQ[g_freeHead % FREEQ_N] = m; MemoryBarrier(); g_freeHead++;
    return 1;
}
static double g_freeMs; static unsigned g_freedLater;
static void FreeQueued(void) {                                   // stream thread
    while (g_freeTail != g_freeHead) {
        uint32_t m = g_freeQ[g_freeTail % FREEQ_N]; double t0 = Now();
        InterlockedOr(&g_stMask, 16);
        DCall(G(FN_MODEL_FREEARR), m, 0, 0); DCall(G(FN_FREE), m, 0, 0);
        MemoryBarrier(); g_freeTail++; g_freeMs += Now() - t0; g_freedLater++;
    }
}
static void FreeModel(int c) {
    uint32_t m = g_model[c], list = g_listPtr, keys[64]; int i, n, nk = 0, lod = g_res[c];
    double tf0 = Now();
    if (!m) return;
    if (list && (i = ListIndex(list, m)) >= 0) DCall(G(FN_LIST_DELETE), list, (uint32_t)i, 0);
    n = (int)RD(m + 8);
    for (i = 0; i < n; i++) {
        uint32_t part = RD(RD(m + 4) + 4 * i), srv = RD(part + 0x14c), k;
        if (!srv) continue;
        *(uint32_t*)(uintptr_t)(part + 0x14c) = 0;
        for (k = 0; lod >= 0 && k < g_L.n_textures && nk < 64; k++) { uint32_t key = TexKey((int)k, lod); if ((uint32_t)(uintptr_t)g_tex[key].srv == srv) { keys[nk++] = key; break; } }
    }
    if (g_refLog >= 3 && FreeLater(m)) ;                          // destructor on the stream thread
    else if (g_refLog < 3) {
        DCall(G(FN_MODEL_FREEARR), m, 0, 0);                       // CPU copy (LOD 0 keeps it for collision); no-op when already freed                                            // diagnostics: does the game's destructor release VB / IB / layout?
        IUnknown* o[3]; ULONG r[3]; int q;
        for (q = 0; q < 3; q++) { o[q] = (IUnknown*)(uintptr_t)RD(m + 0x24 + 4 * q); if (o[q]) IUnknown_AddRef(o[q]); }
        DCall(G(FN_FREE), m, 0, 0);
        for (q = 0; q < 3; q++) r[q] = o[q] ? IUnknown_Release(o[q]) : 99;
        ALog("free chunk %d: refs left after free VB %lu IB %lu layout %lu (0 = released by the game)", c, r[0], r[1], r[2]);
    } else { DCall(G(FN_MODEL_FREEARR), m, 0, 0); DCall(G(FN_FREE), m, 0, 0); }
    if (g_refLog < 3 && nk) {                                      // diagnostics: our reference must be the only one left
        ID3D11ShaderResourceView* s = g_tex[keys[0]].srv; ULONG r = ID3D11ShaderResourceView_AddRef(s); ID3D11ShaderResourceView_Release(s);
        ALog("free chunk %d: srv refcount after free %lu (users %d)", c, r - 1, g_tex[keys[0]].users); g_refLog++;
    }
    for (i = 0; i < nk; i++) TexRelease(keys[i]);
    if (g_vb[c]) BufToPool(g_vb[c], D3D11_BIND_VERTEX_BUFFER);
    if (g_ib[c]) BufToPool(g_ib[c], D3D11_BIND_INDEX_BUFFER);
    g_vb[c] = g_ib[c] = NULL;
    g_model[c] = 0; g_freed++;
    g_tf[T_FREE] += Now() - tf0; g_fFree++;
}

static void JobFree(Job* j) {
    int k;
    for (k = 0; k < j->ntex; k++) BigFree(j->tex[k].dds);
    BigFree(j->blob); free(j);
}
// a job that is not wanted any more: its model (never shown) and its buffers go back; staged textures stay cached (TexSweep)
static void JobDrop(Job* j) {
    if (j->model && !FreeLater(j->model)) { DCall(G(FN_MODEL_FREEARR), j->model, 0, 0); DCall(G(FN_FREE), j->model, 0, 0); }
    if (j->vb) BufToPool(j->vb, D3D11_BIND_VERTEX_BUFFER);
    if (j->ib) BufToPool(j->ib, D3D11_BIND_INDEX_BUFFER);
    g_pend[j->chunk] = -1; g_dropped++;
    JobFree(j);
}
static int JobWanted(const Job* j) {
    int c = j->chunk;
    return g_want[c] == j->lod && (g_res[c] != j->lod || (j->arr && g_model[c] && !RD(g_model[c] + 0xe4)));   // or: the version with arrays
}

// One step of the main-thread work for job j: one texture, or one 4 MB piece of a buffer, or the final hook-up.
// Returns 1 = more to do, 2 = waiting for a free staging resource (next frame), 0 = finished (model is live), -1 = dropped (j is freed).
static int StageJob(Job* j, uint32_t list) {
    const C3hChunk* h = (const C3hChunk*)j->blob; const C3hPart* p = (const C3hPart*)(h + 1);
    const C3hVertex* v = (const C3hVertex*)(p + h->n_parts); const uint32_t* idx = (const uint32_t*)(v + h->n_verts);
    uint32_t m = j->model, i, L = g_lsPtr; double t0 = Now();
    if (!JobWanted(j) || !m) { JobDrop(j); return -1; }
    if (g_noGpu) j->stage = 3;
    if (j->stage == 0) {                                         // textures, one per step; a texture that is neither cached nor in
        for (; j->texStep < (int)h->n_parts; j->texStep++) {    // the job (released meanwhile) means: drop, the stream thread asks again
            uint32_t key; TexEntry* t; int k;
            if (p[j->texStep].texture < 0) continue;
            key = TexKey(p[j->texStep].texture, j->lod); t = &g_tex[key];
            if (t->srv || g_texBad[key]) continue;
            for (k = 0; k < j->ntex; k++) if (j->tex[k].key == key) break;
            if (k == j->ntex) { g_texMissing++; JobDrop(j); return -1; }
            g_texBusy = 0;
            t->srv = TexAcquire(j->tex[k].dds, j->tex[k].size, &t->tex); t->lastUse = g_ticks;
            g_tf[T_TEX] += Now() - t0;
            if (!t->srv && g_texBusy) return 2;                 // staging textures busy: next frame
            if (t->srv) g_texHave[key] = 1;
            else {                                               // can never be made: drawn without it, never asked again
                static int nl; g_texBad[key] = 1;               // (otherwise the chunk is dropped and asked again forever)
                if (nl++ < 20) ALog("texture %d lod %d: %s: drawn without", p[j->texStep].texture, j->lod, g_texErr);
            }
            g_fTex++; j->texStep++;
            return 1;
        }
        j->stage = 1;
    }
    if (j->stage == 1) {                                         // vertex + index buffers in pieces
        if (!g_layout) { j->stage = 2; }                         // first model: the game's own function in FinishJob
        else {
            uint32_t vbBytes = h->n_verts * 32, ibBytes = h->n_indices * 4, n;
            if (!j->vb) j->vb = BufGet(D3D11_BIND_VERTEX_BUFFER, vbBytes);
            if (!j->ib) j->ib = BufGet(D3D11_BIND_INDEX_BUFFER, ibBytes);
            if (!j->vb || !j->ib) { JobDrop(j); return -1; }
            if (j->vbOff < vbBytes) { n = vbBytes - j->vbOff; if (n > STAGE_BUF_SIZE) n = STAGE_BUF_SIZE; if (!UploadBufferAt(j->vb, (const uint8_t*)v + j->vbOff, j->vbOff, n)) return 2; j->vbOff += n; }
            else if (j->ibOff < ibBytes) { n = ibBytes - j->ibOff; if (n > STAGE_BUF_SIZE) n = STAGE_BUF_SIZE; if (!UploadBufferAt(j->ib, (const uint8_t*)idx + j->ibOff, j->ibOff, n)) return 2; j->ibOff += n; }
            if (j->vbOff >= vbBytes && j->ibOff >= ibBytes) j->stage = 2;
            g_tf[T_BUFS] += Now() - t0;
            return 1;
        }
    }
    // stage 2: hook it up (the old model of this chunk goes; not while the stream thread builds its collision trees)
    if (g_model[j->chunk] && TreeBusy(j->chunk)) return 1;
    for (i = 0; i < h->n_parts && !g_noGpu; i++) if (p[i].texture >= 0) {
        uint32_t key = TexKey(p[i].texture, j->lod);
        if (!g_tex[key].srv && !g_texBad[key]) { g_texMissing++; JobDrop(j); return -1; }
    }
    *(uint32_t*)(uintptr_t)(m + 0x50) = DCall(G(FN_EFFECT), RD(RD(G(VAR_EFFECTMGR))), 0, 0);   // main-thread part of AfterTesselate
    if (g_noGpu) ;
    else if (!g_layout) {                                        // first model: the game's own function, keep its layout
        DCall(G(FN_MODEL_GPU), m, 1, 0);
        g_layout = (ID3D11InputLayout*)(uintptr_t)RD(m + 0x2c);
        if (g_layout) ID3D11InputLayout_AddRef(g_layout);
    } else {
        ID3D11Buffer_AddRef(j->vb); ID3D11Buffer_AddRef(j->ib);  // one reference for the model (released by the game's destructor),
        ID3D11InputLayout_AddRef(g_layout);                      // one stays with us for the pool
        *(uint32_t*)(uintptr_t)(m + 0x24) = (uint32_t)(uintptr_t)j->vb; *(uint32_t*)(uintptr_t)(m + 0x28) = (uint32_t)(uintptr_t)j->ib;
        *(uint32_t*)(uintptr_t)(m + 0x2c) = (uint32_t)(uintptr_t)g_layout;
    }
    DCall(G(FN_MODEL_POS), L, m + 0x100, m + 0xf4);
    *(uint32_t*)(uintptr_t)(m + 0x8c) = RD(m + 0x100); *(uint32_t*)(uintptr_t)(m + 0x90) = RD(m + 0x104); *(uint32_t*)(uintptr_t)(m + 0x94) = 0;
    if (j->lod != 0 || !j->arr) DCall(G(FN_MODEL_FREEARR), m, 0, 0);   // no-op when the stream thread already did it
    g_needArr[j->chunk] = 0;
    for (i = 0; i < h->n_parts && !g_noGpu; i++) if (p[i].texture >= 0) {
        uint32_t key = TexKey(p[i].texture, j->lod), part = RD(RD(m + 4) + 4 * i);
        if (!g_tex[key].srv) continue;                           // texture the helper could not deliver
        *(uint32_t*)(uintptr_t)(part + 0x14c) = (uint32_t)(uintptr_t)g_tex[key].srv;   // borrowed: FreeModel clears it first
        g_tex[key].users++; g_tex[key].lastUse = g_ticks;
    }
    if (g_model[j->chunk]) FreeModel(j->chunk);
    g_vb[j->chunk] = j->vb; g_ib[j->chunk] = j->ib;
    if (!g_noGpu) DCall(G(FN_LIST_ADD), list, m, 0);
    g_model[j->chunk] = m; g_res[j->chunk] = j->lod; g_pend[j->chunk] = -1;
    g_created++; g_fJobs++; g_fBytes += j->size;
    g_tf[T_BUILD] += Now() - t0;
    j->model = 0; j->vb = j->ib = NULL;
    JobFree(j);
    return 0;
}
// textures that were staged for a job that was then dropped and that no model took since: back to the pool after ~20 s
static void TexSweep(void) {
    uint32_t k;
    for (k = 0; k < g_L.n_textures * g_L.n_lods; k++) {
        TexEntry* t = &g_tex[k];
        if (t->srv && t->users == 0 && g_ticks - t->lastUse > 1200) { TexToPool(t->tex, t->srv); t->srv = NULL; t->tex = NULL; g_texHave[k] = 0; }
    }
}

static void ResetAll(const char* why) {
    uint32_t c;
    ALog("reset (%s)", why);
    while (g_freeTail != g_freeHead) Sleep(1);
    if (g_cur) { JobDrop(g_cur); g_cur = NULL; }
    for (;;) { Job* j; EnterCriticalSection(&g_qlock); j = g_qhead; if (j) { g_qhead = j->next; if (!g_qhead) g_qtail = NULL; g_qn--; } LeaveCriticalSection(&g_qlock); if (!j) break; JobDrop(j); }
    while (g_treeBusy >= 0) Sleep(1);
    for (c = 0; g_treeFor && c < g_L.n_chunks; c++) g_treeFor[c] = 0;
    for (c = 0; g_model && c < g_L.n_chunks; c++) { g_model[c] = 0; g_res[c] = -1; if (g_vb[c]) ID3D11Buffer_Release(g_vb[c]); if (g_ib[c]) ID3D11Buffer_Release(g_ib[c]); g_vb[c] = g_ib[c] = NULL; }
    for (c = 0; g_tex && c < g_L.n_textures * g_L.n_lods; c++) { if (g_tex[c].srv) { ID3D11ShaderResourceView_Release(g_tex[c].srv); ID3D11Texture2D_Release(g_tex[c].tex); } g_tex[c].srv = NULL; g_tex[c].tex = NULL; g_tex[c].users = 0; g_texHave[c] = 0; }
}

// Once per rendered frame from the HUD hook (main thread, also while the sim is paused). Runs after the frame's update
// (incl. a landscape refresh) and before the next one.
// camera position (world) and pixels per radian, for the LOD choice of the stream thread
#define VAR_CAMMGR 0x007D96F8
static void CameraTick(void) {
    uint32_t cm = RD(G(VAR_CAMMGR)), L = g_lsPtr; const float* w; float ox = 0, oy = 0; D3D11_VIEWPORT vp; UINT nvp = 1;
    if (!Ok(cm, 4) || !Ok(cm + 0x148, 0x60)) return;
    w = (const float*)(uintptr_t)(cm + 0x148);
    if (L && Ok(L + 0x1c9, 8)) { memcpy(&ox, (void*)(uintptr_t)(L + 0x1c9), 4); memcpy(&oy, (void*)(uintptr_t)(L + 0x1cd), 4); }
    g_cx = w[12] + ox; g_cy = w[13] + oy; g_cz = w[14];
    memset(&vp, 0, sizeof vp);
    if (Ctx()) ID3D11DeviceContext_RSGetViewports(Ctx(), &nvp, &vp);
    {   float m11 = *(const float*)(uintptr_t)(cm + 0x188 + 20);
        if (nvp && vp.Height > 100 && m11 > 0.05f && m11 < 50) g_K = vp.Height * m11 * 0.5; }
    g_haveCam = 1;
}

static void FrameTiming(double t) {
    double dt = g_lastTick ? t - g_lastTick : 0; int b, k;
    g_lastTick = t;
    LONG mask = InterlockedExchange(&g_stMask, 0) | g_stNow;
    g_ppTotal = g_pf[T_TOTAL];
    memcpy(g_pf, g_tf, sizeof g_tf); g_pJobs = g_fJobs; g_pTex = g_fTex; g_pFree = g_fFree; g_pBytes = g_fBytes;
    memset(g_tf, 0, sizeof g_tf); g_fJobs = g_fTex = g_fFree = g_fBytes = 0;
    if (dt <= 0 || dt > 5000) return;                                            // pauses of the game loop (menus, loading)
    g_frameN++; if (dt > g_frameMax) g_frameMax = dt;
    g_winFrames++; g_winMs += dt;
    if (g_winMs > 3000) { g_fps = g_winFrames * 1000.0 / g_winMs; g_winFrames = 0; g_winMs = 0; }
    b = dt > 200 ? 3 : dt > 100 ? 2 : dt > 50 ? 1 : dt > 30 ? 0 : -1;
    if (b < 0) return;
    g_slow[b]++; if (g_pf[T_TOTAL] > dt * 0.5) g_slowOurs[b]++;
    {   char s[512]; int n = 0;
        for (k = 0; k < T_N - 1; k++) if (g_pf[k] >= 0.5) n += _snprintf(s + n, sizeof s - n, " %s %.1f", kTName[k], g_pf[k]);
        s[n] = 0;
        ALog("slow frame %.1f ms: streaming %.1f ms (%u chunks %u KB, %u textures, %u frees)%s; tick before %.1f ms; stream thread:%s%s%s%s%s", dt, g_pf[T_TOTAL], g_pJobs, g_pBytes >> 10, g_pTex, g_pFree, s,
             g_ppTotal, mask & 1 ? " io" : "", mask & 2 ? " models" : "", mask & 4 ? " trees" : "", mask & 16 ? " frees" : "", mask ? "" : " idle"); }
}

void AgStream_FrameTick(void) {
    uint32_t ag, list, gl; int done = 0, c, steps = 0; double tick0 = Now();
    FrameTiming(tick0);
    if (g_state != 2) return;
    ag = AutoGen();
    if (!ag || !Ok(ag + 4, 16)) return;
    list = RD(ag + 4); g_ticks++;
    if (list != g_listPtr || Landscape() != g_lsPtr) {
        if (g_listPtr) ResetAll("autogen list or landscape changed");
        g_listPtr = list; g_lsPtr = Landscape();
    }
    gl = RD(G(VAR_GLIDER));
    if (Ok(gl, 4) && Ok(RD(gl) + 0x6d8, 24)) { g_gx = *(double*)(uintptr_t)(RD(gl) + 0x6d8); g_gy = *(double*)(uintptr_t)(RD(gl) + 0x6e0); g_gz = *(double*)(uintptr_t)(RD(gl) + 0x6e8); g_haveGlider = 1; }
    CameraTick();
    // main-thread work of queued jobs within the time budget (at least one step per frame)
    while (steps == 0 || Now() - tick0 < g_budgetMs) {
        int r;
        if (!g_cur) {
            EnterCriticalSection(&g_qlock); g_cur = g_qhead; if (g_cur) { g_qhead = g_cur->next; if (!g_qhead) g_qtail = NULL; g_qn--; } LeaveCriticalSection(&g_qlock);
            if (!g_cur) break;
        }
        r = StageJob(g_cur, list); steps++;
        if (r == 2) break;
        if (r <= 0) g_cur = NULL;
        else if (g_model[g_cur->chunk] && g_cur->stage == 2 && g_treeBusy == g_cur->chunk) break;   // waits for the trees: next frame
    }
    // drop chunks nobody wants (within the budget, at least one per frame)
    for (c = 0, done = 0; c < (int)g_L.n_chunks && (done == 0 || Now() - tick0 < g_budgetMs); c++)
        if (g_res[c] >= 0 && g_want[c] < 0 && !TreeBusy(c)) { FreeModel(c); g_res[c] = -1; done++; }
    if (g_uploads) { ID3D11DeviceContext_Flush(Ctx()); g_uploads = 0; }          // copies to the GPU now: staging resources free sooner
    if (g_ticks % 300 == 0) TexSweep();
    // collision: only the LOD 0 chunk under the glider gets its real tile number (the game tests one model per tile), and only once
    // its triangle trees exist; chunks within 500 m of the glider get their trees (one request at a time)
    if (g_haveGlider) {
        float ts = g_L.tile_size; int gtx = (int)floor(g_gx / ts), gty = (int)floor(g_gy / ts), req = -1; double reqD = 1e30;
        for (c = 0; c < (int)g_L.n_chunks; c++) if (g_model[c]) {
            const float* b = &g_box[c * 4]; int under = g_res[c] == 0 && g_gx >= b[0] && g_gx < b[2] && g_gy >= b[1] && g_gy < b[3];
            int has = g_treeFor[c] == g_model[c];
            if (g_res[c] == 0 && !has) {
                double dx = g_gx < b[0] ? b[0] - g_gx : g_gx > b[2] ? g_gx - b[2] : 0, dy = g_gy < b[1] ? b[1] - g_gy : g_gy > b[3] ? g_gy - b[3] : 0, d = dx * dx + dy * dy;
                if (d < 500.0 * 500.0) {
                    if (!RD(g_model[c] + 0xe4)) g_needArr[c] = 1;        // loaded far away without the CPU copy: load again with it
                    else if (d < reqD) { reqD = d; req = c; }
                }
            }
            under = under && has;
            *(int32_t*)(uintptr_t)(g_model[c] + 0xec) = under ? gtx : -1000; *(int32_t*)(uintptr_t)(g_model[c] + 0xf0) = under ? gty : -1000;
        }
        if (req >= 0 && g_treeReq < 0 && g_treeBusy < 0) {
            EnterCriticalSection(&g_treeLock); g_treeReqModel = g_model[req]; g_treeReq = req; LeaveCriticalSection(&g_treeLock);
        }
    }
    g_tf[T_TOTAL] += Now() - tick0;
}

// AfterTesselate (autogen) pre-handler: the game frees every model outside its 4x4 window; park ours at the window centre so they
// survive (AgStream_FrameTick puts the real values back before the next collision test). It also recomputes their positions (floating origin).
static void AfterTessPre(void* address, DobbyRegisterContext* rc) {
    uint32_t ag = rc->general.regs.eax; int c;
    (void)address;
    if (g_state != 2 || !ag || !g_model) return;
    for (c = 0; c < (int)g_L.n_chunks; c++) if (g_model[c]) {
        *(int32_t*)(uintptr_t)(g_model[c] + 0xec) = (int32_t)RD(ag + 0xc); *(int32_t*)(uintptr_t)(g_model[c] + 0xf0) = (int32_t)RD(ag + 0x10);
    }
}

// ---- stream thread ---------------------------------------------------------------------------------------------
// Level of detail by screen-space error: a LOD whose geometric error (metres, from the helper) would look smaller than g_tolPx
// pixels from the camera is good enough; the coarsest such LOD is used. 3D distance from the camera (altitude counts) to the
// chunk's box. Beyond g_range nothing. The chunk under a low glider always gets full detail (collision).
static double BoxDist(int c, double x, double y, double z) {
    const float* b = &g_box[c * 4]; const float* zb = &g_zb[c * 2]; double dx = 0, dy = 0, dz = 0;
    if (x < b[0]) dx = b[0] - x; else if (x > b[2]) dx = x - b[2];
    if (y < b[1]) dy = b[1] - y; else if (y > b[3]) dy = y - b[3];
    if (z < zb[0]) dz = zb[0] - z; else if (z > zb[1]) dz = z - zb[1];
    return sqrt(dx * dx + dy * dy + dz * dz);
}
static int WantLod(int c, int cur) {
    const C3hChunkInfo* in = &g_info[c]; const float* b = &g_box[c * 4]; double d; int l, best = -1;
    if (g_lodMode == 0) {                                                       // old: 2D distance from the glider, fixed ranges
        double dx = 0, dy = 0;
        if (g_gx < b[0]) dx = b[0] - g_gx; else if (g_gx > b[2]) dx = g_gx - b[2];
        if (g_gy < b[1]) dy = b[1] - g_gy; else if (g_gy > b[3]) dy = g_gy - b[3];
        d = sqrt(dx * dx + dy * dy);
        for (l = 0; l < (int)g_L.n_lods; l++) {
            float r = l < 3 ? g_r[l] : g_r[2];
            if (cur >= 0 && l == cur) r *= 1.1f;
            if (d < r && in->bytes[l]) return l;
        }
        return -1;
    }
    if (in->bytes[0] && g_gx >= b[0] && g_gx < b[2] && g_gy >= b[1] && g_gy < b[3] && g_gz - g_zb[c * 2 + 1] < 300.0) return 0;
    d = BoxDist(c, g_cx, g_cy, g_cz);
    if (d > g_range * (cur >= 0 ? 1.05 : 1.0)) return -1;
    if (g_lodMode == 2) {                                                       // distance per kind of level
        int coarse = -1, last = -1, k = 0;
        for (l = 0; l < (int)g_L.n_lods; l++) if (in->bytes[l]) { last = l; if (in->level[l] >= 0) coarse = l; }
        for (l = 0; l < (int)g_L.n_lods; l++) {
            double D;
            if (!in->bytes[l]) continue;
            if (l == last) return l;                                            // the coarsest there is: up to 'range'
            if (in->level[l] > 0 && l == coarse) D = g_blobs >= g_range ? 1e30 : (LevelDist(in->level[l]) > g_blobs ? LevelDist(in->level[l]) : g_blobs);
            else if (in->level[l] > 0) D = LevelDist(in->level[l]);
            else if (in->level[l] == 0) D = g_full;                             // full detail, landscape without detail sets
            else {                                                              // generated
                k++;
                if (coarse >= 0 && in->level[coarse] > 0) D = g_blobs * (double)(2 << (k - 1));
                else D = k == 1 ? g_blob1 : g_blob2;
            }
            if (d < D * (l == cur ? 1.1 : 1.0)) return l;
        }
        return last;
    }
    if (d < 1.0) d = 1.0;
    for (l = (int)g_L.n_lods - 1; l >= 0; l--) {
        double px;
        if (!in->bytes[l]) continue;
        px = in->err[l] * g_K / d;
        if (px <= g_tolPx * (l == cur ? 1.2 : 1.0)) return l;                   // hysteresis: keep the current LOD a bit longer
        best = l;
    }
    return best;
}

static int OpenInHelper(void) {
    char exe[MAX_PATH], logp[MAX_PATH], err[256]; int st, i; DWORD t0 = GetTickCount();
    if (!g_cl.hd) {
        _snprintf(exe, sizeof exe, "%s\\c3helper.exe", g_gameDir);
        _snprintf(logp, sizeof logp, "%s\\postfx\\c3helper.log", g_gameDir);
        if (!C3h_Start(&g_cl, exe, logp, err, sizeof err)) { ALog("helper start failed: %s", err); return 0; }
        ALog("helper started");
    }
    st = C3h_Request(&g_cl, C3H_REQ_OPEN, 0, 0, g_lsDir, 900000);
    ALog("OPEN %s -> %d in %lu ms: %s", g_lsDir, st, GetTickCount() - t0, g_cl.hd->resp_msg);
    if (st != 0) return 0;
    memcpy(&g_L, g_cl.arena, sizeof g_L);
    if (g_L.n_lods > C3H_MAX_LOD) g_L.n_lods = C3H_MAX_LOD;
    free(g_info); free(g_box); free(g_zb); free((void*)g_want); free((void*)g_res); free((void*)g_pend); free((void*)g_needArr); free(g_model); free(g_tex); free((void*)g_texHave); free((void*)g_texBad); free(g_treeFor);
    g_info = malloc(g_L.n_chunks * sizeof *g_info + 1); memcpy(g_info, g_cl.arena + sizeof g_L, g_L.n_chunks * sizeof *g_info);
    g_box = malloc(g_L.n_chunks * 16 + 16); g_zb = malloc(g_L.n_chunks * 8 + 8); g_want = malloc(g_L.n_chunks * 4 + 4); g_res = malloc(g_L.n_chunks * 4 + 4);
    g_pend = malloc(g_L.n_chunks * 4 + 4); g_needArr = calloc(g_L.n_chunks + 1, 1); g_model = calloc(g_L.n_chunks + 1, 4); g_treeFor = calloc(g_L.n_chunks + 1, 4);
    free(g_vb); free(g_ib); g_vb = calloc(g_L.n_chunks + 1, sizeof *g_vb); g_ib = calloc(g_L.n_chunks + 1, sizeof *g_ib);
    g_tex = calloc(g_L.n_textures * g_L.n_lods + 1, sizeof *g_tex); g_texHave = calloc(g_L.n_textures * g_L.n_lods + 1, 1); g_texBad = calloc(g_L.n_textures * g_L.n_lods + 1, 1);
    for (i = 0; i < (int)g_L.n_chunks; i++) {
        float cxw = ((float)g_info[i].tile_x + 0.5f) * g_L.tile_size, cyw = ((float)g_info[i].tile_y + 0.5f) * g_L.tile_size;
        g_box[i * 4 + 0] = cxw + g_info[i].min[0]; g_box[i * 4 + 1] = cyw + g_info[i].min[1];
        g_box[i * 4 + 2] = cxw + g_info[i].max[0]; g_box[i * 4 + 3] = cyw + g_info[i].max[1];
        g_zb[i * 2 + 0] = g_info[i].min[2]; g_zb[i * 2 + 1] = g_info[i].max[2];
        g_want[i] = -1; g_res[i] = -1; g_pend[i] = -1;
    }
    {   double e[C3H_MAX_LOD] = {0}; uint32_t k;                                // typical errors, for the log
        for (i = 0; i < (int)g_L.n_chunks; i++) for (k = 0; k < g_L.n_lods; k++) e[k] += g_info[i].err[k];
        ALog("LODs %u, mean geometric error %.2f / %.2f / %.2f / %.2f m; selection: %s, %.1f px, range %.0f m", g_L.n_lods,
             e[0] / (g_L.n_chunks + 1e-9), e[1] / (g_L.n_chunks + 1e-9), e[2] / (g_L.n_chunks + 1e-9), e[3] / (g_L.n_chunks + 1e-9),
             g_lodMode == 2 ? "distance per level" : g_lodMode ? "screen-space error" : "fixed ranges", g_tolPx, g_range); }
    g_listPtr = 0; g_lsPtr = 0;
    return 1;
}

static volatile LONG g_streamBusy;       // the stream thread is in a round of work (FlightEnd waits for it)
static DWORD WINAPI StreamThread(LPVOID unused) {
    (void)unused;
    for (;;) {
        int c, best = -1, bestLod = -1; double bestD = 1e30;
        InterlockedExchange(&g_streamBusy, 0);
        Sleep(g_state == 2 ? 15 : 200);
        InterlockedExchange(&g_streamBusy, 1);                                    // set before the state is read (see AgStream_FlightEnd)
        if (g_state == 1) { int ok = OpenInHelper(); if (g_state == 1) g_state = ok ? 2 : -1; continue; }
        if (g_state != 2 || !g_haveGlider || !g_haveCam) continue;
        BuildTrees();
        FreeQueued();
        {   static DWORD lastCfg; if (GetTickCount() - lastCfg > 1000) { lastCfg = GetTickCount(); ConfigCheck(); } }
        for (c = 0; c < (int)g_L.n_chunks; c++) g_want[c] = WantLod(c, g_res[c]);
        if (g_qn >= 3) continue;
        for (c = 0; c < (int)g_L.n_chunks; c++) {                                 // nearest chunk whose LOD must change
            double d;
            int upgrade = g_want[c] == 0 && g_res[c] == 0 && g_needArr[c] && g_pend[c] != 0;   // LOD 0 again, with CPU arrays
            if (!upgrade && (g_want[c] < 0 || g_want[c] == g_res[c] || g_pend[c] == g_want[c])) continue;
            d = BoxDist(c, g_cx, g_cy, g_cz);
            if (g_res[c] < 0 || upgrade) d *= 0.5;                                // missing chunks (and collision) before LOD changes
            if (d < bestD) { bestD = d; best = c; bestLod = g_want[c]; }
        }
        if (best < 0) continue;
        {   Job* j = calloc(1, sizeof *j); int st; const C3hChunk* h; const C3hPart* p; uint32_t i; double t0 = Now();
            InterlockedOr(&g_stMask, 1); g_stNow = 1;
            j->chunk = best; j->lod = bestLod; g_pend[best] = bestLod;
            {   const float* b = &g_box[best * 4];                               // CPU arrays for LOD 0 within 1 km of the glider
                double dx = g_gx < b[0] ? b[0] - g_gx : g_gx > b[2] ? g_gx - b[2] : 0, dy = g_gy < b[1] ? b[1] - g_gy : g_gy > b[3] ? g_gy - b[3] : 0;
                j->arr = bestLod == 0 && dx * dx + dy * dy < 1000.0 * 1000.0; }
            st = C3h_Request(&g_cl, C3H_REQ_CHUNK, best, bestLod, NULL, 120000);   // big landscapes: the first chunk of a tile reads the tile
            if (st == 1) {                                                         // nothing there (lazy tiles: known only now): never again
                memset(g_info[best].bytes, 0, sizeof g_info[best].bytes); g_pend[best] = -1; free(j); g_stNow = 0; g_emptyChunks++; continue;
            }
            if (st != 0) { ALog("chunk %d lod %d: status %d %s", best, bestLod, st, g_cl.hd->resp_msg); g_pend[best] = -1; free(j); g_stNow = 0; if (st < -100) g_state = -1; continue; }
            j->size = g_cl.hd->resp_size; j->blob = BigAlloc(j->size); memcpy(j->blob, g_cl.arena, j->size); g_bytesIn += j->size;
            h = (const C3hChunk*)j->blob; p = (const C3hPart*)(h + 1);
            if (g_info[best].max[2] >= 9000.0f && h->n_verts) {                  // lazy tiles: real bounds from the first data of the chunk
                const C3hVertex* v = (const C3hVertex*)(p + h->n_parts); float mn[3] = { 1e30f, 1e30f, 1e30f }, mx[3] = { -1e30f, -1e30f, -1e30f }; uint32_t q; int a;
                float cxw = ((float)g_info[best].tile_x + 0.5f) * g_L.tile_size, cyw = ((float)g_info[best].tile_y + 0.5f) * g_L.tile_size;
                for (q = 0; q < h->n_verts; q++) for (a = 0; a < 3; a++) { if (v[q].pos[a] < mn[a]) mn[a] = v[q].pos[a]; if (v[q].pos[a] > mx[a]) mx[a] = v[q].pos[a]; }
                memcpy(g_info[best].min, mn, sizeof mn); memcpy(g_info[best].max, mx, sizeof mx);
                g_box[best * 4 + 0] = cxw + mn[0]; g_box[best * 4 + 1] = cyw + mn[1]; g_box[best * 4 + 2] = cxw + mx[0]; g_box[best * 4 + 3] = cyw + mx[1];
                g_zb[best * 2 + 0] = mn[2]; g_zb[best * 2 + 1] = mx[2];
            }
            for (i = 0; i < h->n_parts && j->ntex < JOB_MAX_TEX; i++) if (p[i].texture >= 0) {
                uint32_t key = TexKey(p[i].texture, bestLod); int k, dup = 0;
                if (g_texHave[key] || g_texBad[key]) continue;
                for (k = 0; k < j->ntex; k++) dup |= j->tex[k].key == key;
                if (dup) continue;
                if (C3h_Request(&g_cl, C3H_REQ_TEXTURE, p[i].texture, bestLod, NULL, 10000) == 0) {
                    j->tex[j->ntex].key = key; j->tex[j->ntex].size = g_cl.hd->resp_size;
                    j->tex[j->ntex].dds = BigAlloc(g_cl.hd->resp_size); memcpy(j->tex[j->ntex].dds, g_cl.arena, g_cl.hd->resp_size);
                    g_bytesIn += g_cl.hd->resp_size; j->ntex++;
                } else { g_texBad[key] = 1; ALog("texture %d lod %d: helper could not deliver it (%s): drawn without", p[i].texture, bestLod, g_cl.hd->resp_msg); }
            }
            g_ioMs += Now() - t0; t0 = Now(); InterlockedOr(&g_stMask, 2); g_stNow = 2;
            j->model = BuildModelCpu(j);                                           // game objects + bounds, off the main thread
            g_cpuMs += Now() - t0; g_stNow = 0;
            if (!j->model) { g_pend[best] = -1; JobFree(j); continue; }
            EnterCriticalSection(&g_qlock);
            if (g_qtail) g_qtail->next = j; else g_qhead = j;
            g_qtail = j; g_qn++;
            LeaveCriticalSection(&g_qlock);
        }
    }
    return 0;
}

// ---- end of a flight ---------------------------------------------------------------------------------------------
// The game destroys the world, the landscape and the autogen list (with our models in it) when a flight ends. Nothing of ours
// may point into that afterwards: the next flight would write into freed models (AfterTessPre), free them a second time, and
// release textures the game's destructors already released with the models. So, at the start of the game's teardown (main
// thread, world still alive): stop the stream thread, take our models out of the list and free them, release every GPU object
// of ours and stop the helper (its memory goes back; the next flight starts it again).
static void FlightEndPre(void* address, DobbyRegisterContext* rc) {
    uint32_t c; int i; DWORD t0 = GetTickCount(); int st = g_state;
    (void)address; (void)rc;
    if (!g_enable || (st == 0 && !g_cl.hd)) return;
    g_state = 3;                                                               // the stream thread does nothing in state 3
    MemoryBarrier();
    while (g_streamBusy && GetTickCount() - t0 < 60000) Sleep(1);      // (an OPEN in progress can take a while)
    g_teardown = 1;
    while (g_freeTail != g_freeHead) {                                         // frees the stream thread had not done yet
        uint32_t m = g_freeQ[g_freeTail % FREEQ_N];
        DCall(G(FN_MODEL_FREEARR), m, 0, 0); DCall(G(FN_FREE), m, 0, 0);
        g_freeTail++;
    }
    if (g_cur) { JobDrop(g_cur); g_cur = NULL; }
    for (;;) { Job* j; EnterCriticalSection(&g_qlock); j = g_qhead; if (j) { g_qhead = j->next; if (!g_qhead) g_qtail = NULL; g_qn--; } LeaveCriticalSection(&g_qlock); if (!j) break; JobDrop(j); }
    g_treeReq = -1; g_treeBusy = -1;
    if (g_model && g_listPtr && AutoGen() && RD(AutoGen() + 4) == g_listPtr)
        for (c = 0; c < g_L.n_chunks; c++) if (g_model[c]) FreeModel((int)c);   // out of the game's list, borrowed textures cleared
    for (c = 0; g_model && c < g_L.n_chunks; c++) {                            // (list already gone: forget them, never touch them)
        g_model[c] = 0; g_res[c] = -1; g_want[c] = -1; g_pend[c] = -1; g_needArr[c] = 0; g_treeFor[c] = 0;
        if (g_vb[c]) ID3D11Buffer_Release(g_vb[c]);
        if (g_ib[c]) ID3D11Buffer_Release(g_ib[c]);
        g_vb[c] = g_ib[c] = NULL;
    }
    for (c = 0; g_tex && c < g_L.n_textures * g_L.n_lods; c++) {
        if (g_tex[c].srv) { ID3D11ShaderResourceView_Release(g_tex[c].srv); ID3D11Texture2D_Release(g_tex[c].tex); }
        g_tex[c].srv = NULL; g_tex[c].tex = NULL; g_tex[c].users = 0; g_texHave[c] = 0;
    }
    for (i = 0; i < g_npool; i++) { ID3D11ShaderResourceView_Release(g_pool[i].srv); ID3D11Texture2D_Release(g_pool[i].tex); }
    g_npool = 0; g_poolBytes = 0;
    for (i = 0; i < g_nbpool; i++) ID3D11Buffer_Release(g_bpool[i].b);
    g_nbpool = 0; g_bpoolBytes = 0;
    for (i = 0; i < STAGE_BUF_N; i++) if (g_stageBuf[i]) { ID3D11Buffer_Release(g_stageBuf[i]); g_stageBuf[i] = NULL; }
    for (i = 0; i < STAGE_TEX_N; i++) if (g_stageTex[i].t) { ID3D11Texture2D_Release(g_stageTex[i].t); memset(&g_stageTex[i], 0, sizeof g_stageTex[i]); }
    if (g_layout) { ID3D11InputLayout_Release(g_layout); g_layout = NULL; }
    if (g_cl.hd) C3h_Stop(&g_cl);
    g_lsDir[0] = 0; g_listPtr = 0; g_lsPtr = 0; g_zeroFor = 0; g_haveGlider = 0; g_haveCam = 0; g_uploads = 0;
    g_teardown = 0;
    MemoryBarrier();
    g_state = 0;
    ALog("flight end: streamed autogen freed, helper stopped (state was %d, %lu ms)", st, GetTickCount() - t0);
}

// ---- public ---------------------------------------------------------------------------------------------------
void AgStream_Stats(void (*out)(const char*, ...)) {
    int c, n[C3H_MAX_LOD + 1] = {0}, t = 0; uint32_t k;
    if (g_state != 2) { out("ag: state %d (%s)", g_state, g_lsDir); return; }
    for (c = 0; c < (int)g_L.n_chunks; c++) n[g_res[c] + 1]++;
    for (k = 0; k < g_L.n_textures * g_L.n_lods; k++) t += g_tex[k].srv != NULL;
    out("ag: %u chunks: none %d lod0 %d lod1 %d lod2 %d lod3 %d; textures %d; queue %d; created %u freed %u dropped %u (tex missing %u); MB in %u; tex created %u reused %u pool %d (%u MB); buf created %u reused %u pool %d (%u MB)",
        g_L.n_chunks, n[0], n[1], n[2], n[3], n[4], t, g_qn, g_created, g_freed, g_dropped, g_texMissing, (unsigned)(g_bytesIn >> 20), g_texCreated, g_texReused, g_npool, g_poolBytes >> 20, g_bufCreated, g_bufReused, g_nbpool, g_bpoolBytes >> 20);
    out("ag: glider %.0f %.0f %.0f, camera %.0f %.0f %.0f, K %.0f px/rad; stream thread: io %.0f ms, models %.0f ms, %u collision trees %.0f ms, %u frees %.0f ms",
        g_gx, g_gy, g_gz, g_cx, g_cy, g_cz, g_K, g_ioMs, g_cpuMs, g_treesBuilt, g_treeMs, g_freedLater, g_freeMs);
    out("ag: staging: %u new textures, %u postponed uploads; %u chunk slots turned out empty", g_stageNew, g_stageBusy, g_emptyChunks);
    {   unsigned long long tris = 0; unsigned parts = 0, byLevel[33] = {0}, gen = 0, unk = 0; int lv;
        for (c = 0; c < (int)g_L.n_chunks; c++) if (g_res[c] >= 0 && g_model[c]) {
            tris += g_info[c].tris[g_res[c]]; parts += RD(g_model[c] + 8);
            lv = g_info[c].level[g_res[c]];
            if (lv > 0 && lv < 33) byLevel[lv]++; else if (lv == 0) unk++; else gen++;
        }
        out("ag: resident %.1f M triangles in %u parts (draw calls); chunks by kind: L20 %u L19 %u L18 %u L17 %u full(unknown level) %u generated %u; %.1f fps",
            tris / 1e6, parts, byLevel[20], byLevel[19], byLevel[18], byLevel[17], unk, gen, g_fps); }
    out("ag frames: %u, slowest %.0f ms; slow >30 ms %u (streaming caused %u), >50 %u (%u), >100 %u (%u), >200 %u (%u)",
        g_frameN, g_frameMax, g_slow[0], g_slowOurs[0], g_slow[1], g_slowOurs[1], g_slow[2], g_slowOurs[2], g_slow[3], g_slowOurs[3]);
}

void InstallAgStream(void) {
    char exe[MAX_PATH], p[MAX_PATH], *s; int r1, r2, r3;
    GetModuleFileNameA(NULL, exe, MAX_PATH);
    s = strrchr(exe, '\\'); if (s) *s = 0;
    strncpy(g_gameDir, exe, sizeof g_gameDir - 1);
    _snprintf(p, sizeof p, "%s\\postfx\\agstream.log", g_gameDir);
    g_log = fopen(p, "w");
    LoadConfig();
    if (!g_cfgMarker) WriteConfigTemplate();
    _snprintf(p, sizeof p, "%s\\c3helper.exe", g_gameDir);
    if (GetFileAttributesA(p) == INVALID_FILE_ATTRIBUTES) { ALog("no %s: autogen streaming off", p); g_enable = 0; }
    ALog("agstream: enable %d, lodmode %d (2 = distances: range %.0f, L20 %.0f L19 %.0f L18 %.0f L17 %.0f, blobs %.0f; without sets full %.0f blob1 %.0f blob2 %.0f), budget %.1f ms",
         g_enable, g_lodMode, g_range, LevelDist(20), LevelDist(19), LevelDist(18), LevelDist(17), g_blobs, g_full, g_blob1, g_blob2, g_budgetMs);
    if (!g_enable) return;
    InitializeCriticalSection(&g_qlock); InitializeCriticalSection(&g_treeLock);
    r1 = DobbyInstrument(IdaAddrToRuntime(FN_CACHE_EXECUTE), CacheExecutePre);
    r2 = 0;                                                                     // per-frame work: AgStream_FrameTick from the HUD hook
    r3 = DobbyInstrument(IdaAddrToRuntime(FN_AG_AFTERTESS), AfterTessPre);
    r2 = DobbyInstrument(IdaAddrToRuntime(FN_SIM_TEARDOWN), FlightEndPre);
    ALog("hooks: cache execute %d, flight end %d, aftertess %d", r1, r2, r3);
    if (r1 || r2 || r3) { g_enable = 0; return; }
    CreateThread(NULL, 0, StreamThread, NULL, 0, NULL);
}
