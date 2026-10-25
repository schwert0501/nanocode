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
    "/compact": "立即手动压缩当前对话历史（平时也会在超长时自动触发）",
    "/changes": "列出本项目所有文件改动记录（含编号、时间、增删行数）",
    "/undo": "撤销最近一次文件改动（等价于回滚编号最大的那条）",
    "/revert": "回滚指定编号的文件改动（用法：/revert <编号>）",
    "/kb": "知识库：/kb index <目录> 建索引、/kb update <目录> 增量更新、/kb stat 看概况",
    "/skills": "列出内置 SKILL（团队标准流程/检查清单）及其触发场景",
    "/mode": "切换权限模式 default↔bypass（也可按 Shift+Tab）",
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

    # 记录"补全前"用户已输入的文本。重绘输入行时要用它而不是当前的
    # line buffer：唯一候选被补全后，readline 还会把新插入的后缀再画一次，
    # 若用已补全的 buffer 重绘会得到 "/resumeme" 这种重复。用补全前的文本
    # 重绘，让 readline 补上后缀后正好拼成完整命令。
    _orig_text = [""]

    def _completer(text, state):
        # 返回第 state 个以 text 为前缀的命令，没有更多则返回 None
        if state == 0:
            _orig_text[0] = text
        matches = [c for c in COMMANDS if c.startswith(text)]
        # 只有一个候选时，readline 会直接补全、不触发"多候选展示"钩子，
        # 中文说明就没机会显示。这里加一个带制表符的"假候选"（和真命令
        # 共享前缀、不会改变实际补全结果），配合 show-all-if-ambiguous
        # 强制走展示钩子，从而把中文说明打印出来。
        if len(matches) == 1:
            matches = [matches[0], matches[0] + "\t"]
        return matches[state] if state < len(matches) else None

    def _display(substitution, matches, longest_match_length):
        # Tab 列出候选时，顺带打印每个命令的中文说明
        print()
        seen = set()
        for m in matches:
            cmd = m.strip()  # 去掉"假候选"的制表符
            if cmd in seen:
                continue
            seen.add(cmd)
            print(f"  {cmd}\t{COMMANDS.get(cmd, '')}")
        # 用"补全前"的文本重绘输入行（上面的 print 会把输入行冲掉）
        print(f"❯ {_orig_text[0]}", end="", flush=True)

    try:
        # 默认补全分隔符里含有 "/"，会把 "/resu" 从斜杠处切开，
        # 只把 "resu" 传给补全函数，导致以 "/" 开头的命令永远匹配不到。
        # 这里把分隔符改成仅空白字符，让整个 "/resu" 作为一个词参与补全。
        readline.set_completer_delims(" \t\n")
        readline.set_completer(_completer)
        # 让单个/多个候选在第一次 Tab 就直接列出（否则默认要按两次），
        # 也是"假候选"方案能即时显示中文说明的前提。
        readline.parse_and_bind("set show-all-if-ambiguous on")
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
