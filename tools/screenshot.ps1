# Capture a specific window's client area to PNG.
#
# Why PrintWindow instead of CopyFromScreen:
#   CopyFromScreen grabs whatever is visually on top at those screen
#   coordinates. If another window (a game, a video, a chat app) covers the
#   target, you silently capture the WRONG window. PrintWindow asks the target
#   to render itself into a DC, so overlapping windows do not matter.
#
# The Win32 helper lives in windowcapture.cs. See that file for why it is not
# an inline Add-Type here-string.
#
# ASCII-only source: Windows PowerShell 5.1 parses .ps1 using the active code
# page, so non-ASCII literals would be mangled. Pass non-ASCII titles as args.
param(
  [string]$TitleMatch = "Stellaris Mod Doctor",
  [string]$Out = "$env:TEMP\shot.png",
  [int]$SettleMs = 900,
  [switch]$Foreground
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$cs = Join-Path $PSScriptRoot "windowcapture.cs"
if (-not (Test-Path -LiteralPath $cs)) {
  Write-Output "MISSING $cs"
  exit 2
}

# System.Drawing must be referenced explicitly: Add-Type does not include it
# by default when compiling C# source.
try {
  Add-Type -Path $cs -ReferencedAssemblies "System.Drawing"
} catch {
  # Already loaded in this runspace / compiled earlier: harmless.
  if (-not ("WinCap" -as [type])) {
    Write-Output "COMPILEFAIL $($_.Exception.Message)"
    exit 2
  }
}

$h = [WinCap]::Find($TitleMatch)
if ($h -eq [IntPtr]::Zero) {
  Write-Output "NOTFOUND"
  Write-Output "--- visible windows ---"
  Write-Output ([WinCap]::All())
  exit 1
}

# SW_RESTORE = 9
[WinCap]::ShowWindow($h, 9) | Out-Null
if ($Foreground) {
  [WinCap]::SetForegroundWindow($h) | Out-Null
  Start-Sleep -Milliseconds 600
}
Start-Sleep -Milliseconds $SettleMs

$size = [WinCap]::SizeOf($h, $true)
if ($size -eq "0x0") {
  Write-Output "BADSIZE $size"
  exit 1
}

if ([WinCap]::Grab($h, $Out, $true)) {
  Write-Output "SAVED $Out ($size) [printwindow]"
  exit 0
}

# Fallback: screen grab. Only correct when the window is actually on top.
$pt = New-Object WinCap+POINT
$pt.X = 0; $pt.Y = 0
[WinCap]::ClientToScreen($h, [ref]$pt) | Out-Null
[WinCap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 1200

$cr = New-Object WinCap+RECT
[WinCap]::GetClientRect($h, [ref]$cr) | Out-Null
$w = $cr.Right - $cr.Left
$hh = $cr.Bottom - $cr.Top
if ($w -le 0 -or $hh -le 0) { Write-Output "BADSIZE ${w}x${hh}"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($pt.X, $pt.Y, 0, 0, (New-Object System.Drawing.Size($w, $hh)))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "SAVED $Out ($w x $hh) [screengrab-fallback]"
