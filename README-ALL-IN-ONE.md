# GroktoCrawl All-in-One（全功能单镜像版）

> 上游项目: [groktopus/groktocrawl](https://github.com/groktopus/groktocrawl) · 本仓库:
> **cshdotcom/groktocrawl-allinone** · 镜像: `ghcr.io/cshdotcom/groktocrawl-allinone`

把上游 7 个服务镜像 + 3 个基础组件镜像**合并为一个全功能 Docker 镜像**，由 GitHub
Actions 自动构建并发布到 GHCR。单容器即可获得 Firecrawl v2 兼容的完整爬虫/搜索/
研究平台能力。

## 一个容器里有什么

| 组件 | 端口 | 暴露 | 说明 |
|---|---|---|---|
| agent-svc（爬虫后台） | 8080 | ✅ `AGENT_HOST_PORT` | Firecrawl 兼容 REST API、异步任务、Swagger `/docs` |
| portal-svc（爬虫门户） | 8081 | ✅ `PORTAL_HOST_PORT` | 单搜索框 Web UI |
| scraper-svc（五层爬虫） | 8001 | ❌ 仅内部 | curl_cffi / Playwright Chromium / CloakBrowser 反检测 |
| browser-svc | 8012 | ❌ 仅内部 | 无头浏览器会话服务 |
| semantic-svc | 8003 | ❌ 仅内部 | BGE-M3 多语言向量嵌入 + rerank + 向量索引 |
| parse-svc | 8013 | ❌ 仅内部 | PDF / Office 文档解析（poppler + tesseract 中英文 OCR） |
| llm-svc（离线演示） | 8011 | ❌ 仅内部 | 零配置 fixture LLM，接外部大模型后自动闲置 |
| mcp-svc | 8002 | 可选 | MCP 协议服务器（compose 注释行开启映射） |
| SearXNG 内嵌搜索 | 8888 | ❌ 仅内部 | Google/Bing/DuckDuckGo/startpage/wikipedia 等聚合 |
| Qdrant 内嵌向量库 | 6333 | ❌ 仅内部 | 语义检索持久化索引（官方 musl 静态二进制） |
| Valkey 内嵌缓存 | 6379 | ❌ 仅内部 | 任务队列 + 抓取缓存（redis-server，AOF 持久化） |
| Monitor 巡检 | — | — | 每 10 分钟检查 monitors（替代上游 ofelia 容器） |

对外只需穿透 **9080（爬虫后台）** 与 **9081（爬虫门户）** 两个端口；搜索、向量库、
缓存等全部封在容器内部的 `127.0.0.1` 上。

所有宿主端口号都是 `.env` 变量；MCP / 爬虫执行器 / 浏览器 / 语义检索 / 文档解析 /
内嵌搜索的端口也预留了变量（`MCP_HOST_PORT`、`SCRAPER_HOST_PORT`、`BROWSER_HOST_PORT`、
`SEMANTIC_HOST_PORT`、`PARSE_HOST_PORT`、`SEARXNG_HOST_PORT`），需要哪个就在
compose 里取消对应映射行注释，端口号数字直接改 `.env` 即可。

## 镜像体积（实测 GHCR）

| 指标 | 大小 |
|---|---|
| `docker pull` 下载量（压缩层, linux/amd64） | ≈ 1045 MiB（约 1 GB） |
| `docker pull` 下载量（压缩层, linux/arm64） | ≈ 1038 MiB |
| 解压后磁盘占用 | ≈ 2.5–3 GB |
| 运行期额外下载（首次启动, 存于数据卷） | BGE-M3 模型 ~2.3 GB + CloakBrowser 二进制 |

多架构 manifest 共 37 层/架构；镜像本身不含任何 AI 模型，嵌入模型按需缓存在
`$DATA_DIR/huggingface`，不在镜像体积内。

## 快速开始

```bash
cp .env.allinone.example .env
docker compose up -d

# 等健康检查通过（首次启动需下载 ~2.3GB 嵌入模型）
curl http://localhost:9080/health        # {"status":"ok", ...}
curl http://localhost:9080/docs          # Swagger UI

# 试一把抓取
docker exec groktocrawl groktocrawl scrape https://example.com
```

门户 UI 打开 `http://localhost:9081`，直接输入关键词搜索。

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

1. **多引擎聚合（SearXNG）**：Google、Bing、DuckDuckGo、Startpage、Wikipedia
   等引擎并行检索后按评分聚合 —— 各大商业引擎本身就有最强的本地语言排序；
2. **查询语言自动判定**：已修补上游硬编码 `language=en` 的缺陷，
   `SEARCH_LANGUAGE=auto` 时每个请求按其自然语言动态匹配（也支持固定 `zh-CN`
   等）；SearXNG `default_lang=auto` 与之协同；
3. **BGE-M3 跨语言混合检索**：`research/rerank/hybrid` 管线会把搜索结果下载正文、
   用 BGE-M3 向量与查询跨语言对齐再重排 —— 中文问题也能召回英文权威来源并给出
   排序良好的引用。

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

- `EMBED_QDRANT=false` + `QDRANT_URL=http://...` → 用外部向量库；
- `EMBED_VALKEY=false` + `VALKEY_URL=redis://...` → 用外部 Redis/Valkey；
- `SEARXNG_DISABLED_ENGINES=bing images,wikipedia` → 内嵌搜索剔除引擎；
- 直接挂载自己的配置：`-v my-settings.yml:/etc/searxng/settings.yml`；
- MCP 对外：compose 中取消 `MCP_HOST_PORT` 映射注释并设置 `MCP_ALLOWED_HOSTS`。

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
