//--------------------------------------------------------------------------------------
// File: EffectSun.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
SamplerState samLinear
{
    Filter = ANISOTROPIC;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbChangesEveryFrame
{
  matrix View;
  matrix Projection;
  float4 SunColor;
  float4 AmbientColor;
}

cbuffer cbPerObject
{
  float Alpha;
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
	uint   id  : SV_VertexID;
};

struct VS_OUTPUT
{
    float4 Pos : SV_POSITION;
    float2 Tex : TEXCOORD0;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input)
{
    // Position
    VS_OUTPUT output;
    
	matrix ViewProj = mul( View, Projection );
	output.Pos = mul( input.Pos, ViewProj );
    output.Tex = gTexC[input.id % 4];
	
    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS(VS_OUTPUT input) : SV_Target
{
    return txDiffuse.Sample( samLinear, input.Tex ) * float4(SunColor.xyz, Alpha);
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
