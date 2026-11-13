param(
    [Parameter(Mandatory=$true)][string]$EnginePath,
    [string]$BuildRoot = (Join-Path $PSScriptRoot '.engine-build')
)
$ErrorActionPreference = 'Stop'
$EnginePath = [IO.Path]::GetFullPath($EnginePath)
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
if (-not (Test-Path -LiteralPath $EnginePath -PathType Leaf)) { throw 'Original jsr_backend.exe not found' }
$originalHash = '45b520c43037a7e5033a6e08f97bb4972413440462eea5280986a7237f889729'
$runnerSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'iqoo_easy\runner.py') -Raw
$match = [regex]::Match($runnerSource, 'BACKEND_SHA256 = "([0-9a-f]{64})"')
if (-not $match.Success) { throw 'This source checkout has no verified patched engine hash' }
$patchedHash = $match.Groups[1].Value
$currentHash = (Get-FileHash -LiteralPath $EnginePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($currentHash -eq $patchedHash) { Write-Output 'Matching patched engine is already installed'; return }
if ($currentHash -ne $originalHash) { throw 'Unknown engine version; refusing to patch or overwrite it' }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$archive = Join-Path $BuildRoot 'python-3.10.11-embed-amd64.zip'
$archiveHash = '608619f8619075629c9c69f361352a0da6ed7e62f83a0e19c63e0ea32eb7629d'
if (-not (Test-Path -LiteralPath $archive)) {
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip' -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveHash) {
    throw 'Portable Python archive checksum mismatch'
}
$pythonRoot = Join-Path $BuildRoot 'python310'
Expand-Archive -LiteralPath $archive -DestinationPath $pythonRoot -Force
$python = Join-Path $pythonRoot 'python.exe'
$stage = Join-Path $BuildRoot ('build-' + [Guid]::NewGuid().ToString('N'))
$output = Join-Path $stage 'jsr_backend.exe'
$oldTemp, $oldTmp = $env:TEMP, $env:TMP
try {
    $env:TEMP = Join-Path $BuildRoot 'temp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    & $python (Join-Path $PSScriptRoot 'engine_patch\build.py') --original $EnginePath --output $output
    if ($LASTEXITCODE -ne 0) { throw 'Engine patch build failed' }
    if ((Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant() -ne $patchedHash) {
        throw 'Rebuilt engine differs from the verified release; original is unchanged'
    }
    & $output --iqoo-engine-probe (Join-Path $stage 'startup-probe.json')
    if ($LASTEXITCODE -ne 0) { throw 'Patched engine startup check failed; original is unchanged' }
    $backup = $EnginePath + '.original-v18.3.1.bak'
    if (Test-Path -LiteralPath $backup) {
        if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $originalHash) {
            throw 'Existing backup has unexpected contents; refusing to overwrite it'
        }
    }
    # File.Replace needs the replacement on the target volume and atomically
    # keeps the original as a backup. An in-use executable will fail safely.
    $adjacent = $EnginePath + '.new-' + [Guid]::NewGuid().ToString('N')
    Copy-Item -LiteralPath $output -Destination $adjacent
    [IO.File]::Replace($adjacent, $EnginePath, $backup)
    Copy-Item -LiteralPath ([IO.Path]::ChangeExtension($output, '.patch.json')) -Destination ($EnginePath + '.patch.json')
    Write-Output "Installed patched engine: $EnginePath"
    Write-Output "Original backup: $backup"
} finally {
    $env:TEMP, $env:TMP = $oldTemp, $oldTmp
}
