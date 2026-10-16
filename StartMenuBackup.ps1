#Requires -Version 5.1
<#
============================================================================
  Windows 11 开始菜单布局（Start2.bin）备份 / 一键恢复工具
============================================================================
  背景：
    开始菜单的磁贴布局（固定项、分组、顺序）实际保存在
    StartMenuExperienceHost 包目录下的 LocalState\Start2.bin 中。
    重装系统后该文件丢失，布局即回到默认。本工具把它备份下来，装好系统后
    一键恢复即可找回原来的布局。

  用法（PowerShell）：
    .\StartMenuBackup.ps1                 # 交互式菜单
    .\StartMenuBackup.ps1 -Action Backup  # 一键备份
    .\StartMenuBackup.ps1 -Action Restore # 一键恢复（列出可选的备份点）
    .\StartMenuBackup.ps1 -Action List    # 列出所有备份点

  特性：
    * 备份保存到脚本同目录的 backups\ 下，含版本/构建号等元信息，跨机器可携带。
    * 恢复前自动备份当前状态（安全可回退），并进行版本兼容性检查。
    * 恢复后自动重启相关进程使布局立即生效。

  注意：
    Start2.bin 对系统版本较敏感。跨「构建号/版本」恢复（例如 LTSC 26100 与
    家庭版 26100 之间、或 23H2 与 24H2 之间）可能无效。工具会先给出警告，
    确认后才继续。
============================================================================
#>

[CmdletBinding()]
param(
    [ValidateSet('Backup', 'Restore', 'List')]
    [string]$Action = ''
)

$ErrorActionPreference = 'Stop'

# ============================================================================
# 基础信息与路径
# ============================================================================
$script:ToolDir    = $PSScriptRoot
$script:BackupsDir = Join-Path $ToolDir 'backups'
$script:PackagePrefix = 'Microsoft.Windows.StartMenuExperienceHost_'
$script:PreferredSuffix = 'cw5n1h2txyewy'
$script:StartBinName = 'Start2.bin'
$script:InfoFileName = 'info.json'

# ============================================================================
# 输出辅助（彩色，降级时为普通文本）
# ============================================================================
function Write-Title([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor Cyan
    Write-Host ('  ' + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor Cyan
}
function Write-Info([string]$Text)  { Write-Host ('[信息] ' + $Text) -ForegroundColor Gray }
function Write-Ok([string]$Text)    { Write-Host ('[成功] ' + $Text) -ForegroundColor Green }
function Write-Warn([string]$Text)  { Write-Host ('[警告] ' + $Text) -ForegroundColor Yellow }
function Write-Err([string]$Text)   { Write-Host ('[错误] ' + $Text) -ForegroundColor Red }

function Read-Confirm([string]$Prompt) {
    $r = Read-Host $Prompt
    return ($r -match '^(y|yes|是|Y|Yes|YES)$')
}

# ============================================================================
# 系统 / 路径信息
# ============================================================================
function Get-OsInfo {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $caption = $os.Caption
    } catch {
        $caption = ''
    }
    $build    = [System.Environment]::OSVersion.Version.Build
    $regPath  = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $display  = ''
    $curBuild = ''
    $ubr      = ''
    try {
        $k = Get-ItemProperty -Path $regPath -ErrorAction Stop
        $display  = [string]$k.DisplayVersion
        $curBuild = [string]$k.CurrentBuild
        $ubr      = [string]$k.UBR
    } catch { }
    return [PSCustomObject]@{
        Caption        = $caption
        OSBuild        = $build
        DisplayVersion = $display
        CurrentBuild   = $curBuild
        UBR            = $ubr
    }
}

# 返回开始菜单包目录列表（按名称排序）
function Get-PackageDirs {
    @(Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Packages') -Directory `
        -Filter ($script:PackagePrefix + '*') -ErrorAction SilentlyContinue | Sort-Object Name)
}

# 定位“当前真正使用”的 Start2.bin 文件路径，返回完整文件路径或 $null
function Get-CurrentStartBinPath {
    $dirs = @(Get-PackageDirs)
    if ($dirs.Count -eq 0) { return $null }

    # 首选 cw5n1h2txyewy（Windows 11 正式后缀），其次按名称
    $ordered = @($dirs | Where-Object { $_.Name -eq ($script:PackagePrefix + $script:PreferredSuffix) }) +
               @($dirs | Where-Object { $_.Name -ne ($script:PackagePrefix + $script:PreferredSuffix) })

    foreach ($d in $ordered) {
        $p = Join-Path $d.FullName ('LocalState\' + $script:StartBinName)
        if (Test-Path -LiteralPath $p) { return $p }
    }
    # 都没有文件：返回首选目录下“预期”的路径
    if ($ordered.Count -gt 0) {
        return Join-Path $ordered[0].FullName ('LocalState\' + $script:StartBinName)
    }
    return $null
}

# 计算文件指纹（大小 + 前 16 字节十六进制），用于粗略判断版本兼容性
function Get-FileFingerprint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return [PSCustomObject]@{ Size = 0; Head = '' }
    }
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($null -eq $bytes -or $bytes.Length -eq 0) {
        return [PSCustomObject]@{ Size = 0; Head = '' }
    }
    $n = [Math]::Min(16, $bytes.Length)
    $head = ($bytes[0..($n - 1)] | ForEach-Object { $_.ToString('X2') }) -join ' '
    return [PSCustomObject]@{ Size = $bytes.Length; Head = $head }
}

function Get-TimeStamp {
    Get-Date -Format 'yyyyMMdd_HHmmss'
}

# ============================================================================
# Backup —— 备份
# ============================================================================
function Invoke-Backup {
    Write-Title '备份 Windows 11 开始菜单布局'

    $src = Get-CurrentStartBinPath
    if (-not $src) {
        Write-Err '未找到 StartMenuExperienceHost 包目录，可能不是 Windows 11，或开始菜单从未使用过。'
        return $false
    }
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Err "未找到布局文件 `n  $src `n请先打开一次开始菜单，再执行备份。"
        return $false
    }

    $osInfo = Get-OsInfo
    $fp     = Get-FileFingerprint $src
    $hash   = (Get-FileHash -LiteralPath $src -Algorithm MD5).Hash
    $stamp  = Get-TimeStamp
    $folderName = ('{0}_build{1}' -f $stamp, $osInfo.CurrentBuild)
    $destDir = Join-Path $script:BackupsDir $folderName

    if (-not (Test-Path -LiteralPath $script:BackupsDir)) {
        New-Item -ItemType Directory -Path $script:BackupsDir -Force | Out-Null
    }
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null

    # 拷贝布局文件
    Copy-Item -LiteralPath $src -Destination (Join-Path $destDir $script:StartBinName) -Force

    # 写元信息
    $info = [PSCustomObject]@{
        CreatedAt       = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        BackedUpFrom    = $src
        OsCaption       = $osInfo.Caption
        OsBuild         = $osInfo.OSBuild
        DisplayVersion  = $osInfo.DisplayVersion
        CurrentBuild    = $osInfo.CurrentBuild
        UBR             = $osInfo.UBR
        FileSize        = $fp.Size
        MD5             = $hash
        HeadHex         = $fp.Head
        PackageFullName = (Split-Path (Split-Path (Split-Path $src -Parent) -Parent) -Leaf)
    }
    $info | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destDir $script:InfoFileName) -Encoding UTF8

    Write-Info "备份文件大小：$($fp.Size) 字节   MD5：$hash"
    Write-Info "系统版本：$($osInfo.Caption)   Build $($osInfo.CurrentBuild).$($osInfo.UBR)（$($osInfo.DisplayVersion)）"
    Write-Ok "已备份到：`n  $destDir"
    return $true
}

# ============================================================================
# List —— 列出备份点
# ============================================================================
function Get-BackupList {
    if (-not (Test-Path -LiteralPath $script:BackupsDir)) { return @() }
    @(Get-ChildItem -LiteralPath $script:BackupsDir -Directory -ErrorAction SilentlyContinue | Sort-Object Name)
}

function Invoke-List {
    Write-Title '已存在的开始菜单布局备份'
    $dirs = @(Get-BackupList)
    if ($dirs.Count -eq 0) {
        Write-Info '当前还没有任何备份。'
        return
    }
    $i = 1
    foreach ($d in $dirs) {
        $bin = Join-Path $d.FullName $script:StartBinName
        $inf = Join-Path $d.FullName $script:InfoFileName
        $size = ''; $meta = ''
        if (Test-Path -LiteralPath $bin) {
            $size = ((Get-Item -LiteralPath $bin).Length.ToString() + ' 字节')
        }
        if (Test-Path -LiteralPath $inf) {
            try {
                $j = Get-Content -LiteralPath $inf -Raw | ConvertFrom-Json
                $meta = '  版本: ' + $j.OsCaption + ' Build ' + $j.CurrentBuild + '.' + $j.UBR +
                        ' (' + $j.DisplayVersion + ')   ' + $j.CreatedAt
            } catch { }
        }
        Write-Host ('[' + $i + '] ' + $d.Name) -ForegroundColor White
        if ($size) { Write-Host ('    文件: Start2.bin (' + $size + ')') -ForegroundColor Gray }
        if ($meta)  { Write-Host ('    ' + $meta) -ForegroundColor Gray }
        $i++
    }
}

# ============================================================================
# Restore —— 恢复
# ============================================================================
function Invoke-Restore {
    Write-Title '恢复 Windows 11 开始菜单布局'

    $dirs = @(Get-BackupList)
    if ($dirs.Count -eq 0) {
        Write-Err '没有可用备份。请先用 -Action Backup 备份一次。'
        return $false
    }

    Invoke-List
    Write-Host ''
    $choice = Read-Host ('请输入要恢复的备份编号 (1-' + $dirs.Count + ')，直接回车=取消')
    if ($choice -eq '') { Write-Info '已取消。'; return $false }
    $idx = 0
    if (-not [int]::TryParse($choice, [ref]$idx) -or $idx -lt 1 -or $idx -gt $dirs.Count) {
        Write-Err '编号无效。'
        return $false
    }
    $backupDir = $dirs[$idx - 1]
    $backupBin = Join-Path $backupDir.FullName $script:StartBinName

    if (-not (Test-Path -LiteralPath $backupBin)) {
        Write-Err "备份目录缺少 Start2.bin：`n  $($backupDir.FullName)"
        return $false
    }

    # 读取备份元信息
    $osInfo = Get-OsInfo
    $info = $null
    $infoPath = Join-Path $backupDir.FullName $script:InfoFileName
    if (Test-Path -LiteralPath $infoPath) {
        try { $info = Get-Content -LiteralPath $infoPath -Raw | ConvertFrom-Json } catch { }
    }

    # 定位目标文件
    $target = Get-CurrentStartBinPath
    if (-not $target) {
        Write-Err "本机未找到开始菜单包目录，无法恢复。"
        return $false
    }
    $targetDir = Split-Path $target -Parent
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    # ---- 版本兼容性检查 ----
    $srcFp = Get-FileFingerprint $backupBin
    $bakFp = $null
    if (Test-Path -LiteralPath $target) { $bakFp = Get-FileFingerprint $target }

    $versionMismatch = $false
    $reason = ''
    if ($info) {
        if ($info.CurrentBuild -and $osInfo.CurrentBuild -and ($info.CurrentBuild -ne $osInfo.CurrentBuild)) {
            $versionMismatch = $true
            $reason += "备份来自 Build $($info.CurrentBuild).$($info.UBR)（$($info.DisplayVersion)），当前系统为 Build $($osInfo.CurrentBuild).$($osInfo.UBR)（$($osInfo.DisplayVersion)）。`n"
        } elseif ($info.DisplayVersion -and $osInfo.DisplayVersion -and ($info.DisplayVersion -ne $osInfo.DisplayVersion)) {
            $versionMismatch = $true
            $reason += "备份来自 $($info.DisplayVersion)，当前系统为 $($osInfo.DisplayVersion)。`n"
        }
    }
    if ($bakFp -and ($srcFp.Head -ne $bakFp.Head -or $srcFp.Size -ne $bakFp.Size)) {
        # 目标已有文件且指纹不同（可能是版本不同），提示但不强制阻断
        if (-not $versionMismatch) {
            # 仅作提示
        }
    }

    if ($versionMismatch) {
        Write-Warn '检测到备份与本机系统版本不一致：'
        Write-Warn $reason
        Write-Warn '跨版本恢复可能导致开始菜单布局不生效甚至异常。'
        if (-not (Read-Confirm '仍要强制恢复吗？(y/N): ')) {
            Write-Info '已取消恢复。'
            return $false
        }
    }

    # ---- 覆盖前先备份当前状态（安全回退）----
    if (Test-Path -LiteralPath $target) {
        $curHash = (Get-FileHash -LiteralPath $target -Algorithm MD5).Hash
        $curFp   = Get-FileFingerprint $target
        $rollbackDir = Join-Path $script:BackupsDir ('rollback_' + (Get-TimeStamp))
        New-Item -ItemType Directory -Path $rollbackDir -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination (Join-Path $rollbackDir $script:StartBinName) -Force
        $rb = [PSCustomObject]@{
            CreatedAt      = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
            BackedUpFrom   = $target
            OsCaption      = $osInfo.Caption
            OsBuild        = $osInfo.OSBuild
            DisplayVersion = $osInfo.DisplayVersion
            CurrentBuild   = $osInfo.CurrentBuild
            UBR            = $osInfo.UBR
            FileSize       = $curFp.Size
            MD5            = $curHash
            HeadHex        = $curFp.Head
            Note           = '恢复前自动保存的当前布局，用于回退'
        }
        $rb | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $rollbackDir $script:InfoFileName) -Encoding UTF8
        Write-Info "已把当前布局备份到回退点：`n  $rollbackDir"
    }

    # ---- 结束开始菜单相关进程 ----
    Write-Info '正在结束开始菜单进程...'
    Get-Process StartMenuExperienceHost -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    # ---- 覆盖写入 ----
    try {
        Copy-Item -LiteralPath $backupBin -Destination $target -Force
        Write-Ok '布局文件已写入。'
    } catch {
        Write-Err "写入失败：$($_.Exception.Message)"
        return $false
    }

    # ---- 重启 Explorer 使生效 ----
    Write-Info '正在重启资源管理器使布局生效...'
    try {
        Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Start-Process explorer -ErrorAction Stop
    } catch {
        Write-Warn '自动重启资源管理器失败，请手动注销重登或重启电脑以生效。'
    }

    Write-Ok '恢复完成。'
    return $true
}

# ============================================================================
# 主流程
# ============================================================================
function Show-Menu {
    Write-Title 'Windows 11 开始菜单布局 备份 / 恢复工具'
    Write-Host '  [1] 备份当前布局'
    Write-Host '  [2] 从备份恢复布局'
    Write-Host '  [3] 列出所有备份'
    Write-Host '  [4] 退出'
    Write-Host ''
    return (Read-Host '请输入选项 (1-4)')
}

# 执行入口
$result = $true
switch ($Action) {
    'Backup'  { $null = Invoke-Backup; break }
    'Restore' { $null = Invoke-Restore; break }
    'List'    { Invoke-List; break }
    default {
        # 交互菜单
        $choice = Show-Menu
        switch ($choice) {
            '1' { $null = Invoke-Backup }
            '2' { $null = Invoke-Restore }
            '3' { Invoke-List; break }
            '4' { Write-Info '已退出。'; return }
            default { Write-Err '无效选项。' }
        }
    }
}

# 仅交互菜单模式下才需要暂停等待按键；-Action 参数模式下直接结束
if ($Action -eq '') {
    Write-Host ''
    Write-Host '按任意键继续...' -ForegroundColor DarkGray
    Read-Host | Out-Null
}