@echo off
rem Installs / removes the Condor 3 post-processing mod on Windows (the equivalent of scripts/postfx.sh).
rem
rem   scripts\postfx.bat install   "C:\path\to\Condor3"
rem   scripts\postfx.bat uninstall "C:\path\to\Condor3"
rem   scripts\postfx.bat sync      "C:\path\to\Condor3"     (copies only postfx.hlsl; the running game hot-reloads it)
rem
rem The second argument is the folder that contains Condor.exe. If it is left out, the environment variable
rem CONDOR_DIR is used.
rem
rem What install does:
rem   1. backs up the game's own d3dx.dll as d3dx.dll.before_postfx (only the first time)
rem   2. copies build\d3dx.dll (the prebuilt mod) over d3dx.dll
rem   3. creates <game>\postfx with postfx.hlsl and the colour LUT (postfx\user\lut.cube, else the default one)
rem   4. copies the patched shaders from shaders\build\Effects\fx into <game>\Effects\fx
rem
rem Needs the same Condor version the mod was made for (3.1.0): the mod patches fixed addresses in Condor.exe.
rem Keys in the game: F9 = effect on/off, F8 = reload shader, F10 = capture frame.
rem Log: <game>\postfx\postfx.log

setlocal
set "ROOT=%~dp0.."
set "CMD=%~1"
set "GAME=%~2"
if "%GAME%"=="" set "GAME=%CONDOR_DIR%"
if "%CMD%"=="" goto usage
if "%GAME%"=="" goto usage
if not exist "%GAME%\Condor.exe" (
  echo "%GAME%" does not contain Condor.exe - pass the Condor 3 folder as the second argument.
  exit /b 1
)
set "BAK=%GAME%\d3dx.dll.before_postfx"

if /i "%CMD%"=="install" goto install
if /i "%CMD%"=="uninstall" goto uninstall
if /i "%CMD%"=="sync" goto sync
goto usage

:install
if not exist "%ROOT%\build\d3dx.dll" (
  echo build\d3dx.dll is missing - copy the whole condor3-mod folder including build\.
  exit /b 1
)
tasklist /fi "imagename eq Condor.exe" 2>nul | find /i "Condor.exe" >nul
if not errorlevel 1 (
  echo Condor.exe is running - close it first, then run install again.
  exit /b 1
)
if not exist "%BAK%" copy /y "%GAME%\d3dx.dll" "%BAK%" >nul
copy /y "%ROOT%\build\d3dx.dll" "%GAME%\d3dx.dll" >nul || exit /b 1
if not exist "%GAME%\postfx" mkdir "%GAME%\postfx"
copy /y "%ROOT%\postfx\postfx.hlsl" "%GAME%\postfx\postfx.hlsl" >nul || exit /b 1
if not exist "%GAME%\postfx\lut.cube" (
  if exist "%ROOT%\postfx\user\lut.cube" (
    copy /y "%ROOT%\postfx\user\lut.cube" "%GAME%\postfx\lut.cube" >nul
  ) else (
    copy /y "%ROOT%\postfx\default_lut\lut.cube" "%GAME%\postfx\lut.cube" >nul
  )
)
if exist "%ROOT%\postfx\user\tune.txt" if not exist "%GAME%\postfx\tune.txt" copy /y "%ROOT%\postfx\user\tune.txt" "%GAME%\postfx\tune.txt" >nul
if not exist "%ROOT%\shaders\build\Effects\fx" (
  echo shaders\build\Effects\fx is missing - the shader patches were not copied.
  exit /b 1
)
if not exist "%GAME%\Effects\fx" mkdir "%GAME%\Effects\fx"
copy /y "%ROOT%\shaders\build\Effects\fx\*.fx" "%GAME%\Effects\fx\" >nul || exit /b 1
echo Installed. Start Condor normally.  F9 = effect on/off, F8 = reload shader, F10 = capture frame.
echo Log: %GAME%\postfx\postfx.log
echo Remove again: scripts\postfx.bat uninstall "%GAME%"
exit /b 0

:uninstall
tasklist /fi "imagename eq Condor.exe" 2>nul | find /i "Condor.exe" >nul
if not errorlevel 1 (
  echo Condor.exe is running - close it first, then run uninstall again.
  exit /b 1
)
if exist "%BAK%" move /y "%BAK%" "%GAME%\d3dx.dll" >nul
if exist "%GAME%\postfx" rmdir /s /q "%GAME%\postfx"
rem the game's own .fxc shaders are never modified; the mod only adds Effects\fx
if exist "%GAME%\Effects\fx" rmdir /s /q "%GAME%\Effects\fx"
echo Restored the previous d3dx.dll, removed %GAME%\postfx and %GAME%\Effects\fx
exit /b 0

:sync
copy /y "%ROOT%\postfx\postfx.hlsl" "%GAME%\postfx\postfx.hlsl" >nul && echo postfx.hlsl copied
exit /b 0

:usage
echo usage: %~nx0 install^|uninstall^|sync "C:\path\to\Condor3"
echo        (or set CONDOR_DIR and leave the folder out)
exit /b 2
