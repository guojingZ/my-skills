# 阶段 2-D：事件模型分片（me → `<workdir>/_parts/me-00.json`）

## 输入

- `<workdir>/skeleton.json`：`kind == "EVENT"` 的 ID
- 各对象的状态机（`lifecycle`）与行为的 `postconditions`——**事件几乎总是状态迁移的产物**

## 输出文件

`<workdir>/_parts/me-00.json`

```json
{
  "model": "EVENT",
  "domain": "产品研发",
  "events": [ ... ]
}
```

## 字段封闭集

必填：`eventId` `eventName` `producerType` `producerBehaviorRef` `producerEntityRef` `payload` `ordering` `version`
可选：`alias` `description` `triggerCondition` `subscribers`

**注意：事件的 ID 字段名是 `eventId`（不是 `id`），名称字段是 `eventName`（不是 `name`）。**

| 字段 | 取值/写法 |
|---|---|
| `eventId` | 骨架里的 EVT- ID |
| `producerType` | `BEHAVIOR`（行为产生，最常见）\| `SYSTEM` \| `EXTERNAL` |
| `producerBehaviorRef` | `BHV-` ID；`producerType=BEHAVIOR` 时必填 |
| `producerEntityRef` | `OBJ-` ID，事件所属聚合 |
| `triggerCondition` | 字符串，说明何时触发（如 `BOM状态迁移到已冻结时`） |
| `payload` | 数组，每项必填 `name` `type`；可选 `required` `description` `sourceField` |
| `subscribers` | 数组，每项必填 `subscriberType`；可选 `subscriberBehaviorRef` `subscriberRuleRef` `priority` |
| `ordering` | 字符串，同一聚合内事件的顺序约定（如 `按 ECO 编号升序`，或 `无强序`） |
| `version` | 字符串 |

- `subscriberType`：`BEHAVIOR`（触发下游行为）\| `RULE`（触发判定）\| `SYSTEM`（外部系统）\| `NOTIFICATION`（通知）
- `priority`：整数，越小越先

## 建模要求

1. **事件是"已发生的事实"，用过去式命名**：`Charter已批准` `BOM已冻结` `变更单已生效` `样机测试已通过`。不用"批准Charter"这种动词短语。
2. **每个 COMMAND/APPROVAL 行为产出至少 1 个事件**。这是链路的骨架：没有事件，下游就是靠人打电话。
3. **每个事件至少 1 个订阅者**。没人订阅的事件是死事件，质量检查记 `COV_EVENT_NO_SUB`。如果确实只是对外广播，写 `{"subscriberType": "SYSTEM", "priority": 10}` 并配 `description` 说明。
4. **payload 只带下游真正需要的字段**，5~10 个即可，不要整单透传。至少要有一个业务主键（如 `ecoNo`、`bomId`）。
5. **事件要覆盖"跨域边界"**：研发→制造、研发→采购、研发→服务，这类跨域事件是本体模型最有价值的部分，务必建。
6. **`ordering` 不能全写"无强序"**，至少对同一聚合的状态迁移事件给出顺序依据，否则下游会乱序处理。

## 与其他分片的引用契约

| 本片写的 | 必须存在于 |
|---|---|
| `producerBehaviorRef` | m2 的 `behaviors[].id` |
| `producerEntityRef` | m1 的 `aggregates[].id` |
| `subscribers[].subscriberBehaviorRef` | m2 的 `behaviors[].id` |
| `subscribers[].subscriberRuleRef` | m3 的 `rules[].id` |

**双向闭合**：行为分片的 `producedEvents` 与本片的 `producerBehaviorRef` 必须互为镜像。两边不一致时，`obm.py merge` 会以行为侧为准自动同步并记 `BIDIRECT_FIX`，但**不要依赖它修复**——写的时候就要对上。

## 交付前自检

- [ ] 字段名用的是 `eventId` / `eventName`
- [ ] 每个事件都有 `subscribers` 且非空
- [ ] 每个 COMMAND/APPROVAL 行为都有对应事件（与 m2 交叉核对 `producedEvents`）
- [ ] 命名全部是过去式事实
- [ ] payload 有业务主键
