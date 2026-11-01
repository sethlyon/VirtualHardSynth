<#
.SYNOPSIS
Turns a freshly imaged Raspberry Pi OS Lite (64-bit) card into a VHS-1 card.

.DESCRIPTION
Write the stock image first with Raspberry Pi Imager and no OS
customisation, then point this at the card's boot partition. It:

  - sets up the audio HAT and the panel's GPIO UART in config.txt
  - takes the serial console off the UART in cmdline.txt
  - writes userconf.txt (user vhs, locked password) and your SSH key
  - writes cloud-init user-data: hostname vhs, US keyboard, firstboot hook
  - copies this repo (committed work only) to vhs/ on the card

On first boot setup/firstboot.sh copies the repo into /home/vhs, the panel
offers its Wi-Fi picker, and setup/provision.sh installs the rest once
there's a network. Nothing secret goes on the card: Wi-Fi is chosen on the
touchscreen.

.EXAMPLE
pwsh setup/prepare-card.ps1 -Boot G:\

.EXAMPLE
# No drive letter assigned: use the volume path from Get-Volume
pwsh setup/prepare-card.ps1 -Boot '\\?\Volume{4acfd0a7-...}\'
#>
param(
    [Parameter(Mandatory)] [string] $Boot,
    [string] $SshKey = "$env:USERPROFILE\.ssh\id_ed25519.pub"
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

# Everything here is read by Linux; keep it LF.
function Write-Lf([string] $path, [string] $text) {
    [IO.File]::WriteAllText($path, ($text -replace "`r`n", "`n"))
}

foreach ($f in 'config.txt', 'cmdline.txt') {
    # -LiteralPath throughout: a partition Windows didn't give a letter is
    # reached as \\?\Volume{...}\, and plain -Path reads that ? as a wildcard.
    if (-not (Test-Path -LiteralPath (Join-Path $Boot $f))) {
        throw "$f not found on $Boot - is this a freshly imaged Raspberry Pi OS boot partition?"
    }
}
if (git -C $repo status --porcelain) {
    Write-Warning "uncommitted changes in $repo are NOT copied to the card"
}

# config.txt
$cfgPath = Join-Path $Boot 'config.txt'
$cfg = [IO.File]::ReadAllText($cfgPath)
if ($cfg -notmatch '# --- VHS-1') {
    $cfg = $cfg -replace '(?m)^dtparam=audio=on[ \t]*$', 'dtparam=audio=off'
    $cfg = $cfg -replace '(?m)^dtoverlay=vc4-kms-v3d[ \t]*$', 'dtoverlay=vc4-kms-v3d,noaudio'
    $cfg = $cfg.TrimEnd() + @"


[all]
# --- VHS-1
# WM8960 audio HAT. HDMI and onboard audio are off so it's the only card.
dtparam=i2c_arm=on
dtoverlay=wm8960-soundcard
# Front panel on the GPIO UART (pins 8/10). disable-bt hands it the PL011,
# whose baud rate doesn't drift with the core clock.
enable_uart=1
dtoverlay=disable-bt

"@
    Write-Lf $cfgPath $cfg
}

# cmdline.txt - one line; drop the serial console so the panel owns the UART
$cmdPath = Join-Path $Boot 'cmdline.txt'
$cmd = ([IO.File]::ReadAllText($cmdPath)).Trim() -split '\s+' |
    Where-Object { $_ -notmatch '^console=(serial0|ttyAMA0|ttyS0),' }
Write-Lf $cmdPath (($cmd -join ' ') + "`n")

# cloud-init: hostname, keyboard, and the firstboot.sh hook
Write-Lf (Join-Path $Boot 'user-data') (Get-Content (Join-Path $PSScriptRoot 'user-data') -Raw)

# The user. Raspberry Pi OS renames its placeholder first user from this
# file; without it, boot stops at an interactive "enter new username"
# dialog. '*' is a locked password - log in with the SSH key.
Write-Lf (Join-Path $Boot 'userconf.txt') "vhs:*`n"
$vhsBoot = Join-Path $Boot 'vhs'
[IO.Directory]::CreateDirectory($vhsBoot) | Out-Null
Write-Lf (Join-Path $vhsBoot 'authorized_keys') ((Get-Content $SshKey -Raw).Trim() + "`n")

$sshFlag = Join-Path $Boot 'ssh'
if (-not (Test-Path -LiteralPath $sshFlag)) { [IO.File]::WriteAllText($sshFlag, '') }

# The repo, as a real clone so the Pi can git pull later
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("vhs-card-" + [guid]::NewGuid())
git -c core.autocrlf=false clone --quiet --no-hardlinks $repo $tmp
if ($LASTEXITCODE) { throw "git clone failed" }
git -C $tmp config core.fileMode false
git -C $tmp remote set-url origin (git -C $repo remote get-url origin)
# Copy-Item rather than robocopy: robocopy can't create folders under a
# \\?\Volume{...}\ path and just retries forever.
$vhsDir = Join-Path $Boot 'vhs'
$dest = Join-Path $vhsDir 'VirtualHardSynth'
if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
[IO.Directory]::CreateDirectory($vhsDir) | Out-Null
Copy-Item -LiteralPath $tmp -Destination $dest -Recurse -Force
Remove-Item -LiteralPath $tmp -Recurse -Force

Write-Host "Card ready: $(git -C $repo log -1 --format='%h %s')"
