// postfx_core.c - see postfx_core.h
#define COBJMACROS
#include "postfx_core.h"
#define PFX_SRVS 10   // t0..t9

#include <stdint.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>

// ---------------------------------------------------------------------------------------------
// logging
// ---------------------------------------------------------------------------------------------
static FILE* g_log = NULL;
void PfxSetLog(FILE* f) { g_log = f; }

static void PfxLog(const char* fmt, ...) {
    va_list ap;
    if (!g_log) return;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

#define REL(p) do { if (p) { IUnknown_Release((IUnknown*)(p)); (p) = NULL; } } while (0)

// ---------------------------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------------------------
#define LEVELS 3

typedef struct {
    float screen[4];   // full-res width, height, 1/width, 1/height
    float proj[4];     // m00, m11, m22, m32
    float misc[4];     // time, frame, 0, 0
    float dst[4];      // destination width, height, 1/w, 1/h
    float pass[4];     // pass specific (blur direction in texels etc.)
    float view[16];    // engine view matrix (column major, as in the engine's own constant buffer)
    float camPos[4];   // world camera position, w = atmosphere data valid
    float fog[4], sunDir[4], sunColor[4], amb[4];
} CBData;

typedef struct { ID3D11Texture2D* tex; ID3D11ShaderResourceView* srv; ID3D11RenderTargetView* rtv; UINT w, h; } Target;

struct PfxCore {
    ID3D11Device* dev;
    char hlslPath[MAX_PATH];
    FILETIME hlslTime;
    HMODULE hCompiler;
    HRESULT (WINAPI *pCompile)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, void*, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);

    ID3D11VertexShader* vs;
    ID3D11PixelShader *psBright, *psDown, *psBlur, *psAO, *psAOBlur, *psFinal, *psCapDepth;
    int shaderMS;                       // 1 when the shaders were compiled for a multisampled depth buffer

    ID3D11SamplerState *sLin, *sPoint;
    ID3D11RasterizerState* rs;
    ID3D11DepthStencilState* dss;
    ID3D11BlendState* bs;
    ID3D11Buffer* cb;
    ID3D11Buffer* cbTune;               // b1: live tunables (postfx.hlsl '//@tune <slot> <name> <default>' lines, overridden by tune.txt)
    float tune[64];
    char tuneNames[64][40];
    char tunePath[MAX_PATH];
    FILETIME tuneTime;
    int tuneDirty;
    ID3D11Texture3D* lutTex;            // t7: colour grading LUT (postfx/lut.cube); identity when absent
    ID3D11ShaderResourceView* lutSrv;
    ID3D11ShaderResourceView* ovB; ID3D11ShaderResourceView* ovW;   // translucent overlay layer (not owned), this frame only
    char lutPath[MAX_PATH];
    FILETIME lutTime;
    int lutSize;

    UINT w, h;
    Target scene, ao, aoBlur, lv[LEVELS], tmp[LEVELS], capDepth;

    ID3D11Texture2D* depthTex;          // cached depth view
    ID3D11ShaderResourceView* depthSrv;
    ID3D11Texture2D* worldTex;          // optional: depth saved before the engine cleared it (world only)
    ID3D11ShaderResourceView* worldSrv;
    UINT depthW, depthH, depthSamples;
    DXGI_FORMAT depthFmt;

    unsigned frame;
    int failed;                         // fatal runtime problem: stop trying
    int reload;
    char capturePath[MAX_PATH];
    int capture;
};

// ---------------------------------------------------------------------------------------------
// shader compilation
// ---------------------------------------------------------------------------------------------
static char* ReadFileAll(const char* path, SIZE_T* len) {
    HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    DWORD sz, rd = 0;
    char* buf;
    if (h == INVALID_HANDLE_VALUE) return NULL;
    sz = GetFileSize(h, NULL);
    buf = (char*)malloc(sz + 1);
    if (!buf) { CloseHandle(h); return NULL; }
    if (!ReadFile(h, buf, sz, &rd, NULL)) rd = 0;
    CloseHandle(h);
    buf[rd] = 0;
    *len = rd;
    return buf;
}

static ID3DBlob* CompileEntry(PfxCore* c, const char* src, SIZE_T len, const D3D_SHADER_MACRO* defs,
                              const char* entry, const char* target) {
    ID3DBlob *code = NULL, *err = NULL;
    HRESULT hr = c->pCompile(src, len, "postfx.hlsl", defs, NULL, entry, target, 1u << 15, 0, &code, &err);
    if (FAILED(hr) || !code) {
        PfxLog("postfx: compile of %s (%s) failed hr=0x%08lx", entry, target, (unsigned long)hr);
        if (err) PfxLog("%s", (const char*)ID3D10Blob_GetBufferPointer(err));
        REL(err); REL(code);
        return NULL;
    }
    REL(err);
    return code;
}

static void ReleaseShaders(PfxCore* c) {
    REL(c->vs); REL(c->psBright); REL(c->psDown); REL(c->psBlur); REL(c->psAO);
    REL(c->psAOBlur); REL(c->psFinal); REL(c->psCapDepth);
}

// ---- live tunables -------------------------------------------------------------------------
static void ParseTuneDecls(PfxCore* c, const char* src) {
    const char* p = src;
    int i;
    float defv[64];
    memset(defv, 0, sizeof defv);
    for (i = 0; i < 64; i++) c->tuneNames[i][0] = 0;
    while ((p = strstr(p, "//@tune ")) != NULL) {
        int slot; char name[40]; float v;
        if (sscanf(p + 8, "%d %39s %f", &slot, name, &v) == 3 && slot >= 0 && slot < 64) {
            lstrcpynA(c->tuneNames[slot], name, 40);
            defv[slot] = v;
        }
        p += 8;
    }
    defv[63] = c->tune[63];
    memcpy(c->tune, defv, sizeof defv);
    c->tuneDirty = 1;
}

static void LoadTuneFile(PfxCore* c) {
    SIZE_T len = 0;
    char* t = ReadFileAll(c->tunePath, &len);
    WIN32_FILE_ATTRIBUTE_DATA d;
    char* line;
    if (GetFileAttributesExA(c->tunePath, GetFileExInfoStandard, &d)) c->tuneTime = d.ftLastWriteTime;
    if (!t) return;
    for (line = strtok(t, "\r\n"); line; line = strtok(NULL, "\r\n")) {
        char name[40]; float v; int i;
        if (line[0] == '#' || line[0] == ';') continue;
        if (sscanf(line, "%39s %f", name, &v) != 2) continue;
        for (i = 0; i < 64; i++)
            if (c->tuneNames[i][0] && !_stricmp(c->tuneNames[i], name)) { c->tune[i] = v; c->tuneDirty = 1; break; }
    }
    free(t);
}

static int TuneFileChanged(PfxCore* c) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExA(c->tunePath, GetFileExInfoStandard, &d)) return 0;
    return CompareFileTime(&d.ftLastWriteTime, &c->tuneTime) != 0;
}

// ---- 3D colour grading LUT (.cube) ------------------------------------------------------------
static void SetLut(PfxCore* c, int n, const unsigned short* rgba) {
    D3D11_TEXTURE3D_DESC d;
    D3D11_SUBRESOURCE_DATA sd;
    ID3D11Texture3D* t = NULL;
    ID3D11ShaderResourceView* v = NULL;
    memset(&d, 0, sizeof d);
    d.Width = d.Height = d.Depth = n; d.MipLevels = 1; d.Format = DXGI_FORMAT_R16G16B16A16_UNORM;
    d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    sd.pSysMem = rgba; sd.SysMemPitch = n * 8; sd.SysMemSlicePitch = n * n * 8;
    if (FAILED(ID3D11Device_CreateTexture3D(c->dev, &d, &sd, &t))) { PfxLog("postfx: LUT texture creation failed"); return; }
    if (FAILED(ID3D11Device_CreateShaderResourceView(c->dev, (ID3D11Resource*)t, NULL, &v))) { REL(t); return; }
    REL(c->lutSrv); REL(c->lutTex);
    c->lutTex = t; c->lutSrv = v; c->lutSize = n;
    c->tune[63] = (float)n;            // slot 63 = LUT edge length, read by the shader
    c->tuneDirty = 1;
}

static void IdentityLut(PfxCore* c) {
    unsigned short v[2 * 2 * 2 * 4];
    int i;
    for (i = 0; i < 8; i++) {
        v[i * 4 + 0] = (i & 1) ? 65535 : 0; v[i * 4 + 1] = (i & 2) ? 65535 : 0;
        v[i * 4 + 2] = (i & 4) ? 65535 : 0; v[i * 4 + 3] = 65535;
    }
    SetLut(c, 2, v);
}

static void LoadLutFile(PfxCore* c) {
    SIZE_T len = 0;
    char* t;
    WIN32_FILE_ATTRIBUTE_DATA d;
    int n = 0, count = 0, total = 0;
    unsigned short* out = NULL;
    char* line;
    if (GetFileAttributesExA(c->lutPath, GetFileExInfoStandard, &d)) c->lutTime = d.ftLastWriteTime;
    t = ReadFileAll(c->lutPath, &len);
    if (!t) { if (c->lutSize != 2) IdentityLut(c); return; }
    for (line = strtok(t, "\r\n"); line; line = strtok(NULL, "\r\n")) {
        float r, g, b;
        while (*line == ' ' || *line == '\t') line++;
        if (!*line || *line == '#') continue;
        if (!strncmp(line, "LUT_3D_SIZE", 11)) {
            n = atoi(line + 11);
            if (n < 2 || n > 128) { PfxLog("postfx: bad LUT_3D_SIZE %d", n); break; }
            total = n * n * n;
            out = (unsigned short*)malloc((size_t)total * 8);
            if (!out) break;
            continue;
        }
        if (!out) continue;                                    // header lines before the size (TITLE, DOMAIN_*)
        if ((*line >= '0' && *line <= '9') || *line == '-' || *line == '.') {
            if (sscanf(line, "%f %f %f", &r, &g, &b) == 3 && count < total) {
                r = r < 0 ? 0 : (r > 1 ? 1 : r); g = g < 0 ? 0 : (g > 1 ? 1 : g); b = b < 0 ? 0 : (b > 1 ? 1 : b);
                out[count * 4 + 0] = (unsigned short)(r * 65535.0f + 0.5f);
                out[count * 4 + 1] = (unsigned short)(g * 65535.0f + 0.5f);
                out[count * 4 + 2] = (unsigned short)(b * 65535.0f + 0.5f);
                out[count * 4 + 3] = 65535;
                count++;
            }
        }
    }
    if (out && count == total && total > 0) { SetLut(c, n, out); PfxLog("postfx: LUT loaded (%d^3)", n); }
    else { PfxLog("postfx: lut.cube unusable (%d of %d entries)", count, total); if (c->lutSize != 2) IdentityLut(c); }
    free(out); free(t);
}

static int LutFileChanged(PfxCore* c) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExA(c->lutPath, GetFileExInfoStandard, &d)) return c->lutSize != 2;   // removed -> back to identity
    return CompareFileTime(&d.ftLastWriteTime, &c->lutTime) != 0;
}

// (Re)compiles all shaders for the given depth variant. On failure the old shaders stay in place.
static int BuildShaders(PfxCore* c, int ms) {
    SIZE_T len = 0;
    char* src = ReadFileAll(c->hlslPath, &len);
    D3D_SHADER_MACRO defs[2];
    struct { const char* entry; ID3D11PixelShader** slot; ID3D11PixelShader* made; } ps[] = {
        { "PS_Bright", &c->psBright, NULL }, { "PS_Down", &c->psDown, NULL }, { "PS_Blur", &c->psBlur, NULL },
        { "PS_AO", &c->psAO, NULL }, { "PS_AOBlur", &c->psAOBlur, NULL }, { "PS_Final", &c->psFinal, NULL },
        { "PS_CapDepth", &c->psCapDepth, NULL },
    };
    ID3D11VertexShader* vs = NULL;
    ID3DBlob* b;
    int i, ok = 1;

    if (!src) { PfxLog("postfx: cannot read %s", c->hlslPath); return 0; }
    ParseTuneDecls(c, src);
    LoadTuneFile(c);
    defs[0].Name = "DEPTH_MS"; defs[0].Definition = ms ? "1" : "0";
    defs[1].Name = NULL; defs[1].Definition = NULL;

    b = CompileEntry(c, src, len, defs, "VS_Full", "vs_5_0");
    if (b) {
        if (FAILED(ID3D11Device_CreateVertexShader(c->dev, ID3D10Blob_GetBufferPointer(b), ID3D10Blob_GetBufferSize(b), NULL, &vs))) ok = 0;
        REL(b);
    } else ok = 0;

    for (i = 0; ok && i < (int)(sizeof(ps) / sizeof(ps[0])); i++) {
        b = CompileEntry(c, src, len, defs, ps[i].entry, "ps_5_0");
        if (!b) { ok = 0; break; }
        if (FAILED(ID3D11Device_CreatePixelShader(c->dev, ID3D10Blob_GetBufferPointer(b), ID3D10Blob_GetBufferSize(b), NULL, &ps[i].made))) ok = 0;
        REL(b);
    }
    free(src);

    if (!ok) {
        REL(vs);
        for (i = 0; i < (int)(sizeof(ps) / sizeof(ps[0])); i++) REL(ps[i].made);
        return 0;
    }
    ReleaseShaders(c);
    c->vs = vs;
    for (i = 0; i < (int)(sizeof(ps) / sizeof(ps[0])); i++) *ps[i].slot = ps[i].made;
    c->shaderMS = ms;
    PfxLog("postfx: shaders compiled (depth multisampled: %d)", ms);
    return 1;
}

static void StampFile(PfxCore* c) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (GetFileAttributesExA(c->hlslPath, GetFileExInfoStandard, &d)) c->hlslTime = d.ftLastWriteTime;
}

static int FileChanged(PfxCore* c) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExA(c->hlslPath, GetFileExInfoStandard, &d)) return 0;
    return CompareFileTime(&d.ftLastWriteTime, &c->hlslTime) != 0;
}

// ---------------------------------------------------------------------------------------------
// resources
// ---------------------------------------------------------------------------------------------
static void ReleaseTarget(Target* t) { REL(t->srv); REL(t->rtv); REL(t->tex); t->w = t->h = 0; }

static int MakeTarget(PfxCore* c, Target* t, UINT w, UINT h, DXGI_FORMAT fmt) {
    D3D11_TEXTURE2D_DESC d;
    ReleaseTarget(t);
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    memset(&d, 0, sizeof d);
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    if (FAILED(ID3D11Device_CreateTexture2D(c->dev, &d, NULL, &t->tex))) return 0;
    if (FAILED(ID3D11Device_CreateShaderResourceView(c->dev, (ID3D11Resource*)t->tex, NULL, &t->srv))) return 0;
    if (FAILED(ID3D11Device_CreateRenderTargetView(c->dev, (ID3D11Resource*)t->tex, NULL, &t->rtv))) return 0;
    t->w = w; t->h = h;
    return 1;
}

static int EnsureSize(PfxCore* c, UINT w, UINT h) {
    int i, ok = 1;
    if (c->w == w && c->h == h && c->scene.tex) return 1;
    ok &= MakeTarget(c, &c->scene, w, h, DXGI_FORMAT_R8G8B8A8_UNORM);
    ok &= MakeTarget(c, &c->ao, w, h, DXGI_FORMAT_R8_UNORM);
    ok &= MakeTarget(c, &c->aoBlur, w, h, DXGI_FORMAT_R8_UNORM);
    for (i = 0; i < LEVELS; i++) {
        ok &= MakeTarget(c, &c->lv[i], w >> (2 + i), h >> (2 + i), DXGI_FORMAT_R16G16B16A16_FLOAT);
        ok &= MakeTarget(c, &c->tmp[i], w >> (2 + i), h >> (2 + i), DXGI_FORMAT_R16G16B16A16_FLOAT);
    }
    ReleaseTarget(&c->capDepth);
    if (!ok) { PfxLog("postfx: creating render targets for %ux%u failed", w, h); return 0; }
    c->w = w; c->h = h;
    PfxLog("postfx: render targets created for %ux%u", w, h);
    return 1;
}

static DXGI_FORMAT DepthSrvFormat(DXGI_FORMAT f) {
    switch (f) {
    case DXGI_FORMAT_R32G8X24_TYPELESS: return DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS;
    case DXGI_FORMAT_R24G8_TYPELESS:    return DXGI_FORMAT_R24_UNORM_X8_TYPELESS;
    case DXGI_FORMAT_R32_TYPELESS:      return DXGI_FORMAT_R32_FLOAT;
    case DXGI_FORMAT_R16_TYPELESS:      return DXGI_FORMAT_R16_UNORM;
    default:                            return DXGI_FORMAT_UNKNOWN;
    }
}

static int EnsureDepthView(PfxCore* c, ID3D11Texture2D* depth) {
    D3D11_TEXTURE2D_DESC d;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv;
    DXGI_FORMAT vf;
    ID3D11Texture2D_GetDesc(depth, &d);
    if (c->depthSrv && c->depthTex == depth && c->depthW == d.Width && c->depthH == d.Height &&
        c->depthSamples == d.SampleDesc.Count) return 1;
    REL(c->depthSrv);
    REL(c->depthTex);
    if (!(d.BindFlags & D3D11_BIND_SHADER_RESOURCE)) { PfxLog("postfx: depth texture is not shader readable"); return 0; }
    vf = DepthSrvFormat(d.Format);
    if (vf == DXGI_FORMAT_UNKNOWN) { PfxLog("postfx: unsupported depth format %d", (int)d.Format); return 0; }
    memset(&sv, 0, sizeof sv);
    sv.Format = vf;
    if (d.SampleDesc.Count > 1) {
        sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2DMS;
    } else {
        sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        sv.Texture2D.MipLevels = 1;
    }
    if (FAILED(ID3D11Device_CreateShaderResourceView(c->dev, (ID3D11Resource*)depth, &sv, &c->depthSrv))) {
        PfxLog("postfx: creating the depth shader view failed");
        return 0;
    }
    ID3D11Texture2D_AddRef(depth);
    c->depthTex = depth;
    c->depthW = d.Width; c->depthH = d.Height; c->depthSamples = d.SampleDesc.Count; c->depthFmt = d.Format;
    PfxLog("postfx: depth %ux%u format %d samples %u", d.Width, d.Height, (int)d.Format, d.SampleDesc.Count);
    return 1;
}

// Shader view for the optional "world" depth copy (same format/size as the main depth buffer).
static ID3D11ShaderResourceView* EnsureWorldView(PfxCore* c, ID3D11Texture2D* world) {
    D3D11_TEXTURE2D_DESC d;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv;
    if (!world) { REL(c->worldSrv); REL(c->worldTex); return c->depthSrv; }
    if (c->worldSrv && c->worldTex == world) return c->worldSrv;
    REL(c->worldSrv); REL(c->worldTex);
    ID3D11Texture2D_GetDesc(world, &d);
    if (!(d.BindFlags & D3D11_BIND_SHADER_RESOURCE) || DepthSrvFormat(d.Format) == DXGI_FORMAT_UNKNOWN) return c->depthSrv;
    memset(&sv, 0, sizeof sv);
    sv.Format = DepthSrvFormat(d.Format);
    if (d.SampleDesc.Count > 1) sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2DMS;
    else { sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sv.Texture2D.MipLevels = 1; }
    if (FAILED(ID3D11Device_CreateShaderResourceView(c->dev, (ID3D11Resource*)world, &sv, &c->worldSrv))) return c->depthSrv;
    ID3D11Texture2D_AddRef(world);
    c->worldTex = world;
    PfxLog("postfx: world depth copy in use");
    return c->worldSrv;
}

// ---------------------------------------------------------------------------------------------
// state save / restore
// ---------------------------------------------------------------------------------------------
typedef struct {
    ID3D11RenderTargetView* rtv[8];
    ID3D11DepthStencilView* dsv;
    UINT nvp; D3D11_VIEWPORT vp[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    UINT nsc; D3D11_RECT sc[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    ID3D11RasterizerState* rs;
    ID3D11DepthStencilState* dss; UINT stencilRef;
    ID3D11BlendState* bs; FLOAT bf[4]; UINT sampleMask;
    ID3D11InputLayout* il; D3D11_PRIMITIVE_TOPOLOGY topo;
    ID3D11Buffer* vb[4]; UINT vbStride[4], vbOffset[4];
    ID3D11Buffer* ib; DXGI_FORMAT ibFmt; UINT ibOff;
    ID3D11VertexShader* vs; ID3D11PixelShader* ps; ID3D11GeometryShader* gs;
    ID3D11Buffer* vscb[4]; ID3D11Buffer* pscb[4];
    ID3D11ShaderResourceView* pssrv[PFX_SRVS]; ID3D11ShaderResourceView* vssrv[4];
    ID3D11SamplerState* pssmp[4]; ID3D11SamplerState* vssmp[4];
} SavedState;

static void SaveState(ID3D11DeviceContext* x, SavedState* s) {
    memset(s, 0, sizeof *s);
    ID3D11DeviceContext_OMGetRenderTargets(x, 8, s->rtv, &s->dsv);
    s->nvp = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    ID3D11DeviceContext_RSGetViewports(x, &s->nvp, s->vp);
    s->nsc = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    ID3D11DeviceContext_RSGetScissorRects(x, &s->nsc, s->sc);
    ID3D11DeviceContext_RSGetState(x, &s->rs);
    ID3D11DeviceContext_OMGetDepthStencilState(x, &s->dss, &s->stencilRef);
    ID3D11DeviceContext_OMGetBlendState(x, &s->bs, s->bf, &s->sampleMask);
    ID3D11DeviceContext_IAGetInputLayout(x, &s->il);
    ID3D11DeviceContext_IAGetPrimitiveTopology(x, &s->topo);
    ID3D11DeviceContext_IAGetVertexBuffers(x, 0, 4, s->vb, s->vbStride, s->vbOffset);
    ID3D11DeviceContext_IAGetIndexBuffer(x, &s->ib, &s->ibFmt, &s->ibOff);
    ID3D11DeviceContext_VSGetShader(x, &s->vs, NULL, NULL);
    ID3D11DeviceContext_PSGetShader(x, &s->ps, NULL, NULL);
    ID3D11DeviceContext_GSGetShader(x, &s->gs, NULL, NULL);
    ID3D11DeviceContext_VSGetConstantBuffers(x, 0, 4, s->vscb);
    ID3D11DeviceContext_PSGetConstantBuffers(x, 0, 4, s->pscb);
    ID3D11DeviceContext_PSGetShaderResources(x, 0, PFX_SRVS, s->pssrv);
    ID3D11DeviceContext_VSGetShaderResources(x, 0, 4, s->vssrv);
    ID3D11DeviceContext_PSGetSamplers(x, 0, 4, s->pssmp);
    ID3D11DeviceContext_VSGetSamplers(x, 0, 4, s->vssmp);
}

static void RestoreState(ID3D11DeviceContext* x, SavedState* s) {
    int i;
    ID3D11DeviceContext_OMSetRenderTargets(x, 8, s->rtv, s->dsv);
    ID3D11DeviceContext_RSSetViewports(x, s->nvp, s->vp);
    ID3D11DeviceContext_RSSetScissorRects(x, s->nsc, s->sc);
    ID3D11DeviceContext_RSSetState(x, s->rs);
    ID3D11DeviceContext_OMSetDepthStencilState(x, s->dss, s->stencilRef);
    ID3D11DeviceContext_OMSetBlendState(x, s->bs, s->bf, s->sampleMask);
    ID3D11DeviceContext_IASetInputLayout(x, s->il);
    ID3D11DeviceContext_IASetPrimitiveTopology(x, s->topo);
    ID3D11DeviceContext_IASetVertexBuffers(x, 0, 4, s->vb, s->vbStride, s->vbOffset);
    ID3D11DeviceContext_IASetIndexBuffer(x, s->ib, s->ibFmt, s->ibOff);
    ID3D11DeviceContext_VSSetShader(x, s->vs, NULL, 0);
    ID3D11DeviceContext_PSSetShader(x, s->ps, NULL, 0);
    ID3D11DeviceContext_GSSetShader(x, s->gs, NULL, 0);
    ID3D11DeviceContext_VSSetConstantBuffers(x, 0, 4, s->vscb);
    ID3D11DeviceContext_PSSetConstantBuffers(x, 0, 4, s->pscb);
    ID3D11DeviceContext_PSSetShaderResources(x, 0, PFX_SRVS, s->pssrv);
    ID3D11DeviceContext_VSSetShaderResources(x, 0, 4, s->vssrv);
    ID3D11DeviceContext_PSSetSamplers(x, 0, 4, s->pssmp);
    ID3D11DeviceContext_VSSetSamplers(x, 0, 4, s->vssmp);

    for (i = 0; i < 8; i++) REL(s->rtv[i]);
    REL(s->dsv); REL(s->rs); REL(s->dss); REL(s->bs); REL(s->il);
    for (i = 0; i < 4; i++) { REL(s->vb[i]); REL(s->vscb[i]); REL(s->pscb[i]); REL(s->vssrv[i]); REL(s->pssmp[i]); REL(s->vssmp[i]); }
    for (i = 0; i < PFX_SRVS; i++) REL(s->pssrv[i]);
    REL(s->ib); REL(s->vs); REL(s->ps); REL(s->gs);
}

// ---------------------------------------------------------------------------------------------
// drawing
// ---------------------------------------------------------------------------------------------
static void DrawPass(PfxCore* c, ID3D11DeviceContext* x, const PfxCamera* cam, ID3D11RenderTargetView* rtv,
                     UINT dw, UINT dh, ID3D11PixelShader* ps, ID3D11ShaderResourceView* const srv[PFX_SRVS],
                     float p0, float p1, float p2, float p3) {
    D3D11_MAPPED_SUBRESOURCE m;
    D3D11_VIEWPORT vp;
    CBData cd;
    ID3D11ShaderResourceView* none[PFX_SRVS] = { 0 };

    cd.screen[0] = (float)c->w; cd.screen[1] = (float)c->h; cd.screen[2] = 1.0f / c->w; cd.screen[3] = 1.0f / c->h;
    cd.proj[0] = cam->m00; cd.proj[1] = cam->m11; cd.proj[2] = cam->m22; cd.proj[3] = cam->m32;
    cd.misc[0] = cam->time; cd.misc[1] = (float)c->frame; cd.misc[2] = (c->ovB && c->ovW) ? 1.0f : 0.0f; cd.misc[3] = 0;
    cd.dst[0] = (float)dw; cd.dst[1] = (float)dh; cd.dst[2] = 1.0f / dw; cd.dst[3] = 1.0f / dh;
    cd.pass[0] = p0; cd.pass[1] = p1; cd.pass[2] = p2; cd.pass[3] = p3;
    memcpy(cd.view, cam->view, sizeof cd.view); memcpy(cd.camPos, cam->camPos, sizeof cd.camPos);
    memcpy(cd.fog, cam->fog, 16); memcpy(cd.sunDir, cam->sunDir, 16); memcpy(cd.sunColor, cam->sunColor, 16); memcpy(cd.amb, cam->amb, 16);
    if (SUCCEEDED(ID3D11DeviceContext_Map(x, (ID3D11Resource*)c->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
        memcpy(m.pData, &cd, sizeof cd);
        ID3D11DeviceContext_Unmap(x, (ID3D11Resource*)c->cb, 0);
    }

    ID3D11DeviceContext_OMSetRenderTargets(x, 1, &rtv, NULL);
    vp.TopLeftX = 0; vp.TopLeftY = 0; vp.Width = (float)dw; vp.Height = (float)dh; vp.MinDepth = 0; vp.MaxDepth = 1;
    ID3D11DeviceContext_RSSetViewports(x, 1, &vp);
    ID3D11DeviceContext_PSSetShaderResources(x, 0, PFX_SRVS, srv);
    ID3D11DeviceContext_PSSetShader(x, ps, NULL, 0);
    ID3D11DeviceContext_Draw(x, 3, 0);
    ID3D11DeviceContext_PSSetShaderResources(x, 0, PFX_SRVS, none);
}

static void WriteCapture(PfxCore* c, ID3D11DeviceContext* x, const PfxCamera* cam) {
    D3D11_TEXTURE2D_DESC d;
    ID3D11Texture2D *stC = NULL, *stD = NULL;
    D3D11_MAPPED_SUBRESOURCE mc, md;
    FILE* f;
    UINT y;
    struct { char magic[8]; UINT32 w, h; float m00, m11, m22, m32; UINT32 pad; } hdr;

    memset(&d, 0, sizeof d);
    d.Width = c->w; d.Height = c->h; d.MipLevels = 1; d.ArraySize = 1;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_STAGING; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    if (FAILED(ID3D11Device_CreateTexture2D(c->dev, &d, NULL, &stC))) goto done;
    d.Format = DXGI_FORMAT_R32_FLOAT;
    if (FAILED(ID3D11Device_CreateTexture2D(c->dev, &d, NULL, &stD))) goto done;
    ID3D11DeviceContext_CopyResource(x, (ID3D11Resource*)stC, (ID3D11Resource*)c->scene.tex);
    ID3D11DeviceContext_CopyResource(x, (ID3D11Resource*)stD, (ID3D11Resource*)c->capDepth.tex);
    if (FAILED(ID3D11DeviceContext_Map(x, (ID3D11Resource*)stC, 0, D3D11_MAP_READ, 0, &mc))) goto done;
    if (FAILED(ID3D11DeviceContext_Map(x, (ID3D11Resource*)stD, 0, D3D11_MAP_READ, 0, &md))) {
        ID3D11DeviceContext_Unmap(x, (ID3D11Resource*)stC, 0);
        goto done;
    }
    {   // statistics so a dark / empty frame can be diagnosed from the log alone
        double sr = 0, sg = 0, sb = 0, sd = 0; float dmin = 1e30f, dmax = -1e30f; UINT x; size_t n = (size_t)c->w * c->h;
        UINT zero = 0;
        for (y = 0; y < c->h; y++) {
            const unsigned char* pc = (const unsigned char*)mc.pData + (size_t)y * mc.RowPitch;
            const float* pd = (const float*)((const char*)md.pData + (size_t)y * md.RowPitch);
            for (x = 0; x < c->w; x++) {
                sr += pc[x * 4]; sg += pc[x * 4 + 1]; sb += pc[x * 4 + 2];
                sd += pd[x]; if (pd[x] < dmin) dmin = pd[x]; if (pd[x] > dmax) dmax = pd[x];
                if (pd[x] == 0.0f) zero++;
            }
        }
        PfxLog("postfx: scene mean rgb = %.1f %.1f %.1f (0..255)", sr / n, sg / n, sb / n);
        PfxLog("postfx: depth min %.8g max %.8g mean %.8g, %.1f%% exactly 0", dmin, dmax, sd / n, 100.0 * zero / n);
        PfxLog("postfx: proj m00 %g m11 %g m22 %g m32 %g", cam->m00, cam->m11, cam->m22, cam->m32);
    }
    f = fopen(c->capturePath, "wb");
    if (f) {
        memcpy(hdr.magic, "C3PFXCAP", 8);
        hdr.w = c->w; hdr.h = c->h; hdr.m00 = cam->m00; hdr.m11 = cam->m11; hdr.m22 = cam->m22; hdr.m32 = cam->m32; hdr.pad = 0;
        fwrite(&hdr, sizeof hdr, 1, f);
        for (y = 0; y < c->h; y++) fwrite((char*)mc.pData + (size_t)y * mc.RowPitch, 4, c->w, f);
        for (y = 0; y < c->h; y++) fwrite((char*)md.pData + (size_t)y * md.RowPitch, 4, c->w, f);
        fclose(f);
        PfxLog("postfx: capture written to %s (%ux%u)", c->capturePath, c->w, c->h);
    } else {
        PfxLog("postfx: cannot write capture %s", c->capturePath);
    }
    ID3D11DeviceContext_Unmap(x, (ID3D11Resource*)stC, 0);
    ID3D11DeviceContext_Unmap(x, (ID3D11Resource*)stD, 0);
done:
    REL(stC); REL(stD);
}

// ---------------------------------------------------------------------------------------------
// public api
// ---------------------------------------------------------------------------------------------
PfxCore* PfxCore_Create(ID3D11Device* dev, const char* hlslPath) {
    PfxCore* c = (PfxCore*)calloc(1, sizeof *c);
    D3D11_SAMPLER_DESC sd;
    D3D11_RASTERIZER_DESC rd;
    D3D11_DEPTH_STENCIL_DESC dd;
    D3D11_BLEND_DESC bd;
    D3D11_BUFFER_DESC cbd;
    if (!c) return NULL;
    c->dev = dev;
    ID3D11Device_AddRef(dev);
    lstrcpynA(c->hlslPath, hlslPath, MAX_PATH);

    c->hCompiler = LoadLibraryA("D3DCompiler_43.dll");
    if (!c->hCompiler) c->hCompiler = LoadLibraryA("d3dcompiler_47.dll");
    c->pCompile = c->hCompiler ? (void*)GetProcAddress(c->hCompiler, "D3DCompile") : NULL;
    if (!c->pCompile) { PfxLog("postfx: no D3DCompile available"); PfxCore_Destroy(c); return NULL; }

    memset(&sd, 0, sizeof sd);
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.MaxLOD = D3D11_FLOAT32_MAX; sd.MaxAnisotropy = 1; sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
    ID3D11Device_CreateSamplerState(dev, &sd, &c->sLin);
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    ID3D11Device_CreateSamplerState(dev, &sd, &c->sPoint);

    memset(&rd, 0, sizeof rd);
    rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE;
    ID3D11Device_CreateRasterizerState(dev, &rd, &c->rs);
    memset(&dd, 0, sizeof dd);
    dd.DepthEnable = FALSE; dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO; dd.DepthFunc = D3D11_COMPARISON_ALWAYS;
    ID3D11Device_CreateDepthStencilState(dev, &dd, &c->dss);
    memset(&bd, 0, sizeof bd);
    bd.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    ID3D11Device_CreateBlendState(dev, &bd, &c->bs);

    memset(&cbd, 0, sizeof cbd);
    cbd.ByteWidth = sizeof(CBData); cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    ID3D11Device_CreateBuffer(dev, &cbd, NULL, &c->cb);
    cbd.ByteWidth = 64 * sizeof(float);
    ID3D11Device_CreateBuffer(dev, &cbd, NULL, &c->cbTune);
    { char* sl; lstrcpynA(c->tunePath, hlslPath, MAX_PATH); sl = strrchr(c->tunePath, '\\'); if (!sl) sl = strrchr(c->tunePath, '/'); if (sl) lstrcpyA(sl + 1, "tune.txt"); else lstrcpyA(c->tunePath, "tune.txt"); }

    { char* sl; lstrcpynA(c->lutPath, hlslPath, MAX_PATH); sl = strrchr(c->lutPath, '\\'); if (!sl) sl = strrchr(c->lutPath, '/'); if (sl) lstrcpyA(sl + 1, "lut.cube"); else lstrcpyA(c->lutPath, "lut.cube"); }
    IdentityLut(c);
    LoadLutFile(c);
    if (!c->sLin || !c->sPoint || !c->rs || !c->dss || !c->bs || !c->cb || !c->cbTune) {
        PfxLog("postfx: creating fixed function state failed");
        PfxCore_Destroy(c);
        return NULL;
    }
    StampFile(c);
    return c;
}

void PfxCore_Destroy(PfxCore* c) {
    int i;
    if (!c) return;
    ReleaseShaders(c);
    REL(c->sLin); REL(c->sPoint); REL(c->rs); REL(c->dss); REL(c->bs); REL(c->cb); REL(c->cbTune); REL(c->lutSrv); REL(c->lutTex);
    ReleaseTarget(&c->scene); ReleaseTarget(&c->ao); ReleaseTarget(&c->aoBlur); ReleaseTarget(&c->capDepth);
    for (i = 0; i < LEVELS; i++) { ReleaseTarget(&c->lv[i]); ReleaseTarget(&c->tmp[i]); }
    REL(c->depthSrv); REL(c->depthTex); REL(c->worldSrv); REL(c->worldTex);
    if (c->hCompiler) FreeLibrary(c->hCompiler);
    REL(c->dev);
    free(c);
}

void PfxCore_RequestCapture(PfxCore* c, const char* path) {
    if (!c) return;
    lstrcpynA(c->capturePath, path, MAX_PATH);
    c->capture = 1;
}

float PfxCore_GetTune(PfxCore* c, int slot, float fallback) { return (c && slot >= 0 && slot < 64 && c->tuneNames[slot][0]) ? c->tune[slot] : fallback; }

void PfxCore_RequestReload(PfxCore* c) { if (c) c->reload = 1; }
void PfxCore_SetOverlay(PfxCore* c, ID3D11ShaderResourceView* black, ID3D11ShaderResourceView* white) { if (c) { c->ovB = black; c->ovW = white; } }

int PfxCore_Run(PfxCore* c, ID3D11DeviceContext* x, ID3D11Texture2D* color, ID3D11RenderTargetView* colorRTV,
                ID3D11Texture2D* depth, ID3D11Texture2D* world, const PfxCamera* cam) {
    D3D11_TEXTURE2D_DESC cd;
    ID3D11ShaderResourceView* worldSrv;
    SavedState st;
    ID3D11ShaderResourceView* s[PFX_SRVS];
    ID3D11Buffer* cbs[2];
    ID3D11SamplerState* smp[2];
    int i, depthMS;

    if (!c || c->failed || !color || !colorRTV || !depth || !cam) return 0;
    ID3D11Texture2D_GetDesc(color, &cd);
    if (cd.Format != DXGI_FORMAT_R8G8B8A8_UNORM) {
        PfxLog("postfx: unexpected colour format %d - disabling", (int)cd.Format);
        c->failed = 1;
        return 0;
    }
    if (!EnsureDepthView(c, depth)) { c->failed = 1; return 0; }
    depthMS = c->depthSamples > 1;
    worldSrv = EnsureWorldView(c, world);

    c->frame++;
    if (c->reload || ((c->frame & 31) == 0 && FileChanged(c))) {
        c->reload = 0;
        StampFile(c);
        PfxLog("postfx: reloading shaders");
        BuildShaders(c, depthMS);
    }
    if ((c->frame & 7) == 0 && TuneFileChanged(c)) {   // tune.txt edited: back to the declared defaults, then apply the file
        SIZE_T l2 = 0;
        char* hs = ReadFileAll(c->hlslPath, &l2);
        if (hs) { ParseTuneDecls(c, hs); free(hs); }
        LoadTuneFile(c);
    }
    if (!c->vs || c->shaderMS != depthMS) {
        if (!BuildShaders(c, depthMS) && !c->vs) return 0;   // nothing usable yet; retry after the file changes
    }
    if ((c->frame & 15) == 0 && LutFileChanged(c)) LoadLutFile(c);
    if (c->tuneDirty) {
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(ID3D11DeviceContext_Map(x, (ID3D11Resource*)c->cbTune, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            memcpy(m.pData, c->tune, sizeof c->tune);
            ID3D11DeviceContext_Unmap(x, (ID3D11Resource*)c->cbTune, 0);
            c->tuneDirty = 0;
        }
    }
    if (!EnsureSize(c, cd.Width, cd.Height)) { c->failed = 1; return 0; }

    SaveState(x, &st);

    // fixed pipeline for all passes
    ID3D11DeviceContext_IASetInputLayout(x, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(x, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_RSSetState(x, c->rs);
    ID3D11DeviceContext_OMSetDepthStencilState(x, c->dss, 0);
    {
        FLOAT bf[4] = { 0, 0, 0, 0 };
        ID3D11DeviceContext_OMSetBlendState(x, c->bs, bf, 0xffffffff);
    }
    ID3D11DeviceContext_VSSetShader(x, c->vs, NULL, 0);
    ID3D11DeviceContext_GSSetShader(x, NULL, NULL, 0);
    cbs[0] = c->cb; cbs[1] = c->cbTune;
    ID3D11DeviceContext_VSSetConstantBuffers(x, 0, 2, cbs);
    ID3D11DeviceContext_PSSetConstantBuffers(x, 0, 2, cbs);
    smp[0] = c->sLin; smp[1] = c->sPoint;
    ID3D11DeviceContext_PSSetSamplers(x, 0, 2, smp);
    ID3D11DeviceContext_RSSetScissorRects(x, 0, NULL);

    // unbind the engine's render targets first: the depth buffer is about to be read as a texture
    ID3D11DeviceContext_OMSetRenderTargets(x, 0, NULL, NULL);

    // 1) colour -> single sample scene copy
    if (cd.SampleDesc.Count > 1)
        ID3D11DeviceContext_ResolveSubresource(x, (ID3D11Resource*)c->scene.tex, 0, (ID3D11Resource*)color, 0, DXGI_FORMAT_R8G8B8A8_UNORM);
    else
        ID3D11DeviceContext_CopyResource(x, (ID3D11Resource*)c->scene.tex, (ID3D11Resource*)color);

    if (c->capture) {
        c->capture = 0;
        if (!c->capDepth.tex) MakeTarget(c, &c->capDepth, c->w, c->h, DXGI_FORMAT_R32_FLOAT);
        if (c->capDepth.tex) {
            memset(s, 0, sizeof s);
            s[1] = c->depthSrv; s[6] = worldSrv;
            DrawPass(c, x, cam, c->capDepth.rtv, c->w, c->h, c->psCapDepth, s, 0, 0, 0, 0);
            WriteCapture(c, x, cam);
        }
    }

    // 2) ambient occlusion from depth (+ edge preserving blur)
    memset(s, 0, sizeof s);
    s[1] = c->depthSrv; s[6] = worldSrv;
    DrawPass(c, x, cam, c->ao.rtv, c->w, c->h, c->psAO, s, 0, 0, 0, 0);
    s[0] = c->ao.srv;
    DrawPass(c, x, cam, c->aoBlur.rtv, c->w, c->h, c->psAOBlur, s, 0, 0, 0, 0);

    // 3) bloom: bright pass, down chain, separable blur per level
    memset(s, 0, sizeof s);
    s[0] = c->scene.srv;
    DrawPass(c, x, cam, c->lv[0].rtv, c->lv[0].w, c->lv[0].h, c->psBright, s, 0, 0, 0, 0);
    for (i = 1; i < LEVELS; i++) {
        s[0] = c->lv[i - 1].srv;
        DrawPass(c, x, cam, c->lv[i].rtv, c->lv[i].w, c->lv[i].h, c->psDown, s, 0, 0, 0, 0);
    }
    for (i = 0; i < LEVELS; i++) {
        s[0] = c->lv[i].srv;
        DrawPass(c, x, cam, c->tmp[i].rtv, c->tmp[i].w, c->tmp[i].h, c->psBlur, s, 1.0f, 0.0f, 0, 0);
        s[0] = c->tmp[i].srv;
        DrawPass(c, x, cam, c->lv[i].rtv, c->lv[i].w, c->lv[i].h, c->psBlur, s, 0.0f, 1.0f, 0, 0);
    }

    // 4) final composite straight into the engine's colour buffer
    memset(s, 0, sizeof s);
    s[0] = c->scene.srv; s[1] = c->depthSrv; s[6] = worldSrv;
    s[2] = c->lv[0].srv; s[3] = c->lv[1].srv; s[4] = c->lv[2].srv;
    s[5] = c->aoBlur.srv;
    s[7] = c->lutSrv;
    s[8] = c->ovB; s[9] = c->ovW;
    DrawPass(c, x, cam, colorRTV, c->w, c->h, c->psFinal, s, 0, 0, 0, 0);

    RestoreState(x, &st);
    return 1;
}
