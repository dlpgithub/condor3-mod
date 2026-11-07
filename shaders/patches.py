"""
Shader patches for Condor 3.

Every patch is an exact text replacement against the game's own HLSL (decoded
from Effects/*.fxc). If Condor ever ships a different shader, the replacement
count check fails loudly instead of silently producing a broken effect.

Interfaces that the game binds by name (cbuffer variables, textures, techniques)
are never renamed or removed, only the shader *bodies* change.
"""
import re


class PatchError(Exception):
    pass


def rep(src, old, new, count=1, what=""):
    n = src.count(old)
    if n != count:
        raise PatchError("%s: expected %d match(es) of %r, found %d"
                         % (what or "patch", count, old[:70], n))
    return src.replace(old, new)


def rep_re(src, pattern, repl, count=1, what="", flags=re.S):
    found = len(re.findall(pattern, src, flags))
    if found != count:
        raise PatchError("%s: expected %d regex match(es) of %r, found %d"
                         % (what or "patch", count, pattern[:70], found))
    return re.sub(pattern, repl, src, flags=flags)


def wrap_returns(src, pattern, count, what):
    """return <expr>;  ->  return ModGradeA(<expr>);   for every match."""
    return rep_re(src, pattern, lambda m: "return ModGradeA(%s);" % m.group(1),
                  count=count, what=what)


# --------------------------------------------------------------------------
# Terrain
# --------------------------------------------------------------------------
def patch_land(src, o):
    if o["anisotropy"] > 1:
        src = rep(src, "Filter = ANISOTROPIC;",
                  "Filter = ANISOTROPIC;\n    MaxAnisotropy = %d;" % o["anisotropy"],
                  count=3, what="Land: anisotropy")

    if o["macro_detail"] > 0:
        # The detail texture repeats 30x per tile, which shows as a visible grid
        # from altitude. Add a second, low-frequency pass at an irrational scale.
        old = "TextureColor = TextureColor * (DetailColor + 0.5);"
        new = (old + "\n"
               "\t// [MOD] low-frequency detail pass: breaks up visible tiling\n"
               "\tTextureColor.rgb *= lerp(1.0, txDetail.Sample( samLinearWrap, TexDetail * 0.137 + float2(0.37, 0.61) ).r + 0.5, MOD_MACRO_DETAIL);")
        src = rep(src, old, new, what="Land: macro detail")

    if o["water_glint"] > 0:
        old = ("TextureColor += (1 - TextureColor.a) * ((0.7 * pow( saturate(dot(Rs, V)), 16)) "
               "+ 0.5 * pow(1 - dot(RSky, V), 16));")
        new = ("float ModWater = 1 - TextureColor.a;\n"
               "\t  " + old + "\n"
               "\t  // [MOD] sharp, sun-tinted glitter on top of the broad highlight\n"
               "\t  TextureColor.rgb += ModWater * MOD_WATER_GLINT * lerp(float3(1, 1, 1), SunColor.rgb, 0.6) * pow(saturate(dot(Rs, V)), 160);")
        src = rep(src, old, new, what="Land: water glint")

    # [MOD] stronger shadows + warm sun / cool ambient
    src = rep(src, "float TotalShadow = 0.6 + 0.4 * min(min(min(CloudShadow, shadowFactor), shadowFactorAvion), shadowFactorTowplane);",
              "float TotalShadow = (1.0 - MOD_SHADOW) + MOD_SHADOW * min(min(min(CloudShadow, shadowFactor), shadowFactorAvion), shadowFactorTowplane);", what="Land: shadow")
    src = rep(src, "float4 LightingColor = saturate(SunColor * sundot + AmbientColor);",
              "float4 LightingColor = saturate(ModSun(SunColor) * sundot + ModAmb(AmbientColor));", what="Land: light tint")

    old = "return lerp(input.SkyColor, saturate(LightingColor * TotalShadow * TextureColor), input.fogFactor);"
    if o["per_pixel_atmosphere"]:
        new = (
            "// [MOD] per-pixel fog + haze colour (the original interpolates both between\n"
            "\t// the far-apart vertices of big terrain triangles, which looks blotchy)\n"
            "\tfloat3 ModLook = input.WorldPos - CameraPos;\n"
            "\tfloat ModFog = 1.0;\n"
            "\tif (MapView == false)\n"
            "\t  { ModFog = saturate(1.1 * exp(-0.000000001 * dot(ModLook, ModLook)) - 0.1); }\n"
            "\tfloat3 ModSky = ModSkyColor(normalize(ModLook), FogColor.xyz, SunColor.xyz, SunPos);\n"
            "\tfloat4 ModOut = lerp(float4(ModSky, 0), saturate(LightingColor * TotalShadow * TextureColor), ModFog);\n"
            "\tModOut.rgb = ModGrade(ModOut.rgb) + ModDither(input.Pos.xy);\n"
            "\treturn ModOut;")
    else:
        new = "return ModGradeA(%s);" % old[len("return "):-1]
    return rep(src, old, new, what="Land: final colour")


# --------------------------------------------------------------------------
# Sky dome
# --------------------------------------------------------------------------
def patch_sky(src, o):
    old_ret = "return input.Color;"
    if o["per_pixel_atmosphere"]:
        src = rep(src, "float4 Pos : SV_POSITION;",
                  "float4 Pos : SV_POSITION;\n    float3 ModLook : TEXCOORD0;", what="Sky: struct")
        src = rep(src, "float3 LookVec = normalize(input.Pos.xyz - CameraPos);",
                  "float3 LookVec = normalize(input.Pos.xyz - CameraPos);\n"
                  "\toutput.ModLook = LookVec;", what="Sky: VS")
        new = ("// [MOD] per-pixel sky: no vertex interpolation artefacts around the sun, no banding\n"
               "  float3 ModSky = ModSkyColor(normalize(input.ModLook), FogColor.xyz, SunColor.xyz, SunPos);\n"
               "  return float4(ModGrade(ModSky) + ModDither(input.Pos.xy), input.Color.a);")
    else:
        new = "return ModGradeA(input.Color);"
    return rep(src, old_ret, new, what="Sky: PS")


# --------------------------------------------------------------------------
# Trees / forests
# --------------------------------------------------------------------------
def patch_autoobjects(src, o):
    src = rep(src, "float TotalShadow = 0.6 + 0.4 * min(CloudShadow, shadowFactor);",
              "float TotalShadow = (1.0 - MOD_SHADOW) + MOD_SHADOW * min(CloudShadow, shadowFactor);", what="AutoObjects: shadow")
    src = rep(src, "saturate(SunExposure * SunColor * TexColor", "saturate(SunExposure * ModSun(SunColor) * TexColor", what="AutoObjects: tint")
    if o["unify_tree_haze"]:
        # Trees used a different haze formula than terrain and sky, so distant
        # forests faded to a slightly different colour than the ground behind.
        src = rep_re(src,
                     r"float Red = FogColor\.x / \(3 \* fHeight \+ 1\);.*?"
                     r"output\.SkyColor\.b = Blue \+ dAngleFactor \* \(SunColor\.z - Blue\);",
                     lambda m: "// [MOD] same haze as terrain and sky\n"
                               "\toutput.SkyColor.rgb = ModSkyColor(LookVec, FogColor.xyz, SunColor.xyz, SunPos);",
                     what="AutoObjects: haze")
    return rep(src, "return lerp(input.SkyColor, FinalColor, input.fogFactor);",
               "return ModGradeA(lerp(input.SkyColor, FinalColor, input.fogFactor));",
               what="AutoObjects: grade")


# --------------------------------------------------------------------------
# Grass
# --------------------------------------------------------------------------
def patch_grass(src, o):
    src = rep(src, "float TotalShadow = 0.6 + 0.4 * min(input.CloudShadow, shadowFactor);",
              "float TotalShadow = (1.0 - MOD_SHADOW) + MOD_SHADOW * min(input.CloudShadow, shadowFactor);", what="Grass: shadow")
    src = rep(src, "float4 LightingColor = saturate(SunColor * sundot + AmbientColor);",
              "float4 LightingColor = saturate(ModSun(SunColor) * sundot + ModAmb(AmbientColor));", what="Grass: light tint")
    return rep(src, "return OutputColor;", "return ModGradeA(OutputColor);", what="Grass: grade")


# --------------------------------------------------------------------------
# Clouds
# --------------------------------------------------------------------------
def patch_cloud(src, o):
    if o["cloud_linear_filter"]:
        # Cloud puffs are 256x256 sprites sampled with POINT filtering: blocky up
        # close and shimmering far away. Trilinear fixes both.
        src = rep(src, "Filter = MIN_MAG_MIP_POINT;", "Filter = MIN_MAG_MIP_LINEAR;",
                  what="Cloud: filter")
    return wrap_returns(src, r"return (lerp\([^;]*\));", 2, "Cloud: grade")


def patch_middle_clouds(src, o):
    return wrap_returns(src, r"return (lerp\(float4\(input\.SkyColor\.xyz, color\.a\), color, input\.fogFactor\));",
                        2, "MiddleClouds: grade")


def patch_high_clouds(src, o):
    if o["anisotropy"] > 1:
        src = rep(src, "Filter = ANISOTROPIC;",
                  "Filter = ANISOTROPIC;\n    MaxAnisotropy = %d;" % o["anisotropy"],
                  what="HighClouds: anisotropy")
    return wrap_returns(src, r"return (txDiffuse\.Sample\( samLinear, input\.Tex \) \* float4\(SunColor\.xyz, Alpha\));",
                        1, "HighClouds: grade")


# --------------------------------------------------------------------------
# Aircraft, airports, ground objects
# --------------------------------------------------------------------------
def patch_aircraft(src, o):
    if o["anisotropy"] > 1:
        src = rep(src, "Filter = ANISOTROPIC;",
                  "Filter = ANISOTROPIC;\n    MaxAnisotropy = %d;" % o["anisotropy"],
                  count=2, what="Aircraft: anisotropy")
    # PS, PS_SampleShadowMap, PS_Canopy
    src = rep(src, "return saturate(OutputColor);", "return ModGradeA(saturate(OutputColor));",
              count=3, what="Aircraft: grade (aircraft/canopy)")
    # PS_AirportSurface_SampleShadowMap, PS_GroundObject (after their fog lerp)
    src = rep(src, "return OutputColor;", "return ModGradeA(OutputColor);",
              count=2, what="Aircraft: grade (airport/ground objects)")
    return src


# file name (without extension) -> patch function
PATCHES = {
    "EffectLand":         patch_land,
    "EffectSky":          patch_sky,
    "EffectAutoObjects":  patch_autoobjects,
    "EffectGrass":        patch_grass,
    "EffectCloud":        patch_cloud,
    "EffectMiddleClouds": patch_middle_clouds,
    "EffectHighClouds":   patch_high_clouds,
    "EffectDX11File":     patch_aircraft,
}
