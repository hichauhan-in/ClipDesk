<#
.SYNOPSIS
    Builds a distributable ClipDesk bundle.

.DESCRIPTION
    Produces a clean folder (and optionally a .zip) containing everything a
    colleague needs, and nothing they should not have.

    Deliberately excluded:
      workspace\        recordings and outputs - someone else's meetings
      config\local.yaml your own provider and model choices
      .venv\            not relocatable; absolute paths are baked into it
      vendor\downloads\ install caches, already unpacked
      .git\, __pycache__

.PARAMETER IncludeVendor
    Bundle ffmpeg, the media extractor and the speech-to-text model (~420 MB).
    Without this the recipient downloads them on first run, which needs access
    to github.com and huggingface.co.

.PARAMETER Offline
    Also bundle the Python packages as wheels, so the recipient never contacts a
    package feed. Wheels are built for THIS machine's Python version and CPU
    architecture - the recipient must have the same Python major.minor. The
    script prints which one.

.PARAMETER Zip
    Also produce a .zip beside the folder, and remove the staging folder once it
    has been compressed.

.PARAMETER OutputDir
    Where bundles are written. Defaults to dist\, which is git-ignored.

.EXAMPLE
    .\scripts\package.ps1 -IncludeVendor -Zip
    The usual choice: one file, works on a machine with no special access.
    Writes dist\clipdesk-<version>-<yyyyMMdd-HHmmss>.zip

.EXAMPLE
    .\scripts\package.ps1 -Zip
    Small bundle (~1 MB) for machines that can reach github.com.
#>
[CmdletBinding()]
param(
    [switch]$IncludeVendor,
    [switch]$Offline,
    [switch]$Zip,
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $AppRoot

$version = (Select-String -Path 'clipdesk\__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
# Sortable, so a directory listing is already in build order. Two bundles of the
# same version are otherwise impossible to tell apart once they have been copied
# somewhere and lost their file dates.
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$name = "clipdesk-$version-$stamp"
$stage = Join-Path $OutputDir $name

function Write-Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Note($m) { Write-Host "    $m" -ForegroundColor DarkGray }

if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# --- source ------------------------------------------------------------------
Write-Step "Collecting ClipDesk $version"

$files = @('Start ClipDesk.cmd', 'pyproject.toml', 'README.md', '.gitignore')
foreach ($file in $files) {
    if (Test-Path $file) { Copy-Item $file -Destination $stage }
}

Copy-Item -Recurse 'scripts' -Destination (Join-Path $stage 'scripts')

foreach ($dir in 'clipdesk', 'config', 'vscode-bridge', 'tools', 'tests') {
    if (-not (Test-Path $dir)) { continue }
    Copy-Item -Recurse $dir -Destination (Join-Path $stage $dir)
}

# Never ship the packager's own choices or Python caches.
Remove-Item (Join-Path $stage 'config\local.yaml') -ErrorAction SilentlyContinue
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

# Assets the app reads at run time. A missing one is not a smaller bundle, it is
# a feature that fails on the recipient's machine and nowhere else.
$required = @{
    'tools\template\file.docx' = 'the Word article template'
    'config\default.yaml'      = 'the default settings'
    'clipdesk\web\index.html'  = 'the user interface'
}
$absent = $required.Keys | Where-Object { -not (Test-Path (Join-Path $stage $_)) }
if ($absent) {
    throw "Missing from the bundle: $(($absent | ForEach-Object { "$_ ($($required[$_]))" }) -join ', ')"
}
Write-Note "source: $([math]::Round((Get-ChildItem -Recurse -File $stage | Measure-Object Length -Sum).Sum/1MB,1)) MB"

# --- vendored dependencies ---------------------------------------------------
if ($IncludeVendor) {
    Write-Step 'Bundling ffmpeg, the media extractor and the model'
    $missing = @()
    foreach ($part in 'ffmpeg', 'ytdlp', 'models') {
        $source = Join-Path 'vendor' $part
        if (Test-Path $source) {
            Copy-Item -Recurse $source -Destination (Join-Path $stage "vendor\$part")
            Write-Note "$part : $([math]::Round((Get-ChildItem -Recurse -File $source | Measure-Object Length -Sum).Sum/1MB,1)) MB"
        } else {
            $missing += $part
        }
    }
    if ($missing) {
        Write-Host "    Not present on this machine, so not bundled: $($missing -join ', ')" -ForegroundColor Yellow
        Write-Host "    Run '.\.venv\Scripts\python.exe -m clipdesk bootstrap' first to include them." -ForegroundColor Yellow
    }
}

# --- python packages ---------------------------------------------------------
if ($Offline) {
    Write-Step 'Bundling Python packages as wheels'
    $python = '.\.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        throw "No .venv found. Run '.\Start ClipDesk.cmd' once before packaging with -Offline."
    }
    $pyVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $wheels = Join-Path $stage 'vendor\wheels'
    New-Item -ItemType Directory -Force -Path $wheels | Out-Null

    & $python -m pip download '.[transcribe]' --dest $wheels --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Downloading wheels failed.' }

    # Binary wheels are built per Python version, so the recipient must match.
    Set-Content -Path (Join-Path $stage 'vendor\wheels\PYTHON_VERSION.txt') -Value $pyVersion
    Write-Note "wheels built for Python $pyVersion - recipients need the same major.minor"
    Write-Note "wheels: $([math]::Round((Get-ChildItem -Recurse -File $wheels | Measure-Object Length -Sum).Sum/1MB,1)) MB"
}

# --- recipient instructions --------------------------------------------------
Write-Step 'Writing SETUP.txt'

$pythonLine = if ($Offline) {
    "   Python $((Get-Content (Join-Path $stage 'vendor\wheels\PYTHON_VERSION.txt')) ) - this bundle
   requires that exact version."
} else {
    '   Python 3.10 or newer.'
}
$networkLine = if ($IncludeVendor) {
    'Everything needed is already in this folder.'
} else {
    'First run downloads ffmpeg and the speech-to-text model (~420 MB).'
}

@"
ClipDesk $version
=================
Built $(Get-Date -Format 'dddd d MMMM yyyy, HH:mm')

$networkLine


START IT
--------
1. Extract this folder somewhere you can write to, for example:

       C:\Tools\ClipDesk

   Do NOT run it from inside the .zip.

2. Double-click:  Start ClipDesk.cmd

   That is it. The first run sets everything up and takes a few minutes:
   it installs Python if you do not have it, prepares the app, and adds the
   Copilot bridge to VS Code. A browser then opens at http://127.0.0.1:8760

   If VS Code was already open, it will ask you to reload it once
   (Ctrl+Shift+P -> "Developer: Reload Window"). That is the only manual step,
   and only on the first run.

   If Windows says the file is blocked because it came from another computer:
   right-click Start ClipDesk.cmd -> Properties -> tick Unblock -> OK.


EVERY TIME AFTER THAT
---------------------
   Double-click Start ClipDesk.cmd, or search the Start Menu for ClipDesk.
   It opens in a couple of seconds.

   Keep a VS Code window open while you use it, so ClipDesk can reach Copilot.


WHAT IT NEEDS
-------------
$pythonLine
   The launcher installs it for you if it is missing. No admin rights needed.

   VS Code with GitHub Copilot, signed in, to use your Copilot seat.
   Other providers (Azure OpenAI, Claude, Gemini, a local model) can be set
   up under Settings instead.


YOUR DATA
---------
   Recordings and everything made from them stay in the workspace\ folder on
   your own machine. Only transcript text is ever sent to a model.

   Full documentation is in README.md.
"@ | Set-Content -Path (Join-Path $stage 'SETUP.txt') -Encoding UTF8

# --- zip ---------------------------------------------------------------------
$totalMb = [math]::Round((Get-ChildItem -Recurse -File $stage | Measure-Object Length -Sum).Sum / 1MB, 1)

if ($Zip) {
    Write-Step 'Compressing'
    $archive = Join-Path $OutputDir "$name.zip"
    Remove-Item $archive -ErrorAction SilentlyContinue
    Compress-Archive -Path "$stage\*" -DestinationPath $archive -CompressionLevel Optimal
    $zipMb = [math]::Round((Get-Item $archive).Length / 1MB, 1)
    # The zip is the thing being shipped, and every build is now a new name, so
    # keeping the staging copy too would fill the disk a quarter of a gigabyte
    # at a time.
    Remove-Item -Recurse -Force $stage
    Write-Host "`n  $((Resolve-Path $archive).Path)" -ForegroundColor Green
    Write-Host "  $zipMb MB compressed ($totalMb MB extracted)`n" -ForegroundColor DarkGray
} else {
    Write-Host "`n  $((Resolve-Path $stage).Path)" -ForegroundColor Green
    Write-Host "  $totalMb MB`n" -ForegroundColor DarkGray
}

Write-Host "  Excluded: workspace\, .venv\, config\local.yaml, vendor\downloads\`n" -ForegroundColor DarkGray
