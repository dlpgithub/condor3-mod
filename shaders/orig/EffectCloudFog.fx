//--------------------------------------------------------------------------------------
// File: EffectCloudFog.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------

cbuffer cbChangesEveryFrame
{
  float4 Color;
}

struct PS_INPUT
{
  float4 position_cs : SV_POSITION;
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

	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{
   return Color;
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