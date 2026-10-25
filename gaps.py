#!/usr/bin/env python3
"""gaps - 知识缺口记录模块：当知识库(kb_search)也查不到用户所需信息时，把这个
「未被覆盖的问题/缺口」记录下来，用于后续持续完善 agent 与知识库。

为什么要有它（产品动机）：
- agent 的能力边界 = 术语表 + 知识库覆盖面。用户问到、而知识库答不出的点，
  正是「知识库尚未覆盖 / 信息质量不高」的信号。把这些缺口收集起来，就能知道
  该优先补哪些语料、哪些口径需要澄清，对迭代 agent 价值极高。

设计（与 changelog.py 一致的零依赖 JSONL 追加）：
- 本地：所有缺口追加写入脚本同级 .nanocode_gaps/gaps.jsonl，每行一条。
- 可选对象存储：若配置了环境变量（见下），每条缺口同时尝试上传到对象存储，
  便于跨用户集中收集。上传失败不影响本地记录（best-effort、静默降级）。

对象存储上传采用「可选、解耦」的方式：
- 仅当设置了 GAP_UPLOAD_URL 环境变量时才尝试上传（HTTP PUT/POST 一个 JSON）。
  这样默认零依赖、零外部调用；需要集中收集时，运维配一个接收端点即可，
  无需给本工具引入任何云厂商 SDK。
- 若你用的是 S3/OSS 等需要签名的对象存储，推荐在中间放一个薄接收服务
  （或预签名 URL），把 GAP_UPLOAD_URL 指过去，职责解耦、避免把密钥塞进本工具。

隐私提示：缺口内容可能包含用户对话片段。是否上传、上传到哪、保留多久，
应由团队的数据合规策略决定。默认**不**上传（未配 GAP_UPLOAD_URL 即纯本地）。
"""

import datetime
import json
import os
import urllib.request

# 缺口记录存放在脚本同级 .nanocode_gaps/ 目录（与 cwd 无关，便于集中管理）
GAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nanocode_gaps")
GAPS_FILE = os.path.join(GAPS_DIR, "gaps.jsonl")

# 可选：对象存储/接收端点。仅当设置了该环境变量时才尝试上传（HTTP）。
GAP_UPLOAD_URL = os.environ.get("GAP_UPLOAD_URL")
# 可选：上传鉴权头（如 "Bearer xxx"），接收端需要时配置。
GAP_UPLOAD_AUTH = os.environ.get("GAP_UPLOAD_AUTH")


def _ensure_dir():
    """懒创建 .nanocode_gaps/ 目录"""
    os.makedirs(GAPS_DIR, exist_ok=True)


def _count():
    """已记录的缺口条数（= 下一条编号），文件不存在则为 0"""
    if not os.path.exists(GAPS_FILE):
        return 0
    with open(GAPS_FILE, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _try_upload(entry):
    """best-effort 把一条缺口上传到对象存储/接收端点。

    仅当配置了 GAP_UPLOAD_URL 时才执行；任何失败都静默吞掉（不影响本地记录）。
    返回 True 表示上传成功，False 表示未配置或失败。
    """
    if not GAP_UPLOAD_URL:
        return False
    try:
        headers = {"Content-Type": "application/json"}
        if GAP_UPLOAD_AUTH:
            headers["Authorization"] = GAP_UPLOAD_AUTH
        req = urllib.request.Request(
            GAP_UPLOAD_URL,
            data=json.dumps(entry, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def record_gap(args):
    """record_gap 工具入口：记录一个知识缺口。

    入参（dict）：
      question  -- 用户想问 / 需要但知识库未覆盖的问题（必填）
      context?  -- 相关上下文（如已尝试的检索词、用户的具体场景）
      note?     -- agent 的补充说明（如「口径不清」「文档缺失」）

    行为：本地追加落盘；若配置了对象存储则同时尝试上传。返回给模型的提示文本。
    """
    question = (args.get("question") or "").strip()
    if not question:
        return "error: question 不能为空（请说明知识库未覆盖的问题）"
    _ensure_dir()
    number = _count()
    entry = {
        "number": number,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "question": question,
        "context": (args.get("context") or "").strip(),
        "note": (args.get("note") or "").strip(),
    }
    # 本地追加（主路径，必须成功）
    with open(GAPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # 可选上传（副路径，失败不影响）
    uploaded = _try_upload(entry)
    tail = "，并已上传对象存储" if uploaded else ""
    return (
        f"已记录知识缺口 #{number}{tail}。"
        f"该问题当前知识库未覆盖，已登记供后续完善。"
    )


def list_gaps():
    """返回所有缺口记录列表（供 /gaps 命令展示）"""
    if not os.path.exists(GAPS_FILE):
        return []
    entries = []
    with open(GAPS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def stat():
    """统计信息：{dir, count, upload_enabled}"""
    return {
        "dir": GAPS_DIR,
        "count": _count(),
        "upload_enabled": bool(GAP_UPLOAD_URL),
    }
