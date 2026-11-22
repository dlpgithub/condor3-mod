//--------------------------------------------------------------------------------------
// File: EffectBillboards.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbChangesEveryFrame
{
  float3 EyePos;
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

struct VS_INPUT
{
    float4 Pos : POSITION;
    float4 Color : COLOR;
	float Size: SIZE;
};

struct VS_OUTPUT
{
    float3 Center : POSITION;
    float4 Color : COLOR;
	float Size: SIZE;
};

struct GeoOut
{
    float4 PosH      : SV_POSITION;
    float2 Tex       : TEXCOORD;
	float4 Color     : COLOR;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input )
{
    // Position
    VS_OUTPUT output;
    
	output.Center = input.Pos;
	output.Color = input.Color;
	output.Size = input.Size;

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
    float4 v[4];
    v[0] = float4(gin[0].Center + gin[0].Size*right + gin[0].Size*viewup, 1.0f);
    v[1] = float4(gin[0].Center - gin[0].Size*right + gin[0].Size*viewup, 1.0f);
    v[2] = float4(gin[0].Center + gin[0].Size*right - gin[0].Size*viewup, 1.0f);
    v[3] = float4(gin[0].Center - gin[0].Size*right - gin[0].Size*viewup, 1.0f);

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
		gout.Color      = gin[0].Color;
        triStream.Append(gout);
    }
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS(GeoOut pin) : SV_Target
{
   float4 texColor = txDiffuse.Sample( samLinear, pin.Tex );
   float4 color = texColor * pin.Color;
   clip( color.a - 0.01f );
   return color;
}

DepthStencilState DepthDisable 
{
    DepthEnable = false;
    DepthWriteMask = ZERO;
};

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( CompileShader( gs_4_0, GS() ) );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );

        // SetDepthStencilState(DepthDisable, 0); 
    }
}