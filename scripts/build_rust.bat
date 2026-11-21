@echo off
rem Builds the Rust landscape compilers on Windows and copies them into bin\ (macOS / Linux: scripts/build_rust.sh).
rem   scripts\build_rust.bat             both: bin\tiles2condor_v2.exe and bin\tiles2condor.exe
rem   scripts\build_rust.bat v2          only tiles2condor_v2 (the current one)
rem   scripts\build_rust.bat v1          only tiles2condor (the old one)
rem   scripts\build_rust.bat all debug   debug build
rem Needs the Rust toolchain (https://rustup.rs, default MSVC toolchain with the "Desktop development with C++" build tools,
rem or the x86_64-pc-windows-gnu toolchain). The GPU code (wgpu) uses DX12 / Vulkan at run time, nothing else to install.
setlocal
set "ROOT=%~dp0.."
set "WHICH=%~1"
if "%WHICH%"=="" set "WHICH=all"
set "PROFILE=%~2"
if "%PROFILE%"=="" set "PROFILE=release"
if /i not "%WHICH%"=="all" if /i not "%WHICH%"=="v1" if /i not "%WHICH%"=="v2" goto usage
if /i not "%PROFILE%"=="release" if /i not "%PROFILE%"=="debug" goto usage
set "FLAG="
if /i "%PROFILE%"=="release" set "FLAG=--release"

set "CARGO=cargo"
where cargo >nul 2>nul || set "CARGO=%USERPROFILE%\.cargo\bin\cargo.exe"
if not "%CARGO%"=="cargo" if not exist "%CARGO%" (
  echo cargo not found. Install Rust from https://rustup.rs ^(rustup-init.exe^), then open a new terminal.
  exit /b 1
)
call "%CARGO%" --version || exit /b 1
if not exist "%ROOT%\bin" mkdir "%ROOT%\bin"

if /i not "%WHICH%"=="v1" call :build tiles2condor_v2 tiles2condor_v2 || exit /b 1
if /i not "%WHICH%"=="v2" call :build tiles2condor tiles2condor || exit /b 1
echo done. Usage: bin\tiles2condor_v2.exe ^<export_dir^> [...] ^<config.json^> ^<Landscapes_dir^>\^<Name^>   (see rust\tiles2condor_v2\README.md)
exit /b 0

:build
echo == %2 (%PROFILE%) ==
pushd "%ROOT%\rust\%1" || exit /b 1
call "%CARGO%" build %FLAG%
if errorlevel 1 (popd & exit /b 1)
popd
copy /y "%ROOT%\rust\%1\target\%PROFILE%\%2.exe" "%ROOT%\bin\%2.exe" >nul || exit /b 1
echo -^> bin\%2.exe
exit /b 0

:usage
echo usage: %~nx0 [all^|v1^|v2] [release^|debug]
exit /b 2
