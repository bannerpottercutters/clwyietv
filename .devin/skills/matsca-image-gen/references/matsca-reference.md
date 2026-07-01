# Matsca 参考手册

## 目录

- [API](#api)
- [CLI 参数全集](#cli-参数全集gen_imagepy)
- [错误码与常量](#错误码与常量)
- [manifest.json schema](#manifestjson-schema)
- [脚本职责](#脚本职责)
- [经验教训](#经验教训)
- [开发须知](#开发须知)

## API

Base：`https://img.matsca.com`（环境变量 `MATSCA_API_BASE` 可覆盖）。鉴权：`Authorization: Bearer <key>`。

| 端点 | 方法 | 用途 | 请求体 |
|---|---|---|---|
| `/v1/ping` | GET | 健康检查（返回 `auth.banned` / `ban_remaining_seconds`） | — |
| `/v1/images/generations` | POST | 文生图 | JSON `{model,prompt,n,size,response_format}` |
| `/v1/images/edits` | POST | 改图（可带 mask） | multipart：`image`(+`mask`) + fields |
| `/v1/images/variations` | POST | 图生图/变体（可无 prompt） | multipart：`image` + fields |
| `/api/dev/login` | POST | dev 登录拿 token | `{email,password}` |
| `/api/dev/me` | GET | 列名下 Key | — |
| `/api/dev/keys/{id}/relay` | POST | 开启 relay 模式 | `{}` |
| `/api/dev/keys/{id}/reveal` | GET | 取 Key 明文 | — |

响应取图：`data[].b64_json` 优先，否则下载 `data[].url`（含 `data:` URI）。扩展名按 magic bytes 判定（PNG/JPG/WEBP），判不出回退 hint→png。

## CLI 参数全集（gen_image.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| `prompt`（位置） | — | 单内容提示词 |
| `--prompts-file` | — | 批量：JSON 列表 `[{name,prompt,n?,size?,edit?,mask?,variation?,aspect?,model?}]` 或每行一 prompt |
| `--name` | `image` | 单内容文件名 |
| `--outdir` | `output/fig` | 输出目录（含 manifest.json） |
| `--model` | `gpt-image-2` | 模型 |
| `--size` | `auto` | 尺寸 |
| `--aspect` | `auto` | 比例；`size=auto` 时写进提示词 |
| `-n` | `2` | 每内容张数，钳制 1~4（注意是 `-n`，不能写 `--n`） |
| `--edit` / `--mask` | — | 改图原图 / 蒙版（mask 必须配 edit） |
| `--variation` | — | 变体原图 |
| `--timeout` | `600` | 单请求墙钟上限（秒） |
| `--proxy` | — | 下载 url 图片用代理 |
| `--no-race` | 开 | 关闭赛马（默认开） |
| `--no-coverage-first` | 开 | 关闭覆盖优先（默认开） |
| `--block-after` | `90` | 受阻判定零进展阈值（秒） |
| `--give-up-after` | `0`(关) | 受阻持续多久后提前收尾（秒） |
| `--no-ping-refine` | 开 | 关闭受阻时 ping 辨别封禁（默认开） |
| `--no-date` | 加 | 文件名不加 `_YYYYMMDD`（默认加日期） |
| `--quality/--moderation/--background/--style/--output-format/--output-compression/--input-fidelity` | 不发 | 透传参数白名单 |
| `--keys` | — | 逗号分隔裸 Key |
| `--secrets-file` | — | secrets.env 路径 |
| `--dev-token/--email/--password` | — | dev 账号 |
| `--no-save-keys` | 回写 | 关闭新鲜 Key 回写 secrets（默认回写） |
| `--save-keys-file` | — | 指定回写目标文件 |
| `--preflight-ping` | off | 开跑前对每 Key ping |
| `--ping-only` | off | 只验活所有 Key 后退出 |
| `--json-out` | — | 额外把 manifest+summary 落一份 |
| `--resume` | off | 断点续跑（读旧 manifest 跳过已完成） |

退出码：`0` ok；`3` 有失败 / 部分；`4` 零产出。

### 凭据解析优先级

`resolve_keys` 按以下顺序尝试获取凭据：

1. `--keys "k1,k2"` — 命令行裸传
2. 环境变量 `MATSCA_API_KEYS` — Devin 云端 agent 设此变量
3. `--secrets-file <path>` — 显式指定文件
4. `MATSCA_SECRETS_FILE` 环境变量指向的文件
5. `./secrets.env` — 当前工作目录
6. `~/720_Agents/secrets.env` — 用户 home 约定路径
7. `~/secrets.env` — 用户 home 备选

从 secrets 文件读两类内容：`MATSCA_API_KEYS=`（API Key）和 `MATSCA_DEV_EMAIL=` + `MATSCA_DEV_PASSWORD=`（dev 账号）。dev 账号也可通过环境变量 `MATSCA_DEV_TOKEN` / `MATSCA_DEV_EMAIL` / `MATSCA_DEV_PASSWORD` 传入。

有 API Key + dev 账号时：先 ping 验活，全失效才走 dev 登录 reveal。只有 dev 账号时：直接 reveal。reveal 成功后回写本地 secrets 文件（`--no-save-keys` 可关）；无本地文件可回写时（凭据来源是环境变量），新 Key 输出到 summary JSON 的 `refreshed_keys` 字段。

## 错误码与常量

### 错误码分类

```python
RETRYABLE_HTTP_STATUSES = {408,409,425,429,500,502,503,504,520,522,524}
TRANSIENT_ERROR_CODES   = upstream_timeout/unreachable/session_pool_exhausted/
                          api_agent_queue_full/upstream_rate_limited/no_available_account/
                          account_concurrency_exhausted/upstream_direct_unavailable/
                          upstream_server_error/upstream_error/empty_response
KEY_FAILOVER_ERROR_CODES= account_token_invalid / image_permission_unavailable（+ 401）→ 立即转移到别的 Key
COOLDOWN_ERROR_CODES    = rate_limited/concurrency_exhausted/queue_full/session_pool_exhausted/no_available_account
COOLDOWN_STATUSES       = {401,429,503}
```

不在 transient/key_specific 集合里的（如 `content_policy_violation`）= 客户侧错误，不重试直接失败。

### 常量全集

| 常量 | 值 | 含义 |
|---|---|---|
| QUEUE_CONCURRENCY_PER_KEY | 2 | 每 Key running 任务数 |
| IMAGE_REQUEST_CONCURRENCY_PER_KEY | 2 | 每 Key 在飞请求 |
| GLOBAL_IMAGE_REQUEST_CONCURRENCY | 6 | 全局在飞请求 |
| MAX_TRANSIENT_TASK_RETRIES | 4 | 任务层最大重试 |
| TASK_RETRY_BASE_MS / MAX_MS | 5000 / 120000 | 任务重试退避基数/封顶 |
| KEY_COOLDOWN_BASE_MS / MAX_MS | 6000 / 120000 | 逐 Key 冷却退避基数/封顶 |
| HTTP_RETRIES_IMAGE | 2 | 生图 HTTP 层重试 |
| RETRY_AFTER_CAP_MS | 120000 | Retry-After 尊重上限 |
| SCHEDULER_TICK_S | 1.0 | 调度节拍 |
| IMAGE_TIMEOUT_S | 600 | 单次生图墙钟上限 |
| PING_TIMEOUT_S | 8 | ping 超时 |
| BLOCK_NO_PROGRESS_MS | 90000 | 受阻零进展阈值 |
| CAP_WINDOW_MS | 120000 | 容量类错误统计窗口 |

## manifest.json schema

```jsonc
{
  "created_at": "ISO8601",
  "results": [   // 任何已落盘 ≥1 张的内容都在这（哪怕部分成功）
    {"name","index","prompt","ok","complete","key_id","size",
     "n_requested","n_got","saved":[{"path","bytes","format","role"}],
     "status"?,"note"?,"error"?}   // 后三者仅 complete=false 时出现
  ],
  "errors":  [ {"name","index","prompt","error","key_id"} ],  // 0 张且 failed
  "pending": [ {"name","index","prompt","status","retry_count","n_requested","n_got":0,"saved":[]} ],
  "ok": bool,             // 无 errors、无 pending、无 terminal-but-incomplete
  "saved_images": int, "completed": int, "failed": int, "pending_count": int,
  // 受阻状态（extra）
  "blocked": bool, "block_reason": "502_storm|429_congestion|key_banned|",
  "blocked_since": "ISO8601|null", "no_progress_seconds": int, "gave_up": bool
}
```

要点：`role` 为 `primary`（0=主图）/`backup`（备1、备2…）。`ok=true` 表示全部出齐。注意：单内容在飞、已出部分图但尚未终态时，因 `incomplete` 只统计「终态且未凑齐」的内容，`ok` 可能短暂为 `true`——轮询方应结合 `pending_count`/各 result 的 `complete` 字段判断是否仍在进行。

### 文件命名

`_safe_name(name)`（保留中文、非法字符→`-`、截断 80）+（默认）`_YYYYMMDD`；主图无后缀，备份加 `（备N）`。例：`橘猫_20260629.png`、`橘猫_20260629（备1）.png`。

## 脚本职责

- `gen_image.py`：主 CLI。HTTP 层 / 端点封装 / 凭据解析自愈 / Task / KeyHealth / Scheduler（赛马·覆盖优先·受阻侦测）/ manifest 读写。
- `make_preview.py`：等比缩放压 JPEG（Pillow→ImageMagick→报错）；也可被 import（`make_preview`/`target_size`）。
- `auto_deliver.py`：Devin 云端看护，轮询 manifest → 压预览 → 隧道 `/api/exec` 分片 base64 上传 → SHA256 校验 → 收尾。
- `offline_test.py`：monkeypatch 网络层的 35 个离线用例（Phase 0~6）。

## 经验教训

- 提吞吐唯一手段是多把 Key，不是调高单 Key 并发（账户级风控对猛刷敏感）。
- 几把直连 Key 常打同一上游池，「满池」时故障转移救不了 → 受阻侦测必须池级。
- 长任务别前台同步等：fire-and-forget + 轮询 manifest。
- 成败看 manifest 文件，不 ls 磁盘、不数进程。
- 失败要早响、可读：预检（mask 没 edit、重名、n 越界）、非 JSON 响应收敛成一行、退出码表态。
- 已落盘的图绝不丢：completed 终态不翻转，failed 可被晚到成功翻回。
- 凭据自愈：dev 账号兜底自动 reveal + 回写本地，不让用户手动更新 Key。
- CLI 前缀歧义：`argparse` 默认允许选项缩写，`-n` 与 `--name/--no-race` 等共存时写 `--n` 会报 ambiguous。已给 parser 设 `allow_abbrev=False` 彻底禁用缩写——必须写全 `--name`，张数只用 `-n`。
- 隧道建目录会间歇失败：经隧道 `/api/exec` 快速连发时，建子目录的 `New-Item` 会间歇丢失 / 截断，导致目标子目录（如 `scripts/`、`output/fig/`）根本没建出来，后续 `Add-Content` 因父目录不存在而整体失败。`auto_deliver.upload()` 已改为先 `ensure_remote_dir()`（建目录→`Test-Path` 坐实→失败重试≤6 次）再传文件，且每次 `exec_ps` 之间留 ~0.3s 间隔。
- 空 SHA 的语义：远端 `Get-FileHash` 返回空串＝整条远端命令失败（多半是 `.b64tmp` 缺失 / 目录不存在），不是内容损坏；排错先查目录 / 临时文件，别从 base64 分块方向钻。上传逻辑已区分「空串=命令未落地」与「非空但不等=内容不符」两种日志。
- 本地预览中文名：用 `file:///` 在 Chrome 地址栏打开含中文名的 html，地址栏会把中文段吞掉导致 404；本地自查渲染时先复制一份 ASCII 名打开即可（交付给用户的中文名文件不受影响）。

## 开发须知

- secrets.env 只在本地，绝不随技能同步上云、绝不进 git。
- manifest 是所有轮询方的契约，重构时 schema 不可破坏性变更。
- 改完任何调度/重试/manifest 逻辑后跑离线自测：`python scripts/offline_test.py`（monkeypatch 网络层，35 个用例，不联网）。
