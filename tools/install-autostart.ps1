# Install logon-autostart shortcuts (no admin needed):
#  - WeChat Claude Bridge : visible cmd window (grace + wait for WeChat + retry)
#  - Claude Console       : visible cmd window running `claude`
# WeChat itself already autostarts via HKCU\Run ("Wechat" = WeChat.exe -autorun).
$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell

$bridge = Join-Path $startup "WeChat Claude Bridge.lnk"
$s = $shell.CreateShortcut($bridge)
$s.TargetPath = "$env:SystemRoot\System32\cmd.exe"
$s.Arguments = '/k "E:\1workspace\wechat-claude-bridge\tools\autostart-visible.bat"'
$s.WorkingDirectory = "E:\1workspace\wechat-claude-bridge"
$s.WindowStyle = 1
$s.Description = "WeChat-Claude bridge visible autostart"
$s.Save()

$claude = Join-Path $startup "Claude Console.lnk"
$c = $shell.CreateShortcut($claude)
$c.TargetPath = "$env:WINDIR\System32\cmd.exe"
$c.Arguments = "/k claude"
$c.WorkingDirectory = "E:\1workspace"
$c.Description = "Claude Code console window"
$c.IconLocation = "$env:APPDATA\npm\node_modules\@anthropic-ai\claude-code\cli-win.exe,0"
$c.Save()

Write-Output ("bridge : " + $bridge + " -> " + (Test-Path $bridge))
Write-Output ("claude : " + $claude + " -> " + (Test-Path $claude))
