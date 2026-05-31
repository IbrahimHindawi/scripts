@echo off

:: WARNING:
:: Must be executed from project root directory using `scripts\bbuild.bat`

:: Setup Script Variables
for %%I in (.) do set projectname=%%~nxI
:: echo %projectname%

set generator="Ninja"
set debugdir=build
set releasedir=build-release
set haikaldir=extern\haikal\build
set haikalexe=%haikaldir%\haikal.exe
set debugger=devenv /debugexe 
:: set debugger=raddbg

echo Use -h to display available commands.
echo:
goto GETOPTS

:Help
echo config debug       to configure debug.
echo config release     to configure release.
echo config reldebug    to configure release with debug info.
echo build debug        to build debug.
echo build release      to build release.
echo build reldebug     to build release with debug info.
echo run debug          to build and run debug.
echo run release        to build and run release.
echo debugexe debug     to build debug and attach debugger.
echo debugexe reldebug  to build release with debug info and attach debugger.
echo -x to clean up.
goto :eof

:ResolveConfig
set requested=%1
set builddir=
set cmakeconfig=
if /I "%requested%" == "debug" (
    set builddir=%debugdir%
    set cmakeconfig=Debug
)
if /I "%requested%" == "release" (
    set builddir=%releasedir%
    set cmakeconfig=Release
)
if /I "%requested%" == "reldebug" (
    set builddir=%releasedir%
    set cmakeconfig=RelWithDebInfo
)
if "%builddir%" == "" (
    echo Unknown config: %requested%
    echo Expected: debug, release, or reldebug.
    exit /b 1
)
goto :eof

:Config
call :ResolveConfig %1
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
if not exist %builddir% mkdir %builddir%
cmake -S . -B %builddir% -G %generator% -DCMAKE_C_COMPILER=clang-cl "-DCMAKE_BUILD_TYPE=%cmakeconfig%"
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
goto :eof

:BuildConfig
call :Config %1
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
call :InjectResources %builddir%\res
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
call :BuildHaikal
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
call %haikalexe% --entry src\main.c --meta extern\haikal\src\meta_arena
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
cmake --build %builddir%
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
goto :eof

:BuildHaikal
if not exist %haikaldir% mkdir %haikaldir%
cmake -S extern\haikal -B %haikaldir% -G %generator% -DCMAKE_C_COMPILER=clang-cl "-DCMAKE_BUILD_TYPE=Debug"
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
cmake --build %haikaldir%
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
goto :eof

:RunConfig
call :BuildConfig %1
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
shift
%builddir%\%projectname%.exe %*
goto :eof

:DebugExeConfig
call :BuildConfig %1
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
shift
%debugger% %builddir%\%projectname%.exe %*
goto :eof

:InjectResources
set resdst=%1
if "%resdst%" == "" set resdst=%debugdir%\res
python pipeline\resources_inject.py --dst %resdst% --max-tex-width 128 --max-tex-height 128 --max-gui-width 2048 --max-gui-height 2048
if not %ERRORLEVEL% EQU 0 exit /b %ERRORLEVEL%
goto :eof

:CleanUp
echo Destroy build folders.
if exist %debugdir% rmdir /S /Q %debugdir%
if exist %releasedir% rmdir /S /Q %releasedir%
if exist %haikaldir% rmdir /S /Q %haikaldir%
goto :eof

:GETOPTS
if /I "%1" == "-h" call :Help
if /I "%1" == "help" call :Help
if /I "%1" == "config" call :Config %2
if /I "%1" == "build" call :BuildConfig %2
if /I "%1" == "run" call :RunConfig %2
if /I "%1" == "debugexe" call :DebugExeConfig %2
if /I "%1" == "-x" call :CleanUp
shift
if not "%1" == "" call :Epilogue
:Epilogue
goto :eof
