$ErrorActionPreference = 'Stop'
$tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (!(Test-Path $tailscale)) { throw 'Install Tailscale on this PC and sign in first.' }
& $tailscale status
if ($LASTEXITCODE -ne 0) { throw 'Connect Tailscale first.' }
& $tailscale set --unattended=true
if ($LASTEXITCODE -ne 0) { throw 'Could not enable unattended mode.' }
& $tailscale serve --bg http://127.0.0.1:8765
if ($LASTEXITCODE -ne 0) { throw 'Follow the Tailscale HTTPS setup prompt, then retry.' }
& $tailscale serve status
Write-Host 'Copy the HTTPS origin into SENTINEL setup. Do not enable Funnel or router port forwarding.'
