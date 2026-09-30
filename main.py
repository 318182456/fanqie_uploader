# -*- coding: utf-8 -*-
"""
番茄小说草稿箱批量上传工具

用法：
    python main.py login              首次使用：扫码登录并保存登录状态
    python main.py preview [N]        只切分章节并检查，输出接下来 N 章（默认 3）的填写内容，不上传
    python main.py upload             上传/定时发布 config.BOOKS 里的所有书（支持断点续传）
    python main.py rehearse           发布演练：只走第一章，填好发布设置后点「取消」，不会发布
    python main.py check              全书核对：番茄后台 vs txt（漏发、重复、标题、字数、空档、草稿残留）
    python main.py update <章节号> [章节号...] [--force]  用 txt 新版正文更新已发布/已定时的章节
                                      （番茄上已是新版的章自动跳过，--force 强制重传）
    python main.py changed [旧txt]    列出正文有改动、需要更新的章节（第一次用要带上精修前的旧 txt）
    python main.py update-changed [旧txt]  更新所有有改动的章节；中断后重跑会接着更新
    python main.py publish-one <章节号> <YYYY-MM-DD> [HH:MM]  单独定时发布一章（补漏章）
    python main.py reschedule <章节号> <YYYY-MM-DD> [HH:MM]  修改已定时发布章节的发布时间
    python main.py inspect <book_id>  打开新建章节页 + Inspector，用来修正选择器
"""
import datetime
import os
import sys
import traceback
from pathlib import Path

import config
import splitter
import uploader

# 日文/繁体系统的控制台默认不是 GBK/UTF-8，print 中文会报错
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 图形界面通过环境变量指定只处理某一本书
if os.environ.get("FANQIE_BOOK_ID"):
    config.BOOKS = [b for b in config.BOOKS if b["book_id"] == os.environ["FANQIE_BOOK_ID"]]


class Tee:
    """屏幕输出同时写入日志文件。"""
    def __init__(self, stream, f):
        self.stream, self.f = stream, f

    def write(self, s):
        self.stream.write(s)
        self.f.write(s)
        self.f.flush()

    def flush(self):
        self.stream.flush()


def start_log(cmd):
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{cmd or 'help'}.log"
    f = open(path, "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, f)
    sys.stderr = Tee(sys.stderr, f)
    return path


def preview(n=3):
    for book in config.BOOKS:
        path = Path(book["txt"])
        if not path.exists():
            print(f"[缺失] {path}")
            continue
        chapters = splitter.split_chapters(path)
        print(f"\n《{path.stem}》 -> book_id={book['book_id']}，切出 {len(chapters)} 章")
        for c in chapters[:3] + (["..."] if len(chapters) > 6 else []) + chapters[-3:]:
            if c == "...":
                print("    ...")
            else:
                print(f"    #{c.index:<5} 第{c.number}章 | {c.title} | {c.char_count}字")
        for w in splitter.check(chapters):
            print(f"  ⚠ {w}")
        for c in chapters:
            if c.char_count >= uploader.CHAPTER_MAX:
                print(f"  ✖ 超过番茄单章上限 {uploader.CHAPTER_MAX} 字（{c.char_count}字）：{c.full_title}")
        uploader.upload_book(book, chapters, dry_run=True)
        show_pending(book, chapters, n)


def show_pending(book, chapters, n):
    """输出接下来 n 章实际会填进番茄的内容，并把第一章完整导出到 preview/。"""
    todo = uploader.pending(book, chapters)[0][:n]
    if not todo:
        return

    print(f"\n  ===== 接下来 {len(todo)} 章的填写内容 =====")
    for c in todo:
        no, title = (c.number, c.title) if config.TITLE_MODE == "split" else ("", c.full_title)
        print(f"\n  ▶ 序号框：{no}    标题框：{title}")
        print(f"    所属卷：{c.volume or '（无）'}    段落数：{len(c.paragraphs)}    字数：{c.char_count}")
        if uploader.MODE() == "publish":
            print(f"    定时发布：{uploader.schedule_of(book, c, chapters):%Y-%m-%d %H:%M}    是否使用AI：{'是' if uploader.P()['use_ai'] else '否'}")
        print("    正文开头：")
        for p in c.paragraphs[:3]:
            print(f"      {p[:60]}{'…' if len(p) > 60 else ''}")
        print("      ……")
        print("    正文结尾：")
        for p in c.paragraphs[-2:]:
            print(f"      {p[:60]}{'…' if len(p) > 60 else ''}")

    first = todo[0]
    out = uploader.book_dir(book, "preview") / f"第{first.number}章.txt"
    out.write_text(
        f"【序号】{first.number}\n【标题】{first.title}\n【卷】{first.volume}\n"
        f"【字数】{first.char_count}\n{'=' * 30}\n" + "\n".join(first.paragraphs),
        encoding="utf-8",
    )
    print(f"\n  第{first.number}章完整正文（按段落、去缩进后的样子）已导出：{out}")


def upload(rehearse=False):
    for book in config.BOOKS:
        path = Path(book["txt"])
        if not path.exists():
            print(f"[跳过，文件不存在] {path}")
            continue
        chapters = splitter.split_chapters(path)
        if not chapters:
            print(f"[跳过，没切出章节，检查 CHAPTER_REGEX] {path}")
            continue
        uploader.upload_book(book, chapters, rehearse=rehearse)


def reschedule(number, date, hhmm=None):
    """改第一本书里「第 number 章」的定时发布时间。"""
    import datetime as dt
    book = config.BOOKS[0]
    chapters = splitter.split_chapters(book["txt"])
    ch = next(c for c in chapters if c.number == number)
    uploader.use_book(book)
    when = dt.datetime.strptime(f"{date} {hhmm or uploader.P()['time']}", "%Y-%m-%d %H:%M")
    uploader.reschedule(book, ch, when)


def publish_one(number, date, hhmm=None):
    """把第一本书里「第 number 章」单独定时发布到指定日期（补发漏章用）。"""
    import datetime as dt
    book = config.BOOKS[0]
    ch = next(c for c in splitter.split_chapters(book["txt"]) if c.number == number)
    uploader.use_book(book)
    when = dt.datetime.strptime(f"{date} {hhmm or uploader.P()['time']}", "%Y-%m-%d %H:%M")
    uploader.publish_one(book, ch, when)


def find_changed(old_txt=None):
    """找出第一本书里已传到番茄上、正文有改动的章节。给了旧 txt 就先用它补记番茄上的版本。"""
    book = config.BOOKS[0]
    chapters = splitter.split_chapters(book["txt"])
    if old_txt:
        n = uploader.seed_baseline(book, chapters, old_txt)
        print(f"  已用旧 txt 补记 {n} 章的番茄版本：{old_txt}")
    changed, unknown = uploader.changed_chapters(book, chapters)
    total = len(uploader.on_fanqie(book, chapters))
    print(f"\n《{Path(book['txt']).stem}》番茄上 {total} 章，其中正文有改动 {len(changed)} 章"
          + (f"：{uploader.fmt_numbers(changed)}" if changed else ""))
    if changed:
        words = sum(c.char_count for c in changed)
        print(f"  改动章合计 {words} 字（修改已发布章节也占每日/每月字数额度，可能要分几天跑完）")
    if unknown:
        print(f"  ⚠ {len(unknown)} 章没有记录番茄上的版本，无法判断是否改过：{uploader.fmt_numbers(unknown)}")
        print("    带上精修前的旧 txt 运行一次：python main.py changed <旧txt路径>")
    return changed


def run(cmd):
    if cmd == "login":
        uploader.login()
    elif cmd == "preview":
        preview(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "upload":
        upload()
    elif cmd == "rehearse":
        upload(rehearse=True)
    elif cmd == "check":
        for book in config.BOOKS:
            print(f"\n《{Path(book['txt']).stem}》核对中...")
            uploader.check_book(book, splitter.split_chapters(book["txt"]))
    elif cmd == "update" and len(sys.argv) > 2:
        book = config.BOOKS[0]
        nums = [int(x) for x in sys.argv[2:] if x != "--force"]
        chs = [c for c in splitter.split_chapters(book["txt"]) if c.number in nums]
        uploader.update_content(book, chs, force="--force" in sys.argv)
    elif cmd in ("changed", "update-changed"):
        changed = find_changed(sys.argv[2] if len(sys.argv) > 2 else None)
        if cmd == "update-changed" and changed:
            uploader.update_content(config.BOOKS[0], changed)
    elif cmd == "publish-one" and len(sys.argv) > 3:
        publish_one(int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "reschedule" and len(sys.argv) > 3:
        reschedule(int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "inspect" and len(sys.argv) > 2:
        uploader.inspect(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    log_path = start_log(cmd)
    try:
        run(cmd)
    except SystemExit:
        raise
    except BaseException:
        print("\n========== 出错了 ==========")
        traceback.print_exc()
        print(f"\n完整日志：{log_path}")
        sys.exit(1)
    print(f"\n日志：{log_path}")
