// ============================================================================
// Condor 3 shader mod - shared helpers (inserted by build_shaders.py)
// ============================================================================
#define MOD_SATURATION   @saturation@
#define MOD_CURVE        @curve@
#define MOD_DITHER       @dither@
#define MOD_MACRO_DETAIL @macro_detail@
#define MOD_WATER_GLINT  @water_glint@

// Gentle colour grade: saturation + a soft S-curve for contrast.
// Applied to everything that ends up on screen *together* (terrain, sky,
// clouds, trees, aircraft) so haze and fog colours keep matching each other.
float3 ModGrade(float3 c)
{
    c = saturate(c);
    float l = dot(c, float3(0.299, 0.587, 0.114));
    c = saturate(lerp(float3(l, l, l), c, MOD_SATURATION));
    float3 s = c * c * (3.0 - 2.0 * c);
    return lerp(c, s, MOD_CURVE);
}

float4 ModGradeA(float4 c)
{
    return float4(ModGrade(c.rgb), c.a);
}

// Interleaved-gradient noise, +-0.5 LSB. Removes banding in sky gradients.
float ModDither(float2 p)
{
    return (frac(52.9829189 * frac(dot(p, float2(0.06711056, 0.00583715)))) - 0.5) * (MOD_DITHER / 255.0);
}

// The game's own sky / haze colour formula (identical maths to the original),
// but usable per pixel. Also guards acos() against |x| > 1 rounding.
float3 ModSkyColor(float3 look, float3 fogColor, float3 sunColor, float3 sunPos)
{
    float h = max(look.z, 0);
    float3 baseColor = float3(fogColor.x / (4.0 * h + 1.0),
                              fogColor.y / (1.8 * h + 1.0),
                              fogColor.z / (0.5 * h + 1.0));
    float ang  = acos(clamp(dot(sunPos, look), -1.0, 1.0));
    float angZ = max(look.z - sunPos.z, 0);
    float f = exp(-2.0 * (0.1 + 0.9 * ang) * (0.5 + 0.5 * sunPos.z) - angZ * (1.0 - sqrt(max(sunPos.z, 0))));
    return baseColor + f * (sunColor - baseColor);
}

