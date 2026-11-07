#include "install_patch.h"
#include "dobby.h"

#ifdef ENABLE_MOVE_PATCHES
void InstallMovePatches(void);
#endif
#ifdef ENABLE_HANGAR_PREVIEW_FIX
void InstallHangarPreviewFix(void);
#endif
#ifdef ENABLE_POSTFX
void InstallPostFX(void);
#endif

void InstallPatches(void) {
#ifdef ENABLE_MOVE_PATCHES
    InstallMovePatches();
#endif
#ifdef ENABLE_HANGAR_PREVIEW_FIX
    InstallHangarPreviewFix();
#endif
#ifdef ENABLE_POSTFX
    InstallPostFX();
#endif
}