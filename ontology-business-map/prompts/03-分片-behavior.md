# 阶段 2-B：行为模型分片（m2 → `<workdir>/_parts/m2-00.json`）

## 输入

- `<workdir>/skeleton.json`：`kind == "BEHAVIOR"` 的 ID 为本片节点；`kind == "OBJECT"` 的 ID 用于 `ownerEntity`
- `<workdir>/profile.md` 第 5 节（业务动作候选）

## 输出文件

`<workdir>/_parts/m2-00.json`（超过 30 个可拆片，同 ID 不得跨片重复）

```json
{
  "model": "BEHAVIOR",
  "domain": "产品研发",
  "behaviors": [ ... ]
}
```

## 字段封闭集

必填：`id` `name` `ownerEntity` `behaviorType` `triggerType` `operationSteps`
可选：`alias` `preconditions` `postconditions` `appliedRules` `requiredPermissions` `producedEvents` `syncTriggers` `queryReportRef`

| 字段 | 取值/写法 |
|---|---|
| `id` | 骨架里的 BHV- ID |
| `ownerEntity` | **对象（聚合）的 OBJ- ID**，不是实体名。一个行为只能有一个归属聚合 |
| `behaviorType` | **只能取三值**：`COMMAND`（改变状态）\| `QUERY`（只读）\| `EVENT_HANDLER`（事件驱动处理）。**没有 `APPROVAL`、`CALCULATION`**——审批与计算都用 `COMMAND`，事件驱动的用 `EVENT_HANDLER` |
| `triggerType` | **只能取四值**：`USER_ACTION` \| `EVENT` \| `SYSTEM` \| `EXTERNAL`。**没有 `SCHEDULE`**，定时任务写 `SYSTEM` |
| `operationSteps` | **一个多行字符串**（不是数组！），每行一步，编号 `1. 2. 3.` |
| `preconditions` / `postconditions` | 字符串数组，每条一句可判定的状态描述。**`QUERY` 类型不得有 `postconditions`（查询不改变状态），也不建议有 `preconditions`** |
| `appliedRules` | `RULE-` ID 数组，**必须与规则分片的 `reusedBy` 一致**（merge 阶段会自动双向同步并补反向） |
| `producedEvents` | `EVT-` ID 数组，`COMMAND`/`EVENT_HANDLER` 类行为**必填至少 1 个**；`QUERY` 恒为空 |
| `requiredPermissions` | 权限码字符串数组，如 `["RD:CHARTER:SUBMIT"]` |
| `syncTriggers` | 字符串数组，描述同步触发的下游动作（跨系统/跨聚合） |
| `queryReportRef` | `QUERY` 类行为指向 `QR-`/`RPT-` 报表 ID，与报表的 `behaviorRef` **一对一**。一张报表配一个 QUERY 行为，不要两个报表共用一个 |

## 建模要求（按重要性排序）

1. **行为必须有"状态位移"**。写完后问自己：这个行为执行前后，对象状态变了什么？答不出来就说明这是查询或不该存在。
2. **禁止 CRUD 命名**。`name` 用业务动词：`提交评审` `冻结基线` `拆解工单` `发起变更` `关闭问题单`。`create/update/delete/get/list` 开头的命名会被质量检查记为反模式。
3. **COMMAND 必须有规则或事件**。既无 `appliedRules` 又无 `producedEvents` 的 COMMAND 会被记为"纯搬运"，这类行为要么合并进别的行为，要么补上它的判定/广播。
4. **`operationSteps` 写方法而不是写字段赋值**。反例：`1. 设置status=RELEASED`。正例：`1. 校验BOM已冻结 2. 生成基线编号 3. 冻结BOM所有版本 4. 发布基线并广播`。
5. **审批类动作（用 `COMMAND`）必须在 `operationSteps` 里写出会签/否决分支**，不通过时怎么走要写清。
6. **`QUERY` 行为保持"无副作用"**：不要写 `postconditions`，`producedEvents` 留空数组，`appliedRules` 留空数组。查询是读，不制造事实。
7. **每个聚合至少 1 个行为**。主数据对象同样需要（如"定义产品线""登记技术平台"），否则质量检查报 `COV_OBJ_NO_BEHAVIOR`。
8. **`preconditions` 用对象状态描述**，例如 `"BOM状态 = 已冻结"`，不要写 `"满足条件"`。

## 与其他分片的引用契约

| 本片写的 | 必须存在于 |
|---|---|
| `ownerEntity` | m1 的 `aggregates[].id` |
| `appliedRules` | m3 的 `rules[].id`（不存在会被 merge 报 REF_UNRESOLVED） |
| `producedEvents` | me 的 `events[].eventId` |
| `queryReportRef` | m7 的 `query_reports[].id` |

**分片并行时，先按骨架 ID 写完，即使对方分片还没落盘**——骨架已经把 ID 钉死了，引用不会漂。

## 交付前自检

- [ ] 无骨架外新增 ID，无 CRUD 命名
- [ ] `behaviorType` 只用 `COMMAND`/`QUERY`/`EVENT_HANDLER`，`triggerType` 只用 `USER_ACTION`/`EVENT`/`SYSTEM`/`EXTERNAL`
- [ ] COMMAND / EVENT_HANDLER 类全部有 `appliedRules` 或 `producedEvents`
- [ ] QUERY 类无 `postconditions`、`producedEvents` 为空
- [ ] `operationSteps` 是多行字符串，每步是业务动作
- [ ] 每个聚合至少 1 个行为
- [ ] QUERY 类有 `queryReportRef`，且与报表一一对应
