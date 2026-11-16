---
name: ontology-business-map
description: 把一个业务域（行业 + 业务域 + 业务文档）自动建模成一套五视角本体模型（对象/行为/规则/事件/场景/报表），并输出独立可交互的 HTML 图谱。当用户要求"对某行业某业务域做本体建模""生成业务本体模型""做领域模型 + 可视化""按建模规范产出 YAML + 动态图谱"时使用。也用于在既有本体模型上补视角、修校验错误、重出图谱。
description_zh: 行业+业务域 → 五视角企业级本体模型 + 可交互 HTML 图谱（对象/行为/规则/事件/场景/报表）
description_en: Industry + business domain to a five-view enterprise ontology model with an interactive HTML graph
version: 1.0.0
allowed-tools: Read,Write,Bash,Glob,Grep
---

# 业务域本体建模（ontology-business-map）

## 这个技能做什么

输入：`行业` + `业务域`（+ 可选业务文档、参考方法论、参考系统）
输出：**6 个 YAML 模型文件** + **1 个独立 HTML 动态图谱**（双击即开，无需服务器）+ 校验/质量报告。

核心取舍（改这套流程前先读这三条）：

1. **模型只写 JSON，YAML 由脚本独占生成**。LLM 直接写 YAML 极易在缩进/多行字符串/中文引号上翻车，且出错后无法定位。改为分片写 JSON（可用工具校验、可精确指位），最后由 `obm.py toyaml` 统一序列化。
2. **骨架先行**。四个模型分片是并行的，跨片引用靠 ID 维系。先产出全局 ID 清单（`skeleton.json`）把名字钉死，分片只填内容、不起名，从而根除 ID 漂移。
3. **三道校验门**。片级（字段封闭/引用/枚举）→ 合并级（ID 唯一/双向闭合/跨片可落地）→ 全量级（预算/反模式/覆盖/评分）。每道门都必须 0 error 才进下一阶段。

## 目录

```
<SKILL>
  SKILL.md
  schema/closed-fields.json        # 封闭字段集 + 规模预算（唯一的字段真相来源）
  scripts/obm.py                   # 工具链：init/status/check/merge/quality/toyaml/render/selftest/fromyaml
  scripts/viewer/ontology_viewer.py# 图谱渲染器（ECharts 力导向 + Mermaid 指标树）
  assets/echarts.min.js, mermaid.min.js
  prompts/00-画像.md 01-骨架.md 02-分片-object.md 03-分片-behavior.md
          04-分片-rule.md 05-分片-event.md 06-分片-scenario.md
          07-分片-report.md 08-修复.md
```

**字段口径只有一个来源：`schema/closed-fields.json`。** 任何提示词与它冲突，以 schema 为准；要改字段先改 schema，再改 prompts。

## 工作目录

统一用 `<workdir>`（建议 `<项目>/ontology_<域码小写>/`）：

```
<workdir>/
  config.json          # 行业/域/预算（init 生成）
  profile.md           # 阶段0 业务域画像
  skeleton.json        # 阶段1 全局 ID 清单
  docs/                # 用户提供的业务文档（可选）
  _parts/              # 分片 JSON：m1-00.json m2-00.json m3-00.json me-00.json m4-00.json m7-00.json
  merged/              # 合并产物 + _evidence.json + merge-report.json
  yaml/                # 最终交付：6 个 yaml + ontology-graph.html + ontology-model.json + 报告
  check-report.json / quality-report.json / selftest-report.json
```

分片命名：`<letter>-<两位序号>.json`，letter ∈ `m1 m2 m3 me m4 m7`。同一模型多个分片时序号递增，**同一 ID 不得跨片重复**。

## 工具链

`PY` 指环境里可用的 Python 解释器（Python ≥ 3.8；Windows 用 `python` 或 `py -3`，macOS/Linux 用 `python3`）。
`OBM` = `<SKILL>/scripts/obm.py`，其中 `<SKILL>` 是本技能根目录（`obm.py` 内部用 `__file__` 反推，与安装位置无关，可随意搬迁）。

**唯一第三方依赖：`PyYAML`**（`pip install pyyaml`）；其余全部走标准库，`echarts.min.js` 与 `mermaid.min.js` 已内置在 `assets/`，**运行时不需要联网**。

```bash
PY OBM init    <workdir> --industry "手机制造" --domain "产品研发"
PY OBM status  <workdir>
PY OBM check   <workdir> --json          # 片级校验；有 error 则退出码 1
PY OBM merge   <workdir>                 # 合并 + 双向引用同步
PY OBM quality <workdir>                 # 预算/反模式/覆盖/评分
PY OBM toyaml  <workdir>                 # JSON -> YAML（唯一出口）
PY OBM render  <workdir> --title "手机制造·产品研发 本体模型"
PY OBM selftest<workdir>                 # HTML 冒烟验收
PY OBM fromyaml<workdir> --yaml-dir <dir># YAML 反解为分片（接手别人模型 / 回归用）
```

## 阶段编排

### 阶段 0 · 画像（1 次，串行）

读 `prompts/00-画像.md`，产出 `<workdir>/profile.md`。

要点：先定**总量预算**（对象 15-30 / 行为 25-50 / 规则 10-20 / 事件 8-20 / 场景 4-8 / 报表 10-20），再定能力域（6~10 个，作为后续覆盖率基准），最后必须写清**"本域不建什么"**——这是防膨胀的唯一闸门。

> 有用户文档时：先读文档再写画像，第 6 节（行业硬约束）优先从文档抽取，不要凭常识编。

**人机确认点 ①（建议）**：画像里的"端到端主流程 + 能力域 + 对象候选清单"给用户看一眼。这一步错了，后面全错，成本最低。

### 阶段 1 · 骨架（1 次，串行，最关键）

读 `prompts/01-骨架.md`，产出 `<workdir>/skeleton.json`。

要点：
- 域码全模型唯一（如 `RD`），ID 一旦写入不得改名
- 五视角全部排到预算区间内，**事件和场景最容易漏**
- 用 `edges` 提前查孤岛：每个对象至少一条 `OWNS`，每个事件至少一条入边，每个规则至少一条 `APPLIES`
- `capabilities` 覆盖画像第 3 节全部能力域，每个 node 的 `cap` 指向其中之一

骨架完成后跑一次数量自检（不必用工具，数 `nodes` 里各 kind 的数量）：

```bash
PY -c "import json,collections;d=json.load(open(r'<workdir>/skeleton.json',encoding='utf-8'));print(collections.Counter(n['kind'] for n in d['nodes']))"
```

数量不足预算下限 → **回骨架补，不要进分片**。骨架少一个，最后就少一片。

**人机确认点 ②（可选）**：骨架就是模型的目录，给用户扫一眼 ID 清单，比看 YAML 便宜得多。

### 阶段 2 · 分片（6 个分片并行）

按模型分发，每个分片读自己的 prompt：

| 分片 | prompt | 输出 | 依赖的骨架 ID |
|---|---|---|---|
| 对象 | `02-分片-object.md` | `_parts/m1-00.json` | OBJ- / ASSOC- / DICT- |
| 行为 | `03-分片-behavior.md` | `_parts/m2-00.json` | BHV-（引用 OBJ-/RULE-/EVT-/QR-） |
| 规则 | `04-分片-rule.md` | `_parts/m3-00.json` | RULE-（引用 BHV-） |
| 事件 | `05-分片-event.md` | `_parts/me-00.json` | EVT-（引用 BHV-/OBJ-） |
| 场景 | `06-分片-scenario.md` | `_parts/m4-00.json` | SCN-（引用全部） |
| 报表 | `07-分片-report.md` | `_parts/m7-00.json` | QR-/RPT-（引用 BHV-/OBJ-） |

**并行纪律**：
- 每个分片**只准用骨架里的 ID**，不得新增、不得改名。发现骨架缺 ID → 停下来回骨架补，然后所有引用方重跑。
- 分片之间**允许引用对方还没落盘的 ID**——骨架已把名字钉死，引用不会漂。
- 每个分片交付前跑一次自查：`PY OBM check <workdir> --part m1`（`--part` 只校验以该前缀开头的分片）。

各分片最容易被违反的规则（详见各自 prompt，完整枚举见文末）：
- 对象：`aggregateType` 只能 `AGGREGATE_ROOT`；外键写 `AggregateRootRef`、字典写 `DictionaryRef`；`cardinality` 不写 `1:N`；关联必须连通
- 行为：`operationSteps` 是**多行字符串**不是数组；COMMAND 必须有规则或事件；**QUERY 不得有 postconditions**；禁 CRUD 命名
- 规则：`outputType` 是**字符串**不是对象；`ruleType` 无 `CONSTRAINT`/`ROUTING`；`sourceField` 的聚合+属性都要真实存在
- 事件：ID 字段是 **`eventId`/`eventName`**；必须有 `subscribers`；过去式命名
- 场景：`stepType` 只有 `BEHAVIOR_CALL`/`RULE_EVALUATE`/`EVENT_EMIT`；跨 ≥2 对象、≥5 步、必须有分支；**链路必须无环**（失败路径是终止步）
- 报表：`sourceObjects` 恰好一个 `primary:true`；`having` 是条件对象数组；`objectType=REPORT` 必须有 `reportOptions`；≥2 源对象或 ≥1 聚合列

### 阶段 3 · 三道校验门

```bash
# 门 1：片级（必须先到 0 error）
PY OBM check <workdir> --json
# 门 2：合并级
PY OBM merge <workdir>
# 门 3：全量级
PY OBM quality <workdir>
```

门 1 必过（`check` 有 error 会以退出码 1 结束）。门 2 出现 `REF_UNRESOLVED` / `ID_DUP` 必须修。门 3 的 gate：**无 error，且得分 ≥ 80**。

`merge` 会自动做三向双向引用同步（规则↔行为、事件↔行为、报表↔行为）并补反向，记 `BIDIRECT_FIX`。**不要依赖它**——写的时候两边就要对上，否则说明分片之间已经不一致了。

### 阶段 4 · 定向修复

读 `prompts/08-修复.md`，按 `check-report.json` / `quality-report.json` 里的 `ptr` + `refs` 精确改分片。

纪律：
- 只改报告指名的位点，不重写分片
- **永远不手改 YAML**（会被下一次 `toyaml` 覆盖）
- 一轮最多改 10 条，改完立刻重跑
- `BUDGET_LOW` 最该重视（说明建模没到位），`AP_THIN`(info) 可接受

循环：`check → merge → quality` 直到 门1 0 error、门3 无 error 且 ≥80。

### 阶段 5 · 出 YAML 与图谱

```bash
PY OBM toyaml  <workdir>
PY OBM render  <workdir> --title "<行业>·<业务域> 本体模型"
PY OBM selftest<workdir>
```

`toyaml` 是唯一序列化出口：按 schema 的 `allowed` 排序键、剥离 `_` 开头的旁挂键、`allow_unicode` 落中文。

`render` 会先跑一遍 viewer 自带的九类校验（`yaml/validation-report.json`），再生成 4~5MB 的单文件 HTML（ECharts + Mermaid 内联，双击即开）。`selftest` 是冒烟门：HTML >500KB、`typeof echarts` / `mermaid` / `id="chart"` 存在、`__DATA__` 占位符已被替换、节点数 >0。

**两个实操注意**：
- `--yaml-dir` 传**绝对路径**（相对路径会被 viewer 按自己的 cwd 解析而报"YAML 目录不存在"）。
- HTML 若被预览器/杀软占用会报 `PermissionError`，换一个全新的输出目录（如 `web/`）重新渲染即可，不要反复覆盖同一路径。

**交付前必须 `selftest` 0 error。**

### 阶段 6 · 交付

`present_files` 呈现：

1. `<workdir>/yaml/ontology-graph.html`（主交付物，第一个）
2. 6 个 YAML
3. `quality-report.json`（说明模型规模与质量分）

回复里给：模型规模表（8 个集合各自数量）、质量分、3~5 句"这个模型建了什么"的概述、以及已知 warning 的解释（哪些是设计取舍、哪些是真问题）。

## 失败处理

| 现象 | 处置 |
|---|---|
| 骨架数量远低于预算下限 | 停，回阶段 1 补节点。不要在分片阶段硬凑 |
| 反复 `REF_UNKNOWN` | 说明骨架与分片脱节。以骨架为准改分片；骨架真缺就补骨架后**全部重跑 check** |
| `quality` 得分 < 70 | 几乎总是"模型太薄"：先看 `BUDGET_LOW` 与 `COV_OBJ_NO_BEHAVIOR`，补行为/规则/事件，不要靠删节点提分 |
| `BUDGET_HARD` 超硬上限 | 合并同义节点（如"生产订单"与"工单"只留一个），不是删语义 |
| 场景全被 `AP_SCN_SHORT` | 场景写成了行为列表。按 `00-画像.md` 第 2 节的分支重新展开，把"驳回/返工/终止"路径写进 `nextSteps` |
| `render` 报 YAML 结构错误 | 别改 YAML，回去改分片 JSON 再 `toyaml` |
| `SMOKE_SIZE` 失败 | `assets/` 下 echarts/mermaid 丢了，检查 `<SKILL>/assets/` |
| 用户中途改需求（加/减对象） | 改 `skeleton.json` → 重跑受影响分片 → 重跑三道门。骨架是唯一可改名的入口 |

## 接手既有模型

```bash
PY OBM init   <newworkdir> --industry "..." --domain "..."
PY OBM fromyaml <newworkdir> --yaml-dir <既有yaml目录>
PY OBM check  <newworkdir> --json
```

`fromyaml` 会把 6 个 YAML 反解成 `_parts/*.json` 并生成 `skeleton.json`。随后即可在这套分片上增量改。往返一致性已验证：`YAML → fromyaml → merge → toyaml` 语义差异为 0。

## 字段与枚举（唯一真相来源：`schema/closed-fields.json` 的 `enums`）

- **模型版本**：OBJECT 2.1 / BEHAVIOR 1.0 / RULE 2.0 / EVENT 1.0 / SCENARIO 4.0 / REPORT 1.0
- **aggregateType**：只有 `AGGREGATE_ROOT`
- **associationType**：只有 `REFERENCE` `DEPENDENCY`
- **cardinality**（关联与实体共用）：`ONE` `ZERO_OR_ONE` `ONE_OR_MORE` `ZERO_OR_MORE` `ONE_TO_ONE` `ONE_TO_MANY` `MANY_TO_ONE` `MANY_TO_MANY`
- **attribute.type**（13 值）：`String` `Integer` `Decimal` `Date` `DateTime` `Boolean` `Enum` `Money` `Object` `Void` `ValueObject` `AggregateRootRef` `DictionaryRef`
- **enforcedAt**：`ON_CREATE` `ON_UPDATE` `ON_DELETE` `ALWAYS`
- **behaviorType**：`COMMAND` `QUERY` `EVENT_HANDLER`
- **triggerType**：`USER_ACTION` `SYSTEM` `EVENT` `EXTERNAL`
- **ruleType**：`VALIDATION` `CALCULATION` `DERIVATION` `TRANSFORMATION` `RISK` `EVENT_DRIVEN`
- **executionMode**：`SYNC` `ASYNC`
- **producerType / subscriberType**：`BEHAVIOR` `RULE`
- **ordering**：`AT_LEAST_ONCE` `EXACTLY_ONCE` `BEST_EFFORT`
- **stepType**：`BEHAVIOR_CALL` `RULE_EVALUATE` `EVENT_EMIT`
- **objectType**（报表）：`DETAIL_QUERY` `LIST_QUERY` `STATISTICAL_QUERY` `REPORT`
- **aggregateFunction**：`NONE` `SUM` `COUNT` `AVG` `MAX` `MIN` `COUNT_DISTINCT`
- **joinType**：`INNER` `LEFT` `RIGHT`
- **operator**：`EQ` `NE` `GT` `GE` `LT` `LE` `IN` `NOT_IN` `LIKE` `BETWEEN` `IS_NULL` `IS_NOT_NULL`

## 八个最容易踩的坑（都是跑验证才暴露的真坑）

1. `aggregateType` 不能写 `REFERENCE`/`ENTITY`。主数据也是 `AGGREGATE_ROOT`，用 `tags: ["主数据"]` 区分。
2. 外键属性 `type` 必须写 `AggregateRootRef` 并配 `targetAggregate`；字典属性写 `DictionaryRef` 并配 `dictionaryRef`。写成 `String`/`Enum` 都会报错。
3. 属性没有 `Text` 类型，长文本用 `String`。
4. `cardinality` 不能写 `1:N`，要写 `ONE_TO_MANY`。**实体（entities）的 cardinality 也走同一套枚举**。
5. `QUERY` 行为不得有 `postconditions`（查询不改变状态），否则 `render` 报 `M2_QUERY_POST`。
6. **场景步骤链路必须无环**。判定失败要指向终止步（`nextSteps: []`），不能指回前面的步骤，否则 `render` 报 `M4_CYCLE`。返工回路单独建场景。
7. 报表的 `having` 是**条件对象数组**（`[{"field","operator","value"}]`），不是字符串；`pagination` 是对象；`objectType=REPORT` 必须有 `reportOptions`，其他类型不能有。
8. 规则 `inputParams[].sourceField` 与事件 `payload[].sourceField` 会做**字段级校验**：`OBJ-XXX.attrName` 两段都要真实存在。需要"客户价值/孤儿行数"这类输入，先回 M1 把属性建出来。

## 已验证案例

- **制造业·供应链**（参考华为 ISC / SCOR / ERP-SCM-APS）：30 聚合 / 45 关联 / 4 字典 / 52 行为 / 19 规则 / 22 事件 / 8 场景 / 16 报表，质量分 84，HTML 4.8MB。回归：`fromyaml → merge → toyaml` 语义差异 0。
- **手机制造·产品研发**（参考 IPD / PLM）：30 聚合 / 45 关联 / 6 字典 / 59 行为 / 20 规则 / 20 事件 / 8 场景 / 15 报表，质量分 98，能力域覆盖 8/8，证据锚点 99/99，HTML 4.7MB（152 节点 / 307 边）。产物在 `<项目>/ontology_rd/`。
