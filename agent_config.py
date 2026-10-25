#!/usr/bin/env python3
"""agent_config - 解析 AGENTS.md，产出注入 system_prompt 的身份/本体文本。

为什么要有这个模块：
- 把 agent 的「身份、领域术语表（本体）、协作约定」从代码里抽到 AGENTS.md，
  将来改身份 / 调本体只改 markdown，无需动 Python。AGENTS.md 是唯一事实来源。
- 和 kb.py / skills.py 一样用「脚本同级」定位，保证打包分发后在任意 cwd 都能读到。

AGENTS.md 约定按二级标题分段，本模块只提取三段注入 system_prompt：
    ## IDENTITY   —— 身份与职责
    ## GLOSSARY   —— 领域术语表 / 本体
    ## BEHAVIOR   —— 工作方式 / 协作约定
其余章节（如文件头部的使用说明）不注入，仅供人阅读。

解析零依赖：手写一个极简的「按 ## 标题切块」解析器，不引入任何第三方库。
"""

import os

# AGENTS.md 与脚本同级（分发后随包迁移、与 cwd 无关）
AGENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENTS.md")

# 需要注入 system_prompt 的章节（二级标题名，大小写不敏感），按此顺序拼接
_INJECT_SECTIONS = ["IDENTITY", "GLOSSARY", "BEHAVIOR"]


def _parse_sections(text):
    """把 markdown 按 `## 标题` 切成 {标题大写: 该节正文} 字典。

    只认二级标题（`## `）作为分节点；标题后到下一个二级标题之间的内容为该节正文。
    标题可能形如 `## IDENTITY` 或 `## GLOSSARY（领域本体）`，取第一个空白前的词做键。
    """
    sections = {}
    current_key = None
    buf = []
    for line in text.splitlines():
        if line.startswith("## "):
            # 遇到新章节：先把上一节存档
            if current_key is not None:
                sections[current_key] = "\n".join(buf).strip()
            # 取标题首词（去掉可能的括号补充说明）作为 key，大写归一
            title = line[3:].strip()
            key = title.split()[0].split("（")[0].split("(")[0].upper() if title else ""
            current_key = key
            buf = []
        elif current_key is not None:
            buf.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(buf).strip()
    return sections


def load_prompt():
    """读取 AGENTS.md 并拼出要注入 system_prompt 的身份/本体文本。

    返回拼接好的字符串；AGENTS.md 不存在或为空则返回 ""（调用方据此回退默认提示）。
    """
    if not os.path.exists(AGENTS_FILE):
        return ""
    try:
        with open(AGENTS_FILE, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    sections = _parse_sections(text)
    parts = []
    for key in _INJECT_SECTIONS:
        body = sections.get(key)
        if body:
            parts.append(body)
    return "\n\n".join(parts).strip()


def exists():
    """AGENTS.md 是否存在（供诊断/提示用）"""
    return os.path.exists(AGENTS_FILE)
