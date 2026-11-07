// =====================================================================================================
//  Condor 3 post-processing  (loaded by d3dx.dll from <Condor>\postfx\postfx.hlsl, hot-reloaded: save the
//  file while the game runs and the new look appears within about half a second; a compile error keeps
//  the previous look and is written to <Condor>\postfx\postfx.log)
//
//  Input : the finished frame (sRGB-encoded, no HDR) + the depth buffer.   Output: the graded frame.
//  The HUD is drawn afterwards, so text and instruments stay untouched.
// =====================================================================================================

// ---------------------------------------------------------------- tweakables
#define EXPOSURE         1.00    // linear brightness multiplier (1 = unchanged)
#define SHOULDER         0.72    // highlights above this brightness are compressed softly instead of clipping
#define CONTRAST         0.28    // 0 = off. S-curve in display space: deeper shadows, brighter lights
#define SATURATION       1.06    // 1 = unchanged
#define BLOOM_STRENGTH   0.16    // glow around bright areas (sun, clouds, water glints)
#define BLOOM_THRESHOLD  0.55    // linear brightness where glow starts
#define AO_STRENGTH      0.90    // 0 = off. Darkens creases, tree bases, building edges
#define AO_RADIUS        3.0     // metres
#define AO_FADE_NEAR     1500.0  // metres: AO starts fading ...
#define AO_FADE_FAR      6000.0  //         ... and is gone here
#define SHARPEN          0.35    // local contrast / detail (0 = off)
#define VIGNETTE         0.12
#define HAZE_STRENGTH    0.30    // extra blue depth separation of far terrain
#define HAZE_DISTANCE    30000.0 // metres
#define SKY_SATURATION   1.30    // sky only: deeper blue
#define SKY_DARKEN       0.94    // sky only: slight darkening, richer blue
#define HAZE_COLOR       float3(0.40, 0.56, 0.80)  // linear colour distant terrain fades into (horizon blue)
#define DITHER           1.0     // removes banding in smooth gradients
#define DEBUG_VIEW       0       // 1 = ambient occlusion only, 2 = depth (metres/2000), 3 = bloom only

// ---------------------------------------------------------------- plumbing
cbuffer CB : register(b0)
{
    float4 g_screen;   // full-res width, height, 1/width, 1/height
    float4 g_proj;     // m00, m11, m22, m32 of the camera projection
    float4 g_misc;     // time, frame
    float4 g_dst;      // width, height, 1/w, 1/h of the render target of the current pass
    float4 g_pass;     // pass specific
};

Texture2D g_t0 : register(t0);
#if DEPTH_MS
Texture2DMS<float4> g_depth : register(t1);
#else
Texture2D<float4> g_depth : register(t1);
#endif
Texture2D g_t2 : register(t2);
Texture2D g_t3 : register(t3);
Texture2D g_t4 : register(t4);
Texture2D g_ao : register(t5);

SamplerState g_lin : register(s0);
SamplerState g_pt  : register(s1);

struct VSO { float4 pos : SV_Position; };

VSO VS_Full(uint id : SV_VertexID)
{
    VSO o;
    float2 p = float2((id << 1) & 2, id & 2);
    o.pos = float4(p * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}

static const float3 LUMA = float3(0.2126, 0.7152, 0.0722);

float LoadDepth(int2 p)
{
    p = clamp(p, int2(0, 0), int2(g_screen.xy) - 1);
#if DEPTH_MS
    return g_depth.Load(p, 0).x;
#else
    return g_depth.Load(int3(p, 0)).x;
#endif
}

// D3DX left-handed projection: d = m22 + m32 / z  (works for forward and reversed Z)
// Condor uses a reversed-Z projection (m32 > 0, far plane = depth 0); forward-Z (m32 < 0) is handled too.
bool IsSky(float d) { return (g_proj.w > 0.0) ? (d < 1e-7) : (d > 0.999999); }

float ViewZ(float d) { return g_proj.w / (d - g_proj.z); }

float3 ViewPos(int2 p, float z)
{
    float2 ndc = float2((p.x + 0.5) * g_screen.z * 2.0 - 1.0, 1.0 - (p.y + 0.5) * g_screen.w * 2.0);
    return float3(ndc.x * z / g_proj.x, ndc.y * z / g_proj.y, z);
}

float3 Scene(int2 p)
{
    p = clamp(p, int2(0, 0), int2(g_screen.xy) - 1);
    return g_t0.Load(int3(p, 0)).rgb;
}

float IGN(float2 p, float offset)
{
    return frac(52.9829189 * frac(dot(p + offset, float2(0.06711056, 0.00583715))));
}

// ---------------------------------------------------------------- diagnostics helper
float PS_CapDepth(VSO i) : SV_Target
{
    return LoadDepth(int2(i.pos.xy));
}

// ---------------------------------------------------------------- ambient occlusion (depth only)
#define AO_SAMPLES 12

float PS_AO(VSO i) : SV_Target
{
    int2 p = int2(i.pos.xy);
    float d = LoadDepth(p);
    if (IsSky(d)) return 1.0;

    float z = ViewZ(d);
    float3 P = ViewPos(p, z);

    int2 oL = int2(-1, 0), oR = int2(1, 0), oU = int2(0, -1), oD = int2(0, 1);
    float zl = ViewZ(LoadDepth(p + oL)), zr = ViewZ(LoadDepth(p + oR));
    float zu = ViewZ(LoadDepth(p + oU)), zd = ViewZ(LoadDepth(p + oD));
    float3 dx = (abs(zr - z) < abs(z - zl)) ? ViewPos(p + oR, zr) - P : P - ViewPos(p + oL, zl);
    float3 dy = (abs(zd - z) < abs(z - zu)) ? ViewPos(p + oD, zd) - P : P - ViewPos(p + oU, zu);
    float3 N = normalize(cross(dx, dy));
    if (dot(N, P) > 0) N = -N;                       // face the camera

    float pxPerMeter = 0.5 * g_screen.x * g_proj.x / z;
    float rpx = clamp(AO_RADIUS * pxPerMeter, 3.0, 96.0);
    float angle0 = IGN(i.pos.xy, 0.0) * 6.2831853;

    float occ = 0;
    [unroll]
    for (int k = 0; k < AO_SAMPLES; k++)
    {
        float t = (k + 0.5) / AO_SAMPLES;
        float a = angle0 + k * 2.3999632;
        int2 o = int2(round(float2(cos(a), sin(a)) * (sqrt(t) * rpx)));
        int2 q = p + o;
        float zq = ViewZ(LoadDepth(q));
        float3 v = ViewPos(q, zq) - P;
        float len = length(v) + 1e-4;
        float c = dot(N, v) / len;
        float w = saturate(1.0 - len / AO_RADIUS);
        occ += saturate(c - 0.12) * w;
    }
    return saturate(1.0 - 1.7 * occ / AO_SAMPLES);
}

float PS_AOBlur(VSO i) : SV_Target
{
    int2 p = int2(i.pos.xy);
    float d = LoadDepth(p);
    if (IsSky(d)) return 1.0;
    float z = ViewZ(d);
    float sum = 0, wsum = 0;
    [unroll] for (int y = -1; y <= 2; y++)
    [unroll] for (int x = -1; x <= 2; x++)
    {
        int2 q = clamp(p + int2(x, y), int2(0, 0), int2(g_screen.xy) - 1);
        float zq = ViewZ(LoadDepth(q));
        float w = saturate(1.0 - abs(zq - z) / (0.04 * z + 0.05));
        sum += g_t0.Load(int3(q, 0)).r * w;
        wsum += w;
    }
    return sum / max(wsum, 1e-3);
}

// ---------------------------------------------------------------- bloom
float4 PS_Bright(VSO i) : SV_Target
{
    float2 uv = i.pos.xy * g_dst.zw;
    float2 t = g_screen.zw;
    float3 c = g_t0.SampleLevel(g_lin, uv + float2(-1, -1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2( 1, -1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2(-1,  1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2( 1,  1) * t, 0).rgb;
    float3 l = pow(max(c * 0.25, 0.0), 2.2);
    float lum = dot(l, LUMA);
    float k = max(lum - BLOOM_THRESHOLD, 0.0) / max(lum, 1e-3);
    return float4(l * k, 1);
}

float4 PS_Down(VSO i) : SV_Target
{
    float2 uv = i.pos.xy * g_dst.zw;
    float2 t = g_dst.zw * 0.5;
    float3 c = g_t0.SampleLevel(g_lin, uv + float2(-1, -1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2( 1, -1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2(-1,  1) * t, 0).rgb
             + g_t0.SampleLevel(g_lin, uv + float2( 1,  1) * t, 0).rgb;
    return float4(c * 0.25, 1);
}

float4 PS_Blur(VSO i) : SV_Target
{
    float2 uv = i.pos.xy * g_dst.zw;
    float2 step = g_pass.xy * g_dst.zw;
    float3 c = g_t0.SampleLevel(g_lin, uv, 0).rgb * 0.2270270270;
    c += (g_t0.SampleLevel(g_lin, uv + step * 1.3846153846, 0).rgb + g_t0.SampleLevel(g_lin, uv - step * 1.3846153846, 0).rgb) * 0.3162162162;
    c += (g_t0.SampleLevel(g_lin, uv + step * 3.2307692308, 0).rgb + g_t0.SampleLevel(g_lin, uv - step * 3.2307692308, 0).rgb) * 0.0702702703;
    return float4(c, 1);
}

// ---------------------------------------------------------------- final composite
float3 ACES(float3 x)
{
    return saturate((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14));
}

float4 PS_Final(VSO i) : SV_Target
{
    int2 p = int2(i.pos.xy);
    float2 uv = i.pos.xy * g_screen.zw;

    // --- sharpen in display space, limited to the local range so it cannot ring
    float3 c  = Scene(p);
    float3 cL = Scene(p + int2(-1, 0)), cR = Scene(p + int2(1, 0));
    float3 cU = Scene(p + int2(0, -1)), cD = Scene(p + int2(0, 1));
    float3 lo = min(c, min(min(cL, cR), min(cU, cD)));
    float3 hi = max(c, max(max(cL, cR), max(cU, cD)));
    float3 avg = (cL + cR + cU + cD) * 0.25;
    c = clamp(c + (c - avg) * SHARPEN, lo, hi);

    float3 lin = pow(max(c, 0.0), 2.2);

    float d = LoadDepth(p);
    if (!IsSky(d))
    {
        float z = ViewZ(d);
        float ao = g_ao.Load(int3(p, 0)).r;
        float fade = 1.0 - saturate((z - AO_FADE_NEAR) / (AO_FADE_FAR - AO_FADE_NEAR));
        lin *= lerp(1.0, ao, AO_STRENGTH * fade);

        // aerial perspective: distant terrain loses contrast/saturation and fades towards the horizon blue
        float t = saturate((1.0 - exp(-z / HAZE_DISTANCE)) * HAZE_STRENGTH * 1.0);
        float lz = dot(lin, LUMA);
        float3 faded = lerp(float3(lz, lz, lz), lin, 1.0 - 0.45 * t);
        lin = lerp(faded, HAZE_COLOR * (0.55 + 0.9 * lz), t * 0.75);
    }
    else
    {
        float ls0 = dot(lin, LUMA);
        lin = lerp(float3(ls0, ls0, ls0), lin, SKY_SATURATION) * float3(SKY_DARKEN, SKY_DARKEN * 1.01, 1.02);
    }

    float3 bloom = g_t2.SampleLevel(g_lin, uv, 0).rgb * 0.45
                 + g_t3.SampleLevel(g_lin, uv, 0).rgb * 0.35
                 + g_t4.SampleLevel(g_lin, uv, 0).rgb * 0.20;
    lin += bloom * BLOOM_STRENGTH;
    lin *= EXPOSURE;

    // soft highlight shoulder on luminance: keeps hue and saturation, no hard clipping
    float l = dot(lin, LUMA);
    float ls = (l < SHOULDER) ? l : SHOULDER + (1.0 - SHOULDER) * (1.0 - exp(-(l - SHOULDER) / (1.0 - SHOULDER)));
    lin *= ls / max(l, 1e-5);
    l = dot(lin, LUMA);
    lin = max(lerp(float3(l, l, l), lin, SATURATION), 0.0);

    float3 g = pow(saturate(lin), 1.0 / 2.2);
    g = lerp(g, g * g * (3.0 - 2.0 * g), CONTRAST);
    lin = g;

    float2 q = uv - 0.5;
    lin *= 1.0 - VIGNETTE * dot(q, q) * 2.0;

    float3 outc = saturate(lin);
#if DEBUG_VIEW == 1
    outc = g_ao.Load(int3(p, 0)).r;
#elif DEBUG_VIEW == 2
    outc = IsSky(d) ? 1.0 : saturate(ViewZ(d) / 2000.0);
#elif DEBUG_VIEW == 3
    outc = saturate(bloom * 4.0);
#endif
    float n = (IGN(i.pos.xy, g_misc.y) + IGN(i.pos.xy, g_misc.y + 17.0) - 1.0) * (DITHER / 255.0);
    return float4(outc + n, 1);
}
