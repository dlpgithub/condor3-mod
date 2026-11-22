// postfx_core.h - game independent post-processing renderer (D3D11, plain C).
//
// Takes the finished (possibly multisampled) colour buffer + its depth buffer of a frame, runs a chain of
// full-screen passes (SSAO, bloom, tone mapping, grading, sharpening, ...) defined in an external HLSL file
// and writes the result back into the colour buffer. Used by the game hook (postfx_hook.c) and by the
// offline test tool (tools/pfxtest.c), which is why it knows nothing about Condor.
#ifndef POSTFX_CORE_H
#define POSTFX_CORE_H

#include <windows.h>
#include <d3d11.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PfxCamera {
    float m00, m11, m22, m32;   // projection terms of the D3DX (left handed, row major) matrix
    float time;                 // seconds
    // engine atmosphere (optional, camPos[3] = 1 when valid): copied from the engine's terrain constant buffer
    float view[16];             // engine View matrix as laid out in the HLSL constant buffer (column major)
    float camPos[4];            // world position, w = valid flag
    float fog[4], sunDir[4], sunColor[4], amb[4];
} PfxCamera;

typedef struct PfxCore PfxCore;

// Log sink (may be NULL). Lines are written with fprintf + fflush.
void PfxSetLog(FILE* f);

// hlslPath: full path of postfx.hlsl (loaded at creation and hot-reloaded when its timestamp changes).
PfxCore* PfxCore_Create(ID3D11Device* dev, const char* hlslPath);
void     PfxCore_Destroy(PfxCore* c);

// Runs the chain. color = (multisampled or not) R8G8B8A8_UNORM texture, colorRTV its render target view,
// depth = typeless depth texture created with D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_DEPTH_STENCIL.
// All device state touched is saved and restored. Returns 1 when the chain ran.
int PfxCore_Run(PfxCore* c, ID3D11DeviceContext* ctx,
                ID3D11Texture2D* color, ID3D11RenderTargetView* colorRTV,
                ID3D11Texture2D* depth, ID3D11Texture2D* world, const PfxCamera* cam);
// world (may be NULL): copy of the depth buffer taken before the engine cleared it for the cockpit; the final
// depth used by the shaders is max(depth, world) (reversed Z: larger = nearer).

// Asks the next PfxCore_Run to write <path> (colour + raw depth + camera) for offline tuning.
void PfxCore_RequestCapture(PfxCore* c, const char* path);

// Current value of live tunable <slot> (see '//@tune' lines in postfx.hlsl); 'fallback' when unavailable.
float PfxCore_GetTune(PfxCore* c, int slot, float fallback);

// Force a shader reload at the next run.
void PfxCore_RequestReload(PfxCore* c);
// Translucent overlay layer for the next PfxCore_Run: the overlay drawn onto black and onto white (same size as the colour
// buffer). NULL = none this frame.
void PfxCore_SetOverlay(PfxCore* c, ID3D11ShaderResourceView* black, ID3D11ShaderResourceView* white);

#ifdef __cplusplus
}
#endif
#endif
