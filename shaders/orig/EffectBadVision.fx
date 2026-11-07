//--------------------------------------------------------------------------------------
// File: EffectBadVision.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------

cbuffer cbChangesEveryFrame
{
  float GForce;
}

struct PS_INPUT
{
  float4 position_cs : SV_POSITION;
  float2 tex: TEXCOORD0;
  float4 Color: COLOR;
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
    output.tex = texcoord;

    if (GForce > 5)
    { output.Color = float4(0, 0, 0, (GForce - 5) * 0.4); }
    else if (GForce < -2)
    { output.Color = float4(0.6, 0.2, 0.2, -(GForce + 2) * 0.2); }
    else
    { output.Color = float4(0, 0, 0, 0); };
	
	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{
   float CenterDist = sqrt((pow(input.tex.x - 0.5, 2) + pow(input.tex.y - 0.5, 2)));
   return float4(input.Color.r, input.Color.g, input.Color.b, 2 * input.Color.a * CenterDist + 0.6 * input.Color.a);
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