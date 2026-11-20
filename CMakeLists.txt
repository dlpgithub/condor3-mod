cmake_minimum_required(VERSION 3.15)
project(d3dx_proxy C CXX)

# Vista+ APIs (u.a. GetTickCount64) für MinGW sichtbar machen.
add_compile_definitions(_WIN32_WINNT=0x0600 WINVER=0x0600)

# Optionale Patches (per -DENABLE_...=OFF abschaltbar)
option(ENABLE_MOVE_PATCHES       "Delphi Move/FastMM patches" OFF)
option(ENABLE_HANGAR_PREVIEW_FIX "Hangar preview fix"         ON)
option(ENABLE_POSTFX             "Post-processing (SSAO, bloom, tone mapping)" ON)
option(ENABLE_AGSTREAM           "Stream autogen from the 64-bit helper (c3helper.exe)" ON)

# Dobby comes prebuilt (scripts/build_dobby.sh, run inside the dev container).
set(DOBBY_PREBUILT_DIR "${CMAKE_SOURCE_DIR}/deps/dobby-prebuilt"
    CACHE PATH "Directory containing libdobby.a")
option(DOBBY_FALLBACK_BUILD "Build Dobby from source if no prebuilt exists" OFF)

if(EXISTS "${DOBBY_PREBUILT_DIR}/libdobby.a")
    add_library(dobby_static STATIC IMPORTED GLOBAL)
    set_target_properties(dobby_static PROPERTIES
        IMPORTED_LOCATION "${DOBBY_PREBUILT_DIR}/libdobby.a"
        INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_SOURCE_DIR}/external/dobby-fork/include")
    # Re-run configure when the prebuilt gets refreshed
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
        "${DOBBY_PREBUILT_DIR}/stamp")
    message(STATUS "Using prebuilt Dobby: ${DOBBY_PREBUILT_DIR}/libdobby.a")
elseif(DOBBY_FALLBACK_BUILD)
    message(WARNING "No prebuilt Dobby, building from source (slow)")
    set(DOBBY_BUILD_SHARED_LIBRARY OFF CACHE BOOL "" FORCE)
    add_subdirectory(external/dobby-fork)
else()
    message(FATAL_ERROR
        "Prebuilt Dobby missing. Run scripts/build_dobby.sh inside the dev container "
        "(or configure with -DDOBBY_FALLBACK_BUILD=ON).")
endif()

set(D3DX_SOURCES
    src/d3dx_proxy_stub.c
    src/debug_log.c
    src/install_patch.c
    src/d3dx_proxy.def
)

if(ENABLE_MOVE_PATCHES)
    list(APPEND D3DX_SOURCES src/install_patch_move.c)
endif()
if(ENABLE_HANGAR_PREVIEW_FIX)
    list(APPEND D3DX_SOURCES src/patch_hangar_preview.c)
endif()
if(ENABLE_POSTFX)
    list(APPEND D3DX_SOURCES src/postfx_core.c src/postfx_hook.c)
endif()
if(ENABLE_AGSTREAM)
    list(APPEND D3DX_SOURCES src/agstream.c src/c3h_client.c)
endif()

add_library(d3dx_proxy SHARED ${D3DX_SOURCES})

if(ENABLE_MOVE_PATCHES)
    target_compile_definitions(d3dx_proxy PRIVATE ENABLE_MOVE_PATCHES)
endif()
if(ENABLE_HANGAR_PREVIEW_FIX)
    target_compile_definitions(d3dx_proxy PRIVATE ENABLE_HANGAR_PREVIEW_FIX)
endif()
if(ENABLE_POSTFX)
    target_compile_definitions(d3dx_proxy PRIVATE ENABLE_POSTFX)
endif()
if(ENABLE_AGSTREAM)
    target_compile_definitions(d3dx_proxy PRIVATE ENABLE_AGSTREAM)
endif()

set_target_properties(d3dx_proxy PROPERTIES
    OUTPUT_NAME "d3dx"
    PREFIX ""
    LINKER_LANGUAGE CXX
)

target_include_directories(d3dx_proxy PRIVATE external/dobby-fork/include)
target_link_libraries(d3dx_proxy PRIVATE dobby_static ws2_32)
target_link_options(d3dx_proxy PRIVATE -static-libgcc -static-libstdc++)

# Eigenständiges Debug-Monitor-Tool: zeigt Live-Logs der injizierten Proxy-DLL an.
# Läuft unabhängig vom Zielprozess, einfach vor dem Start des Spiels/der App starten.
add_executable(d3dx_proxy_monitor tools/monitor.c)
target_link_libraries(d3dx_proxy_monitor PRIVATE ws2_32)
target_link_options(d3dx_proxy_monitor PRIVATE -static-libgcc -static-libstdc++)

# Offline test bench for the post-processing chain (no game needed): tools/pfxtest.c
add_executable(pfxtest tools/pfxtest.c src/postfx_core.c)
target_link_libraries(pfxtest PRIVATE d3d11)
target_link_options(pfxtest PRIVATE -static-libgcc -static-libstdc++)
