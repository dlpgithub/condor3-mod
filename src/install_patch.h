#ifndef INSTALL_PATCH_H
#define INSTALL_PATCH_H

#include <windows.h>

#ifdef __cplusplus
extern "C" {
#endif

// Modul-Handle unserer eigenen DLL (wird in DllMain gesetzt).
// Wird u.a. gebraucht, um den DLL-eigenen Pfad zu ermitteln (Flag-Datei).
extern HINSTANCE g_hModule;

// Die Adresse, von der IDA beim statischen Laden ausgeht (siehe IDA:
// Edit -> Segments, oder PE-Header "ImageBase"). Für die meisten alten
// 32-bit-EXEs ohne ASLR ist das 0x00400000. BEI EUCH PRÜFEN, ggf. anpassen!
#ifndef IDA_IMAGE_BASE
#define IDA_IMAGE_BASE 0x00400000ULL
#endif

// Liefert die tatsächliche Ladeadresse der Haupt-EXE zur Laufzeit.
// (GetModuleHandle(NULL) gibt das Modul-Handle des Hauptprozesses zurück,
// das ist bei EXEs numerisch identisch mit der Basisadresse im Speicher.)
static inline uintptr_t GetMainModuleBase(void) {
    return (uintptr_t)GetModuleHandle(NULL);
}

// Rechnet eine IDA-Adresse (die du z.B. im Disassembler abliest, z.B.
// 0x00693ED0) in die tatsächliche Laufzeitadresse um, indem der RVA
// (Offset relativ zu IDA_IMAGE_BASE) auf die echte Basisadresse addiert wird.
//
// Beispiel: IDA zeigt 0x00693ED0, IDA_IMAGE_BASE ist 0x00400000
//   -> RVA = 0x00693ED0 - 0x00400000 = 0x00293ED0
//   -> reale Adresse = GetMainModuleBase() + 0x00293ED0
static inline void* IdaAddrToRuntime(uintptr_t ida_addr) {
    uintptr_t rva = ida_addr - (uintptr_t)IDA_IMAGE_BASE;
    return (void*)(GetMainModuleBase() + rva);
}

// Installiert alle Hooks/Patches. Wird aus DllMain (DLL_PROCESS_ATTACH)
// heraus aufgerufen, NACHDEM DbgLogInit() gelaufen ist, damit Logging
// bereits verfügbar ist.
void InstallPatches(void);

#ifdef __cplusplus
}
#endif

#endif // INSTALL_PATCH_H