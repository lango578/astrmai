"""一次性小工具：给 app.js 找未闭合的括号（本机没有 node，只能自己扫）。

栈式扫描：记录每个 ( 出现的行，遇到 ) 弹栈；结束时栈里剩下的就是漏闭合的那些行。
字符串/模板串/注释按字符状态跳过，够用即可（这是取证工具，不是解析器）。
它也被 tests/test_panel_api.py 当作"前端至少能被解析"的门 —— 面板打不开时，
后端全绿也没有意义。
"""

import sys
from pathlib import Path


def scan(text: str) -> tuple[list[tuple[int, int, str]], list[tuple[int, str]]]:
    """返回 (未闭合的 ( 位置, 多余的 ) 位置)。"""

    lines = text.splitlines()
    stack: list[tuple[int, int, str]] = []
    extra_close: list[tuple[int, str]] = []
    in_block_comment = False
    in_line_comment = False
    quote = None

    for line_no, raw in enumerate(lines, 1):
        in_line_comment = False
        col = 0
        while col < len(raw):
            ch = raw[col]
            nxt = raw[col + 1] if col + 1 < len(raw) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    col += 2
                    continue
                col += 1
                continue
            if in_line_comment:
                col += 1
                continue
            if quote:
                if ch == "\\":
                    col += 2
                    continue
                if ch == quote:
                    quote = None
                    col += 1
                    continue
                col += 1
                continue
            if ch == "/" and nxt == "/":
                in_line_comment = True
                col += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                col += 2
                continue
            if ch in "\"'`":
                quote = ch
                col += 1
                continue
            if ch == "(":
                stack.append((line_no, col, raw.strip()[:60]))
            elif ch == ")":
                if stack:
                    stack.pop()
                else:
                    extra_close.append((line_no, raw.strip()[:60]))
            col += 1
    return stack, extra_close


def main(argv: list[str]) -> int:
    path = Path(argv[1])
    unclosed, extra = scan(path.read_text(encoding="utf-8"))
    print(f"扫描 {len(path.read_text(encoding='utf-8').splitlines())} 行；未闭合的 ( 剩下 {len(unclosed)} 个；多余的 ) {len(extra)} 个")
    for line, col, snippet in unclosed:
        print(f"  行 {line} 第 {col} 列起：{snippet}")
    for line, snippet in extra:
        print(f"  多余 ) 行 {line}：{snippet}")
    return 1 if (unclosed or extra) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
