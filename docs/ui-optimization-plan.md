# H5 跟进记录 UI/UX 优化方案

> 2026-04-24 定稿 · 本轮优化范围小而密，一次打包落地

## 范围

### 要做（5 件，合计 ~2.5h）

1. 草稿自动保存（localStorage）
2. 提交后跳详情页（不是列表）
3. 参会人员 chip 显示真飞书头像
4. 图片上传前端压缩
5. docx URL 实时校验

### 明确不做

- ❌ Pipeline 进度可视化 / "AI 处理中" 提示
  - **不从用户视角讲**：用户不需要知道背后有 AI 和异步流水线，那是系统的事
- ❌ 服务端草稿（跨设备同步，需求暂无）

---

## 1. 草稿自动保存

### 目标

用户没填完关了页面，下次回来能接着写。

### 方案

- **存储**：`localStorage`，key = `followup-draft-{customer_id}`
- **触发**：每个 input / textarea / select 的 `input` 或 `change` 事件，**debounce 500ms** 批量写
- **不存**照片（`File` 不能序列化 + 太大）

### 数据格式

```json
{
  "transcript_url": "...",
  "minutes_url": "...",
  "meeting_date": "2026-04-24T10:00",
  "location": "...",
  "our_attendees": [{"id": "ou_...", "name": "邓鑫"}],
  "client_attendees": ["张总"],
  "background": "...",
  "_saved_at": "2026-04-24T15:30:00"
}
```

### UI

页面加载后，若 localStorage 有草稿 **且** 表单字段都是空的，顶部浮一条：

```
┌─────────────────────────────────────────────────┐
│ 💡 检测到 3 分钟前未完成的草稿                   │
│                        [恢复草稿]  [丢弃]        │
└─────────────────────────────────────────────────┘
```

- "恢复" → 回填字段（照片除外，提示"请重新上传照片"）
- "丢弃" → 清 localStorage
- 5 秒无动作自动收起

### 清理时机

- 成功提交后清除（submit 前 localStorage.removeItem）
- 用户主动丢弃
- 超过 7 天自动过期

### 代码位置

`src/web/templates/followup_new.html` 的 `<script>` 末尾追加，~50 行 JS。**0 后端/DB 改动**。

---

## 2. 提交后跳详情页

### 目标

用户立刻看到自己提交的记录 + 照片 + 所有信息，不用再点一次。

### 改动

`src/web/followup.py` 的 `followup_submit` 末尾：

```python
# 原来：
return RedirectResponse(url=f"/customers/{customer_id}?tab=followup", status_code=302)

# 改成：
return RedirectResponse(url=f"/followup/{record_id}", status_code=302)
```

### 详情页微调

`followup_detail.html`：summary 为空时**不显示任何占位**（不要"生成中"这种暴露后台的字眼）。

### 工作量

15 分钟（含跳转测试 + 空 summary 样式）。

---

## 3. 真·飞书头像

### 目标

Chip 从"邓"这种首字母头像，换成真头像。

### 数据侧

- `/api/users/search` 已经返回 `avatar` 字段
- 前端 `ourState` 从 `{id, name}` 改成 `{id, name, avatar}`

### UI

```html
<!-- 原来 -->
<span class="chip-avatar">邓</span>

<!-- 改成 -->
<span class="chip-avatar">
  <img src="${avatar}" onerror="this.outerHTML=name.slice(0,1)" />
</span>
```

- onerror fallback 到首字（CDN 失败时）
- 圆形裁剪（CSS `border-radius: 50%`，已有）
- 尺寸统一 18×18 px

### 历史数据

已有的 `our_attendees` JSON 里只有 `{id, name}` 没 avatar，详情页继续首字头像展示，不回填。

### 工作量

30 分钟（chip + picker-item 两处渲染 + CSS 微调）。

---

## 4. 图片压缩

### 目标

用户上传手机原图（5-15MB）自动压到 500KB-1MB，飞书上传快 + 不超 10MB 限。

### 方案：前端 Canvas resize

| 项 | 值 |
|---|---|
| 最大边长 | 1920 px（Full HD） |
| 格式 | JPEG |
| 质量 | 0.85 |
| 跳过阈值 | 原图 ≤ 500 KB 不压 |

### 流程

```
<input type=file> change
  ↓
读 File → Image
  ↓
画到 canvas，scale = min(1, 1920 / max(w, h))
  ↓
canvas.toBlob('image/jpeg', 0.85)
  ↓
new File(blob, 'photo.jpg', {type:'image/jpeg'})
  ↓
用 DataTransfer 塞回 input，替换原 file
  ↓
渲染预览（压缩后的）
```

### 用户体验

- 选完瞬间压缩（<500ms）
- 预览所见即所得
- canvas API 不可用时静默 fallback 到原图
- 不给用户看"压缩前/后大小"（无必要）

### 工作量

40 分钟（含不同尺寸手机照片测试）。

---

## 5. docx URL 实时校验

### 目标

用户填完 URL 失焦时就知道对不对，不用等到提交。

### 方案

前端复制一份 `DOC_ID_PATTERNS`（保持和后端一致），在两个 URL 输入框的 `blur` 事件里跑：

```javascript
const DOC_ID_PATTERNS = [
  /\/docx\/([A-Za-z0-9]+)/,
  /\/wiki\/([A-Za-z0-9]+)/,
  /\/docs\/([A-Za-z0-9]+)/,
];

function extractDocId(url) {
  for (const re of DOC_ID_PATTERNS) {
    const m = re.exec(url);
    if (m) return m[1];
  }
  return null;
}
```

### 行为

- 空 → 无提示
- 能解析 → 右侧 ✓ 绿
- 不能 → 右侧 ✗ + 红字 `看起来不是飞书会议纪要链接`

**同理** 妙记 URL：`feishu.cn/minutes/*` 格式校验。

### 工作量

30 分钟。

---

## 实施顺序

按 **价值/时间比** 排：

| # | 任务 | 时间 | 用户可感知价值 |
|---|---|---|---|
| 1 | 草稿保存 | 30 min | ★★★★★ |
| 2 | 提交后跳详情页 | 15 min | ★★★★ |
| 3 | 图片压缩 | 40 min | ★★★★ |
| 4 | 真头像 | 30 min | ★★★ |
| 5 | docx 校验 | 30 min | ★★★ |

**合计 ~2.5h**，一次性打包。

---

## 测试清单

### 草稿保存

- [ ] 填一半字段 → 刷新 → 看到"恢复"条
- [ ] 点"恢复" → 非照片字段回填
- [ ] 点"丢弃" → localStorage 清空
- [ ] 成功提交 → localStorage 清空
- [ ] 两个不同客户的草稿互不干扰

### 提交跳详情页

- [ ] 提交 → `/followup/{id}` 而不是 `/customers/{id}`
- [ ] 详情页显示参会人员 + 背景 + 照片
- [ ] summary 为空时不显示"处理中"字样

### 真头像

- [ ] 搜同事下拉显示真头像
- [ ] chip 显示真头像
- [ ] 图片 404 时 fallback 到首字

### 图片压缩

- [ ] iPhone 拍的 8MB 原图 → 压到 <1 MB
- [ ] <500 KB 的图不动
- [ ] 预览显示压缩后
- [ ] 详情页显示正常

### docx 校验

- [ ] 粘贴真飞书 docx URL → ✓
- [ ] 粘贴百度链接 → ✗
- [ ] 空 → 无提示
