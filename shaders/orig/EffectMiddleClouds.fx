//--------------------------------------------------------------------------------------
// File: EffectMiddleClouds.fx
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
  float3 CameraPos;
  float4 FogColor;
  float3 SunPos;
  float4 SunColor;
  float4 AmbientColor;
}

cbuffer cbFixed
{
  float2 gTexC[4] = 
  {
     float2(1.0f, 0.0f),
     float2(1.0f, 1.0f),
     float2(0.0f, 0.0f),
     float2(0.0f, 1.0f)
  };
};

struct VS_INPUT
{
    float4 Pos : POSITION;
	float2 Tex : TEXCOORD0;
};

struct VS_OUTPUT
{
    float4 Pos : SV_POSITION;
    float2 Tex : TEXCOORD0;
    float4 SkyColor: TEXCOORD1;
	float fogFactor: FOGFACTOR;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VSTop( VS_INPUT input)
{
    VS_OUTPUT output;

    float3 LookVec = input.Pos.xyz - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
    
    // Vertical offset
    float4 offsetPos = input.Pos;
	float4 color = txDiffuse.SampleLevel(samLinear, float4(input.Tex, 0, 0), 0);
	float lift = color.r + color.g + color.b;
	offsetPos.z += 50 * lift - sqrt(fDistanceSqr) * 0.02;
	
	matrix ViewProj = mul( View, Projection );
	output.Pos = mul( offsetPos, ViewProj );
    output.Tex = input.Tex;
	
    // Calculate fog factor for exponential fog
	output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);

    // Sky Color
	LookVec = normalize(LookVec);
	float fHeight = 0;//max(LookVec.z, 0);
    float cFadeFactor = 1;
	float Red = FogColor.x / (4 * cFadeFactor * fHeight * (1.0 + 0.0 * SunPos.z) + 1);
    float Green = FogColor.y / (1.8 * cFadeFactor * fHeight * (1.0 + 0.0 * SunPos.z) + 1);
    float Blue = FogColor.z / (0.5 * cFadeFactor * fHeight * (1.0 + 0.0 * SunPos.z) + 1);
	
    float dAngle = max(acos(dot(SunPos, LookVec)), 0);
	float dAngleZ = max(LookVec.z - SunPos.z, 0);
	
	float dAngleFactor = exp(-2 * (0.1 + 0.9 * dAngle) * (0.5 + 0.5 * SunPos.z) - 1 * dAngleZ * (1 - sqrt(max(SunPos.z, 0))));
  
    output.SkyColor.x = Red + dAngleFactor * (SunColor.x - Red);
    output.SkyColor.y = Green + dAngleFactor * (SunColor.y - Green);
    output.SkyColor.z = Blue + dAngleFactor * (SunColor.z - Blue);
		
    return output;
}

VS_OUTPUT VSBase( VS_INPUT input)
{
    VS_OUTPUT output;

    float3 LookVec = input.Pos.xyz - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
    
    // Vertical offset
    float4 offsetPos = input.Pos;
	float4 color = txDiffuse.SampleLevel(samLinear, float4(input.Tex, 0, 0), 0);
	float lift = color.r + color.g + color.b;
	offsetPos.z -= sqrt(fDistanceSqr) * 0.05;
	
	matrix ViewProj = mul( View, Projection );
	output.Pos = mul( offsetPos, ViewProj );
    output.Tex = input.Tex;
	
    // Calculate fog factor for exponential fog
	output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);

    // Sky Color
	LookVec = normalize(LookVec);
	
    float dAngle = max(acos(dot(SunPos, LookVec)), 0);
	float dAngleZ = max(LookVec.z - SunPos.z, 0);	
	float dAngleFactor = exp(-2 * (0.1 + 0.9 * dAngle) * (0.5 + 0.5 * SunPos.z) - 1 * dAngleZ * (1 - sqrt(max(SunPos.z, 0))));
  
    output.SkyColor.x = FogColor.x + dAngleFactor * (SunColor.x - FogColor.x);
    output.SkyColor.y = FogColor.y + dAngleFactor * (SunColor.y - FogColor.y);
    output.SkyColor.z = FogColor.z + dAngleFactor * (SunColor.z - FogColor.z);
		
    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PSTop(VS_OUTPUT input) : SV_Target
{
    float4 color = txDiffuse.Sample( samLinear, input.Tex );
	
	// Lift
	float lift = color.r + color.g + color.b;
	
    // Alpha
	color.a = saturate(2 * lift / (0.5 + lift));
	clip( color.a - 0.01f );
	
	// Color
    color.rgb = SunColor.xyz;
	
    // Calculate the final color using the fog effect equation.
	return lerp(float4(input.SkyColor.xyz, color.a), color, input.fogFactor);
}

float4 PSBase(VS_OUTPUT input) : SV_Target
{
    float4 color = txDiffuse.Sample( samLinear, input.Tex );
		
	// Lift
	float lift = color.r + color.g + color.b;
	
    // Alpha
	color.a = saturate(2 * lift / (0.5 + lift));
	clip( color.a - 0.01f );
	
	// Color
    color.rgb = lerp(SunColor.rgb, 0.1 * SunColor.rgb + AmbientColor.rgb, smoothstep(0, 3, lift));
	
    // Calculate the final color using the fog effect equation.
	return lerp(float4(input.SkyColor.xyz, color.a), color, input.fogFactor);
}

technique10 RenderTop
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VSTop() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PSTop() ) );
    }
}

technique10 RenderBase // Currently we render base and top with the same vertices, try to make the base flat for 3.x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VSTop() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PSBase() ) );
    }
}
