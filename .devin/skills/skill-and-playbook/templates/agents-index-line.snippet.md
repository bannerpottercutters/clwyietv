# AGENTS.md Playbooks 索引行格式

> AGENTS.md 只索引 **playbooks**：playbook 既**不随 `.devin/skills/` 同步走**、又**不被原生发现**，所以只有它需要在这里登记。
> **skill 不登记**——克隆进工作区的 `.devin/skills/` 被 Devin 自动扫描、Codex/Claude 按 `SKILL.md` 的 `description` 触发。
> **reference 不登记**——随技能走、经该技能 `SKILL.md` 的指针读到。
> 一句话：**建 playbook → 来加一行；建 skill / reference → 不碰 AGENTS.md。**

## 格式

```
- <一句话 what>（<触发场景 或 内容要点>）：playbooks/<name>.md
```

## 例（照抄改）

```
- Agent 运维与排错（隧道 I/O，与具体技能不强相关的通用经验）：playbooks/agent-ops.md
- 技能同步（首次播种 / 增量更新）：playbooks/skills-sync.md
```

## 规则

- 新建 playbook → 加一行；删 playbook → 删那行（**别留死链**）；搬动 → 改路径。
- skill / reference 的增删改 **不动 AGENTS.md**（skill 原生发现、reference 随技能走）。
- 一句话 what 要让人一眼判断"要不要点进去"；触发/要点括号里点明"什么时候会用到它"。
