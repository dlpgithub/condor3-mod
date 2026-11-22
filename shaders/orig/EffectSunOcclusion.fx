//--------------------------------------------------------------------------------------
// File: EffectSunBlend.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2DMS <float4, 1> txDiffuse1;
Texture2DMS <float4, 2> txDiffuse2;
Texture2DMS <float4, 4> txDiffuse4;
Texture2DMS <float4, 8> txDiffuse8;

cbuffer cbChangesOnlyOnce
{
  int NumSamples;
}

cbuffer cbChangesEveryFrame
{
  float2 ScreenPos;
}

struct PS_INPUT
{
  float4 position_cs : SV_POSITION;
  float2 Tex         : TEXCOORD;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS(uint id: SV_VertexID)
{
	PS_INPUT output;
    float2 texcoord;
	
	output.Tex = float2((id << 1) & 2, id & 2);
	output.position_cs = float4(output.Tex * float2(2, -2) + float2(-1, 1), 0, 1);
	
	return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input ) : SV_Target
{   
   float4 SunTexture;
   
   if (NumSamples == 1)
     { SunTexture = txDiffuse1.Load( int2(round(ScreenPos.x + 16 * (input.Tex.x - 0.5)), round(ScreenPos.y + 16 * (input.Tex.y - 0.5))), 0); }
   else if (NumSamples == 2)
     { SunTexture = txDiffuse2.Load( int2(round(ScreenPos.x + 16 * (input.Tex.x - 0.5)), round(ScreenPos.y + 16 * (input.Tex.y - 0.5))), 0); }
   else if (NumSamples == 4)
     { SunTexture = txDiffuse4.Load( int2(round(ScreenPos.x + 16 * (input.Tex.x - 0.5)), round(ScreenPos.y + 16 * (input.Tex.y - 0.5))), 0); }
   else if (NumSamples == 8)
     { SunTexture = txDiffuse8.Load( int2(round(ScreenPos.x + 16 * (input.Tex.x - 0.5)), round(ScreenPos.y + 16 * (input.Tex.y - 0.5))), 0); }
   
   if (SunTexture.r > 0)
     { return float4(0, 0, 0, 1); }
   else	
     { return float4(1, 1, 1, 1); }
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