// patch_hangar_preview.c
//
// Fix fuer Condor 3 unter Wine/CrossOver (macOS): Die 3D-Flugzeugvorschau des Flight Planners
// (HANGAR -> "3D view") wurde auf allen Reitern ueber den Inhalt gezeichnet.
//
// Ursache:
//   * Die Vorschau rendert per D3D11 in eine Swapchain, die an ein natives Child-Fenster
//     gebunden ist (TspSkinPanel "spSkinPanel1", Kind des Tabs "3D view").
//   * TPlaneViewer legt im Konstruktor (0x6C1B08) einen 10-ms-Timer an. Dessen Handler (0x6C1C84)
//     ruft Viewer.Render (0x6C227C) unbedingt auf, auch wenn der Tab nicht sichtbar ist.
//     Render endet mit IDXGISwapChain::Present.
//   * Unter Windows ist das unsichtbar. Unter Wine/macOS wird die Metal-Flaeche der Swapchain
//     unabhaengig vom Win32-Sichtbarkeitsstatus ueber den restlichen Inhalt gezeichnet.
//     Show/Hide-Zustandswechsel (ShowWindow) aendern daran nichts, nur die Fensterposition.
//
// Fix:
//   * Viewer.Render wird gehookt. Ist der Eltern-Tab des Swapchain-Panels nicht sichtbar,
//     wird Render (und damit Present) uebersprungen.
//   * Waehrenddessen wird das Panel ausserhalb des Bildschirms geparkt (-32000,-32000)
//     und beim Wiederanzeigen des Tabs an die urspruengliche Position zurueckgesetzt.
//
// Delphi-Register-Konvention: eax = Self. Render hat keine Stack-Argumente (endet mit plain 'ret').
// Dobby-Replacement-Funktionen erhalten eax nicht als Argument, daher ein kleiner Asm-Shim
// (GCC/Clang, i686). Bei MSVC muesste der Shim umgeschrieben werden.
//
// Adressen sind IDA-Adressen (ImageBase 0x00400000, kein ASLR), Umrechnung ueber IdaAddrToRuntime().

#include "install_patch.h"
#include "dobby.h"
#include "debug_log.h"

#include <stdint.h>

// ----------------------------------------------------------------------------
// Adressen und Strukturoffsets (aus Disassembly/Laufzeitlog ermittelt)
// ----------------------------------------------------------------------------
#define ADDR_VIEWER_RENDER   0x006C227C   // TPlaneViewer.Render (eax = Self, plain ret)
#define ADDR_SWAPMGR_PTR     0x00793998   // Zeiger auf Variable, die den Swapchain-Manager haelt
#define MGR_OFF_ITEMS        0x0C         // Manager+0xC : Array von Item-Zeigern
#define ITEM_OFF_HWND        0x04         // Item+4      : OutputWindow-HWND der Swapchain
#define PREVIEW_ITEM_INDEX   0            // Render waehlt Swapchain 0

#define PARK_POS             (-32000)     // Parkposition fuer das versteckte Panel

// ----------------------------------------------------------------------------
// Hilfsfunktionen
// ----------------------------------------------------------------------------
static BOOL SafeRead32(uintptr_t addr, uint32_t* out) {
    if (addr == 0 || IsBadReadPtr((const void*)addr, 4)) return FALSE;
    *out = *(volatile const uint32_t*)addr;
    return TRUE;
}

// HWND des Swapchain-Panels (Swapchain 0) oder NULL
static HWND GetPreviewHwnd(void) {
    uint32_t p1 = 0, mgr = 0, arr = 0, item = 0, hwnd = 0;
    if (!SafeRead32((uintptr_t)IdaAddrToRuntime(ADDR_SWAPMGR_PTR), &p1)) return NULL;  // doppelte Indirektion
    if (!SafeRead32(p1, &mgr)) return NULL;
    if (!SafeRead32(mgr + MGR_OFF_ITEMS, &arr)) return NULL;
    if (!SafeRead32(arr + PREVIEW_ITEM_INDEX * 4, &item)) return NULL;
    if (!SafeRead32(item + ITEM_OFF_HWND, &hwnd)) return NULL;
    return (HWND)(uintptr_t)hwnd;
}

// ----------------------------------------------------------------------------
// Render-Gate
// ----------------------------------------------------------------------------
static HWND  g_panel  = NULL;     // aktuelles Swapchain-Panel
static BOOL  g_parked = FALSE;    // Panel ist gerade ausserhalb geparkt
static POINT g_saved  = {0, 0};   // urspruengliche Position (Client-Koordinaten des Parents)

static void ParkPanel(HWND h, HWND parent) {
    RECT r;
    GetWindowRect(h, &r);
    g_saved.x = r.left;
    g_saved.y = r.top;
    ScreenToClient(parent, &g_saved);
    SetWindowPos(h, NULL, PARK_POS, PARK_POS, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    g_parked = TRUE;
    DBGLOG("hangar-preview: Tab versteckt, Panel %p geparkt (gemerkt %ld,%ld)", (void*)h, g_saved.x, g_saved.y);
}

static void UnparkPanel(HWND h) {
    SetWindowPos(h, NULL, g_saved.x, g_saved.y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    g_parked = FALSE;
    DBGLOG("hangar-preview: Tab sichtbar, Panel %p zurueck auf %ld,%ld", (void*)h, g_saved.x, g_saved.y);
}

// Rueckgabe 1 = Render ueberspringen, 0 = Original ausfuehren.
// Wird aus dem Asm-Shim (cdecl) aufgerufen. Muss global und 'used' sein.
__attribute__((used))
int hpf_RenderGate(void* self) {
    HWND h, parent;
    (void)self;

    h = GetPreviewHwnd();
    if (!h || !IsWindow(h)) return 0;        // kein gueltiges Ziel: nicht eingreifen
    parent = GetParent(h);
    if (!parent) return 0;

    if (h != g_panel) {                      // neues Panel (z.B. Planner erneut geoeffnet)
        g_panel = h;
        g_parked = FALSE;
    }

    if (IsWindowVisible(parent)) {           // Tab "3D view" ist sichtbar -> normal rendern
        if (g_parked) UnparkPanel(h);
        return 0;
    }

    // Tab nicht sichtbar: Panel parken (und geparkt halten, falls VCL es zurueckschiebt), Render ueberspringen
    if (!g_parked) {
        ParkPanel(h, parent);
    } else {
        RECT r;
        GetWindowRect(h, &r);
        if (r.left > PARK_POS / 2)
            SetWindowPos(h, NULL, PARK_POS, PARK_POS, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    }
    return 1;
}

// ----------------------------------------------------------------------------
// Asm-Shim: eax/ecx/edx sichern, hpf_RenderGate(eax) aufrufen, danach entweder
//   - Original per Tail-Jump (alle Register unveraendert), oder
//   - einfaches 'ret' (Render hat keine Stack-Argumente).
// i686-mingw praefixt C-Symbole mit '_', andere Targets nicht -> __USER_LABEL_PREFIX__.
// ----------------------------------------------------------------------------
#define HPF_STR_(x) #x
#define HPF_STR(x)  HPF_STR_(x)
#define HPF_SYM(x)  HPF_STR(__USER_LABEL_PREFIX__) #x

// Zeiger auf die Original-Funktion (Dobby-Trampolin). Global, da der Asm-Shim darauf zugreift.
void* hpf_orig_render = NULL;

__asm__(
    ".text\n"
    ".globl " HPF_SYM(hpf_render_shim) "\n"
    HPF_SYM(hpf_render_shim) ":\n"
    "    push %eax\n"
    "    push %ecx\n"
    "    push %edx\n"
    "    push %eax\n"                            // Argument: Self
    "    call " HPF_SYM(hpf_RenderGate) "\n"
    "    add  $4, %esp\n"
    "    test %eax, %eax\n"                      // Flags bleiben ueber die pops erhalten
    "    pop  %edx\n"
    "    pop  %ecx\n"
    "    pop  %eax\n"
    "    jnz  1f\n"
    "    jmp  *" HPF_SYM(hpf_orig_render) "\n"
    "1:  ret\n"
);
extern void hpf_render_shim(void);

// ----------------------------------------------------------------------------
// Einstiegspunkt (wird aus InstallPatches() aufgerufen)
// ----------------------------------------------------------------------------
void InstallHangarPreviewFix(void) {
    void* target = IdaAddrToRuntime(ADDR_VIEWER_RENDER);
    int r = DobbyHook(target, (dobby_dummy_func_t)hpf_render_shim, (dobby_dummy_func_t*)&hpf_orig_render);
    DBGLOG("hangar-preview: Hook auf Viewer.Render (%p) -> %s (%d)", target, r == 0 ? "OK" : "FEHLER", r);
    (void)r; (void)target;   // im Release-Build (NDEBUG) entfaellt DBGLOG
}