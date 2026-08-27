# DocsTemplate —— 可复用的「配置教程」模板

一套**自包含、数据驱动**的教程页模板：拷贝 3 个文件到任何项目，改 1 个 JSON 即完成换肤换内容。已在 newapi-bff（NexusAPI）落地，本目录是它的打包副本与使用说明。

## 文件清单

| 文件 | 作用 | 需要改吗 |
|---|---|---|
| `static/docs-template.js` | 渲染逻辑（`window.DocsTemplate`，零依赖） | ❌ 拷走即用 |
| `static/docs-template.css` | 模板样式（全部跟随 `var(--primary)` / `var(--surface)` / `var(--border)`） | ❌ 拷走即用，颜色自动跟项目 |
| `static/docs-catalog.json` | **内容**（页面标题 / 各系统 Tab / 视频 / 步骤） | ✅ 新项目只改这个 |
| `README.md` | 本说明 | — |

## 快速接入（原生 JS SPA）

1. 把 `docs-template.js` / `docs-template.css` / `docs-catalog.json` 拷到新项目 `static/`。
2. `index.html` 引入（在 app.js 之前）：
   ```html
   <link rel="stylesheet" href="/static/docs-template.css">
   <script src="/static/docs-template.js"></script>
   ```
3. 路由渲染时调用：
   ```js
   const docs = await fetch("/api/config").then(r => r.json()); // 或任意来源
   const html = DocsTemplate.render(docs, {
     apiBase: "...",        // 替换 ${APIBASE}
     defaultModel: "...",   // 替换 ${MODEL}
     pointsPerCny: 10000,   // 替换 ${POINTS}
     models: [...],         // model-panel block 用
   });
   container.innerHTML = html;
   DocsTemplate.bind(container);   // 绑定 tab 切换 / 复制 / 折叠
   ```
4. 换内容：编辑 `docs-catalog.json`，保存刷新即可（后端需下发该 JSON，见下文「后端下发」）。

> 渲染输出结构：`<div class="page-title">` + `<div class="page-sub">` + 若干 `<section class="doc-section">`。页面外壳（控制台侧边栏/顶栏）由宿主自己渲染，模板不负责。

## 内容结构（docs-catalog.json）

```jsonc
{
  "page_title": "配置教程",            // 页面大标题
  "page_sub": "按你的系统选择，一键完成客户端配置",  // 副标题（可省略）
  "sections": [                        // 板块列表（一般 1 个）
    {
      "id": "client-setup",
      "title": "客户端配置教程",
      "group": "入门",
      "show_title": false,             // true 则显示板块标题（默认不显示）
      "blocks": [
        {
          "type": "tab-tutorial",      // 核心 block 类型
          "collapsible": true,         // 显示「收起图文步骤 ▲」
          "tabs": [
            {
              "label": "macOS 一键配置",   // Tab 文案
              "active": true,              // 默认选中（第一个 active 生效）
              "video": {                   // 视频区
                "title": "macOS 教程",     // 无 src 时的黑底大字标题
                "src": "/static/videos/mac-setup.mp4",  // 可选：真实视频文件（填了走 <video> 原生播放器，懒加载）
                "poster": "/static/images/poster.jpg",  // 可选：视频封面图（真实视频与模拟器都可用）
                "current": "0:00",         // 仅 CSS 模拟器显示
                "duration": "0:38",
                "progress": 0              // 进度条百分比（仅 CSS 模拟器）
              },
              "steps": [                   // 步骤卡列表
                {
                  "no": "1",
                  "title": "运行配置命令",
                  "desc": "打开「终端」应用程序，粘贴并回车：",
                  "code": "curl -fsSL ... | bash",  // 深色代码块 + 复制
                  "lang": "bash"
                },
                {
                  "no": "2",
                  "title": "开始使用",
                  "desc": "打开客户端软件……",
                  "image": {                          // 截图或占位
                    "src": "",                        // 可选：真实截图 URL
                    "placeholder": "客户端界面预览 · 选择模型下拉"
                  }
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

### step 的三种内容（互斥三选一）
- **`code`**：深色代码块 + 橙色/主题色「复制」按钮。`lang` 显示语言标签。
- **`key`**：白色输入框 + 「复制」按钮。`key_masked: true` 显示圆点脱敏；`key_hint` 显示说明文字。
- **`image`**：`src` 为真实截图 URL；无 src 时用 `placeholder` 显示占位块。

### 占位符
`code` / `desc` / `key_hint` 支持 `${APIBASE}` / `${MODEL}` / `${POINTS}`，渲染时替换为实际值。

## 主题色

模板不写死品牌色，全部使用：
- 主色/强调：`var(--primary)`（跟随宿主项目，newapi-bff 是 `#3b5bfd` 蓝）
- 表面/边框/文字：`var(--surface)` / `var(--surface-2)` / `var(--border)` / `var(--text)` 系列
- 若宿主没有这些变量，在 `:root` 里补一个 `--primary: #3b5bfd;` 即可。

## 后端下发（FastAPI 示例）

`/api/config` 直接返回 `docs-catalog.json` 的内容：

```python
import json, os
_DOCS = None
def _load_docs():
    global _DOCS
    if _DOCS is None:
        fp = os.path.join(os.path.dirname(__file__), "..", "static", "docs-catalog.json")
        with open(fp, "r", encoding="utf-8") as f:
            _DOCS = json.load(f)
    return _DOCS

@app.get("/api/config")
def config():
    return {"data": {"docs": _load_docs(), ...}}
```

支持环境变量覆盖（优先级从高到低）：
1. `BFF_DOCS_CATALOG`（inline JSON）
2. `BFF_DOCS_CATALOG_FILE`（指定文件）
3. `static/docs-catalog.json`（默认）
4. 内置兜底（代码内 DEFAULT）

## 支持的所有 block 类型（模板内置，可按需使用）

| type | 说明 |
|---|---|
| `tab-tutorial` | 多 Tab + 视频 + 步骤卡（本文主角） |
| `steps` | 有序步骤列表（`items:[{no,title,html}]`） |
| `callout` | 提示框（`tone: "info"\|"warn"`） |
| `text` | 富文本段落 |
| `code-tabs` | 多语言代码 Tab（`tabs:[{lang,label,code}]`） |
| `model-panel` | 模型列表面板（数据来自 `vars.models`） |
| `error-table` | 错误码表（`rows:[{code,meaning,fix}]`） |
| `faq` | 问答（`items:[{q,a}]`） |

## 注意事项

- `DocsTemplate` 是全局对象（`window.DocsTemplate`），零外部依赖；`esc/copyText` 内置实现，不会和宿主冲突。
- Tab 切换、复制、折叠交互统一由 `DocsTemplate.bind(root)` 绑定，渲染后必须调用一次。
- 模板 CSS 已从 newapi-bff 的 `style.css` 中抽出（原 `.doc-*` / `.tt-*` / `.mp-*` 块），宿主不要重复引入这些类。
