# GroktoCrawl All-in-One（轻量版 LITE 单镜像）

> 上游项目: [groktopus/groktocrawl](https://github.com/groktopus/groktocrawl) · 本仓库:
> **cshdotcom/groktocrawl-allinone** (`lite` 分支) · 镜像: `ghcr.io/cshdotcom/groktocrawl-allinone:lite`

> **⚠️ 这是 LITE 轻量版（lite 分支）**。与 main 分支全功能版的区别：
>
> | | full (main, `:latest`) | **lite (本分支, `:lite`)** |
> |---|---|---|
> | scrape / crawl / map / extract / llmstxt / parse | ✅ | ✅ |
> | browser 会话 + MCP 全套对应工具 | ✅ | ✅ |
> | monitor 定时巡检 | ✅ scrape+search 型 | ✅ 仅 scrape 型 |
> | 内嵌 SearXNG 搜索（`/v2/search`） | ✅ | ❌ 已移除 |
> | 内嵌 Qdrant + semantic-svc（BGE-M3 向量检索） | ✅ | ❌ 已移除 |
> | 自主研究 Agent（`/v2/agent` / `/v2/answer` / 研究记忆） | ✅ | ❌ 已移除 |
> | 门户 Web UI（9081） | ✅ | ❌ 已移除（配 OpenWebUI 等外部 Agent 更合适） |
> | 常驻内存 | ≈ 3.5–5 GB（BGE-M3 加载后 2–3GB） | **≈ 1–1.5 GB** |
> | 镜像解压体积 | ≈ 2.5–3 GB | **≈ 1.5 GB 量级** |
>
> `lite` 分支推送后 CI 自动产出 `:lite` 与 `:sha-<short>` 两个 tag；`latest` 永远
> 只跟随 main 分支的全功能版。想用搜索/语义检索/研究 Agent，请拉 `:latest`。

把上游服务镜像合并为**单个轻量 Docker 镜像**：砍掉搜索/向量库/研究 Agent 三大
内存大户后，单容器专注做好 Firecrawl v2 兼容的**抓取与浏览器自动化**能力，由
GitHub Actions 自动构建并发布到 GHCR。

## 一个容器里有什么

| 组件 | 端口 | 暴露 | 说明 |
|---|---|---|---|
| agent-svc（爬虫后台） | 8080 | ✅ `AGENT_HOST_PORT` | Firecrawl 兼容 REST API、异步任务、Swagger `/docs` |
| scraper-svc（五层爬虫） | 8001 | ❌ 仅内部 | curl_cffi / Playwright Chromium / CloakBrowser 反检测 |
| browser-svc | 8012 | ❌ 仅内部 | 无头浏览器会话服务 |
| parse-svc | 8013 | ❌ 仅内部 | PDF / Office 文档解析（poppler + tesseract 中英文 OCR） |
| llm-svc（离线演示） | 8011 | ❌ 仅内部 | 零配置 fixture LLM（extract/llmstxt 用），接外部大模型后自动闲置 |
| mcp-svc | 8002 | 可选 | MCP 协议服务器（compose 注释行开启映射） |
| Valkey 内嵌缓存 | 6379 | ❌ 仅内部 | 任务队列 + 抓取缓存（redis-server，AOF 持久化） |
| Monitor 巡检 | — | — | 每 10 分钟检查 scrape 型 monitors（替代上游 ofelia 容器） |

对外只需穿透 **9080（爬虫后台）** 一个端口；缓存等组件全部封在容器内部的
`127.0.0.1` 上。

所有宿主端口号都是 `.env` 变量；MCP / 爬虫执行器 / 浏览器 / 文档解析的端口也
预留了变量（`MCP_HOST_PORT`、`SCRAPER_HOST_PORT`、`BROWSER_HOST_PORT`、
`PARSE_HOST_PORT`），需要哪个就在 compose 里取消对应映射行注释，端口号数字
直接改 `.env` 即可。

## 镜像体积（实测 GHCR）

| 指标 | full (latest) | lite (本分支) |
|---|---|---|
| `docker pull` 下载量（压缩层, linux/amd64） | ≈ 1045 MiB | 以 CI 实际输出为准（删去 torch/qdrant/searxng 后显著更小） |
| 解压后磁盘占用 | ≈ 2.5–3 GB | ≈ 1.5 GB 量级 |
| 运行期额外下载（首次启动, 存于数据卷） | BGE-M3 模型 ~2.3 GB | 仅 CloakBrowser 二进制（可选） |

lite 镜像不含 torch / BGE-M3 / qdrant / searxng，无需下载任何嵌入模型。

## 快速开始

```bash
cp .env.allinone.example .env
docker compose up -d

# 等健康检查通过（lite 无嵌入模型下载, 通常十几秒内就绪）
curl http://localhost:9080/health        # {"status":"ok", ...}
curl http://localhost:9080/docs          # Swagger UI

# 试一把抓取
docker exec groktocrawl groktocrawl scrape https://example.com
```

门户 UI 打开 `http://localhost:9081`，直接输入关键词搜索。

## 从旧多镜像 .env 迁移（防呆已内置）

如果 `.env` 是从旧的多镜像版拷来的，里面可能还留着 `redis://valkey:6379/0`、`http://slopsearx:8080` 这类按容器名互联的地址。单容器里这些主机名并不存在，会导致：搜索空结果、内嵌搜索不启动、爬取缓存被静默禁用、analytics 错误刷屏（日志包实测单日 436 条）。

新版镜像启动时会自动识别这类遗留主机名并忽略（日志打 `WARNING: ... 指向旧多镜像容器名`），回退到内嵌组件，**无需手动清理也能正常工作**。两个例外：

- 显式设置了 `EMBED_*=false` 表示“我要用外部实例”时，地址原样保留；
- 接真实外部服务请写 IP 或域名（如 `redis://192.168.1.10:6379/0`），单个容器名会被视为遗留地址。

## docker compose 一图流

```text
┌─────────────────── groktocrawl:latest ───────────────────┐
│ supervisord (PID1, 进程守护/日志/zombie回收)              │
│   ├─ valkey        127.0.0.1:6379  AOF → ./data/valkey    │
│   ├─ qdrant        127.0.0.1:6333  存储 → ./data/qdrant   │
│   ├─ searxng       127.0.0.1:8888  配置自动生成           │
│   ├─ parse/browser/scraper/semantic   (各业务服务)         │
│   ├─ llm-fixture   127.0.0.1:8011  (可选演示用)           │
│   ├─ agent-svc     0.0.0.0:8080 ◄────────────┐            │
│   ├─ portal-svc    0.0.0.0:8081 ◄─┐          │            │
│   ├─ mcp-svc       0.0.0.0:8002   │          │            │
│   └─ monitor-loop  10min巡检      │          │            │
└────────────────────────────────│─────────│────────────┘
      9081 ↩ 门户                │          │
      9080 ↩ 后台 API ◄──────────┘◄─────────┘ (穿透这两处即可)
```

## 内嵌搜索为什么"任何语言相关性都非常高"

本镜像做了三层相关性保障：

1. **多引擎聚合（SearXNG）**：默认启用「中国大陆直连可达精选集」（见下节）
   并行检索后按评分聚合 —— 各大商业引擎本身就有最强的本地语言排序；
2. **查询语言自动判定**：已修补上游硬编码 `language=en` 的缺陷，
   `SEARCH_LANGUAGE=auto` 时每个请求按其自然语言动态匹配（也支持固定 `zh-CN`
   等）；SearXNG `default_lang=auto` 与之协同；
3. **BGE-M3 跨语言混合检索**：`research/rerank/hybrid` 管线会把搜索结果下载正文、
   用 BGE-M3 向量与查询跨语言对齐再重排 —— 中文问题也能召回英文权威来源并给出
   排序良好的引用。

### 默认引擎集（大陆直连优化，GHCR `latest` 起生效）

国内服务器**无需代理**即可搜索，且已剔除百度/搜狗等广告密集源，只保留直连可达、
结果干净的引擎：

| 类别 | 引擎 | 说明 |
|---|---|---|
| 通用网页/资讯 | `bing` `bing news` `bing images` `bing videos` | 大陆可达的唯一国际主流全网索引 |
| 开发者/IT | `github` `mdn` `microsoft learn` `stackoverflow` | 技术文档与问答 |
| 学术 | `arxiv` `crossref` `semantic scholar` `pubmed` | 全部零广告 |
| 中文垂直 | `bilibili` `chinaso news` `sina` | 国家搜索-新闻/新浪-新闻 |

被墙引擎（Google/DuckDuckGo/Brave/Startpage/Wikipedia 等）默认不启用，避免每次查询都白等
超时；海外服务器或已配代理时设 `SEARXNG_ENGINES=all` 即可恢复全部引擎，或用
`SEARXNG_OUTGOING_PROXY` 只给搜索引擎单独配出站代理。

> 注意：大陆直连下即使引擎可达，数据中心 IP 偶尔仍会触发 Bing 人机校验；
> 出现整批超时可检查 `searxng.log`。

上游 SlopSearX 镜像不需要了：普通部署内嵌 SearXNG 即可；如果你是 SlopSearX 重度用户，
设 `SEARXNG_URL=http://your-slopsearx:8080` 就会自动停用内嵌搜索并切换过去。

## 常见自定义

所有配置都在 `.env`（模板 `.env.allinone.example`），改完 `docker compose up -d` 生效：

```ini
# 改端口
AGENT_HOST_PORT=19080
PORTAL_HOST_PORT=19081
# 可选端口同例(如想暴露 MCP):
# MCP_HOST_PORT=9082


# 改存储位置（整体搬到 NAS/数据盘）
DATA_DIR=/mnt/nas/groktocrawl

# 接入 DeepSeek
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 用外部搜索实例替换内嵌 SearXNG（EMBED_SEARXNG=auto 会自动让位）
SEARXNG_URL=http://192.168.1.20:8888

# 固定中文搜索
SEARCH_LANGUAGE=zh-CN

# 公网前务必设置 API Key
API_KEY=<openssl rand -hex 32>
```

细分开关：

- `BROWSER_NAVIGATE_TIMEOUT_MS=30000` → 浏览器动作（navigate/click/wait 等）
  的默认超时毫秒，与 Playwright 官方默认一致。个别慢站（首字节慢、或阻塞脚本
  指向被墙域）navigate 报 `Timeout xx ms exceeded` 时调大，如 `60000`；
  也可以在单次工具调用里传 `timeout` 参数临时覆盖（毫秒）；
- `PARSE_HOSTED_OCR_API_URL=` → 扫描版 PDF 托管 OCR（可选）：`/v2/parse` 传
  `ocr=hosted`（CLI `--ocr hosted`）时把整份文档交给外部 OCR API；留空则
  始终本地 OCR；
- `YOUTUBE_*_INTERVAL` / `YOUTUBE_RATE_LIMIT_COOLDOWN` → YouTube 字幕抓取
  节流与限流冷却（默认已保守，被限流自动冷却 1 小时并回退浏览器渲染）；
- `EMBED_QDRANT=false` + `QDRANT_URL=http://...` → 用外部向量库；
- `EMBED_VALKEY=false` + `VALKEY_URL=redis://...` → 用外部 Redis/Valkey；
- `SEARXNG_ENGINES=` → 引擎白名单：留空=大陆精选集，`all`=全部，或逗号分隔精确指定；
- `SEARXNG_DISABLED_ENGINES=bing images` → 在白名单基础上再剔除；
- `SEARXNG_OUTGOING_PROXY=http://192.168.1.10:7890` → 仅搜索引擎走代理（可恢复被墙引擎）；
- 直接挂载自己的配置：`-v my-settings.yml:/etc/searxng/settings.yml`
  （检测到挂载后启动时不会重新生成，可放心自定义）；
- MCP 对外：compose 中取消 `MCP_HOST_PORT` 映射注释；若设置了 `API_KEY`，
  MCP 的 `/mcp` 端点与 MCP→agent 内部调用会自动使用同值鉴权
  （`GROKTOCRAWL_API_KEY` 由 compose 自动接线，客户端侧填同一 key 即可）。

## 人机验证（验证码）是怎么处理的

内置验证码自动恢复模块位于 scraper-svc（`scraper/captcha.py`，上游 ADR-0044），
无第三方打码平台依赖：

1. **自动识别**三类主流人机验证：Google reCAPTCHA、hCaptcha、Cloudflare Turnstile；
2. **自动恢复**按序尝试：被动等待自过 → 点复选框 → 图片九宫格视觉识别
   （最多 2 轮）；复选框一步零配置即可用；
3. 图片九宫格题可选接入任一 OpenAI 兼容多模态模型读图作答：
   `CAPTCHA_VISION_BASE_URL` + `CAPTCHA_VISION_API_KEY` + `CAPTCHA_VISION_MODEL`
   （三项全填才启用，与主模型 API Key 独立）；
4. 全部失败时返回 `CAPTCHA_UNRESOLVED` 错误并跳过该页（不会卡死任务）；
   搭配 `SCRAPER_PROXY_URL`（SOCKS/住宅代理）可显著降低触发率；
5. Cloudflare 强校验页另可通过外挂 FlareSolverr 增强（`FLARE_SOLVERR_URL`）。

配套反检测能力：CloakBrowser 反指纹浏览器 + Playwright stealth 配置，从源头减少验证码出现概率。

## 生产部署要点

```bash
# docker logs -f groktocrawl        # 所有进程日志汇聚于此
# docker exec groktocrawl supervisorctl status   # 查看各进程状态
```

- **公网暴露请设置 `API_KEY`**（未设时服务返回 `X-Security-Warning` 头）。
- **穿透方案**：frp/Nginx 反代时保留 `Host` 头指向 9080 即可；门户反代同理指向 9081。
- **内存建议 ≥4GB**（BGE-M3 加载约 2–3GB；不跑语义功能可 `MEM_LIMIT=3g` 限住，
  或保持默认 6g）。
- **首次启动慢**：拉取 bge-m3 模型（HF_ENDPOINT=https://hf-mirror.com 可加速国内网络）、
  CloakBrowser 二进制下载——均已持久化在 `$DATA_DIR` 下，重启不再重复。
- 异步任务与上游一致：**重启即丢，尽力交付**（webhook 尽力补发，无断点续跑）。

## CI/CD（GitHub Actions）

`.github/workflows/build-allinone-image.yml`：

- 触发：push 到 `main`、打 `v*` 标签、或手动选择架构；
- Buildx 多架构构建 `linux/amd64` + `linux/arm64`；
- 推送 `ghcr.io/cshdotcom/groktocrawl-allinone:{latest,main,vX.Y.Z,sha}`；
- 缓存走 GHA cache；使用内置 `GITHUB_TOKEN`（无需额外密钥）。

自建构建：

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/cshdotcom/groktocrawl-allinone:local .
```

## 目录结构

```
Dockerfile                     全功能单镜像构建文件
docker/aio/entrypoint.sh       容器入口: EMBED_* 解析 + SearXNG 配置生成 + supervisor 编排
docker/aio/supervisord.conf    supervisord 基础配置
docker-compose.yml             ← 你要的编排文件(端口/存储/AI/搜索可改)
.env.allinone.example          中文注释环境变量全集
docker-compose.multiimage.yml  上游原始多镜像编排(保留备查)
agent-svc/agent/searxng_client.py   含 ALLINONE PATCH(SEARCH_LANGUAGE)
.github/workflows/build-allinone-image.yml   GHCR 构建流水线
```

其余文件继承自上游 v0.13.0（MIT License, © groktopus/groktopus contributors 及上游作者）。
