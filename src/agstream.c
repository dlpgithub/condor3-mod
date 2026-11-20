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
// Everything that touches game objects or D3D runs on the main thread (AgStream_FrameTick, called by the HUD hook of
// postfx_hook.c once per frame); the stream thread only does IPC.
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

// ---- config -------------------------------------------------------------------------------------------------
static int   g_enable = 1;
static float g_r[C3H_MAX_LOD] = { 2000.0f, 5000.0f, 12000.0f };   // LOD n is used up to distance g_r[n] (metres, 2D, to the chunk box)
static int   g_jobsPerTick = 1;
static int   g_noGpu = 0;                                        // test switch: build models but create no GPU objects, draw nothing
static void LoadConfig(void) {
    char p[MAX_PATH], line[128]; FILE* f;
    _snprintf(p, sizeof p, "%s\\postfx\\agstream.txt", g_gameDir);
    f = fopen(p, "r");
    if (!f) return;
    while (fgets(line, sizeof line, f)) {
        char k[32]; float v;
        if (sscanf(line, "%31s %f", k, &v) != 2) continue;
        if (!_stricmp(k, "enable")) g_enable = (int)v;
        else if (!_stricmp(k, "r0")) g_r[0] = v; else if (!_stricmp(k, "r1")) g_r[1] = v; else if (!_stricmp(k, "r2")) g_r[2] = v;
        else if (!_stricmp(k, "jobs")) g_jobsPerTick = (int)v;
        else if (!_stricmp(k, "nogpu")) g_noGpu = (int)v;
    }
    fclose(f);
}

// ---- state ---------------------------------------------------------------------------------------------------
typedef struct { uint32_t key; uint8_t* dds; uint32_t size; } JobTex;
typedef struct Job { struct Job* next; int chunk, lod; uint8_t* blob; uint32_t size; int ntex; JobTex tex[32]; } Job;
typedef struct { ID3D11ShaderResourceView* srv; ID3D11Texture2D* tex; int users; } TexEntry;

static C3hClient     g_cl;
static volatile int  g_state;            // 0 idle, 1 open requested, 2 streaming, -1 failed
static char          g_lsDir[MAX_PATH];
static C3hLandscape  g_L;
static C3hChunkInfo* g_info;
static float*        g_box;              // per chunk world AABB 2D: minx, miny, maxx, maxy
static volatile int* g_want;             // stream thread: desired LOD (-1 none)
static volatile int* g_res;              // main thread: resident LOD (-1 none)
static volatile int* g_pend;             // LOD of the queued job (-1 none)
static uint32_t*     g_model;            // main thread: game model per chunk
static TexEntry*     g_tex;              // [n_textures * n_lods], main thread
static volatile uint8_t* g_texHave;      // 1 = created (main thread), read by the stream thread
static CRITICAL_SECTION g_qlock; static Job* g_qhead; static Job* g_qtail; static volatile int g_qn;
static uint32_t g_lsPtr, g_listPtr;      // landscape object / autogen resident list we stream into
static volatile double g_gx, g_gy;       // glider position (main thread copies it each tick)
static volatile int g_haveGlider;
static unsigned g_ticks, g_created, g_freed, g_bytesIn;

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
    if (!g_enable || g_state < 0 || !L || !Ok(L + 0x1a4, 8)) return;
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
static StageTex g_stageTex[6]; static unsigned g_stageClock;
static ID3D11Device* Dev(void) { return (ID3D11Device*)(uintptr_t)RD(G(VAR_DEVICE)); }
static ID3D11DeviceContext* Ctx(void) { return (ID3D11DeviceContext*)(uintptr_t)RD(G(VAR_CONTEXT)); }
static void UploadBuffer(ID3D11Buffer* dst, const uint8_t* data, uint32_t bytes) {
    ID3D11DeviceContext* ctx = Ctx(); uint32_t off = 0;
    while (off < bytes) {
        uint32_t n = bytes - off > STAGE_BUF_SIZE ? STAGE_BUF_SIZE : bytes - off;
        ID3D11Buffer** ps = &g_stageBuf[g_stageNext]; D3D11_MAPPED_SUBRESOURCE ms; D3D11_BOX box = { 0, 0, 0, n, 1, 1 };
        g_stageNext = (g_stageNext + 1) % STAGE_BUF_N;
        if (!*ps) {
            D3D11_BUFFER_DESC bd; memset(&bd, 0, sizeof bd);
            bd.ByteWidth = STAGE_BUF_SIZE; bd.Usage = D3D11_USAGE_STAGING; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            if (FAILED(ID3D11Device_CreateBuffer(Dev(), &bd, NULL, ps))) *ps = NULL;
        }
        if (*ps && SUCCEEDED(ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)*ps, 0, D3D11_MAP_WRITE, 0, &ms))) {
            memcpy(ms.pData, data + off, n);
            ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource*)*ps, 0);
            ID3D11DeviceContext_CopySubresourceRegion(ctx, (ID3D11Resource*)dst, 0, off, 0, 0, (ID3D11Resource*)*ps, 0, &box);
        } else {                                                                  // fallback
            D3D11_BOX b2 = { off, 0, 0, off + n, 1, 1 };
            ID3D11DeviceContext_UpdateSubresource(ctx, (ID3D11Resource*)dst, 0, &b2, data + off, n, 0);
        }
        off += n;
    }
}
// blocks: rows of 4x4 blocks for BC formats, rows of pixels for 32-bit
static void UploadTexture(ID3D11Texture2D* dst, uint32_t w, uint32_t h, uint32_t mips, uint32_t fmt, const D3D11_SUBRESOURCE_DATA* sd) {
    ID3D11DeviceContext* ctx = Ctx(); StageTex* st = NULL; int i; uint32_t m;
    for (i = 0; i < 6; i++) if (g_stageTex[i].t && g_stageTex[i].w == w && g_stageTex[i].h == h && g_stageTex[i].mips == mips && g_stageTex[i].fmt == fmt) { st = &g_stageTex[i]; break; }
    if (!st) {                                                                    // replace the least recently used slot
        D3D11_TEXTURE2D_DESC td; int lru = 0;
        for (i = 1; i < 6; i++) if (!g_stageTex[i].t || (g_stageTex[lru].t && g_stageTex[i].used < g_stageTex[lru].used)) lru = i;
        st = &g_stageTex[lru];
        if (st->t) ID3D11Texture2D_Release(st->t);
        memset(&td, 0, sizeof td);
        td.Width = w; td.Height = h; td.MipLevels = mips; td.ArraySize = 1; td.Format = (DXGI_FORMAT)fmt; td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_STAGING; td.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        st->t = NULL; st->w = w; st->h = h; st->mips = mips; st->fmt = fmt;
        if (FAILED(ID3D11Device_CreateTexture2D(Dev(), &td, NULL, &st->t))) st->t = NULL;
    }
    st->used = ++g_stageClock;
    for (m = 0; m < mips; m++) {
        uint32_t mh = h >> m, rows; D3D11_MAPPED_SUBRESOURCE ms;
        if (!mh) mh = 1;
        rows = (fmt == DXGI_FORMAT_B8G8R8A8_UNORM) ? mh : (mh + 3) / 4;
        if (st->t && SUCCEEDED(ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)st->t, m, D3D11_MAP_WRITE, 0, &ms))) {
            uint32_t r;
            for (r = 0; r < rows; r++) memcpy((uint8_t*)ms.pData + (size_t)r * ms.RowPitch, (const uint8_t*)sd[m].pSysMem + (size_t)r * sd[m].SysMemPitch, sd[m].SysMemPitch);
            ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource*)st->t, m);
        } else ID3D11DeviceContext_UpdateSubresource(ctx, (ID3D11Resource*)dst, m, NULL, sd[m].pSysMem, sd[m].SysMemPitch, 0);
    }
    if (st->t) ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource*)dst, (ID3D11Resource*)st->t);
}

// ---- textures ----------------------------------------------------------------------------------------------
// Textures are pooled by (format, size, mips): a texture no chunk uses any more goes into a free pool and is refilled with
// UpdateSubresource for the next one of the same shape. Creating/destroying them instead fragments DXVK's 16 MB memory chunks
// (measured: +100 MB per 400 streamed chunks); the atlas pages have one size per LOD, so the pool is almost always hit.

typedef struct { ID3D11Texture2D* tex; ID3D11ShaderResourceView* srv; uint32_t w, h, mips, fmt, bytes; } PoolTex;
static PoolTex g_pool[96]; static int g_npool; static uint32_t g_poolBytes, g_texCreated, g_texReused;
#define POOL_MAX_BYTES (96u << 20)

typedef struct { uint32_t w, h, mips, fmt, bytes; D3D11_SUBRESOURCE_DATA sd[16]; } DdsInfo;
static int ParseDds(const uint8_t* d, uint32_t n, DdsInfo* di) {
    uint32_t pf, fourcc, bits, i; int bs = 0; size_t off = 128;
    if (n < 128 || memcmp(d, "DDS ", 4)) return 0;
    memcpy(&di->h, d + 12, 4); memcpy(&di->w, d + 16, 4); memcpy(&di->mips, d + 28, 4); memcpy(&pf, d + 80, 4); memcpy(&fourcc, d + 84, 4); memcpy(&bits, d + 88, 4);
    if (di->mips < 1) di->mips = 1; if (di->mips > 16) di->mips = 16;
    if (pf & 4) {
        if (fourcc == 0x31545844) { di->fmt = DXGI_FORMAT_BC1_UNORM; bs = 8; }
        else if (fourcc == 0x33545844) { di->fmt = DXGI_FORMAT_BC2_UNORM; bs = 16; }
        else if (fourcc == 0x35545844) { di->fmt = DXGI_FORMAT_BC3_UNORM; bs = 16; }
        else return 0;
    } else if (bits == 32) di->fmt = DXGI_FORMAT_B8G8R8A8_UNORM; else return 0;
    for (i = 0; i < di->mips; i++) {
        uint32_t mw = di->w >> i, mh = di->h >> i; size_t sz;
        if (!mw) mw = 1; if (!mh) mh = 1;
        sz = bs ? (size_t)((mw + 3) / 4) * ((mh + 3) / 4) * bs : (size_t)mw * mh * 4;
        if (off + sz > n) { di->mips = i; break; }
        di->sd[i].pSysMem = d + off; di->sd[i].SysMemPitch = bs ? ((mw + 3) / 4) * bs : mw * 4; di->sd[i].SysMemSlicePitch = (UINT)sz;
        off += sz;
    }
    di->bytes = (uint32_t)(off - 128);
    return di->mips > 0;
}
static ID3D11ShaderResourceView* TexAcquire(const uint8_t* d, uint32_t n, ID3D11Texture2D** outTex) {
    DdsInfo di; int i; ID3D11Device* dev = (ID3D11Device*)(uintptr_t)RD(G(VAR_DEVICE));
    ID3D11DeviceContext* ctx = (ID3D11DeviceContext*)(uintptr_t)RD(G(VAR_CONTEXT));
    D3D11_TEXTURE2D_DESC td; ID3D11Texture2D* tex = NULL; ID3D11ShaderResourceView* srv = NULL;
    *outTex = NULL;
    if (!dev || !ctx || !ParseDds(d, n, &di)) return NULL;
    for (i = g_npool - 1; i >= 0; i--) {
        PoolTex* p = &g_pool[i];
        if (p->w == di.w && p->h == di.h && p->mips == di.mips && p->fmt == di.fmt) {
            UploadTexture(p->tex, di.w, di.h, di.mips, di.fmt, di.sd);
            *outTex = p->tex; srv = p->srv;
            g_poolBytes -= p->bytes; g_pool[i] = g_pool[--g_npool]; g_texReused++;
            return srv;
        }
    }
    memset(&td, 0, sizeof td);
    td.Width = di.w; td.Height = di.h; td.MipLevels = di.mips; td.ArraySize = 1; td.Format = (DXGI_FORMAT)di.fmt; td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(ID3D11Device_CreateTexture2D(dev, &td, NULL, &tex))) return NULL;
    UploadTexture(tex, di.w, di.h, di.mips, di.fmt, di.sd);
    if (FAILED(ID3D11Device_CreateShaderResourceView(dev, (ID3D11Resource*)tex, NULL, &srv))) { ID3D11Texture2D_Release(tex); return NULL; }
    *outTex = tex; g_texCreated++;
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
static ID3D11Buffer* BufAcquire(uint32_t bind, const void* data, uint32_t bytes) {
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
    UploadBuffer(b, (const uint8_t*)data, bytes);
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

// ---- models (main thread) ----------------------------------------------------------------------------------------
static int ListIndex(uint32_t list, uint32_t item) {
    int n = (int)RD(list + 8), i; uint32_t items = RD(list + 4);
    for (i = 0; i < n; i++) if (RD(items + 4 * i) == item) return i;
    return -1;
}
// Removes chunk c's model from the game's list and frees it. The parts' texture fields are borrowed from our cache (no AddRef),
// so they are cleared first and the game's destructor never touches our SRVs; then our cache references are dropped.
static unsigned g_refLog;
static void FreeModel(int c) {
    uint32_t m = g_model[c], list = g_listPtr, keys[64]; int i, n, nk = 0, lod = g_res[c];
    if (!m) return;
    if (list && (i = ListIndex(list, m)) >= 0) DCall(G(FN_LIST_DELETE), list, (uint32_t)i, 0);
    n = (int)RD(m + 8);
    for (i = 0; i < n; i++) {
        uint32_t part = RD(RD(m + 4) + 4 * i), srv = RD(part + 0x14c), k;
        if (!srv) continue;
        *(uint32_t*)(uintptr_t)(part + 0x14c) = 0;
        for (k = 0; lod >= 0 && k < g_L.n_textures && nk < 64; k++) { uint32_t key = TexKey((int)k, lod); if ((uint32_t)(uintptr_t)g_tex[key].srv == srv) { keys[nk++] = key; break; } }
    }
    DCall(G(FN_MODEL_FREEARR), m, 0, 0);                           // CPU copy (LOD 0 keeps it for collision); no-op when already freed
    if (g_refLog < 3) {                                            // diagnostics: does the game's destructor release VB / IB / layout?
        IUnknown* o[3]; ULONG r[3]; int q;
        for (q = 0; q < 3; q++) { o[q] = (IUnknown*)(uintptr_t)RD(m + 0x24 + 4 * q); if (o[q]) IUnknown_AddRef(o[q]); }
        DCall(G(FN_FREE), m, 0, 0);
        for (q = 0; q < 3; q++) r[q] = o[q] ? IUnknown_Release(o[q]) : 99;
        ALog("free chunk %d: refs left after free VB %lu IB %lu layout %lu (0 = released by the game)", c, r[0], r[1], r[2]);
    } else DCall(G(FN_FREE), m, 0, 0);
    if (g_refLog < 3 && nk) {                                      // diagnostics: our reference must be the only one left
        ID3D11ShaderResourceView* s = g_tex[keys[0]].srv; ULONG r = ID3D11ShaderResourceView_AddRef(s); ID3D11ShaderResourceView_Release(s);
        ALog("free chunk %d: srv refcount after free %lu (users %d)", c, r - 1, g_tex[keys[0]].users); g_refLog++;
    }
    for (i = 0; i < nk; i++) TexRelease(keys[i]);
    if (g_vb[c]) BufToPool(g_vb[c], D3D11_BIND_VERTEX_BUFFER);
    if (g_ib[c]) BufToPool(g_ib[c], D3D11_BIND_INDEX_BUFFER);
    g_vb[c] = g_ib[c] = NULL;
    g_model[c] = 0; g_freed++;
}

// builds a game model from one chunk answer (main thread)
static uint32_t BuildModel(const Job* j) {
    const C3hChunk* h = (const C3hChunk*)j->blob; const C3hPart* p = (const C3hPart*)(h + 1);
    const C3hVertex* v = (const C3hVertex*)(p + h->n_parts); const uint32_t* idx = (const uint32_t*)(v + h->n_verts);
    uint32_t m, i, L = g_lsPtr; float cell = *(float*)(uintptr_t)(L + 0x1b8), pos[3];
    m = DCallPush10(G(FN_MODEL_CREATE), RD(G(VAR_MODELCLASS)), 1, 0);         // = the game's cache thread call (no D3D yet)
    if (!m) return 0;
    *(uint8_t*)(uintptr_t)(m + 0x9c) = 1;
    DSetLength((void*)(uintptr_t)(m + 0x58), RD(G(VAR_TI_GROUPS)), 1);
    ((uint32_t*)(uintptr_t)RD(m + 0x58))[0] = 0; ((uint32_t*)(uintptr_t)RD(m + 0x58))[1] = h->n_parts;
    for (i = 0; i < h->n_parts; i++) {
        uint32_t part = DCall(G(FN_PART_CREATE), RD(G(VAR_PARTCLASS)), 1, 0); size_t nl = strlen(p[i].name);
        uint8_t* b = (uint8_t*)(uintptr_t)part;
        if (nl > 31) nl = 31;
        b[0x10] = (uint8_t)nl; memcpy(b + 0x11, p[i].name, nl);
        b[0xa4] = 0;                                                            // no texture name: we assign our own SRV below
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
    memcpy((void*)(uintptr_t)(m + 0x5c), (void*)(uintptr_t)RD(G(VAR_IDENTITY)), 64);
    *(int32_t*)(uintptr_t)(m + 0xec) = -1000; *(int32_t*)(uintptr_t)(m + 0xf0) = -1000;   // no collision unless under the glider
    pos[0] = ((float)h->tile_x + 0.5f) * 64.0f * cell; pos[1] = ((float)h->tile_y + 0.5f) * 64.0f * cell; pos[2] = 0;
    memcpy((void*)(uintptr_t)(m + 0xf4), pos, 12);
    *(float*)(uintptr_t)(m + 0xd0) = 0.2f;
    // main-thread part of the game's AfterTesselate
    *(uint32_t*)(uintptr_t)(m + 0x50) = DCall(G(FN_EFFECT), RD(RD(G(VAR_EFFECTMGR))), 0, 0);
    if (g_noGpu) ;
    else if (!g_layout) {                                                         // first model: the game's own function, keep its layout
        DCall(G(FN_MODEL_GPU), m, 1, 0);
        g_layout = (ID3D11InputLayout*)(uintptr_t)RD(m + 0x2c);
        if (g_layout) ID3D11InputLayout_AddRef(g_layout);
    } else {
        ID3D11Buffer* vb = BufAcquire(D3D11_BIND_VERTEX_BUFFER, v, h->n_verts * 32), * ib = BufAcquire(D3D11_BIND_INDEX_BUFFER, idx, h->n_indices * 4);
        if (vb) ID3D11Buffer_AddRef(vb);                                          // one reference for the model (released by the game's
        if (ib) ID3D11Buffer_AddRef(ib);                                          // destructor), one stays with us for the pool
        ID3D11InputLayout_AddRef(g_layout);
        *(uint32_t*)(uintptr_t)(m + 0x24) = (uint32_t)(uintptr_t)vb; *(uint32_t*)(uintptr_t)(m + 0x28) = (uint32_t)(uintptr_t)ib;
        *(uint32_t*)(uintptr_t)(m + 0x2c) = (uint32_t)(uintptr_t)g_layout;
        g_newVb = vb; g_newIb = ib;
    }
    DCall(G(FN_MODEL_POS), L, m + 0x100, m + 0xf4);
    *(uint32_t*)(uintptr_t)(m + 0x8c) = RD(m + 0x100); *(uint32_t*)(uintptr_t)(m + 0x90) = RD(m + 0x104); *(uint32_t*)(uintptr_t)(m + 0x94) = 0;
    if (j->lod != 0) DCall(G(FN_MODEL_FREEARR), m, 0, 0);                         // LOD 0 keeps its CPU copy for the collision test
    // textures
    for (i = 0; i < h->n_parts && !g_noGpu; i++) if (p[i].texture >= 0) {
        uint32_t key = TexKey(p[i].texture, j->lod), part = RD(RD(m + 4) + 4 * i);
        TexEntry* t = &g_tex[key];
        if (!t->srv) { int k; for (k = 0; k < j->ntex; k++) if (j->tex[k].key == key) { t->srv = TexAcquire(j->tex[k].dds, j->tex[k].size, &t->tex); break; } if (t->srv) g_texHave[key] = 1; }
        if (t->srv) {
            *(uint32_t*)(uintptr_t)(part + 0x14c) = (uint32_t)(uintptr_t)t->srv;   // borrowed: FreeModel clears it before the game frees the part
            t->users++;
        }
    }
    g_created++;
    return m;
}

static void ResetAll(const char* why) {
    uint32_t c;
    ALog("reset (%s)", why);
    for (c = 0; g_model && c < g_L.n_chunks; c++) { g_model[c] = 0; g_res[c] = -1; if (g_vb[c]) ID3D11Buffer_Release(g_vb[c]); if (g_ib[c]) ID3D11Buffer_Release(g_ib[c]); g_vb[c] = g_ib[c] = NULL; }
    for (c = 0; g_tex && c < g_L.n_textures * g_L.n_lods; c++) { if (g_tex[c].srv) { ID3D11ShaderResourceView_Release(g_tex[c].srv); ID3D11Texture2D_Release(g_tex[c].tex); } g_tex[c].srv = NULL; g_tex[c].tex = NULL; g_tex[c].users = 0; g_texHave[c] = 0; }
}

// Once per rendered frame from the HUD hook (main thread, also while the sim is paused). Runs after the frame's update
// (incl. a landscape refresh) and before the next one.
void AgStream_FrameTick(void) {
    uint32_t ag, list, gl; int done = 0, c;
    if (g_state != 2) return;
    ag = AutoGen();
    if (!ag || !Ok(ag + 4, 16)) return;
    list = RD(ag + 4); g_ticks++;
    if (list != g_listPtr || Landscape() != g_lsPtr) {
        if (g_listPtr) ResetAll("autogen list or landscape changed");
        g_listPtr = list; g_lsPtr = Landscape();
    }
    gl = RD(G(VAR_GLIDER));
    if (Ok(gl, 4) && Ok(RD(gl) + 0x6d8, 24)) { g_gx = *(double*)(uintptr_t)(RD(gl) + 0x6d8); g_gy = *(double*)(uintptr_t)(RD(gl) + 0x6e0); g_haveGlider = 1; }
    // integrate queued chunks
    while (done < g_jobsPerTick) {
        Job* j;
        EnterCriticalSection(&g_qlock); j = g_qhead; if (j) { g_qhead = j->next; if (!g_qhead) g_qtail = NULL; g_qn--; } LeaveCriticalSection(&g_qlock);
        if (!j) break;
        if (g_want[j->chunk] == j->lod && g_res[j->chunk] != j->lod) {
            uint32_t m;
            g_newVb = g_newIb = NULL;
            m = BuildModel(j);
            if (m) {
                if (g_model[j->chunk]) FreeModel(j->chunk);
                g_vb[j->chunk] = g_newVb; g_ib[j->chunk] = g_newIb;
                if (!g_noGpu) DCall(G(FN_LIST_ADD), list, m, 0);
                g_model[j->chunk] = m; g_res[j->chunk] = j->lod;
            }
            done++;
        }
        g_pend[j->chunk] = -1;
        { int k; for (k = 0; k < j->ntex; k++) BigFree(j->tex[k].dds); }
        BigFree(j->blob); free(j);
    }
    // drop chunks nobody wants (a few per frame)
    for (c = 0, done = 0; c < (int)g_L.n_chunks && done < 4; c++)
        if (g_res[c] >= 0 && g_want[c] < 0) { FreeModel(c); g_res[c] = -1; done++; }
    // collision: only the LOD 0 chunk under the glider gets its real tile number (the game tests one model per tile)
    if (g_haveGlider) {
        float ts = g_L.tile_size; int gtx = (int)floor(g_gx / ts), gty = (int)floor(g_gy / ts);
        for (c = 0; c < (int)g_L.n_chunks; c++) if (g_model[c]) {
            const float* b = &g_box[c * 4]; int under = g_res[c] == 0 && g_gx >= b[0] && g_gx < b[2] && g_gy >= b[1] && g_gy < b[3];
            *(int32_t*)(uintptr_t)(g_model[c] + 0xec) = under ? gtx : -1000; *(int32_t*)(uintptr_t)(g_model[c] + 0xf0) = under ? gty : -1000;
        }
    }
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
static int WantLod(int c, int cur) {
    const float* b = &g_box[c * 4]; double dx = 0, dy = 0, d; int l;
    if (g_gx < b[0]) dx = b[0] - g_gx; else if (g_gx > b[2]) dx = g_gx - b[2];
    if (g_gy < b[1]) dy = b[1] - g_gy; else if (g_gy > b[3]) dy = g_gy - b[3];
    d = sqrt(dx * dx + dy * dy);
    for (l = 0; l < (int)g_L.n_lods; l++) {
        float r = g_r[l];
        if (cur >= 0 && l == cur) r *= 1.1f;                                      // hysteresis: keep what we have a bit longer
        if (d < r && g_info[c].bytes[l]) return l;
    }
    return -1;
}

static int OpenInHelper(void) {
    char exe[MAX_PATH], logp[MAX_PATH], err[256]; int st, i; DWORD t0 = GetTickCount();
    if (!g_cl.hd) {
        _snprintf(exe, sizeof exe, "%s\\c3helper.exe", g_gameDir);
        _snprintf(logp, sizeof logp, "%s\\postfx\\c3helper.log", g_gameDir);
        if (!C3h_Start(&g_cl, exe, logp, err, sizeof err)) { ALog("helper start failed: %s", err); return 0; }
        ALog("helper started");
    }
    st = C3h_Request(&g_cl, C3H_REQ_OPEN, 0, 0, g_lsDir, 600000);
    ALog("OPEN %s -> %d in %lu ms: %s", g_lsDir, st, GetTickCount() - t0, g_cl.hd->resp_msg);
    if (st != 0) return 0;
    memcpy(&g_L, g_cl.arena, sizeof g_L);
    free(g_info); free(g_box); free((void*)g_want); free((void*)g_res); free((void*)g_pend); free(g_model); free(g_tex); free((void*)g_texHave);
    g_info = malloc(g_L.n_chunks * sizeof *g_info + 1); memcpy(g_info, g_cl.arena + sizeof g_L, g_L.n_chunks * sizeof *g_info);
    g_box = malloc(g_L.n_chunks * 16 + 16); g_want = malloc(g_L.n_chunks * 4 + 4); g_res = malloc(g_L.n_chunks * 4 + 4);
    g_pend = malloc(g_L.n_chunks * 4 + 4); g_model = calloc(g_L.n_chunks + 1, 4);
    free(g_vb); free(g_ib); g_vb = calloc(g_L.n_chunks + 1, sizeof *g_vb); g_ib = calloc(g_L.n_chunks + 1, sizeof *g_ib);
    g_tex = calloc(g_L.n_textures * g_L.n_lods + 1, sizeof *g_tex); g_texHave = calloc(g_L.n_textures * g_L.n_lods + 1, 1);
    for (i = 0; i < (int)g_L.n_chunks; i++) {
        float cxw = ((float)g_info[i].tile_x + 0.5f) * g_L.tile_size, cyw = ((float)g_info[i].tile_y + 0.5f) * g_L.tile_size;
        g_box[i * 4 + 0] = cxw + g_info[i].min[0]; g_box[i * 4 + 1] = cyw + g_info[i].min[1];
        g_box[i * 4 + 2] = cxw + g_info[i].max[0]; g_box[i * 4 + 3] = cyw + g_info[i].max[1];
        g_want[i] = -1; g_res[i] = -1; g_pend[i] = -1;
    }
    g_listPtr = 0; g_lsPtr = 0;
    return 1;
}

static DWORD WINAPI StreamThread(LPVOID unused) {
    (void)unused;
    for (;;) {
        int c, best = -1, bestLod = -1; double bestD = 1e30;
        Sleep(g_state == 2 ? 30 : 200);
        if (g_state == 1) { g_state = OpenInHelper() ? 2 : -1; continue; }
        if (g_state != 2 || !g_haveGlider) continue;
        for (c = 0; c < (int)g_L.n_chunks; c++) g_want[c] = WantLod(c, g_res[c]);
        if (g_qn >= 3) continue;
        for (c = 0; c < (int)g_L.n_chunks; c++) {                                 // nearest chunk whose LOD must change
            const float* b = &g_box[c * 4]; double dx, dy, d;
            if (g_want[c] < 0 || g_want[c] == g_res[c] || g_pend[c] == g_want[c]) continue;
            dx = (b[0] + b[2]) * 0.5 - g_gx; dy = (b[1] + b[3]) * 0.5 - g_gy; d = dx * dx + dy * dy;
            if (g_res[c] < 0) d *= 0.25;                                          // missing chunks before LOD changes
            if (d < bestD) { bestD = d; best = c; bestLod = g_want[c]; }
        }
        if (best < 0) continue;
        {   Job* j = calloc(1, sizeof *j); int st; const C3hChunk* h; const C3hPart* p; uint32_t i;
            j->chunk = best; j->lod = bestLod; g_pend[best] = bestLod;
            st = C3h_Request(&g_cl, C3H_REQ_CHUNK, best, bestLod, NULL, 10000);
            if (st != 0) { ALog("chunk %d lod %d: status %d %s", best, bestLod, st, g_cl.hd->resp_msg); g_pend[best] = -1; free(j); if (st < -100) g_state = -1; continue; }
            j->size = g_cl.hd->resp_size; j->blob = BigAlloc(j->size); memcpy(j->blob, g_cl.arena, j->size); g_bytesIn += j->size;
            h = (const C3hChunk*)j->blob; p = (const C3hPart*)(h + 1);
            for (i = 0; i < h->n_parts && j->ntex < 32; i++) if (p[i].texture >= 0) {
                uint32_t key = TexKey(p[i].texture, bestLod); int k, dup = 0;
                if (g_texHave[key]) continue;
                for (k = 0; k < j->ntex; k++) dup |= j->tex[k].key == key;
                if (dup) continue;
                if (C3h_Request(&g_cl, C3H_REQ_TEXTURE, p[i].texture, bestLod, NULL, 10000) == 0) {
                    j->tex[j->ntex].key = key; j->tex[j->ntex].size = g_cl.hd->resp_size;
                    j->tex[j->ntex].dds = BigAlloc(g_cl.hd->resp_size); memcpy(j->tex[j->ntex].dds, g_cl.arena, g_cl.hd->resp_size);
                    g_bytesIn += g_cl.hd->resp_size; j->ntex++;
                }
            }
            EnterCriticalSection(&g_qlock);
            if (g_qtail) g_qtail->next = j; else g_qhead = j;
            g_qtail = j; g_qn++;
            LeaveCriticalSection(&g_qlock);
        }
    }
    return 0;
}

// ---- public ---------------------------------------------------------------------------------------------------
void AgStream_Stats(void (*out)(const char*, ...)) {
    int c, n[C3H_MAX_LOD + 1] = {0}, t = 0; uint32_t k;
    if (g_state != 2) { out("ag: state %d (%s)", g_state, g_lsDir); return; }
    for (c = 0; c < (int)g_L.n_chunks; c++) n[g_res[c] + 1]++;
    for (k = 0; k < g_L.n_textures * g_L.n_lods; k++) t += g_tex[k].srv != NULL;
    out("ag: %u chunks: none %d lod0 %d lod1 %d lod2 %d; textures %d; queue %d; created %u freed %u; MB in %u; tex created %u reused %u pool %d (%u MB); buf created %u reused %u pool %d (%u MB); glider %.0f %.0f",
        g_L.n_chunks, n[0], n[1], n[2], n[3], t, g_qn, g_created, g_freed, g_bytesIn >> 20, g_texCreated, g_texReused, g_npool, g_poolBytes >> 20, g_bufCreated, g_bufReused, g_nbpool, g_bpoolBytes >> 20, g_gx, g_gy);
}

void InstallAgStream(void) {
    char exe[MAX_PATH], p[MAX_PATH], *s; int r1, r2, r3;
    GetModuleFileNameA(NULL, exe, MAX_PATH);
    s = strrchr(exe, '\\'); if (s) *s = 0;
    strncpy(g_gameDir, exe, sizeof g_gameDir - 1);
    _snprintf(p, sizeof p, "%s\\postfx\\agstream.log", g_gameDir);
    g_log = fopen(p, "w");
    LoadConfig();
    _snprintf(p, sizeof p, "%s\\c3helper.exe", g_gameDir);
    if (GetFileAttributesA(p) == INVALID_FILE_ATTRIBUTES) { ALog("no %s: autogen streaming off", p); g_enable = 0; }
    ALog("agstream: enable %d, LOD ranges %.0f %.0f %.0f m", g_enable, g_r[0], g_r[1], g_r[2]);
    if (!g_enable) return;
    InitializeCriticalSection(&g_qlock);
    r1 = DobbyInstrument(IdaAddrToRuntime(FN_CACHE_EXECUTE), CacheExecutePre);
    r2 = 0;                                                                     // per-frame work: AgStream_FrameTick from the HUD hook
    r3 = DobbyInstrument(IdaAddrToRuntime(FN_AG_AFTERTESS), AfterTessPre);
    ALog("hooks: cache execute %d, update %d, aftertess %d", r1, r2, r3);
    if (r1 || r2 || r3) { g_enable = 0; return; }
    CreateThread(NULL, 0, StreamThread, NULL, 0, NULL);
}
