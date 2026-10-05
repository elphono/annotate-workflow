<#
    annotate tray icon: Windows PowerShell 5.1, WinForms NotifyIcon.

    Derived from remarkable-sync's rmpapier/windows/plateau.ps1, reduced to
    what this tool needs: one icon whose colour says what the daemon holds,
    one context menu built from the daemon's /api/docs when it opens, and
    start/stop/restart of the systemd unit through wsl.exe.

    IT TALKS TO THE DAEMON OVER HTTP ONLY (WSL2 relays the loopback). The single
    exception is the daemon control, which has to reach systemd inside WSL.

    NO PATH, NO DISTRIBUTION, NO PORT IS WRITTEN HERE: they arrive as
    parameters computed by `annotate tray` from the environment. A test of
    the repository checks it.

    ASCII ONLY. A .ps1 without BOM is read by PowerShell 5.1 in the ANSI code
    page of the machine: one em dash in a comment was enough to break the
    parsing of plateau.ps1 (measured 2026-09-17). A test checks it.

    -Once prints what the menu would list, then exits: the one thing a
    session without a screen can prove of this script.
#>
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$Distro = '',
    [string]$LinuxHome = '',
    [string]$Unit = '',
    [switch]$Once
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Windows PowerShell writes stdout in the OEM code page; document titles are
# accented. Measured in remarkable-sync: without this, -Once was undecodable.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# Only the loopback is ever called: no system proxy may intercept it.
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy

# 127.0.0.1, NEVER localhost: with WSL networkingMode=mirrored, Windows
# tries localhost as ::1 first, which never reaches a socket bound to
# 127.0.0.1 in WSL (measured 2026-10-05: 22 ms against a 6 s timeout).
$BaseUrl = "http://127.0.0.1:$Port"
$ApiHeaders = @{ 'X-Annotate' = 'tray' }
$RefreshMs = 10000

function Invoke-Api {
    <# Calls the daemon. Returns @{ Ok; Data; Error } and never throws:
       a refusal has a text, and the text is what the user needs. #>
    param([string]$Method, [string]$Path)
    try {
        $data = Invoke-RestMethod -Uri ($BaseUrl + $Path) -Method $Method `
            -Headers $ApiHeaders -TimeoutSec 15 -UseBasicParsing
        return @{ Ok = $true; Data = $data; Error = '' }
    }
    catch {
        $text = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try { $text = ($_.ErrorDetails.Message | ConvertFrom-Json).error }
            catch { $text = $_.ErrorDetails.Message }
        }
        return @{ Ok = $false; Data = $null; Error = [string]$text }
    }
}

function Get-Docs {
    <# Always returns @{ Ok; Docs (array, maybe empty); Error }. #>
    $r = Invoke-Api -Method 'GET' -Path '/api/docs'
    if (-not $r.Ok) { return @{ Ok = $false; Docs = @(); Error = $r.Error } }
    $docs = @()
    if ($r.Data.PSObject.Properties.Name -contains 'docs' -and $r.Data.docs) {
        $docs = @($r.Data.docs)
    }
    return @{ Ok = $true; Docs = $docs; Error = '' }
}

function Get-Summary {
    <# One state for the icon: down, running (notes handed to a session that
       has not rewritten the document yet), pending, idle. #>
    param($State)
    if (-not $State.Ok) { return @{ Kind = 'down'; Text = 'annotate: daemon unreachable' } }
    $running = @($State.Docs | Where-Object { $_.status -eq 'delivered' }).Count
    $pending = 0
    foreach ($d in $State.Docs) { $pending += [int]$d.pending }
    $count = @($State.Docs).Count
    if ($running -gt 0) { return @{ Kind = 'running'; Text = "annotate: $running document(s) with a session on them" } }
    if ($pending -gt 0) { return @{ Kind = 'pending'; Text = "annotate: $pending note(s) to send" } }
    return @{ Kind = 'idle'; Text = "annotate: $count document(s)" }
}

function Format-DocLine {
    param($Doc)
    $title = [string]$Doc.title
    if (-not $title) { $title = [string]$Doc.path }
    if ($title.Length -gt 48) { $title = $title.Substring(0, 45) + '...' }
    $state = [string]$Doc.status
    if ($Doc.listening) { $state = "$state, session listening" }
    return "$title  [$state, $($Doc.pending)/$($Doc.annotations) to send]"
}

if ($Once) {
    $state = Get-Docs
    if (-not $state.Ok) {
        Write-Output "daemon unreachable at $BaseUrl : $($state.Error)"
        exit 1
    }
    Write-Output ("daemon at $BaseUrl, " + @($state.Docs).Count + ' document(s); icon: ' +
        (Get-Summary $state).Kind)
    foreach ($doc in $state.Docs) {
        Write-Output ("  " + $doc.id + "  " + (Format-DocLine $doc))
    }
    exit 0
}

Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool DestroyIcon(System.IntPtr handle);
'@ -Name Native -Namespace AnnotateTray
# Before any window, or the text is drawn at 96 dpi and stretched (blurry).
[void][AnnotateTray.Native]::SetProcessDPIAware()
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Colours = @{
    down    = [System.Drawing.Color]::FromArgb(148, 163, 184)
    idle    = [System.Drawing.Color]::FromArgb(22, 163, 74)
    pending = [System.Drawing.Color]::FromArgb(234, 88, 12)
    running = [System.Drawing.Color]::FromArgb(37, 99, 235)
}
# One icon per kind, built once. Icon::FromHandle does not own the HICON
# GetHicon() allocates: rebuilding it at every tick leaks GDI handles until
# the process dies (measured in plateau.ps1: about a day and a half).
$IconCache = @{}

function Get-KindIcon {
    param([string]$Kind)
    if ($IconCache.ContainsKey($Kind)) { return $IconCache[$Kind] }
    $side = [System.Windows.Forms.SystemInformation]::SmallIconSize.Width
    if ($side -lt 16) { $side = 16 }
    $bmp = New-Object System.Drawing.Bitmap $side, $side
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $brush = New-Object System.Drawing.SolidBrush $Colours[$Kind]
    $g.FillEllipse($brush, 1, 1, $side - 3, $side - 3)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), ([Math]::Max(1, $side / 8))
    # A pin: a short white stroke, so the icon reads as "annotation".
    $g.DrawLine($pen, $side * 0.35, $side * 0.65, $side * 0.65, $side * 0.35)
    $pen.Dispose(); $brush.Dispose(); $g.Dispose()
    $icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
    $bmp.Dispose()
    $IconCache[$Kind] = $icon
    return $icon
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Visible = $true
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$notify.ContextMenuStrip = $menu

function Show-Failure {
    param([string]$Title, [string]$Text)
    # A failure goes to a dialog, never to a balloon: a balloon passes and
    # is missed, and a missed failure is a silent failure.
    [void][System.Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', 'Warning')
}

function Show-Done {
    param([string]$Text)
    $notify.ShowBalloonTip(4000, 'annotate', $Text, 'Info')
}

function Update-Icon {
    $state = Get-Docs
    $summary = Get-Summary $state
    $notify.Icon = Get-KindIcon $summary.Kind
    $tip = $summary.Text
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }
    $notify.Text = $tip
    return $state
}

function Invoke-DocAction {
    param([string]$Id, [string]$Action, [string]$Title)
    switch ($Action) {
        'open' { Start-Process ($BaseUrl + '/docs/' + $Id); return }
        'send' { $r = Invoke-Api -Method 'POST' -Path "/api/docs/$Id/send" }
        'new-session' { $r = Invoke-Api -Method 'POST' -Path "/api/docs/$Id/new-session" }
        'forget' {
            $ok = [System.Windows.Forms.MessageBox]::Show(
                "Stop tracking this document? Its notes are discarded, the file stays.`n`n$Title",
                'annotate', 'YesNo', 'Question')
            if ($ok -ne 'Yes') { return }
            $r = Invoke-Api -Method 'DELETE' -Path "/api/docs/$Id"
        }
        'delete' {
            $ok = [System.Windows.Forms.MessageBox]::Show(
                "DELETE the file from the disk, and stop tracking it?`n`n$Title",
                'annotate', 'YesNo', 'Warning')
            if ($ok -ne 'Yes') { return }
            $r = Invoke-Api -Method 'DELETE' -Path "/api/docs/$Id`?delete=1"
        }
    }
    if ($r.Ok) {
        if ($Action -eq 'send' -or $Action -eq 'new-session') {
            if ([string]$r.Data.target -eq 'session') {
                Show-Done "$($r.Data.count) note(s) delivered to the open session."
            }
            else { Show-Done "$($r.Data.count) note(s) opened in a terminal tab." }
        }
        else { Show-Done 'Done.' }
    }
    else { Show-Failure "annotate: $Action failed" $r.Error }
    [void](Update-Icon)
}

function Invoke-Daemon {
    <# systemctl --user <verb> <unit>, inside WSL. Blocks at most 30 s: the
       daemon stops in under a second, the bound covers a cold wsl.exe. #>
    param([string]$Verb)
    if (-not $Distro -or -not $Unit -or -not $LinuxHome) {
        Show-Failure 'annotate' ('This icon was started without -Distro, -LinuxHome or -Unit: ' +
            'it cannot reach systemd. Run `annotate tray` again in WSL.')
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'wsl.exe'
    # The distribution is NOT quoted: wsl.exe does not strip quotes from -d
    # (measured in plateau.ps1: WSL_E_DISTRO_NOT_FOUND).
    $psi.Arguments = "-d $Distro --cd `"$LinuxHome`" -- /usr/bin/bash -lc `"systemctl --user $Verb $Unit`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    try { $proc = [System.Diagnostics.Process]::Start($psi) }
    catch { Show-Failure 'annotate' "wsl.exe could not start: $($_.Exception.Message)"; return }
    if (-not $proc.WaitForExit(30000)) {
        Show-Failure 'annotate' "systemctl --user $Verb did not answer within 30 s."
        return
    }
    # wsl.exe writes its OWN diagnostics in UTF-16LE: drop the NULs.
    $said = ($proc.StandardOutput.ReadToEnd() + "`n" + $proc.StandardError.ReadToEnd()).Replace([string][char]0, '').Trim()
    if ($proc.ExitCode -eq 0) { Show-Done "daemon: $Verb accepted by systemd." }
    else { Show-Failure 'annotate' "systemctl --user $Verb $Unit exited $($proc.ExitCode):`n$said" }
    Start-Sleep -Milliseconds 800
    [void](Update-Icon)
}

function Add-Item {
    param($Parent, [string]$Text, [scriptblock]$OnClick)
    $item = New-Object System.Windows.Forms.ToolStripMenuItem $Text
    if ($OnClick) { $item.add_Click($OnClick) }
    [void]$Parent.Items.Add($item)
    return $item
}

function Add-DocItem {
    <# One menu entry bound to a document. The id and the action travel in
       .Tag: a closure over a loop variable would see its LAST value. #>
    param($Parent, [string]$Text, [string]$Id, [string]$Action, [string]$Title)
    $item = New-Object System.Windows.Forms.ToolStripMenuItem $Text
    $item.Tag = @{ Id = $Id; Action = $Action; Title = $Title }
    $item.add_Click({ param($sender, $e)
            $t = $sender.Tag
            Invoke-DocAction -Id $t.Id -Action $t.Action -Title $t.Title })
    [void]$Parent.DropDownItems.Add($item)
}

function Build-Menu {
    $menu.Items.Clear()
    $state = Update-Icon
    if (-not $state.Ok) {
        [void](Add-Item $menu "Daemon unreachable at $BaseUrl (see Daemon > start)" $null)
    }
    elseif (@($state.Docs).Count -eq 0) {
        [void](Add-Item $menu 'No document registered' $null)
    }
    foreach ($doc in $state.Docs) {
        $title = [string]$doc.title
        $entry = New-Object System.Windows.Forms.ToolStripMenuItem (Format-DocLine $doc)
        Add-DocItem $entry 'Open' $doc.id 'open' $title
        Add-DocItem $entry 'Send to session' $doc.id 'send' $title
        Add-DocItem $entry 'New session' $doc.id 'new-session' $title
        [void]$entry.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator))
        Add-DocItem $entry 'Unmanage' $doc.id 'forget' $title
        Add-DocItem $entry 'Delete file...' $doc.id 'delete' $title
        [void]$menu.Items.Add($entry)
    }
    [void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $daemon = New-Object System.Windows.Forms.ToolStripMenuItem 'Daemon'
    foreach ($verb in @('start', 'stop', 'restart')) {
        $item = New-Object System.Windows.Forms.ToolStripMenuItem $verb
        $item.Tag = $verb
        $item.add_Click({ param($sender, $e) Invoke-Daemon -Verb ([string]$sender.Tag) })
        [void]$daemon.DropDownItems.Add($item)
    }
    [void]$menu.Items.Add($daemon)
    [void](Add-Item $menu 'Quit' {
            $timer.Stop()
            $notify.Visible = $false
            $notify.Dispose()
            [System.Windows.Forms.Application]::Exit()
        })
}

# Built when it opens, so it always lists what the daemon holds NOW; the
# timer only recolours the icon (rebuilding an open menu would close it).
$menu.add_Opening({ param($sender, $e) Build-Menu })
$notify.add_MouseClick({ param($sender, $e)
        if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
            Start-Process ($BaseUrl + '/')
        } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $RefreshMs
$timer.add_Tick({ [void](Update-Icon) })
[void](Update-Icon)
Build-Menu
$timer.Start()
[System.Windows.Forms.Application]::Run()
