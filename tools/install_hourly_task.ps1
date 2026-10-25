# Install/uninstall the hourly bridge deafness probe scheduled task.
# No admin needed: runs as the current interactive user (wxauto needs the
# desktop session), LogonType Interactive means no password is stored.
#
# Why Task Scheduler instead of a resident pythonw watchdog:
# the resident probe could die silently and its guardian cmd window could be
# closed, so on 2026-10-21 bridge + probe + guardian bat were all gone for
# hours with nobody left to restart anything. A short-lived process launched
# hourly by the system scheduler has no resident state that can rot.
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'WxBridgeHourlyProbe'
$Root = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed scheduled task: $TaskName"
    } else {
        Write-Output "scheduled task not present: $TaskName"
    }
    exit 0
}

$pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$probe   = Join-Path $Root 'tools\bridge_probe.py'
if (-not (Test-Path $pythonw)) { throw "pythonw not found: $pythonw" }
if (-not (Test-Path $probe))   { throw "probe script not found: $probe" }

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ('"{0}"' -f $probe) -WorkingDirectory $Root

# Fire every hour at :11 (off the top of the hour). Build the repetition as a
# bare CIM instance with Interval only: omitting Duration means "repeat
# indefinitely" ([TimeSpan]::MaxValue overflows the task XML schema, and
# New-ScheduledTaskTrigger -RepetitionInterval fills a version-dependent
# default duration otherwise).
# Pick the next future :11 explicitly: today's 00:11 boundary is in the past at
# install time. Task Scheduler does compute repetitions forward from a past
# boundary, but an explicit future start removes the version-dependent doubt
# (and avoids a StartWhenAvailable catch-up run at registration).
$startAt = (Get-Date).Date.AddMinutes(11)
while ($startAt -le (Get-Date)) {
    $startAt = $startAt.AddHours(1)
}
$repClass = Get-CimClass -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler
$repetition = New-CimInstance -CimClass $repClass -ClientOnly
$repetition.Interval = 'PT1H'
$trigger = New-ScheduledTaskTrigger -Once -At $startAt
$trigger.Repetition = $repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2)

$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Hourly WeChat-Claude bridge deafness probe: restarts the bridge when user messages go unanswered, relaunches the guardian bat if the whole chain died.' `
    -Force | Out-Null

# Read back and verify the registration.
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$trig = $task.Triggers[0]
Write-Output ("task       : " + $task.TaskName + " / state=" + $task.State)
Write-Output ("execute    : " + $task.Actions[0].Execute)
Write-Output ("argument   : " + $task.Actions[0].Arguments)
Write-Output ("workdir    : " + $task.Actions[0].WorkingDirectory)
Write-Output ("interval   : " + $trig.Repetition.Interval)
Write-Output ("time limit : " + $task.Settings.ExecutionTimeLimit)
Write-Output ("instances  : " + $task.Settings.MultipleInstances)
Write-Output ("next run   : " + $info.NextRunTime)
Write-Output ("last run   : " + $info.LastRunTime)
