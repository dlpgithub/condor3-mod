//--------------------------------------------------------------------------------------
// File: EffectLand.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txDiffuseShadow;
Texture2D txDiffuseShadowAvion;
Texture2D txDiffuseShadowTowplane;
Texture2D txDetail;
Texture2D txCloudShadow;
Texture2D txWaterBumpMap;

SamplerState samLinearMirror
{
    Filter = ANISOTROPIC;
	AddressU = Mirror;
    AddressV = Mirror;	
};

SamplerState samLinearWrap
{
    Filter = ANISOTROPIC;
    AddressU = Wrap;
    AddressV = Wrap;
};

SamplerState samLinearBorder
{
    Filter = ANISOTROPIC;
    AddressU = Border;
    AddressV = Border;
	BorderColor = float4(1, 1, 1, 0);
};

SamplerComparisonState cmpSampler
{
   // sampler state
   Filter = COMPARISON_MIN_MAG_MIP_LINEAR;
   AddressU = CLAMP;
   AddressV = CLAMP;
 
   // sampler comparison state
   ComparisonFunc = GREATER; // LESS_EQUAL; // Use GREATER for (1 - Z/W) depth buffer
};

cbuffer cbChangesOnlyOnce
{
  float NormalOffset;
  float NormalOffsetAvion;
  bool GS;
  bool RealtimeShading;
}

cbuffer cbChangesOccasionally
{
  bool MapView;
}

cbuffer cbChangesEveryFrame
{
  matrix View;
  matrix Projection;
  matrix ViewProjLight;
  matrix ViewProjLightAvion;
  matrix ViewProjLightTowplane;
  float3 CameraPos;
  float4 FogColor;
  float3 SunPos;
  float4 SunColor;
  float4 AmbientColor;
  float Time;
}

cbuffer cbChangesPerTexture
{
  bool MirrorX;
  bool MirrorY;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
	float3 Norm : NORMAL;
    float2 Tex: TEXCOORD;
	float2 Tex1: TEXCOORD_SHADOW;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION;
	float3 Norm: NORMAL;
	float3 WorldPos: POSITION;
    float2 Tex: TEXCOORD0;
	float2 Tex1: TEXCOORD1;
    float4 SkyColor: TEXCOORD2;
	float fogFactor: FOGFACTOR;
	float4 lpos : TEXCOORD3; // https://takinginitiative.wordpress.com/2011/05/15/directx10-tutorial-10-shadow-mapping/	
    float4 lposAvion : TEXCOORD4;
    float4 lposTowplane : TEXCOORD5;
};

struct PS_INPUT_SHADOW
{
    float4 Pos : SV_POSITION;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS( VS_INPUT input )
{
    PS_INPUT output = (PS_INPUT)0;
    
	output.WorldPos = input.Pos;
    output.Pos = mul( input.Pos, View );
    output.Pos = mul( output.Pos, Projection );

	output.Tex = input.Tex;
	output.Tex1 = input.Tex1;
	
	// Cross border
    if (MirrorX == true)
	{
	  output.Tex.x = 1 - input.Tex.x;
	}
    if (MirrorY == true)
	{
      output.Tex.y = 1 - input.Tex.y;
	}
	
    // Calculate fog factor for exponential fog
    float3 LookVec = input.Pos.xyz - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
	if (MapView == true)
	  { output.fogFactor = 1; }
	else
	  { output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1); }
	
    // Sky Color
	LookVec = normalize(input.Pos.xyz - CameraPos);
	float fHeight = max(LookVec.z, 0);
	
	float cFadeFactor = 1;
    float dGradientFactorRed = 1 / (4 * cFadeFactor * fHeight + 1);
    float dGradientFactorGreen = 1 / (1.8 * cFadeFactor * fHeight + 1);
    float dGradientFactorBlue = 1 / (0.5 * cFadeFactor * fHeight + 1);
	
    float Red = FogColor.x * dGradientFactorRed;
    float Green = FogColor.y * dGradientFactorGreen;
    float Blue = FogColor.z * dGradientFactorBlue;
	  
    float dAngle = max(acos(dot(SunPos, LookVec)), 0);
	float dAngleZ = max(LookVec.z - SunPos.z, 0);
	
	float dAngleFactor = exp(-2 * (0.1 + 0.9 * dAngle) * (0.5 + 0.5 * SunPos.z) - 1 * dAngleZ * (1 - sqrt(max(SunPos.z, 0))));
  
    output.SkyColor.x = Red + dAngleFactor * (SunColor.x - Red);
    output.SkyColor.y = Green + dAngleFactor * (SunColor.y - Green);
    output.SkyColor.z = Blue + dAngleFactor * (SunColor.z - Blue);
		
	// Normal
	output.Norm = input.Norm;
	
	// Self shadowing
    if (RealtimeShading == true) 
	{
	  output.lpos = input.Pos;
      output.lpos.xyz += NormalOffset * input.Norm;
	  output.lpos = mul( output.lpos, ViewProjLight );
	}
    
    // Avion shadow
    output.lposAvion = input.Pos;
    output.lposAvion.xyz += NormalOffsetAvion * input.Norm;
    output.lposAvion = mul( output.lposAvion, ViewProjLightAvion );   
    
    // Towplane shadow
    output.lposTowplane = input.Pos;
    output.lposTowplane.xyz += NormalOffsetAvion * input.Norm;
    output.lposTowplane = mul( output.lposTowplane, ViewProjLightTowplane );     
    
	return output;
}

float getShadowFactor(float4 lpos, Texture2D txShadowMap, bool PCF)
{
	// re-homogenize position after interpolation
    lpos.xyz /= lpos.w;
 
    // if position is outside shadow volume - don't shadow it
	if( lpos.x < -1.0f || lpos.x > 1.0f ||
        lpos.y < -1.0f || lpos.y > 1.0f ||
        lpos.z < 0.0f  || lpos.z > 1.0f ) 
	{
	  return 1;
	}
	else
	{
      //transform clip space coords to texture space coords (-1:1 to 0:1)
      lpos.x = 0.5 * lpos.x + 0.5;
      lpos.y = -0.5 * lpos.y + 0.5;
 
      // Basic hardware PCF - single texel
      if (PCF == false)
      {
        return txShadowMap.SampleCmpLevelZero( cmpSampler, lpos.xy, lpos.z );
      }
      else
      {     
        // perform PCF filtering on a 4 x 4 texel neighborhood
        float sum = 0;
        float x, y;
        float fScale = 1.0f/2048;
        for (y = -3; y <= 3; y += 2.0)
        {
          for (x = -3; x <= 3; x += 2.0)
          {
            sum += txShadowMap.SampleCmpLevelZero( cmpSampler, lpos.xy + float2(x * fScale, y * fScale), lpos.z );
          }
        }
        return sum / 16;	 
      }
	}	
 }

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input) : SV_Target
{
	// SELF SHADOW MAPPING
	float shadowFactor;
    if (RealtimeShading == true) 
	  { shadowFactor = getShadowFactor(input.lpos, txDiffuseShadow, false);	}
	else
	  { shadowFactor = 1; }
	
    // AVION SHADOW MAPPING
    float shadowFactorAvion = getShadowFactor(input.lposAvion, txDiffuseShadowAvion, true);
    
    // TOWPLANE SHADOW MAPPING
    float shadowFactorTowplane = getShadowFactor(input.lposTowplane, txDiffuseShadowTowplane, true);    
    
    // Sample the texture pixel at this location.  
	float4 TextureColor = txDiffuse.Sample( samLinearMirror, input.Tex );
	
	// GS
    if (GS == true)
	{
	  float Lum = (TextureColor.r + TextureColor.g + TextureColor.b) * 0.33333333;
	  TextureColor.rgb = Lum;
	}
	
	float4 DetailColor;
	float2 TexDetail;
	
	// Water
	if (TextureColor.a < 0.8)
	{
	  float2 TexWaterBumpMap1 = float2(89 * Time + input.Tex.x * 150, 63 * Time + input.Tex.y * 150);
	  float2 TexWaterBumpMap2 = float2(-69 * Time + input.Tex.x * 150, 77 * Time + input.Tex.y * 150);
	  float3 WaterBumpMapColor1 = txWaterBumpMap.Sample( samLinearWrap, TexWaterBumpMap1 );
	  float3 WaterBumpMapColor2 = txWaterBumpMap.Sample( samLinearWrap, TexWaterBumpMap2 );
	  float3 WaterBumpMapColor = 0.5 * WaterBumpMapColor1 + 0.5 * WaterBumpMapColor2;
	  WaterBumpMapColor = normalize((WaterBumpMapColor * 2.0f) - 1.0f);
	  	  
      // Specular lighting
	  float3 V = normalize(CameraPos - input.WorldPos);
      float3 Rs = reflect(normalize(-SunPos), WaterBumpMapColor);
	  float3 RSky = normalize(float3(0.3 * WaterBumpMapColor.x, 0.3 * WaterBumpMapColor.y, 1));
	  TextureColor += (1 - TextureColor.a) * ((0.7 * pow( saturate(dot(Rs, V)), 16)) + 0.5 * pow(1 - dot(RSky, V), 16));
	  
	  TexDetail = float2(input.Tex.x * 100, input.Tex.y * 100);
	  DetailColor = txDetail.Sample( samLinearWrap, TexDetail ) + 0.1;
	}
	else
	{
	  TexDetail = float2(input.Tex.x * 30, input.Tex.y * 30);
	  DetailColor = txDetail.Sample( samLinearWrap, TexDetail );
	}
	
	TextureColor = TextureColor * (DetailColor + 0.5);

	// Shading
	float sundot;
	if (RealtimeShading == true)
	{
	  sundot = dot(SunPos, input.Norm);
	}
	else
	{
	  sundot = dot(SunPos, float3(0, 0, 1));
	}
	
	// Sample cloud shadow texture	
	float CloudShadow = txCloudShadow.Sample( samLinearBorder, input.Tex1 ).x;
	
	// Combined shadow
	float TotalShadow = 0.6 + 0.4 * min(min(min(CloudShadow, shadowFactor), shadowFactorAvion), shadowFactorTowplane);
		
    // Diffuse lighting
    float4 LightingColor = saturate(SunColor * sundot + AmbientColor);
	
    // Calculate the final color using the fog effect equation.
	return lerp(input.SkyColor, saturate(LightingColor * TotalShadow * TextureColor), input.fogFactor);
}

// ****************************************************************
// VERTEX AND (VOID) PIXEL SHADER FOR SHADOW MAP GENERATION
// ****************************************************************

PS_INPUT_SHADOW VS_ShadowMap( VS_INPUT input )
{
    PS_INPUT_SHADOW output;
    
    output.Pos = mul( input.Pos, ViewProjLight );
	
    return output;
}

void PS_Void( PS_INPUT_SHADOW input) 
{
}

//--------------------------------------------------------------------------------------
BlendState DisableBlending
{
    BlendEnable[0] = FALSE;
};

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );

        SetBlendState( DisableBlending, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}

technique10 Render_ShadowMap
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}