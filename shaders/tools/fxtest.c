// fxtest - compile an .fx file exactly the way Condor 3 does.
//
// Condor calls D3DX11CompileFromMemory(src, len, NULL, {NULL,NULL}, NULL, NULL,
// "fx_5_0", 0x800 /*strictness*/, 0, ...) from the game's own d3dx11_43.dll.
// Run this tool from inside the Condor 3 folder (through CrossOver, same bottle)
// so it loads the very same d3dx11_43.dll / D3DCompiler_43.dll as the game.
// (The fxc.exe that ships with Condor is 64-bit and cannot be used for this.)
//
//   fxtest.exe EffectLand.fx [more.fx ...]      exit code 0 = all compiled
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct { const char* Name; const char* Definition; } MACRO;
typedef struct ID3D10Blob ID3D10Blob;
struct ID3D10BlobVtbl {
    HRESULT (WINAPI *QueryInterface)(ID3D10Blob*, const void*, void**);
    ULONG   (WINAPI *AddRef)(ID3D10Blob*);
    ULONG   (WINAPI *Release)(ID3D10Blob*);
    void*   (WINAPI *GetBufferPointer)(ID3D10Blob*);
    SIZE_T  (WINAPI *GetBufferSize)(ID3D10Blob*);
};
struct ID3D10Blob { const struct ID3D10BlobVtbl* v; };

typedef HRESULT (WINAPI *PFN_Compile)(LPCVOID, SIZE_T, LPCSTR, const MACRO*, void*, LPCSTR,
                                      LPCSTR, UINT, UINT, void*, ID3D10Blob**, ID3D10Blob**, HRESULT*);

int main(int argc, char** argv) {
    if (argc < 2) { fprintf(stderr, "usage: fxtest file.fx [...]\n"); return 2; }
    HMODULE m = LoadLibraryA("d3dx11_43.dll");
    if (!m) { fprintf(stderr, "cannot load d3dx11_43.dll (run from the Condor3 folder)\n"); return 2; }
    PFN_Compile compile = (PFN_Compile)GetProcAddress(m, "D3DX11CompileFromMemory");
    if (!compile) { fprintf(stderr, "D3DX11CompileFromMemory not found\n"); return 2; }

    int failed = 0;
    for (int i = 1; i < argc; i++) {
        FILE* f = fopen(argv[i], "rb");
        if (!f) { printf("%-28s cannot open\n", argv[i]); failed++; continue; }
        fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
        char* src = (char*)malloc(n + 1);
        fread(src, 1, n, f); src[n] = 0; fclose(f);

        MACRO defs[2] = { { NULL, NULL }, { NULL, NULL } };
        ID3D10Blob *code = NULL, *err = NULL;
        HRESULT hr2 = 0;
        DWORD t0 = GetTickCount();
        HRESULT hr = compile(src, (SIZE_T)n, NULL, defs, NULL, NULL, "fx_5_0", 0x800, 0, NULL, &code, &err, &hr2);
        DWORD ms = GetTickCount() - t0;
        if (FAILED(hr) || !code) {
            printf("%-28s FAILED (hr=0x%08lx, %lu ms)\n", argv[i], (unsigned long)hr, (unsigned long)ms);
            if (err) printf("%s\n", (const char*)err->v->GetBufferPointer(err));
            failed++;
        } else {
            printf("%-28s ok (%lu bytes, %lu ms)\n", argv[i], (unsigned long)code->v->GetBufferSize(code), (unsigned long)ms);
            if (err) { const char* w = (const char*)err->v->GetBufferPointer(err); if (w && *w) printf("  warnings:\n%s\n", w); }
        }
        free(src);
        fflush(stdout);
    }
    return failed ? 1 : 0;
}
