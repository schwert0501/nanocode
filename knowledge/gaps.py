#!/usr/bin/env python3
"""gaps - 知识缺口记录模块：当知识库(kb_search)也查不到用户所需信息时，把这个
「未被覆盖的问题/缺口」记录下来，用于后续持续完善 agent 与知识库。

为什么要有它（产品动机）：
- agent 的能力边界 = 术语表 + 知识库覆盖面。用户问到、而知识库答不出的点，
  正是「知识库尚未覆盖 / 信息质量不高」的信号。把这些缺口收集起来，就能知道
  该优先补哪些语料、哪些口径需要澄清，对迭代 agent 价值极高。

设计（与 changelog.py 一致的零依赖 JSONL 追加）：
- 本地：所有缺口追加写入脚本同级 .nanocode_gaps/gaps.jsonl，每行一条。
- 可选对象存储：若配置了凭据（见下），每条缺口同时尝试上传，便于跨用户集中收集。
  上传失败不影响本地记录（best-effort、静默降级）。

对象存储上传支持两种「可选」通道，按优先级尝试，任一成功即算上传成功：
1. 百度对象存储 BOS（knowledge/bos.py，纯标准库签名）——配置 BOS_ACCESS_KEY /
   BOS_SECRET_KEY / BOS_BUCKET（或项目根 .nanocode_bos.json）即启用，
   对象写入 `problem/` 目录下。
2. 自建接收端点——设置 GAP_UPLOAD_URL 环境变量即启用（HTTP POST 一个 JSON）。
   适合不想把密钥放到客户端、而是在中间放一层薄接收服务的场景。

两者都未配置时即纯本地记录，零外部调用。

隐私提示：缺口内容可能包含用户对话片段。是否上传、上传到哪、保留多久，
应由团队的数据合规策略决定。默认**不**上传（未配任何凭据即纯本地）。
"""

import datetime
import json
import os
import urllib.request

from knowledge import bos

# 缺口记录存放在项目根目录 .nanocode_gaps/（与 cwd 无关，便于集中管理）。
# 本模块位于 knowledge/ 子包，需上溯一层到项目根。
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAPS_DIR = os.path.join(_BASE_DIR, ".nanocode_gaps")
GAPS_FILE = os.path.join(GAPS_DIR, "gaps.jsonl")

# 对象存储中的目标目录前缀（用户约定：缺口记录放 problem/ 下）
GAP_PREFIX = "problem"

# 可选：自建接收端点（备用通道）。仅当设置了该环境变量时才尝试 HTTP 上传。
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


def _object_key(entry):
    """按日期分目录生成对象键：problem/YYYY-MM-DD/gap-<编号>-<时间>.json

    加日期子目录便于在控制台按天浏览；文件名带编号与时分秒，避免同秒覆盖。
    """
    day = entry["time"][:10]
    stamp = entry["time"][11:].replace(":", "")
    return "%s/%s/gap-%04d-%s.json" % (GAP_PREFIX, day, entry["number"], stamp)


def _upload_bos(entry):
    """通道一：上传到百度对象存储 BOS 的 problem/ 目录。

    返回 (ok, detail)。未配置凭据时返回 (False, 原因)，不视为错误。
    """
    conf = bos.load_config()
    if not bos.is_configured(conf):
        return False, "BOS 未配置"
    body = json.dumps(entry, ensure_ascii=False, indent=2)
    return bos.put_object(_object_key(entry), body, "application/json", conf)


def _upload_endpoint(entry):
    """通道二：POST 到自建接收端点（GAP_UPLOAD_URL）。返回 (ok, detail)。"""
    if not GAP_UPLOAD_URL:
        return False, "GAP_UPLOAD_URL 未配置"
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, "HTTP %d" % resp.status
    except Exception as err:
        return False, "%s: %s" % (type(err).__name__, err)


def _try_upload(entry):
    """best-effort 把一条缺口上传到对象存储 / 接收端点。

    依次尝试 BOS、自建端点，任一成功即返回 (True, detail)；
    全部未配置或失败则返回 (False, detail)。任何异常都不会外抛——
    上传是副路径，绝不能影响本地记录与主对话流程。
    """
    details = []
    for upload in (_upload_bos, _upload_endpoint):
        try:
            ok, detail = upload(entry)
        except Exception as err:
            ok, detail = False, "%s: %s" % (type(err).__name__, err)
        if ok:
            return True, detail
        details.append(detail)
    return False, "; ".join(details)


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
    # 可选上传（副路径，失败不影响本地记录）
    uploaded, detail = _try_upload(entry)
    if uploaded:
        tail = "，并已上传对象存储 %s" % _object_key(entry)
    elif upload_enabled():
        # 配了凭据却上传失败：如实告知，便于排查（但不中断流程）
        tail = "（对象存储上传失败：%s）" % detail
    else:
        tail = ""
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


def upload_enabled():
    """是否配置了任一上传通道（BOS 凭据 或 自建端点）"""
    return bool(bos.is_configured() or GAP_UPLOAD_URL)


def stat():
    """统计信息：{dir, count, upload_enabled, bos_bucket, bos_prefix}"""
    conf = bos.load_config()
    return {
        "dir": GAPS_DIR,
        "count": _count(),
        "upload_enabled": upload_enabled(),
        "bos_bucket": conf["bucket"] if bos.is_configured(conf) else "",
        "bos_prefix": GAP_PREFIX + "/",
    }
