# 新闻工作台

与原新闻机器人并行运行的本机网页。React 提供界面，TypeScript 提供网页服务及同源接口，FastAPI 读取原机器人的 MySQL 和订阅配置。页面包含工作台总览、新闻库、RSS 来源、简报草稿、推送批次和周期报告。

新闻库可以按关键词、来源和处理状态筛选，点击查看摘要及原文。勾选新闻后创建草稿，可编辑标题和正文、预览 Markdown、保存、复制或下载。草稿保存在本项目的 runtime/drafts.sqlite3。新闻、发送批次和周期报告读取自原库。新页面的草稿与原机器人的推送队列独立，实际发送仍通过原机器人完成。

从 PowerShell 启动现有已构建版本：

~~~powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\start.ps1
~~~

按启动窗口显示的地址在 Chrome 中打开。双击 `启动工作台.cmd` 会自动启动并打开 Chrome。停止时可双击 `停止工作台.cmd`，或在启动窗口按 Ctrl+C。网页优先使用 `4380`，端口被占用时使用系统分配的端口。可在当前进程中通过 `NEWS_WORKBENCH_PORT` 设置首选端口，值为 `0` 时直接使用空闲端口。FastAPI 每次会绑定一个空闲端口。两个服务都监听本机回环地址，数据服务需要启动器生成的内部会话密钥。

## 重建

依赖和重建步骤见 [新闻机器人快速开始](../README.md#windows-快速开始)。默认从 `../config.yaml` 读取配置，也可以用 `NEWS_BOT_HOME` 指定其他机器人目录。运行日志位于 `runtime`，清理前先备份其中的草稿库。
