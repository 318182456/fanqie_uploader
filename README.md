# 番茄小说草稿箱批量上传

把本地 txt 小说按「第X章」切分，逐章存入番茄作家后台对应作品的草稿箱。

## 图形界面

双击 `0_图形界面.bat`（或 `python gui.py`）：

- 顶部选作品，列表显示每章的字数、分卷、状态（已处理 / 本次待发 / 范围外）、发布时间（已排的和计划的），字数偏少、超上限、序号跳号会标颜色
- 双击某章可以看切分后的完整正文；可按状态筛选、按标题搜索，「导出 CSV」导出当前列表
- 按钮对应 main.py 的命令：扫码登录、发布演练、开始上传、全书核对；选中章节后可「更新正文」「单章定时发布」「改发布时间」
- 下方是实时日志和进度条；「本章完成后停止」会等当前章做完再停，不会留下半发布的章节，下次从停下的那章继续
- 书的配置（txt、book_id、start/end、发布设置）仍在 `config.py` 里改，改完点「刷新」

## 使用步骤

1. 安装浏览器内核（只需一次）：`python -m playwright install chromium`
2. 编辑 `config.py` 的 `BOOKS`，填 txt 路径和作品 ID
3. `python main.py login`：扫码登录，登录状态保存在 `browser_profile/`
4. `python main.py preview`：检查章节切分结果、跳号、短章
5. 先把某本书的 `end` 设成 `1`，运行 `python main.py upload`，去草稿箱确认格式没问题
6. 把 `end` 改回 `None`，再运行 `python main.py upload` 上传全部

## 断点续传

已上传的章节记录在 `progress/<book_id>.json`。中断后重新运行会跳过已上传的章节。
想重传某本书，删掉对应的 json 文件即可。

## 选择器失效时

番茄后台改版后，如果提示「找不到标题输入框」之类的错误：

1. 查看 `screenshots/` 里的失败截图
2. 运行 `python main.py inspect <book_id>`，在 Playwright Inspector 里用「Pick locator」点选对应元素
3. 把取到的选择器加到 `config.py` 里 `SELECTORS` 对应列表的最前面
