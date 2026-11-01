"""终端 Markdown 渲染：把模型返回的 Markdown 文本渲染成带 ANSI 样式的终端输出。

纯标准库实现（只用 re / unicodedata），不引入任何第三方依赖。

渲染以「行」为粒度，因此可以配合流式输出使用：
  - ``feed(chunk)``  喂入任意切分的流式片段，返回此刻已可安全打印的渲染结果
  - ``flush()``      流结束时把残留的最后一行（可能没有换行符）渲染出来

内部统一用「行列表」传递结果：空列表表示此刻无内容可输出（例如表格行被暂存），
而列表里的空字符串才表示一个真正的空行。两者不能混用，否则排版会多出空行。

为什么必须按行而不是按字符渲染：块级语法（标题 / 列表 / 代码围栏 / 引用）要看到
整行开头才能判定，行内语法（``**粗体**``）要看到配对的结束标记才能判定。
LLM 输出的单行通常很短，按行刷新在观感上依然是流式的。
"""

import re
import unicodedata

RESET, BOLD, DIM, ITALIC, UNDER = "\033[0m", "\033[1m", "\033[2m", "\033[3m", "\033[4m"
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
)

# 可被反斜杠转义的 Markdown 标点。转义后的字符先用 \x00 掩码藏起来，
# 避免它参与行内语法匹配，渲染完成后再还原成原字符。
_ESCAPABLE = r"\\`*_{}[]()#+\-.!~>|"
_ESCAPE_RE = re.compile(r"\\([" + _ESCAPABLE + r"])")
_UNMASK_RE = re.compile("\x00(.)")


def _mask_escapes(text):
    return _ESCAPE_RE.sub(lambda m: "\x00" + m.group(1), text)


def _unmask_escapes(text):
    return _UNMASK_RE.sub(r"\1", text)


# 行内语法。顺序即优先级：代码片段最高（其内部不再解析任何语法），
# 其次链接，然后 ** 粗体 > ~~删除线 > * 斜体。
# `_` 系列加了 (?<!\w) / (?!\w) 边界，避免 snake_case_name 被误识别成斜体。
_INLINE_RE = re.compile(
    r"(?P<ctick>`+)(?P<code>.+?)(?P=ctick)"
    r"|!?\[(?P<ltext>[^\]\n]*)\]\((?P<lurl>[^)\s]*)(?:\s+\"[^\"]*\")?\)"
    r"|\*\*(?P<bstar>\S(?:.*?\S)?)\*\*"
    r"|(?<!\w)__(?P<bund>\S(?:.*?\S)?)__(?!\w)"
    r"|~~(?P<strike>\S(?:.*?\S)?)~~"
    r"|\*(?P<estar>[^*\s](?:[^*]*?[^*\s])?)\*"
    r"|(?<!\w)_(?P<eund>[^_\s](?:[^_]*?[^_\s])?)_(?!\w)",
    re.DOTALL,
)


def render_inline(text, color=True):
    """渲染一行里的行内语法（粗体 / 斜体 / 代码 / 链接 / 删除线）。"""
    masked = _mask_escapes(text)
    if not color:
        # 不上色时仍然剥掉标记符号，避免终端里出现一堆裸 ** 和 `
        return _unmask_escapes(_INLINE_RE.sub(_plain_sub, masked))
    return _unmask_escapes(_INLINE_RE.sub(_color_sub, masked))


def _plain_sub(m):
    if m.group("code") is not None:
        return m.group("code")
    if m.group("ltext") is not None:
        url = m.group("lurl")
        text = m.group("ltext")
        return f"{text} ({url})" if text and url else (text or url)
    for name in ("bstar", "bund", "strike", "estar", "eund"):
        if m.group(name) is not None:
            return m.group(name)
    return m.group(0)


def _color_sub(m):
    if m.group("code") is not None:
        # 行内代码：青色 + 两侧薄边距，比裸反引号更容易在正文里扫到
        return f"{CYAN}{m.group('code')}{RESET}"
    if m.group("ltext") is not None:
        text, url = m.group("ltext"), m.group("lurl")
        if not text:
            return f"{UNDER}{BLUE}{url}{RESET}"
        if not url:
            return text
        return f"{UNDER}{BLUE}{text}{RESET}{DIM}({url}){RESET}"
    if m.group("bstar") is not None:
        return f"{BOLD}{m.group('bstar')}{RESET}"
    if m.group("bund") is not None:
        return f"{BOLD}{m.group('bund')}{RESET}"
    if m.group("strike") is not None:
        return f"\033[9m{m.group('strike')}{RESET}"
    if m.group("estar") is not None:
        return f"{ITALIC}{m.group('estar')}{RESET}"
    if m.group("eund") is not None:
        return f"{ITALIC}{m.group('eund')}{RESET}"
    return m.group(0)


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def display_width(text):
    """估算字符串在终端里占的列数：CJK 全角算 2 列，ANSI 控制序列算 0 列。"""
    width = 0
    for ch in _ANSI_RE.sub("", text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


# ---------------------------------------------------------------------------
# 块级语法
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+#.\-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_HR_RE = re.compile(r"^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_QUOTE_RE = re.compile(r"^(\s*)>\s?(.*)$")
_BULLET_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d{1,9})([.)])\s+(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
# 表格分隔行：| --- | :--: | ---: |
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

# 标题级别 -> 样式。一级最重，往下逐级减弱
_HEADING_STYLE = {
    1: BOLD + CYAN,
    2: BOLD + CYAN,
    3: BOLD,
    4: BOLD,
    5: BOLD + DIM,
    6: BOLD + DIM,
}


def _split_table_row(line):
    """把 `| a | b |` 拆成单元格列表，忽略首尾的竖线。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    # 用掩码避开 \| 转义竖线，拆完再还原
    masked = stripped.replace("\\|", "\x01")
    return [c.replace("\x01", "|").strip() for c in masked.split("|")]


class MarkdownRenderer:
    """行粒度的 Markdown → ANSI 渲染器（持有跨行状态，可配合流式输出）。"""

    def __init__(self, color=True, width=None):
        self.color = color
        self.width = width
        self._in_fence = False          # 是否处于 ``` 代码围栏内部
        self._fence_marker = ""         # 记录开围栏用的是 ``` 还是 ~~~，闭合需同类且不短于它
        self._table = []                # 表格行缓冲（表格要看完整块才能算列宽）

    # -- 对外：单行输入 -> 渲染后的行列表（空列表 = 此刻无输出） -------------
    def render_line(self, line):
        line = line.rstrip("\n").rstrip("\r")

        fence = _FENCE_RE.match(line)
        if self._in_fence:
            if (
                fence
                and fence.group(2)[0] == self._fence_marker[0]
                and len(fence.group(2)) >= len(self._fence_marker)
            ):
                self._in_fence = False
                self._fence_marker = ""
                return [self._paint(f"{DIM}  └{'─' * 3}{RESET}")]
            return [self._code_line(line)]
        if fence:
            self._in_fence = True
            self._fence_marker = fence.group(2)
            lang = fence.group(3) or "code"
            return [self._paint(f"{DIM}  ┌─ {lang}{RESET}")]

        # 表格：连续的 | 行先攒起来，遇到非表格行再统一吐出
        if self._looks_like_table_row(line):
            self._table.append(line)
            return []

        return self._flush_table() + self._block_line(line)

    def flush(self):
        """流结束：吐出攒着的表格；代码围栏没闭合也补上收尾线。"""
        out = self._flush_table()
        if self._in_fence:
            self._in_fence = False
            self._fence_marker = ""
            out.append(self._paint(f"{DIM}  └{'─' * 3}{RESET}"))
        return out

    # -- 内部 ---------------------------------------------------------------
    def _paint(self, text):
        return text if self.color else _ANSI_RE.sub("", text)

    def _code_line(self, line):
        return self._paint(f"{DIM}  │{RESET} {line}")

    def _looks_like_table_row(self, line):
        stripped = line.strip()
        return stripped.startswith("|") and stripped.count("|") >= 2

    def _block_line(self, line):
        """渲染一个非代码、非表格的块级行，返回只含一行的列表（空行为 [""]）。"""
        if not line.strip():
            return [""]
        return [self._paint(self._block_text(line))]

    def _block_text(self, line):
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text = render_inline(heading.group(2), self.color)
            style = _HEADING_STYLE[level]
            prefix = "" if level <= 2 else "  " * (level - 2)
            return f"{style}{prefix}{text}{RESET}"

        if _HR_RE.match(line):
            return f"{DIM}{'─' * min(self.width or 48, 48)}{RESET}"

        quote = _QUOTE_RE.match(line)
        if quote:
            body = quote.group(2)
            inner = self._block_text(body) if body.strip() else ""
            return f"{DIM}  ▏{RESET} {inner}" if inner else f"{DIM}  ▏{RESET}"

        ordered = _ORDERED_RE.match(line)
        if ordered:
            indent = " " * (len(ordered.group(1)) + 2)
            num = f"{BOLD}{ordered.group(2)}.{RESET}"
            return f"{indent}{num} {self._list_body(ordered.group(4))}"

        bullet = _BULLET_RE.match(line)
        if bullet:
            depth = len(bullet.group(1)) // 2
            marker = ("•", "◦", "▪")[min(depth, 2)]
            indent = " " * (len(bullet.group(1)) + 2)
            return f"{indent}{CYAN}{marker}{RESET} {self._list_body(bullet.group(3))}"

        return render_inline(line, self.color)

    def _list_body(self, body):
        """列表项正文：额外支持 `- [ ] 任务` 复选框。"""
        task = _TASK_RE.match(body)
        if task:
            done = task.group(1).lower() == "x"
            box = f"{GREEN}✔{RESET}" if done else f"{DIM}☐{RESET}"
            text = render_inline(task.group(2), self.color)
            return f"{box} {DIM}{text}{RESET}" if done else f"{box} {text}"
        return render_inline(body, self.color)

    def _flush_table(self):
        """把攒着的表格行渲染成对齐的行列表；没攒到东西时返回空列表。"""
        rows = self._table
        self._table = []
        if not rows:
            return []
        # 没有 |---|---| 分隔行就不是表格，退化成普通文本，避免误吞含竖线的正文
        seps = [i for i, r in enumerate(rows) if _TABLE_SEP_RE.match(r)]
        if not seps:
            return [self._paint(render_inline(r, self.color)) for r in rows]

        cells = [_split_table_row(r) for i, r in enumerate(rows) if i not in seps]
        ncol = max(len(c) for c in cells)
        cells = [c + [""] * (ncol - len(c)) for c in cells]
        rendered = [[render_inline(c, self.color) for c in row] for row in cells]
        widths = [
            max(display_width(row[i]) for row in rendered) for i in range(ncol)
        ]

        header_idx = 0 if seps and seps[0] == 1 else None
        lines = []
        for ri, row in enumerate(rendered):
            padded = []
            for i, cell in enumerate(row):
                pad = " " * max(0, widths[i] - display_width(cell))
                padded.append(cell + pad)
            body = f" {DIM}│{RESET} ".join(padded)
            if ri == header_idx:
                lines.append(self._paint(f"  {BOLD}{body}{RESET}"))
                rule = f"{DIM}─┼─{RESET}".join(DIM + "─" * w + RESET for w in widths)
                lines.append(self._paint(f"  {rule}"))
            else:
                lines.append(self._paint(f"  {body}"))
        return lines


class MarkdownStream:
    """流式适配器：喂入任意切分的片段，按「整行可渲染」的节奏吐出结果。

    ``feed`` / ``flush`` 返回的字符串用 ``print(..., end="")`` 直接输出即可：
    返回值自带必要的前置换行、且不带结尾换行，因此多次调用能无缝接续，
    光标也始终停在最后一行末尾（便于调用方自己决定何时收尾换行）。
    """

    def __init__(self, color=True, width=None):
        self.renderer = MarkdownRenderer(color=color, width=width)
        self._buf = ""
        self._emitted = 0        # 已输出的行数，用于决定是否需要前置换行
        self._pending_blank = 0  # 攒着的空行数；连续空行压成一个，开头的空行直接丢弃

    def feed(self, chunk):
        """喂入一段增量，返回此刻可以安全打印的文本。"""
        self._buf += chunk.replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        while "\n" in self._buf:
            raw, self._buf = self._buf.split("\n", 1)
            lines.extend(self.renderer.render_line(raw))
        return self._emit(lines)

    def flush(self):
        """流结束：渲染最后一行残留 + 吐出攒着的表格。"""
        lines = []
        if self._buf:
            lines.extend(self.renderer.render_line(self._buf))
            self._buf = ""
        lines.extend(self.renderer.flush())
        return self._emit(lines)

    @property
    def wrote_anything(self):
        return self._emitted > 0

    def _emit(self, lines):
        out = []
        for line in lines:
            if not line.strip():
                self._pending_blank += 1      # 空行先攒着，等有实际内容再决定要不要补
                continue
            if self._emitted:
                out.append("\n" * (1 + min(self._pending_blank, 1)))
            self._pending_blank = 0
            out.append(line)
            self._emitted += 1
        return "".join(out)


def render(text, color=True, width=None):
    """一次性渲染整段 Markdown（非流式场景 / 测试用）。"""
    stream = MarkdownStream(color=color, width=width)
    return stream.feed(text) + stream.flush()


def for_terminal(stream=None):
    """按当前终端能力创建一个 MarkdownStream：非 tty 或设了 NO_COLOR 就不上色。"""
    import os
    import shutil
    import sys

    stream = stream or sys.stdout
    color = bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
    try:
        width = shutil.get_terminal_size().columns
    except Exception:
        width = None
    return MarkdownStream(color=color, width=width)
