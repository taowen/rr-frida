# Build the tutorial's official library and driver for Android and host.
#
#   pwsh -File tools/build-example.ps1
#
# Output:
#   build/libgeom.so     AArch64 Android shared library (the "official" code)
#   build/geom-driver    AArch64 Android process that calls it in a loop
#
# The library must be a shared object so Frida can attach by module name. The
# driver is static-libstdc++ so it does not depend on the app's libc++_shared.so.

param(
    [string]$Ndk = $env:ANDROID_NDK_HOME,
    [string]$HostTriple = "windows-x86_64"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
New-Item -ItemType Directory -Path $build -Force | Out-Null

if (-not $Ndk) {
    foreach ($candidate in @(
        "C:\tools\android-sdk\ndk\29.0.14206865",
        "$env:ANDROID_HOME\ndk\29.0.14206865")) {
        if (Test-Path -LiteralPath $candidate) { $Ndk = $candidate; break }
    }
}
if (-not $Ndk) { throw "set ANDROID_NDK_HOME or pass -Ndk" }

$clangxx = Join-Path $Ndk "toolchains\llvm\prebuilt\$HostTriple\bin\clang++.exe"
if (-not (Test-Path -LiteralPath $clangxx)) { throw "missing compiler: $clangxx" }
$nm = Join-Path $Ndk "toolchains\llvm\prebuilt\$HostTriple\bin\llvm-nm.exe"

$example = Join-Path $root "tutorial\example"
$library = Join-Path $build "libgeom.so"
$driver = Join-Path $build "geom-driver"

# The NDK *-clang++.cmd wrapper mangles arguments under PowerShell; drive
# clang++.exe directly and pass an explicit --target.
& $clangxx @("--target=aarch64-linux-android29", "-O2", "-shared", "-fPIC",
             "-o", $library, (Join-Path $example "official.cpp"))
if ($LASTEXITCODE -ne 0) { throw "library build failed" }

& $clangxx @("--target=aarch64-linux-android29", "-O2", "-static-libstdc++",
             "-Wl,-rpath,/data/local/tmp", "-L$build", "-lgeom",
             "-o", $driver, (Join-Path $example "driver.cpp"))
if ($LASTEXITCODE -ne 0) { throw "driver build failed" }

$hash = (Get-FileHash -LiteralPath $library -Algorithm SHA256).Hash.ToLower()
Write-Output "library: $library"
Write-Output "driver:  $driver"
Write-Output "sha256:  $hash"
Write-Output ""
Write-Output "Pin this hash in tutorial/example/probe-set.json, then generate probes with:"
Write-Output "  python3 tools/make_probes.py build/libgeom.so --nm `"$nm`" ..."
