' WeChat-Claude Bridge - logon autostart (hidden)
' 1) initial 45s grace period (WeChat itself starts via HKCU\Run -autorun)
' 2) wait for WeChat.exe process (up to 8 minutes for manual login)
' 3) launch autostart-run.bat hidden (it keeps retrying if not logged in)
' Pure ASCII. Install: shortcut to this file in shell:startup.
Option Explicit

Dim sh
Set sh = CreateObject("WScript.Shell")

WScript.Sleep 45000

Function WeChatRunning()
  Dim code
  code = sh.Run("cmd /c tasklist /FI ""IMAGENAME eq WeChat.exe"" | findstr WeChat.exe", 0, True)
  WeChatRunning = (code = 0)
End Function

Dim i
For i = 1 To 24
  If WeChatRunning() Then Exit For
  WScript.Sleep 20000
Next

sh.Run """E:\1workspace\wechat-claude-bridge\tools\autostart-run.bat""", 0, False
