//--------------------------------------------------------------------------------------
// File: EffectFlightTrail.fx
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
	float3 LightDir;
	float4 LandscapeOrigin;
}

cbuffer cbChangesForEveryTrail
{
	float4 Color;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
	float3 Norm : NORMAL0;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION;
	float3 Norm : TEXCOORD1;
	float lightIntensity : TEXCOORD2;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS( VS_INPUT input)
{
    PS_INPUT output;
	
	// Position and normal
	float4 localPos = input.Pos - LandscapeOrigin;
	
	matrix ViewProj = mul( View, Projection );
	output.Pos = mul( localPos, ViewProj );
    output.Norm = mul( input.Norm, (float3x3)ViewProj );
	
	output.lightIntensity = saturate(dot(input.Norm, -LightDir));
	
    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input, bool vf :  SV_IsFrontFace ) : SV_Target
{
    float4 outputColor;
	if (vf == true)
		{ outputColor.rgb = (0.4 + 0.6 * (1 - input.lightIntensity)) * Color.rgb; }
	else
	    { outputColor.rgb = (0.4 + 0.6 * input.lightIntensity) * Color.rgb; }
    outputColor.a = Color.a;
   
    return outputColor;
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