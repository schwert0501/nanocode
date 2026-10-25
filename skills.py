#!/usr/bin/env python3
"""skills - 内置技能（SKILL）模块：领域专用的提示词/检查清单/标准流程，随包分发。

这是什么 / 为什么这样设计：
- SKILL 是把「某类任务该怎么做」沉淀成的可复用说明书：触发场景、标准步骤、
  检查清单、注意事项。比如「算子数据分析」「算子开发联调」等。模型遇到命中
  某个 SKILL 触发场景的请求时，先调用 skill 工具把该 SKILL 的完整步骤加载进来，
  再照着执行——这就是「机智诱惑」：让 agent 默认走团队沉淀的最佳实践，而不是
  每次临场发挥。

- 与 kb.py 一致的设计理念（构建期打包、解压即用）：
  * SKILL 文件统一放在「脚本同级」的 .nanocode_skills/ 目录（而不是 cwd），
    这样打包成 zip 分发后，团队成员在任意目录运行都能读到内置 SKILL。
  * 纯文本（markdown + 极简 front-matter），零依赖、零索引、人可读可编辑。

- 自动触发（agentic）怎么实现：
  * manifest() 把所有 SKILL 的「名称 + 触发场景 + 一句话描述」压成一份清单，
    由 main() 注入 system_prompt。模型据此知道「有哪些技能、什么时候该用」。
  * skill 工具（skill_use）让模型按名称/关键词按需加载某个 SKILL 的完整正文。
    范式与 kb_search 相同：清单常驻、正文按需加载，省 token 又不丢能力。

SKILL 文件格式（.nanocode_skills/*.md）：
    ---
    name: 算子数据分析
    triggers: 算子, 数据分析, SQL, 报表, 取数
    description: 处理算子相关的数据分析/取数任务的标准流程
    ---
    # 正文（markdown）：标准步骤、检查清单、注意事项……

front-matter 用首部 `---` 包裹的极简 key: value，只认 name/triggers/description
三个字段，其余按正文处理。解析零依赖（不引入 yaml）。
"""

import glob as globlib
import os

# SKILL 统一存放在"脚本同级"的 .nanocode_skills/ 目录（分发后随包迁移、与 cwd 无关）
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".nanocode_skills")


def _ensure_dir():
    """懒创建 .nanocode_skills/ 目录（首次需要时才建）"""
    os.makedirs(SKILLS_DIR, exist_ok=True)


def _parse_front_matter(text):
    """解析极简 front-matter，返回 (meta: dict, body: str)。

    仅当文件以 `---` 开头时才解析首部键值块；否则整篇当正文、meta 为空。
    只认 name/triggers/description 三个键，解析失败时安全降级为纯正文。
    """
    meta = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines()
    # 找到第二个 "---" 作为 front-matter 结束位置
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return meta, text  # 没有闭合的 ---，当作没有 front-matter
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _load_all():
    """扫描 .nanocode_skills/ 下所有 .md 文件，解析成 SKILL 列表。

    每项为 dict：{name, triggers(list), description, body, source}。
    name 缺省用文件名（去扩展名）。目录不存在或读失败则返回空列表。
    """
    skills = []
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for path in sorted(globlib.glob(os.path.join(SKILLS_DIR, "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        meta, body = _parse_front_matter(text)
        name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
        triggers = [t.strip() for t in meta.get("triggers", "").split(",") if t.strip()]
        skills.append({
            "name": name,
            "triggers": triggers,
            "description": meta.get("description", ""),
            "body": body,
            "source": path,
        })
    return skills


def manifest():
    """生成注入 system_prompt 的 SKILL 清单文本；无 SKILL 时返回空串。

    形如：
      - 算子数据分析：处理算子相关的数据分析/取数任务 ｜ 触发：算子, 数据分析, SQL
    只给"名称+描述+触发词"，正文留给模型按需用 skill 工具加载，省 token。
    """
    skills = _load_all()
    if not skills:
        return ""
    lines = []
    for s in skills:
        trig = f" ｜ 触发：{', '.join(s['triggers'])}" if s["triggers"] else ""
        desc = f"：{s['description']}" if s["description"] else ""
        lines.append(f"  - {s['name']}{desc}{trig}")
    return "\n".join(lines)


def skill_use(args):
    """skill 工具实现：按名称/关键词加载某个 SKILL 的完整正文返回给模型。

    匹配策略（宽松，便于模型凭印象召回）：
      1) 精确匹配 name（忽略大小写）；
      2) name 子串包含查询词；
      3) 命中任一 trigger 触发词；
      4) 描述中包含查询词。
    命中多个时返回第一个的正文；全不命中则列出可用 SKILL 供模型重试。
    """
    query = (args.get("name") or "").strip().lower()
    skills = _load_all()
    if not skills:
        return "error: 当前没有内置任何 SKILL（.nanocode_skills/ 为空）"
    if not query:
        names = ", ".join(s["name"] for s in skills)
        return f"error: 请提供 name。可用 SKILL：{names}"

    def _hit(s):
        name = s["name"].lower()
        if query == name or query in name:
            return True
        if any(query in t.lower() or t.lower() in query for t in s["triggers"]):
            return True
        if query in s["description"].lower():
            return True
        return False

    for s in skills:
        if _hit(s):
            trig = ", ".join(s["triggers"])
            return (
                f"# SKILL: {s['name']}\n"
                f"触发场景：{trig}\n"
                f"说明：{s['description']}\n\n"
                f"{s['body']}"
            )

    names = ", ".join(s["name"] for s in skills)
    return f"error: 未找到匹配 '{query}' 的 SKILL。可用 SKILL：{names}"


def list_skills():
    """返回 SKILL 概要列表（供 /skills 命令展示）：[{name, triggers, description}]"""
    return [
        {"name": s["name"], "triggers": s["triggers"], "description": s["description"]}
        for s in _load_all()
    ]


def stat():
    """返回统计信息：{dir, count}，供命令/诊断用"""
    return {"dir": SKILLS_DIR, "count": len(_load_all())}
