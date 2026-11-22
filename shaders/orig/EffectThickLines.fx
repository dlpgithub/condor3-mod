//--------------------------------------------------------------------------------------
// File: EffectThickLines.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbChangesEveryFrame
{
  matrix View;
  matrix Projection;
}

cbuffer cbPerObject
{
  float4 Color;
}


struct VS_INPUT
{
    float3 Pos1 : POSITION0;
	float3 Pos2 : POSITION1;
	float2 Thickness : THICKNESS;
};

struct VS_OUTPUT
{
    float3 Pos1 : POSITION0;
    float3 Pos2 : POSITION1;
	float2 Thickness : THICKNESS;
};

struct GeoOut
{
    float4 PosH      : SV_POSITION;
    float2 Tex       : TEXCOORD;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input )
{
    // Position
    VS_OUTPUT output;
    
	output.Pos1 = input.Pos1;
	output.Pos2 = input.Pos2;
	output.Thickness = input.Thickness;

    return output;
}

//--------------------------------------------------------------------------------------
// Geometry Shader
//--------------------------------------------------------------------------------------
[maxvertexcount(10)]
void GS(point VS_OUTPUT gin[1], 
        uint primID : SV_PrimitiveID, 
        inout TriangleStream<GeoOut> triStream)
{    
    float3 Zup = float3(0.0f, 0.0f, 1.0f);
    float3 dir = gin[0].Pos2 - gin[0].Pos1;
    float3 right = normalize(cross(Zup, dir));
	float3 up = normalize(cross(dir, right));

    //
    // Compute triangle strip vertices (quad) in world space.
    //
    float4 v[10];
    v[0] = float4(gin[0].Pos1 - gin[0].Thickness.x*right + gin[0].Thickness.y*up, 1.0f);
    v[1] = float4(gin[0].Pos2 - gin[0].Thickness.x*right + gin[0].Thickness.y*up, 1.0f);
    v[2] = float4(gin[0].Pos1 + gin[0].Thickness.y*right + gin[0].Thickness.y*up, 1.0f);
    v[3] = float4(gin[0].Pos2 + gin[0].Thickness.y*right + gin[0].Thickness.y*up, 1.0f);
    v[4] = float4(gin[0].Pos1 + gin[0].Thickness.x*right - gin[0].Thickness.y*up, 1.0f);
    v[5] = float4(gin[0].Pos2 + gin[0].Thickness.x*right - gin[0].Thickness.y*up, 1.0f);
    v[6] = float4(gin[0].Pos1 - gin[0].Thickness.y*right - gin[0].Thickness.y*up, 1.0f);
    v[7] = float4(gin[0].Pos2 - gin[0].Thickness.y*right - gin[0].Thickness.y*up, 1.0f);
    v[8] = float4(gin[0].Pos1 - gin[0].Thickness.y*right + gin[0].Thickness.y*up, 1.0f);
    v[9] = float4(gin[0].Pos2 - gin[0].Thickness.y*right + gin[0].Thickness.y*up, 1.0f);
    
	//
    // Transform quad vertices to world space and output 
    // them as a triangle strip.
    //
    GeoOut gout;
    [unroll]
    for(int i = 0; i < 10; ++i)
    {
        gout.PosH       = mul(v[i], View);
        gout.PosH       = mul(gout.PosH, Projection);
        gout.Tex        = float2(0, 0);
        triStream.Append(gout);
    }
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS(GeoOut pin) : SV_Target
{
   // txDiffuse.Sample( samLinear, pin.Tex ) * pin.Color;
   return Color;
}

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( CompileShader( gs_4_0, GS() ) );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );
    }
}