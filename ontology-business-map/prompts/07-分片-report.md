# 阶段 2-F：报表模型分片（m7 → `<workdir>/_parts/m7-00.json`）

## 输入

- `<workdir>/skeleton.json`：`kind == "REPORT"` 的 ID
- m1 的对象与属性、m2 的 QUERY 行为（报表必须挂在行为上）

## 输出文件

`<workdir>/_parts/m7-00.json`

```json
{
  "model": "REPORT",
  "domain": "产品研发",
  "query_reports": [ ... ]
}
```

## 字段封闭集

必填：`id` `name` `alias` `objectType` `description` `behaviorRef` `sourceObjects` `resultColumns` `version`
可选：`joins` `parameters` `conditions` `groupBy` `having` `orderBy` `pagination` `reportOptions`

| 字段 | 取值/写法 |
|---|---|
| `id` | 骨架里的 `QR-` 或 `RPT-` ID（两种前缀都合法） |
| `objectType` | **只能取四值**：`DETAIL_QUERY`（明细查询）\| `LIST_QUERY`（列表）\| `STATISTICAL_QUERY`（统计）\| `REPORT`（报表） |
| `behaviorRef` | **必填**，指向一个 `QUERY` 类型行为（报表由行为承载，不凭空存在） |
| `sourceObjects` | 数组，每项必填 `objectRef` `alias` `primary`（布尔）。**必须恰好一个 `primary: true`**；`alias` 非空且报表内唯一 |
| `joins` | 数组，每项：`joinType`（`INNER`/`LEFT`/`RIGHT`）`leftSource` `rightSource` `condition`；左右两端**只能用已声明的 alias** |
| `parameters` | 数组，每项必填 `name` `dataType`；可选 `label` `required` |
| `conditions` | 数组，每项必填 `field` `operator` `value`；`operator` 取 `EQ` `NE` `GT` `GE` `LT` `LE` `IN` `NOT_IN` `LIKE` `BETWEEN` `IS_NULL` `IS_NOT_NULL` |
| `resultColumns` | 数组，每项必填 `name` `label` `dataType` `sourceExpression` `aggregateFunction`；可选 `nullable` `visible` `sortable` |
| `groupBy` | **字符串数组**（列名或 alias 表达式） |
| `having` | **条件对象数组**（不是字符串！）：`[{"field": "milestoneCount", "operator": "GT", "value": "0"}]` |
| `orderBy` | **字符串数组**，如 `["onTimeRate ASC"]` |
| `pagination` | **对象**：`{"enabled": true, "defaultPageSize": 20, "maxPageSize": 200}` |
| `reportOptions` | 对象。**`objectType=REPORT` 必须提供**（如 `{"exportExcel": true, "exportPdf": true, "drillDown": "QR-XXX", "chartType": "bar"}`）；**其他三种 `objectType` 不要提供非空 reportOptions** |
| `version` | 字符串 |

- `dataType`：`String` `Integer` `Decimal` `Date` `DateTime` `Money` `Boolean`
- `aggregateFunction`：`NONE` `SUM` `COUNT` `AVG` `MAX` `MIN` `COUNT_DISTINCT`（**没有 `RATIO`**，比率用 `NONE` + 表达式算）
- `sourceExpression`：形如 `p.productName`、`SUM(b.unitCost * b.qty)`，用 `sourceObjects` 的 alias 前缀

## 建模要求

1. **每个报表至少 2 个 sourceObjects，或至少 1 个聚合列（`aggregateFunction != NONE`）**，否则质量检查报 `AP_RPT_TRIVIAL`（近乎原始清单）。
2. **报表要能回答管理问题**，不是"字段清单"。命名用管理语言：`项目进度达成率` `BOM变更影响面` `物料复用率` `研发投入产出比` `缺陷逃逸率`。避免 `XX列表` `XX明细`。
3. **必须有 `behaviorRef`**，且目标行为 `behaviorType` 应为 `QUERY`。若骨架里没排 QUERY 行为，回骨架补，不要在报表里编造。
4. **指标类报表必须写出计算口径**：`resultColumns` 里每个指标列都要有 `sourceExpression` + `aggregateFunction`，能让人照着算出来。
5. **`parameters` 让报表可复用**：时间区间、项目、产品线、组织，至少给 1~2 个。
6. **报表数量按骨架（10~20）**，覆盖三类：
   - 过程类（进度、达成率、周期）
   - 质量类（缺陷、返工、逃逸率）
   - 成本/效益类（目标成本达成、研发投入产出、物料复用）
7. 一个报表只用 1 个对象也能成立，但要有聚合列（如按项目 COUNT 缺陷），否则合并成条件查询即可。

## 与其他分片的引用契约

| 本片写的 | 必须存在于 |
|---|---|
| `behaviorRef` | m2 的 `behaviors[].id`（且 `behaviorType=QUERY`） |
| `objectType` / `sourceObjects[].objectRef` | m1 的 `aggregates[].id` |

**双向闭合**：行为分片的 `queryReportRef` 与本片 `behaviorRef` 互为镜像，`obm.py merge` 会自动同步并记 `BIDIRECT_FIX`。写的时候两边对上。

## 交付前自检

- [ ] 每个报表 ≥2 源对象 或 ≥1 聚合列
- [ ] `sourceObjects` 恰好一个 `primary: true`，alias 非空且唯一
- [ ] `objectType` 用四值之一；`REPORT` 有 `reportOptions`，其余三种没有
- [ ] `having` 是条件对象数组，不是字符串
- [ ] `pagination` 是对象，不是字符串
- [ ] `aggregateFunction` 是 7 个枚举值之一，没有 `RATIO`
- [ ] 每列 `sourceExpression` 带 alias 前缀且 alias 在 `sourceObjects` 里存在
- [ ] 每张报表对应一个独立的 `QUERY` 行为（一对一，不共用）
- [ ] 无骨架外新增 ID
