"""命令元数据 + 终端 Tab 补全。

把"有哪些命令、各自的中文说明"集中到 COMMANDS 一处，
作为 Tab 补全候选、/help 帮助、Tab 候选中文显示三者的唯一数据源——
日后新增命令只需在这里加一行，三处行为自动同步。
"""

# 命令 -> 中文说明（有序：决定 /help 和 Tab 候选的展示顺序）
COMMANDS = {
    "/help": "显示本帮助，列出所有命令及其作用",
    "/sessions": "列出历史会话，供恢复选择",
    "/resume": "恢复指定编号的历史会话（用法：/resume <编号>）",
    "/c": "清空当前对话历史，开启全新会话",
    "/q": "退出 nanocode",
}

# /help 的别名：这些输入都触发帮助
HELP_ALIASES = {"/help", "-h", "--help"}


def is_help(user_input):
    """判断一条输入是否是请求帮助。"""
    return user_input.strip() in HELP_ALIASES


def format_help(colors):
    """生成彩色帮助文本。colors 是一个含颜色码的字典（由主程序传入）。"""
    bold = colors.get("BOLD", "")
    cyan = colors.get("CYAN", "")
    dim = colors.get("DIM", "")
    reset = colors.get("RESET", "")
    lines = [f"{bold}可用命令{reset}："]
    # 对齐命令列宽，让说明整齐排版
    width = max(len(c) for c in COMMANDS)
    for cmd, desc in COMMANDS.items():
        lines.append(f"  {cyan}{cmd.ljust(width)}{reset}  {dim}{desc}{reset}")
    lines.append(f"{dim}（提示：输入 / 后按 Tab 可补全命令）{reset}")
    return "\n".join(lines)


def setup_completion(readline):
    """为 readline 配置命令 Tab 补全与候选中文显示。

    readline 不可用或底层实现不支持某些能力时，静默降级，
    不影响 REPL 正常使用。
    """
    if readline is None:
        return

    def _completer(text, state):
        # 返回第 state 个以 text 为前缀的命令，没有更多则返回 None
        matches = [c for c in COMMANDS if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    def _display(substitution, matches, longest_match_length):
        # Tab 列出多个候选时，顺带打印每个命令的中文说明
        print()
        for m in matches:
            desc = COMMANDS.get(m, "")
            print(f"  {m}\t{desc}")
        # 重绘用户当前已输入的内容（上面的 print 会把输入行冲掉）
        print(f"❯ {readline.get_line_buffer()}", end="", flush=True)

    try:
        readline.set_completer(_completer)
        # macOS 自带的是 libedit 而非 GNU readline，Tab 绑定语法不同
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        # 候选中文显示 hook（libedit 不一定支持，支持不了就忽略）
        try:
            readline.set_completion_display_matches_hook(_display)
        except (AttributeError, NotImplementedError):
            pass
    except Exception:
        # 任何配置失败都不应让主程序崩溃
        pass
