#!/usr/bin/env python3
"""static_vec - 随包分发的静态词向量引擎：零模型下载、零重依赖的语义编码底座。

这是什么 / 为什么这样设计：
- KB 第二期要做语义检索，就得把文本变成向量。常规做法是装 sentence-transformers
  （顺带拖进 torch，几百 MB 到 1GB+），再从 HuggingFace 下载模型到 ~/.cache。这与
  nanocode「解压即用、随包分发」的铁律直接冲突：团队成员拿到 zip 还得联网装环境。

- 本模块换了一条路：静态词向量（model2vec 范式）。它把一个句向量模型蒸馏成一张
  「token -> 向量」的查表矩阵，推理退化成三步纯算术：
      Unigram 切分 -> 取出每个 token 的向量求均值 -> L2 归一化
  没有 Transformer 前向、没有注意力、没有 GPU，只需要一次矩阵行索引和一次求均值。
  代价是丢失词序与上下文（"猫追狗" 与 "狗追猫" 同向量），但对「文档片段召回」这种
  袋式语义匹配场景足够用，且配合 kb.py 的 hybrid+RRF 融合 BM25 可以互补短板。

- 资产来自 minishlab/potion-multilingual-128M，经两步瘦身后随包分发（见 assets/）：
      1) 词表裁剪：保留「前 30000 条高频 token」∪「全部含 CJK 的 token」= 51166 条
         （原 500353 条，大量是本项目用不到的其他语种子词）
      2) int8 量化：按行取 scale（|row|.max()/127），float32 -> int8，体积再降 4 倍
  最终 14MB，实测与全量 f32 的打分相关性 0.958，中文检索质量无明显差异。

- 唯一的第三方依赖是 numpy，且是**可选**的：没装 numpy -> available() 返回 False
  -> kb.py 自动回退纯标准库 BM25，解压即用的路径永不受影响。不用纯 Python 做矩阵
  运算是因为 51166x256 的 int8 解码与均值在纯 Python 下是秒级开销，不可接受。

资产文件（knowledge/assets/，三个文件合计约 14MB）：
    vocab.tsv   每行 `piece\\tlog_score`，行号即 token id（51166 行，UTF-8）
    emb.i8      [51166, 256] int8 原始字节，C order，无头部
    scale.f32   [51166] float32 原始字节，第 i 个是 emb.i8 第 i 行的反量化系数

对外接口：
    available()        -> numpy 与资产文件是否都就绪（bool）
    install_hint()     -> 未就绪时给用户看的提示文本
    model_name()       -> 静态模型标识
    dim()              -> 向量维度（256，无需加载即可知）
    encode(texts)      -> 批量编码，返回 [[float,...], ...]（已 L2 归一化）
    warmup()           -> 主动加载一次资产，返回 (ok, msg)
"""

import os
import unicodedata

# 资产与本模块同级的 assets/ 目录下，随包分发、与 cwd 无关。
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
VOCAB_FILE = os.path.join(_ASSET_DIR, "vocab.tsv")
EMB_FILE = os.path.join(_ASSET_DIR, "emb.i8")
SCALE_FILE = os.path.join(_ASSET_DIR, "scale.f32")

MODEL_NAME = "potion-multilingual-128M-int8-pruned"
DIM = 256
UNK_ID = 1  # 与原 tokenizer 一致：id 0 = [PAD]，id 1 = [UNK]

# 进程内缓存（资产加载是百毫秒级 + 14MB 内存，只做一次）。
_TOKENIZER = None
_MATRIX = None
_SCALE = None


class Unigram:
    """极简 SentencePiece-Unigram 分词器（Viterbi 最优切分），纯标准库实现。

    为什么自己写：官方实现在 tokenizers 库里（Rust 扩展，需要编译/预编译轮子），
    而 Unigram 推理本身只是一个「最大化累计 log 概率的动态规划」，六十行就能写完。
    原 tokenizer 的 Precompiled normalizer 这里用 unicodedata.NFKC 近似替代，
    中英文实测无差异。
    """

    def __init__(self, vocab, unk_id=UNK_ID):
        self.unk_id = unk_id
        self.piece2id = {}
        self.score = {}
        self.maxlen = 1
        for i, (piece, sc) in enumerate(vocab):
            if piece in self.piece2id:
                continue  # 同一 piece 只认第一次出现的 id
            self.piece2id[piece] = i
            self.score[piece] = sc
            if len(piece) > self.maxlen:
                self.maxlen = len(piece)
        # 未登录单字符的惩罚分：比最差已知 piece 再低一截，保证只在无路可走时才选它
        self.unk_score = min(self.score.values()) - 10.0 if self.score else -100.0

    def normalize(self, text):
        """NFKC 规范化 + 空白折叠 + 词首 Metaspace 标记（▁），对齐原 pre_tokenizer。"""
        t = unicodedata.normalize("NFKC", text)
        t = " ".join(t.split())
        return "\u2581" + t.replace(" ", "\u2581")

    def encode(self, text):
        """返回 token id 列表；Viterbi 前向 DP 求最大 log-score 切分后回溯。"""
        s = self.normalize(text)
        n = len(s)
        if n == 0:
            return []
        best = [-1e18] * (n + 1)   # best[i]：覆盖 s[:i] 的最优累计分
        back = [(0, self.unk_id)] * (n + 1)  # back[i]：(前驱位置, 该段 token id)
        best[0] = 0.0
        for i in range(n):
            if best[i] <= -1e17:
                continue  # 不可达位置，跳过
            lim = min(self.maxlen, n - i)
            found = False
            for length in range(lim, 0, -1):
                piece = s[i:i + length]
                sc = self.score.get(piece)
                if sc is None:
                    continue
                found = True
                value = best[i] + sc
                j = i + length
                if value > best[j]:
                    best[j] = value
                    back[j] = (i, self.piece2id[piece])
            if not found:
                # 当前位置开头的任何长度都不在词表里 -> 吃掉一个字符记为 UNK
                j = i + 1
                value = best[i] + self.unk_score
                if value > best[j]:
                    best[j] = value
                    back[j] = (i, self.unk_id)
        ids = []
        j = n
        while j > 0:
            i, tid = back[j]
            ids.append(tid)
            j = i
        ids.reverse()
        return ids


def model_name():
    """静态模型标识（写入 KB config.json，便于审计索引与查询是否同源）。"""
    return MODEL_NAME


def dim():
    """向量维度；静态资产维度固定，无需加载即可返回。"""
    return DIM


def assets_ready():
    """三个资产文件是否都在（打包遗漏/被清理时据此降级）。"""
    return all(os.path.exists(p) for p in (VOCAB_FILE, EMB_FILE, SCALE_FILE))


def _numpy():
    """按需 import numpy；未安装返回 None（不抛异常，交由调用方降级）。"""
    try:
        import numpy
        return numpy
    except Exception:
        return None


def available():
    """语义编码是否可用：numpy 已装 + 资产完整。不触发加载，仅做探测。"""
    return _numpy() is not None and assets_ready()


def install_hint():
    """未就绪时给用户看的提示。区分「缺 numpy」与「缺资产」两种原因。"""
    if not assets_ready():
        missing = [os.path.basename(p) for p in (VOCAB_FILE, EMB_FILE, SCALE_FILE)
                   if not os.path.exists(p)]
        return (
            f"向量资产缺失：knowledge/assets/ 下找不到 {', '.join(missing)}。\n"
            "请重新解压完整发布包，或运行 tools/build_static_vec.py 重新生成。\n"
            "资产缺失时知识库自动使用零依赖的 BM25 后端。"
        )
    return (
        "向量/hybrid 后端需要 numpy（仅此一个依赖，约 15MB，不需要 torch）。安装：\n"
        "    pip install numpy\n"
        "装好后直接 /kb index <目录> --backend hybrid 即可。\n"
        "未安装时知识库自动使用零依赖的 BM25 后端。"
    )


def _load_vocab():
    """读 vocab.tsv -> [(piece, score), ...]，行号即 id。"""
    vocab = []
    with open(VOCAB_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            piece, _, score = line.partition("\t")
            try:
                vocab.append((piece, float(score)))
            except ValueError:
                vocab.append((piece, -100.0))
    return vocab


def _load():
    """懒加载分词器与 int8 矩阵；失败抛异常，由调用方转成回退。"""
    global _TOKENIZER, _MATRIX, _SCALE
    if _TOKENIZER is not None:
        return _TOKENIZER, _MATRIX, _SCALE
    np = _numpy()
    if np is None:
        raise RuntimeError("numpy 未安装")
    if not assets_ready():
        raise RuntimeError("向量资产文件缺失")
    vocab = _load_vocab()
    n = len(vocab)
    matrix = np.fromfile(EMB_FILE, dtype=np.int8)
    scale = np.fromfile(SCALE_FILE, dtype=np.float32)
    if matrix.size != n * DIM or scale.size != n:
        raise RuntimeError(
            f"资产尺寸不匹配：vocab {n} 行，emb {matrix.size} 字节，scale {scale.size} 项"
        )
    _MATRIX = matrix.reshape(n, DIM)
    _SCALE = scale.reshape(n, 1)
    _TOKENIZER = Unigram(vocab)
    return _TOKENIZER, _MATRIX, _SCALE


def _embed_ids(ids, np, matrix, scale):
    """一组 token id -> 单条归一化向量；空输入返回全零向量。"""
    if not ids:
        return np.zeros(DIM, dtype=np.float32)
    rows = np.asarray(ids, dtype=np.int64)
    # int8 行先乘回各自 scale 反量化，再按 token 求均值（model2vec 的 mean pooling）
    vec = (matrix[rows].astype(np.float32) * scale[rows]).mean(axis=0)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec


def encode(texts):
    """批量编码文本，返回已 L2 归一化的纯 list 向量（顺序与输入一致）。"""
    tok, matrix, scale = _load()
    np = _numpy()
    out = []
    for text in texts:
        ids = tok.encode(text or "")
        out.append([float(x) for x in _embed_ids(ids, np, matrix, scale)])
    return out


def warmup():
    """主动加载一次资产（供 /kb model 预热）。返回 (ok: bool, msg: str)。"""
    try:
        encode(["预热"])
    except Exception as err:
        return False, f"向量引擎加载失败：{err}"
    size_mb = sum(os.path.getsize(p) for p in (VOCAB_FILE, EMB_FILE, SCALE_FILE)) / 1048576.0
    return True, (
        f"向量引擎就绪：{MODEL_NAME}（维度 {DIM}，"
        f"本地资产 {size_mb:.1f}MB，随包分发、无需下载）"
    )
