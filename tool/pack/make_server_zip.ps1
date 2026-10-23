# ============================================================
#  DeepChat 服务器分发包 server.zip 全新组装
#  结构参考原包（server/workspace 下仅含 client.zip 引导包）：
#    根 README.md（写成包内 server/README.md）, api-key.env(肯德基梗占位,不写真实Key),
#    server/app.py, app.exe(新),
#    server/control.py, icon.ico, icon.png, tray.png,
#    server/run.bat,
#    server/static/**, server/templates/**, server/workspace/client.zip(新)
#
#  ★ 2026-10-10：把 control.py 与图标纳入包内。
#    控制面板是产品自带功能，server/app.py 会 `import control`，
#    图标也由 control.py 从**同目录**查找 —— 少一个，面板就起不来。
#    它们是必需组件，必须随分发包一起走，不能只存在于源码仓库里。
# ============================================================
param(
    [string]$Root = (Join-Path $PSScriptRoot '..\..'),
    [string]$AppExeNew = ''                       # 新打包的 app.exe
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$serverDir = Join-Path $Root 'server'
$outPath   = Join-Path $Root 'server\workspace\server.zip'
$clientZip = Join-Path $Root 'server\workspace\client.zip'
if (-not $AppExeNew) { $AppExeNew = Join-Path $serverDir 'app.exe' }
if (-not (Test-Path $AppExeNew)) {
    $alt = Join-Path $serverDir 'app_new.exe'
    if (Test-Path $alt) { $AppExeNew = $alt } else { Write-Host "[server.zip] 缺少新 app.exe: $AppExeNew"; exit 1 }
}
# 必需文件：缺任何一个都中止，绝不产出"看起来正常但跑不起来"的包。
$required = @(
    $clientZip,
    (Join-Path $serverDir 'app.py'),
    (Join-Path $serverDir 'run.bat'),
    (Join-Path $Root 'README.md'),          # ★ 单一 README：用仓库根的，打进包里作 server/README.md
    (Join-Path $serverDir 'control.py'),
    (Join-Path $serverDir 'static\icon.ico'),
    (Join-Path $serverDir 'static\icon.png'),
    (Join-Path $serverDir 'static\tray.png')
)
foreach ($f in $required) {
    if (-not (Test-Path $f)) {
        Write-Host "[server.zip] 缺少必需文件: $f"
        Write-Host "             分发包必须自带全部必需组件，已中止。"
        exit 1
    }
}
$indexHtml = Join-Path $serverDir 'templates\index.html'
if (-not (Test-Path $indexHtml)) { Write-Host "[server.zip] 缺少 templates\index.html"; exit 1 }

# ★ 2026-10-05：api-key.env 已撤销 —— API 接入信息（base_url / api_key）统一放进
#   server/workspace/settings.json，可在前端「设置 → 模型与价格」里修改。
#   包内只写一个**不含密钥**的初始 settings.json；真实 key 绝不进包。
Write-Host "[server.zip] 组装 -> $outPath"
Write-Host "    app.exe   <- $AppExeNew"
Write-Host "    workspace/settings.json <- 初始占位（api_key 为空，非真实密钥）"

if (Test-Path $outPath) { Remove-Item $outPath -Force }

$fs = [System.IO.File]::Open($outPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::ReadWrite)
try {
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        function Add-DirEntry($zip, $name) {
            $entry = $zip.CreateEntry($name + '/')
            $entry.Open().Dispose()
        }
        function Add-File($zip, $entryName, $filePath) {
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $filePath, $entryName, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
        Add-DirEntry $zip 'server'
        # ★ 2026-10-21：仓库只保留**一份**根 README；这里把它写成包内的 server/README.md，
        #   这样只拿到 server.zip 的人也有完整文档。
        Add-File $zip 'server/README.md'   (Join-Path $Root 'README.md')
        # workspace/settings.json：只写初始占位（api_key 留空，base_url 用默认）——绝不含真实密钥。
        # 首次运行后用户在前端「设置 → 模型与价格」里填自己的 key。
        Add-DirEntry $zip 'server/workspace'
        $stEntry = $zip.CreateEntry('server/workspace/settings.json', [System.IO.Compression.CompressionLevel]::Optimal)
        $sw = New-Object System.IO.StreamWriter($stEntry.Open(), (New-Object System.Text.UTF8Encoding($false)))
        $stPlaceholder = @'
{
  "base_url": "https://api.deepseek.com",
  "api_key": ""
}
'@
        try { $sw.Write($stPlaceholder) } finally { $sw.Dispose() }
        Add-File $zip 'server/app.py'      (Join-Path $serverDir 'app.py')
        Add-File $zip 'server/app.exe'     $AppExeNew
        Add-File $zip 'server/run.bat'     (Join-Path $serverDir 'run.bat')
        # 控制面板模块：app.py 会 import control（图标在 static\ 下、由下面的
        # static/** 递归一并打包，两端目录结构保持一致）
        Add-File $zip 'server/control.py'  (Join-Path $serverDir 'control.py')
        # static/**（递归，含 katex 字体）
        $staticDir = Join-Path $serverDir 'static'
        Add-DirEntry $zip 'server/static'
        $all = Get-ChildItem -Path $staticDir -Recurse -File
        foreach ($f in $all) {
            $rel = $f.FullName.Substring($staticDir.Length).TrimStart('\').Replace('\', '/')
            Add-File $zip ('server/static/' + $rel) $f.FullName
        }
        # templates/index.html
        Add-DirEntry $zip 'server/templates'
        Add-File $zip 'server/templates/index.html' $indexHtml
        # workspace/client.zip（全新 client.zip）
        Add-DirEntry $zip 'server/workspace'
        Add-File $zip 'server/workspace/client.zip' $clientZip
        Write-Host "    条目数: $($zip.Entries.Count)"
    } finally { $zip.Dispose() }
} finally { $fs.Dispose() }
Write-Host "[server.zip] 完成 ✔ -> $outPath"
