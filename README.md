# YouTube 订阅每日摘要

把自己 YouTube 订阅里的新视频，每天自动整理成一封中文深度摘要邮件。

- 只申请 `youtube.readonly`，不碰 Gmail 收件箱权限
- 采集：YouTube Data API（订阅 → 上传列表）+ 频道 RSS 兜底
- 字幕：yt-dlp 自动/人工字幕优先，没有字幕退回标题+简介
- 总结：Gemini，输出固定五节结构（核心观点 / 实盘与点位 / 未来判断 / 板块个股 / 风险提示）
- 发信：Gmail SMTP + 应用专用密码
- 去重：已发过的视频永久跳过，不重复总结
- 部署：Docker 一键起，每天定时跑；也可以在任意机器上用 Python 直接跑

> 本项目为个人自用工具，输出内容仅供参考，不构成任何投资建议。

## 工作流

```
每天定时
  → 读取 data/token.json 并自动刷新 access token
  → 拉取订阅频道
  → 每个频道的最近视频（上传列表 + RSS，取并集）
  → 过滤到最近 24 小时、去重
  → yt-dlp 取字幕（失败则用标题+简介）
  → Gemini 写五节中文摘要（模型链自动回退）
  → HTML 邮件经 Gmail SMTP 发出
  → 记录已发送 videoId
```

## 前置准备（约 15 分钟）

### 1. Google Cloud 项目与 API

1. 打开 [Google Cloud Console](https://console.cloud.google.com/)，新建或选择一个项目
2. 「API 和服务 → 库」搜索 **YouTube Data API v3**，点击启用

### 2. OAuth 同意屏幕（关键）

「API 和服务 → OAuth 同意屏幕」：

1. User Type 选 **External**
2. 填写应用名称、用户支持邮箱、开发者邮箱
3. Scopes 添加 `https://www.googleapis.com/auth/youtube.readonly`
4. **Publishing status 必须改为 In production**

> 如果停留在 Testing 状态，拿到的 refresh token 只有 **7 天**有效期，之后任务会失败。
> 发布到生产后，授权页会显示「此应用未经 Google 验证」的警告，点「高级 → 继续」即可，自用完全没问题。

### 3. OAuth 客户端（Desktop app）

「API 和服务 → 凭据 → 创建凭据 → OAuth 客户端 ID」，类型选 **桌面应用**，创建后下载 JSON。
Desktop 类型无需登记回调地址，本项目的回调固定为 `http://localhost:8765/oauth2callback`。

### 4. Gemini API Key

到 [Google AI Studio](https://aistudio.google.com/apikey) 创建 API Key。

### 5. Gmail 应用专用密码

1. Google 账号「安全性」里开启 **两步验证**
2. 搜索「应用专用密码」，创建一个（名称随意，如 `youtube-digest`）
3. 复制生成的 16 位密码

> 应用专用密码只用于 SMTP 发信，不需要 Gmail API 权限。

## 快速开始（Docker）

```bash
git clone https://github.com/xiansakana/youtube-digest.git
cd youtube-digest
cp .env.example .env
mkdir -p data
```

### 第一步：本机授权，拿到 refresh token

```bash
docker compose --profile auth up --build auth
```

打开 http://localhost:8765 ：

1. 粘贴上一步下载的 `client_secret.json` 内容，点「解析并导入」
2. 点「开始 Google 授权」，登录并同意（遇到未验证警告点「高级 → 继续」）
3. 授权成功后页面会显示订阅频道列表
4. 在「摘要与投递配置」里填写收件邮箱、发件 Gmail、应用专用密码、Gemini Key
5. 点「保存配置」，再点「发送测试邮件」确认 SMTP 可用
6. 按 `Ctrl+C` 停止授权容器

此时 `data/` 下会生成：

```
data/token.json      # refresh token（务必保密）
data/.env            # 密钥与配置
data/config.json     # 收件人等设置
```

> 也可以不用容器，直接 `python auth/auth_server.py` 跑同一个授权页。

### 第二步：启动每日任务

```bash
docker compose up -d --build
docker compose logs -f digest
```

默认每天 **Asia/Shanghai 12:00** 执行一次（可用 `SEND_HOUR` / `TIMEZONE` 调整）。

## 第一天会发生什么

理解这几点，就不会误以为「没收到邮件 = 坏了」：

| 场景 | 预期结果 |
| --- | --- |
| 过去 24 小时订阅里有新视频 | 收到 1 封 HTML 邮件，每个视频一张卡片 |
| 过去 24 小时没有新视频 | **默认不发信**（`SEND_WHEN_EMPTY=false`） |
| 视频数超过 `BATCH_SIZE`（默认 15） | 拆成多封，主题带 `（1/3）` 编号 |
| 视频没有字幕 | 该条自动退回标题+简介，摘要中会注明信息有限 |
| 某个模型当日额度用完 | 自动降级到下一个模型，不会中断 |

想立刻验证而不是等到 12:00：

```bash
# 只看不发货：生成预览 HTML
docker compose run --rm digest python run.py --dry-run --days 3 --limit 3

# 真实跑一次（会发信）
docker compose run --rm digest python run.py --days 1

# 首日想回溯一整周
docker compose run --rm digest python run.py --days 7
```

预览文件在 `data/work/preview-YYYY-MM-DD.html`，可直接用浏览器打开看效果。

### 一封邮件里包含什么

每个视频：

1. 元信息：标题（可点击）、频道、时长、发布日期
2. 一句话总结
3. 一、核心观点（3–5 条加粗结论 + 数据展开）
4. 二、实盘操作与关键点位（表格：买卖明细 / 现金储备 / 支撑压力位）
5. 三、对未来市场或主题的判断（按短期 / 中期 / 长期列表格）
6. 四、重点板块、个股或嘉宾观点讨论
7. 五、客观评价与风险提示（思考框架、口径提醒、最终结论）

## 配置项

所有配置都可以用环境变量提供（`.env` 会被 compose 自动加载）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | 必填 | Gemini API Key |
| `GMAIL_USER` | 必填 | 发件 Gmail 地址 |
| `GMAIL_APP_PASSWORD` | 必填 | 16 位应用专用密码 |
| `RECIPIENTS` | 必填 | 收件邮箱，多个用逗号分隔 |
| `SENDER_NAME` | YouTube 每日摘要 | 邮件发件人显示名 |
| `GEMINI_MODEL` | gemini-4-hydrogen | 主模型 |
| `GEMINI_FALLBACK_MODELS` | gemini-3-flash-preview,gemini-3.5-flash-lite | 回退链 |
| `LOOKBACK_HOURS` | 24 | 采集时间窗口 |
| `MAX_VIDEOS_PER_DAY` | 100 | 单次任务上限 |
| `BATCH_SIZE` | 15 | 每封邮件条数 |
| `MIN_DURATION_SECONDS` | 120 | 过滤过短视频 |
| `SUBTITLE_LANGS` | zh-Hans,zh-Hant,zh,en | 字幕语言优先级 |
| `SUBTITLE_MAX_CHARS` | 60000 | 送入模型的字幕上限 |
| `SEND_WHEN_EMPTY` | false | 无新视频时是否发「今日无更新」 |
| `STATE_RETENTION_DAYS` | 120 | 去重记录保留天数 |
| `TIMEZONE` | Asia/Shanghai | 调度时区 |
| `SEND_HOUR` / `SEND_MINUTE` | 12 / 0 | 每天执行时刻 |
| `RUN_ON_START` | false | 容器启动立即跑一次 |
| `YOUTUBE_PROXY` | 空 | 抓字幕用的代理（见下节） |

也可以把配置写进 `data/config.json`，环境变量优先级更高。

## 模型与额度

Gemini 免费层实测限额（以 AI Studio 速率限制页面为准）：

| 模型 | RPM | TPM | RPD |
| --- | --- | --- | --- |
| Gemini 4 Hydrogen | 5 | 250K | 20 |
| Gemini 3 Flash | 5 | 250K | 20 |
| Gemini 3.8 Flash | 5 | 250K | 20 |
| Gemini 3.5 Flash Lite | 15 | 250K | 500 |

默认回退链与策略：

```
gemini-4-hydrogen → gemini-3-flash-preview → gemini-3.5-flash-lite
```

- **429 / 402 / 404**：额度或权限明确不可用 → 立即熔断该模型，本次任务不再请求
- **503**：临时高负载 → 连续 2 次才熔断
- 熔断结果写入 `data/state/quota.json`（按日期），同一天重跑也不会重复白发请求
- 总结失败的视频不发信、不记录去重，下次任务自动重试
- 高额度模型每天只有 20 次，所以前面有额度就用好模型，打满后逐条自动降级到 lite

## 字幕与代理（重要）

YouTube 会直接拒绝云厂商 IP（AWS / GCP / Azure）的字幕请求，表现为
`Sign in to confirm you're not a bot` 或 `IP belongs to a cloud provider`。

按优先级，程序会使用：

1. `YOUTUBE_PROXY` 环境变量（推荐）
2. 本地 `127.0.0.1:40000`（例如 Cloudflare WARP 的 SOCKS5 proxy 模式）
3. 本地 `127.0.0.1:18080`（SSH 反向隧道）
4. 直连

推荐做法：

- **家用 / 办公网络**：直连通常就能取到字幕，不需要代理
- **云服务器**：在上面跑一个本地代理客户端（如 Cloudflare WARP 的 `warp-cli mode proxy`），
  然后设置 `YOUTUBE_PROXY=socks5h://127.0.0.1:40000`
- 也可以把浏览器导出的 cookies 放到 `data/cookies.txt`（Netscape 格式），程序会自动带上

没有代理时任务不会失败，只是无字幕的视频会退回标题+简介。

## 数据目录

```
data/
├── token.json        # OAuth refresh token（保密）
├── .env              # 密钥与配置（保密）
├── config.json       # 收件人等设置
├── cookies.txt       # 可选，YouTube cookies
├── state/
│   ├── sent.json     # 已发送 videoId 去重
│   └── quota.json    # 当日已耗尽额度的模型
├── logs/             # 每日运行日志
└── work/             # 预览与单条摘要草稿
```

`data/`、`.env` 都已在 `.gitignore` 中，不会被提交。

## 本机运行（不用 Docker）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# 1. 授权
python auth/auth_server.py            # 打开 http://localhost:8765

# 2. 试跑
export YTDIGEST_HOME=./data
python app/run.py --check
python app/run.py --dry-run --days 3 --limit 3

# 3. 真实运行
python app/run.py --days 1
```

本机定时可以交给系统 cron / 计划任务，调用
`YTDIGEST_HOME=./data python app/run.py` 即可。

## 命令行参数

```bash
python run.py --dry-run      # 不发信、不写去重，只生成预览
python run.py --days 7       # 回溯 N 天（同时作用于采集与摘要）
python run.py --limit 5      # 最多处理 5 条
python run.py --channel 关键词 # 只处理标题匹配的频道
python run.py --force        # 忽略去重，重新处理
python run.py --no-subtitles # 跳过字幕抓取
python run.py --check        # 检查 token / API / SMTP / yt-dlp
```

## 安全

- 授权控制台只监听 `127.0.0.1`
- `token.json`、`.env` 写入时权限为 600
- 所有密钥只通过环境变量或本地受限配置文件提供，代码与仓库中不含任何真实凭据
- 仓库中的 `.env.example` 全部为占位符

## 常见问题

**授权成功但 7 天后失效**
OAuth 同意屏幕还在 Testing 状态。改为 In production 后重新授权一次。

**一直收到「无字幕」**
1. 该视频本身没有字幕（yt-dlp 会明确返回 `no subtitles for the requested languages`）
2. 或者 IP 被 YouTube 判定为云厂商 → 配 `YOUTUBE_PROXY`

**Gemini 429 / 503**
免费层日额度或临时负载问题。程序会自动降级；想稳定用强模型可到 AI Studio 为该 key 所在项目充值。

**邮件没收到**
1. 先 `python run.py --check` 确认 SMTP 登录成功
2. 查垃圾邮件
3. 看 `data/logs/` 里当天的日志是否有发送记录

**Docker 构建慢**
主要耗时在 `pip install yt-dlp` 和安装 Deno，首次构建后镜像会缓存。

## 目录结构

```
youtube-digest/
├── app/
│   ├── run.py               # 任务入口（命令行）
│   ├── scheduler.py         # 容器内定时调度
│   └── ytdigest/
│       ├── config.py        # 环境变量优先的配置
│       ├── youtube.py       # YouTube Data API + token 刷新
│       ├── rss.py           # 频道 RSS 兜底
│       ├── subtitles.py     # yt-dlp 字幕抓取与清洗
│       ├── gemini.py        # Gemini 总结与模型回退
│       ├── render.py        # HTML 邮件模板
│       ├── mailer.py        # Gmail SMTP
│       ├── store.py         # 去重与额度状态
│       └── pipeline.py      # 主流程
├── auth/
│   ├── auth_server.py       # 本机 OAuth 授权控制台
│   └── index.html
├── docker/entrypoint.sh
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## License

MIT
