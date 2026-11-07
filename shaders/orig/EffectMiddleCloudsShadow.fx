//--------------------------------------------------------------------------------------
// File: EffectMiddleCloudsShadow.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuseMiddleCloudsShadow;
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

struct PS_INPUT
{
  float4 position_cs : SV_POSITION;
  float2 Tex: TEXCOORD;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS(uint id: SV_VertexID)
{
	PS_INPUT output;
	
	output.Tex = float2((id << 1) & 2, id & 2);
	output.position_cs = float4(output.Tex * float2(2, -2) + float2(-1, 1), 0, 1);
	output.Tex.x = 0.75 - 0.5 * output.Tex.x;
	output.Tex.y = 0.25 + 0.5 * output.Tex.y;

	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{
   return float4(0, 0, 0, txDiffuseMiddleCloudsShadow.Sample( samLinear, input.Tex).a);
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