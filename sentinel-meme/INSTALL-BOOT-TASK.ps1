# Run from an elevated PowerShell as the SAME account used for SETUP-WINDOWS.cmd.
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'windows_host.py'
if (!(Test-Path $python)) { throw 'Run SETUP-WINDOWS.cmd first.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $identity -Message 'Windows account password (not PIN). Used by Task Scheduler to start SENTINEL before sign-in.'
if ($credential.UserName -ne $identity) { throw 'Use the same Windows account that encrypted SENTINEL credentials.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $script + '" run') -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'SENTINEL-Paper' -Action $action -Trigger $trigger -Settings $settings -User $identity -Password $credential.GetNetworkCredential().Password -Force | Out-Null
Start-ScheduledTask -TaskName 'SENTINEL-Paper'
Write-Host 'Task installed. Reboot and verify access before relying on unattended operation.'
