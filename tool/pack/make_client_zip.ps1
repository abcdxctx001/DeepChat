# ============================================================
#  DeepChat 客户端完整包 client.zip 增量更新
#  以现有 server/workspace/client.zip 为基底，仅替换/追加：
#    client/agent.py / client/agent.exe / client/run.bat
#    client/control.py / client/icon.ico / client/icon.png / client/tray.png
#  （内置 python / node / mingw64 / GUI 等运行时保持不变）
#
#  ★ 2026-10-10：把 control.py 与图标也纳入更新清单。
#    这个 zip 是用户从「下载本地代理」界面拿到的**分发物**，必须自带全部必需组件
#    —— 缺 control.py 的话，双击 run.bat 只会看到"正在启动…"打点 30 秒然后超时
#    （实测踩到：run.bat 找不到控制面板模块，却没有任何可读的提示）。
#    原来的增量清单只有 3 个文件，新加的组件永远不会被放进来。
# ============================================================
param(
    [string]$Root = (Join-Path $PSScriptRoot '..\..'),   # DeepChat 根目录
    [string]$AgentExeNew = ''                             # 新打包的 agent.exe（缺省用 client/agent.exe）
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zipPath = Join-Path $Root 'server\workspace\client.zip'
$clientDir = Join-Path $Root 'client'
if (-not $AgentExeNew) { $AgentExeNew = Join-Path $clientDir 'agent.exe' }

if (-not (Test-Path $zipPath)) { Write-Host "[client.zip] 基底不存在: $zipPath"; exit 1 }

# 需要同步进分发包的客户端文件（zip 内名称 <- 本地路径）
$items = [ordered]@{
    'client/agent.py'        = (Join-Path $clientDir 'agent.py')
    'client/run.bat'         = (Join-Path $clientDir 'run.bat')
    'client/agent.exe'       = $AgentExeNew
    'client/control.py'      = (Join-Path $clientDir 'control.py')
    'client/static/icon.ico' = (Join-Path $clientDir 'static\icon.ico')
    'client/static/icon.png' = (Join-Path $clientDir 'static\icon.png')
    'client/static/tray.png' = (Join-Path $clientDir 'static\tray.png')
}

foreach ($k in $items.Keys) {
    $f = $items[$k]
    if (-not (Test-Path $f)) {
        Write-Host "[client.zip] 缺少必需文件: $k <- $f"
        Write-Host "             分发包必须自带全部必需组件，已中止。"
        exit 1
    }
}

Write-Host "[client.zip] 更新: $zipPath"
# 需要删除的条目 = 本次要写入的 + 历史遗留的旧位置。
# ★ 增量更新只删清单里的条目，所以图标从包根移到 static\ 之后，
#   旧的 client/icon.ico 等会**留在包里**（实测发现包里两套图标并存）。
$stale = @('client/icon.ico', 'client/icon.png', 'client/tray.png')
$replace = @($items.Keys) + $stale
$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite)
try {
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Update)
    try {
        # 1) 删除将被替换的旧条目
        $toDelete = @($zip.Entries | Where-Object { $replace -contains $_.FullName })
        foreach ($e in $toDelete) { $e.Delete() }
        # 2) 追加新条目
        function Add-One($zip, $entryName, $filePath) {
            $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $in = [System.IO.File]::OpenRead($filePath)
            try {
                $out = $entry.Open()
                try { $in.CopyTo($out) } finally { $out.Dispose() }
            } finally { $in.Dispose() }
            Write-Host "    + $entryName ($((Get-Item $filePath).Length) bytes)"
        }
        foreach ($k in $items.Keys) { Add-One $zip $k $items[$k] }
    } finally { $zip.Dispose() }
} finally { $fs.Dispose() }
Write-Host "[client.zip] 完成 ✔"
