# setup-autostart.ps1
# Run ONCE as Administrator (right-click PowerShell → "Run as administrator")
# Sets up: Fast Startup off, Hybrid Sleep off, NIC Power Saving off,
#          auto-start task on login + wake from sleep.

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) "assistant-on.ps1"
$userId     = "$env:COMPUTERNAME\$env:USERNAME"
$taskName   = "AssistantAutoStart"

Write-Host "`n[1/5] Disabling Fast Startup..." -ForegroundColor Cyan
Set-ItemProperty `
    -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" `
    -Name HiberbootEnabled -Value 0 -Type DWord
$v = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power").HiberbootEnabled
if ($v -eq 0) {
    Write-Host "  Fast Startup disabled. ✓" -ForegroundColor Green
} else {
    Write-Host "  WARNING: value is still $v — may need a reboot to take effect." -ForegroundColor Yellow
}

Write-Host "[2/5] Disabling Hybrid Sleep (AC) — required for NIC to stay powered in S3..." -ForegroundColor Cyan
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HYBRIDSLEEP 0
powercfg /setactive SCHEME_CURRENT
Write-Host "  Hybrid Sleep (AC) = OFF. ✓" -ForegroundColor Green

Write-Host "[3/5] Disabling Realtek NIC Power Saving Mode — required for WoL magic packet detection..." -ForegroundColor Cyan
Set-NetAdapterAdvancedProperty -Name "Ethernet" -RegistryKeyword "PowerSavingMode" -RegistryValue 0
$psm = (Get-NetAdapterAdvancedProperty -Name "Ethernet" -RegistryKeyword "PowerSavingMode").DisplayValue
Write-Host "  Power Saving Mode = $psm" -ForegroundColor Green

Write-Host "[4/5] Ensuring NIC is allowed to wake the system..." -ForegroundColor Cyan
powercfg /deviceenablewake "Realtek Gaming 2.5GbE Family Controller" 2>$null
Write-Host "  NIC wake = enabled. ✓" -ForegroundColor Green

Write-Host "[5/5] Creating scheduled task '$taskName'..." -ForegroundColor Cyan

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""

# Trigger 1 — at logon: 10 s delay lets the network layer initialise.
$trigLogon       = New-ScheduledTaskTrigger -AtLogOn -User $userId
$trigLogon.Delay = "PT10S"

# Trigger 2 — on wake from sleep: Kernel-Power Event 107 fires when Windows
# resumes from any sleep state (S3).  20 s delay gives WiFi time to reconnect.
# Built via raw XML because New-ScheduledTaskTrigger has no -OnEvent parameter.
$wakeXml = @"
<EventTrigger xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Enabled>true</Enabled>
  <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
  <Delay>PT20S</Delay>
</EventTrigger>
"@

# Register the task with just the logon trigger first, then inject the wake
# trigger via full XML round-trip (the only reliable method for event triggers
# via PowerShell without the MSFT_TaskEventTrigger COM class).
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun                  # allow task to be the wakeup source if needed

$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

# Step A: register with logon trigger only.
$task = New-ScheduledTask `
    -Action    $action `
    -Trigger   $trigLogon `
    -Settings  $settings `
    -Principal $principal `
    -Description "Start Ollama + Telegram bot automatically on login and on wake from sleep."

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null

# Step B: export XML, inject wake trigger, reimport.
$xml = Export-ScheduledTask -TaskName $taskName

# Insert the wake EventTrigger element before the closing </Triggers> tag.
$xml = $xml -replace '</Triggers>', "$wakeXml`n  </Triggers>"

# Re-register from modified XML.
Register-ScheduledTask -Xml $xml -TaskName $taskName -Force | Out-Null

Write-Host "  Task '$taskName' registered." -ForegroundColor Green
Write-Host "  Triggers:" -ForegroundColor Green
Write-Host "    • At logon (10 s delay)" -ForegroundColor Green
Write-Host "    • On wake from sleep — Kernel-Power Event 107 (20 s delay)" -ForegroundColor Green

Write-Host "Verifying task..." -ForegroundColor Cyan
$t = Get-ScheduledTask -TaskName $taskName
Write-Host "  Task state : $($t.State)" -ForegroundColor Green
$triggers = $t.Triggers | ForEach-Object { $_.GetType().Name }
Write-Host "  Triggers   : $($triggers -join ', ')" -ForegroundColor Green

Write-Host "`n=== Setup complete ===" -ForegroundColor Yellow
Write-Host "Fast Startup  : OFF"
Write-Host "Hybrid Sleep  : OFF (AC)"
Write-Host "NIC Power Sav : OFF"
Write-Host "Auto-start    : ON (login + wake)"
Write-Host "`nNext steps:"
Write-Host "  1. Reboot once so all NIC settings take effect."
Write-Host "  2. After reboot, bot should start on its own — check Telegram for the banner."
Write-Host "  3. Let PC sleep, send WoL packet, wait 40 s, text the bot."
Write-Host ""
