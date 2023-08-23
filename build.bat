@echo off

rem WARNING:
rem Must be executed from project root directory using `scripts\bbuild.bat`
rem
rem TODO(Ibrahim): Implement Debug and Release to CompileCommand.

rem Setup Script Variables
for %%I in (.) do set projectName=%%~nxI
rem echo %projectName%

set CompileCommand=msbuild build\%projectName%.sln -nologo -m -v:m /property:Configuration=Debug /property:VcpkgEnabled=false

rem Program Begin
echo Use -h to display available commands.
echo:
goto GETOPTS

:Help
echo -b to build solution. 
echo -c to compile. 
echo -cr to compile and run.
echo -r to run exe.
echo -x to clean up.
goto :eof

:Build
mkdir build
pushd build
cmake .. "-DCMAKE_TOOLCHAIN_FILE=C:\devel\vcpkg\scripts\buildsystems\vcpkg.cmake" "-DCMAKE_BUILD_TYPE=Debug" "-DProjectNameParam:STRING=%projectName%"
powershell -Command ..\scripts\clang-build.ps1 -export-jsondb
popd build
goto :eof

:Compile
%CompileCommand%
goto :eof

:CompileRun
%CompileCommand%
build\Debug\%projectName%.exe
goto :eof

:Run
build\Debug\%projectName%.exe
goto :eof

:CleanUp
echo Destroy build folder.
rmdir /S /Q build
goto :eof

:GETOPTS
if /I "%1" == "-h" call :Help
if /I "%1" == "-b" call :Build
if /I "%1" == "-c" call :Compile
if /I "%1" == "-cr" call :CompileRun
if /I "%1" == "-r" call :Run
if /I "%1" == "-x" call :CleanUp
shift
if not "%1" == "" call :Epilogue
:Epilogue
goto :eof
