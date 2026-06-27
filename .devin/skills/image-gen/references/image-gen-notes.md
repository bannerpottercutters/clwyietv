# 生图认知与排错心法（matsca / gpt-image-2）

> **沉淀知识，平时不进上下文，出 bug / 要深挖才整份翻。** 这里只放"**为什么 / 踩过的坑 / 心法**"——用法看 `../SKILL.md`，端点·参数·错误码·凭证·schema 等**事实型规格**看同目录 `matsca-api.md`。
>
> ⚠️ **本文件已按服务商官方 matscaimg 内核 + 风控后端代码重写。** 早期我们没拿到服务端规则，凭"等很久不出图"猜出过一整套机制（502=每分钟重启 / 在途作废 / 重试免费 / 异步幂等换 id 重发 / turbo 抢跑 / 主图 6 分钟加速 / 单 Key 并发 10），**这些猜测已被服务商真实代码证伪、整套删除**。脚本现以服务商规则为准。最后核对：2026-06。

## 目录
1. 红线根因：为何绝不探活
2. 服务商两道风控（你"效果差"的真实成因）
3. 为何删掉"异步幂等换 id 重发"那套
4. 并发认知：在途共享 + 防撞墙=单进程批量
5. 双模式/赛马/事件驱动调度/自动降级的设计哲学
6. 502 上游网关风暴 vs 账号池满 vs 风控——三者区分与提醒 SOP
7. 隧道长任务真脱离的坑 + 判活心法（仅 Devin）
8. 传输回传压缩经验

---

## 1. 红线根因：为何绝不探活
- **绝不调用 `/v1/ping`、绝不发 "hi"/"test"/"ping" 等无意义短文本测连通性。**
- **经服务商后端代码证实**：服务端有实时拦截器（`trivial_chat_interceptor.py`），会把"无生图意图的琐碎请求"判为异常，短窗内连续多次会**临时封 key + 邮件告警**。
- 要用就**直接发真实生图请求**。Key 是否有效从生图请求返回即可判断（`401`=Key 无效；`403`=应用模式凭证/Header 不对），无需任何预检。

## 2. 服务商两道风控（你"效果差"的真实成因）
服务端有两道**独立**风控（已读后端 `risk_control_service.py`/`trivial_chat_interceptor.py`/`risk_config.py`/`content_filter.py` 证实）：

**第一道 · 实时硬拦截（TrivialChatInterceptor，命中即拦/封 key）**
- 请求里带**编程代理/Codex 指纹**（`codex`/`coding agent`/`you are codex`/`claude code`/`cursor agent`/`cline`/`aider`…）→ 立即拦截，返回固定话术，**根本不进生图模型**。→ 教训：**绝不把宿主 Agent 的 system prompt/角色设定混进生图请求**；脚本只发纯生图参数，天然规避。
- 问候/过短/无意义文本（hi/test/ping/你好/嗯…）→ 拦截；5 分钟内连续 5 次且无生图意图 → 封 key 15 分钟。出现生图意图词（画/生成/draw/generate…）可清零放行。

**第二道 · 后台评分限流（每 60s 评分，按分动态压并发/速率）**
- **失败后盲目快重试** → 抬高频率/短窗错误率 → 飙分。→ 所以脚本对失败**只按服务商分类有节制地退避重试**（清单见 `matsca-api.md` §6）。
- 对**客户侧错误**（内容违规/参数错）重试 → 直接累加错误率进 warning/critical。→ 所以这类**一律不重试**。
- 调用频率阶梯加分（cpm>15/30/60）；并发过高加分。
- 🟢 **关键豁免**：服务端/容量类失败（429/5xx/排队/无账号/`account_concurrency_exhausted`）**不算在你头上**，只有你发错的才计分。
- 风控分一升高，**在途上限被动态砍低**（`risk_control_service.py:1302-1318`，按 `1-0.92*pressure` 比例压，elevated/warning/critical 顶 80/40/10）。→ **保持低分才保得住并发**：别盲重试、别发琐碎请求、别贴着硬顶跑。

## 3. 为何删掉"异步幂等换 id 重发"那套
早期 direct 走的"异步幂等 `/api/image-tasks` + `client_task_id` 换 id 重发"那套已删除——它建立在"502=每分钟重启、在途作废、重试免费"的**错误假设**上，正是把自己推向第二道按量限流的根因。现三模式都走同步 `/v1/images/generations`（事实见 `matsca-api.md` §3/§5）。

## 4. 并发认知：在途共享 + 防撞墙=单进程批量
- **⭐ 在途额度是"同一密钥下所有同时在跑的 `gen_image.py` 进程"共享的**——不是每个进程各有一份。早期误以为有 10 路独立并发、把 N 个后台进程一次性 `&` 出去 → 瞬时在途远超硬顶（app=4）→ 触发风控限流。**多进程编排时，所有在跑任务的在途之和必须 ≤ 硬顶。**（硬顶数字见 `matsca-api.md` §8。）
- **别调到硬顶**：要给重试突发留余量，且在途越高越容易把风控分推高反被动态降并发。
- **⭐ 防"撞墙"的根本做法 = 单进程批量（不是降并发）**：服务商内核**不预探空位**，它把一批 prompt 直接丢进**一个进程的线程池**（`ThreadPoolExecutor(max_workers=并发)`，请求间零间隔），靠"撞了就退避重试 + jitter 把重试时刻随机错开"自愈。关键在于**同一进程内并发被池子硬性卡死，永不超过单密钥在途硬顶**。我们脚本的 `--prompts-file` 就是这条：多内容一次性交给它，**进程内统一卡并发 + Coverage-First（按轮次交错，主图永远排在备图前）**。反例是上次把 3 个阶段当 3 个独立进程几乎同时 `&` 发——**进程间互不知情、瞬时在途叠加**，把撞墙概率自己抬上去。
- 所以：**能合批就合批（`--prompts-file`）；必须多进程时才严格错峰 + `status.py` 确认空位**。注意：`account_concurrency_exhausted`（上游账号池满）靠任何客户端技巧都变不出空位，只能退避等——jitter 只减少"反复同时撞"，不创造容量。

## 5. 双模式 / 赛马 / 事件驱动调度 / 自动降级的设计哲学
三条**正交的轴**，都只作用在"你自己的单密钥在途槽"，**救不了上游账号池拥堵**（app/direct 最终都派发到同一个共享上游池）。命令用法见 SKILL「双模式 / 赛马开关 / 事件驱动调度」。

- **轴 A 双模式 `--modes app,direct`**：每把 key 一个**独立在途池**（`Scheduler.caps`：app≤3 / direct≤4 / native≤3，`--concurrency>0` 覆盖、仍受硬顶约束），总线程=各池相加。每个请求单元下沉为 `(job, n, mode)`，header（是否带 App 三头）+ `response_format`（native→url、其余→b64_json）按**该单元的 mode 现算**。`--dispatch`：`cost`（默认，`min(MODE_COST)`=app 主、direct 溢出）/`throughput`（`max(空位, -cost)`=两池拉满）。批量项 `"mode":"direct"` 钉死某内容走哪把 key（None=调度器按策略自选）。不给 `--modes` 退回 `--mode` 单模式（向后兼容）。`MODE_COST={app:1,direct:2,native:3}`。
- **轴 B 赛马开关 `--single-call`**：默认赛马（`-nN`→N 个并行 `n=1`、先到为主、早交付、占 N 槽）；开启则单请求 `n=N`（占 1 槽、1 HTTP、全有或全无、无早交付）。批量项 `"single":true/false` 逐项覆盖。⚠ 实测：单请求 `n=N` 在**上游池仍占 ~N 个并发槽**（不是 1）——省的是你自己 key 的槽（1 vs N）和 HTTP 请求数，不是上游并发。单请求安全上限约 `n=4`（n≥6 易 502/超时，因中继把单请求内部扇出成 ~N 个并发上游生成）。
- **轴 C 事件驱动 Coverage-First 调度（`Scheduler`，自动、无需开关）**：替代旧静态轮次。每完成一个请求就 `_pick_unit()` 按当前状态重挑下一个该发的单元，三级优先：①还没主图且当前无在飞的内容→开局各发 `n=1` 抢覆盖；②还没主图、在飞那次已 `STALL_SECONDS`(50s) 未完成→再加一路 `n=1` booster 砸它（每内容最多加 1 路）；③所有内容主图就绪后才发备图（`n=1`）。`_free_mode()` 在有空位的 mode 里按 dispatch 挑。无可发单元且无在飞=全部完成。
- **轴 D 自动降级（`DOWNGRADE_THRESHOLD=3`）**：`on_retry` 回调里数连续 `account_concurrency_exhausted`，连续 3 次→`downgraded=True`，后续所有单元强制 `n=1`（成功一次清零计数）。理由：上游只剩 1 个空位时 `n=1` 挤得进、`n≥2` 挤不进。

## 6. 502 上游网关风暴 vs 账号池满 vs 风控——三者区分与提醒 SOP
**`502/503/504/520/522/524`、`account_concurrency_exhausted`、风控降权——三者都可重试、都不计风控分（前两者），但根因不同、提醒话术不同：**

| 信号 | 根因 | 谁的问题 | 怎么应对 |
|---|---|---|---|
| `502/503/504/520/522/524` | 服务商**上游网关层**（nginx）挂/过载 | 服务商基建，**不是你** | 退避重试自愈；**持续撞 = "上游网关风暴"** → 早提醒用户 + 建议换渠道 |
| `account_concurrency_exhausted` | 上游**账号池**被全网占满（每账号并发 3、一池共享） | 容量拥堵，**不是你** | 退避重试；连撞 3 次 → 自动降 N 到 1（轴 D）挤剩余空位 |
| 风控降权/`api_key_temporarily_banned` | **你**盲刷/重试客户侧错误把风控分打高 | 是你 | 客户侧错误**不重试**、别 ping 探活，保住分 |

**Agent 必须做（约束你自己）：** 后台跑批时**轮询 `.heartbeat`**；一旦 `upstream_storm:true` → **立刻非阻塞 `message_user`**，说清四件事：
1. 这是**上游网关 502 风暴，不是你被风控**、也不是池满；
2. 工具正在**有界退避自愈**（不硬刷、不抬风控分）；
3. **主动建议：你可同时去找别的渠道 / 晚点再来**，别傻等；
4. 附**已覆盖 / 缺图清单**（哪几个出了、哪几个还缺）让用户决断。

> **禁止**闷头等到全 `--retries` 跑完才出声。已出的图不会丢，但该提醒就提醒。侦测阈值与 heartbeat 字段、可选熔断 `--storm-give-up-after` 见 `matsca-api.md` §7/§13。

## 7. 隧道长任务真脱离的坑 + 判活心法（仅 Devin）
- **坑**：隧道 `/api/exec` 有服务端超时，会把跑太久的命令杀掉——单张图常要 1~5 分钟，所以"让 curl 一直阻塞等 gen 完成"必失败。
- **更坑**：`Start-Process -NoNewWindow`（或 `-RedirectStandardOutput/Error`）起子进程没用——子进程挂在父进程 console 上，父被隧道杀时子进程跟着死。
- **✅ 正解**：`Start-Process -FilePath "uv" -WindowStyle Hidden -WorkingDirectory <wd> -ArgumentList @(...)`（ShellExecute 真脱离，不带 `-NoNewWindow`、不带 redirect），结果靠 `--json-out` 文件落盘，事后轮询那个 json。
  - 含中文提示词的 `.ps1` 启动器**必须存成 UTF-8 带 BOM**（否则 Windows PowerShell 5.1 按 ANSI 解析中文报 `The string is missing the terminator`）。
  - 轮询：`Get-ChildItem output\fig\_res_*.json`，文件在且 `ok:true` 即该图就绪。
- **判活心法**：迟迟没 `ok:true` 时——心跳 `.heartbeat` 的 mtime 还在动 = 活着（看 `.log` 多半在退避重试）→ 继续等；mtime 不动了 = 进程已死/被杀 → 才重起该任务。**别一没出图就重跑。**（字段 schema 见 `matsca-api.md` §13。）

### 7b. 别用 OS 进程数当任务数
一个 `uv run gen_image.py ...` 在 Windows 上 = `uv.exe` + `python.exe` **两个 OS 进程**，`Get-Process` 数出来是任务数 ×2。真正占用 API 并发的是在途图片 HTTP 请求数（≈各活任务 `inflight` 之和），一律以 `status.py` 的 `concurrency_used` 为准。

## 8. 传输回传压缩经验（仅 Devin）
原图 1024² PNG 约 2MB/张，走隧道分块传输偏大 → 需要回传预览时压成 ≤900px JPEG（quality≈82）约 90–160KB。
