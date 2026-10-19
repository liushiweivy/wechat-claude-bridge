# WeChat auto-login at logon, invoked hidden by autostart-*.bat.
#
# Flow (drill-proved 2026-10-19):
#   wait StartupDelaySec -> if already logged in, exit
#   -> MinimizeAll -> start WeChat.exe if not running -> wait login window
#   -> REPEAT up to 6 rounds (other autostart apps may keep stealing focus):
#        MinimizeAll again (push their windows down) -> restore login window
#        -> steal foreground with AttachThreadInput (plain SetForegroundWindow
#           loses to the Windows foreground lock; drill #1 sent ENTER to QQ)
#        -> ENTER (the green button is the login window's default button)
#      login window gone = logged in.
#   -> last resort: pixel-locate the #07C160 green button in the login rect
#      and physical-click it.
#
# Single-instance via global mutex (both autostart bats may launch it).
# Log: logs\autologin.log. Pure ASCII for GBK-safe cmd/PowerShell 5.1.
param([int]$StartupDelaySec = 15, [int]$WaitLoginSec = 180, [int]$Rounds = 6)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
function Log($msg) {
  Add-Content -Path (Join-Path $LogDir 'autologin.log') -Encoding ASCII `
    -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\WeChatAutologinOnce')
if (-not $mutex.WaitOne(0)) { Log 'another instance running, exit'; exit 0 }

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WL {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  public struct RECT { public int Left, Top, Right, Bottom; }
  public static IntPtr FindClass(string cls) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      var cn = new StringBuilder(256); GetClassName(h, cn, 256);
      if (cn.ToString() == cls) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
[void][WL]::SetProcessDpiAwarenessContext([IntPtr](-4))

Start-Sleep -Seconds $StartupDelaySec
Log "autologin start (delay=${StartupDelaySec}s rounds=$Rounds)"

if ([WL]::FindClass('WeChatMainWndForPC') -ne [IntPtr]::Zero) {
  Log 'already logged in, nothing to do'; exit 0
}

(New-Object -ComObject Shell.Application).MinimizeAll()
Start-Sleep -Seconds 2
if (-not (Get-Process WeChat -ErrorAction SilentlyContinue)) {
  Start-Process -FilePath 'E:\Program Files\WeChat\WeChat.exe'
  Log 'WeChat.exe was not running, started it'
}

# wait for login window (main window may also appear = auto-logged-in)
$deadline = (Get-Date).AddSeconds($WaitLoginSec)
while ((Get-Date) -lt $deadline) {
  if ([WL]::FindClass('WeChatMainWndForPC') -ne [IntPtr]::Zero) {
    Log 'logged in while waiting, done'; exit 0
  }
  if ([WL]::FindClass('WeChatLoginWndForPC') -ne [IntPtr]::Zero) { break }
  if (-not (Get-Process WeChat -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'E:\Program Files\WeChat\WeChat.exe'
    Log 'WeChat.exe vanished, restarted it'
  }
  Start-Sleep -Seconds 3
}

# repeat MinimizeAll + ENTER: later autostart apps can pop windows mid-login
for ($round = 1; $round -le $Rounds; $round++) {
  $login = [WL]::FindClass('WeChatLoginWndForPC')
  if ($login -eq [IntPtr]::Zero) {
    if ([WL]::FindClass('WeChatMainWndForPC') -ne [IntPtr]::Zero) {
      Log "round ${round}: already logged in, done"; exit 0
    }
    Log "round ${round}: login window missing, retry"; Start-Sleep -Seconds 3; continue
  }
  # push down any window that other autostart apps just opened
  (New-Object -ComObject Shell.Application).MinimizeAll()
  Start-Sleep -Milliseconds 800
  $fg = [WL]::GetForegroundWindow()
  $fgThread = [WL]::GetWindowThreadProcessId($fg, [ref]([uint32]0))
  $me = [WL]::GetCurrentThreadId()
  [void][WL]::AttachThreadInput($me, $fgThread, $true)
  [void][WL]::ShowWindow($login, 9)            # SW_RESTORE (MinimizeAll hid it)
  [void][WL]::BringWindowToTop($login)
  [void][WL]::SetForegroundWindow($login)
  [void][WL]::AttachThreadInput($me, $fgThread, $false)
  Start-Sleep -Milliseconds 600
  if ([WL]::GetForegroundWindow() -eq $login) {
    (New-Object -ComObject WScript.Shell).SendKeys('{ENTER}')
    Log "round ${round}: ENTER sent (foreground secured)"
  } else {
    Log "round ${round}: foreground steal failed, will retry"
  }
  for ($i = 0; $i -lt 5; $i++) {
    Start-Sleep -Seconds 1
    if ([WL]::FindClass('WeChatLoginWndForPC') -eq [IntPtr]::Zero) {
      Log "SUCCESS after round ${round}"; exit 0
    }
  }
}

# last resort: physical click on the green button, located inside login rect
$login = [WL]::FindClass('WeChatLoginWndForPC')
if ($login -ne [IntPtr]::Zero) {
  $r = New-Object WL+RECT; [void][WL]::GetWindowRect($login, [ref]$r)
  Add-Type -AssemblyName System.Windows.Forms,System.Drawing
  $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $vs.Width, $vs.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($vs.Left, $vs.Top, 0, 0, $bmp.Size); $g.Dispose()
  $minX=99999;$maxX=0;$minY=99999;$maxY=0
  for ($y = $r.Top; $y -lt $r.Bottom; $y += 2) {
    for ($x = $r.Left; $x -lt $r.Right; $x += 2) {
      $c = $bmp.GetPixel($x, $y)
      if ($c.G -gt 150 -and $c.R -lt 90 -and $c.B -lt 150 -and ($c.G - $c.R) -gt 90) {
        if ($x -lt $minX){$minX=$x}; if ($x -gt $maxX){$maxX=$x}
        if ($y -lt $minY){$minY=$y}; if ($y -gt $maxY){$maxY=$y}
      }
    }
  }
  $bmp.Dispose()
  if ($minX -ne 99999) {
    $cx = [int](($minX + $maxX) / 2); $cy = [int](($minY + $maxY) / 2)
    Log "fallback green-button click at $cx,$cy"
    [void][WL]::SetCursorPos($cx, $cy); Start-Sleep -Milliseconds 300
    [WL]::mouse_event(0x02, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 80
    [WL]::mouse_event(0x04, 0, 0, 0, [IntPtr]::Zero)
    for ($i = 0; $i -lt 12; $i++) {
      Start-Sleep -Seconds 1
      if ([WL]::FindClass('WeChatLoginWndForPC') -eq [IntPtr]::Zero) {
        Log 'SUCCESS after fallback click'; exit 0
      }
    }
  } else { Log 'fallback failed: no green button in login rect' }
}
Log 'FAIL: login window still present after all attempts'; exit 2
