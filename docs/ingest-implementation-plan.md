# Ingest 落地方案

## 0. 目的与范围

把 `docs/v0.2-design.md` 的 Ingest Pipeline 从 spec 落到可部署的代码，并把 Fly 上 `wiki/` `raw/` 的持久化 + 版本控制策略定下来（方案 B，见 §2）。

本文档之后，和早期设计文档冲突时以本文档为准。

相关文档：
- `docs/v0.2-design.md` — 整体架构（三层、Schema、并发、成本）
- `docs/followup-record-design.md` — 跟进记录 H5 表单与字段
- `docs/lark-managed-agent-design.md` — Lark bot 集成

---

## 1. 存储模型

### 1.1 三层职责（继承 v0.2 §0）

| 层 | 目录 / 位置 | 写入方 | 读取方 |
|---|---|---|---|
| 档案 | `raw/customers/*.md` | Python（Fetch 阶段） | LLM 只读 |
| Narrative | `wiki/customers/<id>.md`, `wiki/index.md`, `wiki/log.md` | Claude Agent SDK + karpathy skill | LLM 可读写，H5 渲染只读 |
| 结构化索引 | SQLite | Python（Extract 阶段） | H5 / API |

**硬约束**：
- `wiki/` 只允许一级 topic 子目录，项目里只用 `customers/`
- 客户页文件名 = `customers.id`（CRM slug），不是显示名
- 一个 customer = 一篇 wiki article

### 1.2 代码仓库 vs 数据目录

**本地开发**（一个 git 仓库管代码，wiki/raw 不入库）

```
managed-agent/
├── .git/                           ← 代码仓库
├── .gitignore                      ← 新增 /wiki/ /raw/ 两行
├── src/
├── .claude/skills/karpathy-llm-wiki/
├── wiki/                           ← 本地测试产物，不入 git
│   └── customers/
├── raw/                            ← 本地测试产物，不入 git
│   └── customers/
└── db/app.sqlite                   ← 本地 SQLite，已 gitignore
```

**Fly 运行时**（代码在 `/app/`，数据在 `/data/`，两层独立 git）

```
/app/                               ← 容器文件系统（重启重建，运行时写入丢）
├── .git/                           ← 代码仓库快照，read-only 心态
├── src/
├── .claude/skills/karpathy-llm-wiki/
├── wiki → /data/wiki               ← symlink
└── raw  → /data/raw                ← symlink

/data/                              ← Fly volume（重启保留，每日快照）
├── .git/                           ← 独立仓库，只追踪 raw/ + wiki/
├── app.sqlite  (+ -wal, -shm)
├── raw/customers/
└── wiki/
    ├── index.md
    ├── log.md
    └── customers/
```

- Agent SDK 的 `cwd = PROJECT_ROOT = /app`，通过 symlink 访问 `wiki/` `raw/`，同时 `.claude/skills/` 可见
- `/data/.git` 不推远端，只做本地版本控制，给 H5 wiki 历史页（v0.2 §5.8）用
- 代码仓库 `.gitignore` 忽略 `/wiki/` `/raw/`，本地测试永不污染代码库

---

## 2. Fly 部署：方案 B

### 2.1 为什么是方案 B

三选一（详见对话记录）：
- **方案 A**：`wiki/ raw/` 留在 `/app/`，每次 ingest `git push` 到远端。需外网凭据、多实例会撞、淹没代码 history
- **方案 B**：`/data/` 下独立 `git init`，只追踪 wiki+raw。无外网依赖，代码库干净，git log 白送 ← 选它
- **方案 C**：不用 git，SQLite 存版本。丢了 git 白送的 diff/history 工具

### 2.2 start.sh 幂等初始化

```bash
# 持久化准备（仅 Fly；本地没 /data 直接跳过）
if [ -d /data ]; then
    mkdir -p /data/raw/customers /data/wiki/customers

    if [ ! -d /data/.git ]; then
        git -C /data init -q
        git -C /data config user.email "bot@customer-profile-dec.fly.dev"
        git -C /data config user.name  "ingest-bot"
        git -C /data commit --allow-empty -m "init" -q
    fi

    ln -sfn /data/raw  /app/raw
    ln -sfn /data/wiki /app/wiki
fi
```

### 2.3 Dockerfile.fly 变更

装 Node.js + Claude Code CLI（`claude_agent_sdk` 底层 subprocess）+ git：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libsqlite3-0 curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*
```

### 2.4 fly.toml

删掉 `WIKI_DIR` env（symlink 接管路径问题）。`APP_DB_PATH` 保留 `/data/app.sqlite`。

### 2.5 备份与恢复

| 什么丢了 | 怎么恢复 |
|---|---|
| 当次 ingest 失败 | 业务自己重跑（ingest_jobs 记录状态） |
| `/data/.git` 坏了 | 删掉 `.git` 重新 init，只丢历史不丢当前内容 |
| `/data/wiki/` 全丢 | 从 `/data/raw/` 跑全量 ingest 重建 |
| `/data/raw/` 全丢 | SQLite `followup_records.source_doc_id` 重拉飞书 |
| 整个 volume 坏了 | Fly volume 每日快照恢复 |

---

## 3. Ingest 四阶段落地

沿用 v0.2-design §3.2 的四步，细化到代码。

### 3.1 Fetch（Python）

`src/ingest/fetch.py`：

```python
def fetch_and_save_raw(doc_id: str, customer_id: str, record_id: str,
                       meeting_date: str) -> tuple[Path, str] | None:
    """拉飞书 docx raw 存 raw/customers/<date>-<customer>-<hash>.md。
    返回 (raw_path, raw_text) 或 None。
    """
    text, err = lark_client.fetch_docx_raw(doc_id)
    if err or not text:
        return None

    date = meeting_date[:10]  # YYYY-MM-DD
    short = record_id[:8]
    fname = f"{date}-{customer_id}-{short}.md"
    raw_path = RAW_DIR / "customers" / fname

    header = textwrap.dedent(f"""\
        ---
        source: https://feishu.cn/docx/{doc_id}
        customer_id: {customer_id}
        record_id: {record_id}
        meeting_date: {meeting_date}
        collected: {datetime.now().isoformat(timespec="seconds")}
        published: Unknown
        ---

        """)
    raw_path.write_text(header + text, encoding="utf-8")
    return raw_path, text
```

### 3.2 Ingest（Claude Agent SDK + karpathy skill）

`src/ingest/wiki_agent.py`：

```python
async def run_ingest(customer: Customer, raw_rel_path: str,
                     meeting_date: str) -> str:
    opts = ClaudeAgentOptions(
        model="claude-haiku-4-5",
        allowed_tools=["Read", "Write", "Edit", "Grep", "Glob"],
        cwd=str(PROJECT_ROOT),
        permission_mode="bypassPermissions",
        resume=None,  # 每次 fresh session，状态在文件里
    )

    prompt = f"""按 karpathy-llm-wiki skill 的 Ingest 流程处理这份客户跟进。

客户：{customer.name}（id={customer.id}）
目标 wiki 页：wiki/customers/{customer.id}.md（不存在就新建）
新 raw 文件：{raw_rel_path}
会议时间：{meeting_date}

硬约束：
- wiki/ 只有 customers/ 一个 topic，不要创建其他 topic
- 客户页文件名必须用 {customer.id}（slug），不是显示名
- 不要级联到其他客户的文章
- 不要读 raw/customers/ 下其他客户的文件
"""

    text = ""
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text:
                    text += b.text
    return text
```

### 3.3 Extract（单次 structured output）

`src/ingest/extract.py`：

```python
EXTRACT_TOOL = {
    "name": "extract",
    "description": "从 wiki 文章全文和一份新 raw 纪要提取结构化字段",
    "input_schema": {
        "type": "object",
        "required": ["summary", "journey_stage", "record_summary", "contacts_delta"],
        "properties": {
            "summary": {"type": "string",
                        "description": "300-800 字，从 wiki 提炼的客户整体概况"},
            "journey_stage": {"type": "string", "enum": [
                "线索","初步接触","需求确认","方案沟通",
                "报价商务","合同签约","交付验收","运营维护","流失冻结",
            ]},
            "record_summary": {"type": "string", "description": "2-4 句概括本次跟进"},
            "contacts_delta": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action", "name"],
                    "properties": {
                        "action": {"enum": ["add", "update"]},
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
        },
    },
}


def extract(wiki_text: str, raw_text: str) -> dict:
    resp = anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        system="你是结构化提取助手。输入 wiki 文章全文和一份新 raw 纪要，输出 JSON。",
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract"},
        messages=[{"role": "user",
                   "content": f"Wiki：\n{wiki_text}\n\nRaw：\n{raw_text}"}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "extract":
            return block.input
    raise RuntimeError("extract tool not returned")
```

### 3.4 Commit（SQL 事务 + git）

`src/ingest/commit.py`：

```python
DATA_GIT_DIR = Path("/data")  # Fly 专用；本地无 /data/.git 时跳过


async def commit(record_id: str, customer_id: str,
                 raw_path: Path, extract_result: dict) -> None:
    async with db.transaction():
        await records.update(
            record_id,
            summary=extract_result["record_summary"],
            journey_stage_after=extract_result["journey_stage"],
            raw_path=str(raw_path.relative_to(PROJECT_ROOT)),
        )
        await customers.update(
            customer_id,
            summary=extract_result["summary"],
            journey_stage=extract_result["journey_stage"],
        )
        for delta in extract_result["contacts_delta"]:
            await contacts.upsert(customer_id, delta)

    _git_commit_if_available(customer_id, extract_result["record_summary"])


def _git_commit_if_available(customer_id: str, summary: str) -> None:
    if not (DATA_GIT_DIR / ".git").is_dir():
        return  # 本地开发，跳
    msg_first = summary.splitlines()[0][:60]
    subprocess.run(["git", "-C", str(DATA_GIT_DIR), "add", "raw", "wiki"], check=False)
    subprocess.run(
        ["git", "-C", str(DATA_GIT_DIR), "commit",
         "-m", f"ingest: {customer_id} / {msg_first}"],
        check=False,  # 无变更 commit 会失败，吞
    )
```

---

## 4. Pipeline 编排

`src/ingest/pipeline.py`：

```python
async def run(record_id: str) -> None:
    rec = await records.load_with_customer(record_id)
    if not rec:
        return

    async with CustomerLocks.acquire(rec.customer_id):
        await jobs.set_status(record_id, "fetching")
        fetched = fetch_and_save_raw(
            rec.minutes_doc_id, rec.customer_id, record_id, rec.meeting_date,
        )
        if not fetched:
            await jobs.set_status(record_id, "failed", error="fetch failed")
            return
        raw_path, raw_text = fetched

        await jobs.set_status(record_id, "ingesting")
        try:
            await run_ingest(rec.customer, raw_path.name, rec.meeting_date)
        except Exception as e:
            await jobs.set_status(record_id, "failed", error=f"ingest: {e}")
            return

        wiki_path = WIKI_DIR / "customers" / f"{rec.customer_id}.md"
        wiki_text = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""

        await jobs.set_status(record_id, "committing")
        try:
            extract_result = extract(wiki_text, raw_text)
            await commit(record_id, rec.customer_id, raw_path, extract_result)
        except Exception as e:
            await jobs.set_status(record_id, "failed", error=f"commit: {e}")
            return

        await jobs.set_status(record_id, "done")
```

并发控制 `src/ingest/lock.py` 直接照搬 v0.2-design §4.1 的 `CustomerLocks`。

---

## 5. 代码改动清单

### 5.1 新增

```
src/ingest/__init__.py
src/ingest/pipeline.py   — 编排 + 状态机
src/ingest/fetch.py      — raw 落盘
src/ingest/wiki_agent.py — Agent SDK 调用
src/ingest/extract.py    — structured output
src/ingest/commit.py     — SQL + git
src/ingest/lock.py       — CustomerLocks
docs/ingest-implementation-plan.md (本文档)
```

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `src/config.py` | 回滚 `WIKI_DIR` env 覆盖，定死 `PROJECT_ROOT/wiki` `PROJECT_ROOT/raw`；只 mkdir `wiki/customers/` 和 `raw/customers/`（不再碰 `contacts/`） |
| `src/agent_client.py` | `cwd` 改 `PROJECT_ROOT`；删内置 `INGEST_PROMPT`（让 skill 接管）；保留 query 模式给 v0.2 query pipeline 用 |
| `src/web/followup.py` | `background_tasks.add_task(generate_summary, ...)` 换成 `ingest_pipeline.run(record_id)`；删除 `regen-summary`，保留 `regen-wiki` 入口指向新 pipeline |
| `.gitignore` | 加 `/wiki/` `/raw/` 两行 |
| `start.sh` | 加 §2.2 的 symlink + `/data/.git` init |
| `Dockerfile.fly` | 加 Node.js、Claude Code CLI、git（§2.3） |
| `requirements.txt` | 加 `claude-agent-sdk` |
| `fly.toml` | 删 `WIKI_DIR` env |
| `docs/v0.2-design.md` | §1 目录结构、§3.2 Step 6 git 部分加注 "Fly 详见 ingest-implementation-plan.md" |
| `docs/followup-record-design.md` | "AI ingest（后续迭代）"改成"AI ingest（已实现）"，指向本文档 |

### 5.3 删除 / 作废

| 文件 | 处置 |
|---|---|
| `src/wiki_agent.py` | 我之前手搓的 tool-use 循环，方向错，已删 |
| `src/wiki_ingest.py` | 过渡版 BackgroundTask 入口，并入 `src/ingest/pipeline.py` 后删 |
| `src/ai_summary.py` | 退化版 30 字摘要，被 Extract 取代。PR-C 删 |
| `src/ingest_service.py` | 旧 Managed Agents API 版妙记轮询，不在 Fly 上跑，且依赖已过期的 `agent_client` 接口。PR-C 评估是否删 |

---

## 6. 迁移步骤

分 PR 落地，每个 PR 独立可 deploy。

**PR-A — 基础设施**
- 本文档
- `.gitignore`、`start.sh`、`Dockerfile.fly`、`requirements.txt`、`fly.toml` 改动
- 部署后 `/data/.git` 就位，symlink 就位，Claude Code CLI 可用
- ingest 仍走老路（`ai_summary` summary-only），但不会报错
- 验收：`fly ssh console` 能看到 `/app/wiki -> /data/wiki`，`/data/.git log` 有 init commit

**PR-B — Ingest 管道**
- 新增 `src/ingest/*`
- `src/web/followup.py` 切到 `ingest_pipeline.run`
- `src/agent_client.py` cwd 改 `PROJECT_ROOT`
- `src/config.py` 回滚 WIKI_DIR env
- 验收：H5 提交一条跟进 → raw 文件出现在 `/data/raw/customers/` → wiki 文章生成 → SQL 里 `customers.summary/journey_stage` 更新 → `/data/.git log` 多一条 commit

**PR-C — 清理与文档同步**
- 删 `src/ai_summary.py`、`src/wiki_ingest.py`
- 更新 `docs/followup-record-design.md`
- 评估 `src/ingest_service.py` 去留

**PR-D（可选）— H5 wiki 历史页**
- v0.2-design §5.8 的 `/customers/{id}/wiki/history`，读 `/data/.git log -- wiki/customers/<id>.md`

---

## 7. 开放问题

1. **并发锁落地位置**：FastAPI + BackgroundTasks 场景下，`CustomerLocks` 单例挂在 app state 里还是模块级？倾向模块级 + 单进程假设（Fly 当前 min_machines_running=1）
2. **Extract 失败时 wiki 的状态**：Ingest 已改 wiki，Extract 抛异常 → wiki 改了但 SQL 没更新。要不要在 commit 阶段失败时 `git checkout -- wiki/ raw/` 回滚？第一版先不做，靠重试和人工修复
3. **Haiku 4.5 模型 ID**：设计写 `claude-haiku-4-5`，但实际 CLI 接受的 ID 待确认（可能是 `claude-haiku-4-5-20251001`）
4. **lark-cli 在 Fly 上跑**：`docx_client.py` 用 subprocess 调 lark-cli。需要确认 Dockerfile.fly 里有这个二进制，或者改走纯 HTTP 的 `fetch_docx_raw`（已有，`src/lark_client.py`）。倾向后者
