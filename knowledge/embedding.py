#!/usr/bin/env python3
"""embedding - 本地 embedding（向量）后端：KB 第二期语义检索的模型侧实现。

这是什么 / 为什么独立成模块：
- 第一期的 BM25（见 kb.py）是纯标准库的关键词检索，零依赖、解压即用。第二期要
  加「语义检索」：把片段与查询都编码成向量，用余弦相似度召回「换一种说法也能命中」
  的内容。本模块是 kb.py 与底层编码引擎之间的唯一接口层，kb.py 只在需要时才
  import 它，向量路径的任何不可用都在这里收口成「优雅降级」。

- 依赖取舍（用户已拍板）：不用 sentence-transformers（会拖进 torch，几百 MB 到 1GB+，
  且模型要联网下载到 ~/.cache/huggingface，与「解压即用」冲突）。改用随包分发的
  静态词向量资产（见 static_vec.py，约 14MB，放在 knowledge/assets/）：
      * 唯一第三方依赖是 numpy，且是可选的；
      * 零网络请求、零模型下载，拿到 zip 就能用；
      * numpy 缺失或资产缺失 -> available() 为假 -> kb.py 自动回退 BM25。

- 与静态词向量的接口差异：静态词向量是对称编码（query 与 doc 走同一套管道），
  不存在 bge 系列那种「query 侧加指令前缀」的非对称检索约定，因此本模块不再有
  指令前缀逻辑；encode_docs 与 encode_query 共用同一编码路径。

对外接口（签名与第一版保持一致，kb.py 无需关心底层换没换）：
  available()            -> 编码能力是否就绪（bool）
  install_hint()         -> 未就绪时给用户看的提示文本
  model_name()           -> 当前使用的模型标识
  encode_docs(texts)     -> 批量编码文档，返回 [[float,...], ...]（已 L2 归一化）
  encode_query(text)     -> 编码单条查询，返回 [float,...]（已 L2 归一化）
  ensure_model()         -> 触发一次资产加载，返回 {ok, msg, model, dim}
  dim()                  -> 向量维度
"""

from . import static_vec


def model_name():
    """当前使用的 embedding 模型标识（随包静态资产，无可配置项）。"""
    return static_vec.model_name()


def available():
    """编码能力是否可用（numpy 已装 + 资产完整）。不触发加载，仅做探测。"""
    return static_vec.available()


def install_hint():
    """未就绪时给用户看的提示（区分缺 numpy / 缺资产两种原因）。"""
    return static_vec.install_hint()


def dim():
    """向量维度；静态资产维度固定，无需加载即可返回。"""
    return static_vec.dim()


def encode_docs(texts, batch_size=32):
    """批量编码文档片段，返回已 L2 归一化的向量列表（与输入顺序一一对应）。

    batch_size 参数保留以兼容调用方签名：静态词向量是逐条查表求均值，
    没有需要分批的显存/批处理开销，故此处忽略该参数。
    """
    return static_vec.encode(list(texts))


def encode_query(text):
    """编码单条查询，返回一维向量 list（已 L2 归一化）。

    静态词向量是对称编码，query 与 doc 走完全相同的管道，不加任何指令前缀。
    """
    return static_vec.encode([text])[0]


def ensure_model():
    """主动触发一次资产加载（供 /kb model 预热用）。返回状态 dict。"""
    if not available():
        return {"ok": False, "msg": install_hint(), "model": model_name(), "dim": dim()}
    ok, msg = static_vec.warmup()
    if not ok:
        return {"ok": False, "msg": f"{msg}\n{install_hint()}",
                "model": model_name(), "dim": None}
    return {"ok": True, "msg": msg, "model": model_name(), "dim": dim()}
