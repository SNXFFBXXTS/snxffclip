# Clipper installer for Windows 10/11.
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/SNXFFBXXTS/snxffclip/main/windows/install.ps1 | iex
# Safe to run again - it just updates what's already there.

$ErrorActionPreference = "Continue"
$repo = "https://github.com/SNXFFBXXTS/snxffclip"
$dir  = Join-Path $HOME "clipper"

function Say($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Get-App($id, $cmd) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { Write-Host "  $cmd - already installed"; return }
    Write-Host "  installing $cmd ..."
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    Refresh-Path
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again." -ForegroundColor Red
    return
}

Say "Installing the tools (Python, Git, ffmpeg, yt-dlp)"
Get-App "Git.Git" "git"
Get-App "Gyan.FFmpeg" "ffmpeg"
Get-App "yt-dlp.yt-dlp" "yt-dlp"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "  installing Python ..."
    winget install --id Python.Python.3.13 -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    Refresh-Path
} else { Write-Host "  python - already installed" }

foreach ($c in "git", "ffmpeg", "yt-dlp", "py") {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
        Write-Host "`n$c still isn't found. Close this window, open a NEW PowerShell window and run the same command again." -ForegroundColor Yellow
        return
    }
}

Say "Getting clipper"
if (Test-Path (Join-Path $dir ".git")) { git -C $dir pull --ff-only } else { git clone $repo $dir }

Say "Installing the speech recogniser and face tracking - a few minutes"
py -3 -m pip install --user --upgrade --quiet faster-whisper numpy opencv-python-headless

Say "Making a Clipper shortcut on your desktop"
$desk = [Environment]::GetFolderPath("Desktop")
$bat = "@echo off`r`ntitle Clipper - keep this window open (close it to stop clipper)`r`ncd /d `"$dir`"`r`npy -3 clipper.py --ui --open`r`npause`r`n"
Set-Content -Path (Join-Path $desk "Clipper.bat") -Value $bat -Encoding ASCII

Say "Done!"
Write-Host "Double-click 'Clipper' on your desktop - the page opens at http://localhost:8765"
Write-Host "Have an NVIDIA graphics card? In the page press  AI  then 'Set up GPU' for much faster transcription."
