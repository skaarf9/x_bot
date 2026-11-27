# X 新推文监听（CDP 版）

监控指定 X 用户，发新推文时推送提醒。不需要逆向 API、不需要申请开发者账号、不需要复制 cookie。

**原理**：用远程调试端口（CDP）接上你自己登录过 X 的 Chromium 系浏览器，打开目标用户主页，
截获页面自身发出的时间线响应再解析。X 的 queryId、签名、cookie 加密方式怎么变都不用管——
页面自己能请求成功，程序只读结果。

## 要求

- Python **3.10+**
- 任意 Chromium 系浏览器：Edge / Chrome / Brave / Chromium（Firefox、Safari 不行）
- 依赖：`pip install -r requirements.txt`（只有 requests 和 websocket-client；图形界面用自带的 tkinter）
- **不需要** Playwright / Selenium / Node.js

## 快速开始

```bash
pip install -r requirements.txt
```

然后双击 `run_gui.bat`（macOS/Linux 直接 `python gui.py`），在界面里：

1. 点 **添加**，填要监控的用户名（不带 @）
2. 点 **启动监听**
3. 第一次会弹出监控专用浏览器窗口并停在 X 登录页——登录一次即可，之后一直复用这个会话
4. 顶部「登录」变绿就说明在正常工作

手动启动（无界面）：

```bash
python x_monitor.py                # 常驻
python x_monitor.py --check        # 检查浏览器/登录状态
python x_monitor.py --once         # 跑一轮
python x_monitor.py --user elonmusk --once
```

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `gui.py` | 图形界面：增删改用户、间隔、启停、登录、通知设置、清理缓存 |
| `x_monitor.py` | 监听主程序（配置热加载、去重、单实例锁） |
| `browser_util.py` | 浏览器探测、调试端口、登录态检测 |
| `cdp_client.py` | 极简 CDP 客户端（websocket） |
| `x_parser.py` | 时间线响应解析（兼容新旧两种结构） |
| `telegram_notify.py` | Telegram 推送（多 chat_id、代理、429 重试） |
| `bark_notify.py` | Bark 推送（可选） |
| `version.py` / `updater.py` | 版本号与 GitHub 新版本检查 |
| `config.example.json` | 配置模板 |
| `install_autostart.ps1` / `uninstall_autostart.ps1` | Windows 登录自启任务 |
| `tests/` | 离线自测（解析器、去重） |

首次运行时如果没有 `config.json`，程序会自动从 `config.example.json` 复制一份。

## 配置

`config.json`（不要提交到仓库，已在 `.gitignore` 里）：

```json
{
  "browser": {
    "exe": "",
    "profile_dir": "edge-monitor",
    "debug_port": 9222
  },
  "interval_seconds": 90,
  "login_timeout_seconds": 600,
  "include_replies": false,
  "skip_retweets": true,
  "notify_on_first_seen": false,
  "dedup_days": 7,
  "dedup_max_per_user": 500,
  "cache_cleanup_hours": 0,
  "users": [
    { "screen_name": "elonmusk", "include_replies": false, "skip_retweets": true }
  ],
  "telegram": { "enabled": false, "token": "", "chat_id": "", "proxy": "" },
  "bark": { "enabled": false, "server": "", "device_key": "" }
}
```

- `browser.exe` 留空自动探测；也可以填浏览器绝对路径
- `browser.profile_dir` 是专用浏览器 profile 目录（内含登录态）
- `cache_cleanup_hours` 大于 0 表示每 N 小时清一次 x.com 站点缓存

用户、间隔都可以直接在 GUI 里改，**下一轮自动生效**，不用重启。

## 推送

默认只写日志。支持 Telegram（推荐）：

1. 在 Telegram 里找 `@BotFather`，发 `/newbot`，按提示起名字，拿到形如
   `123456789:AAH...` 的 **Bot Token**
2. 打开你自己的 bot，点 **START** 发一条 `/start`（否则 bot 不能主动给你发消息）
3. GUI「通知设置」→ 粘贴 Token → 点 **获取** 自动填 Chat ID → **发送测试** → **保存**

Telegram API 在部分网络环境直连不通，需要代理时在「通知设置」里填
`http://127.0.0.1:7890` 之类的地址；留空则跟随系统代理。

### 发到群聊

1. 把 bot 拉进群（需要有群的加人权限）
2. 在群里发一条 `/start@你的bot`（群里的普通消息会被 bot 隐私模式过滤，但命令一定能收到）
3. GUI「通知设置」→「获取」，会自动填上群的 chat_id（负数，如 `-1001234567890`）
4. 想私聊和群同时收：`8085113433,-1001234567890`（逗号分隔，数量不限）

注意：如果群限制「仅管理员可发言」，要把 bot 设为管理员，否则发送会报「没有发言权限」。

## 行为与可靠性

- 每轮刷新一次各用户主页，取时间线里 **ID 最大**的推文；置顶推文不会误报。
- 新推文按 ID 顺序推送；一次超过 3 条合并成一条汇总。
- **首次记录**：新添加一个用户时，会先发一条「开始监听」通知，附上当前最新推文作为基线（`notify_on_first_seen`，默认开）。
- **去重**：发送前先把推文 ID 落盘到 `state.json`，所以重启、崩溃、回滚 state 都不会重发；
  发送失败会回滚记录并在下一轮重试。默认保留 7 天 / 每人 500 条。
- **单实例**：`monitor.lock` 文件锁保证只有一个监听进程；`gui.lock` 保证只有一个管理界面，
  重复打开会自动把已运行的窗口叫到前台，不会再出现两个界面同时点启停。
- **缓存**：`edge-monitor/` 是含登录态的浏览器 profile（约 250~300MB），**不能整个删除**，
  删了就要重新登录。GUI 里的「清理缓存」可以在不重新登录的前提下清掉缓存。

## 多用户

`users` 是列表，每个用户一个标签页、每轮依次抓取。建议
`轮询间隔 ≥ 用户数 × 30 秒`，例如 5 个用户填 150 秒以上。

## 跨平台 / 新环境

- **不用 Edge 也行**，也不要求它是默认浏览器：程序用 `--user-data-dir` 起独立 profile 的进程，
  不碰你日常浏览器的数据。
- 浏览器路径留空会自动探测 Edge / Chrome / Brave / Chromium 的常见安装位置；
  探测不到就在 `browser.exe` 里填绝对路径。
- 登录完全自动：检测到未登录会弹登录页，登录后自动继续；不需要导出 cookie / key。
- Windows 用 `install_autostart.ps1` 注册登录自启；macOS/Linux 用 systemd / cron / launchd
  跑 `python x_monitor.py` 即可。

## 版本与更新

- 界面右下角显示当前版本；启动时自动检查一次 GitHub 上的新版本（结果缓存 6 小时）。
  发现新版本会显示「发现新版本 vX.Y.Z，点击查看」，点版本号可以手动重查。
- 检查逻辑依次尝试：**Release → git tag → 直接读 `main` 上的 `version.py`**。
  所以不强制发 Release，打个 tag 就行；GitHub API 对匿名请求限流很严（共享 IP 容易 403），
  前两步失败时会自动走最后一条，不受 API 限流影响。
- 监听进程启动时也会检查，并把结果写进 `monitor.log`。
- 想关掉这个检查：`config.json` 里 `"check_updates": false`。

### 发布新版本

```bash
# 1. 改 version.py 里的 __version__
# 2. 提交
git add -A && git commit -m "v1.1.0: ..." && git push
# 3. 打 tag 并推送
git tag v1.1.0 && git push origin v1.1.0
```

想要更新说明/附件，再到 GitHub 上基于这个 tag 建一个 Release（可选，不建也能被检测到）。

## 常见问题

- **界面显示未登录**：在监控浏览器窗口登录 x.com；或点「打开登录页」。
- **浏览器里明明已登录，界面却提示未登录**：这是监听进程与浏览器之间的调试连接失效被误判成「未登录」，新版本会在每轮开始前 ping 一次并自动重连。旧版本重启一次监听即可恢复。
- **429 / 抓不到响应**：调大轮询间隔、减少同时监控的用户数。
- **浏览器被关掉**：监听器发现端口没了会自动重新拉起。
- **账号不存在**：约 22 秒内识别并在日志里报错，不会误报。
- **千万别复制 `edge-monitor`**：v20 app-bound 加密绑定 profile 路径，换路径后浏览器会判定
  无法解密并删除 cookie，等于掉登录。

## 日志与故障排查

所有版本都会写 `monitor.log`。登录类问题先看这几行：

| 日志 | 含义 |
| --- | --- |
| `启动监听，用户: ...`（不带版本号） | 1.0.0 之前的旧代码 |
| `启动监听 v1.0.1，用户: ...` | 1.0.1 及以上 |
| `CDP 连接已失效，重新连接浏览器（登录态不受影响）` | 调试连接断了，新版会自动恢复 |
| `浏览器连接正常，但当前 x.com cookie: auth_token, ct0, ...` | 浏览器确实已登录；若随后仍报未登录，才是判断逻辑问题 |
| `浏览器连接正常，但当前 x.com cookie: (完全没有)` | 确实掉登录了，需要重新登录 |
| `打开登录页失败: Target.createTarget timed out` | 旧版「连接假死被当成未登录」的典型症状 |
| `本轮异常: 'Browser' object has no attribute 'ping'` | 覆盖更新不完整：只换了 `x_monitor.py`，没换 `cdp_client.py` |
| `等待登录超时` | 浏览器确实停在登录页，10 分钟内没人登录 |

一键诊断（生成脱敏报告，可发给维护者）：

```bash
python x_monitor.py --diag
```

会在目录下生成 `diag.txt`，包含版本、浏览器/端口、登录态、x.com cookie 名、计划任务实际
执行的命令、最近 80 行日志；**Telegram token / chat_id 已打码**。

### 覆盖更新的注意事项

`x_monitor.py`、`cdp_client.py`、`browser_util.py` 是一套，必须一起替换；
建议整个目录覆盖（保留自己的 `config.json` / `state.json` / `edge-monitor/`），并删掉
`__pycache__`。另外确认计划任务指向的是你更新后的目录——`--diag` 里的 `task_command`
就是它实际在跑的命令。

## 隐私提醒

- `config.json` 里有 Telegram token，`edge-monitor/` 里有 X 登录态，`state.json` 里有监控记录。
- 上传 GitHub / 打包分发前，确认这些都不在包里（`.gitignore` 已覆盖）。

## License

MIT License - 见 [LICENSE](LICENSE)。
