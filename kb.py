#!/usr/bin/env python3
"""kb - 领域知识库模块：纯标准库 BM25 检索，零第三方依赖。

这是什么 / 为什么这样设计：
- 让 nanocode 具备"领域知识库"能力：把一批文档（Markdown / 代码等）切成片段、
  建立 BM25 关键词索引，模型在回答领域问题前可以先调用 kb_search 工具检索，
  拿到相关片段（含文件路径+行号）后再决定是否 read 原文。这叫 agentic search：
  "由 agent 决定何时搜、搜几次"，BM25 只是 kb_search 的内部检索后端，二者正交。

- 为什么是 BM25 而不是向量检索？第一期要"零依赖、可随包分发、团队解压即用"。
  BM25 是稀疏/关键词检索，只数词频，用标准库 collections+math 手写约几十行即可，
  不需要 embedding 模型、不需要联网、不需要装 numpy/faiss。向量（稠密/语义）检索
  效果更好但需要 embedding 端点与额外依赖，放到第二期（见 DEVELOPER.md 的 TODO）。

- 构建期 vs 运行期（分发关键）：
  * 构建期：知识库作者在本机运行 `/kb index <目录>`，把语料切块+建索引，
    产物全部落在"脚本同级"的 .nanocode_kb/ 目录（而不是 cwd）。
  * 分发：把 *.py 和 .nanocode_kb/ 一起打成 zip 发给团队。
  * 运行期：团队成员解压后在任意目录运行 nanocode，kb_search 读取随包的
    .nanocode_kb/，无需再建索引、无需任何外部服务。
  因此路径必须用"脚本同级"定位（仿 changelog.py），否则换个 cwd 就找不到内置库。

- 为什么与 session / changelog 一样独立成目录？知识库是"长期资产"，
  生命周期独立于单次会话；用独立目录分开存，职责清晰、便于整体打包迁移。

存储布局（.nanocode_kb/ 下）：
  config.json    —— 后端类型与分块参数（backend=bm25、chunk 配置、bm25 超参）
  chunks.jsonl   —— 每行一个片段：{id, source, start_line, end_line, title, text}
  index.json     —— BM25 索引：df（文档频率）、每片段的词频与长度、全局统计
  sources.json   —— 已索引文件清单及 mtime（支持 /kb index 增量重建，跳过未变文件）
"""

import json
import math
import os
import re
from collections import Counter, defaultdict

# 知识库统一存放在"脚本同级"的 .nanocode_kb/ 目录（分发后随包迁移、与 cwd 无关）
KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nanocode_kb")
CONFIG_FILE = os.path.join(KB_DIR, "config.json")
CHUNKS_FILE = os.path.join(KB_DIR, "chunks.jsonl")
INDEX_FILE = os.path.join(KB_DIR, "index.json")
SOURCES_FILE = os.path.join(KB_DIR, "sources.json")

# BM25 经典超参：k1 控制词频饱和、b 控制文档长度归一化，取通用默认值即可
BM25_K1 = 1.5
BM25_B = 0.75

# 分块参数：单片段目标字符数与相邻片段重叠字符数（重叠可缓解边界切断语义）
CHUNK_MAX_CHARS = 1200
CHUNK_OVERLAP = 120

# 默认纳入索引的文本后缀（二进制/图片等一律跳过）
TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".js", ".ts", ".go", ".java", ".c", ".h", ".cpp", ".hpp",
    ".rb", ".rs", ".sh", ".sql", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
}


def _ensure_dir():
    """懒创建 .nanocode_kb/ 目录（首次写入时才建，避免只读场景报错）"""
    os.makedirs(KB_DIR, exist_ok=True)


# --- 分词 ---
# BM25 是关键词检索，先把文本切成"词"。英文/数字按单词切；中文没有空格，
# 用字符 bi-gram（相邻两字成一个词）兜底，这是零依赖下对中文召回最务实的做法。
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text):
    """把一段文本切成 token 列表（小写英文词 + 中文 bi-gram + 单字兜底）。"""
    text = text.lower()
    tokens = []
    # 1) 英文/数字/下划线连续串作为一个 token
    tokens.extend(_WORD_RE.findall(text))
    # 2) 连续中文串切 bi-gram；长度为 1 的中文串保留单字
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
    return tokens


# --- 分块 ---
# 把一个文件切成若干"片段"，每个片段带 source/start_line/end_line/title/text。
# 行号让检索结果能和 read 工具联动（模型可据此精确读原文）。


def _split_long(text, start_line):
    """把一段过长文本按字符上限二次切分，相邻片段保留少量重叠。

    返回 [(sub_text, rel_start_line, rel_end_line), ...]，行号相对整段首行。
    """
    lines = text.split("\n")
    pieces = []
    buf, buf_start, cur_len = [], 0, 0
    for idx, line in enumerate(lines):
        buf.append(line)
        cur_len += len(line) + 1
        if cur_len >= CHUNK_MAX_CHARS:
            sub = "\n".join(buf)
            pieces.append((sub, start_line + buf_start, start_line + idx))
            # 计算重叠：从末尾回退约 CHUNK_OVERLAP 字符对应的行，作为下一片段开头
            back, acc = len(buf), 0
            while back > 1 and acc < CHUNK_OVERLAP:
                back -= 1
                acc += len(buf[back]) + 1
            buf = buf[back:]
            buf_start = idx - (len(buf) - 1)
            cur_len = sum(len(x) + 1 for x in buf)
    if buf and "".join(buf).strip():
        pieces.append(("\n".join(buf), start_line + buf_start, start_line + len(lines) - 1))
    return pieces


def _chunk_markdown(text):
    """Markdown 按标题层级切块，每块携带"标题路径"（如 一级 > 二级）便于定位。

    返回 [(chunk_text, start_line, end_line, title_path), ...]（行号从 1 开始）。
    """
    lines = text.split("\n")
    sections = []          # [(level, title, start_idx)]
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    # 先找出所有标题及其行号，划分出区间
    headings = []
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            headings.append((len(m.group(1)), m.group(2).strip(), i))
    if not headings:
        # 无标题：整篇当一块（过长再二次切）
        chunks = []
        for sub, s, e in _split_long(text, 0):
            chunks.append((sub, s + 1, e + 1, ""))
        return chunks

    # 以每个标题为界把正文切成区间
    chunks = []
    stack = []  # 维护当前标题路径（按 level 出入栈）
    for hi, (level, title, start_idx) in enumerate(headings):
        end_idx = headings[hi + 1][2] - 1 if hi + 1 < len(headings) else len(lines) - 1
        # 更新标题路径栈
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        title_path = " > ".join(t for _, t in stack)
        body = "\n".join(lines[start_idx:end_idx + 1])
        if not body.strip():
            continue
        for sub, s, e in _split_long(body, start_idx):
            chunks.append((sub, s + 1, e + 1, title_path))
    return chunks


def _chunk_code(text):
    """代码按顶层 def/class 粗切（不上 AST，正则够用），每块带所属符号名作标题。

    返回 [(chunk_text, start_line, end_line, title_path), ...]（行号从 1 开始）。
    """
    lines = text.split("\n")
    # 找出顶层/缩进较浅的 def/class 作为切割点
    def_re = re.compile(r"^(\s*)(def|class|func|function|public|private|protected)\b")
    marks = [i for i, ln in enumerate(lines) if def_re.match(ln)]
    if not marks:
        chunks = []
        for sub, s, e in _split_long(text, 0):
            chunks.append((sub, s + 1, e + 1, ""))
        return chunks
    if marks[0] != 0:
        marks = [0] + marks  # 文件开头到第一个符号之间的内容也要成块
    chunks = []
    for mi, start_idx in enumerate(marks):
        end_idx = marks[mi + 1] - 1 if mi + 1 < len(marks) else len(lines) - 1
        body = "\n".join(lines[start_idx:end_idx + 1])
        if not body.strip():
            continue
        # 取该块首个 def/class 行做标题
        title = ""
        for ln in lines[start_idx:end_idx + 1]:
            if def_re.match(ln):
                title = ln.strip()[:80]
                break
        for sub, s, e in _split_long(body, start_idx):
            chunks.append((sub, s + 1, e + 1, title))
    return chunks


def _chunk_file(path, text):
    """按文件类型选择分块策略。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".markdown", ".rst", ".txt"):
        return _chunk_markdown(text)
    if ext in (".py", ".js", ".ts", ".go", ".java", ".c", ".h", ".cpp",
               ".hpp", ".rb", ".rs"):
        return _chunk_code(text)
    # 其它文本（配置等）：不做语义切分，仅按长度切
    chunks = []
    for sub, s, e in _split_long(text, 0):
        chunks.append((sub, s + 1, e + 1, ""))
    return chunks


# --- 文件收集 ---


def _iter_text_files(root):
    """遍历目录，产出所有可索引的文本文件绝对路径（跳过隐藏目录与非文本后缀）。"""
    root = os.path.abspath(root)
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录、常见缓存与版本控制目录，避免把噪音索引进来
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in
            ("__pycache__", "node_modules", "venv", ".git", ".nanocode_kb")
        ]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in TEXT_EXTS:
                yield os.path.join(dirpath, fn)


# --- 索引构建 ---


def build_index(root, append=False):
    """扫描 root 下文本文件，分块并构建 BM25 索引，落盘到 .nanocode_kb/。

    append=False 全量重建；append=True 在已有索引基础上增量合并（跳过 mtime 未变的文件）。
    返回统计 dict：{files, chunks, skipped}。
    """
    _ensure_dir()
    # 载入已有片段与来源（增量模式用）
    existing_chunks, sources = [], {}
    if append and os.path.exists(CHUNKS_FILE):
        existing_chunks = list(_load_chunks())
        sources = _load_sources()

    files = list(_iter_text_files(root))
    # 增量模式下，按 mtime 判断哪些文件需要重新切块
    skipped = 0
    kept_chunks = []
    changed_files = []
    if append:
        changed = set()
        for f in files:
            mtime = os.path.getmtime(f)
            if sources.get(f) == mtime:
                skipped += 1
            else:
                changed.add(f)
                changed_files.append(f)
        # 保留未变文件的旧片段
        kept_chunks = [c for c in existing_chunks if c["source"] not in changed]
    else:
        changed_files = files

    # 对需要处理的文件重新切块
    new_chunks = []
    cid = len(kept_chunks)
    for f in changed_files:
        try:
            with open(f, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        for sub, s, e, title in _chunk_file(f, text):
            if not sub.strip():
                continue
            new_chunks.append({
                "id": cid,
                "source": f,
                "start_line": s,
                "end_line": e,
                "title": title,
                "text": sub,
            })
            cid += 1
        sources[f] = os.path.getmtime(f)

    all_chunks = kept_chunks + new_chunks
    # 重新编号，保证 id 连续（增量合并后旧 id 可能有空洞）
    for i, c in enumerate(all_chunks):
        c["id"] = i

    _build_bm25(all_chunks)
    _save_chunks(all_chunks)
    _save_sources(sources)
    _save_config()
    return {"files": len(changed_files), "chunks": len(all_chunks), "skipped": skipped}


def _build_bm25(chunks):
    """由片段列表构建 BM25 索引并落盘。

    索引结构：
      df        —— 每个 term 出现在多少个片段里（document frequency）
      doc_len   —— 每个片段的 token 总数
      tf        —— 每个片段的 {term: 词频}（只存非零项，稀疏）
      avg_len   —— 平均片段长度（BM25 长度归一化用）
      n_docs    —— 片段总数
    """
    df = defaultdict(int)
    tf_list = []
    doc_len = []
    for c in chunks:
        toks = tokenize(c["text"] + " " + (c["title"] or ""))
        counts = Counter(toks)
        tf_list.append(counts)
        doc_len.append(len(toks))
        for term in counts:
            df[term] += 1
    n_docs = len(chunks)
    avg_len = (sum(doc_len) / n_docs) if n_docs else 0.0
    index = {
        "df": dict(df),
        "doc_len": doc_len,
        "tf": [dict(c) for c in tf_list],
        "avg_len": avg_len,
        "n_docs": n_docs,
    }
    _ensure_dir()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)


# --- 持久化读写 ---


def _save_chunks(chunks):
    """片段以 JSONL 落盘（每行一个 JSON），便于逐行读取与人工查看。"""
    _ensure_dir()
    with open(CHUNKS_FILE, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def _load_chunks():
    """逐行读回片段；文件不存在则产出空。"""
    if not os.path.exists(CHUNKS_FILE):
        return
    with open(CHUNKS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _save_sources(sources):
    _ensure_dir()
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False)


def _load_sources():
    if not os.path.exists(SOURCES_FILE):
        return {}
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config():
    """记录本次索引所用的后端与参数，便于审计与第二期平滑升级。"""
    _ensure_dir()
    config = {
        "backend": "bm25",
        "bm25": {"k1": BM25_K1, "b": BM25_B},
        "chunk": {"max_chars": CHUNK_MAX_CHARS, "overlap": CHUNK_OVERLAP},
        "tokenizer": "en-word + cjk-bigram",
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_index():
    if not os.path.exists(INDEX_FILE):
        return None
    with open(INDEX_FILE, encoding="utf-8") as f:
        return json.load(f)


# --- 检索 ---


def search(query, k=5):
    """用 BM25 对查询打分，返回 top-k 片段（含来源路径+行号+标题+文本）。

    BM25 公式（对每个查询 term 求和）：
      idf(t) = ln( (N - df + 0.5) / (df + 0.5) + 1 )
      score += idf(t) * (f * (k1+1)) / (f + k1*(1 - b + b*dl/avgdl))
    其中 f 是该 term 在片段里的词频、dl 是片段长度、avgdl 是平均长度。
    """
    index = _load_index()
    if not index or index.get("n_docs", 0) == 0:
        return []
    chunks = list(_load_chunks())
    df = index["df"]
    doc_len = index["doc_len"]
    tf = index["tf"]
    avg_len = index["avg_len"] or 1.0
    n = index["n_docs"]

    q_terms = tokenize(query)
    if not q_terms:
        return []

    scores = [0.0] * n
    for term in set(q_terms):
        if term not in df:
            continue
        idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
        for doc_id in range(n):
            f = tf[doc_id].get(term, 0)
            if not f:
                continue
            dl = doc_len[doc_id]
            denom = f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_len)
            scores[doc_id] += idf * (f * (BM25_K1 + 1)) / denom

    # 取分数 > 0 的 top-k
    ranked = sorted(
        [(s, i) for i, s in enumerate(scores) if s > 0],
        key=lambda x: x[0],
        reverse=True,
    )[:k]
    results = []
    for score, doc_id in ranked:
        c = chunks[doc_id]
        results.append({
            "score": round(score, 4),
            "source": c["source"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
            "title": c["title"],
            "text": c["text"],
        })
    return results


def stat():
    """返回知识库概况：是否已建、片段数、来源文件数、后端类型。"""
    index = _load_index()
    sources = _load_sources()
    if not index:
        return {"built": False}
    return {
        "built": True,
        "backend": "bm25",
        "chunks": index.get("n_docs", 0),
        "files": len(sources),
        "kb_dir": KB_DIR,
    }


# --- 工具层：供 nanocode 的 kb_search 工具调用 ---


def kb_search(args):
    """kb_search 工具入口：接收 {"query": ..., "k"?: ...}，返回给模型看的文本结果。

    返回格式刻意做成"人/模型都易读"：每条给出序号、来源文件:行号区间、标题路径、
    以及截断后的片段正文。模型据此可决定是否用 read 工具去读完整原文。
    """
    query = args.get("query", "").strip()
    if not query:
        return "error: query 不能为空"
    k = args.get("k") or 5
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 5
    results = search(query, k=k)
    if not results:
        st = stat()
        if not st.get("built"):
            return "知识库尚未建立。请先在构建期运行 /kb index <目录> 建立索引。"
        return f"未检索到与「{query}」相关的内容（知识库共 {st['chunks']} 个片段）。"

    lines = [f"检索「{query}」命中 {len(results)} 条："]
    for i, r in enumerate(results, 1):
        rel = os.path.relpath(r["source"]) if os.path.exists(r["source"]) else r["source"]
        loc = f"{rel}:{r['start_line']}-{r['end_line']}"
        title = f"  [{r['title']}]" if r["title"] else ""
        snippet = r["text"].strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + " …(截断，可用 read 读完整内容)"
        lines.append(f"\n#{i} (score={r['score']}) {loc}{title}\n{snippet}")
    return "\n".join(lines)


# --- 命令层：供 /kb REPL 命令调用 ---


def cmd_index(path, append=False):
    """/kb index <目录> 的后端：建立或增量更新索引，返回人类可读的结果文本与统计。"""
    if not path:
        return {"ok": False, "msg": "用法: /kb index <目录或文件>"}
    if not os.path.exists(path):
        return {"ok": False, "msg": f"路径不存在: {path}"}
    info = build_index(path, append=append)
    mode = "增量更新" if append else "全量重建"
    msg = (
        f"{mode}完成：处理 {info['files']} 个文件，"
        f"共 {info['chunks']} 个片段"
        + (f"，跳过 {info['skipped']} 个未变文件" if info["skipped"] else "")
        + f"。索引已保存到 {KB_DIR}"
    )
    return {"ok": True, "msg": msg}


def cmd_stat():
    """/kb stat 的后端：返回知识库概况文本。"""
    st = stat()
    if not st.get("built"):
        return {"ok": True, "msg": "知识库尚未建立。用 /kb index <目录> 建立索引。"}
    return {
        "ok": True,
        "msg": (
            f"后端: {st['backend']} | 片段: {st['chunks']} | "
            f"来源文件: {st['files']} | 目录: {st['kb_dir']}"
        ),
    }
