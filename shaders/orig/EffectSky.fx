//--------------------------------------------------------------------------------------
// File: EffectSky.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
cbuffer cbChangesOnce
{
  float Radius;
}

cbuffer cbChangesEveryFrame
{
  matrix World;
  matrix View;
  matrix Projection;
  float3 CameraPos;
  float3 SunPos;
  float3 SunColor;
  float4 FogColor;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
    float4 Color : COLOR;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION; 
    float4 Color : COLOR;
};


//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS( VS_INPUT input )
{
    // Position
    PS_INPUT output = (PS_INPUT)0;
	input.Pos.x = Radius * input.Pos.x;
	input.Pos.y = Radius * input.Pos.y;
	input.Pos.z = Radius * input.Pos.z;
    input.Pos = mul( input.Pos, World );
    output.Pos = mul( input.Pos, View );
    output.Pos = mul( output.Pos, Projection );

    // Color
	float3 LookVec = normalize(input.Pos.xyz - CameraPos);
	float fHeight = max(LookVec.z, 0);

	float cFadeFactor = 1;
    float dGradientFactorRed = 1 / (4 * cFadeFactor * fHeight + 1);
    float dGradientFactorGreen = 1 / (1.8 * cFadeFactor * fHeight + 1);
    float dGradientFactorBlue = 1 / (0.5 * cFadeFactor * fHeight + 1);
	
    float Red = FogColor.x * dGradientFactorRed;
    float Green = FogColor.y * dGradientFactorGreen;
    float Blue = FogColor.z * dGradientFactorBlue;
	  
    float dAngle = max(acos(dot(SunPos, LookVec)), 0);
	float dAngleZ = max(LookVec.z - SunPos.z, 0);
	
	float dAngleFactor = exp(-2 * (0.1 + 0.9 * dAngle) * (0.5 + 0.5 * SunPos.z) - 1 * dAngleZ * (1 - sqrt(max(SunPos.z, 0))));
  
    output.Color.x = Red + dAngleFactor * (SunColor.x - Red);
    output.Color.y = Green + dAngleFactor * (SunColor.y - Green);
    output.Color.z = Blue + dAngleFactor * (SunColor.z - Blue); 

    return output;
}


//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input) : SV_Target
{
  // Calculate the final color using the fog effect equation.
  return input.Color;
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