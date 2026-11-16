# 阶段 2-E：场景模型分片（m4 → `<workdir>/_parts/m4-00.json`）

## 输入

- `<workdir>/skeleton.json`：`kind == "SCENARIO"` 的 ID
- `<workdir>/profile.md` 第 2 节（端到端主流程 + 分支）
- 已落盘的行为/规则/事件分片（用于取准确 ID）

## 输出文件

`<workdir>/_parts/m4-00.json`

```json
{
  "model": "SCENARIO",
  "domain": "产品研发",
  "event_scenarios": [ ... ]
}
```

## 字段封闭集

必填：`id` `name` `description` `sourceObjectRef` `targetObjectRefs` `triggerEventRef` `steps`
可选：`alias` `preconditions` `postconditions`

| 字段 | 取值/写法 |
|---|---|
| `id` | 骨架里的 SCN- ID |
| `sourceObjectRef` | 起点对象的 `OBJ-` ID（**单个**，不是数组） |
| `targetObjectRefs` | 终点对象 ID 数组，2~4 个 |
| `triggerEventRef` | 触发事件 `EVT-` ID（场景由一个事件启动，这是端到端的"发令枪"） |
| `preconditions` / `postconditions` | 字符串数组 |

### steps 元素

必填：`stepId` `stepType` `nextSteps`
可选：`behaviorRef` `ruleRef` `eventRef` `gatewayType` `condition` `timeout` `waitFor` `branches`

- `stepId`：`S01` `S02`…（本场景内递增，从 S01 开始连续）
- `stepType`：**只能取三值** `BEHAVIOR_CALL`（调行为）\| `RULE_EVALUATE`（做判定/分支）\| `EVENT_EMIT`（抛出事件）。
  **不得使用 `GATE`、`END`、`GATEWAY`、`EVENT_WAIT`、`EXTERNAL_CALL`** —— 人工决策门用 `RULE_EVALUATE` 表达，流程结束就是 `nextSteps: []`
- `behaviorRef`/`ruleRef`/`eventRef`：按 `stepType` 填对应 ID，**必须存在**
- `gatewayType`：仅 `RULE_EVALUATE` 用，写 `EXCLUSIVE`（排他分支）
- `condition`：分支步骤必填，写判定条件（如 `评审通过`）

## 建模要求

1. **场景是跨对象的端到端流转，不是行为的复述**。判据：一个场景必须跨 **≥2 个对象**、**≥5 个步骤**，否则质量检查报 `AP_SCN_SHORT` / `AP_SCN_NARROW`（跨 3 个以上对象更佳）。
2. **每个场景必须有分支**：至少一个 `RULE_EVALUATE` 步骤，`nextSteps` 出现两条路径，并用 `condition` 区分。
3. **步骤链路必须是 DAG，绝对不能成环**（校验器会报 `M4_CYCLE`）。这是最容易犯错的地方：
   - ✅ 正确：判定不通过 → 指向一个**终止步**（`nextSteps: []`），`condition` 写明"退回 X，本场景在此终止"。
   - ❌ 错误：判定不通过 → 指回前面的步骤形成返工回路。
   - 返工/驳回的完整回路应该**单独建一个场景**表达，不要塞进正常闭环场景里。
4. **场景数量按骨架来（4~8 个）**，覆盖主流程的每个阶段段落。例如产品研发：路标→立项、立项→计划、设计→冻结、冻结→量产、验证→认证、变更闭环、需求变更闭环、上市→退市。
5. **`targetObjectRefs` 里的对象必须真的在步骤里出现过**（作为某步行为的 `ownerEntity`，或作为 `sourceObjectRef`）。否则校验报 `M4_TARGET_INDIRECT`。**显式把终点对象挂到一个步骤上**，别指望它是"隐含结果"。
6. **行为与事件合并为一个阶段**。行为产出的事件用一步 `EVENT_EMIT` 表达即可；**不要为"等待事件"单独建步骤**。
7. **`triggerEventRef` 是场景的"发令枪"**，通常是上游场景产生的事件。上游没有本场景，会导致 `M4_TRIGGER_EXTERNAL`（warning，可接受）；但如果该事件在本场景步骤里出现又不产生它，就会矛盾——要么在本场景 `EVENT_EMIT` 它，要么承认它是外部前置。
8. **步骤 ID 连续**：`S01..SN` 中间不跳号，图才好渲染。

## 与其他分片的引用契约

| 本片写的 | 必须存在于 |
|---|---|
| `sourceObjectRef` / `targetObjectRefs` | m1 的 `aggregates[].id` |
| `triggerEventRef` | me 的 `events[].eventId` |
| `steps[].behaviorRef` | m2 的 `behaviors[].id` |
| `steps[].ruleRef` | m3 的 `rules[].id` |
| `steps[].eventRef` | me 的 `events[].eventId` |

## 交付前自检

- [ ] 每个场景跨 ≥2 对象、≥5 步骤
- [ ] `stepType` 只用 `BEHAVIOR_CALL`/`RULE_EVALUATE`/`EVENT_EMIT`
- [ ] 每个场景至少 1 个分支（`nextSteps` 有 2 条路径）
- [ ] **步骤链路是 DAG，无环**（写出所有 `步骤链路` 手推一遍，确认不会回到已出现的步骤）
- [ ] 异常/驳回路径是终止步（`nextSteps: []`），`condition` 已说明
- [ ] `stepId` 从 S01 连续，无跳号
- [ ] `targetObjectRefs` 全部在步骤里显式出现
- [ ] 无"行为+事件"拆成两步的冗余（行为产出的事件已在同一阶段内完成）
