#!/usr/bin/env python3
"""上下文自动压缩模块：对话历史过长时，把较早的消息压成一段摘要，保留最近几轮原文。

为什么需要：每轮请求都会把全部历史重新发给模型（按 input token 计费），
历史里还常塞着巨大的工具输出（读文件 / bash 结果）。压缩不是因为"窗口塞不下"，
而是为了省钱、降延迟、避免历史被巨型工具结果撑爆。

设计上保持零第三方依赖，只用标准库。度量用"字符数"作为 token 的保守高估代理
（中文≈1字符/token，英文≈3~4字符/token，无需引入 tokenizer）。
"""

import json
import os

# ── 模型上下文窗口表（单位：K tokens）──────────────────────────────────
# 存的是"实际有效区间"而非厂商宣称值：超过有效区间模型就开始"失忆"，
# 用宣称的 1M 当基准会压得太晚。key 用模型名里的关键字做子串匹配（小写）。
# 新模型只需在这里加一行。命中不了就用 DEFAULT_CONTEXT_K 兜底。
MODEL_CONTEXT_K = {
    "opus": 600,
    "sonnet": 600,
    "claude": 600,
    "gemini": 1536,
    "gpt-5": 600,
    "gpt5": 600,
    "llama-4": 8192,
    "scout": 8192,
    "deepseek": 600,
    "qwen": 600,
    "kimi": 300,
    "grok": 600,
    "glm": 200,
    "minimax": 600,
}

# 表未命中时的保守默认窗口（宁可压得勤一点，也别超出小模型的有效区间）。
DEFAULT_CONTEXT_K = int(os.environ.get("NANOCODE_DEFAULT_CONTEXT_K", "200"))

# 触发占比：上下文用到"有效窗口"的这个比例就压缩。
# 取 0.5 而非 0.9，是为给压缩本身那一轮 API 和后续工具输出留足余量，避免贴着上限抖动。
COMPACT_RATIO = float(os.environ.get("NANOCODE_COMPACT_RATIO", "0.5"))

# 每 token 约等于多少字符（零依赖无 tokenizer，用字符数做代理度量）。
# 代码/JSON（英文多）约 3~4 字符/token，中文约 1 字符/token，混合场景取 3 较接近。
CHARS_PER_TOKEN = float(os.environ.get("NANOCODE_CHARS_PER_TOKEN", "3"))

# 压缩时保留最近多少轮"真实用户提问"的原文（这些轮次连同其工具调用一并保留）。
KEEP_RECENT_TURNS = int(os.environ.get("NANOCODE_KEEP_TURNS", "3"))


def resolve_context_k(model_name):
    """由模型名解析出"有效上下文窗口"（K tokens）。

    优先级：env NANOCODE_CONTEXT_K（手填）> 映射表子串命中 > DEFAULT_CONTEXT_K。
    """
    env_k = os.environ.get("NANOCODE_CONTEXT_K")
    if env_k:
        return int(env_k)
    name = (model_name or "").lower()
    for key, k in MODEL_CONTEXT_K.items():
        if key in name:
            return k
    return DEFAULT_CONTEXT_K


def char_threshold(model_name):
    """由模型窗口推导出触发压缩的字符阈值。

    阈值(字符) = 有效窗口(tokens) × 触发占比 × 每token字符数
    env NANOCODE_COMPACT_CHARS 可直接定死字符数，优先级最高（老用户兼容）。
    """
    env_chars = os.environ.get("NANOCODE_COMPACT_CHARS")
    if env_chars:
        return int(env_chars)
    context_tokens = resolve_context_k(model_name) * 1000
    return int(context_tokens * COMPACT_RATIO * CHARS_PER_TOKEN)


def total_chars(messages):
    """把整个 messages 序列化后的字符数，作为上下文体量的粗略度量。"""
    return len(json.dumps(messages, ensure_ascii=False))


def _real_user_turns(messages):
    """返回所有"真实用户提问"的下标。

    真实用户提问 = role 为 user 且 content 是纯字符串（而非 tool_result 数组）。
    只在这种边界切割，才能保证不切断 tool_use / tool_result 的配对。
    """
    return [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]


def should_compact(messages, model_name):
    """是否已达到压缩阈值（阈值由模型窗口推导）。"""
    return total_chars(messages) >= char_threshold(model_name)


def maybe_compact(messages, summarize, model_name, force=False):
    """必要时压缩 messages，返回 (新 messages, info)。

    - force=False：未达阈值、或没有足够早的历史可压缩时，原样返回，info 为 None。
    - force=True：无视阈值（供手动 /compact 使用），仍需有可压缩的早期历史。
    - model_name：当前模型名，用于按其上下文窗口推导压缩阈值。
    - summarize(head_messages) 由主程序注入：接收一段历史消息，返回其文字摘要。
      失败时应抛异常或返回空串，这里会据此安全跳过压缩。
    """
    if not force and not should_compact(messages, model_name):
        return messages, None

    turns = _real_user_turns(messages)
    # 真实提问轮数不足以在"保留最近 N 轮"之外还剩可压缩的早期历史时，跳过
    if len(turns) <= KEEP_RECENT_TURNS:
        return messages, None

    split = turns[-KEEP_RECENT_TURNS]   # 从这条真实提问起（含）全部保留原文
    head, tail = messages[:split], messages[split:]
    if not head:
        return messages, None

    try:
        summary = summarize(head)
    except Exception:
        return messages, None           # 摘要调用失败则不动历史，保证不崩
    if not summary or not summary.strip():
        return messages, None

    chars_before = total_chars(messages)
    # 用"摘要 user + 简短 assistant 确认"两条消息替换掉被压缩的早期原文。
    # 成对出现可维持 user/assistant 交替，后面紧接的 tail 以真实提问开头，衔接自然。
    new_messages = [
        {
            "role": "user",
            "content": "[早期对话摘要] 以下是本次会话较早部分的要点，"
            "请据此延续上下文，不要重复已完成的工作：\n\n" + summary.strip(),
        },
        {"role": "assistant", "content": "已了解之前的上下文，我们继续。"},
    ] + tail

    info = {
        "turns_summarized": len(head),
        "msgs_before": len(messages),
        "msgs_after": len(new_messages),
        "chars_before": chars_before,
        "chars_after": total_chars(new_messages),
    }
    return new_messages, info
