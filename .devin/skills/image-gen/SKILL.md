---
name: image-gen
description: 用矩岩(matsca)生图 API 生成或编辑图片。只要用户提到生图/出图/画图/插图/配图/做个图标或 logo/文生图/改图/图生图等任何"要一张图"的意图，就用本技能（不要自己另找别的生图方式）。
---

# 生图技能（matsca / gpt-image-2）

> ⛔ **红线（经服务商后端代码证实，命中即被风控拦截/临时封 key）**：**绝不探活、绝不调用 `/v1/ping`、绝不发 "hi"/"test"/"ping" 等无意义短文本测连通**。服务端有实时拦截器，会把"无生图意图的琐碎请求"判为异常并临时封禁。要用就**直接发真实生图请求**。
>
> 📖 本文件讲"怎么把图生好"（触发、默认、命令、模式、参数、日常多图编排）。**所有网络节奏由脚本的确定性代码决定，模型只负责填提示词和参数**。完整 API 字段、错误码清单、并发数字、凭证、机读产物/可观测性 schema 等**事实型规格**见 → `references/matsca-api.md`（随技能加载）；风控认知、调度设计哲学、踩坑/排错心法等**沉淀知识**见 → `references/image-gen-notes.md`（出 bug 或要深挖才翻）。

## 何时使用
- 用户要求"生成/画/做一张图""配图/插图/出图""文生图""改图/图生图"等。

## ⭐ 默认与推断规则（日常只说"生个图"时照这个做）
用户平常可能只说一句"生成一张…的图"，**不会说多大、几张、什么模式**。那就按下表自动选默认，**不要反问**；用户临时加一句就覆盖对应项。

| 维度 | 用户没说时的默认 | 怎么推断 / 用户说什么能改 |
|---|---|---|
| 数量 `-n` | **2**（第1张主图 + 1 张备选，自动归档） | "就一张/快速试试"→`-n 1`；"重要/封面/多给几张挑"→`-n 3`～4 |
| 质量 `--quality` | **high**（同价，不额外花钱） | 几乎不用改 |
| 尺寸 `--size` | **1536x864（16:9 横图）** | "竖图/海报/手机/屏"→`1024x1536`；"正方/图标/头像"→`1024x1024`；"要大图/高清"→`1920x1080`（最长边 >2048 需高清权限） |
| 输出目录 `--outdir` | **`output/fig/`**（没该目录就建） | 用户说别的路径就用 `-o` 指定完整文件路径 |
| 文件名 | `--name "<描述>"` → `<描述>_<今天YYYYMMDD>.png`；备选 `<描述>（备1）_<日期>.png` | `<描述>` 你根据提示词起个简短中文名；落盘前自动清洗（`/ \ : * ? " < > \|` 等非法字符→`-`、过长截断 80、空名哈希兜底），不会因特殊字符炸盘 |
| 模式 `--mode` | **`auto`→有 App 凭证走 app（×1，最便宜，默认）** | "用直连/原生"→`--mode direct`（×2）/`--mode native`（×3）；见「调用模式」 |
| 并发 `--concurrency` | **按模式自适应**（app→3 / direct→4 / native→3） | 一般不用动；调高易触发账户级风控限流，见「并发预算」 |

**用户每次最少只需说：生成…（内容）的图。** 只有"图的内容/提示词"是必须的；其余都有默认。提示词太粗时你可适当补充风格描述（如"扁平、简洁、白底"），但不要改变用户意图。

**默认命令（用户什么都没说时，你根据提示词起个 `--name`）：**
```bash
uv run "D:\700_Resources\720_Agents\skills\image-gen\scripts\gen_image.py" "<提示词>" --name "<简短描述>" -n 2
```
产出（今天举例 20260626）：`output/fig/<描述>_20260626.png`（主图）+ `output/fig/<描述>（备1）_20260626.png`（备选）+ `output/fig/_index.md`（清单）。用户指定路径时改用 `-o <完整路径>`（备选会在同目录、名为 `<名>（备1）.png`）。

**跑完要做的事：** 把脚本打印的"主图路径 + 备选路径 + 清单路径"**原样告诉用户**（特别是备选存哪了），方便他要换图时去挑。

**机读产物（不要再 `ls` 磁盘猜成败）：** 脚本结束会在**输出目录写 `manifest.json`**（服务商 matscaimg 风格：`{created_at, results[], errors[], total_credits, by_mode}`；每条 result 在服务商 `saved[]`=`[{path,bytes,role,mode}]` 基础上叠我们的 `primary/alts`、`n_requested/n_got`、`job_index`、`single`、`mode`(主图实际走的模式)，每条 error=`{job_index,name,prompt,error}`；`total_credits`=本次总积分、`by_mode`={mode:{images,credits}} 计费透明），并在 stdout 打印一行摘要 JSON `{ok, saved_images, failed_prompts, total_credits, by_mode, manifest, output_dir}`。`--json-out <file>` 则把整份 manifest+摘要写到该文件（后台轮询判完成用）。**判定成败只看这个**（顶层 `ok` / `errors[]`），别去 `ls` 磁盘猜。退出码语义：**主图已落盘 = 成功(exit 0)**，哪怕某张备图失败、或批量里某内容缺主图（缺哪个见 `manifest.json` 的 `errors[]`）；只有"一张主图都没出"才非 0。所以"某张备图失败"是正常的、不该当作整次失败去重跑。

## 内核怎么处理失败（照搬服务商，别手动重试）
脚本已内置服务商的错误分类与退避，**你不需要、也不应该手动重发**：

- **服务端/容量类瞬时错误 → 自动退避重试**：HTTP `408/409/425/429/500/502/503/504/520/522/524`，以及 `upstream_*`、`api_agent_queue_full`、`no_available_account`、`account_concurrency_exhausted` 等。这些**不算在你头上**，脚本优先尊重 `Retry-After`、否则指数退避带 jitter（≤60s），最多 `--retries`（默认 5）次。
- **客户侧错误 → 一律不重试**（重试只会累加风控错误率、甚至加重封禁）：`content_policy_violation`（内容违规）、`invalid_request`（参数错）、`trivial_intercept`（被判琐碎请求）、`api_key_temporarily_banned`（key 临时封禁）、余额不足、无高清权限、鉴权失败。遇到这些**停下来按提示处理/告诉用户**，别重发。

## 调用模式（`--mode {auto,app,direct,native}`）
**默认 `auto`：有 App 凭证走 app（×1 最便宜），否则走 direct（×2）。** 三种模式**现在都走同步端点** `/v1/images/generations`，只在"带不带 App 头 / 取 b64 还是 url"上区分。

| `--mode` | 倍率 | 端点 / 策略 | 适用 |
|---|---|---|---|
| `app`（默认） | ×1 | 同步 `/v1/images/generations`，取 `b64_json` + App 三 Header（`X-App-ID/X-App-Secret`） | 日常/生产，最便宜 |
| `direct` | ×2 | 同步 `/v1/images/generations`，取 `b64_json`（仅需 Key） | 没配 App 凭证时 |
| `native` | ×3 | 同步 `response_format=url` 取链接再下载，环境不通时挂 `--proxy` | 要走官方直连下载时 |

> App 头大小写敏感（`X-App-ID`，不是 `X-App-Id`）；脚本用 `http.client` 原样发送，仅在 `--mode app` 时携带。
>
> **取图与下载（照搬服务商 `download_url`/`generate_one`）**：取图**不按 mode 钉死格式**，而是逐张看返回里实际有什么——有 `b64_json` 就解码、否则下载 `url`（服务端拥堵/降级时即使 native 也可能回 b64，这样不会误判“无结果返回”）。`url` 下载时：① 带 `User-Agent`（避免被 CDN/WAF 用默认 urllib UA 拦成 403）；② 识别内联 `data:` URI 直接 base64 解码；③ 下载超时跟随 `--timeout`（默认 600s）。

```bash
# 默认 auto（有 App 凭证即 app ×1）
uv run scripts/gen_image.py "<提示词>" --name "<描述>" -n 2
# 显式直连 ×2
uv run scripts/gen_image.py "<提示词>" --mode direct -o out.png
# 显式原生 ×3（环境不通官方图片资源时加 --proxy，或设环境 MATSCA_PROXY）
uv run scripts/gen_image.py "<提示词>" --mode native --proxy http://127.0.0.1:7890 -o out.png
```

## ⭐ 并发预算（重要，旧版在这里踩了大坑）
服务商按 **单密钥在途上限（per-key in-flight）** 限流，**普通用户**：

| 模式 | 单密钥在途硬顶 |
|---|---|
| **app（你常用）** | **4** |
| direct | 8 |

- **这个额度由"同一密钥下所有同时在跑的 `gen_image.py` 进程"共享**，不是每个进程各有一份。
- **单次调用的默认并发**：app→3 / direct→4 / native→3（`--concurrency 0` 即自适应）。app 取 3 是在硬顶 4 之下留 1 个余量。日常 `-n 2` 时实际并发 = `min(并发, 2) = 2`，对默认零影响；只有 `-n≥3` 的单次调用才用到更高并发。
- **多内容编排时，所有在跑任务的在途之和必须 ≤ 硬顶**（app=4）。用 `scripts/status.py` 看实时占用：`uv run scripts/status.py output/fig`（默认按 app 的 4 算剩余；走直连加 `--concurrency-limit 8`）。

## ⭐ 异步工作流：发了图就去干主业，别 idle 干等
生图是**长任务**（几十秒~几分钟/张）。**别发完请求就杵在那儿等**。节奏：**发 → 去干别的 → 回来收**。
1. **发**：带 `--json-out <file>` 把生图丢到后台，请求一发出就**立刻回去做主业**（写 HTML、改代码、整理文案…）。
   - **经 Devin 隧道 `/api/exec` 时**：隧道有超时会杀长任务，必须用 **`Start-Process -WindowStyle Hidden`** 真脱离（别用 `-NoNewWindow`/redirect，会被父进程一起杀）。做法见 `references/image-gen-notes.md` §7。
   - **可观测性**：带 `--json-out` 即自动派生 `<json-out>.log`（带时间戳进度）+ `<json-out>.heartbeat`（每 ~15s 刷新的心跳），脱离后靠这俩看状态/判活。
2. **干**：图在跑的这段时间把不依赖图片的活儿全干完。
3. **收**：过一阵回来**轮询 `--json-out` 文件**（存在且 `ok:true` 即就绪），按 Coverage-First 主图齐了就交付，备图后台继续。
   - **迟迟没 `ok` 先判活**：看同名 `.heartbeat` 的 `ts`/mtime——还在动 = 进程活着（多半在退避重试，看 `.log` 证实）就继续等；mtime 不动了 = 进程已死/被杀，才重起该任务。
   - **⚠️ 轮询时必看 `.heartbeat` 的 `upstream_storm` 字段**：为 `true` = 上游网关 502 风暴（详见下面「502 风暴 SOP」）——**别闷头等到全 `--retries` 跑完才出声**，第一时间非阻塞提醒用户。
   - **一眼看全局用 `scripts/status.py`**：`uv run scripts/status.py output/fig`（加 `--json` 出机读）扫该目录所有 `.heartbeat`，聚合打印「几个在跑 / 已出几张、还差几张 / 占几路并发、还剩几路」。**判任务数/并发占用一律以它为准，别去数 OS 进程**（一个 `uv run` 任务 = uv.exe + python.exe 两进程，会把 N 误读成 2N）。

## ⭐ 多图/多阶段交付编排（Coverage-First）
深度任务常一次要 N 个不同内容的图，**默认每内容主图+1 备份**。原则：

- **先到为主**：同内容同提示词发多份并发，**先生成好的 = 主图（无后缀干净命名），后到的依次降级 `（备1）/（备2）`**；不预先钉死主/备。脚本单次调用内部已是这个逻辑。
- **⭐ 覆盖优先（核心）**：空闲并发**永远优先给"还没有任何图的内容"**，不是给已出图内容补备份。优先级：缺图内容 > 缺图内容的备份 > 已出图内容的备份。**绝不能让备份顶掉未覆盖内容的槽**。
- **交付门槛 = N 个内容各有 1 张主图**：一到齐立刻做 HTML（引用无后缀主图）**先交付**；备份后台继续，跑完更新 `output/fig/_index.md`。用户不满意某张就去翻对应 `（备1/备2）` 替换。

### ✅ 首选：一次批量调用（`--prompts-file`，单进程统一卡并发，最防撞墙）
**多内容请优先把它们写进一个文件，交给一次 `gen_image.py --prompts-file` 调用**，而不是起多个进程 `&`。这是**服务商内核防"撞墙"的本来做法**：所有内容的所有请求共用**同一个线程池**（`max_workers=并发上限`），**永不超过单密钥在途硬顶**（app=4），且脚本内部已自动做 Coverage-First（按轮次交错：每内容先各出主图，再回头补各内容备图）。多进程 `&` 互不知情、瞬时在途会叠加，正是上次放大撞墙的根因。

`prompts.json`（JSON 列表，每项 `name`/`prompt`，可选 `n`/`size`/`edit`/`mask`；省略则用命令行默认）：
```json
[
  {"name": "蛙卵", "prompt": "<提示词>", "n": 2},
  {"name": "蝌蚪", "prompt": "<提示词>", "n": 2},
  {"name": "成蛙", "prompt": "<提示词>", "n": 2}
]
```
```bash
uv run scripts/gen_image.py --prompts-file prompts.json --outdir output/fig --json-out output/fig/_batch.json
# 这一次调用会一直跑到出齐才返回，但内部并行（不慢）；要后台脱离照样可配 --json-out（自动派生 .log/.heartbeat）
```
出齐后输出目录的 `manifest.json` = `{created_at, results:[{name,job_index,ok,primary,alts,saved,n_requested,n_got,...}], errors:[{job_index,name,prompt,error}]}`；stdout/`--json-out` 给 `{ok,saved_images,failed_prompts,manifest,output_dir}` 摘要。每内容主图无后缀、备图 `（备N）`，另合并写一份人读 `_index.md`（挑备图用，与机读 manifest.json 互不影响）。

### 次选：多进程 fire-and-forget（只在"必须边出边干主业、且单次批量不便"时）
要让进程立刻脱离、你回去干主业时才用多进程；此时**必须自己卡并发**（批量模式那一层保护没了）：

- **⚠️ 守住并发预算（app=4）**：**别把 N 个内容一次性 `&` 全炸出去**。同一密钥所有在跑进程的在途之和要 ≤ 4（app）。实操：app 模式下**一次最多让 1～2 个内容任务在跑**（单个 `-n 2` 任务就占 2 路）；跨内容之间**错峰 ~3s** 再起下一个，并用 `status.py` 确认 `concurrency_free` 够了再补。
  ```bash
  for c in "蛙卵" "蝌蚪" "成蛙"; do
    uv run scripts/gen_image.py "<对应提示词>" --name "$c" -n 2 --json-out "output/fig/_res_$c.json" &
    sleep 3                       # 跨内容错峰，并用 status.py 守住 app=4 的在途预算
  done
  # 立刻回去干主业；过会儿轮询各 _res_*.json，ok:true 即就绪 → 交付；备图后台续跑
  ```
- **用户喊停 → 撤回/丢弃**：停掉编排器和在途 `gen` 子进程（**绝不碰隧道文件服务进程**），简短回报并更新 `_index.md`。

## ⭐ 双模式 / 赛马开关 / 事件驱动调度（进阶，大批量或撞墙高峰用）
这三条是**正交的轴**，可自由组合，平时不用动；批量大、要更快或在拥堵高峰才开。

### 轴 A：双模式 `--modes app,direct`（扩你自己的在途天花板）
app / direct 是**两把不同的 key，各有独立在途池**（app≤3 / direct≤4 / native≤3，各自永不超单密钥硬顶）。同时用 → 总槽位 ≈ 3+4=7，大批量不再被 app 的 4 槽卡死，且流量摊到两把 key 各自风控分更低。
- ⚠️ **只扩你自己的槽，救不了上游池拥堵**：app/direct 最终都派发到服务商**同一个上游账号池**，池满时两把 key 照样撞 `account_concurrency_exhausted`。双模式治的是"你自己槽不够"，不是"上游高峰堵"。
- **分配策略 `--dispatch`**：`cost`（默认）=app 主、direct 溢出（绝大多数图仍 1 积分、最省）；`throughput`=两池拉满、最快（direct 2 积分用得多）。
- **逐项钉死**：批量项写 `"mode":"direct"` 把某内容钉死走某把 key（赛马时主图/备图甚至能分走不同 key）。不给 `--modes` 则退回 `--mode` 单模式（向后兼容）。
- 💰 direct 每张 2 积分、app 1 积分；默认仍单模式（app 最省），双模式是显式 opt-in，只在赶大批量时开。

### 轴 B：赛马开关 `--single-call`（一个内容内怎么发 N 张）
- **默认（赛马，不给此开关）**：`-n2` 拆成 **2 个并行 `n=1`** → 先到为主、早交付（主图先到先用）、抗单点失败，但占 **2 个在途槽**。
- **`--single-call`（单请求）**：`-n2` 发 **1 个 `n=N`** → 只占 **1 个槽、1 个 HTTP 请求**、对自己的槽更省，但全有或全无、无早交付。批量项写 `"single":true/false` 逐项覆盖。
- ⚠️ 实测：单请求 `n=N` 在上游池仍占 **~N 个并发槽**（不是 1 个）——省的是"你自己 key 的槽"和"HTTP 请求数"，**不是上游池并发**。单请求安全上限约 `n=4`（n≥6 易 502/超时）。

### 轴 C：事件驱动 Coverage-First 调度（自动，无需开关）
多内容编排已升级为**事件驱动**（替代旧的静态轮次）：每完成一个请求就按当前状态重新评估、派发下一个，三级优先：
1. **开局抢覆盖**：每个还没主图的内容先各发 1 个 `n=1`（最快凑齐"每种图各一张"交付 HTML）。
2. **顽固图多路赛马**：某内容主图在飞超 ~50s 仍没出（stall）→ 给它再加一路并行 `n=1` 砸覆盖（每内容最多加 1 路）。
3. **主图全齐才补备图**：所有内容都拿到主图后，才发备图（赛马 `n=1`）。绝不让备图顶掉未覆盖内容的槽。

### 轴 D：自动降级（池满 → 降 N 到 1）
连续撞 **3 次** `account_concurrency_exhausted`（上游池满）→ 后续请求一律降到 `n=1`（每请求只抢 1 个上游槽，最容易挤进只剩 1 个空位的池）。成功一次即清零计数。这是"上游高峰几乎打满"时凑齐覆盖的逃生口。

```bash
# 双模式 + 默认赛马 + 成本优先（app 主 direct 溢出），事件驱动+自动降级全自动
uv run scripts/gen_image.py --prompts-file prompts.json --modes app,direct --outdir output/fig --json-out output/fig/_batch.json
# 双模式 + 单请求省槽 + 吞吐拉满（赶大批量最快）
uv run scripts/gen_image.py --prompts-file prompts.json --modes app,direct --dispatch throughput --single-call --outdir output/fig
```

## ⭐ 遇到 502 / 上游网关风暴怎么办（SOP）
**502/503/504 是什么：** 服务商**上游网关层**（nginx）回的 5xx，是网关/容量类故障。它 **≠ `account_concurrency_exhausted`**（那是上游账号池满），**更 ≠ 你被风控**。三者都“可重试、不计风控分”，但网关风暴**症状更重、等多久不可预测**，所以要**早提醒 + 主动建议换渠道**，不要让用户傻等。

**工具怎么侦测（更迟钝、撞够久才报）：** 脚本自动累计网关 5xx，同时满足“仍在撞 5xx（近 90s 内）” + （累计 ≥ 12 次 **或** ≥150s 零新图）才判为风暴 → 在 `.heartbeat` 写 `upstream_storm:true`（还有 `storm_5xx`/`storm_no_progress_s`）+ 在 stderr/`.log` 打一条醒目 `>>> UPSTREAM_502_STORM … <<<` 标记（只打一次）。**阈值故意调迟钝**，避免偏头阈乱报。

**Agent 必须做（约束你自己）：** 后台跑批时**轮询 `.heartbeat`**；一旦 `upstream_storm:true` → **立刻非阻塞 `message_user`**，说清四件事：
1. 这是**上游网关 502 风暴，不是你被风控**、也不是池满；
2. 工具正在**有界退避自愈**（不硬刷、不抬风控分）；
3. **主动建议：你可同时去找别的渠道 / 晚点再来**，别傻等；
4. 附**已覆盖 / 缺图清单**（哪几个出了、哪几个还缺）让用户决断。

> **禁止**闷头等到全 `--retries` 跑完才出声。已出的图不会丢，但该提醒就提醒。

**可选熔断 `--storm-give-up-after <秒>`（默认关、opt-in）：** 不给 = 一直有界退避重试（默认）。给了 = 上游风暴**持续超过该秒数就提前收尾**：停止重试、返回已出的图 + 缺图清单（`manifest.json` 的 `errors[]`、顶层 `upstream_storm`/`gave_up`），不再耗尽 `--retries` 预算。适合“赶时间、宁可拿部分图先交付”的场景。
```bash
# 上游风暴持续 >120s 就提前收尾，返回已出的图 + 缺图清单（不再傻等）
uv run scripts/gen_image.py --prompts-file prompts.json --modes app,direct --storm-give-up-after 120 --outdir output/fig --json-out output/fig/_batch.json
```

## ⭐ 改图 / 图生图（`--edit` / `--mask`，走 `/v1/images/edits`）
用户说"把这张图改成…/换背景/加个…/基于这张图…"时，用 `--edit` 传原图，脚本走 `/v1/images/edits`（multipart），把 `prompt` 应用到该图：
```bash
# 整图改写（把 prompt 应用到原图）
uv run scripts/gen_image.py "把背景换成星空" --edit input.png --name 星空版 -o out.png
# 局部重绘：--mask 蒙版（白色区域=要重绘的区域；其余保留）
uv run scripts/gen_image.py "把这里换成一只猫" --edit input.png --mask mask.png --name 加猫 -o out.png
```
- 改图保真度用 `--input-fidelity high`（更贴近原图）/`low`。批量改图：在 `prompts.json` 每项写 `"edit": "原图路径"`（可选 `"mask"`）。
- 主备/Coverage-First/退避重试逻辑与文生图完全一致（`-n 2` 即主图+备1）。

### 图生图 / 变体（`--variation`，走 `/v1/images/variations`）
用户说"给这张图来几个变体/换个版本/类似风格再来几张"时，用 `--variation` 传原图，脚本走 `/v1/images/variations`（multipart）。**变体不需要 prompt**（有就带上、没有也行），生成该图的不同版本：
```bash
# 给原图生成 2 个变体（主图+备1）
uv run scripts/gen_image.py --variation input.png --name 变体 -n 2 --outdir output/fig
```
批量变体：在 `prompts.json` 每项写 `"variation": "原图路径"`（可不带 `prompt`）。主备/Coverage-First/退避重试与文生图一致。

## 常用参数
- `--model` 默认 `gpt-image-2`（别名 gpt-image-1/dall-e-3 等上游会归一到它）。
- `--size` `宽x高`（64~8192）；默认 `1536x864`（16:9）；竖图 `1024x1536`、方图 `1024x1024`。最长边 >2048 需高清权限。上游会按档位归一化（请求 `1536x864` 实得略大属正常）。
- `--name` 图片描述（驱动默认命名 `<描述>_<日期>.png`）；`--outdir` 默认输出目录（默认 `output/fig`）；`--date` 日期后缀（默认今天）。给了 `-o` 则优先用显式完整路径。
- `--quality` `auto/low/medium/high`（默认 high；同价）。
- `--moderation` `auto/low`（默认 **low**，放宽审查减少误拦——内容安全拦截是唯一不退款且可能追加罚金的情况，且**绝不重试**）。
- `-n` 张数（默认 2）；`--concurrency` 并发（默认 0=按模式自适应，见「并发预算」）；`--retries` 单张最大重试次数（默认 5）；`--timeout` 单次请求墙钟上限（秒，默认 600）；`--proxy` 原生模式下载代理。
- **批量**：`--prompts-file <file>` 一次跑多内容（JSON 列表或纯文本每行一个 prompt），单进程统一卡并发（见「多图编排」）。
- **双模式/赛马**（见「双模式 / 赛马开关 / 事件驱动调度」）：`--modes app,direct`（多模式各自独立在途池，批量项可写 `"mode":"direct"` 钉死）；`--dispatch cost/throughput`（成本优先/吞吐优先，默认 cost）；`--single-call`（单请求 `n=N` 省槽，默认赛马 `N×n=1`，批量项可写 `"single":true/false`）。撞上游池满连续 3 次自动降 N→1。
- **改图**：`--edit <原图>` 走 `/v1/images/edits`；`--mask <蒙版>` 局部重绘（白色区域）；`--input-fidelity low/high` 对原图保真度（见「改图」）。
- **图生图/变体**：`--variation <原图>` 走 `/v1/images/variations`（无需 prompt，生成原图变体）；批量项写 `"variation":"原图路径"`（见「图生图 / 变体」）。
- **可选透传参数（不给则不发，用服务端默认）**：`--background auto/transparent/opaque`（透明底图标/logo）、`--style vivid/natural`、`--output-format png/jpeg/webp`、`--output-compression 0~100`（jpeg/webp）。
- `--json-out <file>` 机读结果；给了它即**自动派生 `<file>.log` / `<file>.heartbeat`**；也可 `--log-file <file>` 显式指定日志路径。

## 排错速查（错误码详表见 `references/matsca-api.md` §6/§7；排错心法/根因见 `references/image-gen-notes.md`）
- `401` → Key 无效/过期，查对应模式的 Key（app=`MATSCA_APP_KEY`、direct=`MATSCA_DIRECT_KEY`、native=`MATSCA_NATIVE_KEY`）。
- `403` → 凭证缺失（app 模式还要 `MATSCA_APP_ID/MATSCA_APP_SECRET`，且 `X-App-ID` 大小写敏感）。
- `429` → **看报文分流**：
  - "**请求过于频繁/rate/too many**" = 突发频率/并发超限。脚本会自动短退避重试，**通常无需干预**；若频繁，说明同密钥在跑的任务太多 → 用 `status.py` 看占用，减少同时在跑的内容数（守住 app=4）。
  - "**积分不足/insufficient**" = 真没钱了 → **停下来告诉用户充值**，重试无意义。
- `5xx` / `upstream_*` / `account_concurrency_exhausted` → 服务端/容量类瞬时错误，脚本自动退避重试，**不计你的风控分**，耐心等即可，**绝不用 ping 探活**。
- **持续 `502/503/504`（上游网关风暴）** → heartbeat 会写 `upstream_storm:true`、日志打 `>>> UPSTREAM_502_STORM <<<`。这是上游网关层挂了，**不是你被风控、也不是池满**。按上面「502 风暴 SOP」：第一时间非阻塞提醒用户 + 主动建议换渠道；要提前止损用 `--storm-give-up-after <秒>`（默认关）。
- `content_policy_violation` / `high_res_not_enabled` / `trivial_intercept` / `api_key_temporarily_banned` → **客户侧错误，脚本不重试**，按提示处理或告诉用户。
- 中文文件名/日志显示乱码 → 多半是终端（PTY）回显问题，磁盘上的实际文件名是对的；用读文件工具核实。

## 脚本与凭证
- 脚本：`scripts/gen_image.py`（仅标准库，无第三方依赖；`uv run` 或 `python` 均可）。会把图存到目标路径并打印绝对路径。
  - 机读结果 `GEN_RESULT`/`--json-out`、退出码语义（主图落盘=exit 0、备图失败不影响）见上「机读结果」；后台运行/可观测性（`.log`/`.heartbeat`/`status.py`）见上「异步工作流」；并发模型见上「并发预算」。
- 凭证：明文集中在 `720_Agents\secrets.env`：`MATSCA_APP_KEY`（×1）+ `MATSCA_APP_ID` + `MATSCA_APP_SECRET`；`MATSCA_DIRECT_KEY`（×2）；`MATSCA_NATIVE_KEY`（×3）。脚本取 Key 顺序：① 进程环境变量 → ② `secrets.env`。新增/换 Key 只改 `secrets.env` 一处。
