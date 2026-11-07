//--------------------------------------------------------------------------------------
// File: EffectDXFile.fx
//
// Copyright (c) Microsoft Corporation. All rights reserved.
//--------------------------------------------------------------------------------------

//--------------------------------------------------------------------------------------
// Constant Buffer Variables
//--------------------------------------------------------------------------------------
Texture2D txDiffuse;
Texture2D txDiffuseShadow;
Texture2D txDiffuseShadow1;
Texture2D txReflectionOrDetail;
Texture2D txCloudShadow;
Texture2D txAO;
Texture2D txBM;
TextureCube txCubeMap;

SamplerState samLinear
{
    Filter = ANISOTROPIC;
    AddressU = CLAMP;
    AddressV = CLAMP;
	MipLODBias = -0.5;
};

SamplerState samLinearWrap
{
    Filter = ANISOTROPIC;
    AddressU = WRAP;
    AddressV = WRAP;
	MipLODBias = 0;
};

SamplerComparisonState cmpSampler
{
   // sampler state
   Filter = COMPARISON_MIN_MAG_MIP_LINEAR;
   AddressU = CLAMP;
   AddressV = CLAMP;
 
   // sampler comparison state
   ComparisonFunc = GREATER_EQUAL; //LESS_EQUAL; // Use GREATER for (1 - Z/W) depth buffer
};

cbuffer cbChangesOnlyOnce
{
  float NormalOffset;
  float AlphaRef;
  float ShadowMapFactor;
}

cbuffer cbChangesEveryFrame
{
  matrix View;
  matrix Projection; 
  float3 CameraPos;
  float3 LightDir;
  float4 LightColor;
  float3 AmbientDir; // World down vector in cockpit view
  float4 AmbientColor;
  float ShadeFactor;
  float4 FogColor;
};

cbuffer cbAnimationWeights
{
  float Weight1;
  float Weight2;
  float Weight3;
  float Weight4;
}

cbuffer cbPerObject
{
  matrix World;
  matrix LWMatrix; // Just for environmental mapping
  bool CockpitView; 
  float4 MaterialColor;
  float SpecularStrength;
  float SpecularShininess;
  float EnvironmentalStrength;
  float WrapScale;
  float ScaleX; // dU / dX
  float ScaleY; // dV / dY  
  bool BumpMap;
};

cbuffer cbForShadowedObjects
{  
  matrix ViewProjLight;
  matrix ViewProjLight1;
};

cbuffer cbForEveryMovingObject
{
  matrix Move;
  float Flex;
  float Flutter;  
};

struct VS_INPUT
{
    float4 Pos : POSITION;
    float3 Norm : NORMAL;
    float2 Tex : TEXCOORD;
};

struct VS_INPUT_Pos2x
{
    float4 Pos0 : POSITION0;
	float4 Pos1 : POSITION1;
	float3 Norm0 : NORMAL0;
	float3 Norm1 : NORMAL1;
    float2 Tex : TEXCOORD;
};

struct VS_INPUT_Pos3x
{
    float4 Pos0 : POSITION0;
	float4 Pos1 : POSITION1;
	float4 Pos2 : POSITION2;
	float3 Norm0 : NORMAL0;
	float3 Norm1 : NORMAL1;
	float3 Norm2 : NORMAL2;
    float2 Tex : TEXCOORD;
};

struct VS_INPUT_Pos4x
{
    float4 Pos0 : POSITION0;
	float4 Pos1 : POSITION1;
	float4 Pos2 : POSITION2;
	float4 Pos3 : POSITION3;
	float3 Norm0 : NORMAL0;
	float3 Norm1 : NORMAL1;
	float3 Norm2 : NORMAL2;
	float3 Norm3 : NORMAL3;
    float2 Tex : TEXCOORD;
};

struct VS_INPUT_Pos6x
{
    float4 Pos0 : POSITION0;
	float4 Pos1 : POSITION1;
	float4 Pos2 : POSITION2;
	float4 Pos3 : POSITION3;
	float4 Pos4 : POSITION4;
	float4 Pos5 : POSITION5;
	float3 Norm0 : NORMAL0;
	float3 Norm1 : NORMAL1;
	float3 Norm2 : NORMAL2;
	float3 Norm3 : NORMAL3;
	float3 Norm4 : NORMAL4;
	float3 Norm5 : NORMAL5;
    float2 Tex : TEXCOORD;
};

struct PS_INPUT
{
    float4 Pos : SV_POSITION;
	float3 Norm : TEXCOORD2;
    float2 Tex : TEXCOORD0;
	float3 WorldPos: TEXCOORD1;
	float4 lpos : TEXCOORD3; // https://takinginitiative.wordpress.com/2011/05/15/directx10-tutorial-10-shadow-mapping/
	float3 Tangent : TEXCOORD4;
	float3 Binormal : TEXCOORD5;
};

struct PS_INPUT_GROUNDOBJECT
{
    float4 Pos : SV_POSITION;
	float3 Norm : TEXCOORD2;
    float2 Tex : TEXCOORD0;
	float3 WorldPos: TEXCOORD1;
	float4 lpos : TEXCOORD3; // https://takinginitiative.wordpress.com/2011/05/15/directx10-tutorial-10-shadow-mapping/
	float fogFactor: FOGFACTOR;
};

struct PS_INPUT_AIRPORT
{
    float4 Pos : SV_POSITION;
	float3 Norm : TEXCOORD2;
    float2 Tex : TEXCOORD0;
	float3 WorldPos: TEXCOORD1;
	float4 lpos : TEXCOORD3; // https://takinginitiative.wordpress.com/2011/05/15/directx10-tutorial-10-shadow-mapping/
	float4 lpos1: TEXCOORD4; 
	float fogFactor: FOGFACTOR;
};

struct PS_INPUT_SHADOW
{
    float4 Pos : SV_POSITION;
};

//--------------------------------------------------------------------------------------
// Vertex Shader
//--------------------------------------------------------------------------------------
PS_INPUT VS_Solid( VS_INPUT input )
{
    PS_INPUT output;
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm, (float3x3)World );
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	output.lpos = float4(0, 0, 0, 0);
    
    return output;
}

PS_INPUT_GROUNDOBJECT VS_GroundObject( VS_INPUT input )
{
    PS_INPUT_GROUNDOBJECT output;
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm, (float3x3)World );
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	   
    // Calculate fog factor for exponential fog
    float3 LookVec = PosWorld - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
	
	output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);
	
	// Shadowing
    input.Pos.xyz += NormalOffset * input.Norm;
	output.lpos = mul( input.Pos, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT VS_Moving_Flexing( VS_INPUT input )
{
    PS_INPUT output;
	// Moving geometry
	input.Pos = mul( input.Pos, Move );
	input.Norm = mul( input.Norm, Move );
	// Flexing geometry
	float posY = sign(input.Pos.y) * max(0, (abs(input.Pos.y) - 0.5)); // Don't let the first 0.5 meter from the centerline flex
	float Posy2 = posY * posY;
	float Posy3 = Posy2 * posY;
	input.Pos.z += Flex * Posy2 + Flutter * Posy3 * 0.1;
	input.Pos.y -= 0.5 * Flex * Flex * Posy3;
	matrix ViewProj = mul(View, Projection);
	float4 PosWorld = mul( input.Pos, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm, (float3x3)World );
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	   
    return output;
}

PS_INPUT VS_Solid_SampleShadowMap( VS_INPUT input )
{
    PS_INPUT output;
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos, World );
	output.Pos = mul( PosWorld, ViewProj );
    if (BumpMap == true)
	{
		output.Tangent = mul( frac(input.Norm), (float3x3)World ); // Input tangent is encoded as the fracional part of normal
		input.Norm = 0.001 * trunc(input.Norm);		
		output.Norm = mul( input.Norm, (float3x3)World ); // Input normal is encoded as the normal multiplied by 1000
		output.Binormal = cross(output.Norm, output.Tangent);
	}
	else
	{
		output.Norm = mul( input.Norm, (float3x3)World ); // We only need the normal if there is no bump mapping
	}
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
	// Shadowing
    input.Pos.xyz += NormalOffset * input.Norm;
	output.lpos = mul( input.Pos, mul( World, ViewProjLight ) );
    
    return output;
}

PS_INPUT VS_Moving_Flexing_SampleShadowMap( VS_INPUT input )
{
    PS_INPUT output;

	// Moving geometry
	input.Pos = mul( input.Pos, Move );
	input.Norm = mul( input.Norm, Move );
    // Flexing geometry
    float posY = sign(input.Pos.y) * max(0, (abs(input.Pos.y) - 0.5)); // Don't let the first 0.5 meter from the centerline flex
	float Posy2 = posY * posY;
	float Posy3 = Posy2 * posY;
	input.Pos.z += Flex * Posy2 + Flutter * Posy3 * 0.1;
	input.Pos.y -= 0.5 * Flex * Flex * Posy3;
	matrix ViewProj = mul(View, Projection);
	float4 PosWorld = mul( input.Pos, World );
	output.Pos = mul( PosWorld, ViewProj );
    if (BumpMap == true)
	{
		output.Tangent = mul( frac(input.Norm), (float3x3)World ); // Input tangent is encoded as the fracional part of normal
		input.Norm = 0.001 * trunc(input.Norm);		
		output.Norm = mul( input.Norm, (float3x3)World ); // Input normal is encoded as the normal multiplied by 1000
		output.Binormal = cross(output.Norm, output.Tangent);
	}
	else
	{
		output.Norm = mul( input.Norm, (float3x3)World ); // We only need the normal if there is no bump mapping
	}
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
    // Shadowing
    input.Pos.xyz += NormalOffset * input.Norm;
	output.lpos = mul( input.Pos, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_AIRPORT VS_Solid_Airport_SampleShadowMap( VS_INPUT input )
{
    PS_INPUT_AIRPORT output;
	float4 PosFlat = input.Pos; // The edge lid is used only for smooth fadeout, we need to flatten the geometry
	// PosFlat.z = 0;
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( PosFlat, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm, (float3x3)World );
    output.Tex = input.Tex;
	output.WorldPos = mul( input.Pos, World );
	output.WorldPos.z = input.Pos.z;
	    
	// Shadowing
    input.Pos.xyz += NormalOffset * input.Norm;
	output.lpos = mul( input.Pos, mul( World, ViewProjLight ) );    
    output.lpos1 = mul( input.Pos, mul( World, ViewProjLight1 ) );  

    // Calculate fog factor for exponential fog
    float3 LookVec = PosWorld - CameraPos;
	float fDistanceSqr = LookVec.x * LookVec.x + LookVec.y * LookVec.y + LookVec.z * LookVec.z;
	output.fogFactor = saturate(1.1 * exp(-0.000000001 * fDistanceSqr) - 0.1);
    
    return output;
}

PS_INPUT VS_AnimationPos2x( VS_INPUT_Pos2x input )
{
    PS_INPUT output;
	
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight1);
	input.Norm1 = lerp(input.Norm0, input.Norm1, Weight1);
	
	// Transformation
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos1, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm1, (float3x3)World );
	output.Tangent = float3(0, 0, 0);
	output.Binormal = float3(0, 0, 0);
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
    // Shadowing
    input.Pos1.xyz += NormalOffset * input.Norm1;
	output.lpos = mul( input.Pos1, mul( World, ViewProjLight ) );
    
    return output;
}

PS_INPUT VS_AnimationPos3x( VS_INPUT_Pos3x input )
{
    PS_INPUT output;
	// Mesh morphing
	input.Pos2 = lerp(input.Pos1, input.Pos2, Weight2);
	input.Pos1 = lerp(input.Pos0, input.Pos2, Weight1);
	input.Norm2 = lerp(input.Norm1, input.Norm2, Weight2);
	input.Norm1 = lerp(input.Norm0, input.Norm2, Weight1);
	
	// Transformation
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos1, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm1, (float3x3)World );
	output.Tangent = float3(0, 0, 0);
	output.Binormal = float3(0, 0, 0);	
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
    // Shadowing
    input.Pos1.xyz += NormalOffset * input.Norm1;
	output.lpos = mul( input.Pos1, mul( World, ViewProjLight ) );
    
    return output;
}

PS_INPUT VS_AnimationPos4x( VS_INPUT_Pos4x input )
{
    PS_INPUT output;
	
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight2);
	input.Pos3 = lerp(input.Pos2, input.Pos3, Weight2);
	input.Pos1 = lerp(input.Pos1, input.Pos3, Weight1);
	input.Norm1 = lerp(input.Norm0, input.Norm1, Weight2);
	input.Norm3 = lerp(input.Norm2, input.Norm3, Weight2);
	input.Norm1 = lerp(input.Norm1, input.Norm3, Weight1);
	
	// Transformation
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos1, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm1, (float3x3)World );
	output.Tangent = float3(0, 0, 0);
	output.Binormal = float3(0, 0, 0);	
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
    // Shadowing
    input.Pos1.xyz += NormalOffset * input.Norm1;
	output.lpos = mul( input.Pos1, mul( World, ViewProjLight ) );
    
    return output;
}

PS_INPUT VS_AnimationPos6x( VS_INPUT_Pos6x input )
{
    PS_INPUT output;
	
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight2);
	input.Pos3 = lerp(input.Pos2, input.Pos3, Weight2);
	input.Pos1 = lerp(input.Pos1, input.Pos3, Weight1);
	input.Pos5 = lerp(input.Pos4, input.Pos5, Weight4);
	input.Pos1 = lerp(input.Pos1, input.Pos5, Weight3);
	
	input.Norm1 = lerp(input.Norm0, input.Norm1, Weight2);
	input.Norm3 = lerp(input.Norm2, input.Norm3, Weight2);
	input.Norm1 = lerp(input.Norm1, input.Norm3, Weight1);
	input.Norm5 = lerp(input.Norm4, input.Norm5, Weight4);
	input.Norm1 = lerp(input.Norm1, input.Norm5, Weight3);
	
	// Transformation
	matrix ViewProj = mul( View, Projection );
	float4 PosWorld = mul( input.Pos1, World );
	output.Pos = mul( PosWorld, ViewProj );
    output.Norm = mul( input.Norm1, (float3x3)World );
	output.Tangent = float3(0, 0, 0);
	output.Binormal = float3(0, 0, 0);	
    output.Tex = input.Tex;
	output.WorldPos = (float3)PosWorld;
	
    // Shadowing
    input.Pos1.xyz += NormalOffset * input.Norm1;
	output.lpos = mul( input.Pos1, mul( World, ViewProjLight ) );

    return output;
}

//--------------------------------------------------------------------------------------
// Pixel Shader
//--------------------------------------------------------------------------------------
float4 PS( PS_INPUT input) : SV_Target
{   
	// LIGHTING
	float3 N = normalize(input.Norm);
	float3 V = normalize(CameraPos - input.WorldPos);

	// Ambient lighting
	float ambientIntensity;
	float ambientDot = saturate(-dot(N, float3(0, 0, 1))); // Ambient light is always from above cockpit in local frame! Only take into account reduced shading at the bottom
	ambientIntensity = 0.8 - 0.3 * ambientDot;
		
    // Self illumination
    float selfIllumination = 0;
	if (MaterialColor.r > 1) // Flag for self illumination
	{
	  selfIllumination = MaterialColor.r - 1;
	}		
		
	// Diffuse + ambient lighting
	float diffuseIntensity = saturate(dot(N, -LightDir));
	float4 LightingColor = pow(selfIllumination + (1 - selfIllumination) * (ShadeFactor * LightColor * diffuseIntensity + AmbientColor * ambientIntensity), 0.7); // 0.7 is a sort of GAMMA control
    
	// Texture
	float4 TextureColor;
    if (WrapScale > 1)
	  { TextureColor = txDiffuse.Sample( samLinearWrap, input.Tex * WrapScale); }
	else
	  { TextureColor = txDiffuse.Sample( samLinear, input.Tex ); }
    clip(TextureColor.a - AlphaRef);
	
	// Output color
	float4 OutputColor = saturate(LightingColor * saturate(MaterialColor) * TextureColor);
	OutputColor.xyz = OutputColor.xyz * (1 - EnvironmentalStrength);
	
	// Environmental mapping
    if ((EnvironmentalStrength < 0) && ((MaterialColor.x == 1) || (MaterialColor.y == 1) || (MaterialColor.z == 1))) // LED lights
      { return(MaterialColor); }
	 
	float3 Re = reflect(V, N);
	Re = mul(Re, LWMatrix);
    OutputColor.xyz += TextureColor.a * saturate(EnvironmentalStrength) * txCubeMap.Sample(samLinear, Re);
	
	// Specular lighting
	if (SpecularStrength > 0)
	{ 
	  float3 Rs = reflect(LightDir, N);
	  float dotRsV = saturate(dot(Rs, V));
	  OutputColor += TextureColor.a * SpecularStrength * LightColor * ShadeFactor * (0.5 * pow( dotRsV, SpecularShininess * 4 ) + 0.5 * pow( dotRsV, SpecularShininess * 16 ));
	}  

    return saturate(OutputColor);
}

float getShadowFactor(float4 lpos, Texture2D txShadowMap, bool PCF)
{
	// re-homogenize position after interpolation
    lpos.xyz /= lpos.w;
	 
    // if position is outside shadow volume - don't shadow it
	if( lpos.x < -1.0f || lpos.x > 1.0f ||
        lpos.y < -1.0f || lpos.y > 1.0f ||
        lpos.z < 0.0f  || lpos.z > 1.0f ) 
	{
	  return 1;
	}
	else
	{
      //transform clip space coords to texture space coords (-1:1 to 0:1)
      lpos.x = 0.5 * lpos.x + 0.5;
      lpos.y = -0.5 * lpos.y + 0.5;
 
      // Basic hardware PCF - single texel
      if (PCF == false)
      {
        return txShadowMap.SampleCmpLevelZero( cmpSampler, lpos.xy, lpos.z );
      }
      else
      {     
        // perform PCF filtering on a 4 x 4 texel neighborhood
        float sum = 0;
        float x, y;
        float fScale = 0.5f/2048;
        for (y = -3; y <= 3; y += 2.0)
        {
          for (x = -3; x <= 3; x += 2.0)
          {
            sum += txShadowMap.SampleCmpLevelZero( cmpSampler, lpos.xy + float2(x * fScale, y * fScale), lpos.z );
          }
        }
        return sum / 16;	 
      }
	}	
 }

float4 PS_SampleShadowMap( PS_INPUT input) : SV_Target
{	
	float4 TextureColor; // Diffuse Texture
	float3 BM = 0; // Bump mapping texture	
    if (WrapScale > 1)
	  { 
        TextureColor = txDiffuse.Sample( samLinearWrap, input.Tex * WrapScale); 
		if (BumpMap == true)
		  { BM = 2.0 * txBM.Sample(samLinearWrap, input.Tex * WrapScale).rgb - 1; } // Remap to [-1, 1]
	  }
	else
	  { 
        TextureColor = txDiffuse.Sample( samLinear, input.Tex ); 
        if (BumpMap == true)
		  { BM = 2.0 * txBM.Sample(samLinear, input.Tex).rgb; } // Remap to [-1, 1]
      }
    clip(TextureColor.a - AlphaRef);	
		
	// Shadow
	float shadowFactor = getShadowFactor(input.lpos, txDiffuseShadow, true);
	
	// LIGHTING	
	float3 N;
	if (BumpMap == true)
	{
		N = normalize(BM.x * input.Tangent + BM.y * input.Binormal + BM.z * input.Norm);
	}
	else
	{ 	
		N = normalize(input.Norm); 
	}
	float3 V = normalize(CameraPos - input.WorldPos);

	// Ambient lighting
	float ambientIntensity;
	float ambientZFactor = 1;
	if (CockpitView == true)
	{
	  float ambientDot = dot(N, float3(0, 0, 1)); // Ambient light is always from above cockpit in local frame!
	  ambientZFactor = 0.6 - 0.4 * AmbientDir.z;    	
	  ambientIntensity = 0.8 + 0.3 * ambientDot * ambientZFactor;
	}
	else
	{
	  float ambientDot = saturate(-dot(N, float3(0, 0, 1))); // Only take into account reduced shading at the bottom
	  ambientIntensity = 0.8 - 0.3 * ambientDot;
	}
  	
	// Ambient occlusion texture
	float4 AO;
	float ambientIntensityAO;
	if (shadowFactor * ShadeFactor < 0.99)
	{
	  AO = txAO.Sample(samLinear, input.Tex);
	  ambientIntensityAO = ambientIntensity * (AO.r + AO.g + AO.b) / 3;  
	  ambientIntensityAO = ambientIntensity + (ambientIntensityAO - ambientIntensity) * (1 - shadowFactor * ShadeFactor);
    }
    else
	{ 
      ambientIntensityAO = ambientIntensity;
	}	

    // Self illumination
    float selfIllumination = 0;
	if (MaterialColor.r > 1) // Flag for self illumination
	{
	  selfIllumination = MaterialColor.r - 1;
	}

	// Self illumination + diffuse + ambient lighting
	float diffuseIntensity = saturate(dot(N, -LightDir));
	float4 LightingColor = pow(selfIllumination + (1 - selfIllumination) * (shadowFactor * ShadeFactor * LightColor * diffuseIntensity + AmbientColor * ambientIntensityAO), 0.7); // 0.7 is a sort of GAMMA control
	LightingColor.a = 1;
	   
	// Output color
	float4 OutputColor = saturate(LightingColor * saturate(MaterialColor) * TextureColor);
	
	// Environmental mapping
	if (EnvironmentalStrength > 1) // Flag for reflection
	{ 
	  // Alpha of OutputColor is not changed!
      OutputColor.xyz = 5 * (EnvironmentalStrength - 1) * LightColor * (0.3 + 0.7 * ShadeFactor) * txReflectionOrDetail.Sample(samLinear, float2(1.2 * V.y + 0.5, 1.2 * V.z));
	}
	else // Normal environment map
	{ 
	  OutputColor.xyz = OutputColor.xyz * (1 - EnvironmentalStrength);
	  if ((EnvironmentalStrength < 0) && ((MaterialColor.x == 1) || (MaterialColor.y == 1) || (MaterialColor.z == 1))) // LED lights
        { return(MaterialColor); }
 
      float3 Re = reflect(V, N);
	  Re = mul(Re, LWMatrix);
      OutputColor.xyz += TextureColor.a * LightColor * saturate(EnvironmentalStrength) * txCubeMap.Sample(samLinear, Re);
	}
	
	// Specular lighting
	if (SpecularStrength > 0)
	{ 
	  float3 Rs = reflect(LightDir, N);
	  float3 AmbientRs = reflect(float3(0, 0, -1), N);
	  float dotRsV = saturate(dot(Rs, V));
	  float dotAmbientRsV = saturate(dot(AmbientRs, V));
	  if (CockpitView == true) // TextureColor.a is there only because of RCN alpha
	    {OutputColor += TextureColor.a * SpecularStrength * LightColor * (0.3 * ambientZFactor * pow( dotAmbientRsV, SpecularShininess * 4 ) + 0.7 * shadowFactor * ShadeFactor * pow( dotRsV, SpecularShininess * 16 ));}
	  else
	    {OutputColor += TextureColor.a * SpecularStrength * LightColor * shadowFactor * ShadeFactor * (0.5 * pow( dotRsV, SpecularShininess * 4 ) + 0.5 * pow( dotRsV, SpecularShininess * 16 ));}
	}  

	return saturate(OutputColor);
}

float4 PS_GroundShadow( PS_INPUT input) : SV_Target
{
  return float4(0, 0, 0, MaterialColor.a);
}

float4 PS_AirportSurface_SampleShadowMap( PS_INPUT_AIRPORT input) : SV_Target
{
    // Min shadow factor of Avion and Towplane
    float shadowFactor = min(getShadowFactor(input.lpos, txDiffuseShadow, true), getShadowFactor(input.lpos1, txDiffuseShadow1, true));   
    
    // LIGHTING
	float3 N = float3(0, 0, 1);

	// Diffuse lighting
    float lightIntensity = saturate(dot(N, -LightDir));
   	float4 LightingColor = saturate(LightColor * lightIntensity + AmbientColor);
	float4 TextureColor = txDiffuse.Sample( samLinear, input.Tex ); TextureColor.a = 1;
	float4 DetailColor = txReflectionOrDetail.Sample( samLinearWrap, float2(input.Tex.x / (ScaleX * 2), input.Tex.y / (ScaleY * 2)));
    float4 BigDetailColor = txAO.Sample( samLinearWrap, float2(input.Tex.x / (ScaleX * 20), input.Tex.y / (ScaleY * 20))); BigDetailColor.a = 1;
	
  	// Adjust color based on cloud shadows
    float2 ShadowTexPos = float2(((input.WorldPos.x * ShadowMapFactor) + 1) * 0.5, ((input.WorldPos.y * ShadowMapFactor) + 1) * 0.5);
    float CloudShadow = 0.6 + 0.4 * txCloudShadow.SampleLevel(samLinear, ShadowTexPos, 0).x;	
	
	// Output color
	float4 OutputColor;
    OutputColor = saturate(LightingColor * MaterialColor * TextureColor * (0.5 + DetailColor) * (0.5 + BigDetailColor));
	OutputColor.xyz = OutputColor.xyz * CloudShadow * (0.6 + 0.4 * shadowFactor);  
    	
    // Calculate the final color using the fog effect equation. Simply use fogfactor for alpha!
	OutputColor = lerp(FogColor, OutputColor, input.fogFactor);
	OutputColor.a = input.fogFactor * MaterialColor.a;
	
	return OutputColor;
}

float4 PS_GroundObject( PS_INPUT_GROUNDOBJECT input) : SV_Target
{
	// Sampletexture and early Alpha test
	float4 TextureColor = txDiffuse.Sample( samLinearWrap, input.Tex );
	clip(TextureColor.a - AlphaRef);
	
	// Diffuse lighting
    float3 N = normalize(input.Norm);
    float lightIntensity = saturate(dot(N, -LightDir));
   	float4 LightingColor = saturate(LightColor * lightIntensity + AmbientColor);
	
	// Adjust color based on cloud shadows
    float2 ShadowTexPos = float2(((input.WorldPos.x * ShadowMapFactor) + 1) * 0.5, ((input.WorldPos.y * ShadowMapFactor) + 1) * 0.5);
    float CloudShadow = 0.6 + 0.4 * txCloudShadow.SampleLevel(samLinear, ShadowTexPos, 0).x;	

	// Output color - Use also fogfactor for alpha!
	float4 OutputColor = lerp(FogColor, LightingColor * CloudShadow * MaterialColor * TextureColor, input.fogFactor);
	OutputColor.a = TextureColor.a * MaterialColor.a * input.fogFactor;
	
	return OutputColor;
}

float4 PS_Canopy( PS_INPUT input) : SV_Target
{
    float shadowFactor = getShadowFactor(input.lpos, txDiffuseShadow, true);
	
	// LIGHTING
	float3 N = normalize(input.Norm);
	float3 V = normalize(CameraPos - input.WorldPos);

	// Diffuse + ambient lighting
	float4 LightingColor = pow(ShadeFactor * shadowFactor * LightColor + AmbientColor, 0.7); // 0.7 is a sort of GAMMA control
    
	// Texture
    float4 TextureColor = txDiffuse.Sample( samLinearWrap, input.Tex ); 
    clip(TextureColor.a - AlphaRef);
	
	// Output color
	float4 OutputColor = saturate(MaterialColor * TextureColor);
	float Dot = dot(N, V);
	OutputColor.a = pow((1 - Dot), 1.0) * MaterialColor.a * TextureColor.a * (0.5 + 0.5 * ShadeFactor) * (0.5 + 0.5 * shadowFactor);

	return saturate(OutputColor);
}

float4 PS_Lightning( PS_INPUT input) : SV_Target
{
	return saturate(float4(0.9, 0.95, 1, 1));
}

// ****************************************************************
// VERTEX AND (VOID) PIXEL SHADER FOR SHADOW MAP GENERATION
// ****************************************************************

PS_INPUT_SHADOW VS_ShadowMap_Solid( VS_INPUT input )
{
    PS_INPUT_SHADOW output;
    
    output.Pos = mul( input.Pos, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_SHADOW VS_ShadowMap_Moving_Flexing( VS_INPUT input )
{
    PS_INPUT_SHADOW output;

    // Moving geometry
	input.Pos = mul( input.Pos, Move );
	// Flexing geometry
	float posY = sign(input.Pos.y) * max(0, abs(input.Pos.y) - 0.5); // Don't let the first 0.5 meter from the centerline flex
	float Posy2 = posY * posY;
	float Posy3 = Posy2 * posY;
	input.Pos.z += Flex * Posy2 + Flutter * Posy3 * 0.1;
	input.Pos.y -= 0.5 * Flex * Flex * Posy3;

    output.Pos = mul( input.Pos, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_SHADOW VS_ShadowMap_AnimationPos2x( VS_INPUT_Pos2x input )
{
    PS_INPUT_SHADOW output;
    
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight1);
	
    output.Pos = mul( input.Pos1, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_SHADOW VS_ShadowMap_AnimationPos3x( VS_INPUT_Pos3x input )
{
    PS_INPUT_SHADOW output;
    
	// Mesh morphing
	input.Pos2 = lerp(input.Pos1, input.Pos2, Weight2);
	input.Pos1 = lerp(input.Pos0, input.Pos2, Weight1);
	
    output.Pos = mul( input.Pos1, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_SHADOW VS_ShadowMap_AnimationPos4x( VS_INPUT_Pos4x input )
{
    PS_INPUT_SHADOW output;
    
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight2);
	input.Pos3 = lerp(input.Pos2, input.Pos3, Weight2);
	input.Pos1 = lerp(input.Pos1, input.Pos3, Weight1);
	
    output.Pos = mul( input.Pos1, mul( World, ViewProjLight ) );
	
    return output;
}

PS_INPUT_SHADOW VS_ShadowMap_AnimationPos6x( VS_INPUT_Pos6x input )
{
    PS_INPUT_SHADOW output;
    
	// Mesh morphing
	input.Pos1 = lerp(input.Pos0, input.Pos1, Weight2);
	input.Pos3 = lerp(input.Pos2, input.Pos3, Weight2);
	input.Pos1 = lerp(input.Pos1, input.Pos3, Weight1);
	input.Pos5 = lerp(input.Pos4, input.Pos5, Weight4);
	input.Pos1 = lerp(input.Pos1, input.Pos5, Weight3);
	
    output.Pos = mul( input.Pos1, mul( World, ViewProjLight ) );
	
    return output;
}

void PS_Void( PS_INPUT_SHADOW input) 
{
}

//--------------------------------------------------------------------------------------
technique10 Render_Solid
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );
    }
}

technique10 Render_Moving_Flexing
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Moving_Flexing() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS() ) );
    }
}

technique10 Render_Solid_SampleShadowMap
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid_SampleShadowMap() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_Moving_Flexing_SampleShadowMap
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Moving_Flexing_SampleShadowMap() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_Solid_GroundShadow
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_GroundShadow() ) );
    }
}

technique10 Render_AirportSurface
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid_Airport_SampleShadowMap() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_AirportSurface_SampleShadowMap() ) );
    }
}

technique10 Render_GroundObject
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_GroundObject() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_GroundObject() ) );
    }
}

technique10 Render_Canopy
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid_SampleShadowMap() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Canopy() ) );
    }
}

technique10 Render_Lightning
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Solid() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Lightning() ) );
    }
}

technique10 Render_Moving_Flexing_GroundShadow
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_Moving_Flexing() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_GroundShadow() ) );
    }
}

technique10 Render_ShadowMap_Solid
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_Solid() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}

technique10 Render_ShadowMap_Moving_Flexing
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_Moving_Flexing() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}

technique10 Render_AnimationPos2x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_AnimationPos2x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_ShadowMap_AnimationPos2x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_AnimationPos2x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}

technique10 Render_AnimationPos3x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_AnimationPos3x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_ShadowMap_AnimationPos3x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_AnimationPos3x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}

technique10 Render_AnimationPos4x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_AnimationPos4x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_ShadowMap_AnimationPos4x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_AnimationPos4x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}

technique10 Render_AnimationPos6x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_AnimationPos6x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_SampleShadowMap() ) );
    }
}

technique10 Render_ShadowMap_AnimationPos6x
{
    pass P0
    {
        SetVertexShader( CompileShader( vs_4_0, VS_ShadowMap_AnimationPos6x() ) );
        SetGeometryShader( NULL );
        SetPixelShader( CompileShader( ps_4_0, PS_Void() ) );
    }
}