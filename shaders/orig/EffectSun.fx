//--------------------------------------------------------------------------------------
// File: EffectSun.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txSunOcclusionDiffuse;
Texture2D txSunOcclusionInteriorDiffuse;
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbChangesEveryFrame
{
  float3 EyePos;
  float3 SunPos;
  float SunSize;
  float4 SunColor;
  float Angle;
  matrix View;
  matrix Projection;
}

cbuffer cbFixed
{
  float2 gTexC[4] = 
  {
     float2(1.0f, 1.0f),
     float2(0.0f, 1.0f),
     float2(1.0f, 0.0f),
     float2(0.0f, 0.0f)
  };
};

struct VS_OUTPUT
{
    float3 Center : POSITION;
    float Occlusion: OCCLUSION;
};

struct GeoOut
{
    float4 PosH      : SV_POSITION;
    float2 Tex       : TEXCOORD;
	float Occlusion  : OCCLUSION;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( void )
{
    // Position
    VS_OUTPUT output;
    output.Center = SunPos;

	// The 4th mipmap is size 1x1 and is thus the average of the 16x16 texture with depth buffer values
    output.Occlusion = txSunOcclusionDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r * 
	                   txSunOcclusionInteriorDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r;
	
    return output;
}

//--------------------------------------------------------------------------------------
// Geometry Shader
//--------------------------------------------------------------------------------------
[maxvertexcount(4)]
void GS(point VS_OUTPUT gin[1], 
        uint primID : SV_PrimitiveID, 
        inout TriangleStream<GeoOut> triStream)
{    
    //
    // Compute the local coordinate system of the sprite relative to the world
    // space such that the billboard is aligned with the y-axis and faces the eye.
    //

    float3 up = float3(0.0f, 0.0f, 1.0f);
    float3 look = gin[0].Center - EyePos;
    // look.z = 0.0f; // z-axis aligned, so project to xy-plane
    float3 right = normalize(cross(look, up));
    float3 viewup = normalize(cross(right, look));

    //
    // Compute triangle strip vertices (quad) in world space.
    //
	float Size = SunSize * gin[0].Occlusion;
    float4 v[4];
    v[0] = float4(gin[0].Center + Size * right + Size * viewup, 1.0f);
    v[1] = float4(gin[0].Center - Size * right + Size * viewup, 1.0f);
    v[2] = float4(gin[0].Center + Size * right - Size * viewup, 1.0f);
    v[3] = float4(gin[0].Center - Size * right - Size * viewup, 1.0f);

    //
    // Transform quad vertices to world space and output 
    // them as a triangle strip.
    //
    GeoOut gout;
    [unroll]
    for(int i = 0; i < 4; ++i)
    {
        gout.PosH       = mul(v[i], View);
        gout.PosH       = mul(gout.PosH, Projection);
        gout.Tex        = gTexC[i];
		gout.Occlusion  = gin[0].Occlusion;
        triStream.Append(gout);
    }
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS(GeoOut pin) : SV_Target
{  
   // return txDiffuse.Sample( samLinear, pin.Tex ) * SunColor;
   
   float alpha = 1.5 * txDiffuse.Sample( samLinear, pin.Tex ).a;// * exp(-1 * Angle);
   clip(alpha - 0.01);
   
   // Add some contrast
   // return saturate(float4(1 + alpha, 1 + alpha, 1 + alpha, alpha) * SunColor);
   return saturate(float4(1 + alpha, 1 + alpha, 1 + alpha, alpha * pin.Occlusion) * SunColor);
}

//--------------------------------------------------------------------------------------
BlendState SrcAlphaBlending
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

        SetBlendState( SrcAlphaBlending, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}