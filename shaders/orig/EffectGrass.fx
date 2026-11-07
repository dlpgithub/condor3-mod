//--------------------------------------------------------------------------------------
// File: EffectAutoObjects.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txDiffuseGround;
Texture2D txCloudShadow;
Texture2D txDiffuseShadowAvion;
Texture2D txDiffuseShadowTowplane;

SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = CLAMP;
    AddressV = CLAMP;
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

// Max unsigned integer number - needed for pseudo random generator
float fUINT_MAX = float(0xffffffff);

// Converting a color to grayscale coefficients for reg, green, blue
float3 coef = {0.3, 0.59, 0.11};

cbuffer cbChangesOnlyOnce
{
    float ScaleX;
    float ScaleY;  
    float OffsetX;
    float OffsetY;
    float ShadowMapFactor;
}

cbuffer cbChangesEveryFrame
{
    matrix View;
    matrix Projection;
    float3 AirportLocalPos;
    float3 CameraLocalPos;
    matrix ViewProjLightAvion;
    matrix ViewProjLightTowplane;
    float3 SunPos;
    float4 SunColor;     
    float4 AmbientColor;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
};

struct VS_OUTPUT
{
    float3 Pos : POSITION;
    float Random: RANDOM;
    float4 Color: COLOR;
    float Alpha: ALPHA;
    float CloudShadow: CLOUDSHADOW;
};

struct GeoOut
{
    float4 PosH      : SV_POSITION;
	float2 Tex       : TEXCOORD;
    float4 Color     : COLOR;
    float4 lpos      : TEXCOORD1;
    float4 lpos1     : TEXCOORD2;
    float CloudShadow: CLOUDSHADOW;
};

cbuffer cbFixed
{ 
  int iNumTextures = 6;
   
  float3 gSprucePos[6] = 
  {
     float3(-2, 2, 1.5),
     float3(-2, 0, 0),
     float3( 2, 0, 0),
     
     float3( 2, 0, 0),
     float3( 2, 1, 1.5),
     float3(-2, 2, 1.5)
  };  
  
  float2 gSpruceTex[6] = 
  {
     float2(0, 0),
     float2(0, 1),
     float2(1, 1),
     float2(1, 1),
     float2(1, 0),
     float2(0, 0)
  };  
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input )
{
    VS_OUTPUT output;
   
	output.Pos = input.Pos.xyz + float3(AirportLocalPos.xy, 0);   
    output.Random = input.Pos.w;
    output.Color = txDiffuseGround.SampleLevel(samLinear, float2(OffsetX + input.Pos.x * ScaleX, OffsetY + input.Pos.y * ScaleY), 0); // Alpha channel contains grass height 
    output.Color += 0.05 * (input.Pos.w - 0.5);
  
    // Cloud shadows
    float2 ShadowTexPos = float2(((output.Pos.x * ShadowMapFactor) + 1) * 0.5, ((output.Pos.y * ShadowMapFactor) + 1) * 0.5);
	output.CloudShadow = txCloudShadow.SampleLevel(samLinear, ShadowTexPos, 0).x;	  
  
    // Calculate distance to camera
    float3 LookVec = output.Pos.xyz - CameraLocalPos;
	float fDistanceSqr = pow(LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z, 1);
	//output.Alpha = saturate(1 - 0.005 * fDistanceSqr); 
    output.Alpha = saturate(1.01 * exp(-0.0002 * fDistanceSqr) - 0.01);    
  
    return output;
}

float2 RotateAndScale(float2 Vector, float fSin, float fCos, float fScale)
{
  
    float2 output;
    output.x = fScale * (Vector.x * fCos - Vector.y * fSin);
    output.y = fScale * (Vector.x * fSin + Vector.y * fCos);
   
    return output;
}

// https://www.reedbeta.com/blog/quick-and-easy-gpu-random-numbers-in-d3d11/
uint pcg_hash(uint input)
{
    uint state = input * 747796405u + 2891336453u;
    uint word = ((state >> ((state >> 28u) + 4u)) ^ state) * 277803737u;
    return (word >> 22u) ^ word;
}

float Random(inout uint RandSeed)
{
    RandSeed = pcg_hash(RandSeed);
    return float(RandSeed) / fUINT_MAX;
}

//--------------------------------------------------------------------------------------
// Geometry Shader
//--------------------------------------------------------------------------------------
[maxvertexcount(6)]
[instance(20)]
void GS(point VS_OUTPUT gin[1], 
        uint primID : SV_PrimitiveID,
        uint InstanceID : SV_GSInstanceID,
        inout TriangleStream<GeoOut> triStream)
{         
    // Initialize random seed
    uint RandSeed = uint(gin[0].Random * fUINT_MAX) * InstanceID;

	// Compute triangles   
    float Rotation = Random(RandSeed) * 6.28318530718;
	float fSin = sin(Rotation);
    float fCos = cos(Rotation);    
  
    float fScaleXY = 2 * gin[0].Color.a * (0.2 + 0.8 * gin[0].Alpha) * (0.07 + 0.06 * Random(RandSeed)); // Random aspect ratio
	float fScaleZ = 2 * gin[0].Color.a * (0.2 + 0.8 * gin[0].Alpha) * (0.07 + 0.08 * Random(RandSeed)); // Random size  
  
    float BatchSize = 0.4 + 0.2 * Random(RandSeed);
    float3 Pos = gin[0].Pos;
    Pos.x += BatchSize * (Random(RandSeed) - 0.5);
    Pos.y += BatchSize * (Random(RandSeed) - 0.5);
    float4 v[6];
    v[0] = float4(Pos + float3(RotateAndScale(gSprucePos[0].xy, fSin, fCos, fScaleXY), gSprucePos[0].z * fScaleZ), 1);
    v[1] = float4(Pos + float3(RotateAndScale(gSprucePos[1].xy, fSin, fCos, fScaleXY), gSprucePos[1].z * fScaleZ), 1);
    v[2] = float4(Pos + float3(RotateAndScale(gSprucePos[2].xy, fSin, fCos, fScaleXY), gSprucePos[2].z * fScaleZ), 1);
    v[3] = float4(Pos + float3(RotateAndScale(gSprucePos[3].xy, fSin, fCos, fScaleXY), gSprucePos[3].z * fScaleZ), 1);
    v[4] = float4(Pos + float3(RotateAndScale(gSprucePos[4].xy, fSin, fCos, fScaleXY), gSprucePos[4].z * fScaleZ), 1);
    v[5] = float4(Pos + float3(RotateAndScale(gSprucePos[5].xy, fSin, fCos, fScaleXY), gSprucePos[5].z * fScaleZ), 1);
 
    // Pick random texture
    float fTexture = trunc(Random(RandSeed) * iNumTextures);
 
	// Transform quad vertices to world space and output them as a triangles
	GeoOut gout;
	[unroll]
    for (int i = 0; i < 6; ++i)
    {       
        gout.PosH        = mul(v[i], View);
        gout.PosH        = mul(gout.PosH, Projection);
        gout.Tex.x       = (fTexture + gSpruceTex[i].x) / iNumTextures;
        gout.Tex.y       = gSpruceTex[i].y;
        gout.Color       = float4(gin[0].Color.rgb, gin[0].Alpha);
        gout.lpos        = mul(v[i], ViewProjLightAvion);
        gout.lpos1       = mul(v[i], ViewProjLightTowplane);
        gout.CloudShadow = gin[0].CloudShadow;
        
        triStream.Append(gout);
        if (i == 2) 
          {triStream.RestartStrip();};
    }
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
float4 PS( GeoOut input) : SV_Target
{
    // SHADOW MAPPING
    float shadowFactor = min(getShadowFactor(input.lpos, txDiffuseShadowAvion, false), getShadowFactor(input.lpos1, txDiffuseShadowTowplane, false)); 

	// Combined shadow
	float TotalShadow = 0.6 + 0.4 * min(input.CloudShadow, shadowFactor);  
    
    // Sun lighting
	float sundot = SunPos.z;
   	float4 LightingColor = saturate(SunColor * sundot + AmbientColor);    
       
    // Sample the texture pixel at this location.
	float4 TexColor = txDiffuse.Sample(samLinear, input.Tex);        
    TexColor.rgb = lerp(TexColor.rgb, dot(coef.rgb, TexColor.rgb), saturate(4 * (1 - input.Color.a))); // Desaturate (1 = full desaturation)
    
    // Combine grass texture color with the ground texture color
    float4 OutputColor = float4(input.Color.rgb * (TexColor.rgb + 0.5) * LightingColor * TotalShadow, TexColor.a);
    
    // Clip transparent regions
    clip(OutputColor.a - 0.5f); 
	
	return OutputColor;
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
        SetVertexShader( CompileShader( vs_5_0, VS() ) );
        SetGeometryShader( CompileShader( gs_5_0, GS() ) );
        SetPixelShader( CompileShader( ps_5_0, PS() ) );

        SetBlendState( SrcAlphaBlendingAdd, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}