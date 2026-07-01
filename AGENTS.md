# 全局 Agent 指令

> 所有 AI 编程工具的全局指令入口。

## Playbooks（深度经验 / 运维；出 bug 或要深挖才整份翻）
- `playbooks/agent-ops.md` — 本地工具联网代理（v2rayN）、Chrome 渲染中文名坑。本地工具（运行在用户本机的 AI 编程助手，如 Codex / Claude Code / Windsurf 等，区别于 Devin 云端）联网超时时翻。
- `playbooks/devin-tunnel-ops.md` — Devin 云端隧道端点、读写策略、终端乱码根因、文件传输踩坑。隧道 I/O 出问题或要批量传文件时翻。
- `playbooks/skills-sync.md` — 本地技能推到 Devin 云端、每会话取用技能的完整步骤与踩坑。要同步 / 部署技能时翻。

## 技能（skills/）
- 中心 = 唯一源（CC Switch 软链分发到各 App）。技能**靠原生发现**，方式如下：
  - **本地工具**：扫各自 skills 目录，按 `SKILL.md` 的 `description` 自动触发。
  - **Devin 云端**：把含 `.devin/skills/` 的仓库克隆进工作区即被扫描、列入可用技能清单（同步办法见 `playbooks/skills-sync.md`）。