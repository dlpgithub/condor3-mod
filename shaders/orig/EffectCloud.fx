//--------------------------------------------------------------------------------------
// File: EffectCloud.fx
//
// Copyright (c) UBSoft d.o.o. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D tx;
Texture2D txRainShaft;

SamplerState samLinear
{
    Filter = MIN_MAG_MIP_POINT;
    AddressU = CLAMP;
    AddressV = CLAMP;
};

cbuffer cbChangesOnlyOnce
{
  int NumTextures;
}

cbuffer cbPerCloud
{
  float3 LocalPos;
  float RelativeSize;
  float Maturity;
  float Alpha;
  float DissipateFactor;
  float RainShaftRelativeSize;
  float3 ScaleFactors;
  float3 LightningPos;
}

cbuffer cbChangesEveryFrame
{
  float3 EyePos;
  matrix View;
  matrix Projection;
  float3 LightDir;
  float4 AmbientColor;
  float4 FogColor;
  float4 SunColor;
  // TEMP (just for testing purposes, == 1 in release version)
  float PuffScale;
}

struct VS_INPUT
{
    uint id: SV_VertexID;
	float3 Pos : POSITION;
	float3 Norm : NORMAL;
    int TextureIndex : TINDEX;
    float Size : PSIZE;
};

struct VS_OUTPUT
{
    float3 Center : POSITION;
	float3 Normal : NORMAL;
    int TextureIndex : TINDEX;
    float Size : PSIZE;
	float FogFactor : FOGFACTOR;
	float FogFactor1 : FOGFACTOR1;
	float dAngle : ANGLEFACTOR;
	float3 FogColor : TEXCOORD0;
	float LightningGlow: TEXCOORD1;
};

struct GeoOut
{
    float4 PosH     : SV_POSITION;
    float2 Tex      : TEXCOORD0;
	float3 Norm     : TEXCOORD1;
	float3 NormUp   : TEXCOORD2;
	float Size      : PSIZE;
	float FogFactor : TEXCOORD3;
	float FogFactor1: TEXCOORD4;
    float dAngle    : TEXCOORD5;
	float3 FogColor : TEXCOORD6;
	int TextureIndex : TINDEX;
	float LightningGlow: TEXCOORD7;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
VS_OUTPUT VS( VS_INPUT input)
{
    // Position
    VS_OUTPUT output;
	
	output.Size = input.Size * ScaleFactors.z;
	output.Center.x = LocalPos.x + input.Pos.x * ScaleFactors.x;    
	output.Center.y = LocalPos.y + input.Pos.y * ScaleFactors.x;
	output.Center.z = LocalPos.z + input.Pos.z * ScaleFactors.y + output.Size; 			

	if (Maturity < 1) 
	{	
		float fMod = 0.2 * (input.id % 5);
		float fModFactor;
		if (Maturity > fMod)
			{ fModFactor = saturate(5 * (Maturity - fMod)); }
		else
			{ fModFactor = 0; }	
		output.Size *= fModFactor;
	}
	else
	{	
		float fMod = 0.2 * ((0.03 * input.id) % 5 + 1);
		float fModFactor;
		if (Maturity - 1 <= fMod)
			{ fModFactor = saturate(5 * (fMod - (Maturity - 1))); } 
		else
			{ fModFactor = 0; }
		output.Size *= fModFactor;	
	}

	output.Normal = input.Norm;
    output.TextureIndex = input.TextureIndex;
		
    // Calculate fog factor for exponential fog
    float3 LookVec = output.Center - EyePos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
	float fDistance = sqrt(fDistanceSqr);
	float k = 9.81 / 287 / 280;
	float fAverageTurbidity = abs((exp(-k * EyePos.z) - exp(-k * output.Center.z)) / (k * (EyePos.z - output.Center.z)));
	float fOpticalDistance = fDistance * fAverageTurbidity;
	output.FogFactor = saturate(1.05 * exp(-0.00002 * fOpticalDistance) - 0.05); 
	output.FogFactor1 = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);
	
    // Color
	LookVec = normalize(LookVec);
    output.dAngle = max(acos(dot(-LightDir, LookVec)), 0);
	float dAngleFactor = exp(-3 * output.dAngle * (0.5 - 0.5 * LightDir.z));
  
	// For rainshafts, we need to compute skycolor at height = 0
    output.FogColor.x = FogColor.x + dAngleFactor * (SunColor.x - FogColor.x);
    output.FogColor.y = FogColor.y + dAngleFactor * (SunColor.y - FogColor.y);
    output.FogColor.z = FogColor.z + dAngleFactor * (SunColor.z - FogColor.z);	
	
	// LightningGlow
	if (LightningPos.z > 0)
	{ 
		float3 Distance; 
		Distance.xy = LightningPos.xy - output.Center.xy;
		Distance.z = output.Center.z - LocalPos.z - output.Size;
		if (Distance.z < 0)
			{ Distance.z = 0; }
		output.LightningGlow = pow((Distance.x * Distance.x + Distance.y * Distance.y + Distance.z * Distance.z), 0.5);	
		output.LightningGlow = exp(-0.001 * output.LightningGlow);
	}
	else
		{ output.LightningGlow = 0; }
	
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
    // Compute the local coordinate system of the sprite relative to the world
    // space such that the billboard is aligned with the y-axis and faces the eye.
    //

    float3 up = float3(0.0f, 0.0f, 1.0f);
    float3 look = gin[0].Center - EyePos;
    look = normalize(look);
    float3 right = normalize(cross(look, up));
    float3 viewup = normalize(cross(right, look));
	
	// Rain shafts / cloud
	if (gin[0].TextureIndex == 16)
	{		
		if (RainShaftRelativeSize > 0) 
		{
			// Compute texture coordinates
			float2 gTexC[4];
			gTexC[0] = float2(0.0f, 0.0f);
			gTexC[1] = float2(1.0f, 0.0f);
			gTexC[2] = float2(0.0f, 1.0f);
			gTexC[3] = float2(1.0f, 1.0f);
			
			float3 center;
			center = gin[0].Center;
			
			// construct vertex positions
			float4 v[4];
			v[0] = float4(center + gin[0].Size*right*PuffScale, 1.0f);
			v[1] = float4(center - gin[0].Size*right*PuffScale, 1.0f);
			v[2] = float4(center + gin[0].Size*right*PuffScale - up * 2 * RainShaftRelativeSize * center.z, 1.0f);
			v[3] = float4(center - gin[0].Size*right*PuffScale - up * 2 * RainShaftRelativeSize * center.z, 1.0f);
			
			// construct vertex normals
			float3 norm[4];
			norm[0] = normalize(3 * gin[0].Normal + 2 * viewup + 1 * right);
			norm[1] = normalize(3 * gin[0].Normal + 2 * viewup - 1 * right);
			norm[2] = normalize(3 * gin[0].Normal - 3 * viewup + 1 * right);
			norm[3] = normalize(3 * gin[0].Normal - 3 * viewup - 1 * right);
			
			//
			// Transform quad vertices to world space and output 
			// them as a triangle strip.
			//		
			{
				GeoOut gout;
				[unroll]
				for(int i = 0; i < 4; ++i)
				{
					gout.PosH          = mul(v[i], View);
					gout.PosH          = mul(gout.PosH, Projection);
					gout.Tex           = gTexC[i];
					gout.NormUp        = norm[i];
					gout.Norm          = gin[0].Normal;
					gout.Size          = gin[0].Size;
					gout.FogFactor     = gin[0].FogFactor;
					gout.FogFactor1    = gin[0].FogFactor1;
					gout.dAngle        = gin[0].dAngle;
					gout.FogColor      = gin[0].FogColor;
					gout.TextureIndex  = gin[0].TextureIndex;
					gout.LightningGlow = gin[0].LightningGlow;
								 
					triStream.Append(gout);
				}
			}
		}		
	}
	else
	{
		// Compute texture coordinates
		float fU0 = (gin[0].TextureIndex + 0.0f) / NumTextures;
		float fU1 = (gin[0].TextureIndex + 1.0f) / NumTextures;

		float2 gTexC[4];
		gTexC[0] = float2(fU1, 0.0f);
		gTexC[1] = float2(fU0, 0.0f);
		gTexC[2] = float2(fU1, 1.0f);
		gTexC[3] = float2(fU0, 1.0f);
	
		// Clip backfacing (disabled)
		if ((dot(look, gin[0].Normal) < 2) || (gin[0].TextureIndex >= 8))
		{			
			float3 center;
			center = gin[0].Center;
			
			// construct vertex positions
			float4 v[4];
			v[0] = float4(center + gin[0].Size*right*PuffScale + gin[0].Size*viewup*PuffScale, 1.0f);
			v[1] = float4(center - gin[0].Size*right*PuffScale + gin[0].Size*viewup*PuffScale, 1.0f);
			v[2] = float4(center + gin[0].Size*right*PuffScale - gin[0].Size*viewup*PuffScale, 1.0f);
			v[3] = float4(center - gin[0].Size*right*PuffScale - gin[0].Size*viewup*PuffScale, 1.0f);
			
			// construct vertex normals
			float3 norm[4];
			norm[0] = normalize(3 * gin[0].Normal + 2 * viewup + 1 * right);
			norm[1] = normalize(3 * gin[0].Normal + 2 * viewup - 1 * right);
			norm[2] = normalize(3 * gin[0].Normal - 3 * viewup + 1 * right);
			norm[3] = normalize(3 * gin[0].Normal - 3 * viewup - 1 * right);
			
			//
			// Transform quad vertices to world space and output 
			// them as a triangle strip.
			//
			//if (gin[0].TextureIndex >= 8)
			{
				GeoOut gout;
				[unroll]
				for(int i = 0; i < 4; ++i)
				{
					gout.PosH          = mul(v[i], View);
					gout.PosH          = mul(gout.PosH, Projection);
					gout.Tex           = gTexC[i];
					gout.NormUp        = norm[i];
					gout.Norm          = gin[0].Normal;
					gout.Size          = gin[0].Size;
					gout.FogFactor     = gin[0].FogFactor;
					gout.FogFactor1    = gin[0].FogFactor1;
					gout.dAngle        = gin[0].dAngle;
					gout.FogColor      = gin[0].FogColor;
					gout.TextureIndex  = gin[0].TextureIndex;
					gout.LightningGlow = gin[0].LightningGlow;
								 
					triStream.Append(gout);
				}
			}
		}
	}
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PSTransparent(GeoOut input) : SV_Target
{
    float4 TextureColor;
	
	// EARLY CLIP
    // Rain shafts
	if (input.TextureIndex == 16)
		{ TextureColor = txRainShaft.Sample( samLinear, input.Tex ); }
	// Cloud
	else
		{ TextureColor = tx.Sample( samLinear, input.Tex ); }
	clip( TextureColor.a - 0.01f );

	float4 LightingColor;
	float4 SkyColor;

	// Light intensity
	float Dot = dot(input.NormUp, -LightDir);
	float lightIntensity;
	lightIntensity = saturate(0.4 + 0.4 * Alpha * Dot * max(min(Maturity, 2 - Maturity), pow(RelativeSize, 0.5))); 

	// Glow
	lightIntensity = saturate(lightIntensity + 0.3 * input.LightningGlow);
	
	// Rain shafts
	if (input.TextureIndex == 16)
	{ 
		TextureColor.a = 0.1 * Alpha * TextureColor.a;

		LightingColor = saturate(SunColor * lightIntensity + AmbientColor);
		LightingColor.a = 1;
		
		// Calculate the final color using the fog effect equation.
        return lerp(float4(input.FogColor, TextureColor.a), saturate(LightingColor * TextureColor), input.FogFactor1);		
	}
	// Cloud
	else
	{ 
		// Growing cloud
		if (Maturity < 1)
		{
			if (input.Tex.x < 0.5)
			{ 
			  float2 TexFractus;
			  TexFractus.x = input.Tex.x + 0.5;
			  TexFractus.y = input.Tex.y;
			  float4 TextureColorFractus = tx.Sample( samLinear, TexFractus );
			  TextureColor = lerp(TextureColorFractus, TextureColor, DissipateFactor * Maturity * saturate(0.2 + 0.8 * RelativeSize));
			}
			TextureColor.a = TextureColor.a * Alpha * saturate((input.Size - 50) * 0.02);
			clip( TextureColor.a - 0.01f );
		}
		// Dissipating cloud
		else if (Maturity <= 2)
		{
			if (input.Tex.x < 0.5)
			{ 
			  float2 TexFractus;
			  TexFractus.x = input.Tex.x + 0.5;
			  TexFractus.y = input.Tex.y;
			  float4 TextureColorFractus = tx.Sample( samLinear, TexFractus );
			  TextureColor = lerp(TextureColorFractus, TextureColor, DissipateFactor * pow(2 - Maturity, 2) * saturate(0.2 + 0.8 * RelativeSize));
			}
			TextureColor.a = TextureColor.a * Alpha * saturate((input.Size - 50) * 0.02);	  
			clip( TextureColor.a - 0.01f );
		}
		else
		  clip( -1 );	
	
		float glare = saturate(1 - 2 * abs(dot(input.Norm, -LightDir))) * exp(-3 * input.dAngle);		
		LightingColor = saturate(SunColor * lightIntensity + AmbientColor + 0.5 * glare);
				
		// Calculate the final color using the fog effect equation.
		float4 finalcolor = float4(float3(LightingColor.rgb * TextureColor.rgb), TextureColor.a * input.FogFactor);				
		return lerp(float4(FogColor.rgb, TextureColor.a), finalcolor, input.FogFactor);				
	}	
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

technique10 RenderTransparent
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS() ) );
        SetGeometryShader( CompileShader( gs_4_0, GS() ) );
        SetPixelShader( CompileShader( ps_4_0, PSTransparent() ) );

        SetBlendState( SrcAlphaBlendingAdd, float4( 0.0f, 0.0f, 0.0f, 0.0f ), 0xFFFFFFFF );
    }
}
