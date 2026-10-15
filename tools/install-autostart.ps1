# Install logon-autostart shortcuts (no admin needed):
#  - WeChat Claude Bridge : hidden VBS launcher (grace + wait for WeChat)
#  - Claude Console       : visible cmd window running `claude`
# WeChat itself already autostarts via HKCU\Run ("Wechat" = WeChat.exe -autorun).
$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell

$bridge = Join-Path $startup "WeChat Claude Bridge.lnk"
$s = $shell.CreateShortcut($bridge)
$s.TargetPath = "wscript.exe"
$s.Arguments = '"E:\1workspace\wechat-claude-bridge\tools\autostart.vbs"'
$s.WorkingDirectory = "E:\1workspace\wechat-claude-bridge"
$s.Description = "WeChat-Claude bridge hidden autostart"
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
