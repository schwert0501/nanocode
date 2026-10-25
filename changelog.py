#!/usr/bin/env python3
"""改动记录模块：独立于 session 之外，记录每一次文件写入/编辑的全量快照，支持回滚。

设计目标与取舍：
- 为什么独立于 session？session 存的是"对话历史"（给模型看的上下文），
  改动记录存的是"文件变更事实"（给人审计/回滚用的），二者关注点不同、
  生命周期不同（清空会话不该丢掉改动历史），所以用独立目录分开存。
- 为什么用"全量快照"而不是反向 patch？全量快照回滚最可靠：直接把旧内容
  整体写回即可，不依赖当前文件处于某个中间状态，也不怕 patch 对不上。
  代价是多占一点磁盘，但对一个极简工具而言完全可接受。

保持零第三方依赖，只用标准库。每条记录是一个 JSON 对象，所有记录按时间
顺序追加进一个 JSONL（每行一个 JSON）文件，天然有序、易追加、易读取。
"""

import datetime
import json
import os

# 改动记录统一存放在脚本同级的 .nanocode_changes/ 目录下
CHANGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nanocode_changes")
# 所有改动追加写入同一个 JSONL 文件，每行一条记录
CHANGES_FILE = os.path.join(CHANGES_DIR, "changes.jsonl")


def _ensure_dir():
    """确保改动记录目录存在（不存在则创建）"""
    os.makedirs(CHANGES_DIR, exist_ok=True)


def record_change(tool, path, before, after):
    """追加一条改动记录，返回这条记录的整数编号（从 0 开始，供回滚时引用）。

    参数：
      tool   -- 触发改动的工具名（"write" / "edit"）
      path   -- 被改动的文件路径
      before -- 改动前的完整内容；文件原本不存在则为 None（表示新建）
      after  -- 改动后的完整内容
    """
    _ensure_dir()
    # 编号 = 现有记录条数，保证单调递增且与行号一致
    number = _count()
    entry = {
        "number": number,
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool,
        "path": os.path.abspath(path),
        "before": before,  # None 代表新建文件（回滚时应删除该文件）
        "after": after,
    }
    with open(CHANGES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return number


def _count():
    """统计当前已有多少条改动记录（即文件行数），文件不存在则为 0"""
    if not os.path.exists(CHANGES_FILE):
        return 0
    with open(CHANGES_FILE, encoding="utf-8") as f:
        return sum(1 for _ in f)


def list_changes():
    """读取全部改动记录，按编号顺序返回列表；无记录则返回空列表"""
    if not os.path.exists(CHANGES_FILE):
        return []
    entries = []
    with open(CHANGES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def load_change(number):
    """按编号取出单条改动记录；编号越界返回 None"""
    entries = list_changes()
    if 0 <= number < len(entries):
        return entries[number]
    return None


def revert_change(number):
    """把指定编号的改动回滚：将该文件恢复到那次改动"之前"的内容。

    回滚本身也是一次文件变更，因此会再追加一条新的改动记录（tool="revert"），
    这样回滚动作同样可被审计、甚至可被再次回滚。

    返回一个结果 dict：{"ok": bool, "msg": 说明文本, "path": 文件路径}
    """
    entry = load_change(number)
    if entry is None:
        return {"ok": False, "msg": f"编号 {number} 不存在", "path": None}

    path = entry["path"]
    before = entry["before"]

    # 回滚前先读当前内容，作为这条 revert 记录的 before，保证可审计/可再回滚
    current = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                current = f.read()
        except Exception:
            current = None

    if before is None:
        # 该次改动是"新建文件"，回滚即删除这个文件
        if os.path.exists(path):
            os.remove(path)
        record_change("revert", path, current, None)
        return {"ok": True, "msg": f"已删除（回滚新建）#{number} 对应文件", "path": path}

    # 普通改动：把改动前的内容整体写回
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(before)
    record_change("revert", path, current, before)
    return {"ok": True, "msg": f"已回滚 #{number}，{path} 恢复到改动前内容", "path": path}
