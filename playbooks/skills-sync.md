# 技能同步（本地中心库 → Devin 云端）

> 两件事，别混：
> - **同步上去**（手动 + 需隧道）：把本地 skills 推到 Devin 云端 `devin/skills` 分支。用户说"同步技能"才做。
> - **每会话取用**（自动 + 免隧道）：每个新会话开局 clone+copy，让技能落进 `~/.devin/skills/`。

## 场景速查

| 场景        | 用户说什么  | agent 做什么                 | 效果                    |
| --------- | ------ | ------------------------- | --------------------- |
| 已同步 + 有隧道 | 什么都不用说 | 读 AGENTS.md + clone+copy  | 技能载入，直接干活             |
| 已同步 + 无隧道 | 什么都不用说 | clone+copy（跳过读 AGENTS.md） | 技能载入，直接干活             |
| 首次 + 有隧道  | "同步技能" | 拉本地 → push → clone+copy   | 技能上 GitHub + 当前会话立即可用 |
| 首次 + 无隧道  | —      | clone 失败 → 跳过取用，正常干活         | 无技能，用内置工具干活           |

> "同步技能"一个词搞定首次和更新，agent 自己判断是播种还是增量。

## 铁律

- **本地 = 正本，云端 = 构建产物**：永远本地 → push 云端，绝不反向 pull 覆盖。产物可 `--force-with-lease`（可重建，安全）。
- **全程排除 `secrets.env`**：始终被 `.gitignore` 排除、永不进仓。优先用 private 仓，public 仓也行——别为改 private 仓折腾登录而卡死。
- **"只改本地不 push"的唯一例外**：仅限本地中心库 → 云端 `devin/skills` 分支、逐次征得用户确认后才 push；日常普通文件改动仍只改本地、不推。

## 为什么走分支而非 main

Devin 的原生技能发现扫描的是**磁盘上家目录的 `~/.devin/skills/`**，与分支、与 main 无关（已实测）。但要把技能放进仓库让 agent 能 clone，最直接的做法是推到 main——这走不通：

- 平台禁 agent 直推 main / 合并 PR
- 本地 git 对他人账号组织无写权
- 用户常登不了 GitHub

所以走一个独立分支 `devin/skills`：推分支 agent 有权、不碰 main、不需合并、不需登录。Agent clone 时指定 `-b devin/skills` 即可。

**固定约定**：宿主 = 该账号组织里某个已连接仓；分支名固定 `devin/skills`。

## 同步上去（需隧道，手动触发）

### 判别首次还是更新

`git ls-remote <宿主仓URL> refs/heads/devin/skills` → 有结果 = 增量更新；无 = 首次播种。不需要用户区分，agent 自己判。

### 步骤

1. 经隧道 base64 从本地取 `skills/` + `playbooks/` + `AGENTS.md`（排除 `secrets.env`）。
2. 重组成 `.devin/skills/<name>/` 布局（SKILL.md 内指针均相对路径，迁目录零改动）。
3. 选一个**已连接仓**（空 / 废稿即可，public 也行）。
   - ⚠️ Agent **无权 API 建仓**。
   - 若该账号一个已连接仓都没有、用户又登不了 GitHub → 退回纯隧道用法（每次给隧道、直接读本地 SKILL.md 干活）。
4. `git push`（播种）或 `git push --force-with-lease`（更新）到分支 `devin/skills`。
5. 回报 commit + 远端树核对（`.devin/skills/...`、`playbooks/...`、`AGENTS.md` 都在、`secrets.env` 不在）。
6. 当前会话执行一次 clone+copy（见下节），让技能立即生效，不用换会话。

## 每会话取用（免隧道，自动执行）

每个新 Devin 云端会话开局、动手干活前先执行（否则技能没载入，agent 只会 fallback 到内置工具）：

```
rm -rf ~/skills-mirror && git clone -b devin/skills <宿主仓URL> ~/skills-mirror && mkdir -p ~/.devin/skills && cp -r ~/skills-mirror/.devin/skills/* ~/.devin/skills/ && ls ~/.devin/skills/
```

技能落进 `~/.devin/skills/`（被扫描的唯一路径），立即进 `<available_skills>`。

**clone 失败**（分支不存在）= 该账号尚未同步技能。跳过取用、正常干活即可，不需要提示用户。等用户给隧道说"同步技能"时再处理。

### 踩坑：clone + copy 缺一不可

Devin **只扫 `~/.devin/skills/`**。`git clone ... ~/skills-mirror` 把技能落在 `~/skills-mirror/.devin/skills/`，**不在扫描路径里**——必须再 `cp` 到 `~/.devin/skills/` 才进 `<available_skills>`。

只 clone 不 copy = 技能用不了、agent 退回内置工具（曾因此把"画猫"用内置 `generate_image` 画了、没走本技能）。

### 不知宿主仓

列已连接仓、挑带 `devin/skills` 分支的那个。最稳的做法是把这条命令放进该账号的**环境蓝图 initialize 步骤**，每个会话自动执行、不靠 agent 自觉。