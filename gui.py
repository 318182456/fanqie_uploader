# -*- coding: utf-8 -*-
"""
番茄上传工具的图形界面：python gui.py

左边选书、看章节列表和排期，右下角看实时日志。
按钮背后都是调用 main.py 的对应命令（子进程），所以和命令行的效果完全一样，
日志也照样写进 logs/。
"""
import csv
import datetime as dt
import importlib
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import config
import splitter
import uploader

BASE_DIR = Path(__file__).parent


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("番茄小说批量上传")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.proc = None            # 正在跑的 main.py 子进程
        self.out_q = queue.Queue()  # 子进程输出 → 界面
        self.chapters = []
        self.book = None

        self._build()
        self.load_books()
        self.after(100, self._drain_output)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 界面 ----------
    def _build(self):
        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="作品：").pack(side="left")
        self.book_var = tk.StringVar()
        self.book_box = ttk.Combobox(top, textvariable=self.book_var, state="readonly", width=48)
        self.book_box.pack(side="left")
        self.book_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(top, text="打开书目录", command=lambda: self.open_dir(uploader.book_dir(self.book))).pack(side="left")
        ttk.Button(top, text="打开截图", command=lambda: self.open_dir(uploader.book_dir(self.book, "screenshots"))).pack(side="left", padx=4)
        ttk.Button(top, text="打开日志", command=lambda: self.open_dir(BASE_DIR / "logs")).pack(side="left")
        ttk.Button(top, text="编辑 config.py", command=lambda: os.startfile(BASE_DIR / "config.py")).pack(side="left", padx=4)

        self.info_var = tk.StringVar()
        ttk.Label(self, textvariable=self.info_var, padding=(8, 6), foreground="#333",
                  justify="left").pack(fill="x")

        # 操作按钮
        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(fill="x")
        self.run_buttons = []
        for text, args, confirm in [
            ("① 扫码登录", ["login"], None),
            ("② 发布演练", ["rehearse"], None),
            ("③ 开始上传", ["upload"], "upload"),
            ("全书核对", ["check"], None),
        ]:
            b = ttk.Button(bar, text=text, command=lambda a=args, c=confirm: self.run(a, c))
            b.pack(side="left", padx=(0, 4))
            self.run_buttons.append(b)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        for text, fn in [("更新选中章正文", self.update_selected),
                         ("单章定时发布", lambda: self.one_chapter("publish-one")),
                         ("改发布时间", lambda: self.one_chapter("reschedule"))]:
            b = ttk.Button(bar, text=text, command=fn)
            b.pack(side="left", padx=(0, 4))
            self.run_buttons.append(b)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.stop_btn = ttk.Button(bar, text="■ 本章完成后停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 4))
        self.kill_btn = ttk.Button(bar, text="强制终止", command=self.kill, state="disabled")
        self.kill_btn.pack(side="left")
        ttk.Button(bar, text="导出 CSV", command=self.export_csv).pack(side="right")

        # 进度
        pf = ttk.Frame(self, padding=(8, 6))
        pf.pack(fill="x")
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(pf, textvariable=self.status_var, width=40).pack(side="left", padx=8)

        # 章节表 + 日志，上下可拖动
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tf = ttk.Frame(pane)
        fbar = ttk.Frame(tf)
        fbar.pack(fill="x", pady=(0, 4))
        ttk.Label(fbar, text="筛选：").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        for f in ("全部", "本次待发", "已处理", "有问题"):
            ttk.Radiobutton(fbar, text=f, value=f, variable=self.filter_var,
                            command=self.fill_table).pack(side="left")
        ttk.Label(fbar, text="  搜索：").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(fbar, textvariable=self.search_var, width=20)
        e.pack(side="left")
        e.bind("<KeyRelease>", lambda ev: self.fill_table())

        cols = ("index", "number", "title", "chars", "volume", "status", "when", "note")
        heads = ("顺序", "章节号", "标题", "字数", "分卷", "状态", "发布时间", "提示")
        widths = (50, 60, 260, 60, 150, 70, 130, 240)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("title", "volume", "note") else "center")
        self.tree.tag_configure("done", foreground="#888")
        self.tree.tag_configure("todo", foreground="#0a5")
        self.tree.tag_configure("warn", background="#fff3cd")
        self.tree.tag_configure("bad", background="#f8d7da")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self.show_chapter)
        pane.add(tf, weight=3)

        lf = ttk.Frame(pane)
        self.log = tk.Text(lf, height=12, wrap="word", font=("Consolas", 10),
                           bg="#1e1e1e", fg="#ddd", insertbackground="#ddd")
        self.log.tag_configure("err", foreground="#ff6b6b")
        self.log.tag_configure("ok", foreground="#6bd66b")
        self.log.tag_configure("warn", foreground="#f0c060")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")
        pane.add(lf, weight=2)

    # ---------- 数据 ----------
    def load_books(self):
        importlib.reload(config)
        names = [f"{Path(b['txt']).stem}  [{b['book_id']}]" for b in config.BOOKS]
        self.book_box["values"] = names
        if names:
            cur = self.book["book_id"] if self.book else None
            i = next((n for n, b in enumerate(config.BOOKS) if b["book_id"] == cur), 0)
            self.book_box.current(i)
        self.refresh(reload=False)

    def refresh(self, reload=True):
        if reload:
            importlib.reload(config)  # 上传后 config.py 里的 start/start_date 会被改写
        if not config.BOOKS:
            self.info_var.set("config.py 的 BOOKS 里还没有书")
            return
        self.book = config.BOOKS[max(self.book_box.current(), 0)]
        uploader.use_book(self.book)
        path = Path(self.book["txt"])
        if not path.exists():
            self.chapters = []
            self.info_var.set(f"txt 不存在：{path}")
            self.fill_table()
            return

        self.chapters = splitter.split_chapters(path)
        todo, done = uploader.pending(self.book, self.chapters)
        self.todo_idx = {c.index for c in todo}
        self.done_idx = done
        sched = uploader._load(self.book["book_id"]).get("schedule", {})
        self.when = {int(k): v for k, v in sched.items()}
        mode = uploader.MODE()
        if mode == "publish":
            try:
                for idx, t in uploader.plan_schedule(self.book, self.chapters).items():
                    self.when.setdefault(idx, f"{t:%Y-%m-%d %H:%M}（计划）")
            except Exception as e:
                self.log_line(f"排期计算失败：{e}\n", "err")

        # 每章的问题提示
        self.notes = {}
        for prev, cur in zip(self.chapters, self.chapters[1:]):
            if cur.number != prev.number + 1:
                self.notes[cur.index] = f"序号不连续（上一章 {prev.number}）"
        for c in self.chapters:
            if not c.paragraphs:
                self.notes[c.index] = "空章节"
            elif c.char_count >= uploader.CHAPTER_MAX:
                self.notes[c.index] = f"超过单章上限 {uploader.CHAPTER_MAX}"
            elif c.char_count < config.MIN_CHARS_WARN:
                self.notes.setdefault(c.index, "字数偏少")

        p = uploader.P()
        info = (f"txt：{path}    模式：{'定时发布' if mode == 'publish' else '存草稿'}    "
                f"共 {len(self.chapters)} 章 / 已处理 {len(done)} / 本次待发 {len(todo)}    "
                f"start={self.book.get('start')}  end={self.book.get('end')}")
        if mode == "publish":
            day, month = uploader.word_limits()
            pday, pmonth = uploader.plan_limits()
            info += (f"\n发布时间 {p['time']}   Lv.{p.get('author_level', 0)} 上限 每日<{day} 每月<{month}，"
                     f"排期只用 {pday}/{pmonth}")
            if todo:
                info += f"   本次排期：{self.when[todo[0].index][:10]} ~ {self.when[todo[-1].index][:10]}"
        self.info_var.set(info)
        self.fill_table()

    def fill_table(self):
        self.tree.delete(*self.tree.get_children())
        f, kw = self.filter_var.get(), self.search_var.get().strip()
        for c in self.chapters:
            done, todo, note = c.index in self.done_idx, c.index in self.todo_idx, self.notes.get(c.index, "")
            if f == "本次待发" and not todo or f == "已处理" and not done or f == "有问题" and not note:
                continue
            if kw and kw not in c.full_title and kw != str(c.number):
                continue
            status = "已处理" if done else "本次待发" if todo else "范围外"
            tags = ["done" if done else "todo" if todo else ""]
            if note:
                tags.append("bad" if "上限" in note or "空" in note else "warn")
            self.tree.insert("", "end", iid=str(c.index), tags=tags, values=(
                c.index, c.number, c.title, c.char_count, c.volume, status,
                self.when.get(c.index, ""), note))

    def selected_chapters(self):
        sel = {int(i) for i in self.tree.selection()}
        return [c for c in self.chapters if c.index in sel]

    def show_chapter(self, _=None):
        chs = self.selected_chapters()
        if not chs:
            return
        c = chs[0]
        w = tk.Toplevel(self)
        w.title(f"第{c.number}章 {c.title}（{c.char_count}字）")
        w.geometry("720x640")
        t = tk.Text(w, wrap="word", font=("Microsoft YaHei", 11), padx=12, pady=8)
        t.insert("1.0", f"【序号】{c.number}\n【标题】{c.title}\n【卷】{c.volume or '（无）'}\n"
                        f"【段落】{len(c.paragraphs)}    【字数】{c.char_count}\n{'=' * 30}\n\n"
                 + "\n\n".join(c.paragraphs))
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)

    def export_csv(self):
        if not self.chapters:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialdir=uploader.book_dir(self.book),
            initialfile=f"章节清单_{dt.datetime.now():%Y%m%d_%H%M}.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["顺序", "章节号", "标题", "字数", "分卷", "状态", "发布时间", "提示"])
            for iid in self.tree.get_children():
                w.writerow(self.tree.item(iid, "values"))
        self.log_line(f"已导出：{path}\n", "ok")

    # ---------- 运行 main.py ----------
    def run(self, args, confirm=None):
        if self.proc:
            messagebox.showinfo("提示", "已有任务在运行")
            return
        if not self.book:
            return
        if confirm == "upload":
            n = len(self.todo_idx)
            mode = "定时发布" if uploader.MODE() == "publish" else "存入草稿箱"
            if not n:
                messagebox.showinfo("提示", "没有待处理的章节（检查 config.py 里的 start / end）")
                return
            if not messagebox.askyesno("确认", f"将把《{Path(self.book['txt']).stem}》的 {n} 章{mode}。\n\n"
                                             "建议先跑一次「发布演练」。确定开始吗？"):
                return

        uploader.STOP_FLAG.unlink(missing_ok=True)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "FANQIE_BOOK_ID": self.book["book_id"]}
        cmd = [sys.executable, "-u", str(BASE_DIR / "main.py"), *args]
        self.log_line(f"\n$ python main.py {' '.join(args)}\n", "warn")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=flags)
        self.total = len(self.todo_idx) if args[0] == "upload" else 1 if args[0] == "rehearse" else 0
        self.progress.configure(maximum=max(self.total, 1), value=0)
        self.status_var.set(f"运行中：{args[0]}")
        self._set_running(True)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for raw in proc.stdout:
            self.out_q.put(raw.decode("utf-8", errors="replace"))
        proc.wait()
        self.out_q.put(None)

    def _drain_output(self):
        try:
            while True:
                line = self.out_q.get_nowait()
                if line is None:
                    self._finished()
                    continue
                m = re.match(r"\s*\[(\d+)/(\d+)\]\s*(.*)", line)
                if m:
                    self.progress.configure(maximum=int(m[2]), value=int(m[1]) - 1)
                    self.status_var.set(f"{m[1]}/{m[2]}  {m[3][:30]}")
                tag = ("err" if re.search(r"失败|出错|Error|✖|Traceback", line)
                       else "ok" if "✓" in line or "完成" in line
                       else "warn" if "⚠" in line else None)
                self.log_line(line, tag)
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    def _finished(self):
        code = self.proc.returncode if self.proc else None
        self.proc = None
        uploader.STOP_FLAG.unlink(missing_ok=True)
        self._set_running(False)
        if code == 0 and self.total:
            self.progress.configure(value=self.progress["maximum"])
        self.status_var.set("完成" if code == 0 else f"已结束（退出码 {code}）")
        self.log_line(f"—— 结束，退出码 {code} ——\n", "ok" if code == 0 else "err")
        self.refresh()

    def _set_running(self, running):
        for b in self.run_buttons:
            b.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.kill_btn.configure(state="normal" if running else "disabled")

    def stop(self):
        uploader.STOP_FLAG.touch()
        self.status_var.set("停止中：当前章处理完就停")
        self.log_line("已请求停止，当前章节处理完后停下…\n", "warn")

    def kill(self):
        if self.proc and messagebox.askyesno(
                "强制终止", "强制终止可能停在「已点确认发布、但还没记进度」的中间状态，\n"
                          "之后要用「全书核对」确认是否重复/漏发。\n\n确定强制终止吗？"):
            self.proc.kill()

    # ---------- 针对选中章节的操作 ----------
    def update_selected(self):
        chs = self.selected_chapters()
        if not chs:
            messagebox.showinfo("提示", "先在列表里选中要更新的章节（可多选）")
            return
        nums = [str(c.number) for c in chs]
        if messagebox.askyesno("更新正文", f"用 txt 里的新版正文替换番茄上的这些章节：\n第 {', '.join(nums)} 章\n\n确定吗？"):
            self.run(["update", *nums])

    def one_chapter(self, cmd):
        chs = self.selected_chapters()
        if len(chs) != 1:
            messagebox.showinfo("提示", "请在列表里选中一章")
            return
        c = chs[0]
        label = "单章定时发布" if cmd == "publish-one" else "修改发布时间"
        default = (self.when.get(c.index) or f"{dt.date.today() + dt.timedelta(days=1)} {uploader.P()['time']}")[:16]
        s = simpledialog.askstring(label, f"第{c.number}章 {c.title}\n发布时间（YYYY-MM-DD HH:MM）：",
                                   initialvalue=default, parent=self)
        if not s:
            return
        try:
            when = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            messagebox.showerror("格式不对", "请按 2027-01-31 09:00 这样的格式填写")
            return
        self.run([cmd, str(c.number), f"{when:%Y-%m-%d}", f"{when:%H:%M}"])

    # ---------- 杂项 ----------
    def log_line(self, s, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", s, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_dir(self, path):
        if self.book or path.name == "logs":
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)

    def on_close(self):
        if self.proc and not messagebox.askyesno("退出", "还有任务在运行，退出会强制终止它。确定退出吗？"):
            return
        if self.proc:
            self.proc.kill()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
