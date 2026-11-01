# knowledge/assets —— 静态词向量资产

本目录存放 nanocode 知识库 `vector` / `hybrid` 后端使用的**随包静态词向量**。
加载与推理代码见上一级目录的 `static_vec.py`，接口层见 `embedding.py`。

## 为什么是「静态词向量」而不是 sentence-transformers

nanocode 的分发前提是「解压即用、Python 3.8+、核心零第三方依赖」。
`sentence-transformers` 会拖进 torch（几百 MB 到 1GB+），且模型要联网下载到
`~/.cache/huggingface`，与该前提直接冲突。

静态词向量（model2vec 一类）把 Transformer 蒸馏成一张**查表矩阵**，推理只需三步：

1. Unigram 分词得到 token id 序列；
2. `E[ids].mean(axis=0)` 取词向量均值；
3. L2 归一化。

没有任何神经网络前向，因此不需要 torch / transformers / tokenizers / safetensors
这些库，唯一的额外依赖是 numpy（而且是可选的，缺失时 kb.py 自动回退 BM25）。

## 文件清单

| 文件 | 大小 | 格式 |
|------|------|------|
| `vocab.tsv` | ~854 KB | 51,166 行，每行 `piece\tscore`（Unigram log 概率），**行序即 token id** |
| `emb.i8` | ~13.1 MB | `[51166, 256]` int8 裸数组，C order，无 header |
| `scale.f32` | ~205 KB | `[51166, 1]` float32 裸数组，per-row 反量化系数 |

还原为 float32 矩阵：

```python
M_f32 = emb_i8.astype(np.float32) * scale   # scale 广播到 256 列
```

约定：`[PAD]` = id 0，`[UNK]` = id 1（`UNK_ID`）；维度 `DIM = 256`。

## 资产是怎么生成的

### 1. 下载原始模型

来源：[`minishlab/potion-multilingual-128M`](https://huggingface.co/minishlab/potion-multilingual-128M)
（model2vec 静态词向量，多语言，`tokenizer_name = BAAI/bge-m3`）。

```bash
# 直连 huggingface.co 常不可达，走镜像
BASE=https://hf-mirror.com/minishlab/potion-multilingual-128M/resolve/main
curl -L -o model.safetensors "$BASE/model.safetensors"
curl -L -o tokenizer.json   "$BASE/tokenizer.json"
curl -L -o config.json      "$BASE/config.json"
```

原始 `model.safetensors` 只含一个张量 `embeddings`，shape `[500353, 256]`，F32，约 489 MB。

### 2. 解析 safetensors（无需第三方库）

safetensors 的结构是「8 字节小端 header 长度 + JSON header + 连续数据区」：

```python
import json, struct, numpy as np

with open("model.safetensors", "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    header = json.loads(f.read(n))
    meta = header["embeddings"]
    off0, off1 = meta["data_offsets"]
    f.seek(8 + n + off0)
    raw = f.read(off1 - off0)
matrix = np.frombuffer(raw, dtype=np.float32).reshape(meta["shape"])
```

### 3. 词表裁剪

从 `tokenizer.json` 取出 `model.vocab`（`[[piece, score], ...]`，顺序即 id），
按以下规则保留，得到 **51,166** 条：

```
保留集合 = 前 30,000 个高频 token（原 vocab 前 30000 行）
         ∪ 全部包含 CJK 字符的 token（23,530 条）
```

目标场景是「中文为主、1000~5000 片段」的小型知识库，裁掉大量低频多语言 token
对质量影响很小（见下方评测），但把矩阵从 489 MB 压到 52 MB。

裁剪后**直接用新 vocab 重建 Unigram 即可**，不需要维护 old2new id 映射，
因为 Unigram 解码只依赖 `piece -> score` 与最长匹配长度。

### 4. int8 量化

per-row（每个 token 一个系数）对称量化：

```python
sc = np.abs(M).max(axis=1, keepdims=True) / 127.0
sc[sc == 0] = 1.0                      # 避免除零（全零行）
emb_i8 = np.round(M / sc).astype(np.int8)
```

写出 `emb_i8.tobytes()` 与 `sc.astype(np.float32).tobytes()` 即为 `emb.i8` / `scale.f32`。

### 5. 分词器移植要点

`static_vec.py` 里的 `Unigram` 类复刻了 bge-m3 tokenizer 的关键行为：

- normalizer：原始是 `Precompiled`（SentencePiece 预编译的字符映射表），
  这里用 `unicodedata.normalize("NFKC")` **近似替代**，再折叠连续空白。
  中英文实测无差异，属已知可接受的近似。
- pre_tokenizer：`Metaspace`（`prepend_scheme=always`, `split=false`）——
  文本前置 `▁`（U+2581）并把空格替换为 `▁`。
- 解码：Viterbi 动态规划求最大 log 概率切分；无法匹配时回退 `[UNK]`，
  `unk_score = min(all_scores) - 10.0`。
- `byte_fallback = false`，所以不做字节级兜底。

## 质量评测（10 条中英混合 query，判定 Top-1 是否命中）

| 变体 | 体积 | Top-1 | 与全量打分相关性 |
|------|------|-------|------------------|
| full-f32 | 489 MB | 8/10 | 1.000 |
| pruned-f32 | 52 MB | 7/10 | 0.958 |
| pruned-f16 | 26 MB | 7/10 | 0.958 |
| **pruned-int8（当前采用）** | **13 MB** | **7/10** | **0.958** |
| pca128-f32 | 26 MB | 8/10 | 0.811 |
| pca128-int8 | 7 MB | 8/10 | 0.811 |

结论：

- int8 量化相对 f32 **几乎无损**（相关性完全一致），是性价比最高的一档。
- PCA 降到 128 维只保留 75.3% 方差，与全量打分相关性掉到 0.811，已弃用。
- 实测加载耗时约 0.04 s；分词速度约 316 字符 / 0.9 ms，3000 片段建索引约 2.8 s。

## 使用须知

绝对相似度分数区间偏低（中文约 0.26~0.53，英文可到 0.64），
**只有相对排序有意义，不要设绝对分数阈值做过滤**。
这也是知识库默认推荐 `hybrid`（BM25 + 向量用 RRF 融合）而非纯 `vector` 的原因。
