# AGENTS.md 知识索引行格式

> 新建 / 改动 / 删除任何 skill·reference·playbook 后，都要回 `AGENTS.md` 的「知识索引」加 / 改 / 删对应一行。
> Devin 不扫 `skills/` 目录，全靠这里发现 reference 和 playbook；skill-native 工具（Codex/Claude）能自动加载随技能的 `references/`，但 Devin 不行——所以索引必须登记。

## 格式

```
- <一句话 what>（<触发场景 或 内容要点>）：<相对路径>
```

## 例（照抄改）

```
- 生图（用户要 matsca 出图 / 批量 / N 张主备时）：skills/image-gen/SKILL.md
- 生图 API 事实（端点·参数·错误码·凭证·manifest schema）：skills/image-gen/references/matsca-api.md
- 生图认知（风控根因·调度哲学·502 风暴·隧道脱离坑）：playbooks/image-gen-notes.md
```

## 规则

- 新建资产 → 加一行；删资产 → 删那行（**别留死链**）；搬动资产 → 改路径。
- 一句话 what 要让人一眼判断"要不要点进去"；触发/要点括号里点明"什么时候会用到它"。
