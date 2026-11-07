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
#define CONTRAST         0.45    // 0 = off. S-curve in display space: deeper shadows, brighter lights
#define SATURATION       1.12    // 1 = unchanged
#define BLOOM_STRENGTH   0.22    // glow around bright areas (sun, clouds, water glints)
#define BLOOM_THRESHOLD  0.55    // linear brightness where glow starts
#define AO_STRENGTH      1.80    // 0 = off. Darkens creases, tree bases, building edges
#define AO_RADIUS        8.0     // metres
#define AO_FADE_NEAR     1500.0  // metres: AO starts fading ...
#define AO_FADE_FAR      6000.0  //         ... and is gone here
#define SHARPEN          0.70    // local contrast / detail (0 = off)
#define VIGNETTE         0.18
#define HAZE_STRENGTH    0.90    // extra blue depth separation of far terrain
#define HAZE_DISTANCE    9000.0 // metres (fallback when no engine data)
#define HAZE_DENSITY     7.0e-5  // extinction per metre at sea level (physical-ish haze)
#define HAZE_HEIGHT      2500.0  // metres: scale height of the haze (denser in valleys)
#define AUTO_EXPOSURE    0.30    // eye adaptation: brightens low-sun scenes (0 = off), uses the engine sun elevation
#define HAZE_INSCATTER   1.0     // strength of the scattered light added
#define SKY_SATURATION   1.18    // sky only: deeper blue
#define SKY_DARKEN       0.93    // sky only: slight darkening, richer blue
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
    column_major float4x4 g_view;   // engine view matrix
    float4 g_camPos;   // world camera position, w = 1 when the engine atmosphere data is valid
    float4 g_fog;      // engine fog colour
    float4 g_sunDir;   // engine sun direction (world, z up)
    float4 g_sunColor; // engine sun colour
    float4 g_amb;      // engine ambient colour
    column_major float4x4 g_prevView;  // previous frame view (TAA)
    float4 g_prevCam;  // previous camera position, w = 1 when valid
};

Texture2D g_t0 : register(t0);
#if DEPTH_MS
Texture2DMS<float4> g_depth : register(t1);
Texture2DMS<float4> g_world : register(t6);
#else
Texture2D<float4> g_depth : register(t1);
Texture2D<float4> g_world : register(t6);
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
    return max(g_depth.Load(p, 0).x, g_world.Load(p, 0).x);
#else
    return max(g_depth.Load(int3(p, 0)).x, g_world.Load(int3(p, 0)).x);
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


// The engine's own sky colour formula (EffectSky/EffectLand vertex shader), evaluated per direction.
// Haze fades to exactly this colour, so distant terrain meets the sky without a seam, including the glow towards the sun.
float3 EngineSky(float3 look, float3 fogColor, float3 sunColor, float3 sunPos)
{
    float h = max(look.z, 0);
    float3 baseColor = float3(fogColor.x / (4.0 * h + 1.0), fogColor.y / (1.8 * h + 1.0), fogColor.z / (0.5 * h + 1.0));
    float ang  = acos(clamp(dot(sunPos, look), -1.0, 1.0));
    float angZ = max(look.z - sunPos.z, 0);
    float f = exp(-2.0 * (0.1 + 0.9 * ang) * (0.5 + 0.5 * sunPos.z) - angZ * (1.0 - sqrt(max(sunPos.z, 0))));
    return baseColor + f * (sunColor - baseColor);
}

// Height-dependent aerial perspective: optical depth along the ray with an exponentially decaying density
// (valleys are hazier than the air at altitude), wavelength dependent extinction, in-scatter in the engine's sky colour.
float3 Atmosphere(float3 lin, int2 p, float z)
{
    float3 vp = ViewPos(p, z);
    float dist = length(vp);
    float3 wdir = mul(vp / dist, transpose((float3x3)g_view));
    float k = dist * wdir.z / HAZE_HEIGHT;
    float path = (abs(k) < 1e-3) ? dist : dist * (1.0 - exp(-k)) / k;      // integral of exp(-dz*s/H) ds
    float lowSun = smoothstep(0.05, 0.5, g_sunDir.z);                        // low sun: thinner, less glaring haze
    float tau = HAZE_DENSITY * lerp(0.55, 1.0, lowSun) * exp(-g_camPos.z / HAZE_HEIGHT) * path;
    float3 trans = exp(-tau * float3(0.80, 1.00, 1.30));                    // blue is scattered out of the light path first
    float3 look = normalize(float3(wdir.xy, max(wdir.z, 0.0) * 0.35));      // sky colour near the horizon in that direction
    float3 hazeCol = pow(saturate(EngineSky(look, g_fog.rgb, g_sunColor.rgb, g_sunDir.xyz)), 2.2);
    return lin * trans + hazeCol * (1.0 - trans) * HAZE_INSCATTER * lerp(0.6, 1.0, lowSun);
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


// ---------------------------------------------------------------- temporal anti-aliasing
// The game renders every frame with a sub-pixel shifted viewport (Halton 2,3). Here the current frame is combined
// with the previous result, reprojected with depth + camera motion, and clamped to the local colour range.
#define TAA_BLEND 0.08      // weight of the new frame (lower = smoother but slower to settle)

float3 TAA_History(float2 px)
{   // Catmull-Rom, 5 bilinear taps
    float2 pos = px;
    float2 c = floor(pos - 0.5) + 0.5;
    float2 f = pos - c;
    float2 w0 = f * (-0.5 + f * (1.0 - 0.5 * f));
    float2 w1 = 1.0 + f * f * (-2.5 + 1.5 * f);
    float2 w2 = f * (0.5 + f * (2.0 - 1.5 * f));
    float2 w3 = f * f * (-0.5 + 0.5 * f);
    float2 w12 = w1 + w2;
    float2 p0 = (c - 1.0) * g_screen.zw, p3 = (c + 2.0) * g_screen.zw, p12 = (c + w2 / w12) * g_screen.zw;
    float3 r = g_t2.SampleLevel(g_lin, float2(p12.x, p0.y), 0).rgb * (w12.x * w0.y)
             + g_t2.SampleLevel(g_lin, float2(p0.x, p12.y), 0).rgb * (w0.x * w12.y)
             + g_t2.SampleLevel(g_lin, p12, 0).rgb * (w12.x * w12.y)
             + g_t2.SampleLevel(g_lin, float2(p3.x, p12.y), 0).rgb * (w3.x * w12.y)
             + g_t2.SampleLevel(g_lin, float2(p12.x, p3.y), 0).rgb * (w12.x * w3.y);
    float wsum = w12.x * w0.y + w0.x * w12.y + w12.x * w12.y + w3.x * w12.y + w12.x * w3.y;
    return max(r / wsum, 0.0);
}

struct TAAO { float4 c : SV_Target0; float4 h : SV_Target1; };

TAAO PS_TAA(VSO i)
{
    int2 p = int2(i.pos.xy);
    float3 cur = Scene(p);
    TAAO o;

    float dc, dw;
#if DEPTH_MS
    dc = g_depth.Load(p, 0).x; dw = g_world.Load(p, 0).x;
#else
    dc = g_depth.Load(int3(p, 0)).x; dw = g_world.Load(int3(p, 0)).x;
#endif
    float d = max(dc, dw);
    bool cockpit = (dc > dw) && !IsSky(dc);       // the engine clears depth again before the cockpit: it moves with the camera

    float2 px = i.pos.xy;
    bool ok = (g_pass.x > 0.5);
    bool okWorld = (g_prevCam.w > 0.5);
    if (!cockpit)
    {
        float3 pv;
        float2 ndc = float2((p.x + 0.5) * g_screen.z * 2.0 - 1.0, 1.0 - (p.y + 0.5) * g_screen.w * 2.0);
        float3x3 R = (float3x3)g_view;
        float3x3 Rp = (float3x3)g_prevView;
        if (IsSky(d))
        {
            float3 wd = mul(float3(ndc.x / g_proj.x, ndc.y / g_proj.y, 1.0), transpose(R));
            pv = mul(wd, Rp);
        }
        else
        {
            float3 wp = mul(ViewPos(p, ViewZ(d)), transpose(R));
            pv = mul(wp + g_camPos.xyz - g_prevCam.xyz, Rp);
        }
        okWorld = okWorld && (pv.z > 0.01);
        float2 n2 = float2(pv.x / max(pv.z, 0.01) * g_proj.x, pv.y / max(pv.z, 0.01) * g_proj.y);
        px = float2(n2.x * 0.5 + 0.5, 0.5 - n2.y * 0.5) * g_screen.xy;
        okWorld = okWorld && all(px > 1.0) && all(px < g_screen.xy - 1.0);
        if (!okWorld) px = i.pos.xy;
    }

    float3 res = cur;
    if (ok)
    {
        float3 m1 = 0, m2 = 0, mn = 1e9, mx = -1e9;
        [unroll] for (int y = -1; y <= 1; y++)
        [unroll] for (int x = -1; x <= 1; x++)
        {
            float3 s = Scene(p + int2(x, y));
            m1 += s; m2 += s * s; mn = min(mn, s); mx = max(mx, s);
        }
        m1 /= 9.0; m2 /= 9.0;
        float3 sd = sqrt(max(m2 - m1 * m1, 0.0));
        float3 lo = max(mn, m1 - 1.25 * sd), hi = min(mx, m1 + 1.25 * sd);
        // two motion hypotheses: static world (reprojected) and "moves with the camera" (cockpit, own glider body in
        // external view). Use whichever history agrees better with the current neighbourhood.
        float3 h = TAA_History(px);
        float3 hc = clamp(h, lo, hi);
        float dist = length(h - hc);
        float3 h2 = g_t2.SampleLevel(g_lin, i.pos.xy * g_screen.zw, 0).rgb;
        float3 hc2 = clamp(h2, lo, hi);
        float dist2 = length(h2 - hc2);
        if (cockpit || dist2 < dist * 0.6) { h = h2; hc = hc2; dist = dist2; }
        float k = saturate(TAA_BLEND + dist * 0.35);
        res = lerp(hc, cur, k);
    }
    o.c = float4(res, 1);
    o.h = float4(res, 1);
    return o;
}

// ---------------------------------------------------------------- final composite
float3 ACES(float3 x)
{
    return saturate((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14));
}


// ---------------------------------------------------------------- spatial edge smoothing (FXAA-like, on the TAA result)
#define EDGE_AA 0.85        // 0 = off
float Luma2(float3 c) { return dot(c, float3(0.299, 0.587, 0.114)); }

float3 EdgeAA(int2 p)
{
    float3 cM = Scene(p);
    {   // only geometry silhouettes (depth jumps): texture detail, text and needles must stay crisp
        float d0 = LoadDepth(p);
        float z0 = IsSky(d0) ? 1e5 : ViewZ(d0);
        float edge = 0;
        int2 o[4] = { int2(1, 0), int2(-1, 0), int2(0, 1), int2(0, -1) };
        [unroll] for (int k = 0; k < 4; k++)
        {
            float dn = LoadDepth(p + o[k]);
            float zn = IsSky(dn) ? 1e5 : ViewZ(dn);
            edge = max(edge, abs(zn - z0) / max(min(zn, z0), 0.1));
        }
        if (edge < 0.03) return cM;
    }
    float3 cN = Scene(p + int2(0, -1)), cS = Scene(p + int2(0, 1)), cE = Scene(p + int2(1, 0)), cW = Scene(p + int2(-1, 0));
    float lM = Luma2(cM), lN = Luma2(cN), lS = Luma2(cS), lE = Luma2(cE), lW = Luma2(cW);
    float lmax = max(lM, max(max(lN, lS), max(lE, lW)));
    float lmin = min(lM, min(min(lN, lS), min(lE, lW)));
    float range = lmax - lmin;
    if (range < max(0.04, lmax * 0.15)) return cM;
    float lNW = Luma2(Scene(p + int2(-1, -1))), lNE = Luma2(Scene(p + int2(1, -1)));
    float lSW = Luma2(Scene(p + int2(-1, 1))),  lSE = Luma2(Scene(p + int2(1, 1)));
    float eH = abs(lNW + lNE - 2 * lN) + 2 * abs(lW + lE - 2 * lM) + abs(lSW + lSE - 2 * lS);
    float eV = abs(lNW + lSW - 2 * lW) + 2 * abs(lN + lS - 2 * lM) + abs(lNE + lSE - 2 * lE);
    bool horz = eH >= eV;
    float l1 = horz ? lN : lW, l2 = horz ? lS : lE;
    float sgn = (abs(l1 - lM) >= abs(l2 - lM)) ? -1.0 : 1.0;
    float2 dir = horz ? float2(0, sgn) : float2(sgn, 0);
    float3 cOff = g_t0.SampleLevel(g_lin, (float2(p) + 0.5 + dir * 0.5) * g_screen.zw, 0).rgb;
    float avg = (2.0 * (lN + lS + lE + lW) + lNW + lNE + lSW + lSE) / 12.0;
    float sub = saturate(abs(avg - lM) / max(range, 1e-4));
    float w = saturate(0.5 + 0.5 * sub) * EDGE_AA;
    return lerp(cM, cOff, w);
}

float4 PS_Final(VSO i) : SV_Target
{
    int2 p = int2(i.pos.xy);
    float2 uv = i.pos.xy * g_screen.zw;

    // --- sharpen in display space, limited to the local range so it cannot ring
    float3 c  = EdgeAA(p);
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

        if (g_camPos.w > 0.5)
        {
            lin = Atmosphere(lin, p, z);
        }
        else
        {
            // aerial perspective fallback (no engine data): fade towards a fixed horizon blue
            float t = saturate((1.0 - exp(-z / HAZE_DISTANCE)) * HAZE_STRENGTH * 1.0);
            float lz = dot(lin, LUMA);
            float3 faded = lerp(float3(lz, lz, lz), lin, 1.0 - 0.45 * t);
            lin = lerp(faded, HAZE_COLOR * (0.55 + 0.9 * lz), t * 0.75);
        }
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
    float expo = EXPOSURE;
    if (g_camPos.w > 0.5 && !IsSky(d)) expo *= 1.0 + AUTO_EXPOSURE * (1.0 - smoothstep(0.05, 0.65, g_sunDir.z));   // ground only: the sky is already bright
    lin *= expo;

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
