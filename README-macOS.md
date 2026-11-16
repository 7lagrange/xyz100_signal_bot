# Mac 运行说明

沿用同一套 Hyperliquid XYZ100、盘中 30 分钟 SMA14/99、SQLite 和 Slack 通知逻辑。每分钟增量读取 1m 行情，本地合成并固定已收盘 30m K 线，当前 K 线用最新价计算信号；运行时不请求原生 30m 数据。这里只补 Mac 启动入口和原生 launchd 登录自启，不另外维护一份策略。

## 在当前这台 Mac 上运行

终端进入项目目录：

```bash
cd "/Users/qihongjian/Library/CloudStorage/OneDrive-Personal/trading/xyz100_signal_bot"
./run_macos.command --check
```

启动器选择 Python 3.11+，会跳过 macOS 自带的旧 Python；支持 PATH 中的 Python、常见 Conda 和 Homebrew 安装位置。当前这台机器已验证可使用现有 Conda Python，无需再安装 Python。

如果提示缺少 requests，按启动器打印的准确解释器路径安装依赖，不要换用另一个 Python 安装。也可以使用已确认的环境直接运行：

```bash
python -m pip install -r requirements.txt
python bot.py --check
```

`--check` 仅读取公共行情，不发 Slack、不写正式数据库。需要新装 Python 时使用 [Python 官方安装程序](https://www.python.org/downloads/macos/)。

## 配置和手动启动

第一次正常运行：

```bash
./run_macos.command
```

首次会创建空配置并退出，提示填写：

```text
~/Library/Application Support/XYZ100SignalBot/config.json
```

打开并填入原来有效的 Slack bot token 和目标频道：

```bash
open -e "$HOME/Library/Application Support/XYZ100SignalBot/config.json"
```

```json
{
  "slack_bot_token": "xoxb-替换为原来有效的BotToken",
  "slack_channel": "#fut-trading-30mins"
}
```

新建的配置权限为 600，本机状态目录为 700。正式配置、数据库和日志都放在本机 Application Support，不进入 OneDrive 代码目录。

可主动发送一条配置测试，再开始常驻：

```bash
./run_macos.command --test-slack
./run_macos.command
```

第一条命令会发送一条明确标记为“配置测试／不是交易信号”的真实消息；它不推进策略基线。正常运行首次只建立历史基线，以后新的交叉才发消息；关闭终端前先按 Ctrl+C 停止手动进程。

Finder 中也可以双击 `run_macos.command` 手动启动。新解压的副本若未保留可执行权限，在项目目录执行一次：

```bash
chmod +x run_macos.command install_macos.command
```

## 登录后自动启动

先停掉手动运行的实例，再执行或双击：

```bash
./install_macos.command
```

安装器会：

1. 验证 Slack 配置已经填写。
2. 把运行代码复制到 `~/Library/Application Support/XYZ100SignalBot/app/`，不让后台任务依赖 OneDrive 或下载目录。
3. 保存当前所用 Python 的绝对路径，并生成 `~/Library/LaunchAgents/local.xyz100.signalbot.plist`。
4. 使用 `plutil` 检查 plist，加载当前用户的 LaunchAgent，并立即启动。重装时只替换本程序的代理和运行代码，保留配置、数据库和通知记录。

代理登录后运行，异常退出时由 launchd 重启，启动间隔至少 60 秒。正常行情请求错误由程序按约 1、2、4、8、10 分钟逐步退避，并遵循更长的 Retry-After；恢复后回到每分钟采集。它不需要 sudo，不更改系统睡眠、代理或登录设置。[Apple 的用户级 launchd 说明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)

**安装脚本会真正启用后台通知；本次代码验证没有代你执行这个安装步骤。**

## 本地行情和信号图表

在项目目录，另开一个终端运行：

```bash
./run_macos.command --dashboard
```

浏览器会打开 **http://localhost:8765**，可查看 30m K 线/收盘线、SMA14/99、实际 BUY/SELL 和 Slack 发送状态。页面每 5 秒刷新，读取本机 SQLite；它本身不采集行情、不发送通知，也不读取 Slack token。

持续更新需要让普通模式 `./run_macos.command` 或已安装的采集 LaunchAgent 保持运行；已有后台采集时只开网页即可。首次启动不补发历史信号，因此历史均线上看得到交叉也不一定有通知标记。图表的历史均线使用最终收盘价，当前一根使用已保存的最新分钟价。

网页服务窗口按 Ctrl+C 停止；关浏览器不会停止采集。网页仅监听本机，其他电脑不能通过它们自己的 localhost 访问这台 Mac 的数据。完整图表与数据边界见 [通用说明](README.md#在-localhost-查看图表)。

## 状态、日志和停用

```bash
launchctl print "gui/$(id -u)/local.xyz100.signalbot"
tail -n 20 "$HOME/Library/Application Support/XYZ100SignalBot/bot.log"
```

正常每分钟有一条 `Healthy` 日志，列出正在形成的 K 线起点、最新价和均线。常规日志按原程序轮转；`launchd.error.log` 留存导入错误等启动诊断，不重复记录每分钟的常规日志。

卸载当前已加载代理、停止当前实例（不删除配置和数据库）：

```bash
launchctl bootout "gui/$(id -u)/local.xyz100.signalbot"
```

这会停止本次登录会话的代理；plist 仍在，下次登录会再次加载。需要跨登录停用时先执行：

```bash
launchctl disable "gui/$(id -u)/local.xyz100.signalbot"
launchctl bootout "gui/$(id -u)/local.xyz100.signalbot"
```

恢复时先 `launchctl enable "gui/$(id -u)/local.xyz100.signalbot"`，再重新运行 `./install_macos.command`。修改项目中的代码后也需要重装，后台使用的是本机 app 副本。

## 需要保留的边界

- 登录后运行；注销、关机、睡眠期间不运行。没有替你启用防睡眠或自动登录。
- 要保留安装时使用的 Python 环境；删除/移动环境后，重新选择可用 Python 并重装代理。解释器和其环境也应安装在本机，避免放在 OneDrive 或下载目录。
- launchd 不继承临时终端环境变量。代理只配置在终端时，应先让后台运行环境也能访问 Hyperliquid 和 Slack；安装器不复制代理密码等环境变量。验证结果中的精简环境行情检查用于确认当前本机系统代理可正常工作。
- Mac 和 Windows 各自维护本机数据库。只选一台电脑正式推送，不能靠 OneDrive 合并去重；首次在另一台机器新建数据库时会重新建立基线。
- 正常重启去重、数据过期/断档保护、历史补发和极端 Slack 回包丢失的边界，仍按 [通用说明](README.md) 执行。

## 已验证内容

```bash
python -m unittest -v test_bot.py test_macos.py test_dashboard.py test_site_sync.py
```

50 项测试通过（33 项策略/请求控制、5 项 Mac 配置/安装、6 项网页、6 项 Sites 同步测试）。`macos_source_check.json` 记录盘中模式通过 Mac 启动脚本的行情检查；`live_validation.json` 记录增量分钟采集和本地 30m 合成验证。`rate_limit_validation.json` 记录限流的离线验证；`macos_validation.json` 保留此前临时本机运行副本、精简代理环境和原生 plist 校验的历史结果，以其中时间为准。

早期平台验证没有安装真实 LaunchAgent 或发送真实 Slack。之后已按用户要求从 notebook 填写本机私有配置；本地看板开发时首次保存了真实公共分钟数据供预览，没有建立正式通知基线、生成历史通知或启动 Slack 常驻采集。是否已注册自启、是否产生健康日志，以及真实频道是否收到配置测试，应按上面步骤确认。
