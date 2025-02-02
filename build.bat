@echo off

:: WARNING:
:: Must be executed from project root directory using `scripts\bbuild.bat`

:: Setup Script Variables
for %%I in (.) do set projectname=%%~nxI
:: echo %projectname%

set generator="Ninja"
set compilecommand=cmake --build build
set buildcommand=cmake -B=build -G=%generator%
set debugger=devenv
:: set debugger=raddbg

echo Use -h to display available commands.
echo:
goto GETOPTS

:Help
echo -b to build.
echo -br to build release.
echo -brd to build release with debug info.
echo -mb to build haikal meta.
echo -mc to compile haikal meta.
echo -c to compile.
echo -cr to compile and run.
echo -crd to compile and run with debugger.
echo -r to run exe.
echo -x to clean up.
goto :eof

:Build
mkdir build
::-DDEBUGRENDER=1
%buildcommand% "-DCMAKE_BUILD_TYPE=Debug"
goto :eof

:BuildRelease
mkdir build
%buildcommand% "-DCMAKE_BUILD_TYPE=Release"
goto :eof

:BuildReleaseDebug
mkdir build
%buildcommand% "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
goto :eof

:Compile
call extern\haikal\build\haikal.exe
%compilecommand%
goto :eof

:CompileRun
call extern\haikal\build\haikal.exe
%compilecommand%
build\%projectname%.exe
goto :eof

:CompileRunDebugger
call extern\haikal\build\haikal.exe
%compilecommand%
%debugger% build\%projectname%.exe
goto :eof

:MetaBuild
echo Building haikal.
pushd extern\haikal
echo %cd%
call scripts\build.bat -x
call scripts\build.bat -b
popd
goto :eof

:MetaCompile
echo Compile haikal.
pushd extern\haikal
echo %cd%
call scripts\build.bat -c
popd
goto :eof

:Run
build\%projectname%.exe
goto :eof

:CleanUp
echo Destroy build folder.
rmdir /S /Q build
goto :eof

:GETOPTS
if /I "%1" == "-h" call :Help
if /I "%1" == "-b" call :Build
if /I "%1" == "-br" call :BuildRelease
if /I "%1" == "-brd" call :BuildReleaseDebug
if /I "%1" == "-mb" call :MetaBuild
if /I "%1" == "-mc" call :MetaCompile
if /I "%1" == "-c" call :Compile
if /I "%1" == "-cr" call :CompileRun
if /I "%1" == "-crd" call :CompileRunDebugger
if /I "%1" == "-r" call :Run
if /I "%1" == "-x" call :CleanUp
shift
if not "%1" == "" call :Epilogue
:Epilogue
goto :eof
