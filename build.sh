#!/bin/bash

# WARNING:
# Must be executed from project root directory using `scripts/bbuild.sh`

# TODO (Ibrahim): Implement Debug and Release to compileCommand.

# Setup Script Variables
projectName=$(basename "$PWD")
# echo $projectName

# compileCommand="msbuild build/$projectName.sln -nologo -m -v:m /property:Configuration=Debug /property:VcpkgEnabled=false"
compileCommand="cmake --build build"
# compileCommand="cmake --build build --target $projectName"

# Program Begin
echo "Use -h to display available commands."
echo

function Help {
    echo "-b to build solution."
    echo "-c to compile."
    echo "-cr to compile and run."
    echo "-m to compile haikal metaprogram generator."
    echo "-r to run exe."
    echo "-x to clean up."
}

function Build {
    mkdir -p build
    pushd build
    cmake .. "-DCMAKE_TOOLCHAIN_FILE=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake" "-DCMAKE_BUILD_TYPE=Debug" "-DProjectNameParam:STRING=$projectName"
    powershell -Command ../scripts/clang-build.ps1 -export-jsondb
    popd
    echo "Building haikal metaprogram generator..."
    cmake --build build/extern/haikal
}

function Compile {
    build/extern/haikal/Debug/haikal
    $compileCommand
}

function CompileRun {
    build/extern/haikal/Debug/haikal
    $compileCommand
    build/Debug/$projectName
}

function MetaGen {
    echo "Building haikal metaprogram generator..."
    cmake --build build/extern/haikal
}

function Run {
    build/Debug/$projectName
}

function CleanUp {
    echo "Destroy build folder."
    rm -rf build
}

# GETOPTS
while [[ "$1" != "" ]]; do
    case $1 in
        -h ) Help
             ;;
        -b ) Build
             ;;
        -c ) Compile
             ;;
        -cr ) CompileRun
              ;;
        -m ) MetaGen
             ;;
        -r ) Run
             ;;
        -x ) CleanUp
             ;;
    esac
    shift
done
