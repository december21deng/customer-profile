# 会议纪要媒体 ingest 设计（画板 / 图片）

## 背景

跟进记录的「会议纪要」docx URL 里有两类**非文字**内容需要展示在详情页「会议总结」section：

1. **画板（board block, type 43）**：妙记自动插入的概览图 / AI 生成的思维图 / 用户手绘
2. **图片（image block, type 27）**：销售在 docx 里嵌入的截图、照片

之前的实现是详情页加载时**实时**调 `docx/v1/documents/{id}/blocks` + media 下载 API。这条路被飞书权限模型堵死 —— 即使 app 申请了 `docx:document:readonly`，`tenant_access_token` 读任何文档都要**文档拥有者手动把 app 加进共享列表**（`forBidden` = 1770032）。不现实。

本文档定稿：**把媒体在 ingest 时间物化到我们自己的存储**，详情页只读本地。

## 飞书权限模型（从官方文档确认）

调 docx API 必须同时满足两层：

| 层 | 要求 | 满足方式 |
|---|---|---|
| App scope | `docx:document:readonly` | 应用后台已审批 ✅ |
| 文档访问权 | `tenant_access_token`：doc owner 手动把 app 加进每份文档 | ❌ 不可规模化 |
| 同上 | `user_access_token`：**调用者本人**有这份 doc 的读权限（owner / 被分享者） | ✅ 销售贴自己的文档链接，当然有 |

所以方案是用 **user_access_token** —— 用上传者（销售）自己的身份读他自己的文档。

另有一个 OAuth 侧的细节：scope 必须在 OAuth 时**显式请求**才会注入到 user_access_token。当前 OAuth 只请求 `contact:user:search`，所以用户的 token 虽然能拿到，但不具备 docx scope。要加进 scope 字符串，用户重登一次触发二次授权。

## 架构决策

**在 ingest pipeline 里读，不在详情页读。**

```
销售提交表单
  └─ INSERT followup_records（owner_id = 销售 open_id）
  └─ BackgroundTask: run_ingest_pipeline(record_id)
       ├─ 读 followup_records.owner_id → 查 user_tokens → 得到 user_access_token
       ├─ fetch_docx_raw(doc_id, token=user_token)    → 纯文字 → AI summary
       ├─ fetch_docx_media(doc_id, token=user_token)  → blocks 列表
       │    ├─ 对每个 image block: download bytes via drive/v1/medias → photo_storage.save() → local_xxx
       │    └─ 对每个 board block: download_as_image → photo_storage.save() → local_yyy
       │         （画板 API 权限审批中，暂时 skip，留 TODO）
       └─ UPDATE followup_records SET minutes_media = JSON(...)
```

详情页：

```
GET /followup/{id}
  └─ SELECT r.minutes_media
  └─ 渲染：for each item → <img src="/api/image/{key}">
  └─ 不调飞书任何 API
```

## 好处

1. **权限链简化**：只在 ingest 时用一次 user token，view 时完全不碰飞书
2. **view 速度**：详情页没有 5-30s 的飞书 API 往返
3. **跨用户可见**：管理者 / 同事看别人的记录，不需要文档访问权
4. **内容快照**：飞书原文后期删改不影响我们已 ingest 的历史数据
5. **成本可控**：每文档只调一次媒体下载 API，不是"每人每次打开都调"

## 数据模型

### `followup_records` 新增列

```sql
ALTER TABLE followup_records ADD COLUMN minutes_media TEXT NOT NULL DEFAULT '[]';
```

JSON 数组，按原文档顺序：
```json
[
  {"kind": "image", "key": "local_abc..."},
  {"kind": "board", "key": "local_def..."},
  {"kind": "image", "key": "local_ghi..."}
]
```

`key` 复用 `photo_storage` 的 `local_*` / `img_v*` 两种值域 —— 和用户手动上传的合照走同一套代理路由 `/api/image/{key}`。

### 权限审批中的画板

画板导出的权限（`board:whiteboard:node:read` + `board:whiteboard:node:content:read`）还没批。ingest 时如果下载失败：
- image 部分照常存
- board 部分**不存条目**（而不是存一个坏 key）
- 等权限批下来后，手动 regen-wiki 该记录即可补齐

## OAuth scope 变更

`src/web/auth.py` 的 authorize URL：
```diff
- "scope": "contact:user:search"
+ "scope": "contact:user:search docx:document:readonly"
```

**老用户要重登一次**才能获得新 scope（飞书会在授权页显示"额外请求：查看新版文档"）。
**新用户**第一次 OAuth 就直接获得。

## 函数签名变化

### `src/lark_client.py`

**改前**（tenant token，硬编码在函数里）：
```python
def fetch_docx_raw(doc_id: str) -> tuple[str | None, str | None]:
    token = _get_tenant_token()
    ...
```

**改后**（caller 传 user token）：
```python
def fetch_docx_raw(doc_id: str, access_token: str) -> tuple[str | None, str | None]:
    # 不再取 tenant token，调用方从 user_tokens 表拿
    ...
```

同样改：
- `fetch_docx_media(doc_id, access_token)`
- `stream_docx_image(file_token, access_token)`（ingest 用这个下载二进制存本地）
- `stream_board_image(whiteboard_token, access_token)`

### `src/ingest/pipeline.py`

新增一步（放 extract 之后，commit 之前）：
```python
def _fetch_media(record: dict, user_token: str) -> list[dict]:
    """返回 minutes_media JSON 数组。"""
    items, err = fetch_docx_media(record["minutes_doc_id"], user_token)
    if err or not items:
        return []
    result = []
    for it in items:
        if it["kind"] == "image":
            stream, _ctype = stream_docx_image(it["token"], user_token)
            if stream is None: continue
            buf = b"".join(stream)
            key = photo_storage.save(buf, filename=f"docx-{it['token'][:8]}.png", content_type="image/png")
            if key: result.append({"kind": "image", "key": key})
        elif it["kind"] == "board":
            stream, _ctype = stream_board_image(it["token"], user_token)
            if stream is None: continue  # 权限未批时走这里
            buf = b"".join(stream)
            key = photo_storage.save(buf, filename=f"board-{it['token'][:8]}.png", content_type="image/png")
            if key: result.append({"kind": "board", "key": key})
    return result
```

## 详情页模板

**改前**（实时调飞书）：
```html
{% if minutes_media %}
  {% for m in minutes_media %}
    {% if m.kind == 'image' %}
      <img src="/api/docimg/{{ m.token }}">
    {% elif m.kind == 'board' %}
      <img src="/api/docboard/{{ m.token }}">
    {% endif %}
  {% endfor %}
{% endif %}
```

**改后**（读 DB）：
```html
{% if r.minutes_media %}
  {% for m in r.minutes_media %}
    <img src="/api/image/{{ m.key }}">
  {% endfor %}
{% endif %}
```

## 会议详情 section 简化

目前该 section 里同时有：
1. 纪要正文（长文本）
2. 打开原文 / 打开妙记 按钮

改成：只保留按钮（可选加一句"点击下方按钮在飞书查看完整内容"）。

原因：
- "打开原文"按钮本来就在 → 正文在本地是冗余
- 正文长，占屏幕
- 飞书原文可能被修改，本地拷贝会过时
- AI 的 summary / meeting_title / progress_line 已经提炼了要点，不需要再塞长文

## 要清理的东西

刚才提交的 view-time 方案里有一批代码会变成死码，要一起删：

| 文件 | 动作 |
|---|---|
| `src/web/followup.py` | 删 `/api/docimg/{file_token}` 和 `/api/docboard/{whiteboard_token}` 路由 |
| `src/web/followup.py` | `followup_detail` 不再调 `fetch_docx_media` / `fetch_docx_raw` |
| `src/lark_client.py` | 保留 `fetch_docx_*` / `stream_docx_*`，但签名改成收 access_token |
| `src/web/templates/followup_detail.html` | 会议总结 section 从 `minutes_media`（临时对象）改读 `r.minutes_media`（DB） |

## 迁移

**已有 followup 记录**（local / prod）：
- `minutes_media` 默认 `[]`
- 想要历史记录的画板/图片：手动 `flyctl ssh console -C "python -m src.scripts.reingest_media"` 或走 regen-wiki 重跑

**新记录**：自动生效（只要用户已重登获取新 scope）。

## 错误处理

| 情况 | 行为 |
|---|---|
| 上传者没重登（user_token 没 docx scope） | ingest 那步 403，`minutes_media = []`，详情页"未找到画板或图片"。日志告警 |
| 上传者 token 过期（2h）且没 refresh_token | 同上，提示用户重登 |
| 画板 API 还没审批权限 | image 部分正常，board 部分静默跳过 |
| 文档被删 / 无权限 | 同"403"。不阻塞整个 pipeline（summary 仍然跑，只是 media 为空） |
| 下载到二进制后保存失败（磁盘满 / flyctl storage 问题） | 记日志，该条 media 跳过，其它继续 |

## 缓存 / 去重

- `fetch_docx_raw` 保留 5 分钟内存 cache（ingest 和 regen-wiki 可能连续打同一个 doc）
- `fetch_docx_media` 同 5 分钟 cache
- 存本地的 image 按 UUID 命名，重跑 regen-wiki **会重新下载并再存一遍**（旧 key 留着，新 key 覆盖 minutes_media 字段）—— 磁盘冗余可忽略

## 安全

- `minutes_media` 存的是 local_* key，不是飞书 file_token，外部无法反推原文档
- `/api/image/{key}` 已有 cookie 鉴权（任何登录用户都能看，这是产品设计，不是安全漏洞）
- 用户 user_access_token 只在 ingest 那一瞬间从 DB 读出用一次，不出 pipeline 范围

## 工作量估算

| 项 | 文件 | 行数 |
|---|---|---|
| OAuth scope + 1 行 | auth.py | 2 |
| `fetch_docx_*` 函数签名加 token 参数 | lark_client.py | ~30 |
| DB migration（新列） | schema.py + migrate.py | 5 |
| Pipeline 新加一步 `_fetch_media` | ingest/pipeline.py | ~40 |
| Commit 时写 minutes_media 列 | ingest/pipeline.py | 3 |
| 详情页模板改 DB 读 | followup_detail.html | 10 |
| 删 view-time 路由 | followup.py | −60 |
| 会议详情去正文 | followup_detail.html | 5 |

合计 ≈ 95 行增加，60 行删除。

## 不做的事

- **不做**：详情页实时调飞书 docx API（权限成本不合算）
- **不做**：把纪要正文缓存进 DB（大字段、会过期、AI summary 已经够）
- **不做**：画板 API 没批前的替代方案（画板没了就没了，等审批）
- **不做**：把上传者的 user_token 持久"常驻" —— 只在 ingest 时一次性读用

## 时间

代码改动 + 本地测试约 40 分钟，部署 + 重登验证 10 分钟。
