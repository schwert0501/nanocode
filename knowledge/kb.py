#!/usr/bin/env python3
"""kb - 领域知识库模块：纯标准库 BM25 检索，零第三方依赖。

这是什么 / 为什么这样设计：
- 让 nanocode 具备"领域知识库"能力：把一批文档（Markdown / 代码等）切成片段、
  建立 BM25 关键词索引，模型在回答领域问题前可以先调用 kb_search 工具检索，
  拿到相关片段（含文件路径+行号）后再决定是否 read 原文。这叫 agentic search：
  "由 agent 决定何时搜、搜几次"，BM25 只是 kb_search 的内部检索后端，二者正交。

- 两期检索后端（通过 config.json 的 backend 字段切换）：
  * 第一期 bm25（默认）：稀疏/关键词检索，只数词频，用标准库 collections+math 手写
    约几十行即可，零依赖、不联网、解压即用。适合关键词/专有名词/代码符号精确命中。
  * 第二期 vector / hybrid（可选，方案 A）：静态词向量做稠密/语义检索，
    "换一种说法也能召回"。模型侧全部收口在 embedding.py / static_vec.py：向量资产
    （约 14MB）随包放在 knowledge/assets/，零下载零联网，唯一可选依赖是 numpy。
    hybrid 用 RRF 融合 BM25 与向量结果。
  * 关键回退：向量后端需要 numpy 与资产文件；任一缺失时本模块自动降级到 bm25，
    保证"解压即用"的 BM25 路径永不受影响（见 _resolve_backend / search）。

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
  config.json    —— 后端类型与分块参数（backend=bm25/vector/hybrid、chunk 配置、bm25 超参、embedding 模型名）
  chunks.jsonl   —— 每行一个片段：{id, source, start_line, end_line, title, text}
  index.json     —— BM25 索引：df（文档频率）、每片段的词频与长度、全局统计
  sources.json   —— 已索引文件清单及 mtime（支持 /kb index 增量重建，跳过未变文件）
  vectors.jsonl  —— （vector/hybrid 后端才有）每行一个片段向量：{id, vec}，已 L2 归一化
"""

import json
import math
import os
import re
from collections import Counter, defaultdict

from . import embedding

# 知识库统一存放在项目根目录 .nanocode_kb/（分发后随包迁移、与 cwd 无关）。
# 本模块位于 knowledge/ 子包，需上溯一层到项目根。
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(_BASE_DIR, ".nanocode_kb")
CONFIG_FILE = os.path.join(KB_DIR, "config.json")
CHUNKS_FILE = os.path.join(KB_DIR, "chunks.jsonl")
INDEX_FILE = os.path.join(KB_DIR, "index.json")
SOURCES_FILE = os.path.join(KB_DIR, "sources.json")
VECTORS_FILE = os.path.join(KB_DIR, "vectors.jsonl")

# 检索后端：bm25（默认，零依赖）/ vector（纯语义）/ hybrid（BM25+向量 RRF 融合）。
# 默认仍是 bm25，保证"解压即用"路径不变；向量后端需要可选依赖 numpy（见 embedding.py）。
DEFAULT_BACKEND = "bm25"
VALID_BACKENDS = ("bm25", "vector", "hybrid")
# hybrid 融合用的 RRF 常数：名次 r 的贡献为 1/(RRF_K + r)，K 越大越平滑。
RRF_K = 60

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


def build_index(root, append=False, backend=None):
    """扫描 root 下文本文件，分块并构建索引，落盘到 .nanocode_kb/。

    append=False 全量重建；append=True 在已有索引基础上增量合并（跳过 mtime 未变的文件）。
    backend=None 时沿用已有 config 的后端（没有则默认 bm25）；也可显式传 bm25/vector/hybrid。
    无论哪种后端都会构建 BM25 索引（hybrid 需要、也让回退有依据）；vector/hybrid 额外编码向量。
    返回统计 dict：{files, chunks, skipped, backend, vectors}。
    """
    _ensure_dir()
    # 决定本次使用的后端：显式参数优先，否则沿用已有配置
    if backend is None:
        backend = (_load_config() or {}).get("backend", DEFAULT_BACKEND)
    if backend not in VALID_BACKENDS:
        backend = DEFAULT_BACKEND

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

    # vector/hybrid：编码全部片段向量并落盘。依赖/资产缺失或编码失败时回退 bm25 并给出原因。
    n_vectors = 0
    emb_model = None
    emb_dim = None
    effective_backend = backend
    vector_note = ""
    if backend in ("vector", "hybrid"):
        if not embedding.available():
            effective_backend = "bm25"
            vector_note = "（向量编码不可用，已回退 bm25；运行 /kb model 查看原因与解决方法）"
        else:
            try:
                vecs = embedding.encode_docs([c["text"] for c in all_chunks])
                _save_vectors(all_chunks, vecs)
                n_vectors = len(vecs)
                emb_model = embedding.model_name()
                emb_dim = embedding.dim()
            except Exception as err:
                effective_backend = "bm25"
                vector_note = f"（向量编码失败已回退 bm25：{err}）"
    if effective_backend == "bm25":
        _clear_vectors()

    _save_config(effective_backend, emb_model, emb_dim)
    return {
        "files": len(changed_files),
        "chunks": len(all_chunks),
        "skipped": skipped,
        "backend": effective_backend,
        "vectors": n_vectors,
        "note": vector_note,
    }


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


def _save_vectors(chunks, vecs):
    """向量以 JSONL 落盘（每行 {id, vec}），与片段一一对应、便于逐行加载。"""
    _ensure_dir()
    with open(VECTORS_FILE, "w", encoding="utf-8") as f:
        for c, v in zip(chunks, vecs):
            f.write(json.dumps({"id": c["id"], "vec": v}, ensure_ascii=False) + "\n")


def _load_vectors():
    """读回 {doc_id: [float,...]}；文件不存在返回空 dict。"""
    if not os.path.exists(VECTORS_FILE):
        return {}
    out = {}
    try:
        with open(VECTORS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                out[row["id"]] = row["vec"]
    except Exception:
        return {}
    return out


def _clear_vectors():
    """删除向量文件（后端回退 bm25 时调用，避免残留旧向量误导检索）。"""
    try:
        if os.path.exists(VECTORS_FILE):
            os.remove(VECTORS_FILE)
    except OSError:
        pass


def _save_config(backend=DEFAULT_BACKEND, emb_model=None, emb_dim=None):
    """记录本次索引所用的后端与参数，便于审计与运行期后端分发。"""
    _ensure_dir()
    config = {
        "backend": backend,
        "bm25": {"k1": BM25_K1, "b": BM25_B},
        "chunk": {"max_chars": CHUNK_MAX_CHARS, "overlap": CHUNK_OVERLAP},
        "tokenizer": "en-word + cjk-bigram",
    }
    if backend in ("vector", "hybrid"):
        config["embedding"] = {"model": emb_model, "dim": emb_dim}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_backend():
    """返回运行期实际可用的后端：配置要向量但库不可用/向量缺失时回退 bm25。"""
    cfg = _load_config() or {}
    backend = cfg.get("backend", DEFAULT_BACKEND)
    if backend in ("vector", "hybrid"):
        if not embedding.available() or not os.path.exists(VECTORS_FILE):
            return "bm25"
    return backend


def _load_index():
    if not os.path.exists(INDEX_FILE):
        return None
    with open(INDEX_FILE, encoding="utf-8") as f:
        return json.load(f)


# --- 检索 ---


def search(query, k=5):
    """按已配置后端检索 top-k 片段（含来源路径+行号+标题+文本）。

    后端由 _resolve_backend() 决定：
      bm25   —— 纯关键词检索（_bm25_search）
      vector —— 纯语义向量检索（_vector_search）
      hybrid —— BM25 与向量各出排名，用 RRF 融合（_hybrid_search）
    若配置要向量但依赖不可用或向量缺失，_resolve_backend 会回退到 bm25。
    """
    q = (query or "").strip()
    if not q:
        return []
    backend = _resolve_backend()
    if backend == "vector":
        ranked = _vector_search(q, k)
    elif backend == "hybrid":
        ranked = _hybrid_search(q, k)
    else:
        ranked = _bm25_search(q, k)
    if not ranked:
        return []

    chunks = list(_load_chunks())
    results = []
    for score, doc_id in ranked:
        if doc_id < 0 or doc_id >= len(chunks):
            continue
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


def _bm25_search(query, k):
    """BM25 打分，返回 [(score, doc_id), ...]（已按分数降序、取 top-k）。

    BM25 公式（对每个查询 term 求和）：
      idf(t) = ln( (N - df + 0.5) / (df + 0.5) + 1 )
      score += idf(t) * (f * (k1+1)) / (f + k1*(1 - b + b*dl/avgdl))
    其中 f 是该 term 在片段里的词频、dl 是片段长度、avgdl 是平均长度。
    """
    index = _load_index()
    if not index or index.get("n_docs", 0) == 0:
        return []
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

    return sorted(
        [(s, i) for i, s in enumerate(scores) if s > 0],
        key=lambda x: x[0],
        reverse=True,
    )[:k]


def _vector_search(query, k):
    """语义向量检索：编码查询后与各片段向量算点积（均 L2 归一化=余弦），取 top-k。

    返回 [(cosine, doc_id), ...]（降序）。向量缺失或编码失败时返回空。
    片段上千条时纯 Python 双层循环会到百毫秒级，故优先走 numpy 矩阵乘，
    numpy 不可用时回退纯 Python（功能等价，只是慢）。
    """
    vectors = _load_vectors()
    if not vectors:
        return []
    try:
        qv = embedding.encode_query(query)
    except Exception:
        return []
    dim = len(qv)
    doc_ids = [d for d, v in vectors.items() if len(v) == dim]
    if not doc_ids:
        return []
    try:
        import numpy as np
    except Exception:
        np = None
    if np is not None:
        matrix = np.asarray([vectors[d] for d in doc_ids], dtype=np.float32)
        scores = matrix @ np.asarray(qv, dtype=np.float32)
        top = min(k, len(doc_ids))
        # argpartition 只做 top-k 部分排序，比全量 sort 更省；再对这 k 个精排
        idx = np.argpartition(-scores, top - 1)[:top]
        idx = idx[np.argsort(-scores[idx])]
        return [(float(scores[i]), doc_ids[i]) for i in idx]
    scored = []
    for doc_id in doc_ids:
        vec = vectors[doc_id]
        dot = 0.0
        for a, b in zip(qv, vec):
            dot += a * b
        scored.append((dot, doc_id))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def _hybrid_search(query, k):
    """混合检索：BM25 与向量各自出排名，用 RRF（Reciprocal Rank Fusion）融合。

    RRF：对每路结果，第 rank 名（从 0 起）贡献 1/(RRF_K+rank+1)，两路相加得融合分。
    这样无需对 BM25 分数与余弦做量纲对齐，鲁棒且实现简单。取融合分 top-k。
    """
    # 两路各多取一些候选，融合后再截断，避免边界漏召回
    pool = max(k * 3, k)
    bm25 = _bm25_search(query, pool)
    vec = _vector_search(query, pool)
    if not vec:
        # 向量不可用时退化为 BM25
        return bm25[:k]

    fused = defaultdict(float)
    for rank, (_s, doc_id) in enumerate(bm25):
        fused[doc_id] += 1.0 / (RRF_K + rank + 1)
    for rank, (_s, doc_id) in enumerate(vec):
        fused[doc_id] += 1.0 / (RRF_K + rank + 1)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]
    return [(score, doc_id) for doc_id, score in ranked]


def stat():
    """返回知识库概况：是否已建、片段数、来源文件数、后端类型与模型信息。"""
    index = _load_index()
    sources = _load_sources()
    if not index:
        return {"built": False}
    cfg = _load_config() or {}
    configured = cfg.get("backend", DEFAULT_BACKEND)
    effective = _resolve_backend()
    out = {
        "built": True,
        "backend": configured,
        "effective_backend": effective,
        "chunks": index.get("n_docs", 0),
        "files": len(sources),
        "kb_dir": KB_DIR,
    }
    emb = cfg.get("embedding") or {}
    if emb:
        out["model"] = emb.get("model")
        out["dim"] = emb.get("dim")
    if os.path.exists(VECTORS_FILE):
        out["vectors"] = len(_load_vectors())
    return out


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


def cmd_index(path, append=False, backend=None):
    """/kb index <目录> 的后端：建立或增量更新索引，返回人类可读的结果文本与统计。

    backend 可选 bm25/vector/hybrid；None 时沿用已有配置（首次为 bm25）。
    """
    if not path:
        return {"ok": False, "msg": "用法: /kb index <目录或文件> [--backend bm25|vector|hybrid]"}
    if not os.path.exists(path):
        return {"ok": False, "msg": f"路径不存在: {path}"}
    if backend is not None and backend not in VALID_BACKENDS:
        return {"ok": False, "msg": f"未知后端 '{backend}'，可选: {', '.join(VALID_BACKENDS)}"}
    info = build_index(path, append=append, backend=backend)
    mode = "增量更新" if append else "全量重建"
    msg = (
        f"{mode}完成：处理 {info['files']} 个文件，"
        f"共 {info['chunks']} 个片段"
        + (f"，跳过 {info['skipped']} 个未变文件" if info["skipped"] else "")
        + f"。后端: {info['backend']}"
        + (f"（向量 {info['vectors']} 条）" if info["vectors"] else "")
        + (info["note"] or "")
        + f"。索引已保存到 {KB_DIR}"
    )
    return {"ok": True, "msg": msg}


def cmd_stat():
    """/kb stat 的后端：返回知识库概况文本。"""
    st = stat()
    if not st.get("built"):
        return {"ok": True, "msg": "知识库尚未建立。用 /kb index <目录> 建立索引。"}
    parts = [
        f"后端: {st['backend']}",
    ]
    if st.get("effective_backend") and st["effective_backend"] != st["backend"]:
        parts.append(f"(实际: {st['effective_backend']})")
    parts.append(f"片段: {st['chunks']}")
    parts.append(f"来源文件: {st['files']}")
    if st.get("model"):
        parts.append(f"模型: {st['model']}" + (f"/{st['dim']}维" if st.get("dim") else ""))
    if st.get("vectors"):
        parts.append(f"向量: {st['vectors']}")
    parts.append(f"目录: {st['kb_dir']}")
    return {"ok": True, "msg": " | ".join(parts)}


def cmd_model():
    """/kb model 的后端：主动加载向量资产并返回状态（无需联网下载）。"""
    res = embedding.ensure_model()
    return {"ok": res["ok"], "msg": res["msg"]}
