# -*- coding: utf-8 -*-
"""用 Playwright 操作番茄作家后台：存草稿，或直接定时发布到章节管理。"""
import datetime as dt
import json
import random
import re
import sys
import traceback
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import config
from splitter import cn_to_int

BASE_DIR = Path(__file__).parent
BOOKS_DIR = BASE_DIR / "books"
# 图形界面点「停止」时创建这个文件，上传循环在每章开始前检查，做完当前章再停
STOP_FLAG = BASE_DIR / ".stop"


# ---------- 按书区分的设置和目录 ----------
_BOOK = None  # 当前正在处理的书


def use_book(book):
    """切换到这本书：之后的设置（P/MODE）和文件路径都按这本书来。"""
    global _BOOK
    _BOOK = book


def P() -> dict:
    """当前书的发布设置：书里写的 publish 覆盖全局 config.PUBLISH。"""
    return {**config.PUBLISH, **((_BOOK or {}).get("publish") or {})}


# 番茄按作者等级限制发布字数（均为「小于」）：等级: (每日, 每月)
LEVEL_LIMITS = {0: (10000, 250000), 1: (10000, 250000),
                2: (20000, 500000), 3: (20000, 500000),
                4: (50000, 1000000)}
CHAPTER_MAX = 50000  # 单章上限


def plan_limits() -> tuple:
    """排期用的额度：平台上限 × (1 - reserve_ratio)，剩下的留给修改已发布章节。"""
    day, month = word_limits()
    keep = 1 - float(P().get("reserve_ratio") or 0)
    return int(day * keep), int(month * keep)


def word_limits() -> tuple:
    """(每日上限, 每月上限)。按 author_level 查表；手动写了 words_per_day/month 的优先。"""
    lv = min(int(P().get("author_level", 0)), 4)
    day, month = LEVEL_LIMITS[lv]
    return P().get("words_per_day") or day, P().get("words_per_month") or month


def MODE() -> str:
    return (_BOOK or {}).get("mode") or config.MODE


def book_dir(book=None, sub=None) -> Path:
    """books/<书名>_<book_id>/[sub]，存进度、截图、预览、错别字报告。
    带上 book_id：同一个 txt 重新建书时，新旧书的进度互不干扰。"""
    book = book or _BOOK
    name = re.sub(r'[\\/:*?"<>|]', "_", Path(book["txt"]).stem) + f"_{book['book_id']}"
    d = BOOKS_DIR / name / sub if sub else BOOKS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _book_by_id(book_id):
    if _BOOK and _BOOK["book_id"] == book_id:
        return _BOOK
    return next(b for b in config.BOOKS if b["book_id"] == book_id)


# ---------- 进度记录（断点续传） ----------
def _progress_file(book_id):
    suffix = "draft" if MODE() == "draft" else "publish"
    return book_dir(_book_by_id(book_id)) / f"progress_{suffix}.json"


def _load(book_id) -> dict:
    f = _progress_file(book_id)
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"done": []}


def load_progress(book_id) -> set:
    return set(_load(book_id)["done"])


def save_progress(book_id, index, when=None):
    data = _load(book_id)
    data["done"] = sorted(set(data["done"]) | {index})
    if when:
        data.setdefault("schedule", {})[str(index)] = when
    _progress_file(book_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_back_config(book, ch, when=None):
    """发布成功后写回 config.py 里这本书的：
    start      -> 下一章
    start_date -> 这次用的发布日期（下一章可能还能排进同一天）"""
    path = BASE_DIR / "config.py"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    key = f'"book_id": "{book["book_id"]}"'
    try:
        i = next(n for n, l in enumerate(lines) if key in l and not l.lstrip().startswith("#"))
    except StopIteration:
        print("    （config.py 里没找到这本书，未写回进度）")
        return

    depth = 0
    for j in range(i, min(i + 30, len(lines))):
        line = lines[j]
        indent = line[:len(line) - len(line.lstrip())]
        if '"start":' in line:
            done = "定时发布" if when else "存草稿"
            note = f"已{done}到第{ch.number}章" + (f"（{when:%Y-%m-%d %H:%M}）" if when else "")
            lines[j] = f'{indent}"start": {ch.index + 1},   # {note}，下次从这里开始\n'
        elif '"start_date":' in line and when:
            lines[j] = (f'{indent}"start_date": "{when:%Y-%m-%d}",   '
                        f'# 最近使用的发布日，下一章从这天起排（改晚可整体后推）\n')
        depth += line.count("{") - line.count("}")
        if depth < 0:  # 走出这本书的 {...}
            break

    path.write_text("".join(lines), encoding="utf-8")
    book["start"] = ch.index + 1
    if when and "publish" in book:
        book["publish"]["start_date"] = when.strftime("%Y-%m-%d")


# ---------- 定时发布排期 ----------
def plan_schedule(book, chapters) -> dict:
    """给还没发布的章节排发布时间：{txt顺序: datetime}。
    从已排过的最后一个发布日接着往下，按章节顺序装箱，
    同一发布日、同一发布月的累计字数都要小于作者等级对应的上限（见 LEVEL_LIMITS），
    本月额度不够就跳到下月 1 号；每月额度均摊到每一天，保证天天有更新。"""
    use_book(book)
    P_ = P()
    limit, mlimit = plan_limits()
    cap = P_.get("max_per_day")
    at = dt.time.fromisoformat(P_["time"])
    words = {c.index: c.char_count for c in chapters}

    sched = _load(book["book_id"]).get("schedule", {})
    usage, count, musage = {}, {}, {}
    for idx, v in sched.items():
        d = v[:10]
        usage[d] = usage.get(d, 0) + words.get(int(idx), 0)
        count[d] = count.get(d, 0) + 1
        musage[d[:7]] = musage.get(d[:7], 0) + words.get(int(idx), 0)
    # 从「已排过的最后一天」和 start_date 里较晚的那天开始排；
    # 想整体往后推（比如停更几天），把 start_date 改晚即可
    first = P_.get("start_date")  # 每本书自己的；没写就从明天开始
    day = dt.date.fromisoformat(first) if first else dt.date.today()
    if sched:
        day = max(day, max(dt.date.fromisoformat(v[:10]) for v in sched.values()))

    start = book.get("start") or 1
    plan = {}
    for c in chapters:
        if c.index < start or str(c.index) in sched:
            continue
        while True:
            k = day.isoformat()
            if mlimit and musage.get(k[:7], 0) + c.char_count >= mlimit:
                day = next_month(day)  # 本月额度用完
                continue
            day_cap = limit - 1
            if mlimit:
                # 按月均摊：本月剩余额度 ÷ 本月剩余天数，保证月底每天也有更新
                days_left = (next_month(day) - day).days
                budget = mlimit - musage.get(k[:7], 0) + usage.get(k, 0)
                day_cap = min(limit - 1, budget / days_left)
            fits = usage.get(k, 0) + c.char_count <= day_cap
            room = not cap or count.get(k, 0) < cap
            if (fits and room) or usage.get(k, 0) == 0:  # 单章就超限时独占一天
                break
            day += dt.timedelta(days=1)
        plan[c.index] = dt.datetime.combine(day, at)
        usage[k] = usage.get(k, 0) + c.char_count
        count[k] = count.get(k, 0) + 1
        musage[k[:7]] = musage.get(k[:7], 0) + c.char_count
    return plan


def next_month(d: dt.date) -> dt.date:
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def schedule_of(book, ch, chapters) -> dt.datetime:
    return plan_schedule(book, chapters)[ch.index]


# ---------- 浏览器 ----------
def open_browser(pw):
    opts = dict(
        user_data_dir=str(BASE_DIR / config.PROFILE_DIR),
        headless=False,
        viewport={"width": 1400, "height": 900},
        locale="zh-CN",
        # 降低自动化特征：去掉 navigator.webdriver 标记和「受自动测试软件控制」提示条
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    try:
        try:
            # 优先用本机正式版 Chrome，指纹和日常浏览器一致
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **opts)
        except Exception as e:
            if "exitCode=21" in str(e):
                raise
            print(f"  （没能启动本机 Chrome，改用 Playwright 自带的 Chromium：{str(e).splitlines()[0]}）")
            ctx = pw.chromium.launch_persistent_context(**opts)
    except Exception as e:
        if "exitCode=21" in str(e):
            raise SystemExit(
                "\n浏览器启动失败：登录目录被占用。\n"
                "请关闭其他正在运行的登录/上传窗口（黑窗口），然后重试。"
            ) from None
        raise
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(15000)
    return ctx, page


def find(page, key, timeout=8000):
    """在候选选择器里找第一个可见的元素。"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in config.SELECTORS[key]:
            loc = page.locator(sel).first
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                pass
        time.sleep(0.3)
    return None


def close_tour(page):
    """关掉新建章节页的引导助手（reactour，1/3 → 3/3）。
    注意只点引导卡片里的按钮，页面上还有个发布用的「下一步」。"""
    for _ in range(6):
        tour = page.locator(".reactour__helper--is-open").first
        try:
            if not tour.is_visible():
                return
        except Exception:
            return
        btn = tour.locator("button").last
        try:
            btn.click(timeout=2000)
        except Exception:
            page.keyboard.press("Escape")
        time.sleep(0.6)


def dismiss_popups(page):
    close_tour(page)
    for sel in config.SELECTORS["dismiss"]:
        loc = page.locator(sel).first
        try:
            if loc.is_visible():
                loc.click(timeout=1000)
                time.sleep(0.3)
        except Exception:
            pass


def visible_dialogs(page) -> list:
    """当前可见的弹窗文字，用于报错信息。"""
    return page.evaluate(r"""()=>[...document.querySelectorAll('.byte-modal,.arco-modal,[role=dialog]')]
        .filter(e=>e.offsetWidth&&e.innerText.trim())
        .map(e=>e.innerText.trim().replace(/\n+/g,' / ').slice(0,200))""")


def login():
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        page.goto(config.URLS["home"])
        print("请在打开的浏览器里扫码登录番茄作家后台。")
        print("登录成功、能看到作品列表后，直接关掉浏览器窗口即可。")
        page.wait_for_event("close", timeout=0)
        try:
            ctx.close()
        except Exception:
            pass
        print("登录状态已保存。")


def inspect(book_id):
    """打开新建章节页并启动 Playwright Inspector，用来重新取选择器。"""
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        page.goto(config.URLS["new_chapter"].format(book_id=book_id))
        page.pause()
        ctx.close()


# ---------- 分卷 ----------
def parse_volume(s):
    """'第三卷 武林外传' / '第三卷：武林外传' -> (3, '武林外传')"""
    m = re.match(r"\s*第\s*([0-9０-９零〇一二两三四五六七八九十百千万]+)\s*卷[\s:：]*(.*)", s or "")
    if not m:
        return None, (s or "").strip()
    return cn_to_int(m.group(1)), m.group(2).strip()


def ensure_volume(page, volume):
    """在新建章节页顶部把分卷切到 volume；番茄上没有这个卷就新建。"""
    if not volume:
        return
    num, name = parse_volume(volume)
    cur = page.locator(".publish-header-volume-name").first.inner_text().strip()
    if parse_volume(cur)[1] == name:
        return

    page.locator(".publish-header-volume-wrap").first.click()
    modal = page.locator(".editor-volume").first
    modal.wait_for(state="visible")
    time.sleep(0.5)

    def find_item():
        items = modal.locator(".editor-volume-list-item-normal")
        for i in range(items.count()):
            it = items.nth(i)
            if parse_volume(it.locator("span").first.inner_text())[1] == name:
                return it
        return None

    target = find_item()
    if target is None:
        # 同卷号、名字不同（如新书自带的「第一卷：默认」）：改名成 txt 里的卷名
        items = modal.locator(".editor-volume-list-item-normal")
        for i in range(items.count()):
            it = items.nth(i)
            old_label = it.locator("span").first.inner_text().strip()
            if parse_volume(old_label)[0] == num:
                if len(name) > 16:
                    raise RuntimeError(f"卷名超过 16 字，番茄不允许：{name}")
                # 编辑图标鼠标悬停才显示
                it.hover()
                time.sleep(0.5)
                it.locator("i.tomato-edit").click(force=True)
                edit = modal.locator(".editor-volume-list-item-edit").first
                edit.wait_for(state="visible")
                edit.locator("input").fill(name)
                edit.locator("i.tomato-confirm").click()
                time.sleep(2)
                target = find_item()
                if target is None:
                    raise RuntimeError(f"把「{old_label}」改名为「{name}」后没在列表里找到")
                print(f"    分卷改名：{old_label} → {name}")
                break
    if target is None:
        if not P()["create_volume"]:
            raise RuntimeError(f"番茄上没有分卷「{volume}」，且配置为不自动创建")
        if len(name) > 16:
            raise RuntimeError(f"卷名超过 16 字，番茄不允许：{name}")
        modal.locator(".editor-volume-footer-add-volume").click()
        edit = modal.locator(".editor-volume-list-item-edit").first
        edit.wait_for(state="visible")
        # 番茄自动给出下一个卷号前缀（如「第四卷：」），核对是否和 txt 一致
        prefix = edit.locator(".editor-volume-list-item-edit-left").evaluate(
            "e=>e.childNodes[0].textContent")
        if parse_volume(prefix)[0] != num:
            edit.locator("i.tomato-cancel").click()
            modal.locator("button:has-text('取消')").click()
            raise RuntimeError(
                f"番茄要新建的是「{prefix}」，但 txt 里是「{volume}」，卷号对不上，已取消")
        edit.locator("input").fill(name)
        edit.locator("i.tomato-confirm").click()
        time.sleep(2)
        target = find_item()
        if target is None:
            raise RuntimeError(f"新建分卷「{volume}」后没在列表里找到: {visible_dialogs(page)}")
        print(f"    已新建分卷：{prefix}{name}")

    target.click()
    time.sleep(0.5)
    modal.locator("button:has-text('确定')").click()
    time.sleep(1.5)
    cur = page.locator(".publish-header-volume-name").first.inner_text().strip()
    if parse_volume(cur)[1] != name:
        raise RuntimeError(f"切换分卷失败：当前是「{cur}」，应为「{volume}」")


# ---------- 填写章节 ----------
def fill_editor(page, editor, paragraphs):
    try:
        editor.click(timeout=3000)
    except Exception:
        editor.focus()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    for i, p in enumerate(paragraphs):
        page.keyboard.insert_text(p)
        if i < len(paragraphs) - 1:
            page.keyboard.press("Enter")


def open_and_fill(page, book_id, ch, with_volume=False):
    """打开新建章节页，（可选）切分卷，填序号/标题/正文并核对。"""
    # 页面一直有轮询请求，等不到 networkidle，改为等标题框出现
    page.goto(config.URLS["new_chapter"].format(book_id=book_id),
              wait_until="domcontentloaded")
    find(page, "title", timeout=20000)
    time.sleep(2)  # 等引导助手弹出来
    dismiss_popups(page)

    title_box = find(page, "title")
    if title_box is None:
        if "login" in page.url or "passport" in page.url:
            raise RuntimeError("登录已失效，请先运行 python main.py login")
        raise RuntimeError("找不到标题输入框，请用 inspect 检查选择器")

    if with_volume:
        ensure_volume(page, ch.volume)

    if config.TITLE_MODE == "split":
        no_box = find(page, "chapter_no", timeout=2000)
        if no_box is not None:
            no_box.fill(str(ch.number))
            title_box.fill(ch.title)
        else:
            title_box.fill(ch.full_title)
    else:
        title_box.fill(ch.full_title)

    editor = find(page, "content")
    if editor is None:
        raise RuntimeError("找不到正文编辑器，请用 inspect 检查选择器")
    fill_editor(page, editor, ch.paragraphs)
    time.sleep(0.8)

    # 正文字数要和 txt 基本一致
    got = len(editor.inner_text().replace("\n", "").replace(" ", ""))
    if got < ch.char_count * 0.95:
        raise RuntimeError(f"正文没填进去（编辑器 {got} 字 / 原文 {ch.char_count} 字）")
    wait_editor_synced(page, editor)
    return got


def header_word_count(page) -> int:
    text = page.evaluate("()=>document.body.innerText.slice(0, 600)")
    m = re.search(r"正文字数\s*(\d+)", text)
    return int(m.group(1)) if m else -1


def wait_editor_synced(page, editor):
    """等番茄识别到正文（顶部字数非 0、「下一步」可点）。
    编辑器没初始化完就输入时，内容看得见但字数是 0，按钮一直是灰的。"""
    btn = page.locator("button.publish-button").first
    for nudge in range(3):
        for _ in range(20):
            if header_word_count(page) > 0 and btn.is_enabled():
                return
            time.sleep(0.5)
        # 在正文末尾敲个空格再删掉，触发编辑器重新统计
        editor.click()
        page.keyboard.press("Control+End")
        page.keyboard.type(" ")
        page.keyboard.press("Backspace")
    raise RuntimeError(f"正文已填入但番茄没识别（顶部字数 {header_word_count(page)}，「下一步」不可点）")


class NotSubmitted(RuntimeError):
    """点「确认发布」之前出的错：章节肯定没发出去，可以放心重试。"""


def save_draft(page, book_id, ch):
    open_and_fill(page, book_id, ch)
    dismiss_popups(page)
    btn = find(page, "save_draft")
    if btn is None:
        raise RuntimeError("找不到「存草稿」按钮")
    btn.click()

    # 点完要等保存请求真正完成，否则马上跳转/关浏览器会丢稿
    time.sleep(3)
    if find(page, "save_ok", timeout=12000) is None:
        err = page.locator(".arco-message-error, .byte-message-error").first
        if err.count() and err.is_visible():
            raise RuntimeError(f"保存失败：{err.inner_text()}")
        raise RuntimeError("没等到「已保存到云端」，可能没存上")
    time.sleep(2)


# ---------- 定时发布 ----------
def set_picker(page, inp, value):
    inp.click()
    time.sleep(0.3)
    page.keyboard.press("Control+A")
    page.keyboard.type(value, delay=30)
    page.keyboard.press("Enter")
    time.sleep(0.5)
    if inp.input_value() != value:
        raise RuntimeError(f"日期/时间没填进去：期望 {value}，实际 {inp.input_value()}")


class DailyLimit(RuntimeError):
    """番茄提示「提交字数超出每日/每月上限」：这一章没提交，换个发布日期可以再试。"""
    def __init__(self, text):
        super().__init__(text)
        self.monthly = "每月" in text


def publish(page, book_id, ch, when: dt.datetime, rehearse=False) -> dt.datetime:
    """定时发布一章，返回实际使用的发布时间。
    遇到每日上限挪到下一天，遇到每月上限挪到下月 1 号。"""
    try:
        m = prepare_publish(page, book_id, ch)
        fill_modal(page, m, ch, when)
    except Exception as e:
        raise NotSubmitted(f"{type(e).__name__}: {e}") from e

    if rehearse:
        shot = book_dir(sub="screenshots") / f"演练_第{ch.number}章.png"
        page.screenshot(path=str(shot))
        print(f"    【演练】发布设置已填好：AI=否，{when_label(when)}")
        print(f"    【演练】截图：{shot}")
        m.locator("button:has-text('取消')").first.click()
        return when

    max_shift = P().get("limit_shift_days", 7)
    for shift in range(max_shift + 1):
        try:
            confirm_publish(page, ch, m)
            break
        except DailyLimit as e:
            if shift == max_shift:
                raise NotSubmitted(f"往后挪了 {max_shift} 次仍超出上限：{e}") from e
            if e.monthly:
                when = dt.datetime.combine(next_month(when.date()), when.time())
            else:
                when += dt.timedelta(days=1)
            print(f"    {e} → 改到 {when:%m-%d %H:%M} 再试")
            time.sleep(4)  # 等报错提示消失，免得下一轮误判
            try:
                m = reopen_modal(page, ch)
                fill_modal(page, m, ch, when)
            except Exception as e2:
                raise NotSubmitted(f"改日期重试时出错：{type(e2).__name__}: {e2}") from e2

    # 真正的成功判断：重新打开新建章节页，顶部「上次提交」应是这一章
    verify_submitted(page, book_id, ch)
    return when


def wait_modal(page, ch):
    """点「下一步」后依次可能出现：内容检测方式 → 错别字提示 → 发布设置。"""
    m = page.locator(".publish-confirm-container-new").first
    deadline = time.time() + 90
    while not m.is_visible():
        if time.time() > deadline:
            raise RuntimeError(f"点「下一步」后 90 秒没出现发布设置，当前弹窗：{visible_dialogs(page)}")
        handle_prompts(page, ch)
        time.sleep(1)
    time.sleep(0.8)
    return m


def prepare_publish(page, book_id, ch):
    """填好章节，点「下一步」直到出现发布设置弹窗。"""
    open_and_fill(page, book_id, ch, with_volume=True)
    dismiss_popups(page)
    page.locator("button.publish-button").first.click()
    return wait_modal(page, ch)


def reopen_modal(page, ch):
    """报错后发布设置弹窗若已关闭，重新点「下一步」打开（正文还在页面上）。"""
    m = page.locator(".publish-confirm-container-new").first
    if m.is_visible():
        return m
    page.locator("button.publish-button").first.click()
    return wait_modal(page, ch)


def is_today(when: dt.datetime) -> bool:
    return when.date() == dt.date.today()


def when_label(when: dt.datetime) -> str:
    return "今天直接发布" if is_today(when) else f"定时 {when:%Y-%m-%d %H:%M}"


def fill_modal(page, m, ch, when):
    """核对分卷，选「是否使用AI」；排到今天的直接发布，之后的打开定时发布并填日期时间。"""
    vol_text = m.locator(".card-content-line").filter(
        has=page.locator(".card-content-line-label", has_text="分卷")
    ).locator(".card-content-line-control").first.inner_text().strip()
    if ch.volume and parse_volume(vol_text)[1] != parse_volume(ch.volume)[1]:
        raise RuntimeError(f"发布设置里的分卷是「{vol_text}」，应为「{ch.volume}」")

    ai = m.locator("label.arco-radio", has_text="是" if P()["use_ai"] else "否").first
    ai.click()
    time.sleep(0.3)
    if "arco-radio-checked" not in (ai.get_attribute("class") or ""):
        raise RuntimeError("「是否使用AI」没选上")

    sw = m.locator(".arco-switch").first
    if is_today(when):
        if sw.get_attribute("aria-checked") == "true":
            sw.click()
            time.sleep(0.8)
        if sw.get_attribute("aria-checked") == "true":
            raise RuntimeError("「定时发布」关不掉，今天的章节没法直接发布")
        return
    if sw.get_attribute("aria-checked") != "true":
        sw.click()
        time.sleep(0.8)
    set_picker(page, m.locator("input[placeholder='请选择日期']").first, when.strftime("%Y-%m-%d"))
    set_picker(page, m.locator("input[placeholder='请选择时间']").first, when.strftime("%H:%M"))


def confirm_publish(page, ch, m):
    m.locator("button:has-text('确认发布')").first.click()

    # 确认后也可能再弹错别字提示等；处理完再等几秒让提交请求发出去
    deadline = time.time() + 30
    quiet = 0
    while time.time() < deadline:
        time.sleep(1)
        err = page.locator(".arco-message-error, .byte-message-error").first
        if err.count() and err.is_visible():
            text = err.inner_text().strip()
            if "每日上限" in text or "每月上限" in text:
                raise DailyLimit(text)
            raise RuntimeError(f"发布失败：{text}")
        if handle_prompts(page, ch):
            quiet = 0
            continue
        quiet = quiet + 1 if not m.is_visible() else 0
        if quiet >= 3:
            return
    raise RuntimeError(f"点「确认发布」后 30 秒仍有弹窗未处理：{visible_dialogs(page)}")


def handle_prompts(page, ch) -> bool:
    """处理发布流程中的已知弹窗，处理了返回 True；遇到未知弹窗抛错。"""
    for d in visible_dialogs(page):
        if "发布设置" in d or "检测中" in d:
            continue
        if "内容检测方式" in d:
            btn = "全面检测" if P()["content_check"] == "full" else "仅基础检测"
            page.locator(f"button:visible:has-text('{btn}')").last.click()
            print(f"    内容检测：{btn}")
            time.sleep(1)
            return True
        if "错别字" in d:
            if P()["typo_prompt"] != "submit":
                raise RuntimeError(f"番茄提示有错别字未修改，已按配置停下：{d}")
            page.locator(".byte-modal button:visible:has-text('提交'), .arco-modal button:visible:has-text('提交')").last.click()
            print("    ⚠ 番茄提示有错别字未修改，已点「提交」（记入 错别字待改.txt）")
            with open(book_dir() / "错别字待改.txt", "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M}  {ch.volume}  {ch.full_title}\n")
            time.sleep(1)
            return True
        if "非章节内容" in d:
            # 番茄怀疑章末有作者的话等无关内容（常见误判：结尾的总结段落、「全书完」）
            if P().get("non_chapter_prompt", "submit") != "submit":
                raise RuntimeError(f"番茄提示章末可能有非章节内容，已按配置停下：{d}")
            page.get_by_role("button", name="提交", exact=True).last.click()
            print("    ⚠ 番茄提示章末可能有非章节内容，已点「提交」（记入 审核风险提示.txt）")
            with open(book_dir() / "审核风险提示.txt", "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M}  {ch.volume}  {ch.full_title}"
                        f"  章末可能有非章节内容\n")
            time.sleep(1)
            return True
        if "请在发布时间前30分钟" in d:
            btn = page.locator("button:visible:has-text('我知道了')")
            if not btn.count():
                continue  # 弹窗已关，只是还留在页面结构里
            btn.last.click()
            time.sleep(0.5)
            return True
        raise RuntimeError(f"出现未知弹窗，请人工确认：{d}")
    return False


def last_submitted(page) -> str:
    text = page.evaluate("()=>document.body.innerText.slice(0, 600)")
    m = re.search(r"上次提交[：:]\s*([\s\S]{0,80}?)\s*已保存", text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def verify_submitted(page, book_id, ch):
    # 「上次提交」偶尔还没渲染出来就读到空：空的话刷新重读，别误判成失败
    got = ""
    for _ in range(4):
        page.goto(config.URLS["new_chapter"].format(book_id=book_id), wait_until="domcontentloaded")
        find(page, "title", timeout=20000)
        time.sleep(2)
        got = last_submitted(page)
        if got:
            break
        time.sleep(3)
    if not got:
        raise RuntimeError(f"读不到「上次提交」，第{ch.number}章可能已提交，请到章节管理确认后再重跑")
    if f"第{ch.number}章" not in got:
        raise RuntimeError(f"发布后核对失败：「上次提交」是「{got}」，不是第{ch.number}章，请到章节管理确认")
    print(f"    ✓ 已提交（上次提交：{got}）")


# ---------- 整本书 ----------
# ---------- 修改已排期章节的发布时间 ----------
def manage_url(book):
    from urllib.parse import quote
    return (f"https://fanqienovel.com/main/writer/chapter-manage/"
            f"{book['book_id']}&{quote(Path(book['txt']).stem)}?type=1")


def find_chapter_row(page, book, ch):
    """章节管理里切到这一章的卷，返回 (编辑链接, 当前发布时间)。"""
    page.goto(manage_url(book), wait_until="domcontentloaded")
    page.locator("tr.arco-table-tr").first.wait_for(timeout=20000)
    time.sleep(1.5)
    # 默认显示最新一卷；这一章不在当前列表里才切分卷
    if ch.volume and not page.locator("tr.arco-table-tr", has_text=f"第{ch.number}章 ").count():
        name = parse_volume(ch.volume)[1]
        cur = page.get_by_text(re.compile(r"^第.+卷：")).first
        cur.click()
        time.sleep(1)
        page.get_by_text(re.compile(r"^第.+卷：" + re.escape(name))).last.click()
        time.sleep(2.5)
    for _ in range(20):  # 翻页找
        row = page.locator("tr.arco-table-tr", has_text=f"第{ch.number}章 ").first
        if row.count():
            link = row.locator("a.link")  # 审核中的章节没有编辑链接
            href = link.first.get_attribute("href") if link.count() else None
            # 第 5 列是发布时间（已发布的章节没有定时图标，直接读文字）
            when = row.locator("td").nth(4).inner_text().strip()
            return ("https://fanqienovel.com" + href) if href else None, when
        nxt = page.locator(".arco-pagination-item-next").first
        if not nxt.count() or "disabled" in (nxt.get_attribute("class") or ""):
            break
        nxt.click()
        time.sleep(2)
    raise RuntimeError(f"章节管理里没找到第{ch.number}章（卷：{ch.volume}）")


def reschedule(book, ch, when: dt.datetime):
    """把已定时发布的一章改到新的发布时间（不动正文）。"""
    use_book(book)
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            url, old = find_chapter_row(page, book, ch)
            print(f"  第{ch.number}章 当前发布时间：{old} → 改为 {when:%Y-%m-%d %H:%M}")
            if old == f"{when:%Y-%m-%d %H:%M}":
                print("  已经是这个时间，不用改。")
                return
            if dt.datetime.strptime(old, "%Y-%m-%d %H:%M") < dt.datetime.now() + dt.timedelta(minutes=35):
                raise RuntimeError("离原定发布时间不到 30 分钟，番茄不允许修改")

            page.goto(url, wait_until="domcontentloaded")
            find(page, "title", timeout=20000)
            time.sleep(2)
            handle_prompts(page, ch)  # 「请在发布时间前30分钟提交修改」
            close_tour(page)
            handle_prompts(page, ch)
            if header_word_count(page) <= 0:
                raise RuntimeError("编辑页正文字数为 0，没加载出来，不提交")

            page.locator("button.publish-button").first.click()
            m = wait_modal(page, ch)
            fill_modal(page, m, ch, when)
            confirm_publish(page, ch, m)

            # 回章节管理核对
            _, now = find_chapter_row(page, book, ch)
            if now != f"{when:%Y-%m-%d %H:%M}":
                raise RuntimeError(f"提交后章节管理里的时间是 {now}，不是 {when:%Y-%m-%d %H:%M}")
            data = _load(book["book_id"])
            data.setdefault("schedule", {})[str(ch.index)] = f"{when:%Y-%m-%d %H:%M}"
            _progress_file(book["book_id"]).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  ✓ 已改好，章节管理里显示 {now}")
        except Exception:
            shot = book_dir(sub="screenshots") / f"改时间_第{ch.number}章.png"
            try:
                page.screenshot(path=str(shot))
                print(f"  截图：{shot}")
            except Exception:
                pass
            raise
        finally:
            ctx.close()


def publish_one(book, ch, when: dt.datetime):
    """把单独一章（如补发的漏章）定时发布到指定时间；不改 config 的 start。"""
    use_book(book)
    if str(ch.index) in _load(book["book_id"]).get("schedule", {}):
        raise SystemExit(f"第{ch.number}章在进度里已经发布过了，不重复发")
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            print(f"  第{ch.number}章 {ch.title}（{ch.char_count}字） → {when:%Y-%m-%d %H:%M}")
            when = publish(page, book["book_id"], ch, when)
            save_progress(book["book_id"], ch.index, when.strftime("%Y-%m-%d %H:%M"))
            print(f"  ✓ 完成，定时 {when:%Y-%m-%d %H:%M}")
        except Exception:
            shot = book_dir(sub="screenshots") / f"单章发布_第{ch.number}章.png"
            try:
                page.screenshot(path=str(shot))
                print(f"  截图：{shot}")
            except Exception:
                pass
            raise
        finally:
            ctx.close()


def update_content(book, chapters_to_update):
    """用 txt 里的新版正文替换已发布/已定时章节的内容（标题、序号一起核对）。
    已定时的章节保持原发布时间不变；已发布的章节番茄会重新审核。"""
    use_book(book)
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            for ch in chapters_to_update:
                url, when = find_chapter_row(page, book, ch)
                print(f"  第{ch.number}章 {ch.title}（新版 {ch.char_count}字，发布时间 {when}）")
                if url is None:
                    print("    这一章正在审核中，暂时不能编辑，跳过")
                    continue
                if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", when):
                    t = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
                    if dt.datetime.now() < t < dt.datetime.now() + dt.timedelta(minutes=35):
                        print("    离发布不到 30 分钟，番茄不允许修改，跳过（发布后再运行）")
                        continue

                page.goto(url, wait_until="domcontentloaded")
                find(page, "title", timeout=20000)
                time.sleep(2)
                for _ in range(3):
                    b = page.locator("button:visible:has-text('我知道了')")
                    if b.count():
                        b.last.click()
                        time.sleep(0.8)
                close_tour(page)
                before = header_word_count(page)

                # 核对/补齐序号和标题，再整体替换正文
                no_box = find(page, "chapter_no", timeout=2000)
                if no_box is not None and no_box.input_value() != str(ch.number):
                    raise RuntimeError(f"编辑页序号是 {no_box.input_value()}，不是 {ch.number}，不改")
                title_box = find(page, "title")
                if title_box.input_value() != ch.title:
                    title_box.fill(ch.title)
                editor = find(page, "content")
                fill_editor(page, editor, ch.paragraphs)
                time.sleep(0.8)
                got = len(editor.inner_text().replace("\n", "").replace(" ", ""))
                if got < ch.char_count * 0.95:
                    raise RuntimeError(f"正文没填进去（编辑器 {got} 字 / 原文 {ch.char_count} 字）")
                wait_editor_synced(page, editor)
                print(f"    正文已替换：{before} → {header_word_count(page)} 字")

                page.locator("button.publish-button").first.click()
                m = wait_modal(page, ch)
                ai = m.locator("label.arco-radio", has_text="是" if P()["use_ai"] else "否").first
                ai.click()
                time.sleep(0.3)
                if "arco-radio-checked" not in (ai.get_attribute("class") or ""):
                    raise RuntimeError("「是否使用AI」没选上")
                confirm_publish(page, ch, m)

                # 回章节管理核对字数
                page2_url, when2 = find_chapter_row(page, book, ch)
                row = page.locator("tr.arco-table-tr", has_text=f"第{ch.number}章 ").first
                cells = row.locator("td").all_inner_texts()
                words, status = cells[1].strip(), cells[3].strip()
                # 已发布章节改完会进「修改审核中」，字数要审核通过后才更新
                ok = "审核中" in status or (
                    words.isdigit() and abs(int(words) - ch.char_count) <= max(30, ch.char_count * 0.02))
                print(f"    {'✓' if ok else '⚠'} 章节管理：{words} 字，{status}，发布时间 {when2}")
                if not ok:
                    raise RuntimeError(f"提交后章节管理里是 {words} 字，和新版 {ch.char_count} 字对不上")
                time.sleep(random.uniform(*config.DELAY_RANGE))
        except Exception:
            shot = book_dir(sub="screenshots") / f"更新正文_{dt.datetime.now():%H%M%S}.png"
            try:
                page.screenshot(path=str(shot))
                print(f"  截图：{shot}")
            except Exception:
                pass
            raise
        finally:
            ctx.close()


# ---------- 全书核对：番茄后台 vs txt ----------
def _read_rows(page):
    """当前列表所有页的行：[标题, 字数, 错别字, 审核状态, 发布时间]"""
    rows = []
    for _ in range(100):
        page.locator("tr.arco-table-tr").first.wait_for(timeout=15000)
        time.sleep(0.8)
        rows += page.evaluate(r"""()=>[...document.querySelectorAll('tr.arco-table-tr')]
            .map(tr=>[...tr.querySelectorAll('td')].map(td=>td.innerText.trim()))
            .filter(t=>t.length>=2)""")
        nxt = page.locator(".arco-pagination-item-next").first
        if not nxt.count() or "disabled" in (nxt.get_attribute("class") or ""):
            break
        nxt.click()
        time.sleep(1.5)
    return rows


def check_book(book, chapters):
    """读出章节管理里所有分卷的章节和草稿箱，与 txt 比对，输出检查报告。"""
    use_book(book)
    online = []  # (卷名, 标题, 字数, 状态, 发布时间)
    drafts = []
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            page.goto(manage_url(book), wait_until="domcontentloaded")
            page.locator("tr.arco-table-tr").first.wait_for(timeout=20000)
            time.sleep(1.5)
            vol_re = re.compile(r"^第.+卷：")
            page.get_by_text(vol_re).first.click()
            time.sleep(1)
            volumes = []
            for t in page.get_by_text(vol_re).all_inner_texts():
                t = t.strip()
                if t and t not in volumes:
                    volumes.append(t)
            page.keyboard.press("Escape")
            time.sleep(0.5)
            print(f"  番茄上共 {len(volumes)} 个分卷")
            for v in volumes:
                cur = page.get_by_text(vol_re).first
                if cur.inner_text().strip() != v:
                    cur.click()
                    time.sleep(1)
                    page.get_by_text(v, exact=True).last.click()
                    time.sleep(2.5)
                rows = _read_rows(page)
                print(f"    {v}：{len(rows)} 章")
                for r in rows:
                    online.append((v, r[0], r[1], r[3] if len(r) > 3 else "", r[4] if len(r) > 4 else ""))
            # 草稿箱
            page.goto(manage_url(book).replace("type=1", "type=2"), wait_until="domcontentloaded")
            time.sleep(5)
            if page.locator("tr.arco-table-tr").count():
                drafts = [r[0] for r in _read_rows(page)]
        finally:
            ctx.close()

    # ---- 比对 ----
    lines = []
    out = lines.append
    num_re = re.compile(r"^第(\d+)章\s*(.*)")
    by_num = {}
    for v, title, words, status, when in online:
        m = num_re.match(title)
        if m:
            by_num.setdefault(int(m.group(1)), []).append((v, m.group(2).rstrip("…").strip(), words, status, when))
    txt_nums = {c.number: c for c in chapters}

    missing = [n for n in sorted(txt_nums) if n not in by_num]
    dup = {n: v for n, v in by_num.items() if len(v) > 1}
    extra = [n for n in by_num if n not in txt_nums]
    out(f"txt：{len(chapters)} 章（第{min(txt_nums)}~{max(txt_nums)}章）；"
        f"番茄章节管理：{len(online)} 章；草稿箱：{len(drafts)} 篇")
    out(f"\n【漏发】{len(missing)} 章：" + ("无" if not missing else
        "、".join(f"第{n}章 {txt_nums[n].title}" for n in missing)))
    out(f"【重复】{len(dup)} 章：" + ("无" if not dup else
        "；".join(f"第{n}章×{len(v)}（{'/'.join(x[4] for x in v)}）" for n, v in dup.items())))
    out(f"【txt 里没有的章节号】" + ("无" if not extra else "、".join(f"第{n}章" for n in sorted(extra))))

    title_diff, word_diff, bad_status, wrong_vol = [], [], [], []
    for n, c in txt_nums.items():
        for v, title, words, status, when in by_num.get(n, []):
            if title and not (c.title.startswith(title) or title.startswith(c.title)):
                title_diff.append(f"第{n}章 txt「{c.title}」/ 番茄「{title}」")
            if words.isdigit() and abs(int(words) - c.char_count) > max(30, c.char_count * 0.02):
                word_diff.append(f"第{n}章 txt {c.char_count} 字 / 番茄 {words} 字")
            if status and status not in ("已发布", "待发布"):
                bad_status.append(f"第{n}章 {status}")
            if c.volume and parse_volume(c.volume)[1] != parse_volume(v)[1]:
                wrong_vol.append(f"第{n}章 在「{v}」，txt 是「{c.volume}」")
    for name, items in (("标题不一致", title_diff), ("字数差异大", word_diff),
                        ("审核状态异常", bad_status), ("分卷不对", wrong_vol)):
        out(f"【{name}】{len(items)} 章" + ("" if items else "：无"))
        for x in items:
            out(f"    {x}")

    # 排期：每天都有更新、单日不超 1 万字
    per_day = {}
    for n, items in by_num.items():
        for v, title, words, status, when in items:
            if re.match(r"\d{4}-\d{2}-\d{2}", when):
                d = when[:10]
                per_day.setdefault(d, [0, []])
                per_day[d][0] += int(words) if words.isdigit() else 0
                per_day[d][1].append(n)
    if per_day:
        days = sorted(per_day)
        d0, d1 = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
        gaps = [(d0 + dt.timedelta(i)).isoformat() for i in range((d1 - d0).days + 1)
                if (d0 + dt.timedelta(i)).isoformat() not in per_day]
        over = [f"{d}（{per_day[d][0]}字）" for d in days if per_day[d][0] >= word_limits()[0]]
        out(f"\n发布日期范围：{days[0]} ~ {days[-1]}")
        out(f"【没有更新的日子】{len(gaps)} 天：" + ("无" if not gaps else "、".join(gaps)))
        out(f"【单日达到 {word_limits()[0]} 字上限】" + ("无" if not over else "、".join(over)))
    if drafts:
        out(f"\n【草稿箱残留】{len(drafts)} 篇：" + "、".join(drafts))

    report = "\n".join(lines)
    path = book_dir() / f"检查报告_{dt.datetime.now():%Y%m%d_%H%M}.txt"
    path.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n报告已保存：{path}")


def pending(book, chapters):
    """本次要处理的章节（发布模式下按 days_per_run 截断）。"""
    use_book(book)
    done = load_progress(book["book_id"])
    start = book.get("start") or 1
    end = book.get("end") or len(chapters)
    todo = [c for c in chapters if start <= c.index <= end and c.index not in done]
    if MODE() == "publish" and todo and P().get("days_per_run"):
        plan = plan_schedule(book, chapters)
        last_day = plan[todo[0].index].date() + dt.timedelta(
            days=P()["days_per_run"] - 1)
        todo = [c for c in todo if plan[c.index].date() <= last_day]
    return todo, done


def upload_book(book, chapters, dry_run=False, rehearse=False):
    use_book(book)
    book_id = book["book_id"]
    todo, done = pending(book, chapters)
    mode = "定时发布" if MODE() == "publish" else "存草稿"

    print(f"\n《{Path(book['txt']).stem}》共 {len(chapters)} 章，模式：{mode}，"
          f"已处理 {len(done)} 章，本次 {len(todo)} 章")
    if MODE() == "publish" and todo:
        plan = plan_schedule(book, chapters)
        print(f"  发布时间：{plan[todo[0].index]:%Y-%m-%d %H:%M}"
              f" ~ {plan[todo[-1].index]:%Y-%m-%d %H:%M}"
              f"（Lv.{P().get('author_level', 0)} 上限每日 <{word_limits()[0]}、每月 <{word_limits()[1]} 字；"
              f"排期只用 {plan_limits()[0]} / {plan_limits()[1]}，其余留给修改）")
        too_soon = [c for c in todo if not is_today(plan[c.index])
                    and plan[c.index] < dt.datetime.now() + dt.timedelta(minutes=40)]
        if too_soon:
            raise SystemExit(
                f"\n第{too_soon[0].number}章的发布时间 {plan[too_soon[0].index]:%Y-%m-%d %H:%M} "
                "已过去或太近（番茄要求提前 30 分钟以上），请把 config 里这本书 publish 的 start_date 改晚")
    if not todo and book.get("end") and (book.get("start") or 1) > book["end"]:
        print(f"  config 里 start={book['start']} 已超过 end={book['end']}，这批已发完。"
              f"要继续请把 end 改成新的章节号（或 None = 全部）。")
    if dry_run or not todo:
        return
    if rehearse:
        todo = todo[:1]

    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        try:
            for n, ch in enumerate(todo, 1):
                if STOP_FLAG.exists():
                    print(f"  收到停止请求，已停在第{ch.number}章之前。重新运行即可从这一章继续。")
                    return
                # 每章都按最新进度重排（前一章若被挪后，后面自动顺延）
                when = plan_schedule(book, chapters)[ch.index] if MODE() == "publish" else None
                extra = f" → {when_label(when)}" if when else ""
                print(f"  [{n}/{len(todo)}] {ch.full_title}（{ch.char_count}字）{extra}")
                for attempt in (1, 2):
                    try:
                        if MODE() == "publish":
                            when = publish(page, book_id, ch, when, rehearse=rehearse)
                        else:
                            save_draft(page, book_id, ch)
                        break
                    except Exception as e:
                        shot = book_dir(sub="screenshots") / f"第{ch.number}章_{attempt}.png"
                        try:
                            page.screenshot(path=str(shot))
                        except Exception:
                            shot = "（截图失败，浏览器可能已关闭）"
                        print(f"    失败（第{attempt}次）：{type(e).__name__}: {e}")
                        print(f"    截图：{shot}")
                        traceback.print_exc(file=sys.stdout)
                        # 点了「确认发布」之后的失败不重试：可能已经发出去了，重试会重复
                        unsafe = MODE() == "publish" and not isinstance(e, NotSubmitted)
                        if attempt == 2 or unsafe or "登录已失效" in str(e):
                            print("  已停止。确认后台状态、修复问题后重新运行即可从这一章继续。")
                            return
                        time.sleep(5)

                if rehearse:
                    print("  演练结束，没有发布任何章节。")
                    return
                save_progress(book_id, ch.index, when and when.strftime("%Y-%m-%d %H:%M"))
                write_back_config(book, ch, when)

                if n % config.REST_EVERY == 0:
                    print(f"  休息 {config.REST_SECONDS} 秒...")
                    time.sleep(config.REST_SECONDS)
                else:
                    time.sleep(random.uniform(*config.DELAY_RANGE))
        finally:
            ctx.close()
    print("  本次完成。")
