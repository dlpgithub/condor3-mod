//--------------------------------------------------------------------------------------
// File: EffectSmoke.fx
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
  matrix View;
  matrix Projection;
}

cbuffer cbFixed
{
  float2 gTexC[4] = 
  {
     float2(0.0f, 0.0f),
	 float2(0.0f, 1.0f),   
     float2(1.0f, 0.0f),
	 float2(1.0f, 1.0f)
  };
};

struct VS_INPUT
{
    float4 Pos : POSITION;
	float4 Color : COLOR;
	uint vertexId : SV_VertexID;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION; 
	float2 Tex : TEXCOORD; 
	float4 Color : COLOR;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS( VS_INPUT input )
{
    // Position
    PS_INPUT output = (PS_INPUT)0;
    input.Pos = mul( input.Pos, View );
    output.Pos = mul( input.Pos, Projection );
    
    // Tex
	output.Tex = gTexC[input.vertexId % 4];
	
	// Color
	output.Color = input.Color;
	
    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{
   clip( input.Color.a - 0.01f );
   return txDiffuse.Sample( samLinear, input.Tex ) * input.Color;
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