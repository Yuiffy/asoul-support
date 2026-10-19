> 此 fork 新增 [华为云 FunctionGraph 版本](cloud_function/README.md)：自动管理持牌直播间的每日点赞、弹幕和观看任务，支持限额灯牌、当天续跑、Cookie 自动刷新和企微汇总。真实账号配置与开源代码分离。

<p align="center">
  <img src="assets/asoul-logo.png" width="120" alt="A-SOUL Logo" />
</p>

<h1 align="center">B站主播 Support · 改</h1>

<p align="center">
  <strong>B站主播粉丝牌与每日亲密度任务工具</strong> — 云函数自动续跑 + 直播点赞与观看 + 未播弹幕 + 限额灯牌
</p>

<p align="center">
  <a href="https://clawhub.ai/skills/asoul-support">🦞 ClawHub</a> ·
  <a href="https://github.com/XiaoYiWeio/asoul-support">📦 GitHub</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-4.1.1-blue" alt="version" />
  <img src="https://img.shields.io/badge/python-3.9+-green" alt="python" />
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="license" />
</p>

---

## 云函数版：相比原版的改动

本 fork 基于原版 v4.1.1，保留原有 CLI、X25Kn E/X 心跳协议及视频、动态应援功能，
新增独立的 `cloud_function/` 调度层。以下能力属于云函数版；后文保留原版的安装与使用说明。
部署步骤、配置示例和接口说明见 [云函数部署指南](cloud_function/README.md)。

### 新增功能与优势

| 方面 | 原版 | 云函数版的改动与收益 |
| --- | --- | --- |
| 部署运行 | 本地脚本、Agent 或 GitHub Actions | 支持华为云 FunctionGraph；建议每 2 小时续跑，每轮最多 110 分钟，不需要本地电脑常开 |
| 直播间管理 | 默认固定成员，可手动修改 | 自动分页获取账号全部持牌直播间；优先直播间第一，随后处理开播房间，最后处理未播房间 |
| 每日任务 | 心跳挂机、粉丝牌点亮等 | 读取观看、弹幕、点赞和灯牌的实际任务进度，区分仅点亮、单项满和三项全满 |
| 当天续跑 | 未提供这套云端持久化队列 | OBS 保存每日名单和逐房进度；已完成当日跳过，未完成继续，跨天重置，冷启动后仍能恢复 |
| 观看调度 | 原有 X25Kn 心跳基础 | 最多 8 间并行观看，每间每轮最多 16 分钟；重叠等待时间，同账号 HTTP 请求仍统一串行调度 |
| 点赞分配 | 未提供低等级牌子优先策略 | 每轮从全部持牌房间计算覆盖至少100间的等级门槛，同级全部纳入；优先房间第一、开播低等级房间随后。OBS保存每日5000次基础尝试＋最多1000次额度实验，逐批记录实际任务计数 |
| 限额送礼 | 无此每日灯牌策略 | 显式开启后，仅主账号可向代码限定的目标补送每日灯牌，最多 1 个、1 电池；副账号及其他房间只做免费任务 |
| 登录与配置 | 本地 Cookie 或 GitHub Secrets | 支持配套 Cookie 与 refresh token 自动刷新，凭据加密保存到 OBS；密钥、账号和 webhook 通过环境变量配置 |
| 通知统计 | Discord 通知 | 企微发送整轮、分账号汇总，包含覆盖数、单项完成数、剩余队列、实际接受的互动及异常原因；长消息分条发送，保留两账号统计 |

这里的“优先直播间”和“目标”指当前代码中限定的房间，尚未提供任意目标房间的环境变量配置。
账号启停、是否处理其他持牌房间、是否允许付费均可通过配置控制。私密配置不进入源码和部署 ZIP，
公开仓库仅保留示例。云端运行需要 FunctionGraph 和 OBS 资源，费用以实际用量及平台计费为准。

### 关键修复

- **识别未点亮任务格式**：兼容“发弹幕 10 次 / 点赞 30 次、仅点亮”的任务响应，避免遍历全量房间却没有执行动作；确认点亮后再处理正式日任务。
- **在换房前完成可执行互动**：普通房间不再只做一次互动后等待全量扫尾，而是在当前房间内最多执行 10 轮正式互动；点亮阶段单独计数，避免刚点亮就耗尽正式任务预算。
- **按确认进度统计完成**：请求成功、心跳秒数和粉丝牌点亮不再等同于日任务全满；今日点赞、弹幕、观看完成数分别按房间去重，观看结果合并也不会降低已确认的互动进度。
- **避免重复执行与扣费**：执行时段预约防止重复轮次；送礼前持久化当天预约并再次检查任务，失败或结果不确定时不盲目补送。
- **完善频控和并发处理**：同账号跨房间统一冷却，等待后才生成签名与时间戳；冷却释放网络锁，避免阻塞观看心跳。弹幕 10031 可有限等待恢复，-352/-412 暂停整个账号本轮。
- **修复云端持久化与打包问题**：无 OBS ListBucket 权限时安全初始化缺失日志，不把权限错误当成空数据覆盖；完整 ZIP 打包 Linux 加密依赖，避免网页编辑破坏原生依赖。

### 执行边界与验证

自动弹幕仅在确认 `live_status=0` 时发送；每个房间最多5条或30秒重查一次，换房必查，发送前再核对结果未过期；直播中、轮播及状态未知均跳过。检查窗口内突然开播仍可能收到当前组的少量弹幕。
点赞和观看仅在真实直播中执行。弹幕冷却为 6～8 秒，点赞批次间隔为 15～20 秒，观看心跳遵循服务端间隔。
点赞按任务要求分批（通常每批 30 次），允许在计数延迟时提交最初确认的剩余批次，达到计划次数后本轮不再重发。
名单外仍处理弹幕和观看；策略完成要求名单内三项满、名单外观看和弹幕满，付费目标另需灯牌满。
5000 是程序的基础发送尝试预算；未满任务可继续尝试到6000，并分别统计实验提交与计数增长。
每账号在基础预算内为优先房间预留330次，确认其当天点赞已满后才释放；预留期间其他房间不能通过实验预算挤占。
含失败和结果不确定的请求，不包括策略记录前、手动操作或其他工具的点赞，也不是平台官方保证的有效额度。
不会为了用完预算向已满任务重复提交。单次300次提交尚未确认入账，生产环境暂不启用。
弹幕 10031 本轮最多等待 60 秒、120 秒各恢复一次，再次触发则暂停弹幕；未登录、账号风控和结果不确定的请求均停止相应操作。

这些策略不能保证消除风控。自动刷新需要同一次登录配套的 Cookie 和 refresh token，
有效登录也不代表点赞接口一定接受请求。并行心跳成功不等于所有房间均计入观看任务，
主播开播时间、储蓄上限和平台结算都会影响完成率，因此不保证全部持牌房间每天三项全满。

云函数版维护 Windows 与最终 Linux 部署包回归测试，部署时另做健康和凭据读取检查。
实际完成率以接口确认的进度及每日汇总为准。后续修改可在 fork 根目录运行：

```bash
python -m unittest discover -s cloud_function -p 'test_*.py'
```

---

## 🚀 一句话安装

复制下面这句话，发给你的 AI 助手（支持任意 agent 框架），它会帮你搞定一切：

```
帮我安装这个项目并设置 A-SOUL 自动挂机：https://github.com/XiaoYiWeio/asoul-support
```

---

## ✨ 它能做什么？

| 功能 | 使用条件 | 说明 |
|------|----------|------|
| 💓 **心跳挂机涨亲密度** | 需要开播 | X25Kn E/X 协议；结束后记录亲密度前后值和实际增量 |
| 🏅 **粉丝牌自动点亮** | 需要开播 | 发 10 条弹幕点亮牌子，保持 3 天可见 |
| 🪙 **自动投币** | 无 | 给成员视频投币（1 币 = 10 亲密度），需用户明确开启 |
| 👍 **视频自动点赞** | 无 | 自动给成员新视频点赞（默认每周执行，避免风控） |
| 💬 **动态自动点赞** | 无 | 自动给成员新动态点赞（默认关闭，需手动开启） |

> B站亲密度规则：观看直播每分钟少量结算（具体数值由 B 站后端控制）；投币 1 币 = 10 亲密度。粉丝牌点亮（10 条弹幕）只维持牌子可见，不直接计入亲密度。

---

## 📝 更新日志

| 版本 | 日期 | 更新内容 |
|------|------|----------|
| **v4.1.1** | 2026-07-27 | 修复 X25Kn 心跳时间漂移和失败链恢复；新增亲密度增量记录与登录预检；GitHub Actions 升级到 Node 24 运行时 |
| **v4.1** | 2026-05-28 | 心跳协议升级为 **X25Kn E/X**（HMAC 链式签名），替代已失效的 `mobileHeartBeat`；自动获取 LIVE_BUVID；GitHub Action 频率降低以减少风控 |
| v4 | 2026-04-01 | （已废弃）`mobileHeartBeat` 协议——B 站后端已停止为该协议结算亲密度 |
| v3 | 2026-03-23 | 新增开播检测 + 心跳挂机 + Discord 通知 |
| v2 | 2026-03-20 | 粉丝牌点亮（10 条弹幕，3 天有效期） |
| v1 | 2026-03 | 视频点赞 + 动态点赞 + GitHub Actions |

---

## 🔬 技术方案

| 项目 | 说明 |
|------|------|
| **心跳协议** | B 站 Web `X25Kn`（`/x25Kn/E` 进入 → `/x25Kn/X` 心跳，每次响应递推 `secret_key/secret_rule/timestamp`） |
| **签名算法** | HMAC 链式签名（rule 索引 0~5 → HMAC-MD5 / SHA1 / SHA256 / SHA224 / SHA512 / SHA384），`secret_key` 作 HMAC key |
| **设备指纹** | 优先使用 `.cookies.json` 中的浏览器 `LIVE_BUVID`；缺失时以 SPI `b_3` 作为兼容回退 |
| **实现语言** | 纯 Python 3.9+，标准库 `hashlib` + `hmac` |
| **外部依赖** | 原版 CLI 无外部依赖；云函数 Cookie 刷新使用固定版本的加密依赖，见 [部署指南](cloud_function/README.md) |
| **运行方式** | 命令行直接执行 或 任意 agent 框架调度 |

## 🔒 安全说明

- Cookie **加密存储**在本地（权限 600）或 GitHub Secrets 中，所有代码**完全开源**
- 原版 CLI **不会自动投币**（需用户明确开启），不送礼、不关注陌生人；云函数版的付费灯牌默认关闭，显式开启后仍受目标、账号、数量和价格限制
- GitHub Actions 对公开仓库完全免费

---

## 💌 一个魂寄语

做这个工具不是为了让大家不看直播，是因为我自己工作太忙经常错过开播，才写了这个在忙的时候帮我守着。**有时间的话还是去直播间看直播吧**，跟大家一起刷弹幕互动的快乐是工具给不了的。

如果你也关注其他主播，只需要替换脚本里对应的主播 UID 和房间号就可以用。不过……记得先关注[**嘉然今天吃什么**](https://space.bilibili.com/672328094/)哦～

<p align="center">
  <img src="assets/diana-heart.png" width="100" alt="嘉然比心" />
</p>

如果这个项目帮到了你，**请给个 Star ⭐！** 这对我真的很重要，能让更多魂们发现这个工具。也欢迎 **Fork** 到自己账号使用，遇到问题或有好的想法随时提 **Issue**，我都会看。

希望能帮到跟我一样忙碌但心里还惦记着 A-SOUL 的魂们。

---

## 📖 使用教程

### 🤖 OpenClaw / Hermes / QClaw 等 Agent 助手用户

直接把下面这句话发给你的 AI 助手，它会帮你搞定安装、配置和定时任务：

```
帮我安装这个项目并设置 A-SOUL 自动挂机：https://github.com/XiaoYiWeio/asoul-support
```

安装完成后，再告诉它：

```
帮我设置一个定时任务，每 30 分钟检测 A-SOUL 成员是否在直播，在播就帮我挂机涨亲密度并点亮粉丝牌
```

> 支持所有兼容 Python 的 agent 框架，助手会自动处理代码克隆、Cookie 配置和定时调度。

---

<details>
<summary>🐍 纯命令行用户（不使用 Agent 助手，点击展开）</summary>

**环境要求：** Python 3.9+

**第 1 步：获取代码**

```bash
git clone https://github.com/XiaoYiWeio/asoul-support.git
cd asoul-support
```

**第 2 步：配置 Cookie**

```bash
python3 scripts/checkin.py --save-cookie --sessdata "你的SESSDATA" --bili-jct "你的bili_jct"
```

如何获取 B站 Cookie：Chrome 打开 [bilibili.com](https://www.bilibili.com)（确保已登录）→ 按 **F12** → **Application** → **Cookies** → **https://www.bilibili.com**，找到 **SESSDATA** 和 **bili_jct** 复制即可。

> ⚠️ 相当于登录凭证，不要分享。约 6 个月后过期。

**第 3 步：运行**

```bash
# 检测谁在播 + 自动挂机涨亲密度
python3 scripts/heartbeat.py

# 挂机到下播为止
python3 scripts/heartbeat.py --until-offline

# 发弹幕点亮粉丝牌
python3 scripts/checkin.py --live-only
```

**第 4 步：设置定时任务（cron）**

```bash
# crontab -e
*/30 * * * * cd /path/to/asoul-support && python3 scripts/heartbeat.py --until-offline
```

</details>

---

## 🔧 GitHub Actions 自动点赞（不需要 OpenClaw）

只需要视频/动态点赞（不涨亲密度）的话，Fork 本仓库 + 配置 Cookie 即可。

<details>
<summary>📋 设置教程（点击展开）</summary>

1. 点击右上角 **Fork** → **Create fork**
2. **Settings** → **Secrets and variables** → **Actions** → 添加 `SESSDATA` 和 `BILI_JCT`
3. **Actions** 标签 → 点击 **I understand my workflows, go ahead and enable them**
4. 验证：**Actions** → **A-SOUL 自动应援** → **Run workflow**

之后每周自动执行（默认周一、周四，可在 `daily.yml` 调整）。动态点赞需编辑 `daily.yml` 将 `ENABLE_DYNAMIC_LIKE` 改为 `'true'`。

> ⚠️ 频率别调太高：B 站会对跨地区高频自动化触发风控、频繁刷新你的 Cookie。每周 1~2 次是相对安全的节奏。如果你的 Cookie 经常一两天就失效，建议用一个不在浏览器/手机日常登录的小号专门做自动化。

</details>

---

## 🌟 内置成员

| 成员 | 直播间 | 主页 |
|------|--------|------|
| 嘉然 | [22637261](https://live.bilibili.com/22637261) | [space](https://space.bilibili.com/672328094) |
| 贝拉 | [22632424](https://live.bilibili.com/22632424) | [space](https://space.bilibili.com/672353429) |
| 乃琳 | [22625027](https://live.bilibili.com/22625027) | [space](https://space.bilibili.com/672342685) |
| 心宜 | [30849777](https://live.bilibili.com/30849777) | [space](https://space.bilibili.com/3537115310721181) |
| 思诺 | [30858592](https://live.bilibili.com/30858592) | [space](https://space.bilibili.com/3537115310721781) |

## ❓ 常见问题

**Q: Cookie 过期了怎么办？** — 重新获取，重跑 `--save-cookie` 命令更新即可。GitHub Actions 失败时会发邮件通知。

**Q: 需要电脑一直开着吗？** — 用服务器/NAS 跑脚本，或用 GitHub Actions（视频/动态点赞部分），都不需要盯着。

**Q: 投币会消耗硬币吗？** — 默认不投币。需要手动开启 `--coin` 参数才会投。

**Q: 需要 Node.js 吗？** — 不需要。v4.0 纯 Python，只需 Python 3.9+。

---

## 🛠 手动命令参考

```bash
# 检测谁在播
python3 scripts/heartbeat.py --check-only

# 挂机指定成员 25 分钟
python3 scripts/heartbeat.py --members 嘉然,贝拉

# 挂机直到下播
python3 scripts/heartbeat.py --until-offline

# 发弹幕点亮粉丝牌
python3 scripts/checkin.py --live-only

# 给最近视频投币+收藏
python3 scripts/videos.py --days 7 --coin --fav
```

## License

MIT
