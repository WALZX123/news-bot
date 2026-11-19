# 新闻机器人

在本机采集 RSS 新闻，做选稿、AI 摘要和周期报告，在工作台预览简报后发送到钉钉。支持定时运行和独立重跑失败的报告周期。

## Windows 快速开始

准备 Python 3.13、Node.js 24、MySQL 8.0 和 Google Chrome。把 Python 与 Node 加入 PATH，再在项目根目录按顺序执行。

1. 安装依赖并创建私有配置。仅在首次安装时复制示例。

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r workbench/backend/requirements.lock
   Copy-Item config.example.yaml config.yaml
   ```

2. 在 `config.yaml` 中填写本机 MySQL 的连接参数和订阅源。新安装使用独立的空数据库，初始化账号须有建库和建表权限。已有旧库先读[数据库迁移说明](docs/机器人使用说明.md#安装与配置)。

   ```powershell
   .\.venv\Scripts\python.exe -B main.py --init-db
   .\.venv\Scripts\python.exe -B main.py --check-db
   ```

3. 构建工作台。

   ```powershell
   Push-Location workbench
   npm.cmd ci
   npm.cmd run build
   Pop-Location
   ```

4. 双击 [启动工作台.cmd](启动工作台.cmd)，在 Chrome 打开工作台。在「系统设置」填写模型凭据和钉钉入口，在「来源管理」试抓订阅。在自动化页面可以先启用仅采集，确认来源和推送窗口后再开启完整模式。

双击 [停止工作台.cmd](停止工作台.cmd) 可关闭网页服务。默认端口为 `4380`，占用时会自动选空闲端口。

| 目录 | 用途 |
| --- | --- |
| `main.py`、`src/`、`config.yaml` | RSS 采集、AI 分析、定时任务、报表和钉钉推送 |
| `workbench/` | React 页面、TypeScript 网关和 FastAPI 数据服务 |
| `workbench/runtime/` | 启动状态、运行日志和本地 SQLite 草稿 |
| `.venv/` | 两个模块共用的 Python 环境 |

## 自动化和配置

- 在「新闻库」点击稿件可查看单篇 AI 摘要和订阅源摘录，也可直接加入选稿。新摘要会展示模型引用且经过文本匹配的摘录片段，旧摘要可点击补生成证据。
- 新闻详情里的「同事件候选」比较前后 48 小时的稿件，显示原文地址、标题或产品型号和事件动作的匹配依据，供编辑核对。
- 在「发布台」调整选稿顺序、写导语，生成钉钉预览后确认发送。送达成功或未知会保留在批次记录中，手动失败的批次由操作者核对后重试。
- 在「自动化运行」可立即采集、切换仅采集和自动推送或请求停止。「系统设置」可以调整时间窗口、候选门槛和每轮单篇摘要上限。
- 模型配置位于 `ai`，使用 DeepSeek Chat Completions 接口。Windows 加密凭据引用沿用现有保护区。

- 双击 [启动自动化.cmd](启动自动化.cmd) 开启完整后台调度，启动后立即执行首轮。已有任务运行时，模式切换会在本轮结束后生效。双击 [停止自动化.cmd](停止自动化.cmd) 请求在当前轮结束后退出。启动异常可查看 `runtime/automation-start.log`。
- 工作台的「自动化运行」页面显示后台模式、下一轮、近期任务状态和模型 Token 用量。自动化进程与工作台独立。Windows 重启后重新启动调度。
- 「近期运行」可展开环节明细，核对失败来源、报告周期、各环节耗时与模型调用。单篇摘要的模型用量计入本轮汇总。历史运行保留当时实际记录的字段。
- 「周期报告」列出失败周期。点击「单独重跑」使用当前模型重试指定周期，报告保存后可打开查看。
- 工作台的「系统设置」可修改模型、Base URL、API Key、调度参数和连接凭据。高级 YAML 支持完整设置。配置保存前会校验并备份现有文件。新保存的模型密钥用当前用户的 Windows DPAPI 加密，带 SHA-256 摘要，存放在 `%LOCALAPPDATA%\NewsBot\secrets`。YAML 只保存凭据引用。使用同一 Windows 用户启动后台。
- 「测试已保存配置」会向当前模型发送一条固定的短 JSON 测试请求，可查看结果、耗时和用量。该测试与新闻流水线分开。
- 「推送批次」可以用现有待处理新闻生成钉钉格式预览。预览只有读取与模型整理。正式投递前会保存最终 Markdown，批次详情可查看当时的消息文本。
- 已确认失败的批次按 5 分钟和 20 分钟退避，累计三次停止自动尝试。送达未知继续按人工回执流程处理。首页区分新鲜待处理、已过期、已组批和已确认投递批次。
- 新自动简报的趋势与争议条目须引用输入稿件，推送记录以钉钉消息中实际保留的原文链接为准。模型未引用或因长度限制省略的稿件回到待处理。

```powershell
# 只调度 RSS 采集
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\automation.ps1 -Mode Collect
# 查询独立调度状态
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\automation.ps1 -Action Status
```

## 日常操作

在本目录打开 PowerShell。下面的命令行入口会加载项目本地运行环境。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\workbench\start.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bot.ps1 --check-db
```

`bot.ps1` 可带 `--fetch-only` 仅采集入库，或带 `--once` 执行一次完整流水线。不带参数会启动原机器人定时任务，并按配置的推送窗口向钉钉发送。工作台可独立启动。

配置入口是 `config.yaml`。修改工作台读取的 MySQL 或 RSS 配置后，重启工作台以载入新配置。机器人会在定时执行时重新读取配置。

## 数据与并发

- FastAPI 为每个服务进程创建一个异步 MySQL 只读池，按需建立连接，上限 6 条。借用和查询有超时，取消时会丢弃中断的连接，停止时关闭连接池。
- 首页和来源统计分别缓存 3 秒、5 秒，同一时间的重复请求共用一次计算。新闻列表在输入停止后发起查询，新闻来源筛选只读取配置中的来源名称。Markdown 预览在首次打开时加载。
- 草稿保存在 SQLite，采用 WAL 和版本号校验。更新时如果已被另一页面保存，接口返回冲突，重新打开草稿可读取最新版。
- 网页入口和数据服务均监听回环地址。默认启动只运行工作台。数据服务或网关意外退出时，启动器会清理本次启动的进程和状态。

## 本地验证

以下测试使用隔离的数据库和 HTTP 测试服务。

```powershell
.\.venv\Scripts\python.exe -B tools/check_project.py
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\.venv\Scripts\python.exe -B -m unittest discover -s workbench/tests
```

仓库中的配置模板为 `config.example.yaml`。本机的 `config.yaml`、密钥、运行数据和备份保存在私有目录中。

更多命令和恢复步骤见 [机器人使用说明](docs/机器人使用说明.md)及[运维说明](docs/OPERATIONS.md)。
