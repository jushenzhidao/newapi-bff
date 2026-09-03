/* 教程中心渲染器
 *
 * 后端把每个产品的教程描述成一组 section（见 docs/products/*.yml），
 * 这里只负责「section.type -> HTML」的映射。新增一种展现形式＝往 SECTION
 * 注册表里加一个函数，不需要动路由、不需要动索引页、不需要动后端。
 *
 * 依赖 app.js 里已有的 esc / api / renderLayout / MODEL 等全局函数，
 * 不重复实现，避免出现两套转义逻辑（那是 XSS 的经典来源）。
 */

/* ---------- 通用小工具 ---------- */

/* 极简 Markdown：只支持档案里实际用到的语法。
 * 不引三方库是刻意的：教程文案由我们自己写，语法可控，
 * 引一个 40KB 的解析器去渲染几段加粗和列表不划算。 */
function docMd(src) {
  const lines = String(src || "").split("\n");
  const out = [];
  let inList = false;
  const inline = (t) =>
    esc(t)
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img class="doc-img" alt="$1" src="$2" loading="lazy" decoding="async">')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { if (inList) { out.push("</ul>"); inList = false; } continue; }
    const li = line.match(/^[-*]\s+(.*)$/);
    if (li) {
      if (!inList) { out.push('<ul class="doc-ul">'); inList = true; }
      out.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    const h = line.match(/^(#{2,4})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    out.push(`<p>${inline(line)}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}

/* 代码块 + 复制按钮。onclick 里传的是 id 而不是代码内容本身，
 * 否则代码里的引号会把 HTML 属性截断。 */
let _cbSeq = 0;
function docCode(code, lang) {
  // 一行布局：bash 标签 + 命令（占满中间）+ 复制按钮，垂直居中、外框包边。
  // __SITE_ORIGIN__ 占位符替换为当前访问域名（本地 8000 / 线上各显示各自的），
  // 复制按钮取 DOM 文本，拿到的已是替换后的完整地址。
  const resolved = esc(code).replaceAll("__SITE_ORIGIN__", window.location.origin);
  const id = `dc${++_cbSeq}`;
  return `
    <div class="doc-code">
      <span class="doc-code-lang">${esc(lang || "bash")}</span>
      <pre id="${id}"><code>${resolved}</code></pre>
      <button class="doc-copy" onclick="docCopy('${id}',this)">复制</button>
    </div>`;
}

async function docCopy(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.innerText);
    const old = btn.textContent;
    btn.textContent = "已复制";
    btn.classList.add("ok");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("ok"); }, 1500);
  } catch (_) {
    btn.textContent = "复制失败";
  }
}

/* 教程视频：原生 <video>，零额外依赖。src 为空（后台未配视频地址）时渲染占位提示。 */
/* 视频：进入视口才预缓冲，用户点播放即秒开（不自动播放）。
 *
 * 两个目标各用一个手段：
 *   不阻塞首屏   → src 先放在 data-src，滚动进视口才开始预载（IntersectionObserver），
 *                  隐藏 tab 里的视频完全不下载
 *   点播即秒开   → 进视口后 preload="auto" 立即流式缓冲（HTTP Range），
 *                  用户点 controls 的播放键时通常已经缓冲了一部分，几乎不用等
 * 不自动播放：教程视频由用户自己决定何时看，自动播（尤其静音自动播）体验反而差。
 * 封面图（v.poster）暂不渲染，yml 里的字段保留，需要时加回 video 标签即可。 */
function docVideo(v) {
  if (!v) return "";
  const title = v.title ? `<div class="doc-video-cap">${esc(v.title)}</div>` : "";
  if (!v.src) {
    return `<div class="doc-video-wrap"><div class="doc-video-empty">教程视频准备中，敬请期待</div>${title}</div>`;
  }
  return `<div class="doc-video-wrap">` +
    `<video class="doc-video" controls playsinline preload="none"` +
    ` data-src="${esc(v.src)}"></video>${title}</div>`;
}

/* 渲染完成后调用：给页面上的视频挂上「进视口才预载」的逻辑。 */
function initDocVideos() {
  const vids = document.querySelectorAll("video.doc-video[data-src]");
  if (!vids.length) return;

  const start = (v) => {
    if (v.dataset.started) return;
    v.dataset.started = "1";
    v.src = v.dataset.src;
    v.removeAttribute("data-src");
    v.preload = "auto";        // 立即开始流式缓冲，点播放时基本已就绪
    v.load();
  };

  if (!("IntersectionObserver" in window)) {   // 老浏览器：直接全部预载
    vids.forEach(start);
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      start(e.target);
    });
  }, { rootMargin: "200px" });                  // 提前 200px 开始预载
  vids.forEach((v) => io.observe(v));
}

/* 把 fields 段里标记为 __USER_KEY__ 的占位，替换为登录用户最新的 API Key。
 * 取「最新」规则：status===1（启用）的 Key 里取 created_time 最大者；
 * 全部禁用时退回全列表里 created_time 最大者；无 created_time 字段时按列表顺序兜底。
 * 真实环境下 /api/token 列表返回的是掩码 Key（如 QSZO**********Nf48），无法用于客户端配置，
 * 需再调一次 /api/token/{id}/key 取明文（该接口有频控，仅在确为掩码时补调一次）。
 * new-api 存储的 key 是不带 sk- 前缀的裸串，实际调用要用 sk-<key>，注入前统一补齐
 * （mock 模式生成的 key 已带前缀，此处幂等不重复加）。
 * 失败（未登录/无权限/取明文失败）则回退提示文本，不阻塞页面渲染。 */
async function injectUserKeys() {
  const nodes = document.querySelectorAll("[data-user-key]");
  if (!nodes.length) return;
  let key = "";
  try {
    const r = await api("/api/token");
    const list = (r && r.data) || [];
    const byNewest = (a, b) => (b.created_time || 0) - (a.created_time || 0);
    const active = list.filter((t) => t.status === 1);
    const pool = (active.length ? active : list).slice().sort(byNewest);
    const def = pool[0];
    if (def && def.key) {
      key = def.key;
      // 列表是掩码串（含 *）时补调明文接口，拿到真正可复制配置的 Key。
      if (key.indexOf("*") !== -1 && def.id != null) {
        try {
          const kp = await api(`/api/token/${def.id}/key`, { method: "POST" });
          if (kp && kp.data && kp.data.key) key = kp.data.key;
        } catch (_) { /* 取明文失败，保留掩码提示文本 */ }
      }
      // 补 sk- 前缀（new-api 裸串 → 实际调用格式），已带前缀则不动。
      if (key.slice(0, 3) !== "sk-") key = "sk-" + key;
    }
  } catch (_) { /* 忽略，走回退 */ }
  nodes.forEach((n) => { n.textContent = key || "sk-你的Key"; });
}

/* ---------- section 渲染注册表 ---------- */

const SECTION = {
  markdown: (s) => `<div class="card doc-prose">${
    s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""
  }${docMd(s.body)}</div>`,

  callout: (s) => `
    <div class="doc-callout ${esc(s.level || "info")}">
      ${s.title ? `<b>${esc(s.title)}</b>` : ""}
      <div>${docMd(s.body)}</div>
    </div>`,

  code: (s) => `<div class="card">${
    s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""
  }${docCode(s.code, s.lang)}</div>`,

  steps: (s) => `
    <div class="card">
      ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
      ${(s.items || []).map((it, i) => `
        <div class="doc-step">
          <div class="step-no">${i + 1}</div>
          <div class="step-body">
            <b>${esc(it.name)}</b>
            ${it.body ? docMd(it.body) : ""}
            ${it.button ? `<button class="doc-btn" type="button" onclick="docShowModels('${esc(it.button.action || "")}')">${esc(it.button.label || "查看")}</button>` : ""}
            ${it.code ? docCode(it.code, it.lang) : ""}
            ${Array.isArray(it.fields) && it.fields.length ? `
              <div class="doc-fields">
                ${it.fields.map((f) => `
                  <div class="doc-field${f.copyable ? " has-copy" : ""}">
                    <span class="doc-field-label">${esc(f.label || "")}</span>
                    <span class="doc-field-value"${f.value === "__USER_KEY__" ? ' data-user-key="1"' : ""}>${esc(f.value === "__USER_KEY__" ? "你的 API Key" : (f.value || ""))}</span>
                    ${f.copyable ? `<button class="doc-copy" type="button" onclick="copyDocField(this)">复制</button>` : ""}
                  </div>`).join("")}
              </div>` : ""}
            ${it.image ? `<img class="doc-img" alt="${esc(it.name || "")}" src="${esc(it.image)}" loading="lazy" decoding="async">` : ""}
          </div>
        </div>`).join("")}
    </div>`,

  faq: (s) => `
    <div class="card">
      <div class="card-title">${esc(s.title || "常见问题")}</div>
      ${(s.items || []).map((it) => `
        <details class="doc-faq">
          <summary>${esc(it.q)}</summary>
          <div>${docMd(it.a)}</div>
        </details>`).join("")}
    </div>`,

  /* 多平台安装：Tab 切换。默认选中第一个平台，而不是猜用户的操作系统 ——
   * 猜错了用户会照着错误的平台命令去执行。
   * tab.sections 是嵌套段数组（按 SECTION[type] 递归渲染），旧的 tab.body/code 兼容保留。 */
  platform_tabs: (s) => {
    const tabs = s.tabs || [];
    if (!tabs.length) return "";
    const gid = `pt${++_cbSeq}`;
    const renderTabSections = (t) => {
      if (Array.isArray(t.sections) && t.sections.length) {
        return t.sections.map((sec) => {
          if (!sec || !sec.type) return "";
          const fn = SECTION[sec.type];
          if (!fn) { console.warn("docs: no renderer for", sec.type); return ""; }
          try { return fn(sec); }
          catch (e) { console.error("docs section render error:", sec.type, e); return ""; }
        }).join("");
      }
      /* 向后兼容旧格式：tab.video/body/code 三个并列 */
      return [
        t.video ? docVideo(t.video) : "",
        t.body ? docMd(t.body) : "",
        t.code ? docCode(t.code, t.lang) : "",
      ].join("");
    };
    const inner = `
      ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
      <div class="doc-tabs" id="${gid}">
        ${tabs.map((t, i) => `
          <button class="doc-tab${i === 0 ? " active" : ""}"
                  onclick="docTab('${gid}',${i},this)">${esc(t.name)}</button>`).join("")}
      </div>
      ${tabs.map((t, i) => `
        <div class="doc-tab-panel" data-g="${gid}" data-i="${i}"
             style="${i === 0 ? "" : "display:none"}">
          ${renderTabSections(t)}
        </div>`).join("")}`;
    return s.bare ? inner : `<div class="card">${inner}</div>`;
  },

  pricing_table: (s) => renderPricing(s.data || {}),

  /* 配置字段表单：label + value 列表。value 为 "__USER_KEY__" 时，
   * 渲染后由 injectUserKeys 异步填入登录用户的默认 Key。
   * items[i].copyable = true 时在该 item 后追加「复制」按钮。 */
  fields: (s) => `
    <div class="card">
      ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
      <div class="doc-fields">
        ${(s.items || []).map((it) => `
          <div class="doc-field${it.copyable ? " has-copy" : ""}">
            <span class="doc-field-label">${esc(it.label || "")}</span>
            <span class="doc-field-value"${it.value === "__USER_KEY__" ? ' data-user-key="1"' : ""}>${esc(it.value === "__USER_KEY__" ? "你的 API Key" : (it.value || ""))}</span>
            ${it.copyable ? `<button class="doc-copy" type="button" onclick="copyDocField(this)">复制</button>` : ""}
          </div>`).join("")}
      </div>
    </div>`,

  /* 模型能力表：对应「查看模型信息」。内联表格，长文本列省略号 + title 悬浮。 */
  model_table: (s) => {
    const rows = s.rows || [];
    /* 能力标记：支持用 SVG 勾（P0-1 禁 emoji/字符图标，复用 app.js 全局 ICONS.check，
     * app.js 先于本文件加载，全局词法作用域可直接引用）；不支持保留 —。 */
    const flag = (v) =>
      (v === true || v === 1)
        ? `<span class="mt-yes">${ICONS.check}</span>`
        : (v === false || v === "—" || v === "" || v == null)
          ? '<span class="mt-no">—</span>'
          : `<span class="mt-dyn">${esc(String(v))}</span>`;
    return `
      <div class="card">
        ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
        <div class="doc-table-wrap">
          <table class="doc-table mt-table">
            <thead><tr>
              <th>模型</th><th>最大输入</th><th>最大输出</th>
              <th>工具调用</th><th>视觉</th><th>推理</th>
              <th>说明</th><th>适合场景</th>
            </tr></thead>
            <tbody>
              ${rows.map((m) => `
                <tr>
                  <td class="doc-model"><b>${esc(m.id)}</b></td>
                  <td>${esc(m.maxInput || "—")}</td>
                  <td>${esc(m.maxOutput || "—")}</td>
                  <td class="mt-c">${flag(m.toolCall)}</td>
                  <td class="mt-c">${flag(m.vision)}</td>
                  <td class="mt-c">${flag(m.reasoning)}</td>
                  <td class="mt-text" title="${esc(m.description || "")}">${esc(m.description || "—")}</td>
                  <td class="mt-text" title="${esc(m.scenario || "")}">${esc(m.scenario || "—")}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
  },

  /* 教程视频（独立段类型，也可嵌在 platform_tabs 的 tab 内） */
  video: (s) => `
    <div class="card">
      ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
      ${docVideo(s)}
    </div>`,

  /* WorkBuddy 自定义模型导入（链接触发，链接不含明文密钥）。
   * 当前用户在 workbuddy.oneapis.cn 已登录 → 主按钮直接调 /api/setup/import-doc-prep-session
   * 让后端从用户 cookie 的会话里把 PAT 抽出来，签发一次性 tok、返回
   * https://<origin>/api/setup/import-doc?tok=... 的链接（链接本身不含明文 PAT）。
   * 平台再把链接用自然语言包裹（「读取我给你的链接对应的文档，按照文档执行 <链接>」），
   * 复制到剪贴板。备选路径（details 折叠）保留"未登录/无 cookie 时手填 PAT"的兜底。 */
  workbuddy_setup: (s) => {
    let body = "";
    try { body = docMd(s.body || ""); }
    catch (e) { body = `<div class="doc-callout warn">简介渲染失败：${esc(e.message || String(e))}</div>`; }
    const btnLabel = (s && typeof s.button_label === "string" && s.button_label) ? s.button_label : "一键生成导入链接并复制";
    return `
      <div class="card" data-workbuddy-setup="1">
        ${s.title ? `<div class="card-title">${esc(s.title)}</div>` : ""}
        <div class="doc-prose">${body}</div>
        <div style="margin-top:12px;display:flex;flex-direction:column;gap:10px">
          <button class="doc-btn doc-btn-primary" type="button" onclick="docGenerateImportLink(this)" data-mode="session">${esc(btnLabel)}</button>
          <div class="doc-note">已登录用户免粘贴：平台直接用你的当前会话生成。链接 10 分钟内有效，链接本身不含明文密钥。</div>
        </div>
        <details style="margin-top:10px">
          <summary style="cursor:pointer;color:var(--hp-link,#4aa3ff);font-size:13px">未登录/无 cookie？手填 PAT 再生成</summary>
          <div style="margin-top:10px;display:flex;flex-direction:column;gap:10px">
            <label class="doc-field-label" for="wb-setup-pat">你的 new-api 访问令牌（PAT）</label>
            <input id="wb-setup-pat" class="doc-setup-pat" type="text" placeholder="sk-..." autocomplete="off" spellcheck="false"
                   style="width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--hp-border,#333);background:var(--hp-bg-input,#161616);color:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,monospace" />
            <button class="doc-btn" type="button" onclick="docGenerateImportLink(this)" data-mode="pat">用填写的 PAT 生成导入链接</button>
          </div>
        </details>
        <details style="margin-top:6px">
          <summary style="cursor:pointer;color:var(--hp-link,#4aa3ff);font-size:13px">以上都不行？生成纯文本导入指令（PAT 会明文嵌入指令）</summary>
          <div style="margin-top:10px">
            <button class="doc-btn" type="button" onclick="docGenerateImportCommand(this)">生成纯文本导入指令并复制</button>
          </div>
        </details>
      </div>`;
  },
};

function docTab(gid, idx, btn) {
  document.querySelectorAll(`#${gid} .doc-tab`).forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  document.querySelectorAll(`.doc-tab-panel[data-g="${gid}"]`).forEach((p) => {
    p.style.display = String(p.dataset.i) === String(idx) ? "" : "none";
  });
}

/* 复制字段值：取同 doc-field 内的 doc-field-value 文本（含 __USER_KEY__ 异步替换后的真实 Key）。
 * 不依赖外部注入顺序：injectUserKeys 通过 DOM textContent 替换，复制时直接读 textContent 就拿到已替换值。 */
function copyDocField(btn) {
  const valEl = btn.closest(".doc-field")?.querySelector(".doc-field-value");
  const text = valEl ? (valEl.textContent || "").trim() : "";
  if (!navigator.clipboard || !window.isSecureContext) {
    /* localhost 非 https：Clipboard API 在某些浏览器不可用，fallback 给提示 */
    btn.textContent = "请手动复制";
    setTimeout(() => { btn.textContent = "复制"; }, 1500);
    return;
  }
  navigator.clipboard.writeText(text).then(
    () => { const orig = btn.textContent; btn.textContent = "已复制"; setTimeout(() => { btn.textContent = orig; }, 1200); },
    () => { const orig = btn.textContent; btn.textContent = "复制失败"; setTimeout(() => { btn.textContent = orig; }, 1500); }
  );
}

/* 「查看支持的模型」：从 /api/config 取可配置模型清单与厂商映射，按厂商分组弹窗展示。
 * action 目前仅 "models" 一种，保留参数便于以后扩展别的清单类型。
 * 厂商排序沿用部署脚本约定：国外厂商（OpenAI、Anthropic）置顶，其余按出现顺序，Custom 永远最后。 */
const _MODEL_VENDOR_PIN = ["OpenAI", "Anthropic"];
async function docShowModels(action) {
  let models = [], vendors = {};
  try {
    const r = await api("/api/config");
    const apiCfg = (r && r.data && r.data.api) || {};
    models = apiCfg.models || [];
    vendors = apiCfg.model_vendors || {};
  } catch (e) { /* 取失败则走空清单兜底 */ }

  const groups = {};
  const appearOrder = [];
  for (const m of models) {
    const v = vendors[m] || "Custom";
    if (!groups[v]) { groups[v] = []; appearOrder.push(v); }
    groups[v].push(m);
  }

  const order = [];
  for (const p of _MODEL_VENDOR_PIN) if (groups[p]) order.push(p);
  for (const v of appearOrder) if (!_MODEL_VENDOR_PIN.includes(v) && v !== "Custom") order.push(v);
  if (groups["Custom"]) order.push("Custom");

  const listHtml = order.length
    ? order.map((v) => `
        <div class="doc-models-group">
          <div class="doc-models-vendor">${esc(v)}</div>
          <ul class="doc-models-list">${
            (groups[v] || []).map((m) => `<li>${esc(m)}</li>`).join("")
          }</ul>
        </div>`).join("")
    : '<div class="muted">暂无可用模型</div>';

  openModal(`
    <h3>支持的模型</h3>
    <div class="doc-models-modal">${listHtml}</div>
    <div class="modal-actions">
      <button class="btn" type="button" onclick="closeModal()">关闭</button>
    </div>
  `, { width: "520px" });
}

/* 「生成 WorkBuddy 导入链接」：调本平台 /api/setup/import-doc-prep（手填 PAT）或
 * /api/setup/import-doc-prep-session（已登录用户，从 cookie 自动拿 PAT）。把 PAT
 * 加密进一次性 tok、返回不含明文密钥的链接；再把链接用自然语言包裹
 * （「读取我给你的链接对应的文档，按照文档执行 <链接>」），复制到剪贴板并弹窗展示。
 * 用户把这段粘贴到本机 WorkBuddy 新建对话发送，AI 会自动 fetch 链接、按返回的文档
 * 直接写本机 models.json。全程零 skill 预装、链接不含明文密钥。 */
async function docGenerateImportLink(btn) {
  const card = btn ? btn.closest(".card") : null;
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const mode = (btn && btn.dataset && btn.dataset.mode) || "session";
    let url, body;
    if (mode === "session") {
      url = "/api/setup/import-doc-prep-session";
      body = undefined;
    } else {
      url = "/api/setup/import-doc-prep";
      const input = card ? card.querySelector("input.doc-setup-pat") : null;
      const pat = (input && input.value || "").trim();
      if (!pat) {
        if (input) { input.focus(); input.style.borderColor = "#e0533d"; }
        openModal(`
          <h3>缺少 PAT</h3>
          <div class="doc-callout warn">请先在上方输入框粘贴你的 new-api 访问令牌（PAT），再点生成。PAT 可在 new-api 官方前端的「个人设置 → 系统访问令牌」处复制。</div>
          <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
        `, { width: "480px" });
        return;
      }
      body = JSON.stringify({ pat });
    }
    const r = await api(url, body ? {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    } : { method: "POST" });
    const link = (r && r.data && r.data.link) || "";
    if (!link) throw new Error("服务端未返回链接");
    // 自然语言包裹：让 WorkBuddy 的 AI 主动 fetch 并按文档执行（不依赖原生链接协议）
    const cmd = `读取我给你的链接对应的文档，按照文档执行 ${link}`;
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(cmd); } catch (_) { /* 复制失败不阻断，弹窗仍可手动复制 */ }
    }
    openModal(`
      <h3>WorkBuddy 导入链接已生成</h3>
      <p class="muted">下面这段已复制到剪贴板。打开你电脑上的 WorkBuddy，新建对话并粘贴发送，AI 会自动读取链接文档并完成模型配置 —— 全程无需安装任何组件、无需手动填 Key。</p>
      ${docCode(cmd, "导入指令")}
      <div class="doc-note">链接本身不含明文密钥（PAT 已加密进一次性令牌，10 分钟内有效），可放心复制。若担心泄露，在 new-api 前端重新生成令牌即可使旧链接失效。</div>
      <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
    `, { width: "640px" });
  } catch (e) {
    const msg = (e && e.message) || "生成失败，请稍后重试";
    const hint = /未登录|401|unauthorized/i.test(msg)
      ? "当前页面已退出登录或会话已过期，请重新登录后再次生成。"
      : msg;
    openModal(`
      <h3>生成失败</h3>
      <div class="doc-callout warn">${esc(hint)}</div>
      <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
    `, { width: "480px" });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/* 「生成 WorkBuddy 导入指令」（from-pat 直连）：纯前端拼接、不调后端，把 PAT 明文嵌入一条
 * 指令（兜底路径，仅在 tok 链接方案不可用时使用，例如 WorkBuddy 版本不能 fetch 远端 URL）。
 * 取同一卡片内输入框的 PAT，拼成 `apikey:<PAT> 去读 <本站 origin>/api/setup/from-pat 写配置`，
 * 复制到剪贴板并弹窗展示。 */
async function docGenerateImportCommand(btn) {
  const card = btn ? btn.closest(".card") : null;
  const input = card ? card.querySelector("input.doc-setup-pat") : null;
  const pat = (input && input.value || "").trim();
  if (!pat) {
    if (input) { input.focus(); input.style.borderColor = "#e0533d"; }
    openModal(`
      <h3>缺少 PAT</h3>
      <div class="doc-callout warn">请先在上方输入框粘贴你的 new-api 访问令牌（PAT），再点生成。PAT 可在 new-api 官方前端的「个人设置 → 系统访问令牌」处复制。</div>
      <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
    `, { width: "480px" });
    return;
  }
  // 用当前站点 origin，自动适配任意部署域名（from-pat 端点就在这个 origin 上）
  const origin = location.origin;
  const cmd = `apikey:${pat} 去读 ${origin}/api/setup/from-pat 写配置`;
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "已生成"; }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(cmd); } catch (_) { /* 复制失败不阻断，弹窗仍可手动复制 */ }
    }
    openModal(`
      <h3>WorkBuddy 导入指令</h3>
      <p class="muted">下面这条指令已复制到剪贴板。打开你电脑上的 WorkBuddy，新建对话并粘贴发送，即可自动完成模型配置。</p>
      ${docCode(cmd, "导入指令")}
      <div class="doc-note">PAT 是你账户的访问令牌，请勿转发给他人。若担心泄露，可在 new-api 前端重新生成令牌使旧令牌失效。</div>
      <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
    `, { width: "600px" });
  } catch (e) {
    openModal(`
      <h3>生成失败</h3>
      <div class="doc-callout warn">${esc((e && e.message) || "生成失败，请稍后重试")}</div>
      <div class="modal-actions"><button class="btn" type="button" onclick="closeModal()">关闭</button></div>
    `, { width: "480px" });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/* ---------- 积分消耗系数表 ---------- */

/* stale 必须显式告知用户并带上快照日期。
 * 一张没有标注的旧价格表比没有价格表更危险 —— 用户会当成现价去做预算。 */
function renderPricing(pt) {
  const unit = pt.unit || "积分";
  const groups = Object.keys(pt.groups || { default: 1 });
  const tokenRows = pt.token_models || [];
  const callRows = pt.per_call_models || [];

  if (!tokenRows.length && !callRows.length) {
    return `<div class="doc-callout warn">
      <b>价格表暂时不可用</b>
      <div>上游定价服务未返回数据，且本地没有可用快照。请稍后刷新，或联系我们获取报价。</div>
    </div>`;
  }

  const staleBar = pt.stale
    ? `<div class="doc-callout warn" style="margin-bottom:12px">
         <b>当前显示的是缓存价格${pt.snapshot_date ? `（${esc(pt.snapshot_date)}）` : ""}</b>
         <div>实时定价服务暂时不可用，实际计费以调用时的费率为准。</div>
       </div>`
    : "";

  const gh = groups.map((g) => `<th>${esc(g)}</th>`).join("");
  const gRatio = groups.map((g) => {
    const r = (pt.groups || {})[g];
    return `<th><span class="doc-gr">×${r}</span></th>`;
  }).join("");

  const tokenTable = tokenRows.length ? `
    <div class="doc-table-title">按 Token 计费<span class="muted">（${esc(unit)} / 1K tokens）</span></div>
    <div class="doc-table-wrap">
      <table class="doc-table">
        <thead>
          <tr><th rowspan="2">模型</th><th rowspan="2">倍率</th>
              <th colspan="${groups.length}">输入</th><th colspan="${groups.length}">输出</th></tr>
          <tr>${gh}${gh}</tr>
        </thead>
        <tbody>
          ${tokenRows.map((m) => `
            <tr>
              <td class="doc-model">${esc(m.model)}</td>
              <td class="muted">${m.ratio}${m.completion_ratio !== 1 ? ` / ${m.completion_ratio}` : ""}</td>
              ${groups.map((g) => `<td>${m.points_in[g] ?? "—"}</td>`).join("")}
              ${groups.map((g) => `<td>${m.points_out[g] ?? "—"}</td>`).join("")}
            </tr>`).join("")}
        </tbody>
      </table>
    </div>` : "";

  const callTable = callRows.length ? `
    <div class="doc-table-title">按次计费<span class="muted">（${esc(unit)} / 次）</span></div>
    <div class="doc-table-wrap">
      <table class="doc-table">
        <thead><tr><th>模型</th>${gh}</tr><tr><th class="muted">分组倍率</th>${gRatio}</tr></thead>
        <tbody>
          ${callRows.map((m) => `
            <tr><td class="doc-model">${esc(m.model)}</td>
            ${groups.map((g) => `<td>${m.points_per_call[g] ?? "—"}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>` : "";

  return `<div class="card">${staleBar}${tokenTable}${callTable}
    <div class="doc-note">实际扣费 = 基础倍率 × 分组倍率 × 用量。以调用时返回的实际消耗为准。</div>
  </div>`;
}

/* ---------- 索引页 ---------- */

async function renderDocs() {
  document.title = `使用教程 — ${BRAND()}`;
  renderLayout("/docs", `
    <div class="page-title">使用教程</div>
    <div class="page-sub">选择你要接入的产品，查看消耗说明与安装步骤</div>
    <div id="docsIdx" class="doc-grid"><div class="muted">加载中…</div></div>
  `);

  let products = [];
  try {
    products = (await api("/api/docs")).data.products || [];
  } catch (e) {
    document.getElementById("docsIdx").innerHTML =
      `<div class="doc-callout warn"><b>教程列表加载失败</b><div>${esc(e.message || "请稍后重试")}</div></div>`;
    return;
  }

  if (!products.length) {
    document.getElementById("docsIdx").innerHTML =
      `<div class="doc-callout info"><b>暂无可用教程</b><div>产品文档正在准备中。</div></div>`;
    return;
  }

  document.getElementById("docsIdx").innerHTML = products.map((p) => `
    <a class="doc-card" href="#/docs/${encodeURIComponent(p.id)}">
      <div class="doc-card-ico">${ICONS[p.icon] || ICONS.book}</div>
      <div class="doc-card-main">
        <b>${esc(p.title)}${p.badge ? `<span class="doc-badge">${esc(p.badge)}</span>` : ""}</b>
        <p>${esc(p.summary || "")}</p>
      </div>
      <span class="doc-card-arrow">→</span>
    </a>`).join("");
}

/* ---------- 产品详情页 ---------- */

async function renderDocProduct(id) {
  renderLayout("/docs", `<div class="muted" style="padding:24px">加载中…</div>`);

  let d;
  try {
    d = (await api(`/api/docs/${encodeURIComponent(id)}`)).data;
  } catch (e) {
    renderLayout("/docs", `
      <div class="page-title">教程未找到</div>
      <div class="doc-callout warn">
        <b>${esc(e.message || "该产品教程不存在")}</b>
        <div>可能已下线或链接有误。<a href="#/docs/client-config">返回使用教程</a></div>
      </div>`);
    return;
  }

  document.title = `${d.title} — ${BRAND()}`;
  // 只有带 title 的 section 才进目录，无标题的 callout 不该占一个锚点。
  const toc = (d.sections || [])
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => s.title && s.type !== "callout");

  renderLayout("/docs", `
    <div class="doc-crumb"><a href="#/docs/client-config">使用教程</a> <span>/</span> ${esc(d.title)}</div>
    <div class="page-title">${esc(d.title)}</div>
    ${d.summary ? `<div class="page-sub">${esc(d.summary)}</div>` : ""}
    ${toc.length > 1 ? `<div class="doc-toc">${
      toc.map(({ s, i }) => `<a href="#sec-${i}">${esc(s.title)}</a>`).join("")
    }</div>` : ""}
    ${(d.sections || []).map((s, i) => {
      const fn = SECTION[s.type];
      // 未知 type 直接跳过而不是渲染出错误占位：
      // 后端新增 section 类型时，旧前端应当优雅降级而非满屏红字。
      if (!fn) return "";
      return `<div id="sec-${i}">${fn(s)}</div>`;
    }).join("")}
  `);
  injectUserKeys();
  initDocVideos();
}
