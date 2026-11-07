#include "install_patch.h"
#include "dobby.h"

#include <stdint.h>
#include <string.h>

// -----------------------------------------------------------------------------
// x87 Move/FastMM copy patches
//
// These patches were meant to fix the underlying issue explained in:
//   https://github.com/Lifeisawful/rosettax87_jit/pull/13
//
// Background: Delphi's Move and FastMM's copy routines use fild/fistp qword,
// which the loader rounds to 53 bits. That corrupts pointers and strings.
// These patches replace those routines with versions that use no x87
// instructions.
//
// Status: on its own, this patch fixed Condor 3 not opening at all, but
// opening an actual flight in the sim still didn't work. The PR above fixes
// the underlying issue completely, so the game opens and is actually
// playable (Rosetta x87). This patch is not needed for that, but it can be
// used on top of the PR.
//
// Why it's still here: it can improve performance (not fully understood why).
// The effect seems much larger with Rosetta x87. It can be toggled via the
// compiler flags, so you can enable/disable it and compare your FPS.
// -----------------------------------------------------------------------------

#define EA_MOVE_RTL     0x0040335C  // Delphi Move
#define EA_MOVE_X12     0x00401660  // FastMM copy, Count = 16n+12
#define EA_MOVE_X4      0x00401690  // FastMM copy, Count = 8n+4
#define EA_UPSIZE_BLOCK 0x00401F5E  // movzx ecx, word ptr [ebx+2] (ebx = size-class record)

// Delphi register convention: eax=Src, edx=Dst, ecx=Count. memmove semantics, no x87.
__attribute__((naked, used, noinline)) static void MoveFixed(void) {
    __asm__ volatile(
        "cmpl %eax, %edx\n\t"
        "je 3f\n\t"
        "testl %ecx, %ecx\n\t"
        "jle 3f\n\t"
        "pushl %esi\n\t"
        "pushl %edi\n\t"
        "movl %eax, %esi\n\t"
        "movl %edx, %edi\n\t"
        "cmpl %esi, %edi\n\t"
        "jbe 1f\n\t"
        "leal (%esi,%ecx), %eax\n\t"
        "cmpl %eax, %edi\n\t"
        "jae 1f\n\t"
        "leal -1(%esi,%ecx), %esi\n\t"
        "leal -1(%edi,%ecx), %edi\n\t"
        "std\n\t"
        "rep movsb\n\t"
        "cld\n\t"
        "jmp 2f\n"
        "1:\n\t"
        "cld\n\t"
        "rep movsb\n"
        "2:\n\t"
        "popl %edi\n\t"
        "popl %esi\n"
        "3:\n\t"
        "ret\n"
    );
}

// Fixed-size copies (eax=Src, edx=Dst, ecx as scratch), dword moves only.
#define MV(o) "movl " #o "(%eax), %ecx\n\t" "movl %ecx, " #o "(%edx)\n\t"

__attribute__((naked, used, noinline)) static void Move36Fixed(void) {
    __asm__ volatile(MV(0) MV(4) MV(8) MV(12) MV(16) MV(20) MV(24) MV(28) MV(32) "ret\n");
}
__attribute__((naked, used, noinline)) static void Move44Fixed(void) {
    __asm__ volatile(MV(0) MV(4) MV(8) MV(12) MV(16) MV(20) MV(24) MV(28) MV(32) MV(36) MV(40) "ret\n");
}
__attribute__((naked, used, noinline)) static void Move52Fixed(void) {
    __asm__ volatile(MV(0) MV(4) MV(8) MV(12) MV(16) MV(20) MV(24) MV(28) MV(32) MV(36) MV(40) MV(44) MV(48) "ret\n");
}
__attribute__((naked, used, noinline)) static void Move60Fixed(void) {
    __asm__ volatile(MV(0) MV(4) MV(8) MV(12) MV(16) MV(20) MV(24) MV(28) MV(32) MV(36) MV(40) MV(44) MV(48) MV(52) MV(56) "ret\n");
}
__attribute__((naked, used, noinline)) static void Move68Fixed(void) {
    __asm__ volatile(MV(0) MV(4) MV(8) MV(12) MV(16) MV(20) MV(24) MV(28) MV(32) MV(36) MV(40) MV(44) MV(48) MV(52) MV(56) MV(60) MV(64) "ret\n");
}

// Overwrite the start of a function with "jmp target" (E9 rel32),
// but only if the expected original bytes are present.
static void PatchJmp(uintptr_t ida_addr, void (*target)(void),
                     const uint8_t* expectA, const uint8_t* expectB, size_t n) {
    uint8_t* a = (uint8_t*)IdaAddrToRuntime(ida_addr);
    if (IsBadReadPtr(a, 5)) return;
    if (memcmp(a, expectA, n) != 0 && (expectB == NULL || memcmp(a, expectB, n) != 0)) return;

    uint8_t jmp[5];
    jmp[0] = 0xE9;
    int32_t rel = (int32_t)((uintptr_t)target - ((uintptr_t)a + 5));
    memcpy(jmp + 1, &rel, 4);
    DobbyCodePatch(a, jmp, sizeof jmp);
}

// FastMM realloc: redirect UpsizeMoveProcedure ([ebx+1Ch]) to MoveFixed
// before the block executes "call [ebx+1Ch]".
static void OnUpsizeBlock(void* addr, DobbyRegisterContext* ctx) {
    (void)addr;
    uint32_t rec = ctx->general.regs.ebx;
    if (IsBadReadPtr((const void*)(uintptr_t)rec, 0x20)) return;
    volatile uint32_t* slot = (volatile uint32_t*)(uintptr_t)(rec + 0x1C);
    uint32_t mine = (uint32_t)(uintptr_t)MoveFixed;
    if (*slot == mine) return;
    if (IsBadWritePtr((void*)slot, 4)) return;
    *slot = mine;
}

void InstallMovePatches(void) {
    static const uint8_t cmp_eax_edx_a[]  = { 0x3B, 0xC2 };
    static const uint8_t cmp_eax_edx_b[]  = { 0x39, 0xD0 };
    static const uint8_t sub_ecx_0c[]     = { 0x83, 0xE9, 0x0C };
    static const uint8_t sub_ecx_04[]     = { 0x83, 0xE9, 0x04 };
    static const uint8_t fild_eax[]       = { 0xDF, 0x28 };
    static const uint8_t movzx_ecx_ebx2[] = { 0x0F, 0xB7, 0x4B, 0x02 };

    // Delphi Move and FastMM variable-size copies
    PatchJmp(EA_MOVE_RTL, MoveFixed, cmp_eax_edx_a, cmp_eax_edx_b, 2);
    PatchJmp(EA_MOVE_X12, MoveFixed, sub_ecx_0c, NULL, 3);
    PatchJmp(EA_MOVE_X4,  MoveFixed, sub_ecx_04, NULL, 3);

    // FastMM fixed-size copies (two sets)
    PatchJmp(0x00401588, Move36Fixed, fild_eax, NULL, 2);
    PatchJmp(0x004015A8, Move44Fixed, fild_eax, NULL, 2);
    PatchJmp(0x004015CC, Move52Fixed, fild_eax, NULL, 2);
    PatchJmp(0x004015F8, Move60Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00401628, Move68Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00409A00, Move36Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00409A20, Move44Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00409A44, Move52Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00409A70, Move60Fixed, fild_eax, NULL, 2);
    PatchJmp(0x00409AA0, Move68Fixed, fild_eax, NULL, 2);

    // FastMM realloc upsize-procedure redirect
    uint8_t* up = (uint8_t*)IdaAddrToRuntime(EA_UPSIZE_BLOCK);
    if (!IsBadReadPtr(up, 4) && memcmp(up, movzx_ecx_ebx2, 4) == 0)
        DobbyInstrument(up, OnUpsizeBlock);
}