# 直播小助手（DSH 插件）

自动盯某位 UP 主的直播：**开播就录 → 下播收尾 → 进待发布队列 → 你确认后按 B 站投稿选项发到自己账号**。
录播全程脱离 DSH 生命周期（守护进程独立运行，DSH 关了照样在录）。

## 组成

| 部分 | 位置 | 作用 |
|---|---|---|
| Host 插件 | `<工作区>\bili-live-plugin`（本目录） | 注册 7 个工具 + 侧边栏整页面板 + `/bili-live/api/*` 路由 + 拉起守护进程 |
| Python 后端 | `<你的后端目录>`（`bili_rec` 包） | 轮询开播、抓 FLV、录弹幕、转 mp4、出封面、投稿上传与提交 |
| 工作目录 | `<后端目录>/_bili_live_work` | `settings.json` `ups.json` `queue.json` `daemon.json` `sessions/<id>/` |
| 凭据 | `<后端目录>/bili_cookie.txt` | 面板里可直接粘贴保存；也可放同目录 `quark` 那套的做法 |

后端**只用 Python 标准库**（本机命令行程序建不了 HTTPS，Python 自带 ssl 正常）。
ffmpeg 只处理本地文件，不联网。

## 安装

1. 面板/工具安装：把 `<工作区>\bili-live-plugin` 作为 bundle 安装（`plugin_manager install_bundle`）。
2. 首次安装后**重启 DSH Desktop**（Cordis 用固定 URL import 插件模块，ESM 缓存不会热加载）。
3. 打开左侧导航「直播小助手」，先「保存凭据」（整行 cookie，必须含 `SESSDATA` 与 `bili_jct`），再「添加 UP 主」（填 UID 即可，插件自己解析直播间）。

## 面板

- **顶部**：守护进程状态/启停、磁盘剩余、cookie 状态与「检测有效性」。
- **UP 主**：开播状态、当前分P/已录时长/体积/码率/弹幕数/断流重连次数；开始/停止录制、暂停监控、移除。
- **待发布队列**：按「一场直播」分组，多分P 会合成一个投稿（P1/P2/…）。
  每行可改标题/简介/标签/分区/可见范围；上传中有百分比进度；失败有原因；成功后显示 BV 号。
  工具栏：全选当前(N) / **清空勾选** / 发布选中(N) / 重试失败(N) / **删除选中(N)** / **清空队列(N)**。
  - 队列**默认只显示「待发布」**；已投稿的是历史记录，筛选切到「已投稿」才显示。
  - **「删除」= 从列表移除 + 删本地成品文件**（省空间），需点两次确认；每行也有单独的「删除」。
    已投稿的条目也能删 —— 只删本地记录与文件，**B 站上的稿件不受影响**。
  - 「清空队列(N)」一次性删掉所有「待发布/失败/已丢弃」条目及其文件。
  - 删除记录写在 `_bili_live_work/deleted.jsonl`（含文件名与释放的字节数），方便追溯。
- **已录直播**：历史会话，可单独「收尾进队列」或「删除本地」。
  每行会标出**真实状态**：`本地 N 个文件` / `本地无文件`、`队列 N 条`。
  只剩录制记录、既没文件也没队列条目的会话会显示说明，按钮变成「删除记录」（只删这条 meta）。
  - 分段短于 5 秒的不投稿，且**文件会被直接删掉**（以前会留在盘上变成隐形占用）。
  - 「接口说在播、取流说没在播」的空转会话会**自动删目录**并给该 UP 主 15 分钟冷却。
- **设置**：分P 时长（默认 120 分钟）、清晰度、编码偏好、轮询间隔、磁盘下限、录弹幕/自动封面/保留 FLV；
  发布默认值（标题/简介模板、分区、标签、原创/转载、禁止转载、充电、关弹幕、关评论、动态、可见范围、定时发布）、
  「录完自动投稿」（默认关）、「投稿成功删本地」。

日志在面板底部，文件是 `_bili_live_work/daemon.log`。

## 七个工具

| 工具 | 用途 |
|---|---|
| `bili_live_status` | 总览：守护进程、UP 主与录制进度、队列统计、磁盘 |
| `bili_live_ups` | `list` / `add {mid}` / `remove {mid}` / `toggle {mid,enabled}` |
| `bili_live_record` | `start` / `stop`（手动，守护进程须在跑） |
| `bili_live_daemon` | `start`（后台拉起，脱离 DSH）/ `stop` / `status` |
| `bili_live_queue` | `list` / `publish {ids}` / `discard` / `retry` / `update` / `finalize` |
| `bili_live_settings` | 读写设置（不传 patch 即读） |
| `bili_live_auth` | 检查 cookie 是否有效 |

## 发布选项（对齐 B 站投稿页）

| 选项 | 字段 | 说明 |
|---|---|---|
| 分区 | `tid` / `tidName` | 默认 `21 日常`；面板里可选常用分区 |
| 标签 | `tags` | 支持占位符，逗号拼接；为空时兜底「录播」 |
| 原创/转载 | `copyright` + `source` | 1 原创 / 2 转载（转载需填来源） |
| 简介 | `descTemplate` | 占位符 `{date} {uname} {title} {roomUrl} {duration} {part}` |
| 标题 | `titleTemplate` | 同上；多分P 自动追加 ` P2` 等 |
| 封面 | 自动抽帧 | 从成片第 20 秒抽一帧并裁成 16:10 |
| 可见范围 | `visibility` | `private` 仅自己可见（提交时带 `is_only_self=1`）/ `public` / `scheduled` 定时（`dtime`，须晚于现在 4 小时以上） |
| 禁止转载 | `no_reprint` | |
| 允许充电 | `charging_pay` | |
| 关闭弹幕/评论 | `up_close_danmu` / `up_close_reply` | |
| 同时发动态 | `dynamic` | 留空则不带动 |

## 实测踩到的坑（别再踩）

1. **命令行建不了 HTTPS**：`ffmpeg -i https://...` 直接 `schannel: SEC_E_NO_CREDENTIALS`。
   所以抓流用 Python 的 urllib，ffmpeg 只处理本地文件。
2. **HEVC-in-FLV 转不了**：B 站 HEVC 用的是 Enhanced-FLV（codecID 12），ffmpeg 7.0 解出来没有宽高，转 mp4 必失败。
   故默认 `preferCodec=avc`（请求 `codec=0`），只有没有 AVC 流时才退回 HEVC。
3. **分P 必须补编解码头**：从第 2 段起，流里不会再有 SPS/PPS/AAC header，切段时要手动补在最前面，否则 mp4 没尺寸。
4. **时间戳基准要取首条音视频 tag**：直播 FLV 第一条通常是 `onMetaData` 脚本 tag（时间戳 0），拿它当基准会把 1 小时直播写成 8 小时。
5. **弹幕鉴权必须带真实 uid**：`uid=0`（游客）会被服务器立刻断开；`getDanmuInfo` 要 **WBI 签名**，不签名返回 `-352`。
6. **投稿上传要三步**：`preupload`（`profile=ugcupos/bup`）→ `POST {url}?uploads&output=json` 拿 `upload_id`
   → 分片 `PUT` → `POST` 带 `{"parts":[…]}` 合并。少了 init 会 `NoSuchUpload`。
7. **`videos[].filename` 是「不含目录与扩展名的文件名」**。
8. **投稿有频率限制**：`21070 您投稿的频率过快`、`preupload` 的 `601 / 403 / 406` 都是风控，等几分钟再来。
   插件里对 preupload 做了退避重试。
9. **Windows 上别用 `os.kill(pid, 0)` 探活**：CPython 在 Windows 会走 `TerminateProcess`，等于「查询即杀进程」，
   本插件改用 `OpenProcess + GetExitCodeProcess`。

## 配置（插件的 Config）

| 字段 | 默认 | 说明 |
|---|---|---|
| `scriptDir` | `<你的后端目录>` | 后端目录（含 `bili_rec`） |
| `workRoot` | 空 | 留空 = `<后端目录>/_bili_live_work` |
| `pythonPath` | 空 | 留空自动找 |
| `ffmpegPath` / `ffprobePath` | `D:\Shotcut\*` | 只用于本地转封装/抽帧 |
| `cookieFile` | 空 | 留空 = `<后端目录>/bili_cookie.txt` |
| `autoStartDaemon` | true | 插件加载后自动拉起守护进程 |
| `backendTimeoutMs` | 180000 | 同步后端调用超时 |
| `maxLogTail` | 12000 | 面板日志尾部字节 |

## 常见问题

- **面板显示守护进程未运行**：点「启动守护进程」，或看 `daemon.out.log`；多半是 `pythonPath` 或 `scriptDir` 不对。
- **录到的是 720p/超清而不是原画**：该主播的 AVC 流最高就是那样；把「编码偏好」设成 hevc 能拿更高画质，
  但 ffmpeg 7.0 转不了 HEVC-in-FLV，成品会留在 `raw/*.flv`。
- **投稿失败 `code=21001`**：通常是某个发布参数不被接受，先在面板里把「关闭弹幕/评论」关掉、可见范围设 `public` 再试。
- **磁盘**：2 小时 1080p 约 3～6GB；`minFreeGb` 之下会拒绝开新录制。
