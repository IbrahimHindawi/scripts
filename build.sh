#!/bin/bash

# WARNING:
# Must be executed from project root directory using `scripts/bbuild.sh`

# Setup Script Variables
projectname=$(basename "$(pwd)")
generator="Ninja"
compilecommand="cmake --build build"
buildcommand="cmake -B build -G $generator"
debugger="gdb"
# debugger="raddbg"

echo "Use -h to display available commands."
echo

# Functions for each command
Help() {
    echo "-b   to build."
    echo "-br  to build release."
    echo "-brd to build release with debug info."
    echo "-mb  to build haikal meta."
    echo "-mc  to compile haikal meta."
    echo "-c   to compile."
    echo "-cr  to compile and run."
    echo "-crd to compile and run with debugger."
    echo "-r   to run exe."
    echo "-x   to clean up."
}

Build() {
    mkdir -p build
    $buildcommand "-DCMAKE_BUILD_TYPE=Debug" -DDEBUGRENDER=1
}

BuildRelease() {
    mkdir -p build
    $buildcommand "-DCMAKE_BUILD_TYPE=Release"
}

BuildReleaseDebug() {
    mkdir -p build
    $buildcommand "-DCMAKE_BUILD_TYPE=RelWithDebInfo"
}

Compile() {
    ./extern/haikal/build/haikal
    $compilecommand
}

CompileRun() {
    ./extern/haikal/build/haikal
    $compilecommand
    ./build/$projectname
}

CompileRunDebugger() {
    ./extern/haikal/build/haikal
    $compilecommand
    $debugger ./build/$projectname
}

MetaBuild() {
    echo "Building haikal."
    pushd extern/haikal
    echo "$(pwd)"
    ./scripts/build.sh -x
    ./scripts/build.sh -b
    popd
}

MetaCompile() {
    echo "Compile haikal."
    pushd extern/haikal
    echo "$(pwd)"
    ./scripts/build.sh -c
    popd
}

Run() {
    ./build/$projectname
}

CleanUp() {
    echo "Destroy build folder."
    rm -rf build
}

# Main logic for command-line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h) Help; exit 0 ;;
        -b) Build; exit 0 ;;
        -br) BuildRelease; exit 0 ;;
        -brd) BuildReleaseDebug; exit 0 ;;
        -mb) MetaBuild; exit 0 ;;
        -mc) MetaCompile; exit 0 ;;
        -c) Compile; exit 0 ;;
        -cr) CompileRun; exit 0 ;;
        -crd) CompileRunDebugger; exit 0 ;;
        -r) Run; exit 0 ;;
        -x) CleanUp; exit 0 ;;
        *) echo "Unknown option: $1"; Help; exit 1 ;;
    esac
done
