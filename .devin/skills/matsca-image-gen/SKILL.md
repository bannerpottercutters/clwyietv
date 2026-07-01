---
name: matsca-image-gen
description: 通过 Matsca 图像服务生成、编辑、保存图片及变体，成本低于内置 generate_image。当用户需要生成图片、画一张图、配图 / 插图、制作图标或 logo、改图 / 图生图、蒙版重绘、生成变体，或需要为 HTML / 文档 / PPT 批量配图时使用。不用于纯裁剪 / 缩放 / 格式转换等不涉及 AI 生成的操作。
---

# Matsca 生图

本技能通过 `scripts/gen_image.py` 调用 Matsca 图像服务出图，内置多 Key 调度与自动容灾，以 `manifest.json` 为结果契约。

## 快速开始

```bash
# 单张（默认 -n 2 = 主图 + 备1，赛马 + 覆盖优先常开）
python scripts/gen_image.py "一只戴宇航头盔的橘猫，扁平插画" --name 橘猫 --outdir output/fig

# 批量（JSON 列表：每项可带 name/prompt/n/size/edit/mask/variation/aspect/model）
python scripts/gen_image.py --prompts-file batch.json --outdir output/fig

# 只验活所有 Key（全失败退出码 4）
python scripts/gen_image.py --ping-only
```

退出码：`0` 全部出齐；`3` 有失败 / 部分成功；`4` 一张都没出。判定成败只读 `outdir/manifest.json`，不要 `ls` / `dir` 磁盘或数进程。

## 凭据管理

凭据包含 API Key（调接口）和 dev 账号（Key 失效时自动 reveal 新 Key）。

本地运行：脚本自动搜索 `~/720_Agents/secrets.env`、`~/secrets.env`、`./secrets.env`，agent 无需传 `--secrets-file`。Key 失效时自动 reveal 并回写本地。

Devin 云端运行：Agent 通过隧道读取用户本地 `secrets.env`，设为云端环境变量 `MATSCA_API_KEYS` / `MATSCA_DEV_EMAIL` / `MATSCA_DEV_PASSWORD`。Key 失效时新 Key 输出到 summary JSON 的 `refreshed_keys` 字段，agent 应通过隧道回写用户本地 `secrets.env`。

不要让用户手动更新凭据。

## 调度策略

调度策略由脚本内部自动处理，以下仅列出可调参数与需知晓的行为：

- 并发：每 Key 在飞请求 ≤2，全局 ≤6（硬编码，提吞吐靠加 Key 而非调高单 Key 并发）。
- 重试：HTTP 层 2 次 + 任务层 ≤4 次，指数退避、尊重 `Retry-After`（429 ≥10s、封顶 30s/120s）。
- 逐 Key 冷却：6s→120s 指数退避 + 连击 + jitter；故障转移挑最闲且不在冷却的别的 Key。
- 赛马（`--no-race` 关）：`-n N` 时并发抢主图，默认开。
- 覆盖优先（`--no-coverage-first` 关）：多内容时先各凑一张再补备份，默认开。
- 受阻侦测：零进展超 `--block-after`（默认 90s）时标 `blocked`，manifest 中可见 `blocked` 及 `block_reason`。`--give-up-after N` 秒后提前收尾。
- 内容违规等客户侧错误不重试，直接失败。
- 已落盘 ≥1 张的内容绝不回退成 `failed`；`completed` 是终态，`failed` 可被晚到成功翻回。

完整 CLI 参数、凭据解析优先级、错误码分类、manifest schema、常量表见 `references/matsca-reference.md`。

## 长任务 + 自动回传

上游单张 30s~10min、批量可达几十分钟。不要在前台同步等——后台跑 `gen_image.py`，另起 `auto_deliver.py` 看护：

Linux / macOS：

```bash
# 后台出图
python scripts/gen_image.py --prompts-file batch.json --outdir output/fig \
    --secrets-file secrets.env & GEN_PID=$!

# 看护：轮询 manifest → 压预览 → 经隧道回传用户机 → SHA256 校验
python scripts/auto_deliver.py --outdir output/fig \
    --tunnel http://<隧道>/api/exec --token "Bearer <token>" \
    --remote-dir "D:\\目标目录" --gen-pid $GEN_PID
```

Windows (PowerShell)：

```powershell
# 后台出图
Start-Process -FilePath python -ArgumentList 'scripts/gen_image.py','--prompts-file','batch.json','--outdir','output/fig','--secrets-file','secrets.env' -PassThru | Set-Variable GEN_PROC

# 看护：轮询 manifest → 压预览 → 经隧道回传用户机 → SHA256 校验
python scripts/auto_deliver.py --outdir output/fig `
    --tunnel http://<隧道>/api/exec --token "Bearer <token>" `
    --remote-dir "D:\\目标目录" --gen-pid $GEN_PROC.Id
```

看护收尾：`manifest.ok` / 出图进程结束 / `--max-idle-min` / `--deadline-min` 任一成立即退。

## 压缩预览

```bash
python scripts/make_preview.py <图或目录> [--outdir preview] [--max-px 900] [--quality 82]
```

最长边压到 ≤900px JPEG，原图保留；不放大小图。

## 注意

- 默认 `--model gpt-image-2`、`--size auto`、`-n 2`；`size=auto` 且给 `--aspect` 时比例写进提示词。
