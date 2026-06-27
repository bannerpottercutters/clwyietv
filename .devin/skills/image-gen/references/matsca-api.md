# matsca / gpt-image-2 API 参考（随技能加载）

> **事实型规格速查**——端点、参数、字段、错误码、凭证、在途数字、计费、机读产物 schema。能查表/照抄的硬事实都在这；**用法看 `SKILL.md`，根因/踩坑/排错心法看 `image-gen-notes.md`。**
>
> 这是 `image-gen` 技能的随附 reference（skill-native 工具随技能自动加载；Devin 经 `AGENTS.md` 索引按需读）。最后核对：2026-06。

## 目录
1. Base / 鉴权 / 红线
2. 模型
3. 端点速查
4. 文生图参数与返回结构
5. 调用模式与计费
6. 错误分类：可重试 / 不可重试清单
7. 错误码速查
8. 单密钥在途上限（数字）
9. 凭证（env / header / 取值顺序）
10. native 取图与下载行为
11. 文件名清洗与扩展名判定
12. 机读产物：manifest.json / 退出码 / --json-out
13. 可观测性字段：.log / .heartbeat / status.py
14. 开发者登录与账号自检（/api/dev/*）

---

## 1. Base / 鉴权 / 红线
- Base URL：`https://img.matsca.com`（OpenAI 兼容，替换 `base_url` 即可用 OpenAI SDK）。
- 鉴权：`Authorization: Bearer <API_KEY>`（app 模式另加 `X-App-ID`/`X-App-Secret`）。
- ⛔ **`GET /v1/ping` 红线禁用**——即便官方文档说 ping 不耗积分，实测会触发风控封禁，绝不调用（根因见 `image-gen-notes.md` §1）。

## 2. 模型
- 图片：**`gpt-image-2`（主力）**；别名 `gpt-image-1`/`dall-e-3`/`chatgpt-image-latest` 等自动映射到 `gpt-image-2`。

## 3. 端点速查
| 方法 | 端点 | 说明 |
|---|---|---|
| POST | `/v1/images/generations` | 文生图（同步）——**app/direct/native 都走这里**（native 用 `response_format=url`） |
| POST | `/v1/images/edits` | 图生图 / 编辑（multipart：`prompt` + `image`/`image[]` + 可选 `mask` + 同生成参数；JSON 体也可内联 `image`/`images`/`mask`） |
| POST | `/v1/images/variations` | 图片变体（用 edit 解析器，无 prompt 时默认 `Create a natural variation of the provided image.`） |
| GET | `/v1/models` | 模型列表 |

> 异步任务端点 `/api/image-tasks/*` 已不再使用（早期 direct 异步幂等方案已废弃，根因见 `image-gen-notes.md` §3）。
>
> **脚本已实现 `/v1/images/edits`**：`gen_image.py --edit <原图> [--mask <蒙版>]` 以 multipart/form-data 发送（`prompt`+`image`+可选 `mask`+同生成参数），用 `http.client` 手写 boundary 以保 `X-App-ID` 头大小写。`--input-fidelity high/low` 控制对原图保真度。批量改图在 `prompts.json` 每项写 `"edit"`/`"mask"`。
>
> **脚本已实现 `/v1/images/variations`**：`gen_image.py --variation <原图>` 同样走 multipart，**无需 prompt**（给了则带上，服务端无 prompt 时默认 `Create a natural variation of the provided image.`）；生成原图的变体。批量变体在 `prompts.json` 每项写 `"variation":"原图路径"`（可不带 `prompt`）。三者（文生/改图/变体）互斥，主备/Coverage-First/退避重试逻辑一致。

## 4. 文生图参数与返回结构（`/v1/images/generations`）
| 参数 | 取值 | 说明 |
|---|---|---|
| `model` | `gpt-image-2`（默认） | 模型 |
| `prompt` | string | 必填，提示词 |
| `n` | 1–4 | 生成数量，默认 1（脚本逐张以 `n=1` 并发发，便于先到为主） |
| `size` | `宽x高`，`64`~`8192`；`auto` | 非 admin 客户最长边 >`2048` 需高清权限；上游会按档位归一化（请求 `1536x864` 实得略大属正常） |
| `quality` | `auto`/`low`/`medium`/`high` | 旧别名 `standard`→`auto`、`hd`→`high`；三档同价 |
| `background` | `auto`/`transparent`/`opaque` | 透明 JPEG 自动转 PNG |
| `moderation` | `auto`/`low` | 脚本默认 `low` |
| `style` | `vivid`/`natural` | 风格 |
| `output_image_format` | `png`/`jpeg`/`webp` | `jpg` 归一为 `jpeg` |
| `output_compression` | 0–100 | jpeg/webp 压缩 |
| `response_format` | `b64_json`（默认）/`url` | 返回形态（native 用 url） |
| `input_fidelity` | `low`/`high` | 仅图生图 |
| `partial_images` | 0–3 | 流式分块数 |

### 返回结构（OpenAI 兼容）
```json
{"created": 1710000000, "data": [{"b64_json": "<base64>"}]}
```
`response_format=url` 时为 `{"created":..., "data":[{"url":"https://img.matsca.com/v1/images/tmp/..."}]}`。

## 5. 调用模式与计费
每把 Key 同一时间只属于一种模式，三种模式各登记一把 Key（`secrets.env`），脚本 `--mode {auto,app,direct,native}` 选；默认 `auto`（有 App 凭证→app，否则→direct）。本仓已配 App 凭证，**默认就是 app（×1，最便宜）**。

| 模式 | `--mode` | env 变量名 | 倍率 | 端点 / 策略 | 额外凭证 |
|---|---|---|---|---|---|
| 应用（默认） | `app` | `MATSCA_APP_KEY` | **×1** | **同步** `/v1/images/generations`，取 `b64_json` | `X-App-ID`+`X-App-Secret` |
| 直连 | `direct` | `MATSCA_DIRECT_KEY` | ×2 | **同步** `/v1/images/generations`，取 `b64_json` | 无（仅 Key） |
| 原生 | `native` | `MATSCA_NATIVE_KEY` | ×3 | **同步** `response_format=url` 取链接再下载，环境不通挂 `--proxy` | 无（仅 Key） |
| 商用 | —（账号级） | — | ×3 | 开通后全仓 Key 统一 ×3，独立资源池 | — |

- **三模式现在都走同步端点**；脚本仅在 `--mode app` 时带 `X-App-ID`/`X-App-Secret`；direct/native 只带 `Authorization`。App 头**大小写敏感**（`X-App-ID`，不是 `X-App-Id`），脚本用 `http.client` 原样发送（urllib 会把头名 title 化导致服务端误报"缺少应用凭证"）。
- **计费公式：`基础分 × 张数 × 尺寸质量倍率 × 模式倍率`**（app ×1 / direct ×2 / native·商用 ×3）。质量 low/medium/high **同价**，真正抬价的是尺寸与模式倍率。实测（app ×1）`1024x1024`、`1536x864` 均约 **2 积分/张**，默认 `-n 2`≈ 4 积分/次。
- 普通失败自动退款；**唯一不退款且可能追加罚金的是"内容安全拦截"** → 这类失败**必须停、改 prompt，绝不重试**。

## 6. 错误分类：可重试 / 不可重试清单
脚本已内置服务商官方的错误分类与退避（`with_retries`/`should_retry_error`/`retry_delay`），**不应手动重发**（为什么见 `image-gen-notes.md` §2）。

**可重试（服务端/容量类瞬时错误，不计你的风控分）：**
- HTTP `408/409/425/429/500/502/503/504/520/522/524`
- 业务码：`upstream_timeout`、`upstream_unreachable`、`upstream_session_pool_exhausted`、`api_agent_queue_full`、`upstream_rate_limited`、`no_available_account`、`account_concurrency_exhausted`、`upstream_direct_unavailable`、`upstream_server_error`、`upstream_error`
- 纯网络层异常（连接重置/超时/DNS）

**不可重试（客户侧错误，重试只会累加风控错误率/加重封禁）：**
- 鉴权/凭证错（bad or missing credentials）
- 参数错（invalid image parameters）
- `content_policy_violation`（内容违规）
- 余额不足 / 额度耗尽（insufficient wallet balance / quota exhausted）
- `high_res_not_enabled`（无高清权限，最长边 >2048 需要）
- `trivial_intercept`（被判琐碎请求）、`api_key_temporarily_banned`（key 临时封禁）

**退避策略：** 优先尊重服务端 `Retry-After`；否则指数退避带 jitter（`min(60, 1.5*2^attempt) + jitter`，上限 60s）。固定次数（`--retries`，默认 5）后失败即停。单次请求墙钟上限 `--timeout` 默认 600s。

## 7. 错误码速查
- `401` Key 无效/格式错 → 查对应模式的 Key（app=`MATSCA_APP_KEY`/direct=`MATSCA_DIRECT_KEY`/native=`MATSCA_NATIVE_KEY`）。
- `403` 凭证/Header 不对（app 模式缺 `X-App-ID`/`X-App-Secret`，或 `X-App-ID` 大小写错）。
- `429` **看报文分流**：
  - "请求过于频繁 / rate / too many" = 突发频率/并发超限 → 脚本自动短退避重试，**通常无需干预**；频繁则用 `status.py` 看占用、减少同时在跑的内容数（守住 app=4）。**别一律当"要慢下来"砍并发/砍备图。**
  - "积分不足 / insufficient" = 真没钱 → **停下来告诉用户充值**，重试无意义。
- `5xx` / `upstream_*` / `account_concurrency_exhausted` → 服务端/容量类瞬时，脚本自动退避重试，**不计你的风控分**，耐心等，**绝不 ping 探活**。
- **上游网关 5xx 风暴侦测**：脚本累计网关 5xx（`GATEWAY_5XX_CODES={502,503,504,520,522,524}`），当**仍在撞 5xx（近 `STORM_RECENT_WINDOW=90s`）**且（累计 ≥ `STORM_5XX_THRESHOLD=12` **或** ≥ `STORM_NO_PROGRESS_SECONDS=150s` 零新图）→ 判风暴：heartbeat 写 `upstream_storm:true`/`storm_5xx`/`storm_no_progress_s`，stderr/`.log` 打一次 `>>> UPSTREAM_502_STORM <<<`。阈值**故意调迟钝**避免乱报。可选熔断 `--storm-give-up-after <秒>`（默认关）：风暴持续超时即提前收尾，返回已出图 + 缺图清单（manifest `errors[]`、顶层 `upstream_storm`/`gave_up`），不再耗尽 `--retries`。（502 风暴 vs 账号池满 vs 风控的区分与提醒 SOP 见 `image-gen-notes.md` §7。）
- `content_policy_violation` / `high_res_not_enabled` / `trivial_intercept` / `api_key_temporarily_banned` → **客户侧错误，脚本不重试**，按提示处理或告诉用户。
- 中文文件名/日志乱码 → 多半是终端 PTY 回显问题，磁盘上实际文件名是对的，用读文件工具核实。

## 8. 单密钥在途上限（数字）
**真正限制你的 = 单密钥在途上限（per-key in-flight），按密钥算、不是按账号。普通用户额度（服务商已确认未给本账号提额）：**

| 场景 | 默认在途上限 | 后端代码位置 |
|---|---|---|
| **app 模式**普通 key（你常用） | **4** | `defaults.py:55` / `risk_config.py:142` |
| 直连普通 key | 8 | `defaults.py:54` / `risk_config.py:132` |
| app·商业版 | 12 | `defaults.py:57` / `risk_config.py:162` |
| 直连·商业版 | 16 | `defaults.py:56` / `risk_config.py:152` |
| signed_url 直连 / 商业 | 12 / 48 | `defaults.py:58-59` |

- **上游账号并发**（`defaults.py:37` `DEFAULT_IMAGE_ACCOUNT_CONCURRENCY = 3`）：服务商账号池侧调度，撞满返回 `account_concurrency_exhausted`（容量类、不计你风控分，脚本自动退避重试）。
- **单次调用默认并发（脚本 `--concurrency 0` 自适应）**：app→3 / direct→4 / native→3（`conc = min(--concurrency, n)`，所以日常 `-n 2` 实际并发恒为 2）。
- 该额度由"同一密钥下所有同时在跑的 `gen_image.py` 进程"**共享**（认知与防撞墙打法见 `image-gen-notes.md` §4）。

## 9. 凭证（三模式各一把 Key，集中存于 `720_Agents\secrets.env`）
- `MATSCA_APP_KEY`（应用 ×1）+ `MATSCA_APP_ID` + `MATSCA_APP_SECRET`；app 模式三 Header：`Authorization: Bearer <KEY>`、`X-App-ID: <APP_ID>`、`X-App-Secret: <APP_SECRET>`。
- `MATSCA_DIRECT_KEY`（直连 ×2）：仅 `Authorization: Bearer <KEY>`。
- `MATSCA_NATIVE_KEY`（原生 ×3）：仅 `Authorization`；下载结果图走 `--proxy`/`MATSCA_PROXY`。
- 取值顺序：① 进程环境变量覆盖 → ② `secrets.env`（脚本同时尝试相对路径与 `D:\700_Resources\720_Agents\secrets.env` 绝对路径）。新增/换 Key 只改 `secrets.env` 一处。密钥数量上限：普通客户最多 3 把。

## 10. native 取图与下载行为
- 原生模式同步图片接口走官方 API 直连：`POST /v1/images/generations` 带 `response_format=url` → 取 `data[].url` → 下载该图片。
- 能直连官方图片资源的环境可不设代理；**不能直连时，只在"下载结果图片"这一步挂 HTTP/SOCKS 代理**（脚本 `--proxy` 或环境 `MATSCA_PROXY`）。官方直连不可用时服务端回 `upstream_direct_unavailable`，按 5xx 退避重发。
- **取图不按 mode 钉死格式（照搬服务商 `generate_one`）**：逐张看返回里实际有什么——有 `b64_json` 就解码、否则下载 `url`。理由：上游拥堵/降级时即便请求了 `response_format=url` 也可能回 b64，钉死 mode 会把已出的图误判成「无结果返回」而失败。
- **下载（`_download`，照搬服务商 `download_url`）三个细节**：
  - ① 带 `User-Agent: image-gen/1.0`——urllib 默认 UA（`Python-urllib/x.y`）常被 CDN/WAF 直接 403，导致 native 下载间歇失败。
  - ② 识别内联 `data:` URI（`data:image/png;base64,...`）→ 直接 base64 解码返回，不当成网址去 urlopen（否则崩）。
  - ③ 下载超时跟随 `--timeout`（默认 600s），不再写死 120s——大图 + 慢 CDN 时不会被早早掐断。
- stream：同步 `/v1/images/generations` 带 `stream:true` 实测常只回 keepalive 不回图，**脚本同步路径默认走非流式**（取 `data[0].b64_json`），不必也别开 stream。

## 11. 文件名清洗与扩展名判定
- **文件名清洗（`_safe_name`，照搬服务商 `compact_slug`）**：`--name`/批量项 `name` 落盘前过一层清洗——把 Windows 非法字符（`/ \ : * ? " < > |`）等替换为 `-`、去掉首尾 `-._`、截断到 80、空名用 `sha1(uuid4)[:12]` 兜底（保留中文）。避免名字带特殊字符或过长时落盘报错/把图丢进意外子目录。注意：`-o <完整路径>` 显式指定时按用户给的路径走、不清洗。
- 扩展名按图片 magic bytes 判（PNG/JPEG/WEBP），判不出回退 `--output-format`、再回退 `png`。
- 落盘约定：图片 → `output/fig/`，网页 → `output/html/`。

## 12. 机读产物：manifest.json / 退出码 / --json-out
### 退出码语义：主图落盘＝成功
`_gen` 对**每个并发任务单独 try/except**：单张失败只记日志、跳过，**绝不连累其余/主图**（`SystemExit` 也吞，`KeyboardInterrupt` 才放行）；主图一旦落盘，之后任何异常都吞掉、正常收尾。**口径：有主图＝exit 0，没主图才非 0。**"某张备图失败"是正常的，不该当整次失败去重跑。

### manifest.json + stdout 摘要 / --json-out（服务商 matscaimg 风格，别再 ls 磁盘猜）
脚本结束会在**输出目录写 `manifest.json`**（单内容带 `-o` 时写到主图所在目录；批量/默认写到 `--outdir`），结构照服务商：
```json
{
  "created_at": "2026-06-26T08:25:01Z",
  "results": [
    {"name":"蛙卵","job_index":1,"prompt":"...","ok":true,
     "primary":".../蛙卵_20260626.png","alts":[".../蛙卵（备1）_20260626.png"],
     "n_requested":2,"n_got":2,
     "saved":[{"path":"...","bytes":12345,"role":"primary"},{"path":"...","bytes":11000,"role":"alt"}],
     "size":"1536x864","mode":"app","created":null}
  ],
  "errors": [
    {"job_index":2,"name":"蝌蚪","prompt":"...","error":"account_concurrency_exhausted"}
  ]
}
```
- `results[]` 在服务商 `saved[]`=`[{path,bytes,role}]` 基础上叠了我们的 `primary/alts`（先到为主语义）、`n_requested/n_got`、`job_index`、`mode`（主图实际走的模式）；`role` 取 `primary`/`alt`。
- `errors[]` 是未出主图的内容（`{job_index,name,prompt,error}`），`error` 取该内容最后一次失败原因。
- **计费透明**：`_compute_billing(jobs)` 按每张已落盘图的实际 mode 聚合 → `total_credits` + `by_mode={mode:{images,credits}}`，写进 manifest 与 stdout 摘要。
- **stdout** 打印一行摘要 JSON：`{ok, saved_images, failed_prompts, total_credits, by_mode, manifest, output_dir}`；`ok` 仅当没有任何 `errors[]`（每个内容都出了主图）才 true。
- **`--json-out <file>`**：把**整份 manifest + 顶层摘要（含 `ok`）**写到该文件，供后台 fire-and-forget 轮询判完成。
- **据此判定成败、拿全路径**（看摘要 `ok` / `manifest.json` 的 `errors[]`），别 `Get-ChildItem` 翻目录、别用退出码猜。后台异步跑时，**轮询 `--json-out` 文件存在且顶层 `ok:true`** 即全部内容就绪；单内容是否就绪看 `results[]` 里有无该 `name`。

## 13. 可观测性字段：.log / .heartbeat / status.py
（脱离/判活的操作心法见 `image-gen-notes.md` §8，这里只列字段 schema。）

### 任务日志 `<json-out>.log`
`_Tee` 把 `sys.stderr` 复制一份到日志文件（每行前缀 `[HH:MM:SS]`），所有进度都走 `sys.stderr.write` → 零改调用点即得完整任务日志。

### 心跳 `<json-out>.heartbeat`
守护线程 `_heartbeat_loop` 每 `HEARTBEAT_INTERVAL`（15s）覆写：
```json
{"ts":..., "elapsed_s":..., "alive":true, "name":"...", "n":2, "done":1, "inflight":1,
 "upstream_storm":false, "storm_5xx":0, "storm_no_progress_s":0, "last":"..."}
```
`atexit` 注册 `_final_heartbeat`：进程退出写一条 `alive:false, inflight:0` 终态。**`upstream_storm:true` = 上游网关 502 风暴**（应对 SOP 见 `image-gen-notes.md` §7）。
- **怎么用**：`--log-file <path>` 显式指定；**不给但给了 `--json-out` 时自动派生 `<json-out>.log` 与 `<json-out>.heartbeat`**。任何文件错误静默降级，绝不连累生图主流程。

### 状态聚合 `scripts/status.py`
扫某目录所有 `*.heartbeat`，按"`alive:false` 或 心跳 mtime 老化超 `--stale`（默认 45s）"判结束，聚合出 `{tasks_running, images_done, images_missing_running, concurrency_used, concurrency_limit, concurrency_free}` + 每任务明细。`uv run scripts/status.py output/fig`（`--json` 出机读）。
- `--concurrency-limit` 默认 **4**（app 单密钥在途上限）；走直连用 `8`。`concurrency_used` 是**同一密钥下所有活任务的共享在途之和**（§8），判并发占用一律以它为准。
- **别用 OS 进程数当任务数**：一个 `uv run gen_image.py ...` 在 Windows 上 = `uv.exe` + `python.exe` 两个 OS 进程，`Get-Process` 数出来是任务数 ×2。

## 14. 开发者登录与账号自检（/api/dev/*，仅账号管理，不生图、不计风控分）
用途：在怀疑"被封/限流/没额度"时，**不发生图请求**就能客观确认账号状态。凭据在 `secrets.env`（`MATSCA_DEV_EMAIL` / `MATSCA_DEV_PASSWORD`）。

**1) 登录拿 token（24h 有效，过期重登即可）**
```http
POST https://img.matsca.com/api/dev/login
Content-Type: application/json
{"email": "<MATSCA_DEV_EMAIL>", "password": "<MATSCA_DEV_PASSWORD>"}
```
返回：`token`(Bearer)、`token_type`、`expires_in`(=86400 秒/24h)、`wallet`(钱包余额)、`keys`[]、`high_res_enabled`、`must_change_password`、`unlimited_until`、`off_peak_start/off_peak_end`(错峰时段)。

**2) 查当前账号状态**
```http
GET https://img.matsca.com/api/dev/me
Authorization: Bearer <token>
```
关注字段：
- `keys[].enabled` —— **每把 key 是否被禁**（true=正常；false=被封/停用）。真正的处罚在这里看得到。
- `wallet` —— 余额（够不够）。
- `high_res_enabled` —— 是否有高清（>2048 边长）权限（普通用户通常 false）。
- `off_peak_start/off_peak_end` —— 错峰时段（None=未设）。

**3) 揭示某把 key 的明文**（需要时）
```http
GET https://img.matsca.com/api/dev/keys/{key_id}/reveal
Authorization: Bearer <token>
```
账号无 Key 时 `POST /api/dev/keys` 创建（Turnstile 开启时改去 `https://img.matsca.com/redeem` 浏览器内创建）。

**判读口径**：`enabled=true` + `wallet>0` ⇒ 账号本身没被处罚；此时若生图仍报"并发请求过多 / read timeout"，那是**上游共享账号池拥堵/变慢**（容量类，不计你的风控分），不是你被封。上游池实时占用**无公开接口**，只能问服务商。

> ⚠️ `expires_in=86400`：token 只活 24h，不是"永久"。永久凭据 = `secrets.env` 里的邮箱+密码，任何工具用它重登即可拿新 token。
