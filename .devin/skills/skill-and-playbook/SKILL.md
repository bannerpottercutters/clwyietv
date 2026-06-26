---
name: skill-and-playbook
description: 创建、修改、优化、重组我们的可复用资产——技能(skill)、随技能的参考(reference)、中心沉淀经验(playbook)，并维护它们与 AGENTS.md 的四层分层归置。触发场景：做个 skill / 写个技能 / 把这个流程沉淀下来 / 把这段经验记成知识 / 建个 reference 或 playbook / 改进某 skill 的触发词 / 优化更新某个技能 / 审阅别人加进来的内容是否合我们风格 / 把某段内容在 AGENTS.md·SKILL·reference·playbook 之间挪到该放的那层——任何把经验或流程固化成可复用资产的意图都用本技能。它先按四层归置原则判断内容落哪层(AGENTS.md 只留全局红线与索引、SKILL 薄、随技能 reference 厚而全、playbook 沉淀认知)，再判断做 skill / reference / playbook，然后复制 templates/ 现成骨架、按我们的目录命名约定产出或重组，落进中心目录 720_Agents。
---

# skill-and-playbook（建 / 改 / 重组 skill · reference · playbook）

把一段经验或流程**固化成可复用资产**时用本技能——能**新建**，也能**优化更新已有的、审阅别人加进来的内容、把内容在四层之间重新归置**。核心一句话：**先想清每段内容落哪一层、做成哪种资产，再复制 `templates/` 骨架改。** 改编自 Anthropic 官方 `skill-creator`，砍掉偏 Claude 的评测/基准/子代理重型流程，改成中文、配合我们中心目录 `D:\700_Resources\720_Agents` 的约定。

> 🎯 **质量标杆 = `image-gen`**：薄 `SKILL.md`（只讲怎么把图生好）+ 随技能 `references/matsca-api.md`（端点/字段/错误码等事实型规格）+ 中心 `playbooks/image-gen-notes.md`（风控认知/调度哲学/踩坑心法）。建/改任何资产都**对照它的分层与写法**。
> 🧰 **本技能自带模板**：`skills/skill-and-playbook/templates/` 里有四个现成骨架（SKILL / reference / playbook / AGENTS 索引行）。**建任何资产先复制对应模板再改，别从零敲。**

---

## Quick Start（30 秒上手）

建任何资产就两步，照做即可：

1. **判断**：这段内容落哪一层、做成哪种资产？→ 用 §1 的决策树。
2. **复制模板改**：
   ```
   新建 skill      → 复制 templates/SKILL.template.md      到 skills/<name>/SKILL.md
   新建 reference   → 复制 templates/reference.template.md   到 skills/<name>/references/<topic>.md
   新建 playbook    → 复制 templates/playbook.template.md    到 playbooks/<name>.md
   登记索引        → 照 templates/agents-index-line.snippet.md 在 AGENTS.md 加一行
   ```
3. 改完按 §「完成清单」逐项勾，确认无死链、索引已登记。

> 已有资产的优化/审阅/跨层重组走 **D**；细节写法看下面对应章节。

---

## 0. 四层模型（最重要：所有归置都靠它）

我们把可复用资产分四层，**越上层越金贵、越要精简；越下层越厚、越按需加载**：

| 层 | 是什么 | 放什么 | 何时进上下文 | 产物位置 |
|---|---|---|---|---|
| **① AGENTS.md** | 全局入口 / 常驻 | 全局红线、核心约定、**知识索引** | 始终 | `AGENTS.md` |
| **② SKILL.md** | 薄执行主干 | 触发/默认/命令/参数 + 指针 | 技能触发时 | `skills/<name>/SKILL.md` |
| **③ reference（随技能）** | **事实型规格** | 能查表/照抄的硬事实：API 端点·参数·字段·返回、错误码清单、凭证、数字阈值、机读 schema | 实现/排错时按需读 | `skills/<name>/references/<topic>.md` |
| **④ playbook（中心沉淀）** | **经验/认知** | 要理解、讲为什么、讲踩坑的：根因、风控真相、设计哲学、排错心法、SOP、跨工具运维 | 出 bug / 要深挖时整份翻 | `playbooks/<name>.md` |

### ③ 与 ④ 的区别（这是我们和服务商最大的不同，务必分清）
- **reference（③）= 随技能走的"事实"**：**属于某个 skill**，和脚本/API 强绑定，skill-native 工具（Codex/Claude）会随技能自动加载。判据：**能不能查表/照抄？** API 字段、错误码、凭证名、schema、阈值常量——能。→ 放 `skills/<name>/references/`。
- **playbook（④）= 中心沉淀的"认知"**：**不属于触发动作**，平时碰不到，出问题/要深挖才整份翻出来喂模型。判据：**要不要讲"为什么/怎么想/踩过什么坑"？** 风控根因、调度设计哲学、排错心法、跨工具运维坑——要。→ 放中心 `playbooks/`。
- 一句话准则：**能查表的硬事实 → reference；要讲根因/踩坑的 → playbook。** 同一主题常两者都有（如 `image-gen`：字段在 `references/matsca-api.md`，认知在 `playbooks/image-gen-notes.md`），SKILL 用两个指针分别指过去。

> ⚠️ **"reference" 一词只指 ③（随技能的事实规格）**。我们中心的沉淀库叫 **playbook（④）**，不要再叫它 reference——它根本不随任何 skill 打包，是跨任务、按需整份翻阅的知识容器。

### 归置铁律
- **默认下沉**：拿不准一段内容放哪 → 往下放（reference 或 playbook 是兜底容器）；只有 happy-path 必需才上提 SKILL，只有全局红线/索引才上提 AGENTS.md。
- **AGENTS.md 越瘦越值钱**：它常驻上下文，只收最重要的；可有可无、细节性的统统下沉。
- **唯一事实来源**：同一条知识只放一处，上层用指针引到下层，**绝不两边各抄一份**（会漂移）。
- **Devin 靠索引发现**：Devin 不扫 `skills/` 目录——**新增任何 skill / reference / playbook，都必须在 `AGENTS.md` 的「知识索引」登记一行**，否则 Devin 找不到（skill-native 工具能自动扫到随技能的 ③，但 Devin 不行）。

---

## 1. 先判断：做成哪种资产？

```
              要固化一段内容
                    │
        ┌───────────┴───────────┐
   可重复触发的能力/流程?        不靠触发、遇到才翻的知识?
   （能写出"当用户…时触发"）          │
        │ 是                  ┌──────┴───────┐
        ▼                能查表/照抄的         要讲为什么/
      skill              硬事实?              根因/踩坑?
     （走 A）          （API/码/schema/阈值）   （认知/排错心法）
                          │ 是                  │ 是
                          ▼                     ▼
                   reference（走 B）       playbook（走 C）
              skills/<name>/references/    playbooks/<name>.md

   改既有 / 审阅别人加的 / 跨层重组 ── 一律走 D
```

**别做成 skill 的情况（避免滥建）：**
- **一次性任务**（这次跑完不会再触发）→ 直接做，别沉淀。
- **纯信息/事实**（没有"动作流程"）→ 那是 reference 或 playbook，不是 skill。
- **只是给某个已有 skill 补一句话/改个默认值** → 直接改那个 skill（走 D1），别新建。
- 拿不准是不是值得做成 skill → 问用户。

---

## 2. 模板（复制即改，别从零敲）

`skills/skill-and-playbook/templates/` 下四个现成骨架：

| 模板 | 用途 | 复制到 |
|---|---|---|
| `SKILL.template.md` | 新建/重写 skill | `skills/<name>/SKILL.md` |
| `reference.template.md` | 新建随技能 reference | `skills/<name>/references/<topic>.md` |
| `playbook.template.md` | 新建中心 playbook | `playbooks/<name>.md` |
| `agents-index-line.snippet.md` | AGENTS.md 索引行格式 + 例子 | 照着在 `AGENTS.md` 加/改一行 |

用法：复制 → 把 `{{占位}}` 全部替换成真内容 → 删掉模板里的写作自查注释 → 按对应章节（A/B/C）补全 → 按「完成清单」收尾。

---

## A. 创建 / 修改一个 skill

### A1. 捕获意图
先弄清用户到底要什么。当前对话里可能已有要沉淀的流程（"把这个变成 skill"）——先从对话历史抽：用过的工具、步骤顺序、用户做过的纠正、输入/输出格式。缺的让用户补，确认后再往下。要问清四件事：
1. 这个 skill 要让 agent 能做什么？
2. 什么时候该触发？（用户会怎么说、什么场景）
3. 期望的输出格式是什么？
4. 要不要设测试用例验证？（有客观可验证输出的——文件转换、数据抽取、代码生成、固定流程——值得设；纯主观的——写作风格、审美——通常不用。给默认建议让用户定。）

### A2. 目录骨架
```
skills/<name>/
├── SKILL.md            （必需：YAML frontmatter + 薄正文，复制 templates/SKILL.template.md）
├── scripts/            （确定性/重复性任务的可执行脚本，能不进上下文就直接跑）
└── references/         （随技能的事实型规格，按需加载；多主题就拆多份）
```
> playbook **不在 skill 目录里**——它在中心 `playbooks/<name>.md`，由 AGENTS.md 索引串起。skill 目录只装"随技能走的"东西。

### A3. 写 frontmatter（触发的唯一机制）
只有两项必需：
- **name**：英文/ASCII kebab-case，和目录名一致。
- **description**：**决定 agent 会不会调用它**，要同时写清"做什么 + 什么时候用"，把所有"何时用"信息都塞这儿、别放正文。
- ⭐ **description 要"主动一点"**：模型常常**该用却不用**（欠触发）。别只写"做 X 的技能"，要把触发场景铺开、带点"只要提到 A/B/C 就用本技能"的语气。
- ⚠️ 约束（`quick_validate` 会查）：description **不能含尖括号 `<` `>`**、≤1024 字符；name kebab-case、≤64 字符。

**好 vs 坏 description（这是头号变量，照好的抄）：**
```
✗ 坏（欠触发、太干）：
  description: 一个用 matsca 生成图片的技能。
  → 模型只在用户几乎原样说"用 matsca 生成图片"时才想起它，换个说法就漏触发。

✓ 好（铺开触发场景、pushy、说清默认）：
  description: 通过 OpenAI 兼容服务 img.matsca.com 生成/编辑/批量出图并落盘。只要用户提到：
  生图 / 出图 / 画张图 / matsca / 文生图 / 图生图 / 批量生图 / N 张主备 / 生成产品图或封面——
  就用本技能。默认 1536x864、high 质量，优先最便宜的 app 模式，自动重试可重试错误、不盲重试客户侧错误。
  → 触发词铺开、语气主动、默认值前置，模型该用时基本不漏。
```
> 直接对照 `image-gen/SKILL.md` 的 description 抄结构。

### A4. 写薄正文（渐进式披露）
三层加载，越往下越按需：① 元数据（name+description）始终在上下文；② SKILL.md 正文触发时进（**理想 <500 行**）；③ scripts/references 用到才读。
- 正文只留"**顺利把任务做好**"的执行主干（Quick Start/触发/默认/命令/参数 + 指针）。happy-path 不需要的厚重细节**下沉**：事实规格 → 随技能 `references/`；认知/根因/踩坑 → 中心 `playbooks/`。SKILL 用两个指针分别指过去（**对照 `image-gen/SKILL.md` 顶部那段双指针写法**）。
- 多领域/多框架就把 reference 拆多份（`references/aws.md`、`gcp.md`…），agent 只读相关那份；大参考文件（>300 行）开头加目录。
- 用**祈使句**写指令；少用全大写 MUST/NEVER——**多解释"为什么"**，模型理解动机比被硬约束更靠谱。定义输出格式直接给模板；举例给"输入→输出"对照。

### A5. 轻量试跑 + 迭代（定性为主）
> 不用官方那套子代理基准/HTML 查看器。就**手动跑、让用户看、按反馈改**。
1. 写好草稿，编 2–3 个**真实用户会说的** prompt，念给用户确认。
2. 逐个跑：读 SKILL.md、照它做、把产物给用户看（文件存盘告诉路径）。
3. 问反馈："这样行吗？哪里要改？"
4. 按反馈改——**从反馈抽通用规律**，别为过这几个例子做过拟合的硬补丁；删不顶用的内容；把反复出现的辅助步骤固化成 `scripts/` 脚本。
5. 重复到用户满意 / 反馈都 OK。

### A6.（可选）校验与打包
脚本在 `skills/skill-and-playbook/scripts/`，接收**技能目录路径**作参数：
- 校验结构/frontmatter：`python quick_validate.py <skill 目录>`（如 `python quick_validate.py skills/image-gen`）
- 打包成 `.skill`（需要时）：`python package_skill.py <skill 目录> [输出目录]`

### A7. 改既有 skill
**保留原名**（目录名 + frontmatter `name` 都不改）。直接在中心目录那份上改。要系统优化/审阅别人加的/跨层重组 → 走 **D**。

### A8. 端到端微示例（10 行建一个玩具 skill）
假设要沉淀"把一段文字转成规范 commit message"：
```
1. 判断：可重复触发的流程 → skill（走 A）。
2. mkdir skills/commit-msg；复制 SKILL.template.md 过去改：
   name: commit-msg
   description: 把用户给的改动描述转成规范 Conventional Commits。只要用户说：
                写个 commit / 生成提交信息 / commit message / 帮我写 git 提交——就用本技能。
   正文：给出 type(scope): subject 模板 + 3 个"输入→输出"例子（happy-path 就够，不必下沉）。
3. 这个 skill 没有 API 事实、也没踩坑认知 → 不建 reference / playbook。
4. 去 AGENTS.md 加一行索引：- 写提交信息（commit/提交/commit message 时）：skills/commit-msg/SKILL.md
5. python quick_validate.py skills/commit-msg → "Skill is valid!" → 收工。
```
> 这种小技能根本不需要 ③④——**别为了凑四层硬塞**。四层是"容器"，用得着才填。

---

## B. 创建 / 维护一个随技能的 reference

reference 很轻，不要 frontmatter、不要测试循环（复制 `templates/reference.template.md`）：
1. 在 `skills/<name>/references/` 下建 `<topic>.md`（英文/ASCII 名；常用 `<provider>-api.md` 这种点明"什么的规格"，如 `matsca-api.md`）。
2. **只装事实**：API 端点·参数·字段·返回、错误码清单、凭证名/header、数字阈值、机读 schema、退出码语义。**能查表/照抄的才进来**；要讲"为什么"的归 playbook。
3. 结构清晰：标题分节，编号用工具识别稳的形式（`3.1/3.2` 优于 `A/B/C`）；>300 行开头加目录。
4. **在 `AGENTS.md` 知识索引登记一行**（Devin 才找得到；skill-native 工具虽自动加载，但 Devin 不扫 skills 目录）。
5. 与 playbook 互指：reference 里"为什么"一句带过、用指针指向 `playbooks/<name>.md`，反之亦然。

---

## C. 创建 / 维护一个中心 playbook

playbook 同样轻、无 frontmatter（复制 `templates/playbook.template.md`）：
1. 在中心 `playbooks/` 下建 `<name>.md`（属某 skill 的深层经验就同名，如 `image-gen-notes.md`；跨任务全局知识用主题名，如 `agent-ops.md`）。
2. **只装认知**：根因、风控/机制真相、设计哲学、踩坑实测、排错心法、SOP、跨工具运维坑。**写厚、写全、经验拉满**——它就是兜底容器，平时不进上下文，出事才整份翻。
3. 结构清晰（标题分节、`3.1/3.2` 编号、>300 行加目录）；开头一句写明"配哪个 SKILL/reference 一起看"。
4. **在 `AGENTS.md` 知识索引登记一行**。
5. 事实别往这塞（那是 reference 的活）——要引用具体字段/阈值时用指针指向对应 `references/<topic>.md`。

---

## D. 更新 / 优化 / 重组已有资产（改既有、审阅别人加的、跨层归置）

新建之外的所有"整理"活儿都走这里。**贯穿始终的尺子就是 §0 的四层归置原则。**

### D1. 优化 / 更新已有 skill / reference / playbook
- **保留原名**，直接在中心目录那份上改。
- **skill**：自查"`description` 会不会欠触发/误触发？正文是不是超薄主干、有没有该下沉的细节？"——厚知识按③/④分别下沉，SKILL 只留指针。
- **reference**：补全事实、保持可被工具稳定识别的结构；别混进"为什么"（挪去 playbook）。
- **playbook**：尽管加厚加经验；别混进可查表的硬事实（挪去 reference）。
- 改完若动到触发/命令/参数，按 A5 跑 2–3 个真实 prompt 验证。

### D2. 审阅别人加进来的内容（合不合风格 + 该落哪层）
逐段过，对每段判它**该待在哪一层**：
- **该上提 AGENTS.md**：其实是全局红线/必经索引却埋在某资产里 → 提上去（但 AGENTS.md 只收最重要的，别滥提）。
- **该留 SKILL**：happy-path 真需要且够重要的执行要点 → 留 SKILL。
- **该下沉 reference**：可查表的事实规格（字段/错误码/schema/阈值）→ 移到对应 `skills/<name>/references/`。
- **该下沉 playbook**：原理/根因/排错/长篇经验 → 移到 `playbooks/<name>.md`。**这两类下沉最常见——拿不准就下沉。**
- **该删 / 合并**：和已有内容重复（违反唯一事实来源）→ 合并到唯一那处、删冗余；既不重要又冗长又没人会查 → 直接删。

### D3. 重组搬运的铁律
- **唯一事实来源**：搬走后原处只留指针，别两边各留一份。
- **搬动后同步 `AGENTS.md` 知识索引**那一行（路径/描述对得上）；新建的资产别忘加索引行；删掉的资产记得删索引行（**别留死链**——曾经 `common-bugs.md` 索引指向不存在的文件就是反例）。
- 大改动前先跟用户对一下归置方案（哪些上提、哪些下沉、哪些删），再动手。

> 📌 **拆分实例（照着做）**：早期 `image-gen` 的全部深层内容堆在一个 `references/image-gen-notes.md` 里。重构时按 §0 准则**一拆为二**：可查表的硬事实（端点/参数/错误码/凭证/manifest schema/阈值）→ `skills/image-gen/references/matsca-api.md`（③）；要讲为什么的认知（风控根因/调度哲学/隧道脱离坑/排错心法）→ `playbooks/image-gen-notes.md`（④）。SKILL 顶部用两个指针分别指过去，AGENTS.md 索引同步成两行。

---

## 完成清单（Definition of Done · 交付前逐项勾）

**建/改 skill：**
- [ ] frontmatter 只有 name + description；name 与目录同名、kebab-case；description 写清"做什么+何时用"、够 pushy、无尖括号、≤1024 字符
- [ ] 正文 <500 行、只留 happy-path；厚事实下沉 `references/`、厚认知下沉 `playbooks/`，本文件只留指针
- [ ] `python quick_validate.py <skill 目录>` 输出 "Skill is valid!"
- [ ] 跑过 2–3 个真实 prompt、给用户看过（若动了触发/命令/参数）

**建/改 reference 或 playbook：**
- [ ] reference 只装可查表事实、playbook 只装认知；两者不串味
- [ ] 开头写明配哪个 SKILL/reference 一起看；>300 行有目录
- [ ] 引用对方内容时用指针、不复制（唯一事实来源）

**所有资产通用（收尾必查）：**
- [ ] `AGENTS.md` 知识索引：新建已加行 / 改动已改路径 / 删除已删行
- [ ] **无死链**：所有指针路径都解析得到真实文件
- [ ] 落进中心目录 `D:\700_Resources\720_Agents\`，没在别处留副本
- [ ] （隧道场景）只改本地、未推 GitHub

---

## 公共约定（A/B/C/D 都适用）

- **中心目录 = 唯一事实来源**：所有资产都产在 `D:\700_Resources\720_Agents\` 下（`skills/<name>/`、`skills/<name>/references/<topic>.md`、`playbooks/<name>.md`），别在别处另存副本。各工具靠 CC Switch 软链接 / junction 共享这份中心目录（见 `AGENTS.md`）。
- **命名用英文/ASCII**：工具链对中文目录/文件名支持不稳，统一英文小写、连字符分隔。
- **风格对齐 `image-gen`**：frontmatter 写法、中文正文 + pushy description、输出/目录约定、**薄 SKILL + 厚 reference + 厚 playbook 用指针衔接**——全部参照它。
- **跨工具通用**：skill/reference/playbook 是开放格式，Codex/Devin/Claude 都能用。本技能讲"产物长什么样、放哪、怎么登记"——和具体工具无关。
- **写入方式按工具不同**：Devin 经隧道改本地、只改不推 git；Codex 本地直接写中心目录。**具体 I/O 规则见 `AGENTS.md` / `playbooks/agent-ops.md`，本技能不重复。**

---

## 核心循环（记到 TodoList 别忘）

1. **先归置**：按四层归置原则（§0）想清每段内容落 AGENTS.md / SKILL / reference / playbook 哪一层。
2. 判断做 skill 还是 reference 还是 playbook；是新建，还是改既有 / 审阅别人加的 / 跨层重组（§1 决策树）。
3. **复制 `templates/` 对应骨架**，起草（走 A/B/C）或 优化重组（走 D）。
4. （skill）跑 2–3 个真实 prompt，给用户看。
5. 按反馈改，重复到满意。
6. 按「完成清单」收尾：落中心目录、AGENTS.md 索引加/改/删、无死链、指针都解析得到。
