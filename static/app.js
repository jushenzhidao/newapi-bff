/* NewAPI 控制台 SPA —— hash 路由，原生 JS，接口契约镜像 new-api（真实/mock 双模式） */
"use strict";

const app = document.getElementById("app");
let currentUser = null;

/* 站点配置（启动时从 /api/config 拉取）。
   品牌名、logo、接入地址、模型清单全部由服务端下发 ——
   换品牌/换域名/换模型只改环境变量，前端零改动。
   下面的值仅作请求失败时的兜底。 */
let SITE = {
  brand: { name: "API 平台", logo_text: "A", tagline: "", hero_title: "", hero_sub: "", icp: "", contact: "" },
  api: { base_url: "/v1", default_model: "gpt-4o-mini", models: [] },
  features: { redeem_login: true, mock_mode: false },
  demo_codes: [],
};
/* 常用字段的取值捷径 */
const BRAND = () => SITE.brand.name;
const LOGO = () => SITE.brand.logo_text;
const APIBASE = () => SITE.api.base_url;
const MODEL = () => SITE.api.default_model;

/* 活动与积分换算配置（启动时从后端拉取，全部换算口径由服务端下发） */
let PROMO = {
  unit: "积分",
  points_per_cny: 10000,
  pay_amounts: [{ cny: 10, points: 100000 }, { cny: 30, points: 300000 }, { cny: 50, points: 500000 },
                { cny: 100, points: 1000000 }, { cny: 300, points: 3000000 }, { cny: 500, points: 5000000 }],
  signup: { enabled: false, points: 0 },
  first_topup: { enabled: false, title: "", rate: 1, min_cny: 10, max_points: 0 },
};

/* ================= 工具 ================= */
async function api(path, options = {}) {
  const opts = { headers: {}, credentials: "same-origin", ...options };
  if (opts.body && typeof opts.body === "object") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401) {
    currentUser = null;
    const h = location.hash;
    // /docs 是公开页，访客停留在教程页时不应被 401 弹去登录，
    // 否则会出现「读教程读到一半被踢走」。与 PUBLIC_PAGES 保持一致。
    const open = ["#/login", "#/register", "#/home", "#/docs"];
    if (!open.some((p) => h.startsWith(p))) {
      location.hash = "#/login";
    }
    throw new Error((data && (data.message || data.detail)) || "未登录");
  }
  if (!res.ok || (data && data.success === false)) {
    throw new Error((data && (data.message || data.detail)) || `请求失败 (${res.status})`);
  }
  return data;
}

function toast(msg, type = "") {
  const box = document.getElementById("toast-box");
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtNum(n) { return Number(n || 0).toLocaleString("zh-CN"); }
/* 积分是对外唯一计价单位；服务端已完成 quota→积分换算，前端只负责展示 */
function fmtPoints(p) { return fmtNum(Math.round(p || 0)); }
/* 单条日志的积分可能不足 1（1 积分 = 50 quota），四舍五入会变 0，
   这里按量级动态保留小数，保证明细列不再出现「-」或「0」。 */
function fmtPointsFine(p) {
  const n = Number(p || 0);
  if (n === 0) return "0";
  const abs = Math.abs(n);
  if (abs >= 100) return fmtNum(Math.round(n));
  if (abs >= 1) return n.toFixed(2).replace(/\.?0+$/, "");
  return n.toFixed(abs >= 0.01 ? 2 : 4).replace(/\.?0+$/, "");
}
function fmtPointsUnit(p) { return `${fmtPoints(p)} ${PROMO.unit}`; }
/* 积分 → 参考人民币（仅在需要给用户价格感知时使用，如充值档位） */
function pointsToCny(p) { return (Number(p || 0) / (PROMO.points_per_cny || 10000)); }
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function setLoading(btn, loading, text) {
  if (!btn) return;
  if (loading) {
    btn.dataset.orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spin"></span> ${text || "处理中…"}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.orig || text || "";
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板", "success");
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
    toast("已复制到剪贴板", "success");
  }
}

function openModal(html, opts = {}) {
  const root = document.getElementById("modal-root");
  root.innerHTML = `<div class="modal-mask" id="modal-mask"><div class="modal" style="${opts.width ? "width:" + opts.width : ""}">${html}</div></div>`;
  if (!opts.lock) {
    root.querySelector("#modal-mask").addEventListener("click", e => {
      if (e.target.id === "modal-mask") closeModal();
    });
  }
}
function closeModal() { document.getElementById("modal-root").innerHTML = ""; }

async function refreshUser() {
  const r = await api("/api/user/self");
  currentUser = r.data;
  return currentUser;
}

async function loadPromo() {
  try {
    const r = await api("/api/promo");
    PROMO = r.data;
  } catch (_) { /* 用默认值兜底，不阻塞页面 */ }
}

async function loadSite() {
  try {
    const r = await api("/api/config");
    SITE = r.data;
  } catch (_) { /* 用默认值兜底，不阻塞页面 */ }
}

/* 品牌落地：文档标题 + favicon 都跟着配置走，不在 HTML 里写死 */
function applyBrand() {
  document.title = BRAND();
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
    <rect width="32" height="32" rx="7" fill="#2563eb"/>
    <text x="16" y="22" font-family="system-ui,sans-serif" font-size="18"
          font-weight="700" fill="#fff" text-anchor="middle">${esc(LOGO())}</text></svg>`;
  let link = document.querySelector("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = "data:image/svg+xml," + encodeURIComponent(svg);
}

/* 首充活动文案（无资格时返回空） */
function firstTopupTip(cny) {
  const ft = PROMO.first_topup;
  if (!ft.enabled || !currentUser || !currentUser.first_topup_available) return null;
  if (cny < ft.min_cny) return null;
  const bonus = Math.min(Math.round(cny * PROMO.points_per_cny * ft.rate), ft.max_points);
  return bonus > 0 ? bonus : null;
}

/* SVG 图标 */
const ICONS = {
  dashboard: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
  chart: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6" rx="1"/><rect x="12" y="8" width="3" height="10" rx="1"/><rect x="17" y="5" width="3" height="13" rx="1"/></svg>',
  topup: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/><path d="M6 15h4"/></svg>',
  key: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2"/><path d="M17 6l3 3"/><path d="M14 9l2 2"/></svg>',
  log: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/></svg>',
  book: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
  copy: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  eye: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  trash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  check: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  zap: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
  shield: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  layers: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 10 6-10 6L2 8l10-6z"/><path d="m2 14 10 6 10-6"/></svg>',
  wallet: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M16 12h4"/></svg>',
  gauge: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
  code: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/></svg>',
  info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
  alert: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  link: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  gift: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13"/><path d="M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7"/><path d="M7.5 8a2.5 2.5 0 0 1 0-5C11 3 12 8 12 8"/><path d="M16.5 8a2.5 2.5 0 0 0 0-5C13 3 12 8 12 8"/></svg>',
  card: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/><path d="M6 15h4"/></svg>',
  settings: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6 1.65 1.65 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v.09a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
};

/* ================= 路由 ================= */
const routes = {
  "/home": renderLanding,
  "/login": renderLogin,
  "/register": renderRegister,
  "/dashboard": renderDashboard,
  "/analytics": renderAnalytics,
  "/topup": renderTopup,
  "/keys": renderKeys,
  "/logs": renderLogs,
  // 教程页的渲染器定义在 docs.js，而 docs.js 在本文件之后加载。
  // 这里必须包一层惰性调用：直接写 renderDocs 会在求值这个对象字面量时
  // 就去取一个还不存在的绑定，抛 ReferenceError 并中断整个文件，
  // 连末尾的 router() 都不会执行 —— 表现为整站白页。
  "/docs": () => renderDocs(),
  "/admin": renderAdmin,
};
/* 教程是售前页面：未登录用户必须能读到消耗说明和安装步骤，
 * 否则等于把潜在客户拦在注册墙外。 */
const PUBLIC_PAGES = ["/home", "/login", "/register", "/docs"];

/* 取 hash 路由上的 query 参数，如 #/topup?trade_no=USR1NOxxx */
function hashQuery() {
  const q = location.hash.indexOf("?");
  return new URLSearchParams(q >= 0 ? location.hash.slice(q + 1) : "");
}

async function router() {
  const hash = location.hash.replace(/^#/, "") || "/home";
  const path = hash.split("?")[0];
  // 带参数的路径：routes 是精确匹配表，#/docs/points 这类需要单独识别，
  // 否则会静默落到 renderLanding 兜底，表现为「点进教程回到首页」。
  const docId = path.startsWith("/docs/") ? path.slice("/docs/".length) : "";
  const base = docId ? "/docs" : path;
  const view = docId
    ? () => renderDocProduct(decodeURIComponent(docId))
    : (routes[path] || renderLanding);
  if (!PROMO._loaded) {
    await Promise.all([loadPromo(), loadSite()]);   // 并行，省一个 RTT
    PROMO._loaded = true;
    applyBrand();
  }
  if (!PUBLIC_PAGES.includes(base)) {
    try { await refreshUser(); } catch (_) { return; }
  }
  await view();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", router);

/* ================= 产品首页（AI 科技风） ================= */
function renderLanding() {
  document.title = `${BRAND()} — ${SITE.brand.hero_title}`;
  const ft = PROMO.first_topup, sg = PROMO.signup;
  const rc = SITE.features.redeem_login;     // 兑换码入口开关
  const promoBar = (ft.enabled || sg.enabled) ? `
    <div class="promo-bar">
      <span class="promo-flag">限时活动</span>
      ${sg.enabled ? `<b>注册即送 ${fmtPoints(sg.points)} ${PROMO.unit}</b>` : ""}
      ${sg.enabled && ft.enabled ? '<i class="promo-sep"></i>' : ""}
      ${ft.enabled ? `<b>${esc(ft.title)}：首次充值额外赠送 ${Math.round(ft.rate * 100)}% ${PROMO.unit}</b>` : ""}
      <a href="#/register" class="promo-cta">立即参与 →</a>
    </div>` : "";
  app.innerHTML = `
  <div class="landing">
    ${promoBar}
    <div class="lp-bg">
      <div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div>
      <div class="grid-layer"></div>
    </div>
    <div class="lp-inner">
      <nav class="lp-nav">
        <div class="brand"><div class="logo-mark">${esc(LOGO())}</div>${esc(BRAND())}</div>
        <div class="nav-links">
          <a href="#features">能力</a>
          <a href="#pricing">价格</a>
          <a href="#/docs" onclick="location.hash='#/docs'">文档</a>
          <a href="#/login"><button class="btn-ghost-dark" style="padding:8px 18px">登录</button></a>
          <a href="#/register"><button class="btn-glow" style="padding:8px 18px">免费开始</button></a>
        </div>
      </nav>

      <header class="lp-hero">
        <div class="lp-badge"><span class="dot"></span>${esc(SITE.brand.hero_badge)}</div>
        <h1>${esc(SITE.brand.hero_h1)}<br>${esc(SITE.brand.hero_h1_prefix)}<span class="grad">${esc(SITE.brand.hero_h1_accent)}</span></h1>
        <p class="sub">${esc(SITE.brand.hero_sub)}${sg.enabled ? `注册即送 ${fmtPoints(sg.points)} ${PROMO.unit}，` : ""}一分钟完成接入。</p>
        <div class="cta">
          <a href="#/register"><button class="btn-glow">免费获取 API Key</button></a>
          ${rc ? '<a href="#/login"><button class="btn-ghost-dark">用兑换码登录</button></a>' : ""}
        </div>
        ${rc ? '<div class="cta-hint">已有兑换码？无需注册，输码即用</div>' : ""}

        <div class="lp-terminal">
          <div class="t-bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span class="t-title">terminal — 快速开始</span></div>
          <pre><span class="c-dim"># 把 base_url 指向 ${esc(BRAND())}，其余零改动</span>
<span class="c-purple">curl</span> ${APIBASE()}/chat/completions \\
  -H <span class="c-green">"Authorization: Bearer sk-your-key"</span> \\
  -d <span class="c-green">'{"model": "${MODEL()}", "messages": [{"role": "user", "content": "Hello!"}]}'</span>

<span class="c-blue">{"choices": [{"message": {"content": "Hi there! ..."}}]}</span> <span class="cursor"></span></pre>
        </div>

        <div class="lp-stats">
          <div class="s"><b><span class="grad">30+</span></b><span>可用模型</span></div>
          <div class="s"><b><span class="grad">99.9%</span></b><span>服务可用性</span></div>
          <div class="s"><b><span class="grad">&lt;200ms</span></b><span>接入延迟</span></div>
          <div class="s"><b><span class="grad">7×24</span></b><span>不间断服务</span></div>
        </div>
      </header>

      <section id="features">
        <h2 class="lp-sec-title">为开发者而生</h2>
        <p class="lp-sec-sub">兼容 OpenAI SDK，改一行 base_url 即可切换，无迁移成本</p>
        <div class="lp-features">
          <div class="lp-feature">
            <div class="f-icon" style="background:#3b5bfd22;color:#60a5fa">${ICONS.layers}</div>
            <b>全模型聚合</b>
            <p>GPT-4o、Claude、DeepSeek、Qwen 等 30+ 模型统一接入，一个 Key 全部搞定，随时切换。</p>
          </div>
          <div class="lp-feature">
            <div class="f-icon" style="background:#8b5cf622;color:#a78bfa">${ICONS.zap}</div>
            <b>极速响应</b>
            <p>多通道智能调度与故障自动切换，高峰期依然稳定，流式输出丝滑不卡顿。</p>
          </div>
          <div class="lp-feature">
            <div class="f-icon" style="background:#06b6d422;color:#22d3ee">${ICONS.wallet}</div>
            <b>按量计费</b>
            <p>用多少付多少，余额永不过期。微信 / 支付宝在线充值，兑换码即充即用。</p>
          </div>
          <div class="lp-feature">
            <div class="f-icon" style="background:#4ade8022;color:#4ade80">${ICONS.gauge}</div>
            <b>用量看板</b>
            <p>调用趋势、模型分布、Token 消耗实时可视化，成本一目了然。</p>
          </div>
          <div class="lp-feature">
            <div class="f-icon" style="background:#f59e0b22;color:#fbbf24">${ICONS.shield}</div>
            <b>安全可控</b>
            <p>Key 支持随时创建吊销、明文按需查看，会话加密签名，密钥永不落地前端。</p>
          </div>
          <div class="lp-feature">
            <div class="f-icon" style="background:#ec489922;color:#f472b6">${ICONS.code}</div>
            <b>OpenAI 兼容</b>
            <p>完全兼容 OpenAI API 规范，官方 SDK、LangChain、各类客户端开箱即用。</p>
          </div>
        </div>
      </section>

      <section id="pricing">
        <h2 class="lp-sec-title">透明定价</h2>
        <p class="lp-sec-sub">¥1 = ${fmtPoints(PROMO.points_per_cny)} ${PROMO.unit}，充值即时到账，无月费、无隐藏费用</p>
        <div class="lp-pricing">
          ${[
            { name: "体验", cny: 10, desc: "适合个人开发调试", hot: false, cta: "开始使用",
              feats: ["全部模型可用", `${PROMO.unit}永不过期`, "完整用量日志"] },
            { name: "标准", cny: 100, desc: "适合独立产品与小团队", hot: true, cta: "立即充值",
              feats: ["体验版全部功能", "多 Key 分环境管理", "用量看板与统计", "优先通道调度"] },
            { name: "规模化", cny: 500, desc: "适合企业级高并发场景", hot: false, cta: "联系我们",
              feats: ["标准版全部功能", "专属高速通道", "企业对公与发票支持"] },
          ].map(p => {
            const base = p.cny * PROMO.points_per_cny;
            const bonus = ft.enabled && p.cny >= ft.min_cny
              ? Math.min(Math.round(base * ft.rate), ft.max_points) : 0;
            return `
            <div class="lp-price ${p.hot ? "hot" : ""}">
              ${p.hot ? '<div class="hot-tag">最受欢迎</div>' : ""}
              <div class="p-name">${p.name}</div>
              <div class="p-amount">${fmtPoints(base)}<small> ${PROMO.unit}</small></div>
              <div class="p-cny">充值 ¥${p.cny}</div>
              ${bonus ? `<div class="p-bonus">首充再送 ${fmtPoints(bonus)} ${PROMO.unit}</div>` : ""}
              <div class="p-desc">${p.desc}</div>
              <ul>${p.feats.map(f => `<li>${ICONS.check}${f}</li>`).join("")}</ul>
              <a href="#/register"><button class="${p.hot ? "btn-glow" : "btn-ghost-dark"}" style="width:100%">${p.cta}</button></a>
            </div>`;
          }).join("")}
        </div>
      </section>

      <footer class="lp-foot">${esc(BRAND())} · 统一接入多家大模型 · <a href="#/docs" style="color:#7a86a8">接入文档</a>${SITE.brand.icp ? ` · <span>${esc(SITE.brand.icp)}</span>` : ""}${SITE.brand.contact ? ` · <span>${esc(SITE.brand.contact)}</span>` : ""}</footer>
    </div>
  </div>`;
}

/* ================= 登录 / 注册 ================= */

/* 上游限流时锁按钮 10 秒，避免用户狂点让 CriticalRateLimit 窗口无限延长 */
function lockOnRateLimit(btn, msg) {
  if (!/频繁/.test(msg)) return;
  btn.disabled = true;
  let left = 10;
  const orig = btn.textContent;
  btn.textContent = `请稍候 ${left}s`;
  const t = setInterval(() => {
    left--;
    btn.textContent = left > 0 ? `请稍候 ${left}s` : orig;
    if (left <= 0) { clearInterval(t); btn.disabled = false; }
  }, 1000);
}

function renderLogin() {
  document.title = `登录 — ${BRAND()}`;
  const rc = SITE.features.redeem_login;
  /* 兑换码登录是主推入口（免注册、开箱即用），默认选中；入口关闭时只剩密码登录 */
  const tab = (!rc || location.hash.includes("tab=password")) ? "password" : "code";
  /* mock 演示模式把预置卡号显示出来 —— 兑换码必须是真实发放的，编不出来 */
  const demoTip = (SITE.demo_codes && SITE.demo_codes.length) ? `
        <div class="auth-note">${ICONS.info}<span>演示模式可用兑换码：${
          SITE.demo_codes.map(c => `<code class="mono">${esc(c)}</code>`).join("、")
        }</span></div>` : "";
  app.innerHTML = `
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-logo"><div class="logo-mark">${esc(LOGO())}</div><span class="logo-name">${esc(BRAND())}</span></div>
      <div class="auth-sub">${esc(SITE.brand.tagline)}</div>

      ${rc ? `<div class="auth-tabs" role="tablist">
        <button class="auth-tab ${tab === "code" ? "active" : ""}" data-tab="code" role="tab">兑换码登录</button>
        <button class="auth-tab ${tab === "password" ? "active" : ""}" data-tab="password" role="tab">账号密码</button>
      </div>` : ""}

      ${rc ? `<div class="tab-panel ${tab === "code" ? "" : "hidden"}" id="panel-code">
        <form id="code-form">
          <div class="field">
            <label>兑换码</label>
            <input id="l-code" placeholder="请输入卡片上的兑换码" autocomplete="off" spellcheck="false">
            <div class="hint">首次使用将自动开通账号并充值到账，无需注册</div>
          </div>
          <button class="btn btn-primary btn-block" id="c-submit" type="submit">兑换并登录</button>
        </form>
        <div class="auth-note">${ICONS.info}<span>兑换码即账号凭证，请妥善保管。登录后可在控制台绑定用户名密码，避免丢码丢余额。</span></div>
        ${demoTip}
      </div>` : ""}

      <div class="tab-panel ${tab === "password" ? "" : "hidden"}" id="panel-password">
        <form id="login-form">
          <div class="field">
            <label>用户名</label>
            <input id="l-username" placeholder="请输入用户名" autocomplete="username">
          </div>
          <div class="field">
            <label>密码</label>
            <input id="l-password" type="password" placeholder="请输入密码" autocomplete="current-password">
          </div>
          <button class="btn btn-primary btn-block" id="l-submit" type="submit">登 录</button>
        </form>
        <div class="auth-switch">还没有账号？<a href="#/register">立即注册</a></div>
      </div>

      <a class="auth-back" href="#/home">← 返回首页</a>
    </div>
  </div>`;

  app.querySelectorAll(".auth-tab").forEach(b => {
    b.onclick = () => {
      app.querySelectorAll(".auth-tab").forEach(x => x.classList.toggle("active", x === b));
      const t = b.dataset.tab;
      document.getElementById("panel-code")?.classList.toggle("hidden", t !== "code");
      document.getElementById("panel-password")?.classList.toggle("hidden", t !== "password");
    };
  });

  const codeForm = document.getElementById("code-form");
  if (codeForm) codeForm.onsubmit = async e => {
    e.preventDefault();
    const code = document.getElementById("l-code").value.trim();
    if (!code) return toast("请输入兑换码", "error");
    const btn = document.getElementById("c-submit");
    setLoading(btn, true, "兑换中…");
    try {
      const r = await api("/api/user/login/code", { method: "POST", body: { code } });
      toast(r.message, "success");
      location.hash = "#/dashboard";
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
      lockOnRateLimit(btn, err.message);
    }
  };

  document.getElementById("login-form").onsubmit = async e => {
    e.preventDefault();
    const username = document.getElementById("l-username").value.trim();
    const password = document.getElementById("l-password").value;
    if (!username || !password) return toast("请输入用户名和密码", "error");
    const btn = document.getElementById("l-submit");
    setLoading(btn, true, "登录中…");
    try {
      await api("/api/user/login", { method: "POST", body: { username, password } });
      toast("登录成功", "success");
      location.hash = "#/dashboard";
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
      lockOnRateLimit(btn, err.message);
    }
  };
}

function renderRegister() {
  document.title = `注册 — ${BRAND()}`;
  app.innerHTML = `
  <div class="auth-wrap">
    <div class="auth-card">
      <div class="auth-logo"><div class="logo-mark">${esc(LOGO())}</div><span class="logo-name">创建账号</span></div>
      <div class="auth-sub">一分钟完成注册，立即获取 API Key</div>
      ${PROMO.signup.enabled ? `<div class="auth-gift">${ICONS.gift}<span>注册即送 <b>${fmtPoints(PROMO.signup.points)} ${PROMO.unit}</b>，可直接调用体验</span></div>` : ""}
      <form id="reg-form">
        <div class="field">
          <label>用户名</label>
          <input id="r-username" placeholder="2-20 位字母、数字或下划线">
        </div>
        <div class="field">
          <label>密码</label>
          <input id="r-password" type="password" placeholder="8-20 位" maxlength="20" autocomplete="new-password">
        </div>
        <div class="field">
          <label>邮箱</label>
          <input id="r-email" type="email" placeholder="用于接收验证码" required>
        </div>
        <div class="field">
          <label>邮箱验证码</label>
          <div class="field-row">
            <input id="r-code" placeholder="6 位数字" maxlength="6" required>
            <button class="btn btn-outline" id="r-send" type="button">获取验证码</button>
          </div>
          <div class="hint">验证码将发送到上方邮箱，10 分钟内有效；请确认邮箱可正常收信</div>
        </div>
        <button class="btn btn-primary btn-block" id="r-submit" type="submit">注 册</button>
      </form>
      <div class="auth-switch">已有账号？<a href="#/login">直接登录</a></div>
      <a class="auth-back" href="#/home">← 返回首页</a>
    </div>
  </div>`;

  const sendBtn = document.getElementById("r-send");
  sendBtn.onclick = async () => {
    const email = document.getElementById("r-email").value.trim();
    if (!email) return toast("请先填写邮箱", "error");
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return toast("邮箱格式不正确", "error");
    sendBtn.disabled = true;
    try {
      const r = await api("/api/verification", { method: "POST", body: { email } });
      toast(r.message, "success");
      let left = 60;
      sendBtn.textContent = `${left}s 后重发`;
      const timer = setInterval(() => {
        left--;
        if (left <= 0) { clearInterval(timer); sendBtn.disabled = false; sendBtn.textContent = "重新获取"; }
        else sendBtn.textContent = `${left}s 后重发`;
      }, 1000);
    } catch (err) {
      toast(err.message, "error");
      sendBtn.disabled = false;
    }
  };

  document.getElementById("reg-form").onsubmit = async e => {
    e.preventDefault();
    const body = {
      username: document.getElementById("r-username").value.trim(),
      password: document.getElementById("r-password").value,
      email: document.getElementById("r-email").value.trim(),
      verification_code: document.getElementById("r-code").value.trim(),
    };
    const btn = document.getElementById("r-submit");
    setLoading(btn, true, "注册中…");
    try {
      const r = await api("/api/user/register", { method: "POST", body });
      const gift = r.data && r.data.gift_points;
      toast(gift ? `注册成功，已赠送 ${fmtPoints(gift)} ${PROMO.unit}` : "注册成功，已自动登录", "success");
      location.hash = "#/dashboard";
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
    }
  };
}

/* ================= 控制台布局 ================= */
function renderLayout(active, contentHtml) {
  document.title = `控制台 — ${BRAND()}`;
  const nav = [
    ["概览", [["dashboard", "/dashboard", "仪表盘"], ["chart", "/analytics", "用量看板"]]],
    ["资产", [["topup", "/topup", "充值中心"], ["key", "/keys", "API Key"], ["log", "/logs", "调用日志"]]],
    ["帮助", [["book", "/docs", "使用教程"]]],
  ];
  // /docs 是公开页，未登录也会走到这里，此时 currentUser 仍是 null。
  // 用局部空对象兜住，避免访客访问教程页时属性读取抛异常导致整页白屏。
  const u = currentUser || {};
  const guest = !currentUser;
  // 管理入口按后端下发的 is_admin 显示。隐藏只是体感，真正的拦截在
  // /api/admin/* 的 require_admin —— 前端标志被改也拿不到数据。
  if (u.is_admin) nav.push(["管理", [["settings", "/admin", "站点配置"]]]);
  app.innerHTML = `
  <div class="layout">
    <aside class="sidebar">
      <a class="brand" href="#/home"><div class="logo-mark">${esc(LOGO())}</div><b>${esc(BRAND())}</b></a>
      <nav class="nav">
        ${nav.map(([group, items]) => `
          <div class="nav-group">${group}</div>
          ${items.map(([icon, href, label]) =>
            `<a href="#${href}" class="${active === href ? "active" : ""}">${ICONS[icon]}${label}</a>`).join("")}
        `).join("")}
      </nav>
      <div class="sidebar-foot">
        ${guest ? `
        <div class="user-line">
          <span class="uname muted">未登录</span>
          <a class="logout-btn" href="#/login">登录</a>
        </div>
        <div class="muted">登录后可创建 Key 与查看用量</div>
        ` : `
        <div class="user-line">
          <span class="uname">${esc(u.display_name || u.username)}</span>
          <button class="logout-btn" id="btn-logout">退出</button>
        </div>
        <div class="muted">${u.is_redeem_account ? "兑换码账号"
          : (u.email && u.email !== "-" ? esc(u.email) : "已绑定账号")}</div>
        `}
      </div>
    </aside>
    <main class="main">${contentHtml}</main>
  </div>`;
  // 访客态渲染的是登录链接而非按钮，这里必须判空。
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.onclick = async () => {
    await api("/api/user/logout");
    currentUser = null;
    location.hash = "#/home";
  };
}

/* ================= 仪表盘 ================= */
async function renderDashboard() {
  const u = currentUser;
  const ft = PROMO.first_topup;
  const ftBanner = (ft.enabled && u.first_topup_available) ? `
    <a class="ft-banner" href="#/topup">
      <span class="ft-icon">${ICONS.zap}</span>
      <div class="ft-text">
        <b>${esc(ft.title)}</b>
        <span>首次充值满 ¥${ft.min_cny} 起，额外赠送 ${Math.round(ft.rate * 100)}% ${PROMO.unit}（最高 ${fmtPoints(ft.max_points)}）</span>
      </div>
      <span class="ft-go">去充值 →</span>
    </a>` : "";
  /* 兑换码账号：码是唯一凭证，丢码=丢余额，必须显眼提示绑定 */
  const bindBanner = u.is_redeem_account ? `
    <div class="bind-banner">
      <span class="bb-icon">${ICONS.alert}</span>
      <div class="bb-text">
        <b>当前为兑换码账号</b>
        <span>兑换码是你唯一的登录凭证，一旦遗失将无法找回余额。建议绑定用户名密码。</span>
      </div>
      <button class="btn btn-sm" id="btn-bind">立即绑定</button>
    </div>` : "";
  renderLayout("/dashboard", `
    <div class="page-title">仪表盘</div>
    <div class="page-sub">欢迎回来，${esc(u.display_name || u.username)}</div>
    ${bindBanner}
    ${ftBanner}

    <div class="grid grid-3">
      <div class="card balance-card">
        <div class="b-label">可用${PROMO.unit}</div>
        <div class="b-value">${fmtPoints(u.points)}</div>
        <div class="b-sub">${PROMO.unit}余额，永不过期</div>
        <a href="#/topup"><button class="btn btn-sm">立即充值</button></a>
      </div>
      <div class="card stat-card">
        <div class="stat-label">累计消耗</div>
        <div class="stat-value">${fmtPoints(u.used_points)}</div>
        <div class="stat-extra">${PROMO.unit}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">调用次数</div>
        <div class="stat-value">${fmtNum(u.request_count)}</div>
        <div class="stat-extra">全部时间</div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <div class="card-title">快捷入口</div>
      <div class="quick-grid">
        <a class="quick-item" href="#/topup">
          <div class="qi-icon" style="background:var(--primary-soft);color:var(--primary)">${ICONS.topup}</div>
          <b>${PROMO.unit}充值</b><span>在线支付 / 兑换码</span>
        </a>
        <a class="quick-item" href="#/keys">
          <div class="qi-icon" style="background:var(--success-soft);color:var(--success)">${ICONS.key}</div>
          <b>API Key</b><span>创建与管理密钥</span>
        </a>
        <a class="quick-item" href="#/analytics">
          <div class="qi-icon" style="background:var(--warn-soft);color:var(--warn)">${ICONS.chart}</div>
          <b>用量看板</b><span>趋势与模型分布</span>
        </a>
        <a class="quick-item" href="#/docs">
          <div class="qi-icon" style="background:var(--danger-soft);color:var(--danger)">${ICONS.book}</div>
          <b>使用教程</b><span>一分钟快速接入</span>
        </a>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <div class="card-title" style="margin-bottom:0">最近调用</div>
        <a href="#/logs" class="muted" style="color:var(--primary)">查看全部 →</a>
      </div>
      <div id="recent-logs"><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div></div>
    </div>`);

  const bindBtn = document.getElementById("btn-bind");
  if (bindBtn) bindBtn.onclick = openBindModal;

  // 异步加载最近调用，不阻塞首屏
  try {
    const r = await api("/api/log/self?p=1&page_size=5");
    const items = r.data.items || [];
    const box = document.getElementById("recent-logs");
    if (!box) return;
    box.innerHTML = items.length === 0 ? `<div class="empty-row muted" style="text-align:center;padding:20px 0">暂无调用记录</div>` : `
      <div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>类型</th><th>模型 / 说明</th><th>${PROMO.unit}</th></tr></thead>
        <tbody>${items.map(l => logRowHtml(l, true)).join("")}</tbody>
      </table></div>`;
  } catch (_) {}
}

/* ================= 绑定正式账号（兑换码账号专用） ================= */
function openBindModal() {
  openModal(`
    <div class="modal-title">绑定账号</div>
    <div class="modal-desc">设置用户名和密码后，即可不依赖兑换码登录。当前余额、API Key 与调用记录全部保留。</div>
    <form id="bind-form">
      <div class="field">
        <label>用户名</label>
        <input id="b-username" placeholder="2-20 位字母、数字或下划线" autocomplete="username">
      </div>
      <div class="field">
        <label>密码</label>
        <input id="b-password" type="password" placeholder="8-20 位" maxlength="20" autocomplete="new-password">
      </div>
      <div class="field">
        <label>确认密码</label>
        <input id="b-password2" type="password" placeholder="再次输入密码" maxlength="20" autocomplete="new-password">
      </div>
      <div class="auth-note">${ICONS.info}<span>绑定后原兑换码将不能再用于登录，请改用新的用户名密码。</span></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-outline" id="b-cancel">稍后再说</button>
        <button type="submit" class="btn btn-primary" id="b-submit">确认绑定</button>
      </div>
    </form>`, { width: "440px" });

  document.getElementById("b-cancel").onclick = closeModal;
  document.getElementById("bind-form").onsubmit = async e => {
    e.preventDefault();
    const username = document.getElementById("b-username").value.trim();
    const password = document.getElementById("b-password").value;
    const password2 = document.getElementById("b-password2").value;
    if (!/^[a-zA-Z0-9_]{2,20}$/.test(username)) return toast("用户名需为 2-20 位字母、数字或下划线", "error");
    if (password.length < 8 || password.length > 20) return toast("密码需为 8-20 位", "error");
    if (password !== password2) return toast("两次输入的密码不一致", "error");
    const btn = document.getElementById("b-submit");
    setLoading(btn, true, "绑定中…");
    try {
      await api("/api/user/bind", { method: "POST", body: { username, password } });
      closeModal();
      toast("绑定成功，以后可用该账号密码登录", "success");
      await refreshUser();
      renderDashboard();
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
    }
  };
}

/* 日志行渲染（共享） */
function logRowHtml(l, compact = false) {
  const t = l.type;
  let typeBadge, desc;
  if (t === 1) { typeBadge = '<span class="badge badge-blue">充值</span>'; desc = esc(l.content || "充值"); }
  else if (t === 2) { typeBadge = '<span class="badge badge-green">消费</span>'; desc = `<span class="mono">${esc(l.model_name || "-")}</span>`; }
  else if (t === 7) { typeBadge = '<span class="badge badge-gray">登录</span>'; desc = "账号登录"; }
  else { typeBadge = '<span class="badge badge-gray">系统</span>'; desc = esc(l.content || "-").slice(0, 40); }
  const pts = Number(l.points) > 0
    ? (t === 1 ? `<span class="text-green">+${fmtPointsFine(l.points)}</span>` : `<span class="pts-out">-${fmtPointsFine(l.points)}</span>`)
    : "-";
  if (compact) {
    return `<tr><td class="muted">${fmtTime(l.created_at)}</td><td>${typeBadge}</td><td>${desc}</td><td>${pts}</td></tr>`;
  }
  return `<tr>
    <td class="muted">${fmtTime(l.created_at)}</td>
    <td>${typeBadge}</td>
    <td>${desc}</td>
    <td class="muted">${esc(l.token_name || "-")}</td>
    <td>${t === 2 ? fmtNum(l.prompt_tokens) : "-"}</td>
    <td>${t === 2 ? fmtNum(l.completion_tokens) : "-"}</td>
    <td>${pts}</td>
  </tr>`;
}

/* ================= 用量看板 ================= */
async function renderAnalytics() {
  const u = currentUser;
  renderLayout("/analytics", `
    <div class="page-title">用量看板</div>
    <div class="page-sub">调用趋势、模型分布与 Token 消耗</div>
    <div id="ana-body">
      <div class="grid grid-4">
        ${[1,2,3,4].map(() => '<div class="card stat-card"><div class="skeleton sk-line" style="width:60%"></div><div class="skeleton sk-line" style="height:22px"></div></div>').join("")}
      </div>
      <div class="card" style="margin-top:18px"><div class="skeleton sk-block"></div></div>
    </div>`);

  // 拉最近 100 条日志做前端聚合（MVP 方案，量大时应改后端聚合接口）
  let items = [], stat = { request_count: 0, points: 0, prompt_tokens: 0, completion_tokens: 0 };
  try {
    const r = await api("/api/log/self?p=1&page_size=100");
    items = (r.data.items || []).filter(l => l.type === 2);
    stat = r.data.stat || stat;
  } catch (err) {
    toast(err.message, "error");
  }

  // 近 7 天按天聚合
  const days = [];
  const now = new Date();
  for (let i = 6; i >= 0; i--) {
    const d = new Date(now); d.setDate(now.getDate() - i);
    days.push({ label: `${d.getMonth() + 1}/${d.getDate()}`, y: d.getFullYear(), m: d.getMonth(), dd: d.getDate(), count: 0, points: 0 });
  }
  for (const l of items) {
    const d = new Date(l.created_at * 1000);
    const hit = days.find(x => x.y === d.getFullYear() && x.m === d.getMonth() && x.dd === d.getDate());
    if (hit) { hit.count++; hit.points += l.points; }
  }
  // 模型聚合
  const byModel = {};
  for (const l of items) {
    const m = l.model_name || "unknown";
    byModel[m] = byModel[m] || { count: 0, points: 0, pt: 0, ct: 0 };
    byModel[m].count++; byModel[m].points += l.points;
    byModel[m].pt += l.prompt_tokens; byModel[m].ct += l.completion_tokens;
  }
  const models = Object.entries(byModel).sort((a, b) => b[1].points - a[1].points).slice(0, 8);
  const maxModelPoints = Math.max(1, ...models.map(([, v]) => v.points));

  // 柱状图 SVG
  const W = 720, H = 200, pad = 30;
  const maxCount = Math.max(1, ...days.map(d => d.count));
  const bw = (W - pad * 2) / days.length * 0.52;
  const bars = days.map((d, i) => {
    const x = pad + (W - pad * 2) / days.length * (i + 0.5) - bw / 2;
    const h = Math.max(2, (H - pad * 2) * d.count / maxCount);
    const y = H - pad - h;
    return `
      <rect class="bar-anim" style="animation-delay:${i * 60}ms" x="${x}" y="${y}" width="${bw}" height="${h}" rx="5" fill="url(#barGrad)"/>
      <text x="${x + bw / 2}" y="${H - pad + 16}" text-anchor="middle" font-size="11" fill="#98a1b3">${d.label}</text>
      ${d.count ? `<text x="${x + bw / 2}" y="${y - 6}" text-anchor="middle" font-size="11" font-weight="600" fill="#5b6478">${d.count}</text>` : ""}`;
  }).join("");

  const total7 = days.reduce((s, d) => s + d.count, 0);
  const points7 = days.reduce((s, d) => s + d.points, 0);

  const body = document.getElementById("ana-body");
  if (!body) return;
  body.innerHTML = `
    <div class="grid grid-4">
      <div class="card stat-card">
        <div class="stat-label">近 7 天调用</div>
        <div class="stat-value">${fmtNum(total7)}</div>
        <div class="stat-extra">最近 100 条内统计</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">近 7 天消耗</div>
        <div class="stat-value">${fmtPointsFine(points7)}</div>
        <div class="stat-extra">${PROMO.unit}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">累计消耗</div>
        <div class="stat-value">${fmtPoints(u.used_points)}</div>
        <div class="stat-extra">全部时间 · ${PROMO.unit}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">可用${PROMO.unit}</div>
        <div class="stat-value">${fmtPoints(u.points)}</div>
        <div class="stat-extra"><a href="#/topup" style="color:var(--primary)">去充值 →</a></div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <div class="card-title">近 7 天调用趋势</div>
      <div class="chart-box">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="barGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#3b5bfd"/><stop offset="100%" stop-color="#8b5cf6"/>
            </linearGradient>
          </defs>
          <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="#e5e9f2" stroke-width="1"/>
          ${bars}
        </svg>
      </div>
      <div class="chart-legend"><span class="lg"><i style="background:linear-gradient(180deg,#3b5bfd,#8b5cf6)"></i>调用次数</span></div>
    </div>

    <div class="card" style="margin-top:18px;">
      <div class="card-title">模型${PROMO.unit}消耗排行</div>
      <div class="muted" style="margin:-6px 0 12px;font-size:12px">按${PROMO.unit}消耗从高到低排序，右侧为调用次数</div>
      ${models.length === 0 ? '<div class="muted" style="text-align:center;padding:24px 0">暂无消费记录，接入后这里会展示各模型的消耗分布</div>' :
        models.map(([name, v], i) => `
          <div class="model-row">
            <span class="m-name" title="${esc(name)}">${esc(name)}</span>
            <div class="m-bar-track"><div class="m-bar" style="width:${Math.max(3, v.points / maxModelPoints * 100)}%;animation-delay:${i * 70}ms"></div></div>
            <span class="m-val">${fmtPointsFine(v.points)}</span>
            <span class="m-val muted">${v.count} 次</span>
          </div>`).join("")}
    </div>`;
}

/* ================= 充值中心 ================= */
let topupState = { tab: "pay", amount: 100, method: "wxpay" };

async function renderTopup() {
  const u = currentUser;
  const st = topupState;
  const ft = PROMO.first_topup;
  // 档位若被后端配置改动，纠正默认选中项
  const amounts = PROMO.pay_amounts || [];
  if (amounts.length && !amounts.some(a => a.cny === st.amount)) st.amount = amounts[0].cny;

  const ftBanner = (ft.enabled && u.first_topup_available) ? `
    <div class="ft-banner static">
      <span class="ft-icon">${ICONS.zap}</span>
      <div class="ft-text">
        <b>${esc(ft.title)}</b>
        <span>首次充值满 ¥${ft.min_cny}，额外赠送 ${Math.round(ft.rate * 100)}% ${PROMO.unit}（单次最高 ${fmtPoints(ft.max_points)}），仅限一次</span>
      </div>
    </div>` : "";

  renderLayout("/topup", `
    <div class="page-title">充值中心</div>
    <div class="page-sub">当前可用 <b id="topup-balance">${fmtPoints(u.points)}</b> ${PROMO.unit} · ¥1 = ${fmtPoints(PROMO.points_per_cny)} ${PROMO.unit}</div>
    ${ftBanner}

    <div class="topup-tabs">
      <button class="topup-tab ${st.tab === "pay" ? "active" : ""}" data-tab="pay">在线支付</button>
      <button class="topup-tab ${st.tab === "code" ? "active" : ""}" data-tab="code">兑换码充值</button>
    </div>

    <div class="card" id="topup-body">${st.tab === "pay" ? payTabHtml() : codeTabHtml()}</div>`);

  document.querySelectorAll(".topup-tab").forEach(btn => {
    btn.onclick = () => { topupState.tab = btn.dataset.tab; renderTopup(); };
  });
  if (st.tab === "pay") bindPayTab(); else bindCodeTab();

  // 支付回跳落地：URL 上带 trade_no 说明用户刚从收银台回来，直接查这一单
  const tradeNo = hashQuery().get("trade_no");
  if (tradeNo && tradeNo !== settledTradeNo) checkReturnedOrder(tradeNo);
}

/* 已处理过的回跳订单号。避免切 Tab 重渲染时对同一单反复弹提示 */
let settledTradeNo = "";

/* 从收银台回跳后确认到账。
   到账本身由上游 notify 完成，这里只是读状态 —— 查询失败也不影响钱。 */
async function checkReturnedOrder(tradeNo) {
  settledTradeNo = tradeNo;
  const amounts = PROMO.pay_amounts || [];
  // baseline 传 0：回跳后已无从得知支付前余额，服务端会走按单查询，不依赖 baseline
  try {
    const r = await api("/api/user/pay/status", {
      method: "POST",
      body: { amount: topupState.amount || (amounts[0] && amounts[0].cny) || 0,
              baseline_points: 0, order_no: tradeNo },
    });
    const d = r.data;
    if (d.paid) {
      const bonusMsg = d.bonus_points ? `（含首充赠送 ${fmtPoints(d.bonus_points)}）` : "";
      toast(`支付成功，积分已到账${bonusMsg}`, "success");
      await refreshUser();
      const bal = document.getElementById("topup-balance");
      if (bal && currentUser) bal.textContent = fmtPoints(currentUser.points);
    } else {
      // 支付网关回跳通常快于 notify 落库，这属于正常时序，不是错误
      toast("已收到支付结果，正在确认到账，稍后刷新即可查看余额", "");
    }
  } catch (_) {
    // 静默：回跳只是体验优化，查询失败不该给用户报错（钱不受影响）
  }
}

function payTabHtml() {
  const st = topupState;
  const list = PROMO.pay_amounts || [];
  const sel = list.find(a => a.cny === st.amount) || list[0] || { cny: st.amount, points: st.amount * PROMO.points_per_cny };
  const bonus = firstTopupTip(sel.cny);
  const maxCny = Math.max(...list.map(a => a.cny), 0);
  return `
    <div class="card-title">选择充值${PROMO.unit}</div>
    <div class="amount-grid">
      ${list.map(a => {
        const b = firstTopupTip(a.cny);
        const tag = b ? `+${fmtPoints(b)}` : (a.cny === maxCny ? "大额" : (a.cny === 100 ? "热门" : ""));
        return `
        <button class="amount-item ${st.amount === a.cny ? "selected" : ""} ${b ? "has-bonus" : ""}" data-amount="${a.cny}">
          <span class="amt">${fmtPoints(a.points)}</span>
          <span class="amt-unit">${PROMO.unit}</span>
          <span class="amt-cny">¥${a.cny}</span>
          <span class="bonus">${tag || "&nbsp;"}</span>
        </button>`;
      }).join("")}
    </div>
    <div class="card-title">支付方式</div>
    <div class="pay-methods">
      <button class="pay-method ${st.method === "wxpay" ? "selected" : ""}" data-method="wxpay">
        <span class="pm-dot" style="background:#07c160">微</span>微信支付
      </button>
      <button class="pay-method ${st.method === "alipay" ? "selected" : ""}" data-method="alipay">
        <span class="pm-dot" style="background:#1677ff">支</span>支付宝
      </button>
    </div>
    <div class="pay-summary">
      <div class="ps-row"><span>充值到账</span><b>${fmtPoints(sel.points)} ${PROMO.unit}</b></div>
      ${bonus ? `<div class="ps-row bonus"><span>首充赠送</span><b>+${fmtPoints(bonus)} ${PROMO.unit}</b></div>` : ""}
      <div class="ps-row total"><span>合计获得</span><b>${fmtPoints(sel.points + (bonus || 0))} ${PROMO.unit}</b></div>
    </div>
    <button class="btn btn-primary btn-block" id="btn-pay">支付 ¥${sel.cny}，获得 ${fmtPoints(sel.points + (bonus || 0))} ${PROMO.unit}</button>
    <div class="muted" style="margin-top:10px;text-align:center">点击后将打开收银台，支付成功后${PROMO.unit}自动到账</div>`;
}

function bindPayTab() {
  document.querySelectorAll(".amount-item").forEach(btn => {
    btn.onclick = () => { topupState.amount = Number(btn.dataset.amount); renderTopup(); };
  });
  document.querySelectorAll(".pay-method").forEach(btn => {
    btn.onclick = () => { topupState.method = btn.dataset.method; renderTopup(); };
  });
  document.getElementById("btn-pay").onclick = async () => {
    const st = topupState;
    const btn = document.getElementById("btn-pay");
    setLoading(btn, true, "创建订单…");
    try {
      const r = await api("/api/user/pay", { method: "POST", body: { amount: st.amount, payment_method: st.method } });
      const d = r.data;
      if (d.mode === "epay") {
        // 真实模式：打开易支付收银台（新窗口），本页轮询到账。
        // 两种形态：微信支付等网关直接给跳转地址（redirect），支付宝等给表单字段（form）。
        openCashier(d);
        showPayWaiting(d);
      } else {
        showMockCashier(d);
      }
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setLoading(btn, false);
    }
  };
}

// 打开收银台。两种网关形态由后端 gateway_mode 指明：
//   redirect —— 上游只给一个跳转地址（微信支付走这条），直接新窗口打开
//   form     —— 上游给易支付表单字段，需构表单 POST 提交
// 之所以不靠「params 是否为空」自动判断：空 params 也可能是上游异常，
// 那种情况下静默跳到网关根地址会得到一个看不懂的错误页。
function openCashier(order) {
  if (order.gateway_mode === "redirect") {
    const w = window.open(order.gateway, "_blank");
    if (!w) {
      // 弹窗被拦时必须留一个用户可点的入口，否则页面只是转圈、钱永远付不出去。
      // 记在 order 上，由等待弹窗渲染成链接（见 showPayWaiting）。
      order.blocked = true;
      toast("浏览器拦截了新窗口，请点弹窗里的链接继续支付", "error");
    }
    return;
  }
  submitEpayForm(order.gateway, order.params);
}

function submitEpayForm(gateway, params) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = gateway;
  form.target = "_blank";
  for (const [k, v] of Object.entries(params)) {
    const input = document.createElement("input");
    input.type = "hidden"; input.name = k; input.value = v;
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

function showPayWaiting(order) {
  let polling = true;
  let waited = 0;
  openModal(`
    <h3>等待支付结果</h3>
    <div class="pay-waiting">
      <div class="pw-spin"></div>
      <div>已打开收银台，本次将获得 <b>${fmtPoints(order.total_points)} ${PROMO.unit}</b></div>
      <div class="muted" style="margin-top:6px">订单金额 ¥${order.amount}${order.bonus_points ? ` · 含首充赠送 ${fmtPoints(order.bonus_points)}` : ""}</div>
      <div class="muted" style="margin-top:6px">订单号 <span class="mono">${esc(order.order_no)}</span></div>
      <div class="muted" id="pw-hint" style="margin-top:6px">支付完成后本页会自动检测到账，请勿关闭</div>
      ${order.blocked ? `<div style="margin-top:8px"><a href="${esc(order.gateway)}" target="_blank" rel="noopener noreferrer">收银台被拦截，点此手动打开</a></div>` : ""}
    </div>
    <div class="modal-actions">
      <button class="btn btn-outline" id="pw-close">取消等待</button>
      <button class="btn btn-primary" id="pw-check">我已支付，立即检查</button>
    </div>`, { lock: true });
  document.getElementById("pw-close").onclick = () => { polling = false; closeModal(); };
  document.getElementById("pw-check").onclick = () => check(true);

  async function check(manual) {
    try {
      const r = await api("/api/user/pay/status", {
        method: "POST",
        body: {
          amount: order.amount,
          baseline_points: order.baseline_points,
          // 订单号是到账判定的权威依据（服务端按单查上游），余额比对仅作兜底
          order_no: order.order_no || "",
        },
      });
      const d = r.data;
      if (d.paid) {
        polling = false;
        closeModal();
        const bonusMsg = d.bonus_points ? `（含首充赠送 ${fmtPoints(d.bonus_points)}）` : "";
        toast(`支付成功，到账 ${fmtPoints(d.points - order.baseline_points)} ${PROMO.unit}${bonusMsg}`, "success");
        await refreshUser();
        renderTopup();
        return true;
      }
      if (manual) toast("尚未检测到到账，请稍候或确认已完成支付", "");
    } catch (err) {
      if (manual) toast(err.message, "error");
    }
    return false;
  }

  const tick = async () => {
    if (!polling) return;
    const done = await check(false);
    if (done) return;
    waited += 3;
    const hint = document.getElementById("pw-hint");
    if (hint && waited >= 90) hint.textContent = "仍未检测到到账？可关闭本窗口，到账后刷新页面即可看到。";
    if (waited < 600) setTimeout(tick, 3000);
  };
  setTimeout(tick, 3000);
}

function showMockCashier(order) {
  const methodName = order.method === "alipay" ? "支付宝" : "微信支付";
  openModal(`
    <h3>模拟收银台 — ${methodName}</h3>
    <div class="muted">订单号 <span class="mono">${esc(order.order_no)}</span></div>
    <div class="pay-waiting">
      <div style="font-size:30px;font-weight:700;margin:14px 0 4px">¥${order.amount}</div>
      <div class="muted">到账 ${fmtPoints(order.total_points)} ${PROMO.unit}${order.bonus_points ? `（含首充赠送 ${fmtPoints(order.bonus_points)}）` : ""}</div>
    </div>
    <div class="muted" style="text-align:center">演示环境，点击下方按钮模拟到账</div>
    <div class="modal-actions">
      <button class="btn btn-outline" id="cashier-cancel">取消</button>
      <button class="btn btn-primary" id="cashier-ok">我已完成支付</button>
    </div>`);
  document.getElementById("cashier-cancel").onclick = closeModal;
  document.getElementById("cashier-ok").onclick = async () => {
    const btn = document.getElementById("cashier-ok");
    setLoading(btn, true, "确认中…");
    try {
      const r = await api("/api/user/pay/confirm", { method: "POST", body: { order_no: order.order_no } });
      closeModal();
      toast(r.message, "success");
      await refreshUser();
      renderTopup();
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
    }
  };
}

function codeTabHtml() {
  return `
    <div class="card-title">兑换码充值</div>
    <div class="field">
      <label>兑换码</label>
      <div class="field-row">
        <input id="code-input" placeholder="请输入兑换码">
        <button class="btn btn-primary" id="btn-redeem">立即兑换</button>
      </div>
    </div>
    <div class="muted">兑换成功后${PROMO.unit}实时到账，可在调用日志中查看充值记录</div>`;
}

function bindCodeTab() {
  const doRedeem = async () => {
    const key = document.getElementById("code-input").value.trim();
    if (!key) return toast("请输入兑换码", "error");
    const btn = document.getElementById("btn-redeem");
    setLoading(btn, true, "兑换中…");
    try {
      const r = await api("/api/user/topup", { method: "POST", body: { key } });
      toast(r.message, "success");
      await refreshUser();
      renderTopup();
    } catch (err) {
      toast(err.message, "error");
      setLoading(btn, false);
    }
  };
  document.getElementById("btn-redeem").onclick = doRedeem;
  document.getElementById("code-input").onkeydown = e => { if (e.key === "Enter") doRedeem(); };
}

/* ================= API Key 管理 ================= */
async function renderKeys() {
  renderLayout("/keys", `
    <div class="page-title">API Key 管理</div>
    <div class="page-sub">调用接口时通过 <span class="mono">Authorization: Bearer sk-xxx</span> 传入</div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
        <div class="card-title" style="margin-bottom:0">密钥列表</div>
        <button class="btn btn-primary btn-sm" id="btn-new-key">+ 创建 Key</button>
      </div>
      <div id="keys-table">
        <div class="skeleton sk-line"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div>
      </div>
    </div>`);

  document.getElementById("btn-new-key").onclick = openCreateKeyModal;

  let list = [];
  try {
    const r = await api("/api/token");
    list = r.data || [];
  } catch (err) {
    toast(err.message, "error");
  }
  const box = document.getElementById("keys-table");
  if (!box) return;
  box.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>名称</th><th>密钥</th><th>状态</th><th>创建时间</th><th style="text-align:right">操作</th></tr></thead>
        <tbody>
          ${list.length === 0 ? `<tr><td colspan="5" class="empty-row">还没有 Key，点击右上角创建一个</td></tr>` :
            list.map(t => `<tr>
              <td><b>${esc(t.name)}</b></td>
              <td class="key-cell" id="kc-${t.id}">${esc(fmtMaskedKey(t.key))}</td>
              <td>${t.status === 1 ? '<span class="badge badge-green">启用</span>' : '<span class="badge badge-gray">禁用</span>'}</td>
              <td class="muted">${fmtTime(t.created_time)}</td>
              <td style="text-align:right">
                <button class="icon-btn" data-act="reveal" data-id="${t.id}" title="查看明文">${ICONS.eye}</button>
                <button class="icon-btn" data-act="copy" data-id="${t.id}" title="复制明文">${ICONS.copy}</button>
                <button class="icon-btn" data-act="del" data-id="${t.id}" data-name="${esc(t.name)}" title="删除" style="color:var(--danger)">${ICONS.trash}</button>
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  const plainCache = {};
  async function getPlain(id) {
    if (plainCache[id]) return plainCache[id];
    const r = await api(`/api/token/${id}/key`, { method: "POST" });
    const key = r.data.key.startsWith("sk-") ? r.data.key : "sk-" + r.data.key;
    plainCache[id] = key;
    return key;
  }

  box.querySelectorAll(".icon-btn").forEach(btn => {
    const id = Number(btn.dataset.id);
    const act = btn.dataset.act;
    btn.onclick = async () => {
      try {
        if (act === "reveal") {
          const cell = document.getElementById(`kc-${id}`);
          if (cell.dataset.shown === "1") {
            const t = list.find(x => x.id === id);
            cell.textContent = fmtMaskedKey(t.key);
            cell.dataset.shown = "0";
          } else {
            cell.innerHTML = '<span class="spin dark"></span>';
            const key = await getPlain(id);
            cell.textContent = key;
            cell.dataset.shown = "1";
          }
        } else if (act === "copy") {
          const key = await getPlain(id);
          await copyText(key);
        } else if (act === "del") {
          confirmDeleteKey(id, btn.dataset.name);
        }
      } catch (err) {
        toast(err.message, "error");
      }
    };
  });

  function confirmDeleteKey(id, name) {
    openModal(`
      <h3>删除 Key</h3>
      <p>确定删除「<b>${esc(name)}</b>」？使用该 Key 的调用将立即失效，此操作不可恢复。</p>
      <div class="modal-actions">
        <button class="btn btn-outline" id="dk-cancel">取消</button>
        <button class="btn btn-primary" id="dk-ok" style="background:var(--danger)">确认删除</button>
      </div>`);
    document.getElementById("dk-cancel").onclick = closeModal;
    document.getElementById("dk-ok").onclick = async () => {
      const okBtn = document.getElementById("dk-ok");
      setLoading(okBtn, true, "删除中…");
      try {
        await api(`/api/token/${id}`, { method: "DELETE" });
        closeModal();
        toast("已删除", "success");
        renderKeys();
      } catch (err) {
        toast(err.message, "error");
        setLoading(okBtn, false);
      }
    };
  }
}

function fmtMaskedKey(key) {
  // 真实模式返回形如 "tTPn**********6JJ4"；mock 返回完整 sk-，统一展示
  if (key.includes("*")) return "sk-" + key;
  return key.slice(0, 7) + "****************" + key.slice(-4);
}

function openCreateKeyModal() {
  openModal(`
    <h3>创建 API Key</h3>
    <div class="field">
      <label>Key 名称</label>
      <input id="new-key-name" placeholder="例如：生产环境、测试用" maxlength="30">
    </div>
    <div class="modal-actions">
      <button class="btn btn-outline" id="mk-cancel">取消</button>
      <button class="btn btn-primary" id="mk-ok">创建</button>
    </div>`);
  document.getElementById("new-key-name").focus();
  document.getElementById("mk-cancel").onclick = closeModal;
  document.getElementById("mk-ok").onclick = async () => {
    const name = document.getElementById("new-key-name").value.trim() || "未命名 Key";
    const okBtn = document.getElementById("mk-ok");
    setLoading(okBtn, true, "创建中…");
    try {
      const r = await api("/api/token", { method: "POST", body: { name } });
      const t = r.data;
      const plain = t && t.key ? (t.key.startsWith("sk-") ? t.key : "sk-" + t.key) : "";
      if (plain) {
        openModal(`
          <h3>Key 创建成功</h3>
          <p class="muted">请立即复制保存，关闭后可在列表中通过「查看明文」再次获取</p>
          <div class="key-reveal">${esc(plain)}</div>
          <div class="modal-actions">
            <button class="btn btn-outline" id="nk-close">关闭</button>
            <button class="btn btn-primary" id="nk-copy">复制 Key</button>
          </div>`);
        document.getElementById("nk-close").onclick = () => { closeModal(); renderKeys(); };
        document.getElementById("nk-copy").onclick = async () => { await copyText(plain); };
      } else {
        closeModal();
        toast("创建成功", "success");
        renderKeys();
      }
    } catch (err) {
      toast(err.message, "error");
      setLoading(okBtn, false);
    }
  };
}

/* ================= 调用日志 ================= */
let logPage = 1;

async function renderLogs() {
  renderLayout("/logs", `
    <div class="page-title">调用日志</div>
    <div class="page-sub">最近的调用与充值明细，消耗以 ${PROMO.unit} 计</div>
    <div id="logs-body">
      <div class="grid grid-4">
        ${[1,2,3,4].map(() => '<div class="card stat-card"><div class="skeleton sk-line" style="width:60%"></div><div class="skeleton sk-line" style="height:22px"></div></div>').join("")}
      </div>
      <div class="card" style="margin-top:18px"><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line"></div></div>
    </div>`);

  const pageSize = 10;
  let d;
  try {
    const r = await api(`/api/log/self?p=${logPage}&page_size=${pageSize}`);
    d = r.data;
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  const { items, total, stat } = d;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const body = document.getElementById("logs-body");
  if (!body) return;

  body.innerHTML = `
    <div class="grid grid-4">
      <div class="card stat-card">
        <div class="stat-label">记录总数</div>
        <div class="stat-value">${fmtNum(stat.request_count ?? total)}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">累计消耗${PROMO.unit}</div>
        <div class="stat-value">${fmtPoints(stat.points)}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">本页输入 Tokens</div>
        <div class="stat-value">${fmtNum(stat.prompt_tokens)}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">本页输出 Tokens</div>
        <div class="stat-value">${fmtNum(stat.completion_tokens)}</div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <div class="card-title">明细记录</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>类型</th><th>模型 / 说明</th><th>Key</th><th>输入</th><th>输出</th><th>${PROMO.unit}</th></tr></thead>
          <tbody>
            ${items.length === 0 ? `<tr><td colspan="7" class="empty-row">暂无记录</td></tr>` :
              items.map(l => logRowHtml(l)).join("")}
          </tbody>
        </table>
      </div>
      <div class="pager">
        <span>共 ${total} 条 · 第 ${logPage}/${totalPages} 页</span>
        <button class="btn btn-outline btn-sm" id="pg-prev" ${logPage <= 1 ? "disabled" : ""}>上一页</button>
        <button class="btn btn-outline btn-sm" id="pg-next" ${logPage >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
    </div>`;

  document.getElementById("pg-prev").onclick = () => { if (logPage > 1) { logPage--; renderLogs(); } };
  document.getElementById("pg-next").onclick = () => { if (logPage < totalPages) { logPage++; renderLogs(); } };
}

/* ================= 使用教程 ================= */
/* ================= 站点配置（管理员） ================= */
let ADMIN = { items: [], groups: [], dirty: {} };

function adminField(it) {
  const id = `cfg-${it.key}`;
  const val = it.value;
  if (it.type === "bool") {
    return `<label class="cfg-switch">
      <input type="checkbox" id="${id}" data-key="${it.key}" ${val ? "checked" : ""}>
      <span>${val ? "已开启" : "已关闭"}</span></label>`;
  }
  const shown = Array.isArray(val) ? val.join(", ") : String(val);
  const type = (it.type === "int" || it.type === "float") ? "number" : "text";
  const step = it.type === "float" ? ' step="0.01"' : "";
  return `<input class="cfg-input" type="${type}"${step} id="${id}"
    data-key="${it.key}" value="${esc(shown)}">`;
}

async function renderAdmin() {
  renderLayout("/admin", `
    <div class="page-title">站点配置</div>
    <div class="page-sub">改动即时生效，无需重启服务</div>
    <div class="card"><div class="skeleton sk-block"></div></div>`);
  let r;
  try {
    r = await api("/api/admin/settings");
  } catch (e) {
    document.querySelector(".main .card").innerHTML =
      `<div class="empty">${esc(e.message || "无权访问")}</div>`;
    return;
  }
  ADMIN = { ...r.data, dirty: {} };

  const byGroup = g => ADMIN.items.filter(i => i.group === g);
  document.querySelector(".main").innerHTML = `
    <div class="page-title">站点配置</div>
    <div class="page-sub">改动即时生效，无需重启服务。点「恢复默认」即回落到环境变量</div>
    ${ADMIN.groups.map(g => `
      <div class="card" style="margin-bottom:18px">
        <div class="card-title">${esc(g.label)}</div>
        <div class="cfg-list">
          ${byGroup(g.key).map(it => `
            <div class="cfg-row">
              <div class="cfg-meta">
                <div class="cfg-label">${esc(it.label)}
                  ${it.overridden ? '<span class="cfg-tag">已自定义</span>' : ""}</div>
                ${it.hint ? `<div class="cfg-hint">${esc(it.hint)}</div>` : ""}
              </div>
              <div class="cfg-ctl">${adminField(it)}
                <button class="cfg-reset" data-reset="${it.key}"
                  ${it.overridden ? "" : "disabled"}>恢复默认</button></div>
            </div>`).join("")}
        </div>
      </div>`).join("")}
    <div class="cfg-bar">
      <span class="muted" id="cfg-count">未修改</span>
      <button class="btn primary" id="cfg-save" disabled>保存修改</button>
    </div>`;

  const spec = k => ADMIN.items.find(i => i.key === k);
  const countEl = document.getElementById("cfg-count");
  const saveBtn = document.getElementById("cfg-save");
  const sync = () => {
    const n = Object.keys(ADMIN.dirty).length;
    countEl.textContent = n ? `${n} 项待保存` : "未修改";
    saveBtn.disabled = n === 0;
  };

  document.querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key;
    const it = spec(key);
    const orig = Array.isArray(it.value) ? it.value.join(", ") : String(it.value);
    const handler = () => {
      if (it.type === "bool") {
        el.nextElementSibling.textContent = el.checked ? "已开启" : "已关闭";
        if (el.checked === it.value) delete ADMIN.dirty[key];
        else ADMIN.dirty[key] = el.checked;
      } else if (el.value.trim() === orig.trim()) {
        delete ADMIN.dirty[key];
      } else {
        ADMIN.dirty[key] = el.value.trim();
      }
      sync();
    };
    el.addEventListener(it.type === "bool" ? "change" : "input", handler);
  });

  saveBtn.onclick = async () => {
    // POINTS_PER_CNY 改动会立刻改变所有用户看到的余额数字，单独二次确认
    if ("POINTS_PER_CNY" in ADMIN.dirty &&
        !confirm(`积分汇率将从 ${spec("POINTS_PER_CNY").value} 改为 ${ADMIN.dirty.POINTS_PER_CNY}。\n` +
                 "所有用户的余额显示会立即变化，确认继续？")) return;
    saveBtn.disabled = true;
    try {
      const res = await api("/api/admin/settings",
        { method: "PUT", body: { values: ADMIN.dirty } });
      toast(res.message || "已保存", "success");
      await reloadSiteConfig();
      renderAdmin();
    } catch (e) {
      toast(e.message || "保存失败", "error");
      saveBtn.disabled = false;
    }
  };

  document.querySelectorAll("[data-reset]").forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api("/api/admin/settings/reset",
          { method: "POST", body: { keys: [btn.dataset.reset] } });
        toast("已恢复默认", "success");
        await reloadSiteConfig();
        renderAdmin();
      } catch (e) {
        toast(e.message || "重置失败", "error");
        btn.disabled = false;
      }
    };
  });
}

/* 配置改完后刷新前端缓存的站点信息，否则品牌名/单位名要等下次刷新才变 */
async function reloadSiteConfig() {
  await Promise.all([loadPromo(), loadSite()]);
  applyBrand();
}

/* ================= 启动 ================= */
router();
