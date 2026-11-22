//--------------------------------------------------------------------------------------
// File: EffectLensFlares.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txSunOcclusionDiffuse;
Texture2D txSunOcclusionInteriorDiffuse;
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbPerObject
{
  float4 Pos;
  float SizeX;
  float SizeY;
  float4 Color;
  float Angle;
}

struct VS_OUTPUT
{
  float4 Pos: SV_POSITION;
  float2 Tex: TEXCOORD;
  float Occlusion: OCCLUSION;
  float OcclusionInterior: OCCLUSIONINTERIOR;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS (uint id : SV_VertexID)
{
    VS_OUTPUT output;

	// The 4th mipmap is size 1x1 and is thus the average of the 16x16 texture with depth buffer values
    output.Occlusion = txSunOcclusionDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r;
	output.OcclusionInterior = txSunOcclusionInteriorDiffuse.SampleLevel( samLinear, float2(0.5, 0.5), 4).r;
	
	float Occlusion = output.Occlusion * output.OcclusionInterior;

    // Construct a screen space quad (triangle strip)
	if (id == 0)
	{
	  output.Pos = float4(Pos.x - SizeX * Occlusion , Pos.y + SizeY * Occlusion, 0, 1);
	  output.Tex = float2(0, 1);
	}
	else if (id == 1)
	{
	  output.Pos = float4(Pos.x + SizeX * Occlusion, Pos.y + SizeY * Occlusion, 0, 1);
	  output.Tex = float2(1, 1);
	}
	else if (id == 2)
	{
	  output.Pos = float4(Pos.x - SizeX * Occlusion, Pos.y - SizeY * Occlusion, 0, 1);
	  output.Tex = float2(0, 0);
	}
	else if (id == 3)
	{
	  output.Pos = float4(Pos.x + SizeX * Occlusion, Pos.y - SizeY * Occlusion, 0, 1);
	  output.Tex = float2(1, 0);
	}
	else
	{
	  output.Pos = float4(0, 0, 0, 0);
	  output.Tex = float2(0, 0);
	}
	
	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PSFlares( VS_OUTPUT input ) : SV_Target
{
   float alpha = txDiffuse.Sample( samLinear, input.Tex ).a * (0.03 + 0.4 * Angle);
   clip(alpha - 0.05);
   
   return saturate(float4(1, 1, 1, alpha * input.Occlusion * input.OcclusionInterior) * Color);
}

BlendState SrcAlphaBlendingAdd
{
    BlendEnable[0] = TRUE;
    SrcBlend = SRC_ALPHA;
    DestBlend = ONE;
    BlendOp = ADD;
    SrcBlendAlpha = ZERO;
    DestBlendAlpha = ZERO;
    BlendOpAlpha = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

technique10 RenderFlares
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
		SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PSFlares() ) );

        SetBlendState( SrcAlphaBlendingAdd, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
	}
}