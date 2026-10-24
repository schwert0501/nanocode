#!/usr/bin/env python3
"""nanocode - minimal claude code alternative"""

# glob 和内置的 glob 工具函数重名，所以用 globlib 别名导入标准库
import glob as globlib, json, logging, os, re, subprocess, urllib.request

# 仅导入即可让 input() 启用 readline 行编辑（左右移动、退格重绘更正常）
try:
    import readline  # noqa: F401
except ImportError:
    pass

# 会话持久化：存/读/列历史会话，详见 session.py
import session

# 配置日志：输出时间、级别、信息，默认 INFO 级别
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("nanocode")


def _mask(secret):
    """对密钥做脱敏：仅保留首尾少量字符，中间用 *** 代替，避免完整泄露到日志"""
    if not secret:
        return "<none>"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}***{secret[-4:]}"


OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
# 密钥统一从环境变量读取，不再硬编码到源码里（避免泄露 / 误提交进仓库）
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# API 地址和模型也支持用环境变量覆盖，方便切换渠道/模型，缺省回退到默认值
API_URL = os.environ.get("API_URL", "https://oneapi-comate.baidu-int.com/v1/messages")
MODEL = os.environ.get("MODEL", "Opus 4.8")

# ANSI 终端转义色码：用来给终端输出上色/加粗/变暗
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
)


# --- 工具（Tool）实现 ---
# 下面这些函数就是 AI 可以调用的“工具”，每个接收一个 dict 形式的 args 参数，返回字符串结果


def read(args):
    """读取文件内容，并在每行前面加上行号（方便 AI 定位代码位置）"""
    lines = open(args["path"]).readlines()
    offset = args.get("offset", 0)            # 起始行偏移量，默认从第 0 行开始
    limit = args.get("limit", len(lines))     # 最多读取多少行，默认读全部
    selected = lines[offset : offset + limit]  # 截取需要的行区间
    # 把每一行格式化成 "  12| 内容" 的形式，行号按 offset 计算并右对齐 4 位
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(args):
    """把内容整体写入文件（覆盖写），返回 ok 表示成功"""
    with open(args["path"], "w") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    """在文件中把 old 字符串替换为 new 字符串，实现精确编辑"""
    text = open(args["path"]).read()
    old, new = args["old"], args["new"]
    if old not in text:
        return "error: old_string not found"      # 找不到待替换内容则报错
    count = text.count(old)
    # 默认要求 old 唯一；若出现多次又没传 all=true，则拒绝替换，避免误改
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    # all=true 时替换全部，否则只替换第一处
    replacement = (
        text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    )
    with open(args["path"], "w") as f:
        f.write(replacement)
    return "ok"


def glob(args):
    """按通配符模式查找文件，并按修改时间（mtime）从新到旧排序"""
    # 拼出完整查找模式，如 "./**/*.py"，并把可能出现的 "//" 规整成 "/"
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)  # recursive=True 让 ** 可跨目录匹配
    # 按文件修改时间倒序排，最近改动的文件排在最前；目录没有 mtime 则按 0 处理
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"  # 没有结果时返回 "none"


def grep(args):
    """在目录下所有文件中按正则搜索，返回命中的 文件:行号:内容"""
    pattern = re.compile(args["pat"])  # 预编译正则表达式提升效率
    hits = []
    # 递归遍历目录下的所有路径
    for filepath in globlib.glob(args.get("path", ".") + "/**", recursive=True):
        try:
            # 逐行读取（enumerate 从 1 开始计行号），匹配到就记录下来
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            # 读不了的文件（二进制/目录/无权限等）直接跳过
            pass
    return "\n".join(hits[:50]) or "none"  # 最多返回前 50 条，防止输出过长


def bash(args):
    """执行 shell 命令，边执行边实时打印输出，并带 30 秒超时保护"""
    proc = subprocess.Popen(
        args["cmd"], shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # 把 stderr 合并到 stdout
        text=True
    )
    output_lines = []
    try:
        # 循环读取子进程输出，实现“流式”显示
        while True:
            line = proc.stdout.readline()
            # 没有新输出且进程已结束，则退出循环
            if not line and proc.poll() is not None:
                break
            if line:
                # 实时打印（带缩进和暗色竖线前缀），同时缓存起来用于最终返回
                print(f"  {DIM}│ {line.rstrip()}{RESET}", flush=True)
                output_lines.append(line)
        proc.wait(timeout=30)  # 等待进程真正结束，最多 30 秒
    except subprocess.TimeoutExpired:
        proc.kill()  # 超时则强制杀掉进程
        output_lines.append("\n(timed out after 30s)")
    return "".join(output_lines).strip() or "(empty)"  # 汇总全部输出返回


# --- 工具定义表：name -> (描述, 参数 schema, 对应的函数) ---
# 这是一个“注册表”，把工具名、给 AI 看的说明、参数类型、实际函数绑定在一起
# 参数类型里带 "?" 的表示可选参数（如 "number?"、"string?"）
TOOLS = {
    "read": (
        "Read file with line numbers (file path, not directory)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        read,
    ),
    "write": (
        "Write content to file",
        {"path": "string", "content": "string"},
        write,
    ),
    "edit": (
        "Replace old with new in file (old must be unique unless all=true)",
        {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
        edit,
    ),
    "glob": (
        "Find files by pattern, sorted by mtime",
        {"pat": "string", "path": "string?"},
        glob,
    ),
    "grep": (
        "Search files for regex pattern",
        {"pat": "string", "path": "string?"},
        grep,
    ),
    "bash": (
        "Run shell command",
        {"cmd": "string"},
        bash,
    ),
}


def run_tool(name, args):
    """按工具名查表并调用对应函数；出错时统一捕获异常并返回错误文本"""
    try:
        return TOOLS[name][2](args)  # TOOLS[name] 的第 3 个元素就是函数
    except Exception as err:
        return f"error: {err}"


def make_schema():
    """把 TOOLS 注册表转换成 Anthropic API 要求的 tools schema 格式（JSON Schema）"""
    result = []
    for name, (description, params, _fn) in TOOLS.items():
        properties = {}
        required = []
        for param_name, param_type in params.items():
            is_optional = param_type.endswith("?")   # 以 "?" 结尾表示可选
            base_type = param_type.rstrip("?")        # 去掉 "?" 得到真正类型名
            properties[param_name] = {
                # 内部用 "number" 表示，但 JSON Schema 里映射成 "integer"
                "type": "integer" if base_type == "number" else base_type
            }
            if not is_optional:
                required.append(param_name)           # 非可选参数加入 required
        result.append(
            {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return result


def call_api(messages, system_prompt):
    """向大模型 API 发起一次请求，带上对话历史、系统提示和工具 schema，返回解析后的 JSON"""
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            {
                "model": MODEL,
                "max_tokens": 32000,
                "system": system_prompt,
                "messages": messages,
                "tools": make_schema(),
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            # 根据渠道选择鉴权头：OpenRouter 用 Authorization，Anthropic 用 x-api-key
            **({"Authorization": f"Bearer {OPENROUTER_KEY}"} if OPENROUTER_KEY else {"x-api-key": ANTHROPIC_API_KEY}),
        },
    )
    response = urllib.request.urlopen(request)  # 发送请求
    return json.loads(response.read())          # 读取并解析 JSON 响应


def separator():
    """生成一条分隔线，宽度取终端宽度与 80 中的较小值"""
    return f"{DIM}{'─' * min(os.get_terminal_size().columns, 80)}{RESET}"


def render_markdown(text):
    """极简 Markdown 渲染：把 **加粗** 语法转换成终端加粗显示"""
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


def main():
    """程序主入口：实现一个交互式命令行 AI coding 助手"""
    # 启动前校验：两种渠道的密钥都没配置则直接报错退出，避免发出无效请求
    if not OPENROUTER_KEY and not ANTHROPIC_API_KEY:
        print(f"{RED}⏺ 未检测到 API 密钥。请先设置环境变量 "
              f"OPENROUTER_API_KEY 或 ANTHROPIC_API_KEY 后再运行。{RESET}")
        return
    # 打印启动信息：工具名、模型、接入渠道、当前工作目录
    print(f"{BOLD}nanocode{RESET} | {DIM}{MODEL} ({'OpenRouter' if OPENROUTER_KEY else 'Anthropic'}) | {os.getcwd()}{RESET}\n")
    messages = []  # 对话历史，贯穿整个会话
    session_id = session.new_session_id()  # 本次会话的 id，用于自动保存
    system_prompt = f"Concise coding assistant. cwd: {os.getcwd()}"  # 系统提示

    # 外层循环：不断读取用户输入
    while True:
        try:
            print(separator())
            # 读取用户输入并去除首尾空白
            # 提示符里的 ANSI 颜色码用 \001\002 包裹，告诉 readline 这些字符不占显示宽度，
            # 否则退格编辑时光标位置会算错、屏幕重绘错位（内容其实是对的）
            user_input = input(f"\001{BOLD}{BLUE}\002❯\001{RESET}\002 ").strip()
            print(separator())
            if not user_input:
                continue                      # 空输入则跳过
            if user_input in ("/q", "exit"):
                break                         # 退出程序
            if user_input == "/c":
                messages = []                 # /c 清空对话历史
                session_id = session.new_session_id()  # 清空后开启一个全新会话
                print(f"{GREEN}⏺ Cleared conversation{RESET}")
                continue
            if user_input == "/sessions":
                # 列出历史会话，供用户选择恢复
                sessions = session.list_sessions()
                if not sessions:
                    print(f"{DIM}暂无历史会话{RESET}")
                else:
                    print(f"{BOLD}历史会话{RESET}（用 {CYAN}/resume <编号>{RESET} 恢复）:")
                    for idx, s in enumerate(sessions):
                        marker = f"{GREEN} (当前){RESET}" if s["id"] == session_id else ""
                        print(f"  {BOLD}{idx}{RESET} {DIM}{s['id']}{RESET}  {s['preview']}{marker}")
                continue
            if user_input.startswith("/resume"):
                # 恢复到某个历史会话，后续对话将继续写入该会话文件
                parts = user_input.split()
                sessions = session.list_sessions()
                if len(parts) < 2 or not parts[1].isdigit():
                    print(f"{YELLOW}用法: /resume <编号>，先用 /sessions 查看编号{RESET}")
                    continue
                idx = int(parts[1])
                if idx < 0 or idx >= len(sessions):
                    print(f"{RED}⏺ 编号超出范围{RESET}")
                    continue
                session_id = sessions[idx]["id"]
                messages = session.load_session(session_id)
                print(f"{GREEN}⏺ 已恢复会话 {session_id}（{len(messages)} 条消息）{RESET}")
                continue

            messages.append({"role": "user", "content": user_input})

            # agentic loop（智能体循环）：持续调用 API，直到模型不再请求调用工具
            while True:
                response = call_api(messages, system_prompt)
                content_blocks = response.get("content", [])  # 模型返回的内容块
                tool_results = []                             # 收集本轮工具执行结果

                for block in content_blocks:
                    # 文本块：直接渲染并打印给用户
                    if block["type"] == "text":
                        print(f"\n{CYAN}⏺{RESET} {render_markdown(block['text'])}")

                    # 工具调用块：执行对应工具
                    if block["type"] == "tool_use":
                        tool_name = block["name"]
                        tool_args = block["input"]
                        # 取第一个参数值的前 50 字符做预览显示
                        arg_preview = str(list(tool_args.values())[0])[:50]
                        print(
                            f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})"
                        )

                        result = run_tool(tool_name, tool_args)  # 真正执行工具
                        # 生成结果预览：首行前 60 字符，多行时提示剩余行数
                        result_lines = result.split("\n")
                        preview = result_lines[0][:60]
                        if len(result_lines) > 1:
                            preview += f" ... +{len(result_lines) - 1} lines"
                        elif len(result_lines[0]) > 60:
                            preview += "..."
                        print(f"  {DIM}⎿  {preview}{RESET}")

                        # 把工具结果打包，稍后回传给模型
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": result,
                            }
                        )

                # 把模型这一轮的完整回复加入历史
                messages.append({"role": "assistant", "content": content_blocks})

                # 若本轮没有工具调用，说明模型已给出最终回答，结束内层循环
                if not tool_results:
                    break
                # 否则把工具结果作为 user 消息回传，让模型继续处理
                messages.append({"role": "user", "content": tool_results})

            # 每轮对话结束后自动保存会话，实现无感记忆
            session.save_session(session_id, messages)

            print()

        except (KeyboardInterrupt, EOFError):
            break                             # Ctrl+C / Ctrl+D 退出
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")  # 捕获并打印其它异常，不中断会话


if __name__ == "__main__":
    main()  # 作为脚本直接运行时启动主循环
