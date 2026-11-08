// =====================================================================================================
//  Condor 3 post-processing  (loaded by d3dx.dll from <Condor>\postfx\postfx.hlsl, hot-reloaded: save the
//  file while the game runs and the new look appears within about half a second; a compile error keeps
//  the previous look and is written to <Condor>\postfx\postfx.log)
//
//  Input : the finished frame (sRGB-encoded, no HDR) + the depth buffer.   Output: the graded frame.
//  The HUD is drawn afterwards, so text and instruments stay untouched.
// =====================================================================================================

// ---------------------------------------------------------------- live tunables
// Values come from constant buffer b1 and can be changed while the game runs: edit <Condor>\postfx\tune.txt
// (lines "name value") and save. The numbers below are the defaults used when tune.txt does not set a name.
// To add a tunable: add a '//@tune <slot> <name> <default>' line and use T(<slot>) in the shader.
//@tune 0 exposure 1
//@tune 1 shoulder 0.72
//@tune 2 contrast 0.45
//@tune 3 saturation 1.12
//@tune 4 bloom_strength 0.22
//@tune 5 bloom_tint 0   // warm halo colour around bright areas (0 = neutral)
//@tune 6 ao_strength 1.8
//@tune 7 sharpen 0.5
//@tune 8 vignette 0.18
//@tune 9 haze_inscatter 1
//@tune 10 sky_saturation 1.18
//@tune 11 sky_darken 0.93
//@tune 12 auto_exposure 0.3
//@tune 13 dither 1
//@tune 14 clarity 0   // local contrast on mid-tones, large radius (0 = off, ~0.3 = DSLR-like)
//@tune 15 clarity_radius 14   // pixels
//@tune 16 sky_zenith 0   // deeper blue overhead (0 = off, ~0.4)
//@tune 17 sky_horizon 0   // pale glow near the horizon (0 = off, ~0.5)
//@tune 18 chroma_ab 0   // lens colour fringing at the screen edges, pixels (0 = off, ~1.0)
//@tune 19 grain 0   // fine sensor grain (0 = off, ~0.3)
//@tune 20 shadow_cool 0   // cool tint in shadows (0 = off, ~0.5)
//@tune 21 highlight_warm 0   // warm tint in highlights (0 = off, ~0.5)
//@tune 22 green_warm 0   // greens toward yellow-olive (0 = off, ~0.5)
//@tune 23 green_desat 0   // less neon greens (0 = off, ~0.3)
//@tune 24 shadow_lift 0   // open up the darkest shadows (0 = off, ~0.5)
//@tune 25 terrain_contrast 0   // extra S-curve on terrain only, sky and aircraft untouched (0 = off)
//@tune 40 land_shadow 0.62     // terrain: how dark terrain/aircraft shadows get (0 = none, 1 = ambient only)
//@tune 41 land_hemi 0.70       // terrain: sky/ground ambient split by slope (0 = flat ambient)
//@tune 42 land_sun 1.10        // terrain: sunlight gain
//@tune 43 land_amb 0.70        // terrain: ambient (sky) light gain
//@tune 44 land_cloud_shadow 0.62   // terrain: how dark cloud shadows get
//@tune 46 lut_cockpit 0         // how much of the LUT is applied to the cockpit interior (0 = none, 1 = same as outside)
//@tune 47 haze_scale 1           // distance haze density multiplier (1 = current, 0 = clear air)
//@tune 48 haze_blue 0            // 0 = current extinction colour, 1 = Rayleigh-like (distance turns strongly blue, near stays neutral)
//@tune 49 haze_sun_glow 0        // forward-scattering glow of the haze towards the sun (Mie phase), 0 = off
//@tune 50 haze_g 0.76            // anisotropy of that glow (0 = even, 0.9 = tight around the sun)
//@tune 45 lut_strength 0        // colour grading LUT (postfx/lut.cube): 0 = off, 1 = full
cbuffer TUNE : register(b1) { float4 g_t[16]; }
#define T(i) (g_t[(i) >> 2][(i) & 3])
#define BLOOM_TINT       T(5)
#define CLARITY          T(14)
#define CLARITY_RADIUS   T(15)
#define SKY_ZENITH       T(16)
#define SKY_HORIZON      T(17)
#define CHROMA_AB        T(18)
#define GRAIN            T(19)
#define SHADOW_COOL      T(20)
#define HIGHLIGHT_WARM   T(21)
#define GREEN_WARM       T(22)
#define GREEN_DESAT      T(23)
#define SHADOW_LIFT      T(24)
#define TERRAIN_CONTRAST T(25)
#define LUT_STRENGTH     T(45)
#define LUT_COCKPIT      T(46)
#define HAZE_SCALE       T(47)
#define HAZE_BLUE        T(48)
#define HAZE_SUN_GLOW    T(49)
#define HAZE_G           T(50)
#define LUT_SIZE         T(63)   // written by the mod when a LUT is loaded

// ---------------------------------------------------------------- tweakables (fixed values; the ones marked T(n) are live)
#define EXPOSURE         T(0)    // linear brightness multiplier (1 = unchanged)
#define SHOULDER         T(1)    // highlights above this brightness are compressed softly instead of clipping
#define CONTRAST         T(2)    // 0 = off. S-curve in display space: deeper shadows, brighter lights
#define SATURATION       T(3)    // 1 = unchanged
#define BLOOM_STRENGTH   T(4)    // glow around bright areas (sun, clouds, water glints)
#define BLOOM_THRESHOLD  0.55    // linear brightness where glow starts
#define AO_STRENGTH      T(6)    // 0 = off. Darkens creases, tree bases, building edges
#define AO_RADIUS        8.0     // metres
#define AO_FADE_NEAR     1500.0  // metres: AO starts fading ...
#define AO_FADE_FAR      6000.0  //         ... and is gone here
#define SHARPEN          T(7)    // local contrast / detail (0 = off)
#define VIGNETTE         T(8)
#define HAZE_STRENGTH    0.90    // extra blue depth separation of far terrain
#define HAZE_DISTANCE    9000.0 // metres (fallback when no engine data)
#define HAZE_DENSITY     5.0e-5  // extinction per metre at sea level (physical-ish haze)
#define HAZE_HEIGHT      2500.0  // metres: scale height of the haze (denser in valleys)
#define AUTO_EXPOSURE    T(12)    // eye adaptation: brightens low-sun scenes (0 = off), uses the engine sun elevation
#define HAZE_INSCATTER   T(9)     // strength of the scattered light added
#define SKY_SATURATION   T(10)    // sky only: deeper blue
#define SKY_DARKEN       T(11)    // sky only: slight darkening, richer blue
#define HAZE_COLOR       float3(0.40, 0.56, 0.80)  // linear colour distant terrain fades into (horizon blue)
#define DITHER           T(13)     // removes banding in smooth gradients
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
Texture3D<float4> g_lut : register(t7);

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

// Cockpit pixel: the raw depth is nearer than the depth saved before the engine cleared it for the cockpit pass.
bool CockpitPx(int2 p)
{
    p = clamp(p, int2(0, 0), int2(g_screen.xy) - 1);
#if DEPTH_MS
    float dc = g_depth.Load(p, 0).x, dw = g_world.Load(p, 0).x;
#else
    float dc = g_depth.Load(int3(p, 0)).x, dw = g_world.Load(int3(p, 0)).x;
#endif
    return dc > dw && !IsSky(dc);
}


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
    float altFade = lerp(1.0, 0.45, smoothstep(1000.0, 2800.0, g_camPos.z));   // high up (cloud level) the air looks much clearer
    float tau = HAZE_DENSITY * HAZE_SCALE * lerp(0.55, 1.0, lowSun) * altFade * exp(-g_camPos.z / HAZE_HEIGHT) * path;
    float3 ext = lerp(float3(0.80, 1.00, 1.30), float3(0.45, 1.00, 2.40), HAZE_BLUE);
    float3 trans = exp(-tau * ext);                    // blue is scattered out of the light path first
    float3 look = normalize(float3(wdir.xy, max(wdir.z, 0.0) * 0.35));      // sky colour near the horizon in that direction
    float3 hazeCol = pow(saturate(EngineSky(look, g_fog.rgb, g_sunColor.rgb, g_sunDir.xyz)), 2.2);
    float cosT = dot(wdir, g_sunDir.xyz);
    float gg = HAZE_G;
    float phase = (1.0 - gg * gg) / pow(max(1.0 + gg * gg - 2.0 * gg * cosT, 1e-3), 1.5);   // Henyey-Greenstein, ~1 when isotropic
    float3 glow = pow(saturate(g_sunColor.rgb), 2.2) * HAZE_SUN_GLOW * saturate(phase * 0.25 - 0.1);
    return lin * trans + (hazeCol + glow) * (1.0 - trans) * HAZE_INSCATTER * lerp(0.6, 1.0, lowSun);
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
    if (CHROMA_AB > 0.0)   // lens colour fringing, zero in the centre, grows towards the corners
    {
        float2 dd = uv - 0.5;
        float2 sh = dd * dot(dd, dd) * 2.0 * CHROMA_AB * g_screen.zw;
        c.r = g_t0.SampleLevel(g_lin, uv + sh, 0).r;
        c.b = g_t0.SampleLevel(g_lin, uv - sh, 0).b;
    }
    float3 cL = Scene(p + int2(-1, 0)), cR = Scene(p + int2(1, 0));
    float3 cU = Scene(p + int2(0, -1)), cD = Scene(p + int2(0, 1));
    float3 lo = min(c, min(min(cL, cR), min(cU, cD)));
    float3 hi = max(c, max(max(cL, cR), max(cU, cD)));
    float3 avg = (cL + cR + cU + cD) * 0.25;
    c = clamp(c + (c - avg) * SHARPEN, lo, hi);
    if (CLARITY > 0.0)   // local contrast: luminance minus a wide blur, mid-tones only, limited so it cannot halo
    {
        float lc = dot(c, LUMA), lb = 0.0;
        [unroll] for (int k = 0; k < 8; k++)
        {
            float a = k * 0.7853982 + 0.3;
            float2 o = float2(cos(a), sin(a));
            lb += dot(Scene(p + int2(round(o * CLARITY_RADIUS))), LUMA);
            lb += dot(Scene(p + int2(round(o * CLARITY_RADIUS * 0.5))), LUMA);
        }
        lb = (lb + lc) / 17.0;
        float mid = 1.0 - saturate(abs(lc * 2.0 - 1.0));
        c += clamp((lc - lb) * CLARITY * mid * 1.5, -0.12, 0.12);
    }

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
        if (g_camPos.w > 0.5 && (SKY_ZENITH > 0.0 || SKY_HORIZON > 0.0))
        {
            float3 vd = normalize(ViewPos(p, 1.0));
            float elev = mul(vd, transpose((float3x3)g_view)).z;                 // 1 = straight up, 0 = horizon
            float zen = smoothstep(0.15, 0.95, elev);
            float hor = 1.0 - smoothstep(0.0, 0.30, elev);
            lin *= 1.0 - SKY_ZENITH * zen * float3(0.55, 0.38, 0.12);            // overhead: deeper, bluer
            lin += SKY_HORIZON * hor * 0.06 * float3(0.95, 1.0, 1.05);           // horizon: pale haze glow
        }
    }

    float3 bloom = g_t2.SampleLevel(g_lin, uv, 0).rgb * 0.45
                 + g_t3.SampleLevel(g_lin, uv, 0).rgb * 0.35
                 + g_t4.SampleLevel(g_lin, uv, 0).rgb * 0.20;
    lin += bloom * lerp(float3(1, 1, 1), float3(1.2, 1.0, 0.7), BLOOM_TINT) * BLOOM_STRENGTH;
    // colour grade (all off by default)
    {
        float lg = dot(lin, LUMA);
        float greenness = saturate((lin.g - max(lin.r, lin.b)) / max(lin.g, 1e-3) * 3.0) * (IsSky(d) ? 0.0 : 1.0);
        lin.r += GREEN_WARM * greenness * lin.g * 0.18;
        lin.b *= 1.0 - GREEN_WARM * greenness * 0.35;
        lin = lerp(lin, float3(lg, lg, lg), GREEN_DESAT * greenness);
        float wS = 1.0 - smoothstep(0.02, 0.25, lg);
        float wH = smoothstep(0.45, 1.0, lg);
        lin *= lerp(float3(1, 1, 1), float3(0.92, 0.99, 1.10), SHADOW_COOL * wS);
        lin *= lerp(float3(1, 1, 1), float3(1.06, 1.0, 0.91), HIGHLIGHT_WARM * wH);
        lin += SHADOW_LIFT * 0.02 * (1.0 - smoothstep(0.0, 0.18, lg)) * lerp(float3(1, 1, 1), lin / max(lg, 1e-3), 0.5);
    }
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
    g = lerp(g, g * g * (3.0 - 2.0 * g), CONTRAST + (IsSky(d) ? 0.0 : TERRAIN_CONTRAST));
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
    if (LUT_STRENGTH > 0.0)   // 3D colour grading (display-referred .cube), blended with the ungraded image
    {
        float ln = max(LUT_SIZE, 2.0);
        float3 uvw = saturate(outc) * ((ln - 1.0) / ln) + 0.5 / ln;
        outc = lerp(outc, g_lut.SampleLevel(g_lin, uvw, 0).rgb, LUT_STRENGTH * (CockpitPx(p) ? LUT_COCKPIT : 1.0));
    }
    float n = (IGN(i.pos.xy, g_misc.y) + IGN(i.pos.xy, g_misc.y + 17.0) - 1.0) * (DITHER / 255.0);
    {
        float lo2 = dot(outc, LUMA);
        n += (IGN(i.pos.xy, g_misc.y * 1.7 + 31.0) + IGN(i.pos.xy, g_misc.y * 0.9 + 53.0) - 1.0) * GRAIN * 0.05 * (0.25 + 4.0 * lo2 * (1.0 - lo2));
    }
    return float4(outc + n, 1);
}
