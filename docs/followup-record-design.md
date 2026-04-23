# 跟进记录（手动录入）设计

## 0. 背景

跟进记录来源有两类：

1. **AI ingest**：记录落库后异步从会议纪要 docx 抽取，落 raw → 更新 wiki → 回写 SQL。详见 [`ingest-implementation-plan.md`](./ingest-implementation-plan.md)
2. **手动录入**（本文档）：用户在 H5 里填表单，提供 docx URL + 背景 + 参会人等

本文档覆盖「手动录入」的 H5 表单与字段约束。提交后触发的 ingest 流程见 ingest-implementation-plan。

## 1. 设计原则

- **必填优先**：所有字段必填，不做草稿 / 半成品态
- **H5 原生体验**：在飞书小程序容器内跑，参会人用 JSSDK 原生选人
- **会议纪要而非妙记**：妙记 transcript 无 API，只能用「会议纪要」docx URL
- **图片私有存储**：合照存 Lark `im/v1/images`（租户私有），不进云空间
- **权限延迟解除**：`im:resource` 权限现在申请不了（前版本待审），但前后端先写，等权限下来即可用

## 2. 功能范围

**做：**

- 客户详情页右下角 `+` 按钮 → 跳表单
- 客户列表 item 上的 `+` 按钮 → 跳同一表单（预选该客户）
- 表单提交：7 个必填字段 + 1 张合照
- 列表页「跟进记录」tab 展示（简化版，后续迭代）

**不做：**

- 草稿 / 自动保存
- 多张合照
- 编辑已提交的记录
- 手动选客户（只能从客户页 `+` 进，一定带 customer_id）

> AI ingest 在本表单提交后自动触发（异步 BackgroundTask），见 ingest-implementation-plan。

## 3. 入口

| 入口 | 路径 |
|---|---|
| 客户详情页 FAB | `/customers/{id}/followup/new` |
| 客户列表 item 的 `+` 按钮 | `/customers/{id}/followup/new` |

两个入口跳同一路由，`customer_id` 从 URL 拿，不让用户改。

## 4. 表单字段

| 字段 | 说明 | 控件 | 校验 |
|---|---|---|---|
| 会议纪要 URL | 飞书 docx 链接 | `<input type="url">` | 必填；正则匹配 `docs/.+/docx/(\w+)` 或 `wiki/(\w+)` |
| 会议时间 | 具体日期 + 时分 | `<input type="datetime-local">` | 必填；不超过当前时间 |
| 会议地点 | 字符串 | `<input type="text">` | 必填；≤ 100 字 |
| 我方参会人员 | 飞书同事 | JSSDK 选人 | 必填；≥ 1 人 |
| 客户参会人员 | 纯文本名单 | `<textarea>` | 必填；≤ 200 字 |
| 会议背景 | 纯文本 | `<textarea>` | 必填；≤ 500 字 |
| 客户合照 | 图片 | `<input type="file" accept="image/*">` | 必填；≤ 10MB；JPEG/PNG |

### 4.1 会议纪要 URL 解析

前端不校验，后端收到后用正则抽 `doc_id`：

```
docs.feishu.cn/docx/{doc_id}
docs.feishu.cn/wiki/{doc_id}
{tenant}.feishu.cn/docx/{doc_id}
```

拿不到 `doc_id` 就 400。存原 URL + 抽出的 `doc_id`，后者给 AI 后续读 `raw_content` 用。

### 4.2 我方参会人员：Lark JSSDK

飞书 H5 内：

```js
h5sdk.biz.contact.selectChatter({
  max: 20,
  multi: true,
  pickedOpenIds: [],
}, (res) => {
  // res.data: [{open_id, name, avatar}]
});
```

拿到 `[{open_id, name}]` 数组，JSON.stringify 后塞进 hidden input。

**降级**：非飞书环境（桌面浏览器调试）用普通 textarea，输入 `name1,name2`，open_id 留空。降级不是产品态，仅调试。

### 4.3 客户合照：上传到 `im/v1/images`

```
POST https://open.feishu.cn/open-apis/im/v1/images
Authorization: Bearer <tenant_access_token>
Content-Type: multipart/form-data

image_type=message
image=<binary>
```

返回 `image_key`（格式 `img_v2_xxx`），存库。

**权限**：`im:resource` 或 `im:resource:upload`。当前 App 还没有，代码先写好，等权限下来一键可用。

**限制**：单张 ≤ 10MB；格式 JPEG/PNG/GIF。

**展示**：通过后端代理，前端 `<img src="/api/image/{image_key}">`。

## 5. 数据模型

### 5.1 schema.py 变更

`followup_records` 表扩列（全部新列可空，兼容 AI ingest 场景）：

```sql
CREATE TABLE IF NOT EXISTS followup_records (
    id                TEXT PRIMARY KEY,           -- uuid4
    customer_id       TEXT NOT NULL,
    owner_id          TEXT,                       -- 创建人 open_id（来自 cookie）
    meeting_date      TEXT NOT NULL,              -- ISO: 2026-04-23T14:30

    -- 手动录入字段（manual 必填，ingest 为 NULL）
    location          TEXT,
    our_attendees     TEXT,                       -- JSON: [{"open_id":"ou_xxx","name":"张三"}]
    client_attendees  TEXT,                       -- 原文，逗号/顿号分隔
    background        TEXT,
    minutes_doc_url   TEXT,                       -- 原始 URL
    minutes_doc_id    TEXT,                       -- 抽出的 doc_id
    photo_image_key   TEXT,                       -- Lark im/v1/images 的 image_key

    -- 共用字段（ingest 也会用）
    source_type       TEXT,                       -- 'manual' | 'chat' | 'meeting_link'
    source_url        TEXT,                       -- ingest 用
    source_title      TEXT,                       -- ingest 用
    summary           TEXT,                       -- Extract 阶段填入（2-4 句）；ingest 失败保持 NULL
    created_at        TEXT NOT NULL
)
```

**索引保持不变**：`idx_fr_customer_date` 覆盖详情页列表场景。

**迁移策略**：`CREATE TABLE IF NOT EXISTS` 不会对已有表加列。如果本地已有旧表，手写迁移脚本 `ALTER TABLE followup_records ADD COLUMN ...`（7 个 ADD COLUMN）。生产库没数据，`DROP TABLE` 重建也行。

### 5.2 字段映射（表单 → DB）

| 表单 field | DB 列 |
|---|---|
| minutes_url | `minutes_doc_url` + 解析出 `minutes_doc_id` |
| meeting_date | `meeting_date` |
| location | `location` |
| our_attendees (JSON) | `our_attendees` |
| client_attendees | `client_attendees` |
| background | `background` |
| photo (file) | 先上传 Lark → `photo_image_key` |
| — | `source_type = 'manual'` |
| — | `owner_id` ← cookie 里的 open_id |
| — | `id` ← uuid4 |
| — | `created_at` ← now |

## 6. 路由

| 方法 | 路径 | 作用 |
|---|---|---|
| GET  | `/customers/{id}/followup/new` | 表单页（SSR） |
| POST | `/customers/{id}/followup` | 提交（multipart） |
| GET  | `/api/image/{image_key}` | 图片代理（从 Lark 流式下载） |

### 6.1 POST 处理流程

```
1. 鉴权：cookie 拿 open_id（= owner_id）
2. 解析 multipart
3. 字段校验：全部必填 + 长度
4. 解析 minutes_doc_url → doc_id（正则）
5. 上传图片到 Lark im/v1/images → image_key
   失败：返 500，让用户重提
6. INSERT into followup_records
7. 302 到 /customers/{id}?tab=followup
```

**原子性**：图片先上传，再写 DB。DB 写失败时 Lark 上有一张孤儿图，可接受（私有存储，不消耗配额）。

### 6.2 图片代理

```python
@app.get("/api/image/{image_key}")
def proxy_image(image_key: str):
    # 1. 校验格式：^img_v2_\w+$（防路径注入）
    # 2. GET https://open.feishu.cn/open-apis/im/v1/images/{image_key}
    #    Authorization: Bearer <tenant_access_token>
    # 3. StreamingResponse 透传
```

**缓存**：加 `Cache-Control: private, max-age=86400`（image_key 不变）。

**鉴权**：代理路径需要 cookie，继承全局 AuthMiddleware。

## 7. UI 布局

```
┌──────────────────────────────────┐
│  ← 返回      新增跟进             │
│                                  │
│  客户：UPE                        │ ← 只读，从 URL 带
│                                  │
│  会议纪要链接 *                   │
│  ┌──────────────────────────────┐ │
│  │ https://...                  │ │
│  └──────────────────────────────┘ │
│                                  │
│  时间 *                           │
│  ┌──────────────────────────────┐ │
│  │ 2026-04-23 14:30             │ │
│  └──────────────────────────────┘ │
│                                  │
│  地点 *                           │
│  ┌──────────────────────────────┐ │
│  │                              │ │
│  └──────────────────────────────┘ │
│                                  │
│  我方参会人员 *                   │
│  ┌──────────────────────────────┐ │
│  │ + 选择同事                    │ │ ← tap 调 JSSDK
│  └──────────────────────────────┘ │
│  🙂 张三  🙂 李四                 │ ← 选好后展示
│                                  │
│  客户参会人员 *                   │
│  ┌──────────────────────────────┐ │
│  │                              │ │
│  │                              │ │
│  └──────────────────────────────┘ │
│                                  │
│  会议背景 *                       │
│  ┌──────────────────────────────┐ │
│  │                              │ │
│  │                              │ │
│  └──────────────────────────────┘ │
│                                  │
│  客户合照 *                       │
│  ┌──────────────────────────────┐ │
│  │    📷 点击上传                │ │ ← 点后显示预览
│  └──────────────────────────────┘ │
│                                  │
│  ┌──────────────────────────────┐ │
│  │          提交                │ │ ← sticky 底部
│  └──────────────────────────────┘ │
└──────────────────────────────────┘
```

沿用 `base.html` 已有的字段样式 variables，不新增视觉语言。

## 8. 文件结构

新增 / 修改：

```
src/
├── db/
│   └── schema.py                        (修改：扩列)
├── lark_client.py                       (修改：加 upload_im_image/stream_im_image)
├── web/
│   ├── app.py                           (修改：mount followup router)
│   ├── followup.py                      (新增)
│   └── templates/
│       ├── customer_detail.html         (修改：FAB href)
│       ├── _rows.html                   (修改：+ 按钮 href)
│       └── followup_new.html            (新增)
```

`src/web/followup.py`：APIRouter，含 3 个路由。

## 9. 权限 & 上线顺序

| 步骤 | 依赖权限 | 可否先做 |
|---|---|---|
| schema 扩列 | 无 | ✅ |
| 表单页 + POST 校验 | 无 | ✅ |
| 图片上传 Lark | `im:resource` | ⚠️ 代码可写，运行时报 403 |
| 图片代理展示 | `im:resource` | ⚠️ 同上 |
| JSSDK 选人 | 飞书 H5 运行时 | ✅（桌面降级） |

**结论**：全部代码先写，提交前用 mock image_key 过一遍本地。`im:resource` 拿到后去掉 mock。

## 10. 后续迭代（out of scope）

- AI ingest：群聊 / 妙记链接自动抽取 → 同一张表，`source_type != 'manual'`
- 跟进记录列表页（列表 tab 当前占位）
- 编辑 / 删除
- 多图
- 客户无 `+` 的入口（比如主页 + 按钮需要先选客户）
