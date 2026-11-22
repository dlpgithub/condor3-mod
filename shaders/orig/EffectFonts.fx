//--------------------------------------------------------------------------------------
// File: Tutorial02.fx
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

float4 FontColor;

cbuffer Font3DParameters
{
  float3 Pos;
  float3 EyePos; 
  matrix View;
  matrix Projection;
}

struct VS_INPUT
{
    float4 Pos : POSITION;
    float2 Tex : TEXCOORD;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION;
    float2 Tex : TEXCOORD0;
};

//**********************************************************
//******************* 2D screen space fonts *******************
//**********************************************************

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS( VS_INPUT input )
{
    PS_INPUT output = (PS_INPUT)0;
    output.Pos = input.Pos;
    output.Tex = input.Tex;

    return output;
}


//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input) : SV_Target
{
    return txDiffuse.Sample( samLinear, input.Tex ) * FontColor;
}


//**********************************************************
// ******************* 3D world space fonts *******************
//**********************************************************

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS3D( VS_INPUT input )
{
    // Position
    PS_INPUT output;
    
	float3 up = float3(View._12, View._22, View._32);
    float3 look = Pos - EyePos;
    float3 right = normalize(cross(look, up));
    float3 viewup = normalize(cross(right, look));
		
	output.Pos = float4(Pos - right * input.Pos.x - viewup * input.Pos.y, 1.0f);
	output.Pos = mul( output.Pos, View );
    output.Pos = mul( output.Pos, Projection );
	
	output.Tex = input.Tex;

    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS3D( PS_INPUT input) : SV_Target
{    
	float4 TextureColor = txDiffuse.Sample( samLinear, input.Tex );
	clip( TextureColor.a - 0.1f );
	
	return TextureColor * FontColor;
}
//--------------------------------------------------------------------------------------


BlendState PreMultipliedAlpha
{
    BlendEnable[0] = TRUE;
    SrcBlend = SRC_ALPHA;
    DestBlend = INV_SRC_ALPHA;
    BlendOp = ADD;
    SrcBlendAlpha = INV_DEST_ALPHA;
    DestBlendAlpha = ONE;
    BlendOpAlpha = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

DepthStencilState DepthDisable 
{
    DepthEnable = false;
    DepthWriteMask = ZERO;
};

// 2D fonts
technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );

        SetBlendState( PreMultipliedAlpha, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
        SetDepthStencilState(DepthDisable, 0);      
    }
}

// 3D fonts
technique10 Render3D
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS3D() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS3D() ) );

        SetBlendState( PreMultipliedAlpha, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}