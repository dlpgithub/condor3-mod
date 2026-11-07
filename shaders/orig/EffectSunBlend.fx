//--------------------------------------------------------------------------------------
// File: EffectSunBlend.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
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
  float4 Color;
}

struct PS_INPUT
{
  float4 position_cs : SV_POSITION;
  float Occlusion: OCCLUSION;
  float OcclusionInterior: OCCLUSIONINTERIOR;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS(uint id: SV_VertexID)
{
	PS_INPUT output;
    float2 texcoord;
	
	texcoord = float2((id << 1) & 2, id & 2);
	output.position_cs = float4(texcoord * float2(2, -2) + float2(-1, 1), 0, 1);
	
	// The 4th mipmap is size 1x1 and is thus the average of the 16x16 texture with depth buffer values
    output.Occlusion = txSunOcclusionDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r;
	output.OcclusionInterior = txSunOcclusionInteriorDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r;

	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{
   return Color * float4(1, 1, 1, input.Occlusion * input.OcclusionInterior);
}

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
		SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );
    }
}