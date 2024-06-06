# WARNING:
# Must be executed from project root directory using `scripts\bbuild.ps1`
# TODO(Ibrahim): Implement Debug and Release to CompileCommand.

param(
    [string]$arg1
)

function DisplayHelp {
    Write-Host "-b to build solution."
    Write-Host "-c to compile."
    Write-Host "-cr to compile and run."
    Write-Host "-r to run exe."
    Write-Host "-x to clean up."
}

function Build {
    New-Item -ItemType Directory -Force -Path build
    Push-Location build
    cmake .. "-DCMAKE_TOOLCHAIN_FILE=C:\devel\vcpkg\scripts\buildsystems\vcpkg.cmake" "-DCMAKE_BUILD_TYPE=Debug" "-DProjectNameParam:STRING=$projectName"
    & "..\scripts\clang-build.ps1" -export-jsondb
    Pop-Location
}

function Compile {
    Push-Location extern\haikal
    & "..\..\build\extern\haikal\Debug\haikal.exe"
    Pop-Location
    Invoke-Expression $CompileCommand
}

function CompileRun {
    Write-Host "Compile"
    Compile
    Write-Host "Run"
    & "build\Debug\$projectName.exe"
}

function Run {
    & "build\Debug\$projectName.exe"
}

function CleanUp {
    Write-Host "Destroy build folder."
    Remove-Item -Path build -Recurse -Force
}

# Setup Script Variables
$projectName = (Get-Item .).Name
Write-Output "projectName: $projectName"
Push-Location extern\haikal
Start-Process "..\..\build\extern\haikal\Debug\haikal.exe"
Pop-Location

$CompileCommand = "msbuild build\$projectName.sln -nologo -m -v:m /property:Configuration=Debug /property:VcpkgEnabled=false"

# $args = $args | ForEach-Object { $_ -replace '^[/-]+' }

switch ($arg1) {
    "b" {
        Build
        break
    }
    "c" {
        Compile
        break
    }
    "cr" {
        CompileRun
        break
    }
    "r" {
        Run
        break
    }
    "x" {
        CleanUp
        break
    }
    default {
        Write-Host "Unknown option: $args"
        DisplayHelp
    }
}
