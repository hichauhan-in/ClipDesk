<#
.SYNOPSIS
    Undoes everything ClipDesk connected to on this machine.

.DESCRIPTION
    ClipDesk installs nothing system-wide, but it does reach outside its own
    folder in a few places: a VS Code extension, a handshake file that tells the
    app where that extension is listening, and - if you signed in to a tenant
    link - a saved session and its browser profile.

    This removes those connections and nothing else. Your recordings, outputs,
    transcripts and settings are left exactly where they are, because deleting
    work is your decision and not a script's. Delete the ClipDesk folder itself
    whenever you like; nothing here depends on it having been run first.

    Re-linking is not a separate step: running "Start ClipDesk.cmd" installs the
    bridge again and writes a fresh handshake.

.PARAMETER IncludeSessions
    Also remove saved sign-ins: the cookie jar for SharePoint and OneDrive, and
    the browser profile ClipDesk opened for Microsoft sign-in. Left alone by
    default, because signing in again is a nuisance and these are yours.

.PARAMETER Quiet
    Print nothing unless something changed or went wrong.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\unlink.ps1 -WhatIf

    Shows what would be removed without touching anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\unlink.ps1 -IncludeSessions

    Removes the VS Code bridge, the handshake and every saved sign-in.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$IncludeSessions,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# These scripts must run under Windows PowerShell 5.1, which is what every
# Windows machine has and what Start ClipDesk.cmd invokes. No PowerShell 7 syntax, and
# ASCII only: 5.1 reads a UTF-8 file without a BOM as the legacy code page, which
# would corrupt any non-ASCII character in a string it prints.
$extensionId = 'clipdesk.clipdesk-bridge'
$stateDir = Join-Path $HOME '.clipdesk'

function Say($message, $colour = 'Gray') { if (-not $Quiet) { Write-Host $message -ForegroundColor $colour } }

$removed = 0
$kept = @()

function Remove-Target($path, $label) {
    if (-not (Test-Path $path)) { return $false }
    if ($PSCmdlet.ShouldProcess($path, 'Remove')) {
        Remove-Item -Recurse -Force $path
        Say "  Removed $label" DarkGray
    }
    $script:removed++
    return $true
}

Say ''
Say '  Disconnecting ClipDesk' Cyan
Say ''

# --- the VS Code bridge ------------------------------------------------------
# Both channels, because someone can install into either and a copy left in the
# other would keep answering on localhost after the one they meant was gone.
$sawVsCode = $false
foreach ($channel in @('.vscode', '.vscode-insiders')) {
    $extensionsDir = Join-Path $HOME (Join-Path $channel 'extensions')
    if (-not (Test-Path $extensionsDir)) { continue }
    $sawVsCode = $true
    Get-ChildItem $extensionsDir -Directory -Filter "$extensionId-*" -ErrorAction SilentlyContinue |
        ForEach-Object { [void](Remove-Target $_.FullName "VS Code extension $($_.Name)") }
}
if (-not $sawVsCode) { Say '  VS Code was not found on this machine.' DarkGray }

# --- the handshake -----------------------------------------------------------
# How the app finds the bridge: host, port and the bearer token for the session.
# Stale is harmless, but leaving a token lying around when the extension that
# issued it has gone is untidy.
[void](Remove-Target (Join-Path $stateDir 'bridge.json') 'bridge handshake')

# --- saved sign-ins ----------------------------------------------------------
if ($IncludeSessions) {
    [void](Remove-Target (Join-Path $stateDir 'cookies') 'saved sign-in sessions')
    [void](Remove-Target (Join-Path $stateDir 'browser') 'sign-in browser profile')
} else {
    foreach ($name in @('cookies', 'browser')) {
        if (Test-Path (Join-Path $stateDir $name)) { $kept += $name }
    }
}

# --- what is deliberately left behind ----------------------------------------
Say ''
if ($removed -eq 0) {
    Say '  Nothing was linked, so there was nothing to undo.' Yellow
} else {
    Say "  Restart VS Code to finish removing the bridge." Green
    # VS Code caches an extension for the life of its window, so the folder can
    # be gone while the code is still running.
    Say '  Until you do, the bridge keeps running from memory.' DarkGray
}

if ($kept.Count -gt 0) {
    Say ''
    Say "  Kept your saved sign-ins ($($kept -join ', ')). Re-run with" DarkGray
    Say '  -IncludeSessions to remove those as well.' DarkGray
}

Say ''
Say '  Your recordings, outputs and settings were not touched.' DarkGray
Say '  Run "Start ClipDesk.cmd" to link it back up again.' DarkGray
Say ''

return [pscustomobject]@{
    Status  = if ($removed -gt 0) { 'unlinked' } else { 'nothing-to-do' }
    Removed = $removed
    Kept    = $kept
}
