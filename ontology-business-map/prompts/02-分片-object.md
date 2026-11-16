# 阶段 2-A：对象模型分片（m1 → `<workdir>/_parts/m1-00.json`）

## 输入

- `<workdir>/skeleton.json`：只允许使用其中 `kind == "OBJECT"` 的 ID，**不得新增、不得改名**
- `<workdir>/profile.md` 第 4 节（对象候选）

## 输出文件

`<workdir>/_parts/m1-00.json`（若对象超过 18 个，可拆 `m1-00.json` / `m1-01.json`，同 ID 不得跨片重复）

```json
{
  "model": "OBJECT",
  "domain": "产品研发",
  "aggregates": [ ... ],
  "aggregate_associations": [ ... ],
  "data_dictionaries": [ ... ]
}
```

## 字段封闭集（未列出的键一律报错，下划线开头的旁挂键除外）

### aggregates 元素

必填：`id` `name` `alias` `description` `aggregateType` `attributes`
可选：`lifecycle` `tags` `entities` `valueObjects` `invariants`

- `id`：骨架里的 OBJ- ID
- `name`：中文名；`alias`：英文名（大驼峰）
- `description`：一句话职责，写"承载什么、被谁用"，不写属性罗列
- **`aggregateType`：只能是 `AGGREGATE_ROOT`**（规范 M1 未开放其他取值；主数据对象也写这个，不要自造 `REFERENCE`/`ENTITY`）
- `lifecycle`：状态流转，形如 `草稿 → 评审中 → 已发布 → 已冻结 → 已废弃`
- `tags`：中文标签数组，用于图谱分组（如 `["主数据","研发"]`）

### attributes（聚合直挂属性）

必填：`name` `label` `type`
可选：`required` `unique` `defaultValue` `enumValues` `dictionaryRef` `targetAggregate` `valueObjectRef` `refRules` `description`

**`type` 只能取以下 13 个值之一**：

`String` `Integer` `Decimal` `Date` `DateTime` `Boolean` `Enum` `Money` `Object` `Void` `ValueObject` `AggregateRootRef` `DictionaryRef`

- **没有 `Text`**，长文本也用 `String`
- **外键必须写 `AggregateRootRef`** 且填 `targetAggregate`（不要写 `String` + targetAggregate）
- **字典引用必须写 `DictionaryRef`** 且填 `dictionaryRef`（不要写 `Enum` + dictionaryRef）
- **`Enum` 必须给非空且不重复的 `enumValues`**，用于值域封闭的小枚举
- `ValueObject` 类型须配 `valueObjectRef`
- 引用键名字**不能改**：是 `dictionaryRef` / `targetAggregate` / `valueObjectRef`

### entities / valueObjects（聚合内子对象）

`entities` 必填：`name` `localId` `cardinality`；可选：`alias` `description` `cascadeDelete` `attributes`
- `localId`：聚合内局部编号（同一聚合内唯一，不必全局唯一）
- **`cardinality` 与关联同一套枚举**：`ONE` `ZERO_OR_ONE` `ONE_OR_MORE` `ZERO_OR_MORE` `ONE_TO_ONE` `ONE_TO_MANY` `MANY_TO_ONE` `MANY_TO_MANY`。**不要写 `1:N` 这种简写**

`valueObjects` 必填：`name` `immutable`；可选：`alias` `description` `equalityFields` `attributes`

两者内部的 `attributes` 字段集与聚合级 `attributes` 完全一致。

### invariants（不变式）

必填：`name` `expression`；可选：`description` `violationMessage` `enforcedAt`
- `expression`：可读判定式，如 `releaseDate >= freezeDate`
- `enforcedAt`：`ON_CREATE` `ON_UPDATE` `ON_DELETE` `ALWAYS`（**不是 CREATE/UPDATE/ANY**）
- 不变式是**对象内自洽**的约束（跨对象判定放 M3 规则），不要混淆

### aggregate_associations 元素

必填：`id` `sourceAggregate` `targetAggregate` `cardinality`
可选：`associationType` `sourceRole` `targetRole` `referenceField` `description`

- **`associationType` 只能取 `REFERENCE` 或 `DEPENDENCY`**。不要自造 `COMPOSITION`/`AGGREGATION`/`ASSOCIATION`——强/弱归属用 `description` 与 `sourceRole` 表达
- `cardinality`：同上 8 个值，**不要写 `1:N`**
- `sourceRole` / `targetRole`：关系两端的业务角色词（如 `包含` / `被包含于`）
- `referenceField`：承载外键的属性名（必须真实存在于对应聚合的属性里）

### data_dictionaries 元素

必填：`id` `name` `types`；可选：`description`
- `types` 每项必填：`code` `name`；可选：`description`
- 注意集合键名是 **`types`**，不是 `items`

## 建模要求

1. **聚合边界按事务一致性划**：一次业务操作要保证一起改的，放同一个聚合。跨聚合只放 ID 引用。
2. **主数据对象必须建**：组织、人员、物料/器件、供应商、工厂/产线、仓库、编码规则这类只被引用的对象，同样标 `aggregateType: AGGREGATE_ROOT`，用 `tags` 标 `主数据` 区分。
3. **每个聚合至少 5 个属性**，核心单据类聚合 8~15 个。属性只写业务有意义的，不要凑数（尤其不要写 createTime/updateTime/creator 这类技术字段）。
4. **枚举优先走字典**：同一组枚举被两个以上对象用到，就抽成 `DICT-`，属性类型写 `DictionaryRef`。
5. **关联数 ≥ 对象数 × 1.2**，且必须连通（不能有孤岛子图）。
6. **不变式每个核心聚合 1~2 条**，写真正会拦流程的那条。
7. `targetAggregate` / `dictionaryRef` / `sourceAggregate` / `targetAggregate` 引用的 ID **必须已存在于骨架**，否则校验报 `REF_UNKNOWN`。
8. **规则要读的属性必须真的存在**：M3 的 `inputParams[].sourceField` 会做字段级校验，写 `OBJ-RD-XXX.attrName` 时属性名要和这里一字不差。所以"客户价值/市场规模/实现成本/孤儿行数"这类计算输入，必须在对象上先建出来。

## 交付前自检

- [ ] 无骨架外新增 ID
- [ ] `aggregateType` 全部是 `AGGREGATE_ROOT`
- [ ] 属性 `type` 全部在 13 个合法值内，无 `Text`
- [ ] 外键写 `AggregateRootRef` + `targetAggregate`；字典写 `DictionaryRef` + `dictionaryRef`
- [ ] 所有 `Enum` 属性都有非空且不重复的 `enumValues`
- [ ] 关联 `associationType` 只用 `REFERENCE`/`DEPENDENCY`，`cardinality` 不用 `1:N` 简写
- [ ] `dictionaryRef` / `targetAggregate` 用的都是骨架里的 ID
- [ ] 关联连通，无孤岛
- [ ] 每个聚合 ≥ 5 属性，核心单据 ≥ 8
- [ ] 结构类对象（BOM、版本、基线、变更单）已建模，没有用扁平字段糊过去
