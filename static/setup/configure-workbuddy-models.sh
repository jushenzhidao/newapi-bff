#!/bin/bash
# ============================================================
# WorkBuddy 模型一键配置（Windows 版 · Git Bash + Node.js）
# ------------------------------------------------------------
# 由 macOS 版（osascript/JXA）移植而来：
#   - JSON 合并引擎由 osascript/JXA 改为 Node.js（飞哥机器自带 Node，无需额外依赖）
#   - 平台检查从 "仅 Darwin" 改为依赖检查（node / curl）
#   - API Key 读取从 /dev/tty 改为 stdin（支持环境变量 WORKBUDDY_API_KEY 跳过输入）
#   - 图片能力清单支持「在线 + 本地补充」合并，适配 new-api 渠道模型
# 核心逻辑不变：
#   new-api 的 /v1/models 受 token model_limits 白名单控制（带 Authorization 时
#   只返回该 token 可用模型）→ 与现有 models.json 合并（命中则更新、缺失则追加、
#   其它模型保留）→ 原子替换 + 自动备份。
# 用法：先在下方改 API_BASE_URL，然后在 Git Bash 中执行：
#   bash configure-workbuddy-models.sh
# ============================================================

set -euo pipefail

# ===================== 已按你的 new-api 渠道配置 =====================
API_BASE_URL="https://api.aihuobao.cn/v1"

# 在线图片能力清单（WorkBuddy 官方，尽力拉取；失败不阻塞配置）
CAPABILITIES_URL="https://workbuddy.oneworker.cn/setup/workbuddy-model-capabilities.txt"

# 本地补充图片能力：new-api 渠道中支持「图片输入」的模型，一行一个，查找键统一小写。
# 在线清单拉取失败时本清单依然生效。
# 你渠道 5 个模型中：gpt-4o / gpt-4o-mini / claude-sonnet-4 / gemini-2.0-flash
# 支持图片输入；deepseek-chat 不支持（不列出 = 默认关闭图片输入）。
EXTRA_CAPABILITIES='# aihuobao.cn 渠道中支持图片输入的模型
gpt-4o|true
gpt-4o-mini|true
claude-sonnet-4|true
gemini-2.0-flash|true'

# 内置兜底模型列表：在线拉取失败时的降级清单，与 /v1/models 同构
#（已同步为 aihuobao.cn 白名单模型）。
FALLBACK_MODELS_JSON='{"object":"list","data":[{"id":"gpt-4o"},{"id":"gpt-4o-mini"},{"id":"claude-sonnet-4"},{"id":"deepseek-chat"},{"id":"gemini-2.0-flash"}]}'
# ============================================================

CONFIG_DIR="${HOME}/.workbuddy"
CONFIG_FILE="${CONFIG_DIR}/models.json"
BACKUP_FILE=""
TEMP_DIR=""
TEMP_CONFIG=""

print_error() {
  printf '\n配置失败：%s\n' "$1" >&2
}

cleanup() {
  if [ -n "${TEMP_CONFIG}" ] && [ -f "${TEMP_CONFIG}" ]; then
    rm -f "${TEMP_CONFIG}"
  fi
  if [ -n "${TEMP_DIR}" ] && [ -d "${TEMP_DIR}" ]; then
    rm -rf "${TEMP_DIR}"
  fi
}

trap cleanup EXIT INT TERM

# ---- 依赖检查（Windows / Git Bash）----
for cmd in node curl; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    print_error "缺少 ${cmd}，请先安装（Node.js: https://nodejs.org）。"
    exit 1
  fi
done

# 将 POSIX 路径转换为 Windows 路径传给 Node（Git Bash 调用原生程序的标准做法）
winpath() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1" 2>/dev/null || printf '%s' "$1"
  else
    printf '%s' "$1"
  fi
}

printf '\nWorkBuddy 模型一键配置（Windows 版）\n'
printf '配置：%s\n\n' "${CONFIG_FILE}"

# ---- 读取 API Key（支持环境变量覆盖，避免交互）----
if [ -n "${WORKBUDDY_API_KEY:-}" ]; then
  API_KEY="${WORKBUDDY_API_KEY}"
else
  printf '请输入 API Key：'
  IFS= read -r API_KEY || true
fi

API_KEY=${API_KEY%$'\r'}
if [ -z "${API_KEY}" ]; then
  print_error "API Key 不能为空（可设置环境变量 WORKBUDDY_API_KEY 跳过输入）。"
  exit 1
fi

# 符号链接保护（Windows 上为 junction/symlink 场景，尽力检测）
if [ -L "${CONFIG_DIR}" ]; then
  print_error "${CONFIG_DIR} 是符号链接，为避免误写入已停止操作。"
  exit 1
fi
if [ -L "${CONFIG_FILE}" ]; then
  print_error "${CONFIG_FILE} 是符号链接，为避免误写入已停止操作。"
  exit 1
fi

umask 077
mkdir -p "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}" 2>/dev/null || true

if [ -f "${CONFIG_FILE}" ]; then
  BACKUP_FILE="${CONFIG_FILE}.backup-$(date '+%Y%m%d-%H%M%S')-$$"
  cp -p "${CONFIG_FILE}" "${BACKUP_FILE}"
  chmod 600 "${BACKUP_FILE}" 2>/dev/null || true
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/workbuddy-install.XXXXXX")
chmod 700 "${TEMP_DIR}" 2>/dev/null || true
KEY_FILE="${TEMP_DIR}/api-key"
NODE_FILE="${TEMP_DIR}/update-models.js"
SETUP_FILE="${TEMP_DIR}/setup.json"
FALLBACK_FILE="${TEMP_DIR}/fallback-models.json"
CAPABILITIES_FILE="${TEMP_DIR}/model-capabilities.txt"
CURL_CONFIG="${TEMP_DIR}/curl.conf"
TEMP_CONFIG="${CONFIG_DIR}/.models.json.tmp.$$"

printf '%s' "${API_KEY}" > "${KEY_FILE}"
chmod 600 "${KEY_FILE}" 2>/dev/null || true
printf '%s' "${FALLBACK_MODELS_JSON}" > "${FALLBACK_FILE}"
chmod 600 "${FALLBACK_FILE}" 2>/dev/null || true
printf 'header = "Authorization: Bearer %s"\n' "${API_KEY}" > "${CURL_CONFIG}"
chmod 600 "${CURL_CONFIG}" 2>/dev/null || true

# ---- 拉取 /v1/models（new-api：token model_limits 白名单决定返回哪些模型）----
printf '\n正在读取可用模型列表...\n'
# 不区分失败类型：连接失败与 HTTP 非 200（401/403 等可能是 API Key 问题）一律降级到
# 内置默认清单（写入 SETUP_FILE，走与在线结果完全相同的后续解析）；HTTP 200 但空/无效
# 列表的情况由下方 Node 再兜一层。保证配置总能离线完成，而不是失败退出。
MODELS_FETCH_FAILED=0
if ! HTTP_STATUS=$(curl --silent --show-error --location \
  --connect-timeout 10 --max-time 30 \
  --output "$(winpath "${SETUP_FILE}")" --write-out '%{http_code}' \
  --config "$(winpath "${CURL_CONFIG}")" "${API_BASE_URL%/}/models"); then
  MODELS_FETCH_FAILED=1
elif [ "${HTTP_STATUS}" != "200" ]; then
  MODELS_FETCH_FAILED=1
fi
API_KEY=""
if [ "${MODELS_FETCH_FAILED}" = "1" ]; then
  case "${HTTP_STATUS:-}" in
    ""|000) MODELS_FAIL_REASON="无法连接配置服务" ;;
    *) MODELS_FAIL_REASON="配置服务返回 HTTP ${HTTP_STATUS}" ;;
  esac
  printf '警告：无法在线获取模型列表（%s），已改用内置默认模型列表。\n' "${MODELS_FAIL_REASON}" >&2
  printf '%s' "${FALLBACK_MODELS_JSON}" > "${SETUP_FILE}"
fi
chmod 600 "${SETUP_FILE}" 2>/dev/null || true

# ---- 图片能力清单：先写在线内容（低优先级），再追加本地补充（高优先级，覆盖同名键）----
if [ "${MODELS_FETCH_FAILED}" = "1" ]; then
  # 兜底模式：连接层已不可用，在线能力配置同样拉不到，仅使用本地补充清单。
  : > "${CAPABILITIES_FILE}"
  printf '警告：无法在线读取图片能力配置，仅使用本地补充清单。\n' >&2
else
  printf '正在读取模型图片能力配置...\n'
  : > "${CAPABILITIES_FILE}"
  if CAPABILITIES_HTTP_STATUS=$(curl --silent --show-error --location \
    --connect-timeout 10 --max-time 30 \
    --header 'Cache-Control: no-cache' \
    --output "$(winpath "${CAPABILITIES_FILE}")" --write-out '%{http_code}' \
    "${CAPABILITIES_URL}"); then
    if [ "${CAPABILITIES_HTTP_STATUS}" != "200" ]; then
      : > "${CAPABILITIES_FILE}"
      printf '警告：模型能力配置返回 HTTP %s，仅使用本地补充清单。\n' "${CAPABILITIES_HTTP_STATUS}" >&2
    fi
  else
    : > "${CAPABILITIES_FILE}"
    printf '警告：无法读取模型能力配置，仅使用本地补充清单。\n' >&2
  fi
fi
printf '%s\n' "${EXTRA_CAPABILITIES}" >> "${CAPABILITIES_FILE}"
chmod 600 "${CAPABILITIES_FILE}" 2>/dev/null || true

# ---- Node.js 合并逻辑（1:1 移植自原 macOS 版 JXA）----
cat > "${NODE_FILE}" <<'NODE'
'use strict';
const fs = require('fs');

function fileExists(path) {
  try { fs.accessSync(path); return true; } catch (e) { return false; }
}

function readUTF8(path) {
  return fs.readFileSync(path, 'utf8');
}

function writeUTF8(path, content) {
  fs.writeFileSync(path, content, 'utf8');
}

function readImageCapabilities(path) {
  const capabilities = {};
  if (!fileExists(path)) {
    return capabilities;
  }

  readUTF8(path).split(/\r?\n/).forEach(function (rawLine) {
    const line = String(rawLine || '').replace(/^\uFEFF/, '').trim();
    if (!line || line.charAt(0) === '#') {
      return;
    }

    const separatorIndex = line.indexOf('|');
    if (separatorIndex <= 0) {
      return;
    }

    const modelID = line.slice(0, separatorIndex).trim();
    const rawValue = line.slice(separatorIndex + 1).trim().toLowerCase();
    if (!modelID) {
      return;
    }
    if (rawValue === 'true' || rawValue === '1' || rawValue === 'yes' || rawValue === 'on') {
      capabilities[modelID.toLowerCase()] = true;
    } else if (rawValue === 'false' || rawValue === '0' || rawValue === 'no' || rawValue === 'off') {
      capabilities[modelID.toLowerCase()] = false;
    }
  });

  return capabilities;
}

function run(argv) {
  const configPath = argv[0];
  const outputPath = argv[1];
  const keyPath = argv[2];
  const setupPath = argv[3];
  const capabilitiesPath = argv[4];
  const apiBaseURL = String(argv[5] || '').trim();
  const fallbackPath = argv[6];
  const apiKey = readUTF8(keyPath).replace(/[\r\n]+$/, '');

  if (!apiKey) {
    throw new Error('API Key 不能为空');
  }
  if (!apiBaseURL) {
    throw new Error('缺少 API 服务地址(API_BASE_URL)');
  }

  // /v1/models 返回 OpenAI 格式: { object: 'list', data: [ { id: '...', ... }, ... ] }
  const modelIDs = [];
  const seenModelIDs = {};
  // 模型白名单由 new-api token 的 model_limits 控制：/v1/models 已只返回白名单模型，
  // 这里只做去空 + 去重（大小写不敏感），不再二次过滤。
  function collectModelIDs(entries) {
    entries.forEach(function (entry) {
      const modelID = String((entry && entry.id) || '').trim();
      const key = modelID.toLowerCase();
      if (!modelID || seenModelIDs[key]) {
        return;
      }
      modelIDs.push(modelID);
      seenModelIDs[key] = true;
    });
  }

  let onlineData = null;
  try {
    const response = JSON.parse(readUTF8(setupPath));
    onlineData = response && Array.isArray(response.data) ? response.data : null;
  } catch (error) {
    onlineData = null;
  }
  if (onlineData) {
    collectModelIDs(onlineData);
  }

  // HTTP 200 但空/无效列表（token 白名单与渠道可用模型无交集、代理返回 HTML 等）
  // 与传输层失败一样只能拿到 0 个模型，这里降级到内置默认清单（sh 侧已把传输
  // 失败的响应替换成内置 JSON，这一层兜「请求成功但数据为空」）。
  let usedFallback = false;
  if (modelIDs.length === 0) {
    usedFallback = true;
    const fallback = JSON.parse(readUTF8(fallbackPath));
    if (!fallback || !Array.isArray(fallback.data)) {
      throw new Error('内置默认模型列表不可用');
    }
    collectModelIDs(fallback.data);
    if (modelIDs.length === 0) {
      throw new Error('内置默认模型列表为空');
    }
  }

  const supportsToolCall = true;
  const supportsReasoning = false;
  const useCustomProtocol = false;
  const maxInputTokens = 200000;
  const maxOutputTokens = 65536;
  const imageCapabilities = readImageCapabilities(capabilitiesPath);

  let root = [];
  let models = root;
  let rootType = 'array';

  if (fileExists(configPath)) {
    const source = readUTF8(configPath).trim();
    if (source) {
      try {
        root = JSON.parse(source);
      } catch (error) {
        throw new Error('现有 models.json 不是有效 JSON，已保留原文件和备份');
      }
    }

    if (Array.isArray(root)) {
      models = root;
    } else if (root && typeof root === 'object' && Array.isArray(root.models)) {
      rootType = 'object';
      models = root.models;
    } else {
      throw new Error('现有 models.json 必须是模型数组，或包含 models 数组的对象');
    }
  }

  const fixedModels = modelIDs.map(function (modelID) {
    const capabilityKey = modelID.toLowerCase();
    const supportsImages = Object.prototype.hasOwnProperty.call(imageCapabilities, capabilityKey)
      ? imageCapabilities[capabilityKey]
      : false;
    return {
      id: modelID,
      name: modelID,
      vendor: 'Custom',
      url: apiBaseURL,
      apiKey: apiKey,
      supportsToolCall: supportsToolCall,
      supportsImages: supportsImages,
      supportsReasoning: supportsReasoning,
      useCustomProtocol: useCustomProtocol,
      maxInputTokens: maxInputTokens,
      maxOutputTokens: maxOutputTokens
    };
  });
  const fixedModelByID = {};
  fixedModels.forEach(function (model) {
    fixedModelByID[model.id] = model;
  });

  const updatedModels = [];
  const installedIDs = {};
  models.forEach(function (item) {
    if (item && fixedModelByID[item.id]) {
      if (!installedIDs[item.id]) {
        updatedModels.push(fixedModelByID[item.id]);
        installedIDs[item.id] = true;
      }
      return;
    }
    updatedModels.push(item);
  });

  fixedModels.forEach(function (model) {
    if (!installedIDs[model.id]) {
      updatedModels.push(model);
      installedIDs[model.id] = true;
    }
  });

  if (rootType === 'object') {
    root.models = updatedModels;
  } else {
    root = updatedModels;
  }

  writeUTF8(outputPath, JSON.stringify(root, null, 2) + '\n');
  return (usedFallback ? 'FALLBACK|' : '') + modelIDs.join('、');
}

try {
  process.stdout.write(run(process.argv.slice(2)));
} catch (e) {
  process.stderr.write(String((e && e.message) || e) + '\n');
  process.exit(1);
}
NODE

NODE_CONFIG_FILE=$(winpath "${CONFIG_FILE}")
NODE_TEMP_CONFIG=$(winpath "${TEMP_CONFIG}")
NODE_KEY_FILE=$(winpath "${KEY_FILE}")
NODE_SETUP_FILE=$(winpath "${SETUP_FILE}")
NODE_CAPABILITIES_FILE=$(winpath "${CAPABILITIES_FILE}")
NODE_FALLBACK_FILE=$(winpath "${FALLBACK_FILE}")
NODE_SCRIPT=$(winpath "${NODE_FILE}")

if ! INSTALLED_MODELS=$(node "${NODE_SCRIPT}" "${NODE_CONFIG_FILE}" "${NODE_TEMP_CONFIG}" "${NODE_KEY_FILE}" "${NODE_SETUP_FILE}" "${NODE_CAPABILITIES_FILE}" "${API_BASE_URL}" "${NODE_FALLBACK_FILE}"); then
  print_error "无法更新 ${CONFIG_FILE}。原配置没有被覆盖。"
  if [ -n "${BACKUP_FILE}" ]; then
    printf '备份文件：%s\n' "${BACKUP_FILE}" >&2
  fi
  exit 1
fi

case "${INSTALLED_MODELS}" in
  FALLBACK\|*)
    INSTALLED_MODELS=${INSTALLED_MODELS#FALLBACK|}
    printf '\n注：在线模型列表为空或不可用，本次使用内置默认模型列表，清单可能不是最新。\n'
    ;;
esac

chmod 600 "${TEMP_CONFIG}" 2>/dev/null || true
mv -f "${TEMP_CONFIG}" "${CONFIG_FILE}"
TEMP_CONFIG=""
chmod 600 "${CONFIG_FILE}" 2>/dev/null || true

printf '\n配置成功。\n'
printf '模型：%s\n' "${INSTALLED_MODELS}"
printf '文件：%s\n' "${CONFIG_FILE}"
if [ -n "${BACKUP_FILE}" ]; then
  printf '备份：%s\n' "${BACKUP_FILE}"
fi
printf '\n如果对话框右下角没有显示自定义模型，请重新打开 WorkBuddy。\n'
