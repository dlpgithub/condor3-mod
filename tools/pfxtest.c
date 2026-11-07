// pfxtest.c - offline test bench for the post-processing chain (no window, no game needed).
//
//   pfxtest.exe <capture.bin> <postfx.hlsl> <out.bmp> [samples]
//
// capture.bin comes from the game (hotkey F10, see postfx_hook.c) or from tools/make_capture.py. The frame is
// uploaded into a multisampled colour + depth buffer exactly like the game's, PfxCore_Run is executed and the
// result is written as 24 bit BMP. Run it next to D3DCompiler_43.dll.
#define COBJMACROS
#include "../src/postfx_core.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static const char* FILL_HLSL =
    "Texture2D<float4> tc : register(t0);\n"
    "Texture2D<float> td : register(t1);\n"
    "float4 VS(uint id : SV_VertexID) : SV_Position {\n"
    "  float2 p = float2((id << 1) & 2, id & 2); return float4(p * float2(2, -2) + float2(-1, 1), 0, 1); }\n"
    "struct O { float4 c : SV_Target; float d : SV_Depth; };\n"
    "O PS(float4 pos : SV_Position) { O o; o.c = tc.Load(int3(pos.xy, 0)); o.d = td.Load(int3(pos.xy, 0)); return o; }\n";

static void Die(const char* m, HRESULT hr) { printf("FAIL: %s (hr=0x%08lx)\n", m, (unsigned long)hr); exit(1); }

int main(int argc, char** argv) {
    FILE* f;
    struct { char magic[8]; uint32_t w, h; float m00, m11, m22, m32; uint32_t pad; } hdr;
    uint8_t* rgba; float* depth;
    UINT w, h, samples = 4, y;
    ID3D11Device* dev; ID3D11DeviceContext* ctx; D3D_FEATURE_LEVEL fl;
    HRESULT hr;
    D3D11_TEXTURE2D_DESC td;
    D3D11_SUBRESOURCE_DATA sd;
    ID3D11Texture2D *srcC, *srcD, *color, *depthT, *resolved, *stage;
    ID3D11ShaderResourceView *srvC, *srvD;
    ID3D11RenderTargetView* rtv; ID3D11DepthStencilView* dsv;
    ID3D11VertexShader* vs; ID3D11PixelShader* ps;
    ID3D11DepthStencilState* dss;
    HMODULE hc; ID3DBlob *b, *err;
    HRESULT (WINAPI *pCompile)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, void*, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);
    D3D11_VIEWPORT vp;
    PfxCamera cam;
    PfxCore* core;
    D3D11_MAPPED_SUBRESOURCE m;
    FILE* lg;

    if (argc < 4) { printf("usage: pfxtest capture.bin postfx.hlsl out.bmp [samples]\n"); return 2; }
    if (argc > 4) samples = (UINT)atoi(argv[4]);
    lg = fopen("pfxtest.log", "w");
    PfxSetLog(lg ? lg : stdout);

    f = fopen(argv[1], "rb");
    if (!f || fread(&hdr, sizeof hdr, 1, f) != 1 || memcmp(hdr.magic, "C3PFXCAP", 8)) { printf("bad capture\n"); return 2; }
    w = hdr.w; h = hdr.h;
    rgba = (uint8_t*)malloc((size_t)w * h * 4);
    depth = (float*)malloc((size_t)w * h * 4);
    if (fread(rgba, 4, (size_t)w * h, f) != (size_t)w * h || fread(depth, 4, (size_t)w * h, f) != (size_t)w * h) { printf("short capture\n"); return 2; }
    fclose(f);
    printf("capture %ux%u proj %.4f %.4f %.5f %.4f\n", w, h, hdr.m00, hdr.m11, hdr.m22, hdr.m32);

    hr = D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0, D3D11_SDK_VERSION, &dev, &fl, &ctx);
    if (FAILED(hr)) Die("D3D11CreateDevice", hr);

    // ---- source textures (single sample) holding the captured data
    memset(&td, 0, sizeof td);
    td.Width = w; td.Height = h; td.MipLevels = 1; td.ArraySize = 1; td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.pSysMem = rgba; sd.SysMemPitch = w * 4; sd.SysMemSlicePitch = 0;
    if (FAILED(hr = ID3D11Device_CreateTexture2D(dev, &td, &sd, &srcC))) Die("srcC", hr);
    td.Format = DXGI_FORMAT_R32_FLOAT;
    sd.pSysMem = depth;
    if (FAILED(hr = ID3D11Device_CreateTexture2D(dev, &td, &sd, &srcD))) Die("srcD", hr);
    ID3D11Device_CreateShaderResourceView(dev, (ID3D11Resource*)srcC, NULL, &srvC);
    ID3D11Device_CreateShaderResourceView(dev, (ID3D11Resource*)srcD, NULL, &srvD);

    // ---- game-like targets
    td.SampleDesc.Count = samples; td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    if (FAILED(hr = ID3D11Device_CreateTexture2D(dev, &td, NULL, &color))) Die("color", hr);
    td.Format = DXGI_FORMAT_R24G8_TYPELESS; td.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(hr = ID3D11Device_CreateTexture2D(dev, &td, NULL, &depthT))) Die("depth", hr);
    ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource*)color, NULL, &rtv);
    {
        D3D11_DEPTH_STENCIL_VIEW_DESC dv; memset(&dv, 0, sizeof dv);
        dv.Format = DXGI_FORMAT_D24_UNORM_S8_UINT;
        dv.ViewDimension = samples > 1 ? D3D11_DSV_DIMENSION_TEXTURE2DMS : D3D11_DSV_DIMENSION_TEXTURE2D;
        if (FAILED(hr = ID3D11Device_CreateDepthStencilView(dev, (ID3D11Resource*)depthT, &dv, &dsv))) Die("dsv", hr);
    }

    // ---- fill them
    hc = LoadLibraryA("D3DCompiler_43.dll");
    pCompile = hc ? (void*)GetProcAddress(hc, "D3DCompile") : NULL;
    if (!pCompile) Die("D3DCompiler_43.dll", 0);
    err = NULL;
    hr = pCompile(FILL_HLSL, strlen(FILL_HLSL), "fill", NULL, NULL, "VS", "vs_5_0", 0, 0, &b, &err);
    if (FAILED(hr)) Die("fill VS", hr);
    ID3D11Device_CreateVertexShader(dev, ID3D10Blob_GetBufferPointer(b), ID3D10Blob_GetBufferSize(b), NULL, &vs);
    hr = pCompile(FILL_HLSL, strlen(FILL_HLSL), "fill", NULL, NULL, "PS", "ps_5_0", 0, 0, &b, &err);
    if (FAILED(hr)) Die("fill PS", hr);
    ID3D11Device_CreatePixelShader(dev, ID3D10Blob_GetBufferPointer(b), ID3D10Blob_GetBufferSize(b), NULL, &ps);
    {
        D3D11_DEPTH_STENCIL_DESC dd; memset(&dd, 0, sizeof dd);
        dd.DepthEnable = TRUE; dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL; dd.DepthFunc = D3D11_COMPARISON_ALWAYS;
        ID3D11Device_CreateDepthStencilState(dev, &dd, &dss);
    }
    {
        ID3D11ShaderResourceView* sv[2]; sv[0] = srvC; sv[1] = srvD;
        ID3D11DeviceContext_OMSetRenderTargets(ctx, 1, &rtv, dsv);
        ID3D11DeviceContext_OMSetDepthStencilState(ctx, dss, 0);
        vp.TopLeftX = vp.TopLeftY = 0; vp.Width = (float)w; vp.Height = (float)h; vp.MinDepth = 0; vp.MaxDepth = 1;
        ID3D11DeviceContext_RSSetViewports(ctx, 1, &vp);
        ID3D11DeviceContext_IASetPrimitiveTopology(ctx, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ID3D11DeviceContext_VSSetShader(ctx, vs, NULL, 0);
        ID3D11DeviceContext_PSSetShader(ctx, ps, NULL, 0);
        ID3D11DeviceContext_PSSetShaderResources(ctx, 0, 2, sv);
        ID3D11DeviceContext_Draw(ctx, 3, 0);
        sv[0] = sv[1] = NULL;
        ID3D11DeviceContext_PSSetShaderResources(ctx, 0, 2, sv);
    }

    // ---- the thing under test (engine would have color+depth bound at this point)
    core = PfxCore_Create(dev, argv[2]);
    if (!core) Die("PfxCore_Create", 0);
    cam.m00 = hdr.m00; cam.m11 = hdr.m11; cam.m22 = hdr.m22; cam.m32 = hdr.m32; cam.time = 0;
    {
        int ok = 0, i;
        for (i = 0; i < 3; i++) ok = PfxCore_Run(core, ctx, color, rtv, depthT, NULL, &cam);   // a few frames: reload/resize paths
        printf("PfxCore_Run -> %d (see pfxtest.log)\n", ok);
        if (!ok) return 1;
    }

    // ---- read back
    td.SampleDesc.Count = 1; td.BindFlags = 0; td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    ID3D11Device_CreateTexture2D(dev, &td, NULL, &resolved);
    td.BindFlags = 0; td.Usage = D3D11_USAGE_STAGING; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ID3D11Device_CreateTexture2D(dev, &td, NULL, &stage);
    if (samples > 1) ID3D11DeviceContext_ResolveSubresource(ctx, (ID3D11Resource*)resolved, 0, (ID3D11Resource*)color, 0, DXGI_FORMAT_R8G8B8A8_UNORM);
    else ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource*)resolved, (ID3D11Resource*)color);
    ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource*)stage, (ID3D11Resource*)resolved);
    if (FAILED(hr = ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)stage, 0, D3D11_MAP_READ, 0, &m))) Die("map", hr);

    f = fopen(argv[3], "wb");
    {
        uint32_t rowBytes = (w * 3 + 3) & ~3u, imgSize = rowBytes * h, off = 54, fsz = off + imgSize;
        uint8_t fh[54] = { 'B', 'M' };
        uint8_t* row = (uint8_t*)calloc(1, rowBytes);
        uint32_t t32;
        t32 = fsz; memcpy(fh + 2, &t32, 4);
        t32 = off; memcpy(fh + 10, &t32, 4);
        t32 = 40; memcpy(fh + 14, &t32, 4);
        t32 = w; memcpy(fh + 18, &t32, 4);
        t32 = h; memcpy(fh + 22, &t32, 4);
        fh[26] = 1; fh[28] = 24;
        t32 = imgSize; memcpy(fh + 34, &t32, 4);
        fwrite(fh, 1, 54, f);
        for (y = 0; y < h; y++) {
            const uint8_t* s = (const uint8_t*)m.pData + (size_t)(h - 1 - y) * m.RowPitch;
            UINT x;
            for (x = 0; x < w; x++) { row[x * 3] = s[x * 4 + 2]; row[x * 3 + 1] = s[x * 4 + 1]; row[x * 3 + 2] = s[x * 4]; }
            fwrite(row, 1, rowBytes, f);
        }
        fclose(f);
        free(row);
    }
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource*)stage, 0);
    printf("wrote %s\n", argv[3]);
    if (lg) fclose(lg);
    return 0;
}
