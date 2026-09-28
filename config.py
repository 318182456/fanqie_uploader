# -*- coding: utf-8 -*-
"""
配置文件：按需修改这里即可。
"""

# ========== 1. 要上传的书 ==========
# txt:        本地 txt 路径
# book_id:    番茄作家后台的作品 ID
#             （进入作品的「章节管理」页，浏览器地址栏里那串长数字就是）
# start / end: 只上传这个范围内的章节（按 txt 里切出来的顺序，从 1 开始），None 表示不限
#             发布成功后工具会自动把 start 改成下一章
# mode:       可选，这本书单独的上传模式，不写就用下面的全局 MODE
# publish:    可选，这本书单独的发布设置，写了的项覆盖下面的全局 PUBLISH，没写的沿用全局
#             start_date 只能在这里写（每本书不同）；不写则从明天开始排
#
# 每本书的进度、截图、预览、错别字报告都在 books\<书名>\ 目录下
BOOKS = [
    {
        "txt": r"C:\Users\admin\Downloads\诸天：我只是个看客.txt",
        "book_id": "7690032840003570712",
        "start": 501,   # 已定时发布到第500章（2027-09-23 09:00），下次从这里开始
        "end": None,
        "mode": "publish",
        "publish": {
            "start_date": "2027-09-23",   # 最近使用的发布日，下一章从这天起排（改晚可整体后推）
            "time": "09:00",
        },
    },
    # {
    #     "txt": r"D:\小说\另一本.txt",
    #     "book_id": "7yyyyyyyyyyyyyyyyyy",
    #     "start": None,
    #     "end": None,
    #     "publish": {"start_date": "2026-11-01", "time": "12:00"},
    # },
]

# ========== 上传模式 ==========
#   "publish" -> 直接发布到章节管理，并设置定时发布
#   "draft"   -> 只存草稿箱
MODE = "publish"

# 定时发布的全局默认设置（每本书可在 BOOKS 里用 publish 覆盖）
PUBLISH = {
    "non_chapter_prompt": "submit",  # 提示「章末可能有非章节内容」时："submit" 点提交并记入 审核风险提示.txt / "stop" 停下来
    "typo_prompt": "submit",     # 提示「还有错别字未修改」时："submit" 点提交并记入报告 / "stop" 停下来
    "time": "09:00",             # 每天的发布时间
    "reserve_ratio": 0.4,        # 每天/每月额度留多少给修改已发布章节（0.4 = 排期用 6 成，约每天 6000 字）
    "author_level": 1,           # 番茄作者等级：决定发布字数上限（Lv.0/1 每日<1万 每月<25万；Lv.2/3 <2万 <50万；Lv.4+ <5万 <100万）
                                 # 升级后改这里即可；想手动指定可加 "words_per_day" / "words_per_month"
    "max_per_day": None,         # 每个发布日最多几章（None = 只按字数）
    "days_per_run": None,           # 每次运行只排几天的量（None = 一次排完全部）
    "use_ai": False,             # 「是否使用AI」选 否
    "create_volume": True,       # txt 里的卷在番茄上不存在时自动新建
    "limit_shift_days": 7,       # 提示「超出每日/每月上限」时往后挪（下一天/下月1号）重试，最多挪几次
    "content_check": "basic",    # 内容检测方式："basic" 仅基础检测（不限次数）/ "full" 全面检测（每章限 2 次）
}

# ========== 2. 章节切分 ==========
# 匹配章节标题行的正则（整行匹配）。默认支持：第一章 / 第1章 / 第一百零二章 / 第12章：标题
CHAPTER_REGEX = r"^\s*第\s*([0-9０-９零〇一二两三四五六七八九十百千万]+)\s*章[\s:：、.．]*(.*?)\s*$"

# 卷名行（如「第一卷 序章」），不当成正文上传。要求「卷」后面是空白或行尾，
# 所以「第三卷，七侠镇的……」这种正文句子不会被误判
VOLUME_REGEX = r"^\s*第\s*[0-9０-９零〇一二两三四五六七八九十百千万]+\s*卷(\s+.*)?$"

# 去掉段首的全角/半角空格（番茄编辑器会自动缩进，保留的话会双重缩进）
STRIP_INDENT = True

# 正文少于这个字数时给出警告（只是提醒，不影响存草稿）
MIN_CHARS_WARN = 1500

# ========== 3. 上传节奏 ==========
# 每章之间的间隔秒数（随机取区间内的值）。太快可能触发风控，建议不要调得太小
DELAY_RANGE = (4, 8)

# 每上传多少章额外休息一次，以及休息的秒数
REST_EVERY = 50
REST_SECONDS = 60

# 浏览器用户数据目录（保存登录状态，别删）
PROFILE_DIR = "browser_profile"

# ========== 4. 页面元素（番茄改版后如果失效，在这里改） ==========
# 可以运行 `python main.py inspect` 打开 Playwright Inspector 重新取选择器
URLS = {
    "home": "https://fanqienovel.com/main/writer/?enter_from=author_zone",
    # 新建章节页面
    "new_chapter": "https://fanqienovel.com/main/writer/{book_id}/publish/?enter_from=newchapter",
}

# 每个元素给多个候选选择器，依次尝试，第一个可见的生效
SELECTORS = {
    # 「第 __ 章」的章节序号输入框
    "chapter_no": [
        ".serial-editor-title-left input",
    ],
    # 章节标题输入框
    "title": [
        ".serial-editor-title-right input",
        "input[placeholder='请输入标题']",
    ],
    # 正文编辑器（页面右侧还有大纲用的 ProseMirror，必须限定在正文容器里）
    "content": [
        ".syl-editor-container .ProseMirror",
    ],
    # 「存草稿」按钮
    "save_draft": [
        "button.auto-editor-save-btn",
        "button:has-text('存草稿')",
    ],
    # 保存成功提示
    # 注意页面顶部一直挂着「已保存」，不能拿它判断；点完存草稿后会变成「已保存到云端」
    "save_ok": [
        "text=已保存到云端",
    ],
    # 各种引导弹窗的关闭按钮，出现就点掉
    "dismiss": [
        "button:has-text('我知道了')",
        "button:has-text('知道了')",
        "button:has-text('跳过')",
        ".arco-modal-close-icon",
        ".byte-modal-close",
    ],
}

# 填写方式：
#   "split" -> 章节序号框填数字，标题框只填「标题」部分（番茄新版编辑器是这样的）
#   "full"  -> 没有序号框，标题框填完整的「第X章 标题」
TITLE_MODE = "split"
