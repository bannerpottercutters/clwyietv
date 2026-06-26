# 全局 Agent 指令

> 所有 AI 编程工具的全局指令入口。只放**全程不能忘的红线 + 索引**；长流程与排错沉到 `playbooks/`，按需翻阅。

## 仅 Devin（红线，细节见 playbooks/agent-ops.md）
- **产出物落盘 + 对话回传**：为给用户看而产出的附属物，**两处都给**——①落盘 `output/`（md→`output/`、图→`output/fig/`、其它按类型分子目录）；②同时在对话框作为附件回传，别只落盘不回传。改项目代码不挪进 `output/`；用户指定路径以用户为准。
- **隧道只改本地、不 push**：经隧道操作用户本地文件时只改本地、不 `commit`/`push`，提交时机由用户定。**例外**：skills 中心库同步 / 新账号引导同步，且仅逐次征得用户确认后。
- **隧道 I/O 红线（仅 Devin）**：收发只走 `/api/exec`；**读**用 base64（防中文乱码），整树先掂量大小再决定整拉/按需拉；**写**分块（单次 ≤~5000 b64 字符）+ 同条命令 `Get-FileHash` 自校验；别信终端中文回显。完整打法 → `playbooks/agent-ops.md`。

## Playbooks（知识沉淀，平时不碰、出 bug/深挖才整份翻；原生发现不会列它们）
- Agent 运维与排错（含隧道 I/O 完整打法）: `playbooks/agent-ops.md`
- 新账号/空白账号引导同步（探测连接仓 → 判云端 → 选空连接仓 → `.devin/skills/` 重组 → 排除 `secrets.env` → push → 重开验证；含「本地=正本、云端镜像=产物、单向同步」铁律）: `playbooks/devin-bootstrap.md`

## 技能（skills/）
- 可复用流程见 `skills/` 目录（中心=唯一源；由 CC Switch 软链分发到各 App）。技能**靠原生发现，不在此罗列**：
  - **Codex / Claude Code**：扫各自 skills 目录，按 SKILL.md 的 `description` 自动触发。
  - **Devin**：本仓库连接组织 / 带 `.devin/skills/` 被克隆后，开机原生索引各 `SKILL.md` 自动发现；未连接或纯隧道时，直接列 `skills/` 目录、读对应 SKILL.md 发现。