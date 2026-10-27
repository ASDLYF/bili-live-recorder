# B站录播姬（DSH 插件）

自动盯某位 UP 主的直播：**开播就录 → 下播收尾 → 进待发布队列 → 确认后按 B 站投稿选项发到自己账号**。
录制跑在独立守护进程里，DSH 退出也继续录。

## 特性

- **自动录制**：轮询开播状态，开播就录、下播就停；每场直播按设定时长切成多个分P（默认 2 小时）
- **画质**：默认自动取该主播当前能给的**最高画质**；也支持按 UP 主单独指定档位
- **弹幕**：手写 WebSocket 客户端（WSS 鉴权 + WBI 签名），导出 XML / ASS
- **封面**：自动抽帧并裁成 B 站要求的 16:10
- **投稿**：upos 分片上传 + 稿件提交，支持标题/简介模板、标签、分区、原创/转载、禁止转载、充电、关弹幕评论、动态、定时发布、**仅自己可见**
- **合集**：可给每个 UP 主设置目标合集（B 站写入接口不可用时会在日志里如实提示）
- **安全**：默认「等审核通过才删本地」，被驳回就保留源文件；可选连会话目录一起删
- **断点恢复**：守护进程启动时自动捞回上次被强杀/断电留下的未收尾录制

## 结构

| 目录 | 内容 |
|---|---|
| `plugin/` | DSH 插件（Host：工具 + `/bili-live/api/*` 路由；Client：侧边栏整页面板） |
| `backend/bili_rec/` | Python 后端（只用标准库）：抓流、弹幕、转封装、投稿、守护进程 |

后端**不依赖第三方包**；抓流与投稿都在 Python 里做，ffmpeg 只处理本地文件。

## 安装

1. 把 `backend/` 放到任意目录，`plugin/cordis.patch.yml` 里的 `scriptDir` 指过去；
2. 用 DSH 的 `plugin_manager install_bundle` 安装 `plugin/` 目录；
3. 重启 DSH Desktop（Cordis 用固定 URL import 模块，ESM 缓存不会热加载）；
4. 打开左侧导航「B站录播姬」→ 保存 B 站 cookie（需 `SESSDATA` + `bili_jct`）→ 添加 UP 主（填 UID 即可）。

## 实测踩过的坑

1. **命令行建不了 HTTPS**（schannel `SEC_E_NO_CREDENTIALS`）→ 抓流/投稿全用 Python，ffmpeg 只处理本地文件
2. **HEVC-in-FLV**（B 站 4K）用的是 Enhanced-FLV，**ffmpeg 7.0 解不了**，需要 7.1+；否则转不出 mp4
3. **分P 必须补编解码头**（SPS/PPS/AAC header），否则第 2 段起 mp4 没尺寸
4. **时间戳基准要取首条音视频 tag**，拿 `onMetaData`（时间戳 0）当基准会把 1 小时直播写成 8 小时
5. **弹幕鉴权必须带真实 uid**（游客 uid=0 会被立刻断开），`getDanmuInfo` 需要 **WBI 签名**，否则 `-352`
6. **投稿上传要三步**：`preupload` → `POST ?uploads` 拿 `upload_id` → 分片 PUT → 带 `{"parts":[…]}` 合并
7. **`videos[].filename` 不含目录与扩展名**；提交里带 `desc_v2` 会 `21001 参数错误`
8. **B 站合集写入接口不可用**（`season/section/add|edit`、`season/edit` 全返回 `-400`），只能投稿后手动加入
9. **Windows 上不能用 `os.kill(pid, 0)` 探活**（会走 `TerminateProcess`，等于查询即杀进程）


## 平台支持

| 平台 | 状态 | 说明 |
|---|---|---|
| Windows | ✅ 实测通过 | 本项目的开发与验证环境（Windows 11 / Python 3.14 / ffmpeg 2026-master） |
| macOS | ✅ 代码兼容 | Python 后端只用标准库；ffmpeg 走 `/opt/homebrew/bin`、`/usr/local/bin` 自动发现 |
| Linux | ✅ 代码兼容 | 同上（`shutil.which` + `/usr/bin`） |

需要一个 **支持 HEVC-in-FLV 的 ffmpeg（7.1+）**，4K 录制才转得出 mp4：

```bash
# macOS
brew install ffmpeg
# 若 Homebrew 版本偏旧，可从 https://evermeet.cx/ffmpeg/ 或 BtbN/FFmpeg-Builds 取新版

# Linux (Debian/Ubuntu)
sudo apt install ffmpeg      # 需要 >= 7.1，否则装静态包
```

## 依赖

| 依赖 | 是否随仓库提供 | 说明 |
|---|---|---|
| Python 3.8+ | 否（系统自带） | **只用标准库**，无需 pip install |
| Node.js / React | 否 | 由 DSH 宿主提供，插件不额外依赖 npm 包 |
| ffmpeg / ffprobe | 否（体积 300MB+，不入库） | 仅用于本地转封装 / 抽帧；需 7.1+ |
| B 站 cookie | 否（自己填） | 面板里粘贴，写入后端目录的 `bili_cookie.txt`，不会进仓库 |

自检脚本：`python -m bili_rec.cli --workdir <工作目录> status` 会打印 Python/ffmpeg/守护进程状态。

## 许可

MIT
