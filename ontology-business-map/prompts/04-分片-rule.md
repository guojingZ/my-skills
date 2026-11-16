# 阶段 2-C：规则模型分片（m3 → `<workdir>/_parts/m3-00.json`）

## 输入

- `<workdir>/skeleton.json`：`kind == "RULE"` 的 ID
- `<workdir>/profile.md` 第 6 节（行业硬约束）
- 行为分片已知的 `appliedRules`（若已落盘，读一遍保证对得上；未落盘就按骨架走）

## 输出文件

`<workdir>/_parts/m3-00.json`

```json
{
  "model": "RULE",
  "domain": "产品研发",
  "rules": [ ... ]
}
```

## 字段封闭集

必填：`id` `name` `ruleType` `description` `inputParams` `outputType` `expression` `reusedBy` `version`
可选：`alias` `externalEngine` `executionMode` `timeout`

| 字段 | 取值/写法 |
|---|---|
| `ruleType` | **只能取六值**：`VALIDATION`（校验）\| `CALCULATION`（计算）\| `DERIVATION`（推导/分类）\| `TRANSFORMATION`（转换）\| `RISK`（风险评级）\| `EVENT_DRIVEN`（事件驱动）。**没有 `CONSTRAINT`、`ROUTING`**——约束用 `VALIDATION`，决策分派用 `DERIVATION` |
| `description` | 一句话说清"在什么情况下判什么、不满足怎么办" |
| `inputParams` | 数组，每项必填 `name` `type` `sourceField`；可选 `required` `description` |
| `outputType` | **字符串**（不是对象！），如 `Boolean` / `Decimal` / `Enum` / `String` / `Integer`。**注意字段名是 `outputType` 不是 `outputParam`** |
| `expression` | 可读判定/计算式，用 `inputParams` 的 name 作变量；条件分支写 `IF ... THEN ... ELIF ... ELSE ...` |
| `reusedBy` | `BHV-` ID 数组，允许**空数组**（merge 会自动从行为的 `appliedRules` 反向补齐） |
| `version` | 字符串，如 `"1.0"` |
| `externalEngine` | 外部引擎名（如 `Drools` / `规则中心`），纯文档化用 |
| `executionMode` | `SYNC` \| `ASYNC` |
| `timeout` | 整数毫秒 |

**事件驱动规则专用字段**（`ruleType=EVENT_DRIVEN` 时才用）：`triggerType` `subscribedEvents` `triggeredBehaviors` `producedEvents` `eventTriggerCondition`。普通五模型价值链规则**不要用** `EVENT_DRIVEN`。

### inputParams 元素

必填：`name` `type` `sourceField`；可选：`required` `description`
- `sourceField`：写成 **`OBJ-<域码>-<对象>.<attributeName>`**，head 必须是 M1 里真实存在的聚合 ID，tail 必须是该聚合真实存在的属性名。**这里会做字段级校验，写错就报 `FIELD_MISSING`**。
- 需要"计数/求和"这类派生输入时，sourceField 仍指向一个真实属性（在 description 里说明是按该字段反向统计），不要凭空编字段。若确实需要新输入（如客户价值分、孤儿行数），**先回 M1 补属性**再写规则。

## 建模要求

1. **规则必须是"可复用判定"，不是流程步骤**。凡是"某单据必须先审批再发布"这种顺序约束，属于场景/行为前置条件，不该建规则。规则要能回答"给我输入，我还你一个判定值"。
2. **一个规则至少被 1 个行为复用**。质量检查会把没人用的规则记为 `COV_RULE_UNUSED`。骨架里排了规则却没行为用，说明规则想多了。
3. **`expression` 要能照着实现**。写 `生命周期阶段 IN (概念, 计划) AND 目标成本 <= 目标售价 * 0.6`，不写 `"满足门禁条件"`。
4. **同一判定只建一条**。多个场景用同一判定，就共享一条规则，用 `reusedBy` 挂多个行为——这就是"规则"和"行为里的 if"的本质区别。
5. **行业硬约束优先**：合规门禁、准入资格、配额上限、版本冻结、变更影响面、成本红线。这类规则是模型体现行业深度的主要载体。
6. **`ruleType` 分布要合理**：全模型 `VALIDATION` 占比不要超过 60%，`CALCULATION`/`DERIVATION` 各至少 2 条，否则模型只是校验清单。

## 与其他分片的引用契约

| 本片写的 | 必须存在于 |
|---|---|
| `reusedBy` | m2 的 `behaviors[].id` |
| `inputParams[].sourceField` | m1 的 `aggregates[].id` + 该聚合的 `attributes[].name`（**字段级强校验**） |

## 交付前自检

- [ ] `outputType` 是字符串，不是对象
- [ ] `ruleType` 只用六值，无 `CONSTRAINT`/`ROUTING`
- [ ] 每条规则至少 1 个 `reusedBy`（或确认行为分片会引用它）
- [ ] `expression` 是可执行表达式，不是自然语言
- [ ] `inputParams[].sourceField` 的聚合 ID 与属性名都能在 m1 里对上
- [ ] `VALIDATION` 占比 ≤ 60%
- [ ] 无骨架外新增 ID
