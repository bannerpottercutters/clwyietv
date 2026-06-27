# Playbook：技能同步（本地中心库 → 当前 Devin 账号云端）

> **何时翻**：用户说"同步 / 拉取 / 部署技能"——想让当前/新的 Devin 账号用上本地中心库 `D:\700_Resources\720_Agents` 的 skills 时。**手动触发**，agent 不要自己每次瞎查、瞎触发。
> **正本在这里**：知识只一行触发指路；完整步骤/约束/红线都在本文件，随仓库走。

## 0. 铁律：本地=正本，云端=构建产物，单向同步
- **方向只有一个**：永远 `本地 D:\700_Resources\720_Agents → push 云端`。云端**绝不在 GitHub 手改**、绝不从云端 `pull` 回来覆盖本地。
- **云端是产物**：由本地 `skills/` 重组成 `.devin/skills/` 生成、每次从本地**重生/覆盖**；分叉时可 `--force-with-lease`（产物可重建，安全）。
- 全程**排除 `secrets.env`**；**private 优先、public 也可**——secrets.env 始终被 `.gitignore` 排除、永不进仓，公开仓也不泄密；别为改 private 折腾登录而卡死。

## 1. 一个动作，两种情形（"同步"自动判别）
触发后先判云端目标分支状态，自动走对应支——**不需要你区分是"首次"还是"更新"**：
- 云端**没有**（宿主仓里无 `devin/skills` 分支）→ **首次播种**：整套推上去。
- 云端**已有** → **增量更新**：本地为准，覆盖该分支。
- 判别：`git ls-remote <宿主仓URL> refs/heads/devin/skills`（有→更新；无→播种）。

## 2. 选定路线：分支 + 每会话 clone 进工作区，免 main / 免登录
**为什么不走 main**：「开机原生发现」需技能落默认分支 main，而落 main 需有人登录 GitHub 动手（平台禁 agent 直推 main/合并 PR；本地 git 对他人账号组织无写权；用户常登不了 GitHub）→ 走不通。该路线只需技能在**任一分支**：Devin 扫描的是**工作区磁盘上的 `.devin/skills/`**，与分支、与 main 无关（已实测）。
**固定约定（免每次猜）**：宿主 = 该账号组织里**某个已连接仓**；分支名固定 **`devin/skills`**。

### 2.1 同步上去（需隧道；首次播种与增量更新同一套）
1. 经隧道 base64 从本地取 `skills/`+`playbooks/`+`AGENTS.md`（**排除 `secrets.env`**）。
2. 重组成 `.devin/skills/<name>/` 布局（SKILL.md 内指针均相对路径，**迁目录零改动**）。
3. 选一个**已连接仓**（空/废稿即可，**public 也行**）。⚠️ Agent **无权 API 建仓**；若该账号一个已连接仓都没有、用户又登不了 GitHub → 退回纯隧道用法（每次给隧道、直接读本地 SKILL.md 干活）。
4. `git push`（播种）或 `git push --force-with-lease`（更新）到分支 **`devin/skills`**。推分支 agent 有权、**不碰 main、不需合并、不需登录**。
5. 回报 commit + 远端树核对（`.devin/skills/...`、`playbooks/...`、`AGENTS.md` 都在、`secrets.env` 不在）。

### 2.2 日常每会话取用（免隧道 / 免 main / 免登录）
会话开局执行：
```
git clone -b devin/skills <宿主仓URL> ~/skills-mirror
```
→ 工作区落盘 `.devin/skills/`，技能立即进 `<available_skills>`，正常使用。不知宿主仓：列已连接仓、挑带 `devin/skills` 分支的那个 clone。

## 3. 与"隧道只改本地、不 push"关系
本流程是"隧道只改本地、不 push"的**唯一同步类例外**：仅限**本地中心库 → 云端 `devin/skills` 分支**、仅在**逐次征得用户确认后**才 push；日常普通文件改动仍只改本地、不推。