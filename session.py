#!/usr/bin/env python3
"""会话持久化模块：把对话历史 messages 存成 JSON 文件，支持列出与恢复历史会话。

设计上保持零第三方依赖，只用标准库。messages 的结构直接沿用 Anthropic API
所需的格式，存取时原样读写，无需额外转换。
"""

import datetime
import json
import os

# 会话文件统一存放在脚本同级的 .nanocode_sessions/ 目录下，每个会话一个 JSON 文件
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nanocode_sessions")


def _ensure_dir():
    """确保会话目录存在（不存在则创建）"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def new_session_id():
    """生成一个新的会话 id，使用当前时间戳，形如 20261024_124341"""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def session_path(session_id):
    """根据会话 id 拼出对应的 JSON 文件路径"""
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def save_session(session_id, messages):
    """把整个 messages 列表写入会话文件（覆盖写）。messages 为空则跳过，避免产生空文件。"""
    if not messages:
        return
    _ensure_dir()
    with open(session_path(session_id), "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def load_session(session_id):
    """读取指定会话文件，返回 messages 列表"""
    with open(session_path(session_id), encoding="utf-8") as f:
        return json.load(f)


def _first_user_text(path):
    """从会话文件里取第一条纯文本用户消息作为预览；取不到则返回占位符"""
    try:
        with open(path, encoding="utf-8") as f:
            messages = json.load(f)
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"].replace("\n", " ")[:60]
        return "<无文本预览>"
    except Exception:
        return "<文件损坏>"


def list_sessions():
    """列出所有历史会话，按最近修改时间倒序返回。

    每一项为 dict：{"id", "preview", "mtime"}
    """
    _ensure_dir()
    sessions = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, fname)
        sessions.append(
            {
                "id": fname[:-5],                 # 去掉 .json 后缀
                "preview": _first_user_text(path),
                "mtime": os.path.getmtime(path),
            }
        )
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions
