' Open a VISIBLE CC window for the bridge /open command.
' Args: <cwd> <claude_bin> <session_id|"">
' Why wscript: the bridge itself may run as a hidden windowless process;
' "start" spawned from it inherits the hidden state (window never shows).
' wscript is a GUI app - sh.Run with style 1 gives a normal visible window.
' Pure ASCII.
Option Explicit

Dim sh, cwd, bin, sid, inner, title
Set sh = CreateObject("WScript.Shell")
cwd = WScript.Arguments(0)
bin = WScript.Arguments(1)
sid = WScript.Arguments(2)

' bypassPermissions: the bridge injects WeChat prompts into this window;
' default mode would block on interactive permission prompts and hang tasks.
' (User env is sandbox-free, same as config.json.)
' WARNING: keep this file PURE ASCII - wscript parses .vbs as ANSI/GBK,
' UTF-8 Chinese comments corrupt following code lines (2026-10-18 bug:
' inner stayed Empty, window opened with no claude command at all).
inner = bin & " --permission-mode bypassPermissions"
title = "Claude (wechat)"
If sid <> "" Then
  inner = inner & " --resume " & sid
  title = "Claude-" & Left(sid, 8)
End If

sh.CurrentDirectory = cwd
sh.Run "cmd /k ""title " & title & " & " & inner & """", 1, False
