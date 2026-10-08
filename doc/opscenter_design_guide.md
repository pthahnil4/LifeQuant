# 个人量化运维监控中心 · 设计与实现指南

> 版本 v0.1.0 · 基于 `opscenter_demo/` 演示站整理
> 目标读者：未来开发正式站点、或搭建其他类似"运维/监控/管理面板"的开发者
> 阅读方式：本文按"从 0 到 1"的构建顺序组织，代码片段均对应仓库真实文件路径，可边读边对照复现。

---

## 目录

1. [项目概述与定位](#1-项目概述与定位)
2. [技术选型与依赖](#2-技术选型与依赖)
3. [目录结构设计规范](#3-目录结构设计规范)
4. [视觉设计规范（Design Tokens）](#4-视觉设计规范design-tokens)
5. [核心组件实现逻辑](#5-核心组件实现逻辑)
6. [部署与运行指南](#6-部署与运行指南)
7. [扩展路线图](#7-扩展路线图)
8. [附录：复刻清单与常见问题](#8-附录复刻清单与常见问题)

---

## 1. 项目概述与定位

### 1.1 这是什么

一个**独立运行的个人量化运维与监控中心**。它把散落在交易系统各处的运行状态——进程存活、内存水位、重启归因、调度任务、告警——聚合到一个**规范、大气、现代化**的可视化面板里，让维护者"一屏看清系统现在好不好"。

当前仓库里的 `opscenter_demo/` 是它的**视觉原型（Demo）**：纯静态样例数据，用来确立设计语言与布局规范，供评估后再推进为接入真实数据的正式站。

### 1.2 架构目标（三条定位）

| 定位 | 含义 | 落地方式 |
|------|------|---------|
| **独立于交易核心** | 与交易系统（`crypto/`）物理隔离，是同级运行的另一个 Web 应用 | 独立入口 `dashboard_demo.py`、独立端口 8889、独立模板/静态目录 |
| **纯只读展示** | 只"看"不"改"，绝不下单、不改配置、不重启交易进程 | 演示阶段零后端数据；正式阶段也只读现有系统产物（文件/DB/HTTP） |
| **可视化运维面板** | 把日志、状态文件、指标转成图表与卡片 | KPI 磁贴、SVG 趋势图、健康评分环、时间线、状态表 |

### 1.3 设计原则（贯穿全文的"为什么"）

1. **保交易不动如山**：这是最高红线。运维中心崩了、重启了、改样式了，都不能影响实盘交易进程。因此**绝不 `import crypto.app`**——该模块在导入阶段就会拉起调度器、内存看门狗、系统监控线程（`crypto/SMOKE_TESTS.md` 第 163 行有同款告诫）。
2. **统一设计语言，摒弃零散感**：所有视觉决策集中到"设计令牌"（CSS Variables），组件用 BEM 命名，页面用 Jinja2 模板继承。改一处即改全站。
3. **轻量、无构建、可长期维护**：不引入 Node 工具链、不引前端框架、不引图表库。纯 Flask + 原生 CSS/JS + 手绘 SVG。两年后打开仍能直接跑，不会"build 不起来"。
4. **安全默认值（fail-safe）**：默认只绑 `127.0.0.1`，与项目现有 `crypto/web_auth.py` 的安全口径一致；要远程访问必须显式设环境变量。

### 1.4 与现有系统的关系图

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  交易系统 crypto/ (端口6001) │        │  运维中心 opscenter/ (8889)   │
│  - 调度器 / 实盘下单         │  只读  │  - 独立 Flask 应用            │
│  - 内存看门狗 / 系统监控     │ ─────► │  - 采集现有系统产物           │
│  - 写 logs/*.jsonl / DB      │  产物  │  - 可视化展示                 │
└─────────────────────────────┘        └──────────────────────────────┘
        绝不被 import、绝不被重启            崩了也不影响左边
```

---

## 2. 技术选型与依赖

### 2.1 选型总览

| 层 | 选型 | 版本/来源 | 为什么 |
|----|------|----------|--------|
| 后端 | **Flask** | 复用项目现有依赖（`requirements.txt` 已含 Flask>=2.0） | 与现有系统兼容，零新增依赖，维护者已熟悉 |
| 模板 | **Jinja2** | Flask 内置 | 模板继承 + 宏，是"统一布局"的载体 |
| 样式 | **原生 CSS + CSS Variables** | 无框架 | 设计令牌驱动；不引 Tailwind 避免构建依赖 |
| 布局 | **Flexbox + CSS Grid** | 浏览器原生 | 外壳用 Grid，组件内部用 Flex，响应式天然 |
| 交互 | **Vanilla JS（ES5 风格 IIFE）** | 无框架 | 演示阶段交互极少；不引 React/Vue 避免工具链腐烂 |
| 图表 | **手绘 SVG** | 无库 | 演示阶段零依赖；正式阶段可平滑换 ECharts（项目已有 `crypto/static/js/echarts.min.js`） |

### 2.2 为什么不上现代前端框架（React/Vue + Vite）

对个人单人维护、要求"长期稳定运行"的运维面板，重型 SPA 是**负债**而非资产：

- **构建链腐烂**：`node_modules` 版本漂移，一两年后可能因 Node/依赖升级而 build 失败。
- **双份部署与调试**：前后端两套体系，个人项目性价比低。
- **技能栈割裂**：整个项目都是 Flask + Jinja2 + 原生 JS，切换成本高。

> 升级空间：若未来确实需要更强响应式，可平滑引入 **Alpine.js / petite-vue（CDN，无构建）** 或 **HTMX（服务端驱动局部刷新）**，本架构的令牌化 CSS 与模板继承体系完全兼容，不需推倒重来。

### 2.3 依赖清单

演示站**零新增第三方依赖**，只用到 Flask 标准能力：

```python
# dashboard_demo.py 顶部唯一的第三方导入
from flask import Flask, render_template
```

标准库：`os`、`sys`。运行环境：Python 3.13（与项目一致）。

---

## 3. 目录结构设计规范

### 3.1 物理隔离结构

演示站在**项目根目录**新增两项，与 `crypto/` 完全平级、互不嵌套：

```
cryptoTrade/                       # 项目根
├── app.py                         # 【现有】交易系统入口(不动)
├── crypto/                        # 【现有】交易系统(零触碰)
│   ├── app.py                     #   ⚠️ 导入即拉起后台线程，运维站禁止 import
│   ├── templates/  static/  ...
│
├── dashboard_demo.py              # 【新增】运维中心独立入口(端口8889)
└── opscenter_demo/                # 【新增】运维中心独立应用包
    ├── templates/                 #   模板层(Jinja2)
    │   ├── base.html              #     应用外壳:侧栏+顶栏+内容区+页脚
    │   └── dashboard.html         #     系统健康概览页(继承 base)
    └── static/                    #   静态资源层
        ├── css/
        │   ├── tokens.css         #     ① 设计令牌(视觉唯一来源)
        │   ├── layout.css         #     ② 布局层(重置+外壳+栅格+响应式)
        │   └── components.css     #     ③ 组件层(卡片/磁贴/徽章/表格/图表)
        └── js/
            └── app.js             #     前端交互(主题/侧栏/动效)
```

### 3.2 三层 CSS 的职责划分（关键规范）

样式刻意拆成三个文件，**加载顺序即依赖顺序**（见 `base.html` `<head>`）：

| 文件 | 职责 | 铁律 |
|------|------|------|
| `tokens.css` | 定义所有 CSS 变量（颜色/间距/字体/圆角/阴影/层级/动效） | **只有变量定义，不写任何具体样式规则** |
| `layout.css` | 重置 + 应用外壳（sidebar/topbar/content/footer）+ 栅格工具类 + 响应式断点 | 只引用令牌，不硬编码视觉值 |
| `components.css` | 可复用组件（按钮/卡片/磁贴/徽章/进度条/图表/表格/时间线/列表/骨架屏） | BEM 命名，只引用令牌 |

> 这套"令牌 → 布局 → 组件"的分层，正是"建立统一设计语言、摒弃零散感"的工程化落地：任何新页面只需组合已有组件类，视觉自动统一。

### 3.3 Flask 如何指向独立目录（不冲突的核心）

`dashboard_demo.py` 通过构造参数把模板/静态目录**显式指向 `opscenter_demo/`**，因此与 `crypto/templates`、`crypto/static` 井水不犯河水：

```python
# dashboard_demo.py
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(_HERE, 'opscenter_demo')

app = Flask(
    __name__,
    template_folder=os.path.join(_PKG, 'templates'),   # 指向 opscenter_demo/templates
    static_folder=os.path.join(_PKG, 'static'),        # 指向 opscenter_demo/static
)
```

**为什么不冲突**：

1. **进程隔离**：运维站是独立进程、独立端口（8889），交易站在 6001，两者不共享 Flask 实例。
2. **文件隔离**：运维站所有文件都在 `opscenter_demo/` 与 `dashboard_demo.py`，不写入 `crypto/` 任何路径。
3. **导入隔离**：运维站不 `import crypto`，只依赖 Flask 标准库。回滚 = 删这两项，交易系统毫发无损。
4. **静态资源端点**：因为设了 `static_folder`，模板里的 `url_for('static', filename='css/tokens.css')` 自动解析到 `opscenter_demo/static/`，不会误取 `crypto/static/`。

### 3.4 命名规范约定

- **目录/文件**：小写下划线（`opscenter_demo`、`dashboard_demo.py`），与项目现有风格一致。
- **CSS 类**：BEM —— `block__element--modifier`，如 `.tile__icon--success`、`.nav-item.is-active`。
- **模板**：`base.html` 为唯一外壳，功能页 `{% extends "base.html" %}`。
- **正式站演进**：Demo 用扁平 `opscenter_demo/`；正式站建议升级为带 `collectors/`（采集层）、`blueprints/`（路由层）的包结构（见第 7 章）。

---

## 4. 视觉设计规范（Design Tokens）

> 对应文件：`opscenter_demo/static/css/tokens.css`
> 一句话理解：**把"设计决策"变成"变量"**。颜色、间距、圆角、阴影不再散落在各处硬编码，而是集中定义一次，全站引用。这是"规范大气、视觉统一"的技术根基。

### 4.1 什么是设计令牌，为什么用它

传统做法：每个组件里写 `color: #2f81f7; padding: 16px; border-radius: 10px;`——改一次主题要全局搜索替换，极易漏改，久了就"零散"。

令牌做法：先定义 `--brand-500: #2f81f7; --sp-4: 16px; --radius-md: 10px;`，组件里只写 `color: var(--brand-500)`。**改一处即改全站**，且天然支持多主题（明/暗）。

### 4.2 令牌的六大类（tokens.css 结构）

`tokens.css` 的 `:root` 块把所有令牌按语义分组：

```css
/* opscenter_demo/static/css/tokens.css */
:root {
  /* ① 品牌强调色：单一主色，克制使用 */
  --brand-500: #2f81f7;
  --brand-400: #58a6ff;
  --brand-600: #1f6feb;
  --brand-glow: rgba(47, 129, 247, .35);

  /* ② 语义状态色：成功/警告/危险/信息/中性，各配一个 soft 半透明底 */
  --success: #3fb950;   --success-soft: rgba(63, 185, 80, .14);
  --warning: #d29922;   --warning-soft: rgba(210, 153, 34, .14);
  --danger:  #f85149;   --danger-soft:  rgba(248, 81, 73, .14);
  --info:    #58a6ff;   --info-soft:    rgba(88, 166, 255, .14);
  --neutral: #8b98a5;   --neutral-soft: rgba(139, 152, 165, .14);

  /* ③ 间距标度：4/8 基准，杜绝魔法数字 */
  --sp-1: 4px;  --sp-2: 8px;   --sp-3: 12px;  --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px;  --sp-8: 32px;  --sp-10: 40px;  --sp-12: 48px;

  /* ④ 字体：字族 + 字号阶 + 字重 + 行高 */
  --font-sans: "Inter", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
  --fs-xs: 12px; --fs-sm: 13px; --fs-md: 14px; --fs-lg: 16px;
  --fs-xl: 20px; --fs-2xl: 26px; --fs-3xl: 34px;
  --fw-regular: 400; --fw-medium: 500; --fw-semibold: 600; --fw-bold: 700;

  /* ⑤ 圆角 + 阴影 + 层级(z-index) + 布局尺寸 */
  --radius-sm: 6px; --radius-md: 10px; --radius-lg: 14px;
  --radius-xl: 20px; --radius-pill: 999px;
  --shadow-1: 0 1px 2px rgba(0,0,0,.24);
  --shadow-2: 0 4px 14px rgba(0,0,0,.28);
  --shadow-3: 0 12px 34px rgba(0,0,0,.40);
  --z-sidebar: 40; --z-topbar: 30; --z-dropdown: 60; --z-toast: 80;
  --sidebar-w: 248px; --topbar-h: 60px; --content-max: 1440px;

  /* ⑥ 动效：统一缓动曲线与时长 */
  --ease: cubic-bezier(.4, 0, .2, 1);
  --dur-fast: .14s; --dur: .22s;
}
```

**设计要点解读：**

- **间距用 `--sp-N` 阶**：`--sp-4=16px` 意味着"任何 16px 的间距都写 `var(--sp-4)`"。全站间距只有 4/8/12/16/20/24/32… 这几档，视觉上自然对齐、有节奏。
- **状态色成对出现**：每个语义色都配一个 `*-soft`（14% 透明底），用于徽章/图标的浅色背景，如 `background: var(--success-soft); color: var(--success)`。这是"大气"的关键——不用高饱和大色块，而用"浅底 + 深字"。
- **`--content-max: 1440px`**：内容区最大宽度，超宽屏居中留白，避免文字行长过宽伤阅读。

### 4.3 主题切换：令牌的第二重威力

同一套结构令牌（间距/字体/圆角）不变，只切换"颜色令牌"即可换肤。`tokens.css` 用属性选择器定义两套颜色：

```css
/* 深色（默认）——专业运维台基调 */
:root,
[data-theme="dark"] {
  --bg: #0b0f14;        --surface: #131a23;    --surface-2: #18212c;
  --border: #232e3b;    --text: #e6edf3;       --text-dim: #9aa7b4;
  --text-mute: #6b7885; --sidebar-bg: #0e141c;
  --topbar-bg: rgba(13,18,25,.82);
  --shadow-card: 0 1px 3px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.18);
  --grid-line: rgba(255,255,255,.04);
}

/* 浅色——令牌驱动，一键切换 */
[data-theme="light"] {
  --bg: #f4f6f9;        --surface: #ffffff;    --surface-2: #f7f9fc;
  --border: #e2e8f0;    --text: #1a222c;       --text-dim: #566373;
  --text-mute: #8492a1; --sidebar-bg: #ffffff;
  --topbar-bg: rgba(255,255,255,.82);
  --shadow-card: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.06);
  --grid-line: rgba(0,0,0,.04);
}
```

**切换机制**：`<html data-theme="dark">` 是默认；JS 只需把这个属性改成 `light`，全站颜色瞬间联动（详见 §5.3）。**组件代码一行都不用改**——因为它们只认 `var(--text)`、`var(--surface)`，不认具体色值。

> 注意语义状态色（success/warning/danger）在两套主题里基本保持一致，只在浅色主题下微调 `*-soft` 透明度以保证对比度。这符合"状态语义跨主题稳定"的可用性原则。

### 4.4 令牌如何落到组件（统一风格的闭环）

以"卡片"为例，`components.css` 里全程只引用令牌，无一处硬编码：

```css
/* opscenter_demo/static/css/components.css */
.card {
  background: var(--surface);          /* ← 令牌，随主题变 */
  border: 1px solid var(--border);     /* ← 令牌 */
  border-radius: var(--radius-lg);     /* ← 令牌 */
  box-shadow: var(--shadow-card);      /* ← 令牌 */
  display: flex; flex-direction: column;
  transition: border-color var(--dur) var(--ease), transform var(--dur) var(--ease);
}
```

**闭环逻辑**：令牌定义视觉 → 组件消费令牌 → 页面组合组件。任何新页面只要用 `.card`、`.tile`、`.badge` 这些类，风格自动统一，无需重复决策。这就是"摒弃有什么功能加什么功能的零散感"的工程答案。

### 4.5 复刻要点（照做即可）

1. 新建 `tokens.css`，先定义**结构令牌**（间距/字体/圆角/阴影/尺寸/动效），再定义**颜色令牌**。
2. 颜色令牌拆成"主题无关"（品牌色、状态色）与"主题相关"（bg/surface/text/border）两组。
3. 用 `:root, [data-theme="dark"]` 定义默认深色，`[data-theme="light"]` 覆盖浅色。
4. **纪律**：`tokens.css` 里绝不写具体组件样式；`layout.css`/`components.css` 里绝不出现裸色值/裸像素（一律 `var(...)`）。

---

## 5. 核心组件实现逻辑

本章按"外壳 → 数据组件 → 交互微件"三层，讲清每个关键 UI 的实现原理。所有片段对应 `opscenter_demo/` 真实文件。

### 5.1 响应式布局外壳

> 对应：`layout.css`（外壳样式）+ `base.html`（外壳结构）

#### 5.1.1 整体骨架：CSS Grid 两列

应用外壳 `.app` 用 **Grid** 切成"侧栏 + 主区"两列，占满整屏高：

```css
/* layout.css */
.app {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;  /* 左固定 248px，右自适应 */
  grid-template-rows: 100vh;
  min-height: 100vh;
}
```

对应 `base.html` 结构：

```html
<div class="app">
  <aside class="sidebar" id="sidebar"> … 侧栏 … </aside>
  <div class="scrim" id="scrim"></div>          <!-- 移动端遮罩 -->
  <div class="main">                            <!-- 主区 = 顶栏 + 内容 + 页脚 -->
    <header class="topbar"> … </header>
    <main class="content">{% block content %}{% endblock %}</main>
    <footer class="footer"> … </footer>
  </div>
</div>
```

主区 `.main` 内部再用 **Flex 纵向**排列，让内容区 `flex:1` 撑开、页脚沉底：

```css
.main { grid-column: 2; display: flex; flex-direction: column; min-width: 0; }
.content { flex: 1; padding: var(--sp-6); width: 100%; max-width: var(--content-max); margin: 0 auto; }
```

> `min-width: 0` 是 Grid/Flex 嵌套的关键细节：不加它，子元素里的宽表格会撑破列宽导致横向溢出。

#### 5.1.2 侧栏：粘性定位 + 内部滚动

侧栏 `position: sticky; top: 0; height: 100vh`，导航区 `flex:1; overflow-y:auto`——品牌区与底部状态卡固定，中间导航独立滚动：

```css
.sidebar { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; z-index: var(--z-sidebar); }
.sidebar__nav { flex: 1; overflow-y: auto; padding: var(--sp-4) var(--sp-3); }
```

导航按业务域**分组**（态势总览 / 系统健康 / 日志与调度 / 交易运行时 / 运维工具箱）。`base.html` 用 Jinja2 数据结构 + 双层循环渲染，避免手写重复 `<a>`：

```jinja
{% set nav = [
  ('态势总览', [('dashboard', '📊', '总览仪表盘', '#', '')]),
  ('系统健康', [('health','🩺','健康检查','#',''), ('lifecycle','♻️','进程生命周期','#',''), …]),
  …
] %}
{% for group_label, items in nav %}
<div class="nav-group">
  <div class="nav-group__label">{{ group_label }}</div>
  {% for key, icon, label, href, badge in items %}
  <a href="{{ href }}" class="nav-item {% if key == active_page %}is-active{% endif %}">
    <span class="nav-item__icon">{{ icon }}</span><span>{{ label }}</span>
    {% if badge %}<span class="nav-item__badge">{{ badge }}</span>{% endif %}
  </a>
  {% endfor %}
</div>
{% endfor %}
```

当前页高亮靠 `active_page` 变量（由各页面 `{% set active_page = "dashboard" %}` 传入），命中则加 `.is-active`，左侧出现品牌色指示条：

```css
.nav-item.is-active { background: linear-gradient(90deg, var(--info-soft), transparent); color: var(--brand-400); font-weight: var(--fw-semibold); }
.nav-item.is-active::before { content:""; position:absolute; left:-3px; top:50%; transform:translateY(-50%); width:3px; height:60%; background:var(--brand-500); border-radius:0 var(--radius-pill) var(--radius-pill) 0; }
```

#### 5.1.3 顶栏：粘性 + 毛玻璃

```css
.topbar {
  position: sticky; top: 0; z-index: var(--z-topbar);
  display: flex; align-items: center; gap: var(--sp-4);
  height: var(--topbar-h); padding: 0 var(--sp-6);
  background: var(--topbar-bg);
  backdrop-filter: saturate(180%) blur(12px);   /* 毛玻璃：内容滚动时顶栏半透明模糊 */
  border-bottom: 1px solid var(--border);
}
```

顶栏元素从左到右：移动端菜单按钮（桌面隐藏）→ 面包屑 → 搜索框（`margin-left:var(--sp-4); flex:1`）→ 右侧动作区（`margin-left:auto`：健康灯、刷新、主题切换、通知、头像）。`margin-left:auto` 是把动作区推到最右的关键。

#### 5.1.4 内容区 12 列栅格

内容区用一套 12 列栅格工具类做响应式布局：

```css
.grid { display: grid; gap: var(--sp-5); }
.grid--cols-12 { grid-template-columns: repeat(12, 1fr); }
.col-3 { grid-column: span 3; }  .col-4 { grid-column: span 4; }
.col-5 { grid-column: span 5; }  .col-7 { grid-column: span 7; }
.col-8 { grid-column: span 8; }  .col-12 { grid-column: span 12; }
```

页面里用 `<div class="grid grid--cols-12">` 包裹，子项用 `col-N` 声明占几列。例如 Dashboard 的"内存趋势图占 8 列 + 健康评分环占 4 列"：

```html
<div class="grid grid--cols-12 mt-5">
  <div class="col-8"> …内存趋势卡片… </div>
  <div class="col-4"> …健康评分环卡片… </div>
</div>
```

#### 5.1.5 响应式断点（两档降级）

```css
/* 中屏(≤1200px)：窄列自动扩为半宽/整宽 */
@media (max-width: 1200px) {
  .col-3 { grid-column: span 6; }
  .col-5, .col-7, .col-8 { grid-column: span 12; }
}
/* 小屏(≤860px)：侧栏改为抽屉，Grid 塌成单列 */
@media (max-width: 860px) {
  .app { grid-template-columns: 1fr; }
  .sidebar { position: fixed; left:0; top:0; bottom:0; width: var(--sidebar-w);
             transform: translateX(-100%); box-shadow: var(--shadow-3); }
  .sidebar.is-open { transform: translateX(0); }
  .main { grid-column: 1; }
  .topbar__toggle { display: grid; place-items: center; }   /* 显示汉堡按钮 */
  .topbar__search { display: none; }
  .col-3, .col-6 { grid-column: span 12; }
}
```

**降级策略**：桌面三/四列 → 中屏两列 → 手机单列 + 侧栏抽屉化。侧栏默认 `translateX(-100%)` 移出屏外，加 `.is-open` 滑入（详见 §5.3.2）。

### 5.2 卡片式数据展示组件

> 对应：`components.css`（组件样式）+ `dashboard.html`（用法）

#### 5.2.1 KPI 数字卡（`.tile`）

统计磁贴是仪表盘的门面，结构为"标签行 + 大数字 + 脚注 + 迷你 sparkline"，右上角有一团模糊光晕增加层次：

```css
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
        padding: var(--sp-5); box-shadow: var(--shadow-card); position: relative; overflow: hidden;
        transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease); }
.tile--hover:hover { transform: translateY(-3px); border-color: var(--border-strong); }
.tile__glow { position:absolute; top:-40px; right:-30px; width:120px; height:120px; border-radius:50%;
              background: var(--info-soft); filter: blur(28px); opacity:.8; }   /* 光晕 */
.tile__value { font-size: var(--fs-3xl); font-weight: var(--fw-bold); letter-spacing:-1px;
               line-height:1; font-variant-numeric: tabular-nums; }             /* 等宽数字，跳动不抖 */
```

`dashboard.html` 里的用法（含内联 SVG sparkline）：

```html
<div class="tile tile--hover fade-in">
  <div class="tile__glow"></div>
  <div class="tile__top">
    <span class="tile__label">综合健康评分</span>
    <span class="tile__icon tile__icon--success">🛡️</span>
  </div>
  <div class="tile__value">96<small>/100</small></div>
  <div class="tile__foot"><span class="trend trend--up">▲ 2.1</span> 较昨日</div>
  <div class="tile__spark">
    <svg viewBox="0 0 100 28" preserveAspectRatio="none" style="height:28px;width:100%">
      <polyline points="0,20 12,18 … 100,6" fill="none" stroke="var(--success)"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </div>
</div>
```

图标底色用修饰类切换语义色：`.tile__icon--success/warning/danger/info`，各是"浅底 + 深字"（`background: var(--*-soft); color: var(--*)`）。

#### 5.2.2 状态列表行（`.list-row`）

用于"服务健康检查""最近告警"这类列表。三段式 Flex：左图标 + 中主体 + 右状态：

```css
.list-row { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) 0; border-bottom: 1px solid var(--border); }
.list-row:last-child { border-bottom: none; }
.list-row__icon { width:36px; height:36px; border-radius:var(--radius-md); display:grid; place-items:center; flex-shrink:0; }
.list-row__main { flex: 1; min-width: 0; }   /* min-width:0 让长文本能省略号截断 */
.list-row__title { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.list-row__aside { text-align: right; flex-shrink: 0; }
```

`dashboard.html` 用 Jinja2 数据列表批量渲染，图标底色/状态徽章都由 `level` 变量驱动：

```jinja
{% set services = [('🐍','交易主进程','app.py · PID 20201','success','存活','100'), …] %}
{% for icon, name, meta, level, stat, pct in services %}
<div class="list-row">
  <div class="list-row__icon" style="background:var(--{{ level }}-soft);color:var(--{{ level }})">{{ icon }}</div>
  <div class="list-row__main">
    <div class="list-row__title">{{ name }}</div>
    <div class="list-row__sub">{{ meta }}</div>
    <div class="progress" style="height:4px;margin-top:8px">
      <div class="progress__bar progress__bar--{{ level }}" style="width:{{ pct }}%"></div>
    </div>
  </div>
  <div class="list-row__aside"><span class="badge badge--{{ level }}"><span class="badge__dot"></span>{{ stat }}</span></div>
</div>
{% endfor %}
```

> ⚠️ 复刻提示：`style="…var(--{{ level }}-soft)"` 这种"内联样式里嵌 Jinja 变量"的写法，**静态 CSS/HTML 检查器会误报语法错误**（它把 `{{ }}` 当 CSS 解析）。这是**误报**，Jinja2 渲染后得到合法的 `var(--success-soft)`，以实际运行渲染结果为准。

#### 5.2.3 徽章 / 进度条（`.badge` / `.progress`）

最小的状态原子组件，全站复用：

```css
.badge { display:inline-flex; align-items:center; gap:5px; padding:3px 10px; border-radius:var(--radius-pill);
         font-size:var(--fs-xs); font-weight:var(--fw-semibold); }
.badge__dot { width:6px; height:6px; border-radius:50%; background:currentColor; }  /* 圆点继承文字色 */
.badge--success { background: var(--success-soft); color: var(--success); }
/* warning/danger/info/neutral 同理 */

.progress { height:6px; border-radius:var(--radius-pill); background:var(--surface-3); overflow:hidden; }
.progress__bar { height:100%; border-radius:var(--radius-pill); transition: width var(--dur) var(--ease); }
.progress__bar--success { background: var(--success); }  /* 其余语义色同理 */
```

`badge__dot` 用 `background: currentColor` 是个巧思：圆点颜色自动跟随徽章文字色，无需为每种状态单独设点的颜色。

#### 5.2.4 SVG 图表容器（`.chart`）

演示阶段的图表是**纯手绘 SVG，零依赖**。以内存趋势面积图为例，`dashboard.html` 内联一个 `viewBox="0 0 800 260"` 的 SVG，分层绘制：网格线 → 阈值虚线 → 面积 → 折线 → 端点圆 → 坐标标签：

```html
<div class="chart">
  <svg viewBox="0 0 800 260" preserveAspectRatio="none" style="height:260px">
    <defs>
      <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%"   stop-color="var(--brand-400)" stop-opacity=".38"/>
        <stop offset="100%" stop-color="var(--brand-400)" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <line class="chart__grid" x1="0" y1="90" x2="800" y2="90"/>       <!-- 网格 -->
    <line class="chart__thresh" x1="0" y1="70" x2="800" y2="70"/>      <!-- 告警阈值虚线 -->
    <path class="chart__area" d="M0,180 L66,165 … L800,260 L0,260 Z"/> <!-- 面积(渐变填充) -->
    <polyline class="chart__line draw-line" points="0,180 66,165 … 800,80"/> <!-- 折线 -->
    <circle class="chart__dot" cx="800" cy="80" r="4"/>                 <!-- 最新值端点 -->
  </svg>
</div>
```

配套样式（注意渐变色也用令牌 `var(--brand-400)`，随主题联动）：

```css
.chart svg { width:100%; height:auto; display:block; overflow:visible; }
.chart__area { fill: url(#areaGrad); }
.chart__line { fill:none; stroke: var(--brand-400); stroke-width:2.2; stroke-linecap:round; stroke-linejoin:round; }
.chart__grid { stroke: var(--grid-line); stroke-width:1; }
.chart__thresh { stroke: var(--danger); stroke-width:1.4; stroke-dasharray:5 4; opacity:.65; }
/* 折线入场描线动画 */
.draw-line { stroke-dasharray:1400; stroke-dashoffset:1400; animation: draw 1.6s var(--ease) forwards; }
@keyframes draw { to { stroke-dashoffset: 0; } }
```

`draw` 动画原理：把折线的 `stroke-dashoffset` 从"整条虚线长度"过渡到 0，视觉上就是线条从左到右"画出来"。

> 另有**健康评分环**（`.score-ring`）：用两个 `<circle>` 叠加，底层灰色轨道 + 上层用 `stroke-dasharray/stroke-dashoffset` 控制进度的彩色弧，中心绝对定位显示分数。这也是纯 SVG，无需图表库。

> **正式站升级路径**：手绘 SVG 适合 Demo 与简单趋势。接入真实数据后，复杂图表建议换 **ECharts**（项目已有 `crypto/static/js/echarts.min.js`，可拷一份到运维站 static 下，避免跨目录依赖），并用 §4 的令牌值配置 ECharts 主题，保证配色一致。

### 5.3 交互微件

> 对应：`app.js`（全部交互逻辑，IIFE 包裹，无全局污染）

#### 5.3.1 主题切换开关

原理：切换 `<html>` 的 `data-theme` 属性，令牌联动换肤；用 `localStorage` 记忆选择：

```javascript
// app.js
var root = document.documentElement;
var THEME_KEY = 'opscenter_theme';

// ① 载入时恢复上次选择
var saved = null;
try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
syncThemeIcon();

// ② 点击切换
document.getElementById('themeToggle').addEventListener('click', function () {
  var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  syncThemeIcon();
});

// ③ 图标随主题变（🌙/☀️）
function syncThemeIcon() {
  var b = document.getElementById('themeToggle');
  if (b) b.textContent = root.getAttribute('data-theme') === 'light' ? '☀️' : '🌙';
}
```

> `localStorage` 全部包在 `try/catch` 里——浏览器隐私模式下访问 `localStorage` 会抛异常，不兜住会导致整个 IIFE 中断。

#### 5.3.2 移动端侧栏滑出 + 遮罩

小屏下侧栏是抽屉。汉堡按钮切换 `.is-open`（侧栏滑入）与遮罩 `.is-visible`（变暗可点），点遮罩关闭：

```javascript
var sidebar = document.getElementById('sidebar');
var scrim   = document.getElementById('scrim');
var navToggle = document.getElementById('navToggle');

navToggle.addEventListener('click', function (e) {
  e.stopPropagation();
  sidebar.classList.toggle('is-open');
  scrim.classList.toggle('is-visible');
});
scrim.addEventListener('click', function () {
  sidebar.classList.remove('is-open');
  scrim.classList.remove('is-visible');
});
```

遮罩样式（默认透明不可点，`.is-visible` 时变暗可点）：

```css
.scrim { position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:calc(var(--z-sidebar) - 1);
         opacity:0; pointer-events:none; transition:opacity var(--dur); }
.scrim.is-visible { opacity:1; pointer-events:auto; }
```

> `z-index: calc(var(--z-sidebar) - 1)` 让遮罩层级正好压在侧栏之下、内容之上——用令牌算层级，避免硬编码数字打架。

#### 5.3.3 刷新按钮动效

演示阶段"刷新"只做旋转动效、不请求数据（保持纯静态）：

```javascript
var refreshBtn = document.getElementById('refreshBtn');
refreshBtn.addEventListener('click', function () {
  refreshBtn.style.transition = 'transform .6s cubic-bezier(.4,0,.2,1)';
  refreshBtn.style.transform = 'rotate(360deg)';
  setTimeout(function () {                 // 转完后瞬间复位，以便下次再转
    refreshBtn.style.transition = 'none';
    refreshBtn.style.transform = 'none';
  }, 620);
});
```

#### 5.3.4 卡片入场淡入（渐进增强）

`.fade-in` 类做"上浮淡入"动画；配合 `IntersectionObserver` 让卡片滚入视口时才播放。不支持 IO 的浏览器直接全显示（渐进增强，不影响可用性）：

```css
.fade-in { animation: fadeIn .5s var(--ease) both; }
@keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
```

```javascript
if ('IntersectionObserver' in window) {
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) { en.target.style.animationPlayState = 'running'; io.unobserve(en.target); }
    });
  }, { threshold: .08 });
  document.querySelectorAll('.fade-in').forEach(function (el) { io.observe(el); });
}
```

> `dashboard.html` 里还用了 `style="animation-delay:.05s"` 给同排卡片做**错峰入场**，视觉更有层次。

---

## 6. 部署与运行指南

> 对应：`dashboard_demo.py`（根目录入口）

### 6.1 启动命令

在项目根目录 `cryptoTrade/` 下执行：

```powershell
python dashboard_demo.py
```

终端会打印访问信息：

```
============================================================
  🛰️  OpsCenter 运维监控中心 · 独立演示站
============================================================
  访问地址 : http://127.0.0.1:8889
  数据模式 : 纯静态样例（未接入真实接口）
  安全边界 : 不 import crypto、不碰交易进程、默认只绑本机
  停止方式 : Ctrl+C（不影响交易服务）
============================================================
 * Running on http://127.0.0.1:8889
```

浏览器打开 `http://127.0.0.1:8889` 即可。

> **PowerShell 注意**：本项目 shell 不支持 `&&` 连接命令，多条命令用 `;` 分隔，例如 `cd d:\python\cryptoTrade; python dashboard_demo.py`。

### 6.2 端口与监听地址配置

入口用环境变量控制端口和绑定地址，均有安全默认值：

```python
# dashboard_demo.py
DEMO_PORT = int(os.environ.get('OPSDEMO_PORT') or 8889)          # 端口，默认 8889
host = (os.environ.get('OPSDEMO_HOST') or '127.0.0.1').strip()   # 绑定，默认只本机
app.run(debug=True, host=host, port=DEMO_PORT, use_reloader=False)
```

| 场景 | 命令（PowerShell） |
|------|-------------------|
| 默认本机访问 | `python dashboard_demo.py` |
| 换端口（如 9000） | `$env:OPSDEMO_PORT='9000'; python dashboard_demo.py` |
| 允许手机/局域网访问 | `$env:OPSDEMO_HOST='0.0.0.0'; python dashboard_demo.py` |

> **为什么默认只绑 127.0.0.1**：这是 fail-safe 默认值，与项目 `crypto/web_auth.py` 的安全口径一致（"缺配置=关闭远程"而非"缺配置=无防护"）。演示站虽无敏感数据，仍沿用这一纪律。若设 `0.0.0.0` 对外开放，正式站必须像交易系统一样加访问口令闸门。

> **端口选择提醒**：不要用 6000——它被 Chrome/Edge 列入"不安全端口"黑名单，浏览器会直接拒连报 `ERR_UNSAFE_PORT`。8889 合法。

### 6.3 平滑验证（不影响交易服务）

验证运维站时，交易系统在 6001 照常运行，两者互不干扰。推荐验证流程：

**① 渲染冒烟（不起服务，最快）**——用 Flask `test_client` 确认模板能正确渲染、无 Jinja 残留：

```powershell
python -c "import dashboard_demo as d; c=d.app.test_client(); r=c.get('/'); print('STATUS', r.status_code); h=r.get_data(as_text=True); print('jinja_leftover', '{{' in h or '{%' in h)"
```

期望输出：`STATUS 200` 且 `jinja_leftover False`。

**② 起服务 + HTTP 探活**：

```powershell
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8889/', timeout=5).status)"
```

期望输出：`200`。

**③ 确认交易系统零改动**——运维站开发全程不应触碰 `crypto/`：

```powershell
git status --short
```

本次演示站的产物**只应是**两项新增：`?? dashboard_demo.py` 与 `?? opscenter_demo/`。（`git status` 里其它 `M crypto/...` 若存在，是工作区里本任务之前就有的未提交改动，与运维站无关。）

**④ 停止**：在运行 `dashboard_demo.py` 的终端按 `Ctrl+C`。因为它是独立进程，**停止它不涉及交易进程的任何重启**，实盘调度不受影响。

### 6.4 生产部署建议（正式站阶段）

演示站用 Flask 开发服务器（`debug=True`）仅供本地评估。正式站上线时：

- 用生产级 WSGI 服务器（`waitress` / `gunicorn`），`debug=False`。
- 参照交易系统用 **supervisord / 宝塔 Python 项目管理器**托管，配 `autorestart`。
- 加访问口令闸门（可直接复用 `crypto/web_auth.py` 的 fail-safe 设计，用独立口令文件）。
- 若要 HTTPS，证书配在 nginx 反代层，不进 Python 项目。

---

## 7. 扩展路线图

演示站确立了视觉基线。接入真实数据、演进为正式运维中心，按下列阶段推进，**每阶段都带安全闸门**。

### 7.1 从 Demo 到正式站的目录演进

Demo 的扁平结构升级为分层包（新增采集层与路由层，模板/静态沿用）：

```
opscenter/                     # 正式站包（替换 opscenter_demo）
├── __init__.py                #   create_app() 工厂
├── config.py                  #   端口/数据源路径/刷新间隔
├── web_auth.py                #   复用 crypto 闸门设计（独立口令文件）
├── collectors/                # ★采集层：只读现有系统产物
│   ├── health.py              #   进程/内存/磁盘/负载
│   ├── lifecycle.py           #   解析 boot_ledger.jsonl / proc_exit.json
│   ├── memory.py              #   解析 memory_history.jsonl
│   ├── logs.py                #   日志聚合 / tail / 搜索
│   ├── scheduler.py           #   调度状态（读状态文件或 HTTP）
│   └── database.py            #   DB 连通性 / 表新鲜度
├── blueprints/                # ★路由层：按模块拆 Blueprint
│   ├── dashboard.py  health.py  logs.py  scheduler.py …
├── templates/  static/        #   沿用 Demo 的令牌与组件体系
└── docs/
```

### 7.2 数据采集的导入白名单 / 黑名单（安全基石）

正式站要读真实数据，但**必须严守边界**——只导入"叶子级纯读模块"，绝不导入带线程/副作用/交易动作的模块：

| ✅ 可安全复用（纯读、无导入副作用） | ❌ 绝对禁止导入（有线程/副作用/交易动作） |
|-----------------------------------|------------------------------------------|
| `crypto.database.resolve_db_url` / `get_engine`（懒加载，import 不建连接不起线程） | `crypto.app`（导入即拉起调度器+看门狗+监控线程） |
| `crypto.data_paths`（路径解析） | `crypto.task.scheduler`（启动后台线程） |
| 各 `*_repo.py` 的查询函数 | `trend_range_trader` / 订单执行 / 仓位状态机 |
| `api_config` 的**配置字典**（脱敏展示，不创建 Trade API） | `memory_watchdog.start_*` / `system_monitor.start_*` |
| `logs/*.jsonl`、`*.log`、`data/*.json`（只读文件） | `api_config.get_trade_api`（下单能力） |

**判据**：一个模块能不能 import，看它导入时是否出现 `start_*()`、`Thread(`、`init_app`、`.run()`。有则拉黑。

**兜底原则**：拿不准能不能 import，就**直接读它产出的文件**（`db_url.txt`、JSONL、日志），文件是死的、不带副作用。实时交易数据/受控动作一律走 **HTTP 客户端**调现有 6001 接口，复用其已过冒烟验证的逻辑，绝不自己实现交易。

### 7.3 分阶段接入真实数据

| 阶段 | 交付 | 数据源 | 安全闸门 |
|------|------|--------|---------|
| **P0**（已完成） | 视觉基线：外壳 + 令牌 + 组件 + Dashboard 静态样例 | 无 | 启动 8889，确认 6001 无变化、`git diff crypto/` 为空 |
| **P1** | 仪表盘 + 健康检查 + 进程生命周期接真实数据 | `system_monitor.run_check()`、`process_lifecycle` 读取函数、`boot_ledger.jsonl` | 复核采集层未 import 任何黑名单模块 |
| **P2** | 内存治理图表 + 日志聚合中心 | `memory_history.jsonl`、`task_scheduler.log`、`trade_operations.log` | 日志读取用增量 tail，不整文件载入（大文件 715KB+） |
| **P3** | 调度可视化 + 实盘只读监控 | 调度状态文件 / HTTP 调 6001 | 确认全部只读，无任何写操作 |
| **P4** | 一键冒烟运行器 + 诊断工具箱 + 配置总览 | `crypto/task/_smoke_*.py`、`_diag_*.py` | 运行器默认只放行 🔒 纯离线脚本；🌐 真实 API 脚本需二次确认 |

### 7.4 具体接入示例：内存趋势从静态到真实

以 §5.2.4 的内存趋势图为例，P2 阶段的改造步骤：

1. **采集**：`collectors/memory.py` 逐行读 `crypto/logs/memory_history.jsonl`（JSON Lines），取最近 N 条的 RSS 与时间戳。
2. **接口**：`blueprints/dashboard.py` 加 `GET /api/memory/trend?hours=24`，返回 `{points:[{ts, rss_mb}], threshold_mb, peak, avg}`。
3. **前端**：把 `dashboard.html` 里硬编码的 `polyline points` 换成 JS `fetch` 拿数据后动态生成；或直接改用 ECharts 消费该 JSON。
4. **安全**：整个链路只读文件，不 import 任何交易模块，不改 `crypto/`。

> 其余模块（生命周期时间线 ← `boot_ledger.jsonl`；服务健康 ← `system_monitor.run_check()`；调度状态 ← 状态文件/HTTP）同理，都是"读产物 → JSON 接口 → 前端渲染"三段式。

### 7.5 快速验证交易正常（P4 冒烟运行器）

`crypto/SMOKE_TESTS.md` 已有一套成熟冒烟脚本（含安全等级标注），但"没有统一 runner"。运维中心的工具箱可做成**一键冒烟运行器**：

- 默认展示"常驻回归三件套"（`_smoke_rg2` / `_smoke_dual_position` / `_smoke_fix_regression`，均 🔒 纯离线），一键跑、看红绿灯。
- 🔒 纯离线脚本可随时批量跑；🛡️ 写库、⚠️ 租约、🌐 真实 API 脚本标红安全等级、需显式确认，🌐 实盘时段禁用。
- 需临时起交易实例验证时，统一用 `CRYPTO_NO_BACKGROUND=1`（项目现有约定），避免测试触发真发信/真调 OKX。

这样维护者不必每天人工全量测试：打开运维中心 → 点"运行常驻回归" → 几秒确认交易链路健康。

---

## 8. 附录：复刻清单与常见问题

### 8.1 从 0 复刻的最短路径（清单）

1. 建目录：`opscenter_demo/{templates,static/css,static/js}`。
2. 写 `static/css/tokens.css`：先结构令牌，再颜色令牌（深/浅两套）。
3. 写 `static/css/layout.css`：重置 → `.app` Grid 外壳 → 侧栏/顶栏/内容/页脚 → 栅格工具类 → 响应式断点。
4. 写 `static/css/components.css`：`.btn/.card/.tile/.badge/.progress/.chart/.table/.timeline/.list-row`。
5. 写 `templates/base.html`：`<head>` 按 tokens→layout→components 顺序引 CSS；body 为 `.app` 外壳；侧栏导航用 Jinja 双层循环；留 `{% block content %}`。
6. 写 `templates/dashboard.html`：`{% extends "base.html" %}`，用栅格 + 组件类拼 KPI/图表/列表/时间线/表格。
7. 写 `static/js/app.js`：主题切换 + 移动端侧栏 + 刷新动效 + 入场淡入。
8. 写根入口 `dashboard_demo.py`：`Flask(template_folder=…, static_folder=…)` 指向独立目录，默认绑 127.0.0.1:8889。
9. 验证：test_client 渲染冒烟 → 起服务 HTTP 探活 → `git status` 确认未碰 `crypto/`。

### 8.2 常见问题（FAQ）

**Q1：编辑器对 `dashboard.html` 报了一堆 CSS 语法错误？**
A：那是静态检查器无法解析内联 `style` 里的 Jinja 变量（如 `var(--{{ level }}-soft)`）导致的**误报**。以实际运行渲染为准（§6.3 的 test_client 冒烟返回 200、无 `{{` 残留即正确）。

**Q2：样式没生效 / 改了 CSS 浏览器看不到变化？**
A：Flask 若 `debug=False`，改模板/路由需重启服务；改静态 CSS/JS 需浏览器硬刷新（`Ctrl+F5`）清缓存。演示站已设 `debug=True` 但 `use_reloader=False`，改 Python 需手动重启。

**Q3：会不会误启动实盘调度器？**
A：不会。只要坚持**不 `import crypto.app` / `crypto.task.scheduler`**（§7.2 黑名单），运维站就是纯观察者。演示站连 `crypto` 都没 import。

**Q4：想从手机看演示站怎么办？**
A：设 `OPSDEMO_HOST=0.0.0.0` 启动，手机与服务器同网段访问 `http://服务器IP:8889`。正式站对外开放务必先加口令闸门。

**Q5：`url_for('static', ...)` 会取到 `crypto/static` 吗？**
A：不会。`dashboard_demo.py` 已把 `static_folder` 指向 `opscenter_demo/static`，`static` 端点只解析到运维站自己的目录。

### 8.3 关键文件索引

| 文件 | 作用 | 对应章节 |
|------|------|---------|
| `dashboard_demo.py` | 独立入口，端口/绑定配置 | §3.3、§6 |
| `opscenter_demo/templates/base.html` | 应用外壳 | §5.1 |
| `opscenter_demo/templates/dashboard.html` | 系统健康概览页 | §5.2 |
| `opscenter_demo/static/css/tokens.css` | 设计令牌 | §4 |
| `opscenter_demo/static/css/layout.css` | 布局层 | §5.1 |
| `opscenter_demo/static/css/components.css` | 组件层 | §5.2 |
| `opscenter_demo/static/js/app.js` | 前端交互 | §5.3 |

---

*本指南基于演示站 v0.1.0 整理。正式站演进时请同步更新本文档，保持"代码与文档一致"。*
