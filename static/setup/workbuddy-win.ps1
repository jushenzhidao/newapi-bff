# ============================================================
# WorkBuddy 模型一键配置脚本（Windows / PowerShell 版）
# 等价于 workbuddy-mac.sh：给 WorkBuddy 自动配置自定义模型。
#
# 用法（PowerShell 中运行）：
#   irm <本脚本URL> | iex
#   或本地运行：
#   powershell -ExecutionPolicy Bypass -File .\workbuddy-win.ps1
#
# 可选参数 / 环境变量（覆盖默认 API 服务地址）：
#   irm <url> | iex -ApiBase https://你的网关/v1
#   或 $env:WORKBUDDY_API_BASE = "https://你的网关/v1"
#
# 配置写入位置：%USERPROFILE%\.workbuddy\models.json
#   （可用 $env:WORKBUDDY_CONFIG_DIR 覆盖目录）
# ============================================================

[CmdletBinding()]
param(
    [string]$ApiBase = "",
    [string]$ConfigDir = ""
)

$ErrorActionPreference = "Stop"

# ---------------- 默认配置 ----------------
$script:DefaultApiBase = "https://ai.modelzoo.tech/v1"
$script:CapabilitiesUrl = "https://workerbuddy.vip/setup/workbuddy-model-capabilities.txt"
# 内置默认模型列表（在线拉取失败时的兜底）
$script:FallbackModelIDs = @("auto","glm-5.2","glm-5.3","glm-5-turbo","deepseek-v4-flash","deepseek-v4-pro","minimax-m3","kimi-k2.7-code","step-3.7-flash")
# 内置图片能力（查找键统一小写）
$script:FallbackImageModels = @("minimax-m3","step-3.7-flash")

# ---------------- 配置目录 / 文件 ----------------
if (-not $ConfigDir) { $ConfigDir = $env:WORKBUDDY_CONFIG_DIR }
if (-not $ConfigDir) { $ConfigDir = Join-Path $env:USERPROFILE ".workbuddy" }
$script:ConfigDir = $ConfigDir
$script:ConfigFile = Join-Path $ConfigDir "models.json"

if (-not $ApiBase) { $ApiBase = $env:WORKBUDDY_API_BASE }
if (-not $ApiBase) { $ApiBase = $script:DefaultApiBase }
$script:ApiBase = $ApiBase.TrimEnd("/")
$script:SetupEndpoint = "$($script:ApiBase)/models"

function Write-ErrorExit($msg) {
    Write-Host ""
    Write-Host ("配置失败：{0}" -f $msg) -ForegroundColor Red
    exit 1
}

# ---------------- 环境校验 ----------------
if ($IsWindows -eq $false) {
    Write-ErrorExit "这个脚本仅支持 Windows（PowerShell）。"
}

if (-not (Get-Command Invoke-RestMethod -ErrorAction SilentlyContinue)) {
    Write-ErrorExit "当前 PowerShell 缺少 Invoke-RestMethod，无法读取模型配置。"
}

# ---------------- 输入 API Key ----------------
Write-Host ""
Write-Host "WorkBuddy 模型一键配置 (Windows)" -ForegroundColor Cyan
Write-Host ("配置：{0}" -f $script:ConfigFile)
Write-Host ""
$apiKey = Read-Host "请输入 API Key"

$apiKey = $apiKey.Trim()
if (-not $apiKey) {
    Write-ErrorExit "API Key 不能为空。"
}

# ---------------- 防误写检查（符号链接 / junction） ----------------
foreach ($path in @($script:ConfigDir, $script:ConfigFile)) {
    if (Test-Path -LiteralPath $path) {
        try {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.LinkType) {
                Write-ErrorExit "「$path」是符号链接，为避免误写入已停止操作。"
            }
        } catch {
            # 忽略读取失败的检查，交给后续写入报错
        }
    }
}

# ---------------- 备份旧配置 ----------------
$backupFile = ""
if (Test-Path -LiteralPath $script:ConfigFile) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupFile = "$($script:ConfigFile).backup-$stamp"
    try {
        Copy-Item -LiteralPath $script:ConfigFile -Destination $backupFile -Force
        Write-Host ("已备份原配置：{0}" -f $backupFile) -ForegroundColor DarkGray
    } catch {
        Write-ErrorExit "无法备份原配置文件：$_"
    }
}

# ---------------- 读取可用模型列表（失败降级内置清单） ----------------
$modelsFetched = $false
$modelIDs = @()
try {
    Write-Host "正在读取可用模型列表..."
    $headers = @{ Authorization = "Bearer $apiKey" }
    $resp = Invoke-RestMethod -Uri $script:SetupEndpoint -Headers $headers -TimeoutSec 30
    if ($resp -and $resp.data) {
        $modelIDs = @($resp.data | ForEach-Object { $_.id } | Where-Object { $_ } | Select-Object -Unique)
    }
    $modelsFetched = $true
} catch {
    Write-Host ("警告：无法在线获取模型列表（{0}），已改用内置默认模型列表。" -f $_.Exception.Message) -ForegroundColor Yellow
}

if ($modelIDs.Count -eq 0) {
    if ($modelsFetched) {
        Write-Host "警告：配置服务未返回有效模型列表，已改用内置默认模型列表。" -ForegroundColor Yellow
    }
    $modelIDs = @($script:FallbackModelIDs)
}

# ---------------- 读取模型图片能力（失败则默认关闭） ----------------
$imageModels = @{}
if ($modelsFetched) {
    Write-Host "正在读取模型图片能力配置..."
    try {
        $capResp = Invoke-WebRequest -Uri $script:CapabilitiesUrl -TimeoutSec 30 -UseBasicParsing
        $capText = $capResp.Content -replace "`r", ""
        foreach ($line in @($capText -split "`n")) {
            $line = $line.Trim().TrimStart([char]0xFEFF)
            if (-not $line -or $line.StartsWith("#")) { continue }
            $parts = $line.Split("|")
            if ($parts.Count -lt 2) { continue }
            $modelID = $parts[0].Trim()
            $raw = $parts[1].Trim().ToLower()
            if (-not $modelID) { continue }
            if ($raw -in @("true","1","yes","on")) {
                $imageModels[$modelID.ToLower()] = $true
            } else {
                $imageModels[$modelID.ToLower()] = $false
            }
        }
    } catch {
        Write-Host "警告：无法读取模型能力配置，所有模型将默认关闭图片输入。" -ForegroundColor Yellow
    }
} else {
    foreach ($m in $script:FallbackImageModels) { $imageModels[$m.ToLower()] = $true }
}

# ---------------- 构造目标模型列表 ----------------
$fixedModels = @()
foreach ($modelID in $modelIDs) {
    $supportsImages = $false
    if ($imageModels.ContainsKey($modelID.ToLower())) {
        $supportsImages = $imageModels[$modelID.ToLower()]
    }
    $fixedModels += [PSCustomObject]@{
        id                 = $modelID
        name               = $modelID
        vendor             = "Custom"
        url                = $script:ApiBase
        apiKey             = $apiKey
        supportsToolCall   = $true
        supportsImages     = $supportsImages
        supportsReasoning  = $false
        useCustomProtocol  = $false
        maxInputTokens     = 200000
        maxOutputTokens    = 65536
    }
}

# ---------------- 合并现有 models.json（保留已有模型，更新/新增目标模型） ----------------
$root = $null
$rootIsObject = $false
if (Test-Path -LiteralPath $script:ConfigFile) {
    try {
        $existing = Get-Content -LiteralPath $script:ConfigFile -Raw -Encoding UTF8
        if ($existing -and $existing.Trim()) {
            $root = $existing | ConvertFrom-Json
        }
    } catch {
        Write-ErrorExit "现有 models.json 不是有效 JSON，已保留原文件和备份。"
    }
}

if ($null -eq $root) {
    $models = @()
} elseif ($root -is [System.Array]) {
    $models = @($root)
} elseif ($root -is [PSCustomObject] -and $root.models -is [System.Array]) {
    $rootIsObject = $true
    $models = @($root.models)
} else {
    Write-ErrorExit "现有 models.json 必须是模型数组，或包含 models 数组的对象。"
}

# id 去重合并：已有条目若 id 匹配目标模型则替换，否则保留；目标新模型追加
$fixedByID = @{}
foreach ($m in $fixedModels) { $fixedByID[$m.id] = $m }

$updatedModels = @()
$installedIDs = @{}
foreach ($item in $models) {
    $itemID = $null
    if ($item -is [PSCustomObject]) { $itemID = $item.id }
    if ($itemID -and $fixedByID.ContainsKey($itemID)) {
        if (-not $installedIDs.ContainsKey($itemID)) {
            $updatedModels += $fixedByID[$itemID]
            $installedIDs[$itemID] = $true
        }
        continue
    }
    $updatedModels += $item
}
foreach ($m in $fixedModels) {
    if (-not $installedIDs.ContainsKey($m.id)) {
        $updatedModels += $m
        $installedIDs[$m.id] = $true
    }
}

if ($rootIsObject) {
    $root.models = $updatedModels
    $json = $root | ConvertTo-Json -Depth 8
} else {
    $json = $updatedModels | ConvertTo-Json -Depth 8
}

# ---------------- 原子写入 ----------------
$tempFile = Join-Path $env:TEMP "workbuddy-models.json.tmp.$([guid]::NewGuid().ToString('N'))"
try {
    [System.IO.File]::WriteAllText($tempFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tempFile -Destination $script:ConfigFile -Force
} catch {
    if (Test-Path -LiteralPath $tempFile) { Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue }
    Write-ErrorExit "无法写入配置文件：$_"
}

# ---------------- 完成 ----------------
Write-Host ""
Write-Host "配置成功。" -ForegroundColor Green
Write-Host ("模型：{0}" -f ($modelIDs -join "、"))
Write-Host ("文件：{0}" -f $script:ConfigFile)
if ($backupFile) { Write-Host ("备份：{0}" -f $backupFile) }
Write-Host ""
Write-Host "如果对话框右下角没有显示自定义模型，请重新打开 WorkBuddy。"
