# 表单 picker 升级设计（我方参会 / 会议纪要 / 妙记）

## 背景与动机

跟进记录表单里三个核心字段现在都是手动输入/搜索：

| 字段 | 当前交互 | 痛点 |
|---|---|---|
| 我方参会人员 | 自研搜索框 | 一次只能搜一个名字，多人会议录入 10+ 次 |
| 会议纪要 | 粘贴 URL | 销售得去飞书复制 URL 切回来贴 |
| 妙记 | 粘贴 URL | 同上 + 贴错不知道，之前的 `forBidden` bug |

飞书生态里有两类可用能力：
- **官方客户端 JSSDK**（`tt.chooseContact`、`tt.docsPicker`）—— 原生组件，弹飞书自己的 UI
- **未公开但实际可用的 REST API** —— `POST /open-apis/minutes/v1/minutes/search`，飞书自家 `lark-cli` 在用

两样叠加，三个字段都能做到**"一次选完 / 所见即所得"**。

## 权限模型（飞书官方确认）

OAuth 2.0 的三层链条：

| 层 | 要求 |
|---|---|
| 应用级 scope | 飞书开发者后台申请 → 审批 |
| OAuth 请求 scope | authorize URL 的 `scope` 参数显式列出 |
| 用户授权 | 用户在飞书授权页点"允许" |
| 资源权限 | 用户自己对该资源有读权限（飞书服务端过滤）|

**前 3 层缺一不可，第 4 层由飞书服务端自动保证。**

新增 scope：
- `minutes:minutes.search:read` — 搜索妙记列表（自研 picker 用）
- `minutes:minutes.basic:read` — 获取单条妙记元信息（验证卡片用）

## 三个字段的最终交互

### 字段 1：我方参会人员

**飞书里**（主路径）：

```
我方参会人员 *
[👤 张心如] [👤 邓鑫]
┌─────────────────────┐
│ 👥 选择同事（多选）  │ → tt.chooseContact({ multi: true, externalContact: false })
└─────────────────────┘
```

**浏览器里**（fallback）：保留现有搜索面板，一个个搜。

**chooseContact 参数:**
```js
tt.chooseContact({
  multi: true,
  externalContact: false,         // 排除外部联系人
  enableChooseDepartment: false,  // 不允许选整个部门
  chosenIds: [/* 已选的 open_id 列表 */],
  // maxNum 不设，不限人数
})
```

**返回项** `{ openId, unionId, name, avatarUrls }` → 转换成我们现有的
`{ id, name, avatar }` chip 结构。

**降级检测：**
```js
if (typeof tt === 'undefined' || typeof tt.chooseContact !== 'function') {
  // 显示原有的搜索面板
} else {
  // 显示 "选择同事（多选）" 按钮
}
```

### 字段 2：会议纪要

**Label:** `会议纪要链接` → `会议纪要`

**飞书里**：

```
会议纪要 *
┌─────────────────────┐
│ 📁 选择会议纪要      │ → tt.docsPicker()
└─────────────────────┘
或粘贴链接：
[________________________]
```

**docsPicker 特性：**
- 不支持 fileType 筛选（飞书官方 API 没提供参数）
- 返回 `{ fileList: [{ filePath: "<完整 URL>", fileName }] }`
- 前端拿 `filePath` → 用现有 `DOC_ID_RE` 校验是 `/docx/` 或 `/wiki/`
- 不是的 → 红色提示 "请选择会议纪要（支持 docx / wiki 文档）"
- 是的 → 把 URL 填进粘贴框 + 触发校验状态变绿

**浏览器里:** 按钮灰掉 + `ⓘ 在飞书客户端内可直接选择`。

### 字段 3：妙记（核心改造）

**Label:** `妙记链接` → `妙记`

**飞书 + 浏览器都用同一套自研 picker（不依赖飞书原生组件）：**

```
妙记 *
┌─────────────────────┐
│ 🔍 选择妙记          │ → 展开自研浮层
└─────────────────────┘
或粘贴链接：
[________________________]
┌───────────────────────────────────┐
│ 🎙 产品销售及订单价格讨论          │  ← 选中后的验证卡片
│ 37 分 · 4 月 21 日 · 周俊泓        │
│ ✓ 已验证                          │
└───────────────────────────────────┘
```

**自研浮层 UI:**

```
┌─────────────────────────────────────┐
│ 🔍 搜索主题 / 主讲人...          × │
├─────────────────────────────────────┤
│ 🎙 产品销售及订单价格讨论            │
│    37 分 · 4月21日 · 周俊泓         │
├─────────────────────────────────────┤
│ 🎙 客户询盘AI回复...                 │
│    44 分 · 4月14日 · 李颉琳         │
├─────────────────────────────────────┤
│         —— 加载更多 ——               │
└─────────────────────────────────────┘
```

**交互:**
- 打开时默认拉最近 15 条（空 query + 空 filter 飞书会返 403，所以默认给 `query=""` 不行 —— 需要 fallback 到 `filter.create_time`：最近 30 天）
- 搜索框 debounce 300ms 调后端
- 下拉到底或点"加载更多"翻页
- 点一项 → URL 填进粘贴框 → 触发验证卡片
- 点面板外 / 按 Esc → 关闭

**为什么默认拉"最近 30 天"而不是空:**

飞书官方约束：**`query / owner_ids / participant_ids / start_time / end_time` 至少填一个**。否则 API 返错 "specify at least one of ..."。
所以默认值策略：`filter.create_time = { start: now - 30d, end: now }`。搜索时再切换到 `query = <关键词>`。

## 数据流

```
飞书 OAuth
 └→ user_access_token 存 user_tokens 表（我们已有）

表单提交路径（不变）
 └→ 粘贴 URL / picker 选中 → transcript_url 字段

妙记搜索路径（新）
 浏览器 → GET /api/minutes/search?q=xxx&page_token=xxx
 ↓
 后端取 request.state.uid → user_tokens → user_access_token
 ↓
 POST https://open.feishu.cn/open-apis/minutes/v1/minutes/search
     ?page_size=15&page_token=xxx
     Body: { query?, filter? { create_time? } }
     Auth: Bearer <user_access_token>
 ↓
 飞书返 { items: [{ token, display_info, meta_data: {...} }], has_more, page_token }
 ↓
 后端透传给前端（仅脱敏/裁剪）

妙记验证路径（新）
 前端检测到 URL 输入变动（或 picker 选中）
 └→ 解析出 minute_token
 └→ GET /api/minutes/meta?token=xxx
     └→ 后端调 GET /open-apis/minutes/v1/minutes/{token}
         用 user_access_token
     └→ 返 { title, duration, create_time, owner_name?, url }
 └→ 前端渲染验证卡片
```

## 后端接口

### `GET /api/minutes/search`

```
Query: q?, page_token?, page_size? (默认 15)
Auth: AuthMiddleware（cookie uid）
Body: 无

Response 200:
{
  "items": [
    {
      "token": "obcnq3b9jl72l83w4f14xxxx",     # 用作唯一 ID
      "title": "产品销售及订单价格讨论",        # display_info
      "url": "https://xxx.feishu.cn/minutes/...",
      "description": "会议简介...",
      "avatar": "https://..."
    }
  ],
  "has_more": true,
  "page_token": "xxx"
}

Response 403:
{ "detail": "minutes_scope_required" } → 前端提示审批中

Response 401:
{ "detail": "feishu_login_required" } → 前端跳登录
```

### `GET /api/minutes/meta`

```
Query: token (必填)  或  url
Auth: AuthMiddleware

Response 200:
{
  "title": "...",
  "duration_secs": 2224,
  "create_time": "2026-04-21T10:00:00+08:00",
  "owner_name": "周俊泓",
  "url": "https://xxx.feishu.cn/minutes/xxx"
}

Response 403:
{ "detail": "forbidden" } → 前端验证卡片显示红叉

Response 400:
{ "detail": "invalid_url" } → 前端提示 URL 格式不对
```

## 新 lark_client 函数

```python
def search_minutes(
    user_access_token: str,
    query: str = "",
    create_time_start: datetime | None = None,
    create_time_end: datetime | None = None,
    page_size: int = 15,
    page_token: str = "",
) -> tuple[dict | None, str | None]:
    """POST /open-apis/minutes/v1/minutes/search

    返回 (data, error)。
    data = { items: [...], has_more, page_token }
    """

def get_minute_meta(
    minute_token: str,
    user_access_token: str,
) -> tuple[dict | None, str | None]:
    """GET /open-apis/minutes/v1/minutes/{minute_token}

    返回 (meta, error)。
    """
```

两个函数都**只收 user_access_token**（lark-cli 源码里 `AuthTypes: []string{"user"}`，tenant 不行）。

## OAuth scope 变更

`src/web/auth.py`:

```python
"scope": (
    "contact:user:search "
    "docx:document:readonly "
    "wiki:node:read "
    "minutes:minutes.search:read "        # 新
    "minutes:minutes.basic:read"          # 新
)
```

用户下次进 OAuth 时会看到"请求访问：搜索您的妙记 / 查看妙记基本信息"，点允许即可。

**审批前** / **老用户没重登**：调 API 会 403 → picker 按钮保留，但点了提示 "权限审批中，请暂时粘贴链接"。

## 环境检测

```js
var isFeishu = (typeof window.h5sdk !== 'undefined') && (typeof window.tt !== 'undefined');

// 字段 1: 我方参会
var nativeContactBtn = document.getElementById('our-native-btn');
var browserSearchPanel = document.getElementById('our-search-panel');
if (isFeishu && typeof tt.chooseContact === 'function') {
  nativeContactBtn.hidden = false;
  browserSearchPanel.hidden = true;
} else {
  nativeContactBtn.hidden = true;
  browserSearchPanel.hidden = false;
}

// 字段 2: 会议纪要
var docsPickerBtn = document.getElementById('docs-picker-btn');
if (!isFeishu || typeof tt.docsPicker !== 'function') {
  docsPickerBtn.disabled = true;
  docsPickerBtn.title = '在飞书客户端内可用';
}

// 字段 3: 妙记（两种环境都用自研 picker，不用检测）
```

## CSS / 样式

复用现有的 `.picker-panel` / `.picker-btn` / `.picker-search` 样式。妙记浮层单独样式：

```css
.minutes-picker-panel {
  position: absolute; z-index: 10;
  max-height: 60vh;
  overflow-y: auto;
  ...（复用 .picker-panel 现有风格）
}
.minutes-picker-item {
  display: flex; padding: 10px 14px;
  gap: 12px; cursor: pointer;
  border-bottom: 1px solid var(--neutral-100);
}
.minutes-picker-item:hover { background: var(--neutral-50); }
.minutes-picker-item .icon { font-size: 18px; }
.minutes-picker-item .title { font-weight: 500; color: var(--neutral-900); }
.minutes-picker-item .meta  { font-size: 12px; color: var(--neutral-400); }
.minutes-picker-more {
  text-align: center;
  padding: 12px;
  color: var(--neutral-400);
  font-size: 13px;
  cursor: pointer;
}
.minutes-validation-card {
  margin-top: 8px;
  padding: 10px 14px;
  border: 1px solid var(--neutral-200);
  border-radius: 10px;
  background: var(--neutral-50);
  display: flex; gap: 12px; align-items: center;
}
.minutes-validation-card.error { background: #FFF1F0; border-color: #FFCCC7; }
.minutes-validation-card .v-title { font-weight: 500; }
.minutes-validation-card .v-meta { font-size: 12px; color: var(--neutral-400); }
.minutes-validation-card .v-check { color: #52C41A; }
```

## 错误处理矩阵

| 场景 | 后端返回 | 前端 UI |
|---|---|---|
| 用户没登录飞书（密码登录 uid）| 401 feishu_login_required | 按钮禁用 + 提示"请用飞书账号登录" |
| Token 过期且无 refresh | 401 reauth_required | 同上 + 跳转 `/auth/lark` |
| 审批中，scope 没有 | 403 minutes_scope_required | 灰按钮 + "权限审批中，请粘贴链接" |
| 用户对某妙记无权限（验证卡片）| 403 forbidden | 红叉 + "无权访问该妙记" |
| URL 格式错 | 400 invalid_url | "不是有效的妙记链接" |
| 飞书 API 500 | 502 upstream_failed | "服务不稳定，稍后重试" |

## 分阶段发布

**阶段 1（代码合并后立即上线）：**
- 我方参会：飞书里用 `chooseContact`，浏览器里用现有搜索
- 会议纪要：飞书里用 `docsPicker`，浏览器里用粘贴
- 妙记：picker 按钮存在但点了 403 → fallback 到粘贴 + 验证卡片无法渲染（因为 meta API 也要 scope）

**阶段 2（scope 审批通过 + 用户重登）：**
- 妙记 picker 工作
- 验证卡片工作

**阶段 3（以后）：** VC 会议 picker（`/open-apis/vc/v1/meetings/search`）作为妙记的另一种入口。

## 工作量

| 项 | 行数 |
|---|---|
| OAuth scope | 1 |
| lark_client: search_minutes + get_minute_meta | 60 |
| 后端代理：/api/minutes/search + /api/minutes/meta | 60 |
| 前端：chooseContact 集成 + 按钮 + 环境检测 | 60 |
| 前端：docsPicker 集成 + 类型校验 | 35 |
| 前端：自研妙记 picker 浮层（搜索 + 列表 + 分页）| 130 |
| 前端：妙记验证卡片 | 40 |
| CSS | 80 |
| Label 改名 + 文案调整 | 5 |

合计 ≈ 470 行。

## 安全

- 两个新 API 路由都挂 AuthMiddleware（cookie uid 鉴权）
- user_access_token 只在后端读用，不返前端
- 所有飞书 API 响应脱敏后再透传（只保留 title/token/url/duration/owner_name 等展示字段）
- minute_token 是飞书 public identifier（URL 里就有），不是机密

## 未来可能的扩展

- 加 VC 会议 picker：`POST /open-apis/vc/v1/meetings/search`（scope 可能是 `vc:meeting:readonly`），让用户按会议名找，再关联出妙记
- 同样模式给"会议纪要"做一个 wiki + docx 搜索的自研 picker，彻底甩开 `tt.docsPicker`（浏览器也能用）
- 妙记 picker 支持按 owner 过滤（"只显示我主讲的"）

这些都是后续迭代，不在本次 scope 里。
