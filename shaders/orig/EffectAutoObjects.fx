//--------------------------------------------------------------------------------------
// File: EffectAutoObjects.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txCloudShadow;
Texture2D txDiffuseShadow;

SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
	MipLODBias = 0;
};

SamplerComparisonState cmpSampler
{
   // sampler state
   Filter = COMPARISON_MIN_MAG_MIP_LINEAR;
   AddressU = CLAMP;
   AddressV = CLAMP;
 
   // sampler comparison state
   ComparisonFunc = GREATER_EQUAL;
};

cbuffer cbChangesOnlyOnce
{
  float VisibilityFactor;
  float ShadowMapFactor;
  bool bGS;
  bool RealtimeShading;
}

cbuffer cbChangesEveryFrame
{
  matrix View;
  matrix Projection;
  matrix ViewProjLight;
  float3 SunPos;
  float4 SunColor; 
  float3 CameraPos;
  float4 FogColor;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
};

struct VS_OUTPUT
{
    float3 Pos : POSITION;
	float4 Normal : NORMAL;
    float Random: float;
    float4 SkyColor: TEXCOORD1;
	float fogFactor: FOGFACTOR;
};

struct GeoOut
{
    float4 PosH  : SV_POSITION;
    float4 Normal : NORMAL;
	float2 Tex   : TEXCOORD;
	float4 lpos : TEXCOORD3;
    float4 SkyColor: TEXCOORD1;
	float fogFactor: FOGFACTOR;
	float Random: RANDOM;
};

struct VS_OUTPUT_SHADOWMAP
{
    float3 Pos : POSITION;
    float Random: float;
};

struct GeoOut_SHADOWMAP
{
    float4 PosH  : SV_POSITION;
};

cbuffer cbFixed
{
  float2 gSpruceTex[6] = 
  {
     float2(0.837805, -0.05),
     float2(0.684104, 0.499016),
     float2(0.991516, 0.499014),
     float2(0.837805, -0.05),
     float2(0.684104, 0.499016),
     float2(0.991516, 0.499014)
  };
  float3 gSprucePos[6] = 
  {
     float3(0.0, 0.0, 15.0),
     float3(-6.0, 0.0, -0.25),
     float3(6.0, 0.0, -0.25),
     float3(0.0, 0.0, 15.0),
     float3(0.0, -6.0, -0.25),
     float3(0.0, 6.0, -0.25)
  };
  float2 gBeechTex[6] = 
  {
     float2(0.210938, 1.888033),
     float2(0.441318, 0.496536),
     float2(-0.020068, 0.496541),
     float2(0.210938, 1.888033),
     float2(0.441318, 0.496536),
     float2(-0.020068, 0.496541)
  };
  float3 gBeechPos[6] = 
  {
     float3(0.0, 0.0, -27.0),
     float3(7.0, 0.0, 15.0),
     float3(-7.0, 0.0, 15.0),
     float3(0.0, 0.0, -27.0),
     float3(0.0, 7.0, 15.0),
     float3(0.0, -7.0, 15.0)
  };
};

float getShadowFactor(float4 lpos)
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
 
      //perform PCF filtering on a 4 x 4 texel neighborhood
      float sum = 0;
      float x, y;
	  float fScale = 1.0f/2048;
      for (y = -1.5; y <= 1.5; y += 1.0)
      {
          for (x = -1.5; x <= 1.5; x += 1.0)
          {
              sum += txDiffuseShadow.SampleCmpLevelZero( cmpSampler, lpos.xy + float2(x * fScale, y * fScale), lpos.z );
          }
      }
      return sum / 16;	
	}	
 }

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input )
{
    VS_OUTPUT output;
  
  	// Adjust color based on cloud shadows
    float2 ShadowTexPos = float2(((input.Pos.x * ShadowMapFactor) + 1) * 0.5, ((input.Pos.y * ShadowMapFactor) + 1) * 0.5);
	float CloudShadow = txCloudShadow.SampleLevel(samLinear, ShadowTexPos, 0).x;	
	float3 Normal = float3(2.5 * (frac(input.Pos.x) - 0.5), 2.5 * (frac(input.Pos.y) - 0.5), 1.125 * frac(input.Pos.z)); // Terrain normal is encoded as frac of Pos
	
	output.Pos = floor(input.Pos.xyz);
    output.Normal = float4(Normal, CloudShadow);
    output.Random = input.Pos.w;
	
    // Calculate fog factor for exponential fog
    float3 LookVec = output.Pos.xyz - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
	output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);

    // Sky Color
	LookVec = normalize(LookVec);
	float fHeight = max(LookVec.z, 0);
	
    float Red = FogColor.x / (3 * fHeight + 1);
    float Green = FogColor.y / (2 * fHeight + 1);
    float Blue = FogColor.z / (0.2 * fHeight + 1);
	  
    float dAngle = max(1 - dot(SunPos, LookVec), 0);
	float dAngleFactor = exp(-20 * (0.93 * fHeight + 0.07) * dAngle);
  
    output.SkyColor.r = Red + dAngleFactor * (SunColor.x - Red);
    output.SkyColor.g = Green + dAngleFactor * (SunColor.y - Green);
    output.SkyColor.b = Blue + dAngleFactor * (SunColor.z - Blue);
	output.SkyColor.a = saturate(1 - fDistanceSqr * VisibilityFactor); // Use Skycolor.a to pass alpha based on distance
	
    return output;
}

float2 RotateAndScale(float2 Vector, float fSin, float fCos, float fScale)
{
  
  float2 output;
  output.x = fScale * (Vector.x * fCos - Vector.y * fSin);
  output.y = fScale * (Vector.x * fSin + Vector.y * fCos);
  
  return output;
}

//--------------------------------------------------------------------------------------
// Geometry Shader
//--------------------------------------------------------------------------------------
[maxvertexcount(6)]
void GS(point VS_OUTPUT gin[1], 
        uint primID : SV_PrimitiveID, 
        inout TriangleStream<GeoOut> triStream)
{         
	// Compute triangles
	float3 Pos = gin[0].Pos;
	float4 v[6];
	float Rotation = abs(gin[0].Random) * 6.29;
	float fSin = sin(Rotation);
    float fCos = cos(Rotation);
  
	if (gin[0].Random > 0) // Spruce
	{ 
   	  float fScaleXY = 0.7 + 0.6 * frac(374.2734 * abs(gin[0].Random)); // Random aspect ratio
	  float fScaleZ = 0.7 + 0.8 * frac(136.3213 * abs(gin[0].Random)); // Random size
	  v[0] = float4(Pos + gSprucePos[0] * fScaleZ, 1);
	  v[1] = float4(Pos + float3(RotateAndScale(gSprucePos[1].xy, fSin, fCos, fScaleXY), gSprucePos[1].z), 1);
	  v[2] = float4(Pos + float3(RotateAndScale(gSprucePos[2].xy, fSin, fCos, fScaleXY), gSprucePos[2].z), 1);
	  v[3] = v[0];
	  v[4] = float4(Pos + float3(RotateAndScale(gSprucePos[4].xy, fSin, fCos, fScaleXY), gSprucePos[4].z), 1);
	  v[5] = float4(Pos + float3(RotateAndScale(gSprucePos[5].xy, fSin, fCos, fScaleXY), gSprucePos[5].z), 1);
	}
	else // Beech (we use a negative Pos.z value as flag for beech)
	{ 
	  float fScaleXY = 0.7 + 0.6 * frac(374.2734 * abs(gin[0].Random)); // Random size
	  float fScaleZ = 0.7 * fScaleXY; // Random size
	  v[0] = float4(Pos + gBeechPos[0] * fScaleZ, 1);
	  v[1] = float4(Pos + float3(RotateAndScale(gBeechPos[1].xy, fSin, fCos, fScaleXY), gBeechPos[1].z * fScaleZ), 1);
	  v[2] = float4(Pos + float3(RotateAndScale(gBeechPos[2].xy, fSin, fCos, fScaleXY), gBeechPos[2].z * fScaleZ), 1);
	  v[3] = v[0];
	  v[4] = float4(Pos + float3(RotateAndScale(gBeechPos[4].xy, fSin, fCos, fScaleXY), gBeechPos[4].z * fScaleZ), 1);
	  v[5] = float4(Pos + float3(RotateAndScale(gBeechPos[5].xy, fSin, fCos, fScaleXY), gBeechPos[5].z * fScaleZ), 1);
	};
	
	// Transform quad vertices to world space and output them as a triangles
	GeoOut gout;
	[unroll]
	for (int i = 0; i < 6; ++i)
	{
		gout.PosH     = mul(v[i], View);
		gout.PosH     = mul(gout.PosH, Projection);
		if (gin[0].Random > 0)
		  {gout.Tex   = gSpruceTex[i];}
		else
		  {gout.Tex   = gBeechTex[i];}
		gout.Normal   = gin[0].Normal;
		gout.lpos     = mul(v[i], ViewProjLight);
        gout.SkyColor = gin[0].SkyColor;
		gout.fogFactor = gin[0].fogFactor;
		gout.Random = abs(gin[0].Random);
		
		triStream.Append(gout);
		if (i == 2) 
		  {triStream.RestartStrip();};
	}
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( GeoOut input) : SV_Target
{
    // Sample the texture pixel at this location.
	float4 TexColor = txDiffuse.Sample(samLinear, input.Tex);
	
	// Clip transparent regions
    clip(TexColor.a - 0.1f); 
	
	// GS
    if (bGS == true)
	{
	  float Lum = (TexColor.r + TexColor.g + TexColor.b) * 0.33333333;
	  TexColor.rgb = Lum;
	}

    // SHADOW MAPPING
    float shadowFactor = getShadowFactor(input.lpos);   

	// Shading
	float sundot = dot(SunPos, input.Normal.xyz);
	
	// Sample cloud shadow texture	
	float CloudShadow = input.Normal.a;
	
	// Combined shadow
	float TotalShadow = 0.6 + 0.4 * min(CloudShadow, shadowFactor);
		
	float SunExposure = saturate(0.0 + 1 * TotalShadow * (0.2 + 0.8 * sundot)); 

    // Calculate the final color 
	float4 FinalColor = saturate(SunExposure * SunColor * TexColor * (0.8 + 0.3 * input.Random));
	FinalColor.a = input.SkyColor.a;
	
	// Apply fog
	return lerp(input.SkyColor, FinalColor, input.fogFactor);
}

//--------------------------------------------------------------------------------------

BlendState SrcAlphaBlendingAdd
{
    BlendEnable[0] = TRUE;
    SrcBlend = SRC_ALPHA;
    DestBlend = INV_SRC_ALPHA;
    BlendOp = ADD;
    SrcBlendAlpha = ZERO;
    DestBlendAlpha = ZERO;
    BlendOpAlpha = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( CompileShader( gs_4_0, GS() ) );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );

        SetBlendState( SrcAlphaBlendingAdd, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}