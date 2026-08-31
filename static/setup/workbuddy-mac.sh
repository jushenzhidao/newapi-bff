#!/bin/bash

set -euo pipefail

# ============================================================
# WorkBuddy 模型一键配置（macOS 版 · 适配 new-api）
# ------------------------------------------------------------
# 基于官方 macOS 脚本修改，仅 3 处适配：
#   1. API_BASE_URL 换成你的 new-api 实例
#   2. 图片能力清单增加「本地补充 EXTRA_CAPABILITIES」：官方在线清单
#      不含 new-api 渠道私有模型，本地补充按行合并（后写覆盖同名键）
#   3. FALLBACK_MODELS_JSON / FALLBACK_CAPABILITIES 建议改成你 token
#      白名单内的模型（离线兜底时清单才是你自己的渠道）
# 核心逻辑不变：new-api 的 /v1/models 受 token model_limits 白名单控制
# （带 Authorization 时只返回该 token 可用模型）→ 与现有 models.json 合并
# （命中更新/缺失追加/其它保留）→ 原子替换 + 自动备份。
# 用法：在 Mac 的「终端」中执行  bash workbuddy-mac.sh
# ============================================================

# ===================== 已按你的 new-api 渠道配置 =====================
# 白名单模型与网关地址优先从站点配置接口动态获取（管理员后台「展示模型清单」
# /「对外 API 地址」改了脚本自动跟随，无需发版）；拉取失败用下方内置兜底。
API_BASE_URL="__API_BASE_URL__"
SETUP_CONFIG_URL="__BFF_ORIGIN__/api/config"

# 在线图片能力清单（WorkBuddy 官方，尽力拉取；失败不阻塞配置）
CAPABILITIES_URL="__BFF_ORIGIN__/setup/workbuddy-model-capabilities.txt"

# 本地补充图片能力：new-api 渠道中支持「图片输入」的模型，一行一个，查找键统一小写。
# 在线清单拉取成功时 = 官方清单 + 本地补充（本地覆盖同名键）；
# 在线清单不可用时 = 内置 FALLBACK_CAPABILITIES + 本地补充。
# 你渠道 5 个模型中：gpt-4o / gpt-4o-mini / claude-sonnet-4 / gemini-2.0-flash
# 全部支持图片输入（产品要求能力默认勾选开启）。
EXTRA_CAPABILITIES='# aihuobao.cn 渠道中支持图片输入的模型
gpt-4o|true
gpt-4o-mini|true
claude-sonnet-4|true
deepseek-chat|true
gemini-2.0-flash|true'

# 内置默认模型列表（与 /v1/models 同构）+ 内置图片能力：在线拉取失败或返回
# 空列表时的兜底，让配置不依赖网络也能完成（已同步为 aihuobao.cn 白名单模型）。
FALLBACK_MODELS_JSON='{"object":"list","data":[{"id":"gpt-4o"},{"id":"gpt-4o-mini"},{"id":"claude-sonnet-4"},{"id":"deepseek-chat"},{"id":"gemini-2.0-flash"}]}'
FALLBACK_CAPABILITIES='# aihuobao.cn 内置图片能力（离线兜底）
gpt-4o|true
gpt-4o-mini|true
claude-sonnet-4|true
deepseek-chat|true
gemini-2.0-flash|true'
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

if [ "$(uname -s)" != "Darwin" ]; then
  print_error "这个脚本仅支持 macOS。"
  exit 1
fi

if [ ! -x /usr/bin/osascript ]; then
  print_error "系统缺少 /usr/bin/osascript，无法安全修改 JSON 配置。"
  exit 1
fi

if [ ! -x /usr/bin/curl ]; then
  print_error "系统缺少 /usr/bin/curl，无法读取模型配置。"
  exit 1
fi

if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
  print_error "没有可用的终端，请在 Mac 的“终端”应用中运行此脚本。"
  exit 1
fi

printf '\nWorkBuddy 模型一键配置\n'
printf '配置：%s\n\n' "${CONFIG_FILE}"
printf '请输入 API Key：' > /dev/tty
IFS= read -r API_KEY < /dev/tty || true

API_KEY=${API_KEY%$'\r'}
if [ -z "${API_KEY}" ]; then
  print_error "API Key 不能为空。"
  exit 1
fi

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
chmod 700 "${CONFIG_DIR}"

if [ -f "${CONFIG_FILE}" ]; then
  BACKUP_FILE="${CONFIG_FILE}.backup-$(date '+%Y%m%d-%H%M%S')-$$"
  cp -p "${CONFIG_FILE}" "${BACKUP_FILE}"
  chmod 600 "${BACKUP_FILE}"
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/workbuddy-install.XXXXXX")
chmod 700 "${TEMP_DIR}"
KEY_FILE="${TEMP_DIR}/api-key"
JXA_FILE="${TEMP_DIR}/update-models.js"
SETUP_FILE="${TEMP_DIR}/setup.json"
FALLBACK_FILE="${TEMP_DIR}/fallback-models.json"
CAPABILITIES_FILE="${TEMP_DIR}/model-capabilities.txt"
CURL_CONFIG="${TEMP_DIR}/curl.conf"
TEMP_CONFIG="${CONFIG_DIR}/.models.json.tmp.$$"

printf '%s' "${API_KEY}" > "${KEY_FILE}"
chmod 600 "${KEY_FILE}"
printf '%s' "${FALLBACK_MODELS_JSON}" > "${FALLBACK_FILE}"
chmod 600 "${FALLBACK_FILE}"
printf 'header = "Authorization: Bearer %s"\n' "${API_KEY}" > "${CURL_CONFIG}"
chmod 600 "${CURL_CONFIG}"

# ---- 站点配置：白名单模型 + 网关地址（管理员后台动态下发，失败保持内置）----
SITE_CONFIG_FILE="${TEMP_DIR}/site-config.json"
printf '正在读取站点配置...\n'
SITE_CONFIG_HTTP_STATUS=$(/usr/bin/curl --silent --show-error --location \
  --connect-timeout 5 --max-time 10 \
  --output "${SITE_CONFIG_FILE}" --write-out '%{http_code}' \
  "${SETUP_CONFIG_URL}" 2>/dev/null) || SITE_CONFIG_HTTP_STATUS=""
if [ "${SITE_CONFIG_HTTP_STATUS}" != "200" ] || ! grep -q '"api"' "${SITE_CONFIG_FILE}" 2>/dev/null; then
  rm -f "${SITE_CONFIG_FILE}"
  printf '站点配置读取失败，使用内置默认清单。\n'
else
  # bash 侧只解析 base_url（供随后拉取 /models 使用）；models 数组交给 JXA 解析
  REMOTE_BASE_URL=$(sed -n 's/.*"base_url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${SITE_CONFIG_FILE}" | head -n 1)
  if [ -n "${REMOTE_BASE_URL}" ]; then
    API_BASE_URL="${REMOTE_BASE_URL%/}"
  fi
fi
[ -f "${SITE_CONFIG_FILE}" ] && chmod 600 "${SITE_CONFIG_FILE}"

printf '\n正在读取可用模型列表...\n'
# 不区分失败类型：连接失败与 HTTP 非 200（401/403 等可能是 API Key 问题）一律降级到
# 内置默认清单（写入 SETUP_FILE，走与在线结果完全相同的后续解析）；HTTP 200 但空/无效
# 列表的情况由下方 JXA 再兜一层。保证配置总能离线完成，而不是失败退出。
MODELS_FETCH_FAILED=0
if ! HTTP_STATUS=$(/usr/bin/curl --silent --show-error --location \
  --connect-timeout 10 --max-time 30 \
  --output "${SETUP_FILE}" --write-out '%{http_code}' \
  --config "${CURL_CONFIG}" "${API_BASE_URL%/}/models"); then
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
chmod 600 "${SETUP_FILE}"

# ---- 图片能力清单：先写基础内容，再追加本地补充（后写覆盖同名键）----
if [ "${MODELS_FETCH_FAILED}" = "1" ]; then
  # 兜底模式：连接层已不可用，在线能力配置同样拉不到，直接用内置图片能力。
  printf '%s\n' "${FALLBACK_CAPABILITIES}" > "${CAPABILITIES_FILE}"
else
  printf '正在读取模型图片能力配置...\n'
  : > "${CAPABILITIES_FILE}"
  if CAPABILITIES_HTTP_STATUS=$(/usr/bin/curl --silent --show-error --location \
    --connect-timeout 10 --max-time 30 \
    --header 'Cache-Control: no-cache' \
    --output "${CAPABILITIES_FILE}" --write-out '%{http_code}' \
    "${CAPABILITIES_URL}"); then
    if [ "${CAPABILITIES_HTTP_STATUS}" != "200" ]; then
      : > "${CAPABILITIES_FILE}"
      printf '警告：模型能力配置返回 HTTP %s，仅使用内置与本地补充清单。\n' "${CAPABILITIES_HTTP_STATUS}" >&2
    fi
  else
    : > "${CAPABILITIES_FILE}"
    printf '警告：无法读取模型能力配置，仅使用内置与本地补充清单。\n' >&2
  fi
fi
printf '%s\n' "${EXTRA_CAPABILITIES}" >> "${CAPABILITIES_FILE}"
chmod 600 "${CAPABILITIES_FILE}"

cat > "${JXA_FILE}" <<'JXA'
ObjC.import('Foundation');

function fileExists(path) {
  return $.NSFileManager.defaultManager.fileExistsAtPath(path);
}

function readUTF8(path) {
  const value = $.NSString.stringWithContentsOfFileEncodingError(
    path,
    $.NSUTF8StringEncoding,
    null
  );
  if (value === null) {
    throw new Error('无法读取文件：' + path);
  }
  return ObjC.unwrap(value);
}

function writeUTF8(path, content) {
  const ok = $(content).writeToFileAtomicallyEncodingError(
    path,
    true,
    $.NSUTF8StringEncoding,
    null
  );
  if (!ok) {
    throw new Error('无法写入临时配置文件：' + path);
  }
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
  const siteConfigPath = argv[7] || '';
  const apiKey = readUTF8(keyPath).replace(/[\r\n]+$/, '');

  if (!apiKey) {
    throw new Error('API Key 不能为空');
  }
  if (!apiBaseURL) {
    throw new Error('缺少 API 服务地址(API_BASE_URL)');
  }

  // 站点配置（管理员后台「展示模型清单」/「对外 API 地址」）：白名单与网关地址
  // 优先从这里取，拉不到/解析失败保持内置默认。
  let allowedModelPrefixes = [
    'gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4', 'deepseek-chat', 'gemini-2.0-flash',
  ];
  let siteModels = [];
  const modelVendors = {};   // 模型 → 供应商；未配置则 vendor 写 'Custom'
  let effectiveBaseURL = apiBaseURL;
  if (siteConfigPath && fileExists(siteConfigPath)) {
    try {
      const sc = JSON.parse(readUTF8(siteConfigPath));
      const api = sc && sc.data && sc.data.api ? sc.data.api : null;
      if (api) {
        if (Array.isArray(api.models)) {
          siteModels = api.models.map(function (m) { return String(m || '').trim(); })
            .filter(function (m) { return m !== ''; });
          if (siteModels.length > 0) {
            allowedModelPrefixes = siteModels.map(function (m) { return m.toLowerCase(); });
          }
        }
        const bu = String(api.base_url || '').trim();
        if (bu !== '') { effectiveBaseURL = bu.replace(/\/+$/, ''); }
        // 模型 → 供应商映射（后台「模型供应商映射」配置）。
        // 网关 /v1/models 的 owned_by 不可信（Claude 会被标成 openai），故由后台维护。
        if (api.model_vendors && typeof api.model_vendors === 'object') {
          Object.keys(api.model_vendors).forEach(function (k) {
            const v = String(api.model_vendors[k] || '').trim();
            if (v !== '') { modelVendors[k] = v; }
          });
        }
      }
    } catch (error) { /* 解析失败保持内置 */ }
  }

  // /v1/models 返回 OpenAI 格式: { object: 'list', data: [ { id: '...', ... }, ... ] }
  const modelIDs = [];
  const seenModelIDs = {};
  // 硬白名单（前缀匹配，大小写不敏感）：渠道模型 ID 常带版本后缀
  // （如 gpt-4o-2024-11-20 / claude-sonnet-4-20250514），以白名单项开头的
  // 都保留原始 ID，其他（o3-mini 等）一律跳过。
  function collectModelIDs(entries) {
    entries.forEach(function (entry) {
      const modelID = String((entry && entry.id) || '').trim();
      const key = modelID.toLowerCase();
      if (!modelID || seenModelIDs[key]) {
        return;
      }
      let allowed = false;
      for (let i = 0; i < allowedModelPrefixes.length; i++) {
        if (key.indexOf(allowedModelPrefixes[i]) === 0) { allowed = true; break; }
      }
      if (!allowed) {
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
  // 与传输层失败一样只能拿到 0 个模型，这里降级：优先站点配置的展示清单
  // （它就是白名单来源，天然通过过滤），再退内置默认清单（sh 侧已把传输
  // 失败的响应替换成内置 JSON，这一层兜「请求成功但数据为空」）。
  let usedFallback = false;
  if (modelIDs.length === 0) {
    usedFallback = true;
    if (siteModels.length > 0) {
      collectModelIDs(siteModels.map(function (m) { return { id: m }; }));
    }
    if (modelIDs.length === 0) {
      const fallback = JSON.parse(readUTF8(fallbackPath));
      if (!fallback || !Array.isArray(fallback.data)) {
        throw new Error('内置默认模型列表不可用');
      }
      collectModelIDs(fallback.data);
    }
    if (modelIDs.length === 0) {
      throw new Error('内置默认模型列表为空');
    }
  }

  const supportsToolCall = true;
  const supportsReasoning = true;
  const useCustomProtocol = false;
  // 思考强度档位：由低到高全开，与客户端手动勾选「支持的思考强度」的产出一致
  const reasoningEfforts = ['low', 'medium', 'high', 'xhigh', 'max'];
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
      : true;
    return {
      id: modelID,
      name: modelID,
      vendor: modelVendors[modelID] || 'Custom',
      url: effectiveBaseURL,
      apiKey: apiKey,
      supportsToolCall: supportsToolCall,
      supportsImages: supportsImages,
      supportsReasoning: supportsReasoning,
      useCustomProtocol: useCustomProtocol,
      reasoning: { supportedEfforts: reasoningEfforts },
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
JXA

if ! INSTALLED_MODELS=$(/usr/bin/osascript -l JavaScript "${JXA_FILE}" "${CONFIG_FILE}" "${TEMP_CONFIG}" "${KEY_FILE}" "${SETUP_FILE}" "${CAPABILITIES_FILE}" "${API_BASE_URL}" "${FALLBACK_FILE}" "${SITE_CONFIG_FILE:-}"); then
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

chmod 600 "${TEMP_CONFIG}"
mv -f "${TEMP_CONFIG}" "${CONFIG_FILE}"
TEMP_CONFIG=""
chmod 600 "${CONFIG_FILE}"

printf '\n配置成功。\n'
printf '模型：%s\n' "${INSTALLED_MODELS}"
printf '文件：%s\n' "${CONFIG_FILE}"
if [ -n "${BACKUP_FILE}" ]; then
  printf '备份：%s\n' "${BACKUP_FILE}"
fi
printf '\n如果对话框右下角没有显示自定义模型，请重新打开WorkBuddy。\n'
