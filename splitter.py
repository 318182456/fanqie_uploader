# -*- coding: utf-8 -*-
"""读取 txt 并按章节切分。"""
import re
from dataclasses import dataclass
from pathlib import Path

import config

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


@dataclass
class Chapter:
    index: int      # 在 txt 里的顺序，从 1 开始
    number: int     # 「第X章」里的 X
    title: str      # 标题部分（不含「第X章」）
    full_title: str # 原始标题行
    volume: str     # 所属卷名（没有分卷则为空）
    paragraphs: list

    @property
    def char_count(self):
        return sum(len(p) for p in self.paragraphs)


def cn_to_int(s: str) -> int:
    """中文或阿拉伯数字转 int：'一百零二' -> 102，'１２' -> 12。"""
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "万":
            total += (section + num) * 10000
            section, num = 0, 0
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]  # 「十二」开头的十 = 1*10
            num = 0
    return total + section + num


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别编码：{path}")


def split_chapters(path) -> list:
    path = Path(path)
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n")
    pattern = re.compile(config.CHAPTER_REGEX)
    vol_pattern = re.compile(config.VOLUME_REGEX)

    chapters, current, volume = [], None, ""
    for line in text.split("\n"):
        if vol_pattern.match(line):
            volume = line.strip()
            continue
        m = pattern.match(line)
        if m:
            current = Chapter(
                index=len(chapters) + 1,
                number=cn_to_int(m.group(1)),
                title=m.group(2).strip(),
                full_title=line.strip(),
                volume=volume,
                paragraphs=[],
            )
            chapters.append(current)
            continue
        if current is None:
            continue  # 第一章之前的内容（书名、简介等）跳过
        para = line.strip() if config.STRIP_INDENT else line.rstrip()
        if para.strip():
            current.paragraphs.append(para)
    return chapters


def check(chapters) -> list:
    """检查序号是否连续、是否有空章/短章，返回警告列表。"""
    warns = []
    for prev, cur in zip(chapters, chapters[1:]):
        if cur.number != prev.number + 1:
            warns.append(f"序号不连续：{prev.full_title} -> {cur.full_title}")
    for c in chapters:
        if not c.paragraphs:
            warns.append(f"空章节：{c.full_title}")
        elif c.char_count < config.MIN_CHARS_WARN:
            warns.append(f"字数偏少（{c.char_count}字）：{c.full_title}")
    return warns
