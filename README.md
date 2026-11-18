# 页畔 · QMD Margin

给 Quarto 作者的本地批注工作台。选中一句原文，把想改的地方写在页边。拿到候选稿后，可以并排读原稿与改稿，继续圈选，直到愿意把这一稿合并进正文。

![页畔中《窗边的一天》的原稿、候选稿与批注对照](docs/workbench.png)

## 先读一篇示例

在 macOS 上准备好 Python 3.9 或更高版本，并安装 [Quarto](https://quarto.org/docs/get-started/)。

1. [下载仓库 ZIP](https://github.com/SSSimpleC/qmd-margin/archive/refs/heads/main.zip) 并解压，或运行 `git clone https://github.com/SSSimpleC/qmd-margin.git`。
2. 双击 **打开页畔.command**。首次启动会创建 Python 虚拟环境并安装 lxml，然后在浏览器打开《窗边的一天》。
3. 选中一句话，点「批注」。按 Enter 保存，Shift+Enter 换行。输入框挡住原文时，按住顶部留白即可拖开。
4. 点击「提交批注」。在写作助手的聊天里告诉它项目路径，让它按照该项目的 `.qmd-margin/latest.md` 处理最新批注。

如果喜欢从 Dock 打开，在仓库目录运行 `./macos/build-app.sh`，用本机的 Xcode Command Line Tools 生成「页畔.app」，再将它拖入 Dock。应用与启动脚本放在同一目录。

## 打开自己的书稿

在页畔仓库目录运行以下命令，将路径换成包含 `_quarto.yml` 的 Quarto **book** 项目目录。

```sh
./打开页畔.command --root "/path/to/my-book"
```

也可以将整个页畔目录放在书稿根目录下，日常直接双击启动。页畔按 `book.chapters` 和 `book.appendices` 读取 QMD。用其他编辑器修改原文、替换插图后，刷新页面即可看到。

在 Linux 或偏好使用终端时，可以从仓库目录执行以下命令。首次运行先完成前两步。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python review.py --root "/path/to/my-book" serve --open
```

服务只监听本机回环地址。终端前台运行时按 Ctrl+C 结束；双击启动的服务会在关闭网页后继续运行，下次打开会复用。服务 PID 和地址记录在项目的 `.qmd-margin/server.json` 中。

## 阅读与修订

- **边读边批。** 可对选文、标题、图、公式、表格或单元格写意见，也可逐字替换。输入会暂存，可收起后继续。确认提交会生成固定批次。
- **跟着正文读批注。** 右栏默认最新在前，切到「文章顺序」即可开启「跟随正文」。原句经过正文上方三分之一处时，对应批注滑到侧栏顶部；同屏意见较多时可单独滚动侧栏。
- **多轮对照。** 每章第一次保存意见时固定本轮原稿。写作助手基于最新候选继续修改；左右对照可查看累计改动，也能从第二稿起只看与上一稿的差别。连续全文可切换工作稿和正式稿。
- **按章合并。** 作者指定章节与稿次后，通过独立命令三方合并并备份。这个步骤需要 Git、Make 和书稿自身的 `make check`、`make render`。后者须在 `build/` 生成或更新一份 PDF。示例已配好这两个目标。构建成功后，核对留存的 PDF，再确认归档。

批注、候选快照和历史图片默认保存在书稿的 **`.qmd-margin/`**。备份时连同书稿保存这个目录。网页使用当前浏览器暂存输入；写作助手通过本机文件接手。发布候选、处理冲突和最终合并的命令见 [写作助手交接约定](AGENTS.md)。

## 开发验证

在已安装依赖的仓库目录执行以下命令。

```sh
.venv/bin/python regression_render.py
.venv/bin/python regression_store.py
.venv/bin/python regression_merge.py
```

合并测试使用临时项目和测试 PDF 检查备份、冲突与恢复逻辑。许可证为 [MIT](LICENSE)。
