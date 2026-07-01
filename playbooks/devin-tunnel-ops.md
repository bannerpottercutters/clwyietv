# Devin 云端隧道运维排错

> 仅适用于 **Devin 云端**通过内网穿透隧道操作用户本地 Windows 机器的场景。

## 隧道基础

隧道提供两个 REST 端点，均需 `Authorization: Bearer <token>`：

| 端点           | 能力     | 限制                                      |
| ------------ | ------ | --------------------------------------- |
| `/api/files` | 列目录    | 沙盒限制在 workspace 内；不能读内容、不能写             |
| `/api/exec`  | 执行任意命令 | 不受沙盒限制，任意盘符 / 路径；**缓冲整个进程输出直到结束**（长任务会超时） |

所有读写都走 `/api/exec`（POST JSON `{"cmd": "..."}`，内层用 `powershell -NoProfile -Command` 执行）。

**防 bash 插值**：请求体用脚本直接拼，别让 bash 展开 `$` 变量——Devin 云端 shell 是 bash，PowerShell 变量 `$env:`、`$_` 等会被 bash 先吃掉，导致命令到远端时已残缺。

**长任务**：`/api/exec` 会等进程结束才返回，跑长任务会超时。用 `Start-Process ... -RedirectStandardOutput <file> -RedirectStandardError <file>.err -NoNewWindow` 后台跑，再分次读文件轮询输出。

**PowerShell 引号地狱**：`{"cmd":"powershell -Command \"...\""}` 经 JSON→命令行→PowerShell 三层转义，复杂命令常返回空 / 报错。
- 正解：用 `powershell -NoProfile -EncodedCommand <BASE64>`，其中 base64 是 **UTF-16LE** 编码的脚本：
  ```bash
  B64=$(iconv -f UTF-8 -t UTF-16LE script.ps1 | base64 -w0)
  curl ... -d "{\"cmd\":\"powershell -NoProfile -EncodedCommand $B64\"}" "$URL/api/exec"
  ```
- 简单命令可用 `findstr`/`dir`/`copy` 等 cmd 内建避开引号。

## 读

读很便宜：文件内容走在**响应**里，不受命令行长度限制，任意路径都能读。`[Convert]::ToBase64String([IO.File]::ReadAllBytes('<任意path>'))` 一次调用取回一个文件（实测单响应 ~60KB base64 没问题）。

**按需拉取，别无脑整树**：
- **单文件 / 几个文件** → 每个文件一次 `ReadAllBytes|ToBase64String`（1 次往返 / 文件）；跨目录的几个文件也可 `tar -cf %TEMP%\b.tar <file1> <file2> ...` 显式列表一次拉。
- **整树拉前先掂量大小**（Devin 打开的文件夹可能很大，整包又慢又可能撑爆单次响应 / 超时）：
  - `D:\700_Resources\720_Agents`（小而固定）→ 直接整树拉。
  - Devin 打开的文件夹（=隧道 workspace，见 `/api/ping`）→ 先量大小再决定（一次调用、只量不取内容）：
    - git 仓库（首选，自动尊重 `.gitignore`）：`git -C <ws> ls-files | Measure-Object` 数文件；大小可 `git -C <ws> ls-files | %{ (Get-Item $_).Length } | Measure-Object -Sum`。
    - 非 git：`Get-ChildItem <ws> -Recurse -File -Force -EA SilentlyContinue | ?{ $_.FullName -notmatch '\\(\.git|node_modules|\.?venv|dist|build|target|__pycache__)\\' } | Measure-Object -Sum Length`。
  - 判定：小（默认阈值 **≲50MB 且 ≲500 文件**，可调）→ 整树拉；大 → 拉目录结构当地图（`git ls-files` 或 `Get-ChildItem -Name`）+ 按需单拉 / 只拉相关子目录。
  - 整树拉取：git 仓库用 `git -C <ws> archive --format=tar HEAD`（只含跟踪文件、自动跳过忽略项）→ base64；非 git 用 `cd <ws>; tar --exclude=.git -cf %TEMP%\b.tar .` → base64。一次往返拉回，本地 `tar -xf` 解开（实测 9 文件/47KB 一次搞定）。`tar.exe` 是 Win10+ 自带。

一句话：**只拉你要看 / 要改的**，单文件就单拉，整目录批量改才整包拉。

### 终端中文乱码：是渲染问题，不是数据问题

通过隧道读中文文件（`type` / `Get-Content` / `curl` 返回的 JSON）时，中文等多字节字符会"一个隔一个"地丢，整段像乱码。

**根因（已实测定位）**：数据没坏，是 PTY 渲染丢字。隧道 API 返回的字节完整正确；乱码只发生在交互式终端渲染这一步。实测证据：同一份 `type` 输出重定向到文件后用读文件工具 / `xxd` 看，字节级完全正确；只有直接打印 / 回显到终端才花。

**解法（都要"落地成文件"）**：
1. **重定向到文件再读**（最简单）：`curl ... > resp.json`，再用读文件工具 / `xxd` 打开 `resp.json` 里的 `stdout`。
2. **base64 取回**（更稳，顺带保证二进制完整）：远端 `[Convert]::ToBase64String([IO.File]::ReadAllBytes('<path>'))` → 本地 `base64 -d > file` 解码再读。base64 是纯 ASCII，过终端不丢。

永远不要靠终端回显判断文件内容。

## 写

写是贵的方向：数据走在 Windows 命令行上，**单次上限约 5000 base64 字符**（实测 5000 可、8000 报错 exit 1），大文件得分块 = 多次调用。

**流程**：分块 base64 写临时文件 → 远端解码落盘 → 校验 SHA：

```
# 分块（每块 ~5000 字符）：第一块 Set-Content -NoNewline，后续 Add-Content -NoNewline 到 <dst>.b64
# 落盘：[IO.File]::WriteAllBytes('<dst>',[Convert]::FromBase64String((Get-Content -Raw '<dst>.b64'))); Remove-Item '<dst>.b64'
# 校验：(Get-FileHash '<dst>' -Algorithm SHA256).Hash.ToLower()  ←  与本地 sha256sum 比对
```

**写 + 校验合并成一条命令**：写完同一调用里返回 `(Get-FileHash <dst> -SHA256)`，与本地 `sha256sum` 比对，省掉单独的校验往返。

**只推改过的文件**：配置/文本通常 1 块即 ~1–3 次调用；改了很多个才考虑"推 tar 再远端解开"。

### 踩坑

- **建目录必须先坐实再传**：快速连发 `/api/exec` 时，建子目录的 `New-Item` 会被隧道间歇丢掉 / 截断，后续 `Add-Content` 因父目录不存在而整体失败。写文件前单独「`New-Item -Force` → `Test-Path` 验证 → 失败重试（≤6 次、间隔 1s）」把目录坐实再传；每次 exec 之间留 ~0.3s 间隔降丢包。
- **空 SHA = 命令未落地，不是内容损坏**：远端 `Get-FileHash` 返回空串 = 整条远端命令失败（多半是 `.b64tmp` 缺失 / 目录不存在）。排错先查目录 / 临时文件，别从 base64 分块方向钻。区分「空串=命令未落地」与「非空但不等=内容不符」两类日志。

## 效率原则

Devin 的成本 ≈ **往返次数 + 进上下文的输出字节**。以下原则帮你在两者上都省：

- **本地改、只推一次**：拉一次 → 用文件工具本地编辑 → 推改动的文件。别"边改边推"或反复回拉确认。
- **能合并就合并**：独立命令用 `;`/`&` 串到一条 exec 里，减少往返。
- **别把内容打到终端**：既乱码又烧 token，重定向到文件用文件工具读。
- **大二进制（如生成的图）**：尽量在远端直接生成到目标目录，别"远端生成→拉下来→再推上去"白跑命令行上限。
