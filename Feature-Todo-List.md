# nanocode 功能清单与迭代路线（Feature / Todo List）

> 本文件是 nanocode 的**功能全景 + 未来迭代设计簿**。
> 上半部分记录「现在已经具备什么能力」，下半部分记录「接下来准备怎么做、为什么这么设计」。
> 面向两类读者：想快速了解现状的使用者，以及准备接手迭代的开发者。
>
> nanocode 是一个极简的 Claude Code 替代品：**零第三方依赖、纯标准库**，以 `nanocode.py` 为唯一入口，
> 功能按依赖逻辑拆分到 `tools/` `knowledge/` `session/` 三个包。目标是用最小代码量跑通一个完整的
> **agentic coding loop（智能体编码循环）**，便于学习与二次开发。

---

## 目录

- [第一部分 · 已实现功能清单](#第一部分--已实现功能清单)
  - [1. 工程结构](#1-工程结构)
  - [2. agentic 工具循环](#2-agentic-工具循环)
  - [3. 内置工具（9 个）](#3-内置工具9-个)
  - [4. 斜杠命令（12 个）](#4-斜杠命令12-个)
  - [5. 权限安全：四层防线](#5-权限安全四层防线)
  - [6. 会话与历史](#6-会话与历史)
  - [7. 领域知识库](#7-领域知识库)
  - [8. 内置 SKILL 与项目记忆](#8-内置-skill-与项目记忆)
  - [9. 终端体验：流式输出与 Spinner](#9-终端体验流式输出与-spinner)
  - [10. 双渠道接入](#10-双渠道接入)
- [第二部分 · 迭代路线与设计决策](#第二部分--迭代路线与设计决策)
  - [A. 知识库 v2：本地 embedding 语义检索](#a-知识库-v2本地-embedding-语义检索已落地可选)
  - [B. 规划中的能力](#b-规划中的能力)
  - [C. 已知局限与风险](#c-已知局限与风险)
  - [D. 贡献者起步点](#d-贡献者起步点)

---

# 第一部分 · 已实现功能清单

## 1. 工程结构

代码以 `nanocode.py` 为唯一入口，其余功能按依赖逻辑拆分到三个标准库包（纯标准库，零第三方依赖）：

| 文件 / 包 | 职责 |
|------|------|
| `nanocode.py` | 主入口：工具实现、工具注册表、API 调用（流式 SSE）、agentic loop、终端交互、Spinner 动效、四层权限防线 |
| `tools/` | 终端交互层。`commands.py`：命令元数据（`COMMANDS`）与 Tab 补全候选的唯一数据源 |
| `knowledge/` | 领域知识层。`kb.py`：知识库检索（BM25 / 向量 / 混合）；`embedding.py`：本地 embedding 向量后端；`skills.py`：内置技能（SKILL）；`agent_config.py`：解析 `AGENTS.md` 注入身份/本体；`gaps.py`：知识缺口记录 |
| `session/` | 会话与历史层。`session.py`：对话历史 JSON 持久化与恢复；`changelog.py`：文件写入/编辑的全量快照与回滚；`compact.py`：历史过长时的自动压缩摘要 |

依赖方向：`nanocode.py` → `tools` / `knowledge` / `session`；三个包之间无交叉 import，各包内部自成一体，便于单独阅读与替换。

运行时数据目录：

| 路径 | 说明 |
|------|------|
| `.nanocode_sessions/` | 每个会话一个 JSON 文件，文件名为时间戳（如 `20261024_124341.json`） |
| `.nanocode_kb/` | 领域知识库索引（构建期生成、随分发包携带） |

---

## 2. agentic 工具循环

模型可以连续多轮自主调用工具，无需人工介入每一步。实现见 `main()` 中的内层 `while True` 循环。

**退出条件**：模型本轮回复里不再包含 `tool_use` 块（即不再请求调工具），此时认为任务已给出最终答案。

```
用户输入
   │
   ▼
┌──────────────────────────────────────────┐
│  main() 交互循环 (nanocode.py)             │
│   1. 读取用户输入 / 处理 /命令             │
│      (命令元数据来自 tools/commands.py)    │
│   2. 追加到 messages 历史                  │
│   ┌──────── agentic loop ──────────┐       │
│   │  call_api(messages, system)    │       │
│   │   (流式 SSE + Spinner 动效)     │       │
│   │       ▼                        │       │
│   │  解析 content_blocks           │       │
│   │   ├─ text    → 流式打印给用户   │      │
│   │   └─ tool_use→ run_tool() 执行  │      │
│   │       ▼                        │       │
│   │  有工具调用? ─是→ 回传结果,继续 │      │
│   │       否                       │       │
│   └───────┼────────────────────────┘       │
│           ▼                                │
│   session.save() 自动保存                  │
└──────────────────────────────────────────┘
```

---

## 3. 内置工具（9 个）

工具在 `TOOLS` 注册表中集中定义，每项为 `(描述, 参数 schema, 函数)` 三元组。`make_schema()` 会自动把参数 schema 转换成 API 所需的 JSON Schema（参数类型以 `?` 结尾表示可选），加工具无需改动 loop。

| 工具 | 功能 | 参数 |
|------|------|------|
| `read` | 读文件，每行带行号，支持 offset/limit 分段读取 | `path`,`offset?`,`limit?` |
| `write` | 覆盖写入文件 | `path`,`content` |
| `edit` | 字符串精确替换；默认要求 old 唯一，`all=true` 可全量替换 | `path`,`old`,`new`,`all?` |
| `glob` | 按通配符查找文件，按 mtime 倒序 | `pat`,`path?` |
| `grep` | 正则搜索文件内容，返回 `文件:行号:内容`，最多 50 条 | `pat`,`path?` |
| `bash` | 执行 shell 命令，实时流式输出，30s 超时保护 | `cmd` |
| `kb_search` | 检索领域知识库（BM25/向量/混合），项目/领域问题优先调用 | `query`,`k?` |
| `skill` | 按名称/关键词加载内置 SKILL（团队标准流程/检查清单） | `name` |
| `record_gap` | 当 `kb_search` 找不到答案时，记录知识缺口供后续补充语料 | `question`,`context?`,`note?` |

---

## 4. 斜杠命令（12 个）

命令元数据集中在 `tools/commands.py` 的 `COMMANDS` 一处，`/help` 展示与 Tab 补全候选都来自它（唯一数据源）。

| 命令 | 作用 |
|------|------|
| `/help`（`-h`/`--help`） | 显示帮助，列出所有命令 |
| `/sessions` | 列出历史会话（含首条用户消息预览） |
| `/resume <编号>` | 恢复指定历史会话，续写该会话 |
| `/c` | 清空当前对话，开启全新会话 |
| `/compact` | 立即手动压缩当前对话历史（超长时也会自动触发） |
| `/changes` | 列出本项目所有文件改动记录（编号、时间、增删行数） |
| `/undo` | 撤销最近一次文件改动 |
| `/revert <编号>` | 回滚指定编号的文件改动 |
| `/kb` | 知识库：`index`/`update`/`stat`/`model`（详见第 7 节） |
| `/skills` | 列出内置 SKILL 及其触发场景 |
| `/mode` | 切换权限模式 default↔bypass（也可按 Shift+Tab） |
| `/q`（`exit`） | 退出 nanocode |

`/` 后按 Tab 可补全命令，候选带中文说明（见 `commands.py` 的 `setup_completion`）。

---

## 5. 权限安全：四层防线

写操作与命令执行受四层递进式防护，层层把关（实现见 `nanocode.py` 的权限区段）：

| 层 | 名称 | 作用 | 能否被 bypass 跳过 |
|----|------|------|--------------------|
| ① | **Deny 硬底线** | 正则黑名单拦截「一条命令毁掉整台机器」的高危操作：`rm -rf /`、fork 炸弹、`mkfs`、`dd of=/dev/*`、重定向覆写磁盘设备、`chmod/chown -R /` 等 | **不能**，任何模式都拦 |
| ② | **cwd 边界校验** | `write`/`edit`/`bash` 的目标路径解析软链后必须落在当前工作目录子树内，越界需确认 | bypass 可跳过 |
| ③ | **交互确认闸门** | 有副作用的操作执行前提示 `y`（本次）/`a`（允许且不再问本类）/`n`（拒绝，默认）；`_APPROVED` 记住已批准签名避免反复打断 | bypass 可跳过 |
| ④ | **凭证防泄漏** | `read`/`grep` 命中 `.env`、`id_rsa`、`*.pem`、`.aws/`、`.ssh/`、`credentials` 等凭证文件时默认跳过；grep 行内对 `password=`/`token:` 等敏感赋值脱敏 | — |

**权限模式**：
- `default`（默认）：②③ 生效，写操作限 cwd，越界/有副作用需确认；① 硬底线照拦。
- `bypass`：跳过 ②③ 一切确认，自担风险；① 硬底线仍然拦。用 `--bypass` 启动参数或运行中 `/mode`（Shift+Tab）切换。

终端底部会有状态条显示当前模式（`_mode_banner()`）。

---

## 6. 会话与历史

- **会话持久化**：每轮对话结束自动保存到 `.nanocode_sessions/`，messages 结构直接沿用 API 格式，存取原样读写无需转换。相关命令：`/c`、`/sessions`、`/resume <编号>`。实现见 `session/session.py`。
- **改动快照与回滚**：`write`/`edit` 每次写入都记录全量快照，可按编号回滚。相关命令：`/changes`、`/undo`、`/revert <编号>`。实现见 `session/changelog.py`。
- **自动压缩**：历史过长时自动摘要压缩（也可 `/compact` 手动触发），控制上下文长度与成本。实现见 `session/compact.py`。

---

## 7. 领域知识库

让 nanocode 变成「自带领域知识」的 agent：构建期把一批语料（文档/代码）切块并建一份轻量检索索引，随分发包一起交付；运行期模型通过 `kb_search` 工具自主检索，命中后再决定是否 `read` 读完整原文。实现见 `knowledge/kb.py`。

**三种检索后端**（可通过 `/kb index --backend` 或 `config.json` 切换）：

| 后端 | 原理 | 依赖 | 适合 |
|------|------|------|------|
| `bm25`（默认） | 稀疏词频检索，带文档长度归一化 | 纯标准库，零第三方 | 关键词/专有名词/代码符号命中 |
| `vector` | 随包静态词向量 + 余弦 Top-k | 可选 `numpy`（资产随包，无需下载） | 语义近似、换种说法也能召回 |
| `hybrid` | BM25 + 向量结果用 RRF 融合排序 | 同 vector | 兼顾关键词精确与语义泛化 |

> 关键澄清：**生成模型 ≠ embedding 模型**。**agentic search 是范式**（模型自主决定搜不搜、搜几次、要不要深读），BM25/向量只是 `kb_search` 内部可替换的后端。向量引擎不可用（未装 numpy / 资产缺失）时，`vector`/`hybrid` 会优雅回退到 BM25，不影响使用。

**存储布局（`.nanocode_kb/`）**：

| 文件 | 内容 |
|------|------|
| `config.json` | 后端标识、超参、分块参数、（向量时）embedding 模型名与维度 |
| `chunks.jsonl` | 每行一个片段 `{id, source, start_line, end_line, title, text}` |
| `index.json` | BM25 稀疏索引 `{df, doc_len, tf, avg_len, n_docs}` |
| `sources.json` | `{文件路径: mtime}`，支撑增量重建 |
| `vectors.jsonl` | （向量后端）每行 `{id, vec}`，L2 归一化 |

**切块策略**：Markdown 按 `#` 标题层级切并用栈维护标题路径；代码按 `def`/`class` 正则粗切、取首符号行作标题；其它按长度切。过长片段带 120 字重叠二次切（`CHUNK_MAX_CHARS=1200`）。行号从 1 起，便于检索结果直接与 `read` 联动。

**命令**：

| 命令 | 作用 |
|------|------|
| `/kb index <目录> [--backend bm25\|vector\|hybrid]` | 全量重建索引 |
| `/kb update <目录> [--backend ...]` | 增量更新（按 mtime 只重切变更文件） |
| `/kb stat` | 查看概况（后端、模型/维度、片段数、向量数、来源文件数） |
| `/kb model` | 预下载/预热本地 embedding 模型 |

**分发思路**：开发仓库里 `.nanocode_kb/` 不入库（见 `.gitignore`），避免误提交测试语料/大索引。打包时由脚本针对正式语料现场 `/kb index`，再把 `*.py` + `.nanocode_kb/` 一起打包交付——解压即用。

---

## 8. 内置 SKILL 与项目记忆

- **内置 SKILL**：把领域专用的提示词、检查清单、标准流程做成 SKILL，清单常驻、正文按需加载。模型遇到匹配场景先 `skill` 加载标准步骤再执行。`/skills` 可列出全部 SKILL 及触发场景。实现见 `knowledge/skills.py`。
- **项目记忆**：启动时解析 `AGENTS.md` 注入模型身份/项目约定到 system prompt，让 agent 了解当前项目的本体与规范。实现见 `knowledge/agent_config.py`。
- **知识缺口记录**：`kb_search` 找不到答案时，模型调 `record_gap` 把未覆盖的问题记下来，作为后续补充语料的输入。实现见 `knowledge/gaps.py`。

---

## 9. 终端体验：流式输出与 Spinner

- **模型回复流式输出**：`call_api` 走流式 SSE，文本增量边收边打印（首片段前打 `⏺` 前缀），不再整段阻塞等待。
- **Spinner 等待动效**（`Spinner` 类）：在「请求发出 → 首字节到达」的空窗期原地转圈（Braille 点阵帧），让用户知道没卡死；收到首个内容块后用 ANSI `\r\033[K` 擦净动效行。非 tty（管道/重定向）时自动退化不启动。
- **bash 工具流式输出**：子进程输出实时打印，长命令执行时可见进度。
- ANSI 颜色/加粗/暗色输出；极简 Markdown 渲染（`render_markdown`，目前支持 `**加粗**`）；分隔线自适应终端宽度（最大 80 列）。

---

## 10. 双渠道接入

- 设置 `OPENROUTER_API_KEY` → 走 OpenRouter（`Authorization: Bearer`），可用 `MODEL` 环境变量切换任意模型。
- 未设置时 → 走 Anthropic 风格接口（`x-api-key`）。

鉴权头选择逻辑见 `call_api`。

**运行方式**：

```bash
# 方式一：Anthropic 风格接口
export ANTHROPIC_API_KEY="your-key"
python nanocode.py

# 方式二：OpenRouter（可切换模型）
export OPENROUTER_API_KEY="your-key"
export MODEL="openai/gpt-5.2"
python nanocode.py

# 可选：跳过权限确认（危险命令硬底线仍生效）
python nanocode.py --bypass
```

---

# 第二部分 · 迭代路线与设计决策

## A. 知识库 v2：随包静态词向量语义检索（已落地，可选）

**动机**：BM25 只能命中关键词；用户换一种说法（语义近似）时召回不到。v2 引入本地向量编码，把知识库升级为语义检索。

**设计（方案 A：随包静态词向量 + numpy 可选依赖 + 优雅回退）**：
- `knowledge/embedding.py` 是 kb.py 与编码引擎之间的唯一接口层，对外只暴露 `available/encode_docs/encode_query/ensure_model/model_name/dim/install_hint`。
- `knowledge/static_vec.py` 是实际编码引擎：纯标准库实现 Unigram/Viterbi 分词（NFKC 归一化 + `▁` metaspace），再对命中 token 的词向量取均值并 L2 归一化。
- 向量资产随包放在 `knowledge/assets/`（约 14MB，三个文件）：`vocab.tsv`（51,166 行 `piece\tscore`，行序即 id）、`emb.i8`（`[51166,256]` int8 raw）、`scale.f32`（per-row 反量化系数）。还原公式 `M_f32 = emb_i8.astype(float32) * scale`。
- 资产来源：`minishlab/potion-multilingual-128M`（model2vec 静态词向量，原始 `[500353,256]` F32 约 489MB）→ 词表裁剪（前 30000 高频 ∪ 全部含 CJK 条目）→ per-row int8 量化。实测 Top-1 命中与全量模型基本持平（7/10 vs 8/10），与全量打分相关性 0.958。
- **为什么不用 sentence-transformers**：它会拖进 torch（几百 MB 到 1GB+），且模型需联网下载到 `~/.cache/huggingface`，与「解压即用」的分发前提冲突。静态词向量只需查表求均值，推理零神经网络。
- 静态词向量是**对称编码**：query 与 doc 走完全相同的管道，不存在 bge 系列那种 query 指令前缀。
- 资产懒加载 + 模块级缓存（加载约 0.04s），避免重复初始化。
- **三处回退鲁棒性**：建索引时引擎不可用/编码失败 → 回退 BM25 并清空向量；`_resolve_backend` 运行期检测引擎不可用或向量缺失 → 回退；`hybrid` 向量路为空 → 退化纯 BM25。
- `hybrid` 用 RRF（Reciprocal Rank Fusion，`RRF_K=60`）融合 BM25 与向量排序。注意静态词向量的绝对分数区间偏低（中文 0.26~0.53），**只有相对排序有意义，不可设绝对阈值**，这也是选 hybrid+RRF 而非分数过滤的原因。
- `_vector_search` 优先走 numpy 矩阵乘 + `argpartition` 取 top-k，numpy 缺失时回退纯 Python 点积（功能等价，仅变慢）。

**依赖**：`numpy` 列为**可选**依赖（见 `requirements.txt`）。不装则知识库行为与 v1 完全一致（纯 BM25）。

**状态**：全链路已本地实测通过（`/kb model` 预热就绪、`/kb index --backend hybrid` 成功落盘向量、`kb_search` 正常召回）。

---

## B. 规划中的能力

### B.1 核心能力增强
| 功能 | 说明 |
|------|------|
| 更多工具 | `apply_patch`（多文件补丁）、`ls`、`web_fetch`/联网搜索、`git` 封装 |
| 子任务 / 子 agent | 把大任务拆给独立子 agent 并行执行（读多文件、跑多测试） |
| 智能忽略 | 遵循 `.gitignore`，搜索时跳过 `node_modules`/`.git` 等 |
| 知识库一键打包 | 打包脚本：正式语料 `/kb index` → 把 `*.py` + `.nanocode_kb/`(+SKILL) 打成 zip 或 PyInstaller onedir，团队解压即用 |

### B.2 工程化与体验
| 功能 | 说明 |
|------|------|
| 配置系统 | 配置文件（模型、超时、工具开关、确认策略等） |
| 测试与 CI | 为工具函数、session、schema 补齐单元测试，接入 CI |
| 更完整的渲染 | 代码块语法高亮、diff 高亮、列表/标题渲染 |
| 成本与用量统计 | 显示每轮 token 消耗与累计成本 |
| 多模型路由 | 按任务难度自动选模型（便宜模型跑简单任务） |
| 错误重试 | API 超时/限流自动退避重试 |
| 可配置超时 | `bash` 固定 30s 超时改为可配置，长任务不被杀 |
| 插件化工具 | 工具支持从外部目录动态加载，无需改主文件 |

---

## C. 已知局限与风险

> 当前代码里**客观存在**的问题，作为未来改进输入。注：阶段一「安全与可信」原路线图多项已落地（见第 5 节四层防线、第 6 节压缩、第 9 节流式）。

- **无 token 精确计数**：历史有自动压缩，但没有精确 token 计量与成本显示。
- **grep/glob 未排除大目录**：未跳过 `.git`、`node_modules` 等，大仓库下较慢。
- `bash` 固定 30s 超时，不可配置；长任务（构建、测试）易被杀。
- Markdown 渲染只支持 `**加粗**`，代码块、列表、标题均不渲染。
- 无单元测试、无 CI。
- 向量后端需外部模型下载，首次使用需联网（之后本地缓存）。

---

## D. 贡献者起步点

1. 补一个最小测试框架（`pytest`），覆盖 `read/write/edit/glob/grep` 与 `session` 读写。
2. 按 B.1 → B.2 推进，保持「少依赖、仅标准库」的项目初衷——核心运行时零依赖，新增依赖一律做成**可选**（如 embedding）。
3. 新增功能优先放入 `tools/` `knowledge/` `session/` 对应包，避免再次让 `nanocode.py` 膨胀。
4. 三个包之间不要交叉 import，保持各包自成一体、可单独替换。
