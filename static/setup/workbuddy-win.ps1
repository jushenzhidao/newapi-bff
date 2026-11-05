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
$script:CapabilitiesUrl = "__BFF_ORIGIN__/setup/workbuddy-model-capabilities.txt"
# 站点配置（管理员后台实时下发：模型供应商映射 / credits 标签 / 展示模型白名单）。
# __BFF_ORIGIN__ 由 BFF 下发脚本时替换为用户实际访问的域名（如 workbuddy.oneapis.cn），
# 不要写死域名 —— 旧值 oneworker.cn 已失效，会导致静默降级成无 credits/vendor 的条目。
$script:SiteConfigUrl = "__BFF_ORIGIN__/api/config"
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

# ---------------- 读取站点配置（供应商映射 + credits 标签，失败则维持默认） ----------------
# 供应商映射值支持「供应商&credits」复合形态，BFF /api/config 已拆好下发：
# model_vendors = 模型 → 纯供应商名；model_credits = 模型 → credits 标签。
$modelCredits = @{}
$vendorMap = @{}
$siteModels = @()   # 站点配置「展示模型清单」，用于白名单严格过滤
if ($modelsFetched) {
    Write-Host "正在读取站点配置（模型供应商映射 / credits 标签）..."
    try {
        # PS 5.1 对未标 charset 的 application/json 默认按 ISO-8859-1 解码，
        # 中文 credits（如「超高级」）会乱码 —— 这里取原始字节强制按 UTF-8 解。
        # PS 7+ 没有 RawContentStream，但其默认就是 UTF-8，直接用 Content。
        $scResponse = Invoke-WebRequest -UseBasicParsing -Uri $script:SiteConfigUrl -TimeoutSec 30
        $scProps = @($scResponse.PSObject.Properties.Name)
        if (($scProps -contains 'RawContentStream') -and $null -ne $scResponse.RawContentStream) {
            $sc = [Text.Encoding]::UTF8.GetString($scResponse.RawContentStream.ToArray()) | ConvertFrom-Json
        } else {
            $sc = [string]$scResponse.Content | ConvertFrom-Json
        }
        $scApi = $sc.data.api
        if ($scApi) {
            if ($scApi.models -and @($scApi.models).Count -gt 0) {
                $siteModels = @($scApi.models)
            }
            if ($scApi.model_credits) {
                foreach ($p in $scApi.model_credits.PSObject.Properties) {
                    $v = [string]$p.Value
                    if ($p.Name -and $v) { $modelCredits[$p.Name.ToLower()] = $v }
                }
            }
            if ($scApi.model_vendors) {
                foreach ($p in $scApi.model_vendors.PSObject.Properties) {
                    $v = [string]$p.Value
                    if ($p.Name -and $v) { $vendorMap[$p.Name] = $v }
                }
            }
        }
    } catch {
        Write-Host ("警告：无法读取站点配置（{0}），模型条目将不带 credits、供应商写 Custom。" -f $_.Exception.Message) -ForegroundColor Yellow
    }
}

# ---------------- 白名单过滤（严格匹配，与 cmd/sh 版一致） ----------------
# 只保留与「展示模型清单」完全相等的模型 ID（大小写不敏感）；
# 渠道里的衍生变体（如 claude-fable-5-1）不在清单内，一律不写入。
if ($siteModels.Count -gt 0 -and $modelIDs.Count -gt 0) {
    $allowSet = @{}
    foreach ($m in $siteModels) {
        $k = ([string]$m).Trim().ToLower()
        if ($k) { $allowSet[$k] = $true }
    }
    $filteredIDs = @($modelIDs | Where-Object { $allowSet.ContainsKey(([string]$_).Trim().ToLower()) })
    if ($filteredIDs.Count -gt 0) {
        $modelIDs = $filteredIDs
    }
}

# 按后台「展示模型清单」的配置顺序输出：客户端按 models.json 数组顺序展示，
# 而网关 /v1/models 返回的是字母序，直接用会打乱管理员配置的展示顺序。
if ($siteModels.Count -gt 0 -and $modelIDs.Count -gt 1) {
    $orderMap = @{}
    for ($i = 0; $i -lt $siteModels.Count; $i++) {
        $k = ([string]$siteModels[$i]).Trim().ToLower()
        if ($k -and -not $orderMap.ContainsKey($k)) { $orderMap[$k] = $i }
    }
    $modelIDs = @($modelIDs | Sort-Object { $k = ([string]$_).Trim().ToLower(); if ($orderMap.ContainsKey($k)) { $orderMap[$k] } else { 9999 } })
}

# ---------------- 构造目标模型列表 ----------------
$fixedModels = @()
foreach ($modelID in $modelIDs) {
    $supportsImages = $false
    if ($imageModels.ContainsKey($modelID.ToLower())) {
        $supportsImages = $imageModels[$modelID.ToLower()]
    }
    $creditsValue = ""
    if ($modelCredits.ContainsKey($modelID.ToLower())) {
        $creditsValue = $modelCredits[$modelID.ToLower()]
    }
    $vendorValue = "Custom"
    if ($vendorMap.ContainsKey($modelID)) {
        $vendorValue = $vendorMap[$modelID]
    }
    $fixedModels += [PSCustomObject]@{
        id                 = $modelID
        name               = $modelID
        vendor             = $vendorValue
        credits            = $creditsValue
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
# 白名单模型按配置顺序排在数组最前（客户端按数组顺序展示），
# 其余已有条目顺序保留、跟在后面。
foreach ($m in $fixedModels) {
    if (-not $installedIDs.ContainsKey($m.id)) {
        $updatedModels += $m
        $installedIDs[$m.id] = $true
    }
}
foreach ($item in $models) {
    $itemID = $null
    if ($item -is [PSCustomObject]) { $itemID = $item.id }
    if ($itemID -and $installedIDs.ContainsKey($itemID)) {
        continue
    }
    $updatedModels += $item
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
