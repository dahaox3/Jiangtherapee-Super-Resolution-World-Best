param(
    [string]$BuildRoot = (Join-Path $PSScriptRoot '.build'),
    [string]$OutputRoot = (Join-Path $PSScriptRoot 'dist')
)
$ErrorActionPreference = 'Stop'
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Path $BuildRoot, $OutputRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $BuildRoot 'temp') -Force | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldPip = $env:PIP_CACHE_DIR
$oldPyi = $env:PYINSTALLER_CONFIG_DIR
try {
    $env:TEMP = Join-Path $BuildRoot 'temp'
    $env:TMP = $env:TEMP
    $env:PIP_CACHE_DIR = Join-Path $BuildRoot 'pip-cache'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildRoot 'pyinstaller-cache'
    $python = Join-Path $BuildRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        py -3.12 -m venv (Join-Path $BuildRoot 'venv')
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create build environment' }
    }
    & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install build tools' }
    & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-test.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install test tools' }
    Push-Location $PSScriptRoot
    try {
        & $python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
        & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name JSR-Easy --distpath $OutputRoot --workpath (Join-Path $BuildRoot 'work') --specpath $BuildRoot (Join-Path $PSScriptRoot 'launch_iqoo.py')
        if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
        $bundle = Join-Path $OutputRoot 'JSR-Easy'
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'EASY-START.md') -Destination (Join-Path $bundle 'README.md')
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'iqoo_easy\LICENSE') -Destination (Join-Path $bundle 'LICENSE-frontend.txt')
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'NOTICE-EASY.md') -Destination $bundle
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ENGINE-PATCH.md') -Destination $bundle
        $patchSource = Join-Path $bundle 'engine-patch-source'
        New-Item -ItemType Directory -Path (Join-Path $patchSource 'engine_patch'), (Join-Path $patchSource 'iqoo_easy') -Force | Out-Null
        foreach ($source in (Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'engine_patch') -Filter '*.py' -File)) {
            Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $patchSource 'engine_patch')
        }
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'iqoo_easy\runner.py') -Destination (Join-Path $patchSource 'iqoo_easy')
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'prepare-engine.ps1') -Destination $patchSource
        $basePython = & $python -c 'import sys; print(sys.base_prefix)'
        $pythonLicense = Join-Path $basePython 'LICENSE.txt'
        if (Test-Path -LiteralPath $pythonLicense) { Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $bundle 'LICENSE-Python.txt') }
        foreach ($component in @('tcl8.6', 'tk8.6')) {
            $license = Join-Path $basePython "tcl\$component\license.terms"
            if (Test-Path -LiteralPath $license) { Copy-Item -LiteralPath $license -Destination (Join-Path $bundle "LICENSE-$component.txt") }
        }
        Write-Output "Built portable launcher: $bundle\JSR-Easy.exe"
    } finally { Pop-Location }
} finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:PIP_CACHE_DIR = $oldPip
    $env:PYINSTALLER_CONFIG_DIR = $oldPyi
}
