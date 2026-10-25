#!/usr/bin/env python3
"""embedding - 本地 embedding（向量）后端：KB 第二期语义检索的模型侧实现。

这是什么 / 为什么独立成模块：
- 第一期的 BM25（见 kb.py）是纯标准库的关键词检索，零依赖、解压即用。第二期要
  加「语义检索」：把片段与查询都编码成向量，用余弦相似度召回「换一种说法也能命中」
  的内容。向量能力需要一个 embedding 模型，这打破了「仅标准库」这条线，因此把所有
  第三方依赖与模型加载逻辑全部收口到本模块，kb.py 只在需要时才 import 它。

- 依赖取舍（用户已拍板方案 A）：用 sentence-transformers 加载一个小型本地 embedding
  模型（默认 BAAI/bge-small-zh-v1.5，约 95MB），首次使用时自动从 HuggingFace 下载到
  本机缓存（~/.cache/huggingface，不进项目目录）。装了库 → 向量/hybrid 后端可用；
  没装库 → kb.py 优雅回退到 BM25，保证「解压即用」的 BM25 路径永远不受影响。

- 模型可换：通过环境变量 NANOCODE_EMBED_MODEL 覆盖默认模型名。bge 系列中文检索需要
  给 query 加指令前缀，这里按模型名自动处理（见 _query_instruction）。

对外接口：
  available()            -> 第三方依赖是否就绪（bool）
  install_hint()         -> 未就绪时给用户看的安装提示文本
  model_name()           -> 当前使用的模型名
  encode_docs(texts)     -> 批量编码文档，返回 [[float,...], ...]（已 L2 归一化）
  encode_query(text)     -> 编码单条查询，返回 [float,...]（已 L2 归一化，含 bge 指令前缀）
  ensure_model()         -> 触发一次模型加载/下载，返回 {ok, msg, model, dim}
  dim()                  -> 向量维度（加载后才知道）
"""

import os

# 默认模型：中文文档检索友好、体积小（~95MB）。可用环境变量覆盖。
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

# bge 中文检索约定：query 侧需加指令前缀，doc 侧不加（显著提升非对称检索效果）。
_BGE_ZH_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
_BGE_EN_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

# 进程内缓存已加载的模型，避免每次检索都重载（模型加载是秒级开销）。
_MODEL = None
_DIM = None


def model_name():
    """当前使用的 embedding 模型名（环境变量优先）。"""
    return os.environ.get("NANOCODE_EMBED_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def available():
    """第三方依赖（sentence-transformers）是否可用。不触发模型下载，仅探测 import。"""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


def install_hint():
    """未就绪时给用户看的安装提示。"""
    return (
        "向量/hybrid 后端需要 sentence-transformers（会顺带装 torch）。安装：\n"
        "    pip install sentence-transformers\n"
        "装好后运行 /kb model 预下载模型，或直接 /kb index <目录> --backend hybrid。\n"
        "未安装时知识库自动使用零依赖的 BM25 后端。"
    )


def _load():
    """懒加载模型（首次调用会触发 HuggingFace 自动下载到 ~/.cache）。"""
    global _MODEL, _DIM
    if _MODEL is not None:
        return _MODEL
    from sentence_transformers import SentenceTransformer
    name = model_name()
    _MODEL = SentenceTransformer(name)
    try:
        _DIM = int(_MODEL.get_sentence_embedding_dimension())
    except Exception:
        _DIM = None
    return _MODEL


def dim():
    """向量维度；未加载过模型时返回 None。"""
    return _DIM


def _query_instruction(name):
    """按模型名返回 query 侧指令前缀（非 bge 模型返回空串）。"""
    low = name.lower()
    if "bge" in low:
        if "-zh" in low or "zh-" in low or low.endswith("-zh"):
            return _BGE_ZH_QUERY_INSTRUCTION
        return _BGE_EN_QUERY_INSTRUCTION
    return ""


def _to_list(vectors):
    """把模型输出（numpy / tensor）统一转成纯 Python list，便于 JSON 落盘。"""
    try:
        return vectors.tolist()
    except AttributeError:
        return [list(v) for v in vectors]


def encode_docs(texts, batch_size=32):
    """批量编码文档片段，返回已 L2 归一化的向量列表（与输入顺序一一对应）。"""
    model = _load()
    vecs = model.encode(
        list(texts),
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return _to_list(vecs)


def encode_query(text):
    """编码单条查询：自动加 bge 指令前缀、L2 归一化，返回一维向量 list。"""
    model = _load()
    prefix = _query_instruction(model_name())
    vec = model.encode(
        [prefix + text],
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return _to_list(vec)[0]


def ensure_model():
    """主动触发一次模型加载/下载（供 /kb model 预热用）。返回状态 dict。"""
    if not available():
        return {"ok": False, "msg": install_hint(), "model": model_name(), "dim": None}
    try:
        encode_query("预热")
        return {
            "ok": True,
            "msg": f"模型就绪：{model_name()}（维度 {dim()}，缓存于 ~/.cache/huggingface）",
            "model": model_name(),
            "dim": dim(),
        }
    except Exception as err:
        return {
            "ok": False,
            "msg": f"模型加载失败：{err}\n{install_hint()}",
            "model": model_name(),
            "dim": None,
        }
