//--------------------------------------------------------------------------------------
// File: Tutorial02.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuseCloudParticle;
SamplerState samLinear
{
    Filter = MIN_MAG_MIP_LINEAR;
    AddressU = Wrap;
    AddressV = Wrap;
};

cbuffer cbPerCloud
{
  float Alpha;
}

cbuffer cbFixed
{
  float2 gTexC[4] = 
  {
     float2(1.0f, 1.0f),
     float2(0.0f, 1.0f),
     float2(1.0f, 0.0f),
     float2(0.0f, 0.0f)
  };
}

struct VS_INPUT
{
    float3 CenterAndSize : POSITION;
};

struct VS_OUTPUT
{
    float3 CenterAndSize : POSITION;
};

struct GeoOut
{
    float4 PosH    : SV_POSITION;
    float2 Tex     : TEXCOORD;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input )
{
    VS_OUTPUT output;
	output.CenterAndSize = input.CenterAndSize;  
	
    return output;
}

//--------------------------------------------------------------------------------------
// Geometry Shader
//--------------------------------------------------------------------------------------
[maxvertexcount(4)]
void GS(point VS_OUTPUT gin[1], 
        uint primID : SV_PrimitiveID, 
        inout TriangleStream<GeoOut> triStream)
{    
    //
    // Compute triangle strip vertices (quad) in screen space.
    //
	float2 right = float2(1.0f, 0.0f);
    float2 up = float2(0.0f, 1.0f);	
	float size = gin[0].CenterAndSize.z; //0.008f;
	
    float4 v[4];
	
	v[0] = float4(gin[0].CenterAndSize.xy - size * right - size * up, 0.0f, 1.0f);
    v[1] = float4(gin[0].CenterAndSize.xy - size * right + size * up, 0.0f, 1.0f);
    v[2] = float4(gin[0].CenterAndSize.xy + size * right - size * up, 0.0f, 1.0f);
    v[3] = float4(gin[0].CenterAndSize.xy + size * right + size * up, 0.0f, 1.0f);

    //
    // Output vertices as a triangle strip.
    //
    GeoOut gout;
    [unroll]
    for(int i = 0; i < 4; ++i)
    {
        gout.PosH     = v[i];
        gout.Tex      = gTexC[i];
        
        triStream.Append(gout);
    }
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS(GeoOut pin) : SV_Target
{   
   float4 color = txDiffuseCloudParticle.Sample( samLinear, pin.Tex );
   color.a *= color.a * Alpha;
   return color;
}

//--------------------------------------------------------------------------------------
BlendState SrcAlphaBlendingAdd
{
    BlendEnable[0] = TRUE;
    SrcBlend = SRC_ALPHA;
    DestBlend = INV_SRC_ALPHA;
    BlendOp = ADD;
    SrcBlendAlpha = ZERO;
    DestBlendAlpha = ZERO;
    BlendOpAlpha = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

DepthStencilState DepthDisable 
{
    DepthEnable = false;
    DepthWriteMask = ZERO;
};

technique10 Render
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( CompileShader( gs_4_0, GS() ) );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );

        SetBlendState( SrcAlphaBlendingAdd, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
        SetDepthStencilState(DepthDisable, 0);      
    }
};
