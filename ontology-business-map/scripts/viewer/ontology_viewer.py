# -*- coding: utf-8 -*-
"""本体模型可视化生成器（通用版）。

读取一个目录下的本体 YAML（按 model_type 自动识别，不依赖文件名），完成：
  1) 结构 / 枚举 / 引用 / 字段级 / 双向 / 场景链路 / 图结构 / 覆盖率 八类交叉校验；
  2) 归一化为严格 JSON（供审阅与复用）；
  3) 生成自包含单文件 HTML（内嵌 ECharts 力导向图 + Mermaid 指标归因树）。

用法：
    python ontology_viewer.py <yaml_dir> [选项]

选项：
    -o/--outdir DIR      输出目录，缺省与 yaml_dir 相同
    --html NAME          HTML 文件名，缺省 ontology-graph.html
    --json NAME          JSON 文件名，缺省 ontology-model.json
    --strict             存在 error 级问题时不出图，仅输出报告（CI 用）
    --vendor DIR         ECharts / Mermaid 所在目录，缺省脚本同级 vendor/
    --title TEXT         页面标题，缺省 "<domain> 本体知识网络"
    -q/--quiet           仅输出结论行
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 PyYAML，请先安装：pip install pyyaml\n")
    sys.exit(2)

# ---------------------------------------------------------------- 常量

MODEL_KINDS = {
    "OBJECT": "m1",
    "BEHAVIOR": "m2",
    "RULE": "m3",
    "EVENT": "me",
    "SCENARIO": "m4",
    "REPORT": "m7",
}

ROOT_FIELDS = {
    "OBJECT": {"model_type", "version", "domain", "aggregates", "aggregate_associations", "data_dictionaries"},
    "BEHAVIOR": {"model_type", "version", "domain", "behaviors"},
    "RULE": {"model_type", "version", "domain", "rules"},
    "EVENT": {"model_type", "version", "domain", "events"},
    "SCENARIO": {"model_type", "version", "domain", "event_scenarios"},
    "REPORT": {"model_type", "version", "domain", "query_reports"},
}
MODEL_VERSION = {"OBJECT": "2.1", "BEHAVIOR": "1.0", "RULE": "2.0", "EVENT": "1.0", "SCENARIO": "4.0", "REPORT": "1.0"}
BANNED_ROOT = {"industry", "business_domain", "domain_code", "description", "tags", "model_boundary_note",
               "boundary_note", "metric_summary", "report_summary", "cross_model_references"}

ENUMS = {
    "aggregateType": {"AGGREGATE_ROOT"},
    "associationType": {"REFERENCE", "DEPENDENCY"},
    "cardinality": {"ONE", "ZERO_OR_ONE", "ONE_OR_MORE", "ZERO_OR_MORE",
                    "ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"},
    "attributeType": {"String", "Integer", "Decimal", "Date", "DateTime", "Boolean", "Enum",
                      "Money", "Object", "Void", "ValueObject", "AggregateRootRef", "DictionaryRef"},
    "enforcedAt": {"ON_CREATE", "ON_UPDATE", "ON_DELETE", "ALWAYS"},
    "behaviorType": {"COMMAND", "QUERY", "EVENT_HANDLER"},
    "triggerType": {"USER_ACTION", "SYSTEM", "EVENT", "EXTERNAL"},
    "ruleType": {"VALIDATION", "CALCULATION", "DERIVATION", "TRANSFORMATION", "RISK", "EVENT_DRIVEN"},
    "executionMode": {"SYNC", "ASYNC"},
    "producerType": {"BEHAVIOR", "RULE"},
    "subscriberType": {"BEHAVIOR", "RULE"},
    "ordering": {"AT_LEAST_ONCE", "EXACTLY_ONCE", "BEST_EFFORT"},
    "stepType": {"BEHAVIOR_CALL", "RULE_EVALUATE", "EVENT_EMIT"},
    "objectType": {"DETAIL_QUERY", "LIST_QUERY", "STATISTICAL_QUERY", "REPORT"},
}

TYPE_INFO = OrderedDict([
    ("object", ("对象", "#3B6FE0")),
    ("behavior", ("行为", "#E4573D")),
    ("rule", ("规则", "#E8A33D")),
    ("event", ("事件", "#8E5CD9")),
    ("scenario", ("场景", "#17A673")),
    ("report", ("报表", "#5B6472")),
])

EDGE_INFO = OrderedDict([
    ("assoc", ("聚合关联", "#A8B7CE")),
    ("owner", ("作用于对象", "#E4573D")),
    ("applies", ("引用规则", "#E8A33D")),
    ("produces", ("产生事件", "#8E5CD9")),
    ("consumes", ("订阅事件", "#B995E6")),
    ("binds", ("查询绑定", "#7FA6D9")),
    ("source", ("数据来源", "#5B6472")),
    ("scope", ("涉及对象", "#17A673")),
    ("step", ("流程编排", "#17A673")),
])

ID_PREFIX_TYPE = [("OBJ-", "object"), ("BHV-", "behavior"), ("RULE-", "rule"),
                  ("EVT-", "event"), ("SCN-", "scenario"), ("QR-", "report"), ("RPT-", "report")]

STEP_TAG = {"BEHAVIOR_CALL": ("行为", "behavior"), "RULE_EVALUATE": ("规则", "rule"), "EVENT_EMIT": ("事件", "event")}


def guess_type(node_id):
    for pref, typ in ID_PREFIX_TYPE:
        if node_id.startswith(pref):
            return typ
    return "object"


# ---------------------------------------------------------------- 校验容器

class Report:
    def __init__(self):
        self.items = []

    def add(self, level, code, message, refs=None):
        self.items.append({"level": level, "code": code, "message": message, "refs": refs or []})

    def error(self, code, message, refs=None):
        self.add("error", code, message, refs)

    def warn(self, code, message, refs=None):
        self.add("warn", code, message, refs)

    @property
    def errors(self):
        return [i for i in self.items if i["level"] == "error"]

    @property
    def warnings(self):
        return [i for i in self.items if i["level"] == "warn"]


# ---------------------------------------------------------------- 加载

def load_models(yaml_dir, rep):
    """扫描目录，按 model_type 自动归位；返回 {kind: (filename, data)}。"""
    if not os.path.isdir(yaml_dir):
        raise SystemExit("错误：YAML 目录不存在 -> %s" % yaml_dir)
    files = [f for f in sorted(os.listdir(yaml_dir)) if f.lower().endswith((".yaml", ".yml"))]
    if not files:
        raise SystemExit("错误：目录内没有 .yaml/.yml 文件 -> %s" % yaml_dir)

    models, seen_kind, source_files = {}, {}, []
    for fname in files:
        path = os.path.join(yaml_dir, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception as exc:  # noqa: BLE001
            rep.error("YAML_PARSE", "%s 解析失败：%s" % (fname, exc), [fname])
            continue
        if not isinstance(data, dict):
            rep.warn("YAML_SHAPE", "%s 根不是映射结构，已跳过" % fname, [fname])
            continue
        mt = data.get("model_type")
        kind = MODEL_KINDS.get(str(mt).upper()) if mt else None
        if not kind:
            rep.warn("MODEL_UNKNOWN", "%s 的 model_type=%r 不在可视化支持的六类模型内，已跳过" % (fname, mt), [fname])
            continue
        if kind in models:
            rep.error("MODEL_DUPLICATE", "%s 与 %s 同为 %s 模型，仅采用前者" % (fname, seen_kind[kind], mt), [fname])
            continue
        models[kind] = (fname, data)
        seen_kind[kind] = fname
        source_files.append(fname)
    return models, source_files


# ---------------------------------------------------------------- 图构建

class Graph:
    def __init__(self):
        self.nodes = OrderedDict()
        self.edges = OrderedDict()

    def node(self, nid, typ, name, **extra):
        if not nid:
            return None
        if nid in self.nodes:
            cur = self.nodes[nid]
            if name and not cur.get("name"):
                cur["name"] = name
            if extra.get("missing") is False:
                cur["missing"] = False
            return cur
        item = {"id": nid, "type": typ, "name": name or nid, "alias": "", "summary": "",
                "group": "", "missing": bool(extra.get("missing", False))}
        item.update({k: v for k, v in extra.items() if k != "missing"})
        self.nodes[nid] = item
        return item

    def ghost(self, nid, rep, origin):
        """悬空引用：仍生成节点，标记 missing，保证图不崩。"""
        if nid in self.nodes:
            return self.nodes[nid]
        rep.error("REF_MISSING", "%s 引用了不存在的 %s" % (origin, nid), [origin, nid])
        return self.node(nid, guess_type(nid), nid, missing=True, summary="缺失引用节点")

    def edge(self, s, t, etype, label, origin="", weight=1):
        if not s or not t or s == t:
            return
        key = "%s|%s|%s" % (s, t, etype)
        if key in self.edges:
            self.edges[key]["weight"] += weight
            return
        self.edges[key] = {"id": key, "source": s, "target": t, "type": etype,
                           "label": label, "weight": weight, "origin": origin}


def build_graph(models, rep):
    g = Graph()
    m1 = models.get("m1", (None, {}))[1]
    m2 = models.get("m2", (None, {}))[1]
    m3 = models.get("m3", (None, {}))[1]
    me = models.get("me", (None, {}))[1]
    m4 = models.get("m4", (None, {}))[1]
    m7 = models.get("m7", (None, {}))[1]

    aggregates = m1.get("aggregates") or []
    behaviors = m2.get("behaviors") or []
    rules = m3.get("rules") or []
    events = me.get("events") or []
    scenarios = m4.get("event_scenarios") or m4.get("scenarios") or []
    reports = m7.get("query_reports") or []

    # ---- 对象
    for agg in aggregates:
        if not isinstance(agg, dict) or not agg.get("id"):
            rep.error("M1_NO_ID", "聚合缺少 id，已跳过")
            continue
        tags = agg.get("tags") or []
        g.node(agg["id"], "object", agg.get("name") or agg["id"],
               alias=agg.get("alias") or "", summary=agg.get("description") or "",
               group=(tags[0] if tags else ""), extra={"lifecycle": agg.get("lifecycle") or ""})
        for ent in agg.get("entities") or []:
            for at in (ent.get("attributes") or []):
                if at.get("type") == "AggregateRootRef" and at.get("targetAggregate"):
                    g.edge(agg["id"], at["targetAggregate"], "assoc", "引用", agg["id"])
        for vo in agg.get("valueObjects") or []:
            for at in (vo.get("attributes") or []):
                if at.get("type") == "AggregateRootRef" and at.get("targetAggregate"):
                    g.edge(agg["id"], at["targetAggregate"], "assoc", "引用", agg["id"])
        for at in (agg.get("attributes") or []):
            if at.get("type") == "AggregateRootRef" and at.get("targetAggregate"):
                g.edge(agg["id"], at["targetAggregate"], "assoc", "引用", agg["id"])

    for asso in m1.get("aggregate_associations") or []:
        s, t = asso.get("sourceAggregate"), asso.get("targetAggregate")
        if not s or not t:
            rep.error("ASSOC_INCOMPLETE", "聚合关联 %s 缺少两端" % asso.get("id"), [asso.get("id") or ""])
            continue
        label = "关联"
        if asso.get("cardinality") in ("MANY_TO_ONE", "ONE_TO_MANY"):
            label = "关联"
        g.edge(s, t, "assoc", label, asso.get("id") or "")

    # ---- 行为
    for b in behaviors:
        bid = b.get("id")
        if not bid:
            rep.error("M2_NO_ID", "行为缺少 id")
            continue
        g.node(bid, "behavior", b.get("name") or bid, alias="",
               summary=b.get("operationSteps") or "",
               group={"COMMAND": "命令", "QUERY": "查询", "EVENT_HANDLER": "事件处理"}.get(b.get("behaviorType"), ""),
               extra={"behaviorType": b.get("behaviorType") or "", "triggerType": b.get("triggerType") or ""})
        if b.get("ownerEntity"):
            g.edge(bid, b["ownerEntity"], "owner", "作用于", bid)
        for rid in b.get("appliedRules") or []:
            g.edge(bid, rid, "applies", "引用规则", bid)
        for eid in b.get("producedEvents") or []:
            g.edge(bid, eid, "produces", "产生事件", bid)
        if b.get("queryReportRef"):
            g.edge(bid, b["queryReportRef"], "binds", "查询绑定", bid)

    # ---- 规则
    for r in rules:
        rid = r.get("id")
        if not rid:
            rep.error("M3_NO_ID", "规则缺少 id")
            continue
        g.node(rid, "rule", r.get("name") or rid, alias="",
               summary=r.get("description") or "", group=r.get("ruleType") or "",
               extra={"ruleType": r.get("ruleType") or ""})

    # ---- 事件
    for e in events:
        eid = e.get("eventId") or e.get("id")
        if not eid:
            rep.error("ME_NO_ID", "事件缺少 eventId")
            continue
        g.node(eid, "event", e.get("eventName") or eid, alias="",
               summary=e.get("description") or "", group=e.get("ordering") or "",
               extra={"producerType": e.get("producerType") or ""})
        if e.get("producerBehaviorRef"):
            g.edge(e["producerBehaviorRef"], eid, "produces", "产生事件", eid)
        for sub in e.get("subscribers") or []:
            ref = sub.get("subscriberBehaviorRef") or sub.get("subscriberRuleRef")
            if ref:
                g.edge(eid, ref, "consumes", "订阅", eid)

    # ---- 场景
    for s in scenarios:
        sid = s.get("id")
        if not sid:
            rep.error("M4_NO_ID", "场景缺少 id")
            continue
        g.node(sid, "scenario", s.get("name") or sid, alias="",
               summary=s.get("description") or "", group="端到端场景",
               extra={"stepCount": len(s.get("steps") or [])})
        for ref in [s.get("sourceObjectRef")] + list(s.get("targetObjectRefs") or []):
            if ref:
                g.edge(sid, ref, "scope", "涉及", sid)
        if s.get("triggerEventRef"):
            g.edge(sid, s["triggerEventRef"], "scope", "触发", sid)

    # ---- 报表
    for rp in reports:
        rid = rp.get("id")
        if not rid:
            rep.error("M7_NO_ID", "报表缺少 id")
            continue
        g.node(rid, "report", rp.get("name") or rid, alias=rp.get("alias") or "",
               summary=rp.get("description") or "", group=rp.get("objectType") or "",
               extra={"objectType": rp.get("objectType") or ""})
        if rp.get("behaviorRef"):
            g.edge(rp["behaviorRef"], rid, "binds", "查询绑定", rid)
        for src in rp.get("sourceObjects") or []:
            if src.get("objectRef"):
                g.edge(rid, src["objectRef"], "source", "数据来源", rid)

    # ---- 悬空引用补齐（先补齐再统计度数）
    for e in list(g.edges.values()):
        for end in (e["source"], e["target"]):
            if end not in g.nodes:
                g.ghost(end, rep, e["origin"] or "模型引用")

    # ---- 度数
    for n in g.nodes.values():
        n["degree"] = 0
    for e in g.edges.values():
        g.nodes[e["source"]]["degree"] += 1
        g.nodes[e["target"]]["degree"] += 1

    return {
        "graph": g, "aggregates": aggregates, "behaviors": behaviors, "rules": rules,
        "events": events, "scenarios": scenarios, "reports": reports,
    }


# ---------------------------------------------------------------- 字段索引与校验

def field_index(aggregates):
    idx = {}
    for agg in aggregates:
        if not isinstance(agg, dict) or not agg.get("id"):
            continue
        names = set()
        # 兼容两种写法：聚合级 attributes，或 rootEntity.attributes 嵌套
        buckets = [agg]
        if isinstance(agg.get("rootEntity"), dict):
            buckets.append(agg["rootEntity"])
        for holder in buckets:
            for at in holder.get("attributes") or []:
                if isinstance(at, dict) and at.get("name"):
                    names.add(at["name"])
            if holder is not agg and holder.get("localId"):
                names.add(holder["localId"])
        for ent in agg.get("entities") or []:
            if ent.get("localId"):
                names.add(ent["localId"])
            for at in ent.get("attributes") or []:
                if at.get("name"):
                    names.add(at["name"])
        for vo in agg.get("valueObjects") or []:
            for at in vo.get("attributes") or []:
                if at.get("name"):
                    names.add(at["name"])
        idx[agg["id"]] = names
    return idx


def check_source_field(val, idx, rep, origin, allow_system=True):
    if not val or "." not in str(val):
        return
    head, _, tail = str(val).partition(".")
    if allow_system and head == "SYSTEM":
        return
    if head not in idx:
        rep.error("FIELD_OBJ_MISSING", "%s 的 sourceField=%s 指向不存在的对象" % (origin, val), [origin, head])
        return
    if tail and tail not in idx[head]:
        rep.error("FIELD_MISSING", "%s 的 sourceField=%s 指向对象上不存在的字段 %s" % (origin, val, tail), [origin, head])


def validate(models, built, rep):
    m1 = models.get("m1", (None, {}))[1]
    m2 = models.get("m2", (None, {}))[1]
    m3 = models.get("m3", (None, {}))[1]
    me = models.get("me", (None, {}))[1]
    m4 = models.get("m4", (None, {}))[1]
    m7 = models.get("m7", (None, {}))[1]

    aggregates = built["aggregates"]
    behaviors = built["behaviors"]
    rules = built["rules"]
    events = built["events"]
    scenarios = built["scenarios"]
    reports = built["reports"]

    agg_ids = {a["id"] for a in aggregates if isinstance(a, dict) and a.get("id")}
    bhv_ids = {b["id"] for b in behaviors if isinstance(b, dict) and b.get("id")}
    rule_ids = {r["id"] for r in rules if isinstance(r, dict) and r.get("id")}
    evt_ids = {(e.get("eventId") or e.get("id")) for e in events if isinstance(e, dict)}
    scn_ids = {s["id"] for s in scenarios if isinstance(s, dict) and s.get("id")}
    rpt_ids = {r["id"] for r in reports if isinstance(r, dict) and r.get("id")}
    dict_ids = {d.get("id") for d in (m1.get("data_dictionaries") or []) if isinstance(d, dict)}
    idx = field_index(aggregates)

    # 1. 根字段与版本
    for kind, (fname, data) in models.items():
        mt = str(data.get("model_type")).upper()
        allowed = ROOT_FIELDS.get(mt, set())
        extra = set(data) - allowed
        missing = {"model_type", "version", "domain"} - set(data)
        if extra:
            rep.error("ROOT_FIELD", "%s 根字段超出契约：%s" % (fname, sorted(extra)), [fname])
        if missing:
            rep.error("ROOT_FIELD", "%s 缺少根字段：%s" % (fname, sorted(missing)), [fname])
        if set(data) & BANNED_ROOT:
            rep.error("ROOT_BANNED", "%s 使用了禁用根字段：%s" % (fname, sorted(set(data) & BANNED_ROOT)), [fname])
        if MODEL_VERSION.get(mt) and str(data.get("version")) != MODEL_VERSION[mt]:
            rep.warn("VERSION", "%s 版本为 %s，契约建议 %s" % (fname, data.get("version"), MODEL_VERSION[mt]), [fname])

    # 2. 全局 ID 唯一
    all_ids = Counter()
    for group, tag in ((agg_ids, "对象"), (bhv_ids, "行为"), (rule_ids, "规则"),
                       (evt_ids, "事件"), (scn_ids, "场景"), (rpt_ids, "报表")):
        for i in group:
            if i:
                all_ids[i] += 1
    for i, c in all_ids.items():
        if c > 1:
            rep.error("ID_DUPLICATE", "ID 在多个模型或同一模型内重复：%s" % i, [i])

    # 3. M1 结构
    if len(aggregates) > 35:
        rep.warn("BUDGET", "聚合数量 %d 超过硬上限 35" % len(aggregates))
    if len(m1.get("aggregate_associations") or []) > 60:
        rep.warn("BUDGET", "聚合关联数量超过硬上限 60")
    for agg in aggregates:
        if not isinstance(agg, dict) or not agg.get("id"):
            continue
        aid = agg["id"]
        if agg.get("aggregateType") and agg["aggregateType"] not in ENUMS["aggregateType"]:
            rep.error("ENUM", "%s 的 aggregateType=%s 非法" % (aid, agg["aggregateType"]), [aid])
        for at in agg.get("attributes") or []:
            _check_attr(at, aid, idx, dict_ids, agg_ids, rep)
        for ent in agg.get("entities") or []:
            if ent.get("cardinality") and ent["cardinality"] not in ENUMS["cardinality"]:
                rep.error("ENUM", "%s/%s 的 cardinality 非法" % (aid, ent.get("name")), [aid])
            for at in ent.get("attributes") or []:
                _check_attr(at, aid, idx, dict_ids, agg_ids, rep)
        for vo in agg.get("valueObjects") or []:
            if vo.get("immutable") is None:
                rep.warn("M1_VO", "%s/%s 值对象缺少 immutable 标记" % (aid, vo.get("name")), [aid])
            for at in vo.get("attributes") or []:
                _check_attr(at, aid, idx, dict_ids, agg_ids, rep)
        for inv in agg.get("invariants") or []:
            if inv.get("enforcedAt") and inv["enforcedAt"] not in ENUMS["enforcedAt"]:
                rep.error("ENUM", "%s 不变量 %s 的 enforcedAt 非法" % (aid, inv.get("name")), [aid])

    for asso in m1.get("aggregate_associations") or []:
        if not isinstance(asso, dict):
            continue
        tag = asso.get("id") or "关联"
        if asso.get("sourceAggregate") not in agg_ids or asso.get("targetAggregate") not in agg_ids:
            rep.error("ASSOC_REF", "%s 关联两端对象不存在" % tag, [tag])
        if asso.get("associationType") and asso["associationType"] not in ENUMS["associationType"]:
            rep.error("ENUM", "%s 的 associationType 非法" % tag, [tag])
        if asso.get("cardinality") and asso["cardinality"] not in ENUMS["cardinality"]:
            rep.error("ENUM", "%s 的 cardinality 非法" % tag, [tag])

    for d in (m1.get("data_dictionaries") or []):
        if isinstance(d, dict) and not d.get("types"):
            rep.warn("DICT_EMPTY", "字典 %s 没有类型项" % d.get("id"), [d.get("id") or ""])

    # 4. M2
    if len(behaviors) > 60:
        rep.warn("BUDGET", "行为数量 %d 超过硬上限 60" % len(behaviors))
    for b in behaviors:
        if not isinstance(b, dict) or not b.get("id"):
            continue
        bid = b["id"]
        if b.get("ownerEntity") not in agg_ids:
            rep.error("M2_OWNER", "%s 的 ownerEntity=%s 不存在" % (bid, b.get("ownerEntity")), [bid])
        if b.get("behaviorType") not in ENUMS["behaviorType"]:
            rep.error("ENUM", "%s 的 behaviorType 非法" % bid, [bid])
        if b.get("triggerType") not in ENUMS["triggerType"]:
            rep.error("ENUM", "%s 的 triggerType 非法" % bid, [bid])
        if not isinstance(b.get("operationSteps"), str) or len(b.get("operationSteps") or "") < 10:
            rep.warn("M2_STEPS", "%s 的 operationSteps 过于简略" % bid, [bid])
        for rid in b.get("appliedRules") or []:
            if rid not in rule_ids:
                rep.error("M2_RULE_REF", "%s 引用了不存在的规则 %s" % (bid, rid), [bid, rid])
        for eid in b.get("producedEvents") or []:
            if eid not in evt_ids:
                rep.error("M2_EVENT_REF", "%s 引用了不存在的事件 %s" % (bid, eid), [bid, eid])
        if b.get("queryReportRef") and b["queryReportRef"] not in rpt_ids:
            rep.error("M2_REPORT_REF", "%s 的 queryReportRef 不存在" % bid, [bid])
        if b.get("behaviorType") == "QUERY":
            if b.get("producedEvents"):
                rep.error("M2_QUERY_EVENT", "QUERY 行为 %s 不应产生事件" % bid, [bid])
            if b.get("postconditions"):
                rep.error("M2_QUERY_POST", "QUERY 行为 %s 不应有状态后置条件" % bid, [bid])
            if not b.get("queryReportRef"):
                rep.warn("M2_QUERY_BIND", "QUERY 行为 %s 未绑定报表" % bid, [bid])

    # 5. M3（含字段级引用）
    for r in rules:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        rid = r["id"]
        for f in ("name", "ruleType", "outputType", "expression", "version"):
            if not r.get(f):
                rep.error("M3_REQUIRED", "%s 缺少必填字段 %s" % (rid, f), [rid])
        if r.get("ruleType") not in ENUMS["ruleType"]:
            rep.error("ENUM", "%s 的 ruleType 非法" % rid, [rid])
        if r.get("executionMode") and r["executionMode"] not in ENUMS["executionMode"]:
            rep.error("ENUM", "%s 的 executionMode 非法" % rid, [rid])
        if not (r.get("inputParams") or []):
            rep.warn("M3_PARAMS", "%s 没有输入参数" % rid, [rid])
        for p in r.get("inputParams") or []:
            check_source_field(p.get("sourceField"), idx, rep, "%s.%s" % (rid, p.get("name")))
        for bid in r.get("reusedBy") or []:
            if bid not in bhv_ids:
                rep.error("M3_REUSED_BY", "%s 的 reusedBy 引用不存在的行为 %s" % (rid, bid), [rid, bid])
            elif rid not in (next((x for x in behaviors if x.get("id") == bid), {}) or {}).get("appliedRules", []):
                rep.error("BIDIRECT", "%s 与行为 %s 未双向一致（行为的 appliedRules 缺该规则）" % (rid, bid), [rid, bid])

    # 6. ME（含字段级引用）
    for e in events:
        if not isinstance(e, dict):
            continue
        eid = e.get("eventId") or e.get("id")
        if not eid:
            continue
        if e.get("producerType") not in ENUMS["producerType"]:
            rep.error("ENUM", "%s 的 producerType 非法" % eid, [eid])
        pb = e.get("producerBehaviorRef")
        if pb and pb not in bhv_ids:
            rep.error("ME_PRODUCER", "%s 的 producerBehaviorRef 不存在" % eid, [eid, pb])
        if pb and eid not in (next((x for x in behaviors if x.get("id") == pb), {}) or {}).get("producedEvents", []):
            rep.error("BIDIRECT", "%s 与行为 %s 未双向一致（行为的 producedEvents 缺该事件）" % (eid, pb), [eid, pb])
        if e.get("producerEntityRef") and e["producerEntityRef"] not in agg_ids:
            rep.error("ME_ENTITY", "%s 的 producerEntityRef 不存在" % eid, [eid])
        if e.get("ordering") and e["ordering"] not in ENUMS["ordering"]:
            rep.error("ENUM", "%s 的 ordering 非法" % eid, [eid])
        if not (e.get("payload") or []):
            rep.warn("ME_PAYLOAD", "%s 没有 payload" % eid, [eid])
        for p in e.get("payload") or []:
            check_source_field(p.get("sourceField"), idx, rep, "%s.%s" % (eid, p.get("name")))
        if not (e.get("subscribers") or []):
            rep.warn("ME_SUBSCRIBER", "%s 没有订阅者，事件将无消费方" % eid, [eid])
        for sub in e.get("subscribers") or []:
            st = sub.get("subscriberType")
            if st and st not in ENUMS["subscriberType"]:
                rep.error("ENUM", "%s 订阅者类型非法" % eid, [eid])
            if st == "BEHAVIOR":
                ref = sub.get("subscriberBehaviorRef")
                if ref not in bhv_ids:
                    rep.error("ME_SUB", "%s 订阅的行为 %s 不存在" % (eid, ref), [eid, ref])
            elif st == "RULE":
                ref = sub.get("subscriberRuleRef")
                if ref not in rule_ids:
                    rep.error("ME_SUB", "%s 订阅的规则 %s 不存在" % (eid, ref), [eid, ref])

    # 7. M4（链路完整性）
    if not (4 <= len(scenarios) <= 10):
        rep.warn("BUDGET", "场景数量 %d 不在建议区间 4-8（硬上限 10）" % len(scenarios))
    for s in scenarios:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = s["id"]
        if s.get("sourceObjectRef") not in agg_ids:
            rep.error("M4_SOURCE", "%s 的 sourceObjectRef 不存在" % sid, [sid])
        targets = s.get("targetObjectRefs") or []
        if not targets:
            rep.error("M4_TARGET", "%s 没有目标对象" % sid, [sid])
        for t in targets:
            if t not in agg_ids:
                rep.error("M4_TARGET_REF", "%s 的目标对象 %s 不存在" % (sid, t), [sid, t])
        if targets and all(t == s.get("sourceObjectRef") for t in targets):
            rep.error("M4_CROSS", "%s 未跨越到不同对象" % sid, [sid])
        if s.get("triggerEventRef") not in evt_ids:
            rep.error("M4_TRIGGER", "%s 的 triggerEventRef 不存在" % sid, [sid])

        steps = s.get("steps") or []
        if not steps:
            rep.error("M4_STEPS", "%s 没有步骤" % sid, [sid])
            continue
        ids = [st.get("stepId") for st in steps]
        if len(ids) != len(set(ids)) or any(not i for i in ids):
            rep.error("M4_STEP_ID", "%s 的 stepId 为空或重复" % sid, [sid])
        id_set = set(i for i in ids if i)
        emitted = set()
        edge_refs = defaultdict(set)
        for st in steps:
            tag = "%s.%s" % (sid, st.get("stepId"))
            stype = st.get("stepType")
            if stype not in ENUMS["stepType"]:
                rep.error("ENUM", "%s 的 stepType=%s 非法" % (tag, stype), [sid])
            if stype == "BEHAVIOR_CALL":
                if st.get("behaviorRef") not in bhv_ids:
                    rep.error("M4_STEP_REF", "%s 的 behaviorRef 不存在" % tag, [sid])
                else:
                    edge_refs[st["behaviorRef"]].add("behavior")
            elif stype == "RULE_EVALUATE":
                if st.get("ruleRef") not in rule_ids:
                    rep.error("M4_STEP_REF", "%s 的 ruleRef 不存在" % tag, [sid])
            elif stype == "EVENT_EMIT":
                if st.get("eventRef") not in evt_ids:
                    rep.error("M4_STEP_REF", "%s 的 eventRef 不存在" % tag, [sid])
                else:
                    emitted.add(st["eventRef"])
            for nxt in st.get("nextSteps") or []:
                if nxt not in id_set:
                    rep.error("M4_NEXT", "%s 的 nextSteps 指向不存在的步骤 %s" % (tag, nxt), [sid])
        # 可达性
        by_id = {st.get("stepId"): st for st in steps}
        reach, stack = set(), [steps[0].get("stepId")]
        while stack:
            cur = stack.pop()
            if cur in reach or cur not in by_id:
                continue
            reach.add(cur)
            stack.extend(by_id[cur].get("nextSteps") or [])
        if reach != id_set:
            rep.error("M4_UNREACHABLE", "%s 存在不可达步骤：%s" % (sid, sorted(id_set - reach)), [sid])
        # 环检测
        color = {}

        def dfs(nid, path):
            color[nid] = 1
            for nxt in by_id.get(nid, {}).get("nextSteps") or []:
                if color.get(nxt) == 1:
                    rep.error("M4_CYCLE", "%s 步骤链路存在环：%s" % (sid, "->".join(path + [nxt])), [sid])
                elif color.get(nxt, 0) == 0:
                    dfs(nxt, path + [nxt])
            color[nid] = 2

        dfs(steps[0].get("stepId"), [steps[0].get("stepId")])
        # 触发事件是否由本场景步骤产生
        if s.get("triggerEventRef") and s["triggerEventRef"] not in emitted:
            rep.warn("M4_TRIGGER_EXTERNAL", "%s 的触发事件 %s 未在本场景步骤中产生（作为外部前置事件）"
                     % (sid, s["triggerEventRef"]), [sid])
        # 声明涉及的对象是否在步骤行为里落点
        step_objs = {(next((x for x in behaviors if x.get("id") == st.get("behaviorRef")), {}) or {}).get("ownerEntity")
                     for st in steps if st.get("stepType") == "BEHAVIOR_CALL"}
        for t in targets:
            if t not in step_objs:
                rep.warn("M4_TARGET_INDIRECT", "%s 的目标对象 %s 未直接出现在步骤行为中（作为结果对象）" % (sid, t), [sid, t])

    # 8. M7（主源 / alias / 表达式）
    for rp in reports:
        if not isinstance(rp, dict) or not rp.get("id"):
            continue
        rid = rp["id"]
        for f in ("name", "alias", "objectType", "behaviorRef"):
            if not rp.get(f):
                rep.error("M7_REQUIRED", "%s 缺少必填字段 %s" % (rid, f), [rid])
        if rp.get("objectType") not in ENUMS["objectType"]:
            rep.error("ENUM", "%s 的 objectType 非法" % rid, [rid])
        bid = rp.get("behaviorRef")
        if bid not in bhv_ids:
            rep.error("M7_BEHAVIOR", "%s 的 behaviorRef 不存在" % rid, [rid, bid or ""])
        else:
            b = next((x for x in behaviors if x.get("id") == bid), {}) or {}
            if b.get("queryReportRef") != rid:
                rep.error("BIDIRECT", "%s 与行为 %s 未双向一对一" % (rid, bid), [rid, bid])
        srcs = rp.get("sourceObjects") or []
        primary = [o for o in srcs if o.get("primary")]
        if len(primary) != 1:
            rep.error("M7_PRIMARY", "%s 主数据源数量为 %d，必须为 1" % (rid, len(primary)), [rid])
        aliases = [o.get("alias") for o in srcs]
        if len(set(aliases)) != len(aliases) or any(not a for a in aliases):
            rep.error("M7_ALIAS", "%s 的 sourceObjects alias 为空或重复" % rid, [rid])
        for o in srcs:
            if o.get("objectRef") not in agg_ids:
                rep.error("M7_SOURCE", "%s 的数据源 %s 不存在" % (rid, o.get("objectRef")), [rid])
        for j in rp.get("joins") or []:
            if j.get("leftSource") not in aliases or j.get("rightSource") not in aliases:
                rep.error("M7_JOIN", "%s 的 join 使用了未声明的 alias" % rid, [rid])
        if rp.get("objectType") == "REPORT" and not rp.get("reportOptions"):
            rep.error("M7_REPORT_OPTIONS", "%s 为 REPORT 但缺少 reportOptions" % rid, [rid])
        if rp.get("objectType") != "REPORT" and rp.get("reportOptions"):
            rep.error("M7_REPORT_OPTIONS", "%s 非 REPORT 却提供了 reportOptions" % rid, [rid])
        if not (rp.get("resultColumns") or []):
            rep.error("M7_COLUMNS", "%s 没有结果列" % rid, [rid])
        alias_pat = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")
        for c in rp.get("resultColumns") or []:
            for f in ("name", "label", "dataType", "sourceExpression"):
                if not c.get(f):
                    rep.error("M7_COLUMN_FIELD", "%s 结果列缺少 %s" % (rid, f), [rid])
            for a, _f in alias_pat.findall(c.get("sourceExpression") or ""):
                if a not in aliases:
                    rep.warn("M7_EXPR_ALIAS", "%s 列 %s 的表达式使用了未声明别名 %s"
                             % (rid, c.get("name"), a), [rid])
        banned = {"ruleRefs", "requiredPermissions", "roleRefs", "eventRefs", "scenarioRefs", "flowRefs"}
        if json.dumps(rp, ensure_ascii=False) .find("ruleRefs") >= 0:
            rep.warn("M7_BANNED", "%s 中出现疑似禁用字段名" % rid, [rid])

    # 9. 图结构与覆盖率
    g = built["graph"]
    linked = set()
    for e in g.edges.values():
        linked.add(e["source"])
        linked.add(e["target"])
    for nid, n in g.nodes.items():
        if not n["missing"] and nid not in linked:
            rep.warn("GRAPH_ISOLATED", "节点 %s（%s）没有任何连线" % (nid, n["name"]), [nid])
        if n["degree"] > 40:
            rep.warn("GRAPH_HUB", "节点 %s 度数 %d 过高，图中易成毛球，建议细分" % (nid, n["degree"]), [nid])

    covered = {b.get("ownerEntity") for b in behaviors}
    for aid in agg_ids:
        if aid not in covered:
            rep.warn("COVERAGE_OBJECT", "对象 %s 没有直连行为（作为被引用主数据可接受）" % aid, [aid])
    used_rules = {rid for b in behaviors for rid in (b.get("appliedRules") or [])}
    for rid in rule_ids:
        if rid not in used_rules:
            rep.warn("COVERAGE_RULE", "规则 %s 未被任何行为引用" % rid, [rid])
    used_dicts = set()
    for agg in aggregates:
        for at in agg.get("attributes") or []:
            if at.get("dictionaryRef"):
                used_dicts.add(at["dictionaryRef"])
        for ent in agg.get("entities") or []:
            for at in ent.get("attributes") or []:
                if at.get("dictionaryRef"):
                    used_dicts.add(at["dictionaryRef"])
    for did in dict_ids:
        if did and did not in used_dicts:
            rep.warn("COVERAGE_DICT", "字典 %s 未被任何属性引用" % did, [did])

    return {
        "counts": {
            "aggregates": len(aggregates), "associations": len(m1.get("aggregate_associations") or []),
            "dictionaries": len(m1.get("data_dictionaries") or []), "behaviors": len(behaviors),
            "rules": len(rules), "events": len(events), "scenarios": len(scenarios), "reports": len(reports),
        }
    }


def _check_attr(at, aid, idx, dict_ids, agg_ids, rep):
    if not isinstance(at, dict):
        return
    name = at.get("name") or "?"
    typ = at.get("type")
    if typ and typ not in ENUMS["attributeType"]:
        rep.error("ENUM", "%s.%s 的属性类型 %s 非法" % (aid, name, typ), [aid])
        return
    if typ == "Enum":
        vals = at.get("enumValues") or []
        if not vals or len(set(vals)) != len(vals):
            rep.error("M1_ENUM", "%s.%s 的 Enum 取值为空或重复" % (aid, name), [aid])
    if typ == "DictionaryRef":
        dr = at.get("dictionaryRef")
        if dr not in dict_ids:
            rep.error("M1_DICT_REF", "%s.%s 引用不存在的字典 %s" % (aid, name, dr), [aid])
    if typ == "AggregateRootRef":
        tg = at.get("targetAggregate")
        if tg not in agg_ids:
            rep.error("M1_REF_TARGET", "%s.%s 的 targetAggregate=%s 不存在" % (aid, name, tg), [aid])
    if typ == "ValueObject" and not at.get("valueObjectRef"):
        rep.error("M1_VO_REF", "%s.%s 为 ValueObject 但缺少 valueObjectRef" % (aid, name), [aid])


# ---------------------------------------------------------------- 面板渲染数据

SAFE = {"String", "Integer", "Decimal", "Date", "DateTime", "Boolean", "Money"}


def _blank(v):
    return "—" if v in (None, "", [], {}) else str(v)


def kv(title, pairs):
    return {"type": "kv", "title": title, "rows": [[k, _blank(v)] for k, v in pairs]}


def table(title, head, rows):
    clean = [["" if c is None else str(c) for c in r] for r in rows]
    return {"type": "table", "title": title, "head": head, "rows": clean}


def text_block(title, body):
    return {"type": "text", "title": title, "text": _blank(body)}


def list_block(title, items):
    return {"type": "list", "title": title, "items": [str(i) for i in items if i]}


def chips_block(title, items):
    return {"type": "chips", "title": title, "items": items}


def attr_type_label(at, agg_name):
    typ = at.get("type") or ""
    if typ == "Enum":
        return "Enum(%s)" % "/".join(at.get("enumValues") or [])
    if typ == "AggregateRootRef":
        return "→ %s" % agg_name.get(at.get("targetAggregate"), at.get("targetAggregate") or "?")
    if typ == "DictionaryRef":
        return "字典 %s" % at.get("dictionaryRef")
    if typ == "ValueObject":
        return "值对象 %s" % at.get("valueObjectRef")
    return typ


def build_payload(models, built, counts, source_files):
    graph = built["graph"]
    aggregates = [a for a in built["aggregates"] if isinstance(a, dict) and a.get("id")]
    behaviors = [b for b in built["behaviors"] if isinstance(b, dict) and b.get("id")]
    rules = [r for r in built["rules"] if isinstance(r, dict) and r.get("id")]
    events = [e for e in built["events"] if isinstance(e, dict) and (e.get("eventId") or e.get("id"))]
    scenarios = [s for s in built["scenarios"] if isinstance(s, dict) and s.get("id")]
    reports = [r for r in built["reports"] if isinstance(r, dict) and r.get("id")]

    agg_by_id = {a["id"]: a for a in aggregates}
    agg_name = {a["id"]: a.get("name") or a["id"] for a in aggregates}
    bhv_by_id = {b["id"]: b for b in behaviors}
    rule_by_id = {r["id"]: r for r in rules}
    evt_by_id = {(e.get("eventId") or e.get("id")): e for e in events}
    scn_by_id = {s["id"]: s for s in scenarios}
    rpt_by_id = {r["id"]: r for r in reports}

    # ---- 反向索引
    obj_behaviors = defaultdict(list)
    for b in behaviors:
        obj_behaviors[b.get("ownerEntity")].append(b["id"])
    obj_scenarios = defaultdict(list)
    obj_reports = defaultdict(list)
    for s in scenarios:
        for o in [s.get("sourceObjectRef")] + list(s.get("targetObjectRefs") or []):
            if o:
                obj_scenarios[o].append(s["id"])
    for r in reports:
        for src in r.get("sourceObjects") or []:
            if src.get("objectRef"):
                obj_reports[src["objectRef"]].append(r["id"])
    bhv_scenarios = defaultdict(list)
    rule_scenarios = defaultdict(list)
    evt_scenarios = defaultdict(list)
    for s in scenarios:
        for st in s.get("steps") or []:
            if st.get("behaviorRef"):
                bhv_scenarios[st["behaviorRef"]].append(s["id"])
            if st.get("ruleRef"):
                rule_scenarios[st["ruleRef"]].append(s["id"])
            if st.get("eventRef"):
                evt_scenarios[st["eventRef"]].append(s["id"])

    domain = ""
    for kind in ("m1", "m2", "m3", "me", "m4", "m7"):
        if kind in models and models[kind][1].get("domain"):
            domain = models[kind][1]["domain"]
            break

    # ---- 节点
    nodes = []
    for nid, n in graph.nodes.items():
        nodes.append({
            "id": nid, "name": n["name"], "type": n["type"],
            "category": list(TYPE_INFO.keys()).index(n["type"]) if n["type"] in TYPE_INFO else 0,
            "degree": n["degree"], "group": n.get("group") or "",
            "alias": n.get("alias") or "", "summary": (n.get("summary") or "")[:160],
            "missing": n["missing"],
        })

    # ---- 边
    edges = [{"id": e["id"], "source": e["source"], "target": e["target"],
              "type": e["type"], "label": e["label"], "weight": e["weight"]}
             for e in graph.edges.values()]

    # ---- 场景序列
    def merge_behavior_events(items):
        """把紧跟行为之后的 EVENT_EMIT 并入该行为步骤（同一播放阶段）。

        - 一个行为后连续多个事件时全部并入；
        - 步骤号沿用行为步骤号，被并入的事件步骤号记录在 mergedSteps；
        - refs 汇总该阶段需要同时高亮的所有节点 ID。
        规则步骤后的事件不合并（规则本身不产生事件的情况更常见，保持原样）。
        """
        out = []
        for it in items:
            if (it["stepType"] == "EVENT_EMIT" and out
                    and out[-1]["stepType"] == "BEHAVIOR_CALL"):
                prev = out[-1]
                prev.setdefault("events", []).append(
                    {"id": it["refId"], "name": it["refName"], "detail": it.get("detail") or ""})
                prev.setdefault("mergedSteps", []).append(it["index"])
                if it["refId"] and it["refId"] not in prev["refs"]:
                    prev["refs"].append(it["refId"])
                if it.get("objectId") and not prev.get("objectId"):
                    prev["objectId"] = it["objectId"]
                    prev["objectName"] = it.get("objectName") or ""
                continue
            it["refs"] = [it["refId"]] if it.get("refId") else []
            out.append(it)
        for i, it in enumerate(out):
            it["seqNo"] = "S%02d" % (i + 1)
            it["sourceStepId"] = it.get("index") or ""
        return out

    scn_payload = []
    for s in scenarios:
        seq, seen_objs = [], []
        for st in s.get("steps") or []:
            stype = st.get("stepType") or ""
            ref_id = st.get("behaviorRef") or st.get("ruleRef") or st.get("eventRef") or ""
            ref_type, detail, obj_id = "", "", ""
            if st.get("behaviorRef") and st["behaviorRef"] in bhv_by_id:
                b = bhv_by_id[st["behaviorRef"]]
                ref_type, detail, obj_id = "behavior", b.get("operationSteps") or "", b.get("ownerEntity") or ""
            elif st.get("ruleRef") and st["ruleRef"] in rule_by_id:
                r = rule_by_id[st["ruleRef"]]
                ref_type, detail = "rule", "判定表达式：%s" % (r.get("expression") or "")
            elif st.get("eventRef") and st["eventRef"] in evt_by_id:
                e = evt_by_id[st["eventRef"]]
                ref_type, detail, obj_id = "event", e.get("description") or "", e.get("producerEntityRef") or ""
            ref_name = graph.nodes[ref_id]["name"] if ref_id in graph.nodes else ref_id
            if obj_id and obj_id not in seen_objs:
                seen_objs.append(obj_id)
            seq.append({
                "index": st.get("stepId") or "", "stepType": stype,
                "typeName": STEP_TAG.get(stype, ("步骤", ""))[0],
                "refType": ref_type, "refId": ref_id, "refName": ref_name,
                "objectId": obj_id, "objectName": agg_name.get(obj_id, ""),
                "detail": detail,
            })
        # 行为产生的事件与行为本身是"一次完成"的语义，合并为同一播放阶段，
        # 避免播放时被拆成两步（行为一步、事件一步）。
        seq = merge_behavior_events(seq)
        focus = [s["id"]] + [r for x in seq for r in x["refs"]] + list(seen_objs)
        src = s.get("sourceObjectRef")
        tgts = list(s.get("targetObjectRefs") or [])
        if s.get("triggerEventRef"):
            focus.append(s["triggerEventRef"])
        focus += [o for o in [src] + tgts if o]
        scn_payload.append({
            "id": s["id"], "name": s.get("name") or s["id"],
            "description": s.get("description") or "",
            "triggerEventId": s.get("triggerEventRef") or "",
            "triggerEventName": graph.nodes.get(s.get("triggerEventRef") or "", {}).get("name", ""),
            "sourceObjectId": src or "", "sourceObjectName": agg_name.get(src, ""),
            "targets": [{"id": t, "name": agg_name.get(t, t)} for t in tgts],
            "preconditions": s.get("preconditions") or [],
            "postconditions": s.get("postconditions") or [],
            "stats": {
                "steps": len(seq),
                "objects": len({o for o in seen_objs} | {src} | set(tgts)),
                "behaviors": len({x["refId"] for x in seq if x["refType"] == "behavior"}),
                "rules": len({x["refId"] for x in seq if x["refType"] == "rule"}),
                "events": len({r for x in seq for r in x["refs"]
                               if graph.nodes.get(r, {}).get("type") == "event"}),
            },
            "sequence": seq,
            "focus": sorted(set(focus)),
        })

    # ---- 报表明细 + 指标树
    def esc_m(text, limit=46):
        t = str(text or "").replace('"', "'").replace("\n", " ").replace("[", "(").replace("]", ")")
        if len(t) > limit:
            t = t[:limit] + "…"
        return t

    alias_pat = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")

    def metric_mermaid(rep, col):
        srcs = rep.get("sourceObjects") or []
        alias_map = {o.get("alias"): o for o in srcs}
        # 自上而下的归因树：报表 → 指标 → 口径 → 字段 → 来源对象，纵向成脊、横向少分叉，
        # 这样在 420px 宽的面板里也不用缩小字号。
        lines = ["graph TD"]
        lines.append('  R0["报表 · %s"]' % esc_m(rep.get("name"), 26))
        lines.append('  R0 --> M0["指标 · %s"]' % esc_m(col.get("label"), 24))
        agg = col.get("aggregateFunction") or "NONE"
        cur = "M0"
        if agg and agg != "NONE":
            lines.append('  M0 --> G0["口径 · %s"]' % esc_m(agg, 20))
            cur = "G0"
        fields, seen = [], set()
        for a, f in alias_pat.findall(col.get("sourceExpression") or ""):
            if a in alias_map and (a, f) not in seen:
                seen.add((a, f))
                fields.append((a, f))
        for i, (a, f) in enumerate(fields[:4]):
            lines.append('  %s --> F%d["%s.%s"]' % (cur, i, esc_m(a, 10), esc_m(f, 18)))
            lines.append('  F%d --> O%d["来源 · %s"]' % (i, i, esc_m(alias_map[a].get("name") or a, 16)))
        gb = rep.get("groupBy") or []
        conds = rep.get("conditions") or []
        if gb:
            lines.append('  M0 --> D0["分组 · %s"]' % esc_m("、".join(gb), 22))
            if conds:
                lines.append('  D0 --> W0["过滤 · %s"]' % esc_m("；".join(
                    "%s %s %s" % (c.get("field"), c.get("operator"), c.get("value"))
                    for c in conds), 26))
        if not fields and not gb:
            lines.append('  M0 --> X0["直接取值（无聚合、无来源字段）"]')
        # 节点配色：报表/指标/口径/字段/来源 逐层区分
        lines.append("  classDef rp fill:#E6F6F2,stroke:#12805A,stroke-width:1.5px,color:#0F4C3A")
        lines.append("  classDef mt fill:#E8F0FE,stroke:#3A6FD8,stroke-width:1.5px,color:#1B3A6B")
        lines.append("  classDef ag fill:#FFF4E0,stroke:#D98A16,stroke-width:1.2px,color:#6B4400")
        lines.append("  classDef fd fill:#F4F6FA,stroke:#A7B0C0,stroke-width:1px,color:#334055")
        lines.append("  classDef ob fill:#F3EBFB,stroke:#7C4DBE,stroke-width:1.2px,color:#3F2363")
        cls = [("R0", "rp"), ("M0", "mt")]
        if cur == "G0":
            cls.append(("G0", "ag"))
        for i in range(len(fields[:4])):
            cls.append(("F%d" % i, "fd")); cls.append(("O%d" % i, "ob"))
        for nm, c in cls:
            lines.append("  class %s %s" % (nm, c))
        return "\n".join(lines)

    rpt_payload = []
    for r in reports:
        srcs = r.get("sourceObjects") or []
        cols = r.get("resultColumns") or []
        beh = bhv_by_id.get(r.get("behaviorRef") or "", {})
        metrics = []
        for c in cols:
            metrics.append({
                "key": c.get("name") or "", "label": c.get("label") or c.get("name") or "",
                "dataType": c.get("dataType") or "", "agg": c.get("aggregateFunction") or "NONE",
                "formula": c.get("sourceExpression") or "",
                "mermaid": metric_mermaid(r, c),
            })
        focus = [r["id"]]
        if r.get("behaviorRef"):
            focus.append(r["behaviorRef"])
        for o in srcs:
            oid = o.get("objectRef")
            if not oid:
                continue
            focus.append(oid)
            for bid in obj_behaviors.get(oid, [])[:6]:
                focus.append(bid)
                focus += (bhv_by_id[bid].get("appliedRules") or [])
                focus += (bhv_by_id[bid].get("producedEvents") or [])
        rpt_payload.append({
            "id": r["id"], "name": r.get("name") or r["id"], "alias": r.get("alias") or "",
            "objectType": r.get("objectType") or "", "description": r.get("description") or "",
            "behaviorId": r.get("behaviorRef") or "", "behaviorName": beh.get("name") or "",
            "sources": [{"id": o.get("objectRef") or "", "name": agg_name.get(o.get("objectRef"), o.get("objectRef") or ""),
                         "alias": o.get("alias") or "", "primary": bool(o.get("primary"))} for o in srcs],
            "joins": [{"left": j.get("leftSource") or "", "right": j.get("rightSource") or "",
                       "condition": j.get("condition") or ""} for j in (r.get("joins") or [])],
            "parameters": [{"name": p.get("name") or "", "label": p.get("label") or "",
                            "dataType": p.get("dataType") or "", "required": bool(p.get("required"))}
                           for p in (r.get("parameters") or [])],
            "conditions": [{"field": c.get("field") or "", "operator": c.get("operator") or "",
                            "value": c.get("value") if isinstance(c.get("value"), str) else json.dumps(c.get("value"), ensure_ascii=False)}
                           for c in (r.get("conditions") or [])],
            "groupBy": r.get("groupBy") or [],
            "having": [{"field": c.get("field") or "", "operator": c.get("operator") or "",
                        "value": str(c.get("value"))} for c in (r.get("having") or [])],
            "columns": [{"name": c.get("name") or "", "label": c.get("label") or "", "dataType": c.get("dataType") or "",
                         "agg": c.get("aggregateFunction") or "NONE", "formula": c.get("sourceExpression") or "",
                         "sortable": bool(c.get("sortable")), "nullable": bool(c.get("nullable"))} for c in cols],
            "metrics": metrics,
            "focus": sorted(set(focus)),
        })

    # ---- 详情面板
    panels = {}
    for a in aggregates:
        aid = a["id"]
        attrs = []
        for at in a.get("attributes") or []:
            attrs.append([at.get("name"), at.get("label"), attr_type_label(at, agg_name),
                          "是" if at.get("required") else "", "是" if at.get("unique") else "", at.get("description") or ""])
        ents = []
        for ent in a.get("entities") or []:
            ents.append([ent.get("name"), ent.get("alias"), ent.get("localId"), ent.get("cardinality"),
                         "%d 个字段" % len(ent.get("attributes") or [])])
        vos = []
        for vo in a.get("valueObjects") or []:
            vos.append([vo.get("name"), vo.get("alias"), "是" if vo.get("immutable") else "否",
                        "、".join(vo.get("equalityFields") or [])])
        invs = ["%s：%s（%s）" % (i.get("name"), i.get("description") or "", i.get("expression") or "")
                for i in (a.get("invariants") or [])]
        rels = []
        for asso in (models.get("m1", (None, {}))[1].get("aggregate_associations") or []):
            if asso.get("sourceAggregate") == aid:
                rels.append("→ %s（%s，%s）" % (agg_name.get(asso.get("targetAggregate"), asso.get("targetAggregate")),
                                               asso.get("cardinality"), asso.get("description") or ""))
            elif asso.get("targetAggregate") == aid:
                rels.append("← %s（%s，%s）" % (agg_name.get(asso.get("sourceAggregate"), asso.get("sourceAggregate")),
                                               asso.get("cardinality"), asso.get("description") or ""))
        sections = [
            kv("基本信息", [("对象 ID", aid), ("别名", a.get("alias")), ("说明", a.get("description")),
                        ("生命周期", a.get("lifecycle")), ("业务段", "、".join(a.get("tags") or [])),
                        ("关联边数", graph.nodes[aid]["degree"] if aid in graph.nodes else 0)]),
            table("属性（%d）" % len(attrs), ["字段", "名称", "类型", "必填", "唯一", "说明"], attrs),
        ]
        if ents:
            sections.append(table("子实体（%d）" % len(ents), ["名称", "别名", "局部主键", "基数", "说明"], ents))
        if vos:
            sections.append(table("值对象（%d）" % len(vos), ["名称", "别名", "不可变", "相等性字段"], vos))
        if invs:
            sections.append(list_block("不变量（%d）" % len(invs), invs))
        if rels:
            sections.append(list_block("聚合关联（%d）" % len(rels), rels))
        if obj_behaviors.get(aid):
            sections.append(list_block("相关行为（%d）" % len(obj_behaviors[aid]),
                                       ["%s %s" % (bid, bhv_by_id[bid].get("name")) for bid in obj_behaviors[aid]]))
        if obj_scenarios.get(aid):
            sections.append(list_block("参与场景（%d）" % len(obj_scenarios[aid]),
                                       ["%s %s" % (sid, scn_by_id[sid].get("name")) for sid in obj_scenarios[aid]]))
        if obj_reports.get(aid):
            sections.append(list_block("支撑报表（%d）" % len(obj_reports[aid]),
                                       ["%s %s" % (rid, rpt_by_id[rid].get("name")) for rid in obj_reports[aid]]))
        panels[aid] = {"kind": "object", "badge": "对象", "title": a.get("name") or aid, "subtitle": aid,
                       "chips": [{"label": t, "kind": "tag"} for t in (a.get("tags") or [])], "sections": sections}

    for b in behaviors:
        bid = b["id"]
        owner = b.get("ownerEntity")
        secs = [
            kv("基本信息", [("行为 ID", bid), ("所属对象", "%s %s" % (owner, agg_name.get(owner, ""))),
                        ("行为类型", b.get("behaviorType")), ("触发方式", b.get("triggerType")),
                        ("所属场景", "、".join(scn_by_id[x].get("name") for x in bhv_scenarios.get(bid, [])))]),
            text_block("操作步骤", b.get("operationSteps")),
        ]
        if b.get("preconditions"):
            secs.append(list_block("前置条件", b["preconditions"]))
        if b.get("postconditions"):
            secs.append(list_block("后置结果", b["postconditions"]))
        if b.get("appliedRules"):
            secs.append(list_block("引用规则（%d）" % len(b["appliedRules"]),
                                   ["%s %s" % (rid, rule_by_id.get(rid, {}).get("name")) for rid in b["appliedRules"]]))
        if b.get("producedEvents"):
            secs.append(list_block("产生事件（%d）" % len(b["producedEvents"]),
                                   ["%s %s" % (eid, evt_by_id.get(eid, {}).get("eventName")) for eid in b["producedEvents"]]))
        if b.get("queryReportRef"):
            rp = rpt_by_id.get(b["queryReportRef"], {})
            secs.append(kv("绑定报表", [("报表", "%s %s" % (b["queryReportRef"], rp.get("name"))),
                                    ("报表类型", rp.get("objectType"))]))
        panels[bid] = {"kind": "behavior", "badge": "行为", "title": b.get("name") or bid, "subtitle": bid,
                       "chips": [{"label": b.get("behaviorType"), "kind": "stage"},
                                 {"label": b.get("triggerType"), "kind": "stage"}], "sections": secs}

    for r in rules:
        rid = r["id"]
        params = [[p.get("name"), p.get("type"), p.get("sourceField"), "是" if p.get("required") else "",
                   p.get("description") or ""] for p in (r.get("inputParams") or [])]
        secs = [
            kv("基本信息", [("规则 ID", rid), ("规则类型", r.get("ruleType")), ("输出类型", r.get("outputType")),
                        ("执行方式", r.get("executionMode")), ("超时", r.get("timeout")),
                        ("外部引擎", r.get("externalEngine")), ("版本", r.get("version"))]),
            text_block("说明", r.get("description")),
            text_block("判定表达式", r.get("expression")),
        ]
        if params:
            secs.append(table("输入参数（%d）" % len(params), ["参数", "类型", "来源字段", "必填", "说明"], params))
        if r.get("reusedBy"):
            secs.append(list_block("被以下行为引用（%d）" % len(r["reusedBy"]),
                                   ["%s %s" % (bid, bhv_by_id.get(bid, {}).get("name")) for bid in r["reusedBy"]]))
        if rule_scenarios.get(rid):
            secs.append(list_block("参与场景（%d）" % len(rule_scenarios[rid]),
                                   ["%s %s" % (sid, scn_by_id[sid].get("name")) for sid in rule_scenarios[rid]]))
        panels[rid] = {"kind": "rule", "badge": "规则", "title": r.get("name") or rid, "subtitle": rid,
                       "chips": [{"label": r.get("ruleType"), "kind": "stage"},
                                 {"label": r.get("executionMode"), "kind": "stage"}], "sections": secs}

    for e in events:
        eid = e.get("eventId") or e.get("id")
        payload_rows = [[p.get("name"), p.get("type"), "是" if p.get("required") else "",
                         p.get("sourceField"), p.get("description") or ""] for p in (e.get("payload") or [])]
        subs = []
        for s in e.get("subscribers") or []:
            ref = s.get("subscriberBehaviorRef") or s.get("subscriberRuleRef")
            subs.append("%s %s（优先级 %s）" % (ref, graph.nodes.get(ref, {}).get("name", ""), s.get("priority")))
        secs = [
            kv("基本信息", [("事件 ID", eid), ("产生者类型", e.get("producerType")),
                        ("产生行为", "%s %s" % (e.get("producerBehaviorRef"),
                                            bhv_by_id.get(e.get("producerBehaviorRef") or "", {}).get("name"))),
                        ("所属对象", "%s %s" % (e.get("producerEntityRef"), agg_name.get(e.get("producerEntityRef") or ""))),
                        ("顺序保证", e.get("ordering")), ("版本", e.get("version"))]),
            text_block("说明", e.get("description")),
            text_block("触发条件", e.get("triggerCondition")),
        ]
        if payload_rows:
            secs.append(table("消息载荷（%d）" % len(payload_rows), ["字段", "类型", "必填", "来源字段", "说明"], payload_rows))
        if subs:
            secs.append(list_block("订阅方（%d）" % len(subs), subs))
        if evt_scenarios.get(eid):
            secs.append(list_block("参与场景（%d）" % len(evt_scenarios[eid]),
                                   ["%s %s" % (sid, scn_by_id[sid].get("name")) for sid in evt_scenarios[eid]]))
        panels[eid] = {"kind": "event", "badge": "事件", "title": e.get("eventName") or eid, "subtitle": eid,
                       "chips": [{"label": e.get("ordering"), "kind": "stage"}], "sections": secs}

    for sp in scn_payload:
        secs = [
            kv("场景概览", [("场景 ID", sp["id"]), ("触发事件", "%s %s" % (sp["triggerEventId"], sp["triggerEventName"])),
                        ("源对象", sp["sourceObjectName"]),
                        ("目标对象", "、".join(t["name"] for t in sp["targets"])),
                        ("步骤数", sp["stats"]["steps"]),
                        ("覆盖行为 / 规则 / 事件",
                         "%d / %d / %d" % (sp["stats"]["behaviors"], sp["stats"]["rules"], sp["stats"]["events"]))]),
            text_block("业务目标", sp["description"]),
        ]
        if sp["preconditions"]:
            secs.append(list_block("前置条件", sp["preconditions"]))
        if sp["postconditions"]:
            secs.append(list_block("完成标志", sp["postconditions"]))
        secs.append({"type": "steps", "title": "流程阶段（每一步即一个阶段）", "items": sp["sequence"]})
        panels[sp["id"]] = {"kind": "scenario", "badge": "场景", "title": sp["name"], "subtitle": sp["id"],
                            "chips": [{"label": "%d 个阶段" % sp["stats"]["steps"], "kind": "stage"},
                                      {"label": "端到端", "kind": "tag"}], "sections": secs}

    for rp in rpt_payload:
        secs = [
            kv("报表概览", [("报表 ID", rp["id"]), ("报表类型", rp["objectType"]), ("别名", rp["alias"]),
                        ("查询行为", "%s %s" % (rp["behaviorId"], rp["behaviorName"])),
                        ("主数据源", "、".join(s["name"] for s in rp["sources"] if s["primary"]))]),
            text_block("指标口径说明", rp["description"]),
            {"type": "metrics", "title": "指标口径与归因树（三层：指标 → 口径/字段 → 来源对象）",
             "items": rp["metrics"]},
            table("数据源（%d）" % len(rp["sources"]), ["对象", "别名", "主源"],
                  [[s["name"], s["alias"], "是" if s["primary"] else ""] for s in rp["sources"]]),
        ]
        if rp["joins"]:
            secs.append(table("关联条件（%d）" % len(rp["joins"]), ["左", "右", "条件"],
                              [[j["left"], j["right"], j["condition"]] for j in rp["joins"]]))
        if rp["parameters"]:
            secs.append(table("查询参数", ["参数", "名称", "类型", "必填"],
                              [[p["name"], p["label"], p["dataType"], "是" if p["required"] else ""] for p in rp["parameters"]]))
        conds = rp["conditions"] + rp["having"]
        if conds:
            secs.append(table("过滤与聚合条件", ["字段/别名", "运算符", "取值"],
                              [[c["field"], c["operator"], str(c["value"])] for c in conds]))
        if rp["groupBy"]:
            secs.append(list_block("分组维度", rp["groupBy"]))
        secs.append(table("结果列（%d）" % len(rp["columns"]), ["列", "名称", "类型", "聚合", "表达式"],
                          [[c["name"], c["label"], c["dataType"], c["agg"], c["formula"]] for c in rp["columns"]]))
        panels[rp["id"]] = {"kind": "report", "badge": "报表", "title": rp["name"], "subtitle": rp["id"],
                            "chips": [{"label": rp["objectType"], "kind": "stage"},
                                      {"label": "%d 个指标" % len(rp["metrics"]), "kind": "tag"}], "sections": secs}

    return {
        "meta": {
            "title": "%s · 本体知识网络" % domain if domain else "本体知识网络",
            "domain": domain, "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "ontology_viewer.py", "sourceFiles": source_files, "counts": counts,
        },
        "categories": [{"key": k, "name": v[0], "color": v[1]} for k, v in TYPE_INFO.items()],
        "edgeTypes": [{"key": k, "name": v[0], "color": v[1]} for k, v in EDGE_INFO.items()],
        "nodes": nodes, "edges": edges,
        "scenarios": scn_payload, "reports": rpt_payload, "panels": panels,
    }


# ---------------------------------------------------------------- 前端依赖

CDN = {
    "echarts": "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js",
    "mermaid": "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js",
}


def _script_tag(path, cdn_url, key, rep):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                code = fh.read()
            code = code.replace("</script", "<\\/script")
            return "<script>/* %s (inlined) */\n%s\n</script>" % (key, code)
        except Exception as exc:  # noqa: BLE001
            rep.warn("VENDOR_READ", "%s 读取失败，回退 CDN：%s" % (path, exc))
    else:
        rep.warn("VENDOR_MISSING", "未找到本地 %s，已回退 CDN（离线环境将无法渲染）" % key)
    return '<script src="%s"></script>' % cdn_url


def build_libs(vendor_dir, rep):
    here = os.path.dirname(os.path.abspath(__file__))
    search = [vendor_dir] if vendor_dir else []
    search += [os.path.join(here, "vendor"), os.path.join(here, "ontology_view", "vendor"),
               os.path.join(here, "ontology_view", "assets"),
               os.path.expanduser("~/.workbuddy/skills/ontology-app-builder/tools")]
    found = {}
    for name in ("echarts.min.js", "mermaid.min.js"):
        for d in search:
            if d and os.path.isfile(os.path.join(d, name)):
                found[name] = os.path.join(d, name)
                break
    echarts = _script_tag(found.get("echarts.min.js"), CDN["echarts"], "echarts", rep)
    mermaid = _script_tag(found.get("mermaid.min.js"), CDN["mermaid"], "mermaid", rep)
    return echarts + "\n" + mermaid


# ---------------------------------------------------------------- 渲染与主流程

def render_html(payload, libs_html, rep):
    title = payload["meta"]["title"]
    m = payload["meta"]["counts"]
    meta_line = "聚合 %d · 行为 %d · 规则 %d · 事件 %d · 场景 %d · 报表 %d · 生成于 %s" % (
        m.get("aggregates", 0), m.get("behaviors", 0), m.get("rules", 0), m.get("events", 0),
        m.get("scenarios", 0), m.get("reports", 0), payload["meta"]["generatedAt"])
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE
    html = html.replace("__TITLE__", title)
    html = html.replace("__META_LINE__", meta_line)
    html = html.replace("__LIBS__", libs_html)
    html = html.replace("__DATA__", data_json)
    html = html.replace("__STAMP__", datetime.now().strftime("%Y%m%d%H%M%S"))
    if "__DATA__" in html or "__TITLE__" in html:
        rep.warn("TEMPLATE_TOKEN", "HTML 模板存在未替换的占位符")
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description="本体模型可视化生成器（YAML -> JSON + 自包含 HTML）")
    ap.add_argument("yaml_dir", help="本体 YAML 目录")
    ap.add_argument("-o", "--outdir", default=None, help="输出目录，缺省与 yaml 目录相同")
    ap.add_argument("--html", default="ontology-graph.html")
    ap.add_argument("--json", default="ontology-model.json")
    ap.add_argument("--report", default="validation-report.json")
    ap.add_argument("--strict", action="store_true", help="存在 error 时不出图")
    ap.add_argument("--vendor", default=None, help="ECharts/Mermaid 目录")
    ap.add_argument("--title", default=None, help="页面标题")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    rep = Report()
    models, source_files = load_models(args.yaml_dir, rep)
    if "m1" not in models:
        rep.error("MODEL_M1_MISSING", "目录内未找到 OBJECT 模型，本体图画不出来")
    built = build_graph(models, rep)
    meta = validate(models, built, rep)
    payload = build_payload(models, built, meta["counts"], source_files)
    payload["validation"] = {
        "errors": len(rep.errors), "warnings": len(rep.warnings),
        "items": [{"level": i["level"], "code": i["code"], "message": i["message"]} for i in rep.items][:300],
    }
    if args.title:
        payload["meta"]["title"] = args.title

    outdir = os.path.abspath(args.outdir or args.yaml_dir)
    os.makedirs(outdir, exist_ok=True)

    report_path = os.path.join(outdir, args.report)
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "meta": {"generatedAt": payload["meta"]["generatedAt"], "sourceDir": os.path.abspath(args.yaml_dir),
                     "sourceFiles": source_files},
            "summary": {"errors": len(rep.errors), "warnings": len(rep.warnings), "counts": meta["counts"]},
            "items": rep.items,
        }, fh, ensure_ascii=False, indent=2)

    json_path = os.path.join(outdir, args.json)
    with open(json_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    html_path = None
    if rep.errors and args.strict:
        if not args.quiet:
            print("存在 %d 个 error，--strict 模式下不生成 HTML" % len(rep.errors))
    else:
        libs = build_libs(args.vendor, rep)
        html = render_html(payload, libs, rep)
        html_path = os.path.join(outdir, args.html)
        with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)

    if not args.quiet:
        c = meta["counts"]
        print("模型识别：%s" % "、".join(source_files))
        print("规模：聚合 %d / 关联 %d / 字典 %d / 行为 %d / 规则 %d / 事件 %d / 场景 %d / 报表 %d"
              % (c["aggregates"], c["associations"], c["dictionaries"], c["behaviors"],
                 c["rules"], c["events"], c["scenarios"], c["reports"]))
        print("图：节点 %d / 边 %d" % (len(payload["nodes"]), len(payload["edges"])))
        print("校验：error %d / warning %d  ->  %s" % (len(rep.errors), len(rep.warnings), report_path))
        for item in rep.errors[:20]:
            print("  [E][%s] %s" % (item["code"], item["message"]))
        if len(rep.errors) > 20:
            print("  ... 其余 %d 条见报告" % (len(rep.errors) - 20))
        for item in rep.warnings[:8]:
            print("  [W][%s] %s" % (item["code"], item["message"]))
        if len(rep.warnings) > 8:
            print("  ... 其余 %d 条见报告" % (len(rep.warnings) - 8))
        print("JSON：%s" % json_path)
        if html_path:
            print("HTML：%s（%.1f MB）" % (html_path, os.path.getsize(html_path) / 1024 / 1024))
    return 0 if not rep.errors else 1


# ---------------------------------------------------------------- HTML 模板

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#F5F7FB; --card:#FFFFFF; --line:#E4E9F2; --ink:#1F2937; --ink2:#5B6675; --ink3:#8A94A6;
    --accent:#3B6FE0; --accent-soft:#EDF2FE;
    --object:#3B6FE0; --behavior:#E4573D; --rule:#E8A33D; --event:#8E5CD9; --scenario:#17A673; --report:#5B6472;
  }
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    color:var(--ink);background:var(--bg);font-size:13px;overflow:hidden;}
  #app{display:flex;flex-direction:column;height:100vh;}
  /* ---------- 顶栏 ---------- */
  #topbar{height:64px;flex:0 0 64px;background:var(--card);border-bottom:1px solid var(--line);
    display:flex;align-items:center;justify-content:space-between;padding:0 18px;}
  .brand{display:flex;align-items:center;gap:12px;min-width:0;}
  .logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#3B6FE0,#6C8FF0);
    color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;
    box-shadow:0 4px 12px rgba(59,111,224,.28);}
  .brand h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px;white-space:nowrap;}
  .brand p{margin:2px 0 0;font-size:11.5px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .top-actions{display:flex;gap:8px;}
  button{font-family:inherit;font-size:12.5px;cursor:pointer;border-radius:8px;border:1px solid var(--line);
    background:#fff;color:var(--ink2);padding:6px 12px;transition:.16s;}
  button:hover{border-color:#C7D3E8;color:var(--ink);background:#FAFBFF;}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
  button.primary:hover{background:#2F5FD0;color:#fff;}
  /* ---------- 主体 ---------- */
  #main{flex:1;display:flex;min-height:0;position:relative;}
  #sidebar{width:232px;flex:0 0 232px;background:var(--card);border-right:1px solid var(--line);
    overflow-y:auto;padding:14px 14px 24px;}
  .block{margin-bottom:18px;}
  .block-title{font-size:11.5px;font-weight:650;color:var(--ink3);letter-spacing:.6px;
    text-transform:uppercase;margin-bottom:9px;display:flex;justify-content:space-between;align-items:center;}
  .type-row{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:7px;cursor:pointer;}
  .type-row:hover{background:#F7F9FE;}
  .type-row input{accent-color:var(--accent);cursor:pointer;}
  .dot{width:10px;height:10px;border-radius:50%;flex:0 0 10px;}
  .type-name{flex:1;font-size:12.5px;color:var(--ink);}
  .type-count{font-size:11px;color:var(--ink3);font-variant-numeric:tabular-nums;}
  #search{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);font-size:12.5px;
    font-family:inherit;outline:none;transition:.16s;}
  #search:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,111,224,.14);}
  .mini-select{width:100%;padding:6px 8px;border-radius:8px;border:1px solid var(--line);
    font-family:inherit;font-size:12px;color:var(--ink2);background:#fff;outline:none;cursor:pointer;}
  .mini-select:focus{border-color:var(--accent);}
  #layoutPicker{display:flex;flex-direction:column;gap:5px;}
  .lp-btn{display:flex;align-items:center;gap:7px;width:100%;padding:6px 9px;border-radius:8px;
    border:1px solid var(--line);background:#fff;color:var(--ink2);font-family:inherit;font-size:12px;
    cursor:pointer;transition:.15s;text-align:left;line-height:1.2;}
  .lp-btn:hover{border-color:#C3D3F0;background:#F7FAFF;}
  .lp-btn i{width:7px;height:7px;border-radius:50%;background:#C8D2E2;flex:0 0 auto;}
  .lp-btn.on{border-color:var(--accent);background:var(--accent-soft);color:#1B3A6B;font-weight:600;}
  .lp-btn.on i{background:var(--accent);box-shadow:0 0 0 3px rgba(59,111,224,.16);}
  #searchResult{margin-top:6px;max-height:230px;overflow-y:auto;}
  .sr-item{padding:6px 8px;border-radius:7px;cursor:pointer;display:flex;gap:7px;align-items:center;}
  .sr-item:hover,.sr-item.on{background:var(--accent-soft);}
  .sr-item .nm{font-size:12px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .sr-item .id{font-size:10px;color:var(--ink3);font-family:ui-monospace,Consolas,monospace;}
  .legend-row{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--ink2);padding:2.5px 6px;}
  .legend-line{width:16px;height:0;border-top-width:2px;border-top-style:solid;flex:0 0 16px;}
  .tips p{margin:4px 0;font-size:11.5px;color:var(--ink3);line-height:1.6;}
  /* ---------- 画布 ---------- */
  #stage{flex:1;position:relative;min-width:0;
    background:radial-gradient(1200px 700px at 50% 38%,#FBFCFE 0%,#F3F6FB 55%,#EDF1F8 100%);}
  #chart{position:absolute;inset:0;}
  .hint{position:absolute;left:16px;top:14px;display:flex;gap:8px;align-items:center;
    background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:10px;padding:6px 10px;
    font-size:12px;color:var(--ink2);box-shadow:0 4px 14px rgba(31,41,55,.06);max-width:calc(100% - 32px);}
  .hint b{color:var(--ink);}
  .hint button{padding:3px 8px;font-size:11.5px;border-radius:6px;}
  .hidden{display:none !important;}
  #alert{position:absolute;right:16px;top:14px;max-width:460px;max-height:62vh;overflow:auto;
    background:#FFF4F2;border:1px solid #F3C5BB;
    color:#A33521;border-radius:10px;padding:8px 12px;font-size:11.5px;line-height:1.55;
    box-shadow:0 6px 18px rgba(163,53,33,.10);cursor:pointer;z-index:30;}
  #alert.warn{background:#FFFAEE;border-color:#F0DFB4;color:#8A5B00;
    box-shadow:0 6px 18px rgba(138,91,0,.10);}
  #alert b{font-weight:700;}
  #alert .aitem{padding:2px 0 2px 10px;text-indent:-10px;}
  #alert .aitem i{font-style:normal;opacity:.7;font-family:ui-monospace,Consolas,monospace;font-size:10.5px;}
  /* ---------- 播放条 ---------- */
  .playbar{position:absolute;left:50%;bottom:18px;transform:translateX(-50%);
    width:min(680px,calc(100% - 40px));background:rgba(255,255,255,.98);border:1px solid var(--line);
    border-radius:14px;box-shadow:0 10px 30px rgba(31,41,55,.12);padding:10px 14px;
    display:flex;align-items:center;gap:14px;backdrop-filter:blur(6px);}
  .pb-left{display:flex;align-items:center;gap:6px;}
  .pb-btn{width:34px;height:34px;border-radius:9px;padding:0;display:flex;align-items:center;
    justify-content:center;font-size:13px;}
  .pb-btn.main{background:var(--accent);border-color:var(--accent);color:#fff;width:38px;height:38px;}
  .pb-btn.main:hover{background:#2F5FD0;}
  .pb-mid{flex:1;min-width:0;}
  .pb-label{font-size:12px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    margin-bottom:5px;}
  .pb-label .tag{margin-right:6px;}
  .pb-track{display:flex;align-items:center;gap:10px;}
  #pbRange{flex:1;accent-color:var(--accent);height:4px;cursor:pointer;}
  .pb-count{font-size:11px;color:var(--ink3);font-variant-numeric:tabular-nums;white-space:nowrap;}
  #pbSpeed{border:1px solid var(--line);border-radius:7px;padding:3px 5px;font-family:inherit;
    font-size:11.5px;color:var(--ink2);background:#fff;outline:none;}
  /* ---------- 右面板 ---------- */
  #panel{width:0;flex:0 0 0;background:var(--card);border-left:1px solid var(--line);
    display:flex;flex-direction:column;overflow:hidden;transition:flex-basis .18s ease,width .18s ease;}
  #panel.open{width:452px;flex:0 0 452px;}
  .p-head{display:flex;align-items:flex-start;gap:10px;padding:16px 16px 10px;border-bottom:1px solid var(--line);}
  .p-badge{font-size:11px;font-weight:700;color:#fff;background:var(--ink3);border-radius:6px;
    padding:3px 8px;white-space:nowrap;margin-top:2px;}
  .p-badge.object{background:var(--object);} .p-badge.behavior{background:var(--behavior);}
  .p-badge.rule{background:var(--rule);} .p-badge.event{background:var(--event);}
  .p-badge.scenario{background:var(--scenario);} .p-badge.report{background:var(--report);}
  .p-title-wrap{flex:1;min-width:0;}
  .p-title-wrap h2{margin:0;font-size:15px;font-weight:650;line-height:1.35;}
  .p-title-wrap p{margin:3px 0 0;font-size:10.5px;color:var(--ink3);
    font-family:ui-monospace,Consolas,monospace;word-break:break-all;}
  .p-close{border:none;background:transparent;font-size:20px;line-height:1;color:var(--ink3);
    padding:0 4px;border-radius:6px;}
  .p-close:hover{background:#F2F4F8;color:var(--ink);}
  .p-chips{display:flex;flex-wrap:wrap;gap:6px;padding:10px 16px 0;}
  .chip{font-size:10.5px;padding:2px 8px;border-radius:20px;background:#F1F4F9;color:var(--ink2);
    border:1px solid #E6EBF3;}
  .chip.stage{background:var(--accent-soft);border-color:#DCE6FC;color:#2C5CC5;}
  .p-body{flex:1;overflow-y:auto;padding:6px 16px 40px;}
  .sec{margin-top:16px;}
  .sec h3{font-size:11.5px;font-weight:650;color:var(--ink3);letter-spacing:.5px;margin:0 0 8px;}
  .kv{display:grid;grid-template-columns:96px 1fr;gap:6px 10px;font-size:12px;}
  .kv dt{color:var(--ink3);}
  .kv dd{margin:0;color:var(--ink);word-break:break-word;}
  .txt{font-size:12px;line-height:1.75;color:var(--ink2);white-space:pre-wrap;word-break:break-word;
    background:#FAFBFE;border:1px solid #EEF2F8;border-radius:9px;padding:9px 11px;}
  table.tb{width:100%;border-collapse:collapse;font-size:11.5px;}
  table.tb th{text-align:left;color:var(--ink3);font-weight:600;padding:6px 6px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:#fff;}
  table.tb td{padding:5px 6px;border-bottom:1px solid #F2F5FA;color:var(--ink2);vertical-align:top;
    word-break:break-word;}
  table.tb tr:hover td{background:#FAFBFF;}
  ul.lst{margin:0;padding-left:16px;font-size:12px;line-height:1.75;color:var(--ink2);}
  ul.lst li{margin:2px 0;}
  /* 步骤 */
  .steps{position:relative;padding-left:4px;}
  .step{display:flex;gap:10px;padding:8px 8px 8px 6px;border-radius:9px;cursor:pointer;
    border:1px solid transparent;transition:.14s;position:relative;}
  .step:hover{background:#F8FAFE;border-color:#E7EEFA;}
  .step.active{background:var(--accent-soft);border-color:#C9D9FA;}
  .step.done .step-no{background:#E9F6F0;color:#12805A;border-color:#CBEBDE;}
  .step-no{flex:0 0 42px;height:22px;border-radius:6px;border:1px solid var(--line);background:#fff;
    display:flex;align-items:center;justify-content:center;font-size:10.5px;color:var(--ink3);
    font-family:ui-monospace,Consolas,monospace;}
  .step.active .step-no{background:var(--accent);border-color:var(--accent);color:#fff;}
  .step-main{flex:1;min-width:0;}
  .step-line{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px;}
  .step-line b{font-weight:600;color:var(--ink);}
  .step-obj{font-size:11px;color:var(--ink3);}
  .step-ev{font-size:10.5px;color:#6B3FA0;background:#F4EEFB;border:1px solid #E4D9F4;
    border-radius:5px;padding:1px 5px;white-space:nowrap;}
  .pb-ev{color:#6B3FA0;}
  .step-detail{font-size:11px;color:var(--ink3);line-height:1.6;margin-top:3px;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
  .tag{font-size:10px;padding:1.5px 6px;border-radius:5px;font-weight:600;white-space:nowrap;}
  .tag.behavior{background:#FDECE9;color:#C2432C;} .tag.rule{background:#FDF3E1;color:#A76A12;}
  .tag.event{background:#F2ECFC;color:#6E45B5;} .tag.object{background:#EAF0FD;color:#2C5CC5;}
  /* 指标 */
  .mtabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;}
  .mtab{font-size:11.5px;padding:5px 9px;border-radius:7px;border:1px solid var(--line);background:#fff;
    color:var(--ink2);cursor:pointer;}
  .mtab.on{background:var(--accent);border-color:var(--accent);color:#fff;}
  .mmeta{font-size:11px;color:var(--ink3);line-height:1.7;margin-bottom:8px;word-break:break-word;}
  .mmeta code{background:#F4F6FB;border-radius:4px;padding:1px 5px;font-size:10.5px;color:#2C5CC5;
    font-family:ui-monospace,Consolas,monospace;}
  .mermaid-box{border:1px solid #EEF2F8;border-radius:10px;background:#FCFDFF;padding:12px 8px;
    overflow:auto;min-height:200px;display:flex;align-items:flex-start;justify-content:center;}
  .mermaid-box svg{max-width:100%;height:auto;}
  .mermaid-err{font-size:11.5px;color:#A33521;line-height:1.6;}
  /* 滚动条 */
  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:#D7DEEA;border-radius:8px;}
  ::-webkit-scrollbar-track{background:transparent;}
  @media (max-width:1100px){
    #sidebar{display:none;}
    #panel.open{width:100%;flex:0 0 100%;position:absolute;right:0;top:0;bottom:0;z-index:20;}
  }
</style>
</head>
<body>
<div id="app">
  <header id="topbar">
    <div class="brand">
      <div class="logo">本</div>
      <div style="min-width:0">
        <h1>__TITLE__</h1>
        <p id="metaLine">__META_LINE__</p>
      </div>
    </div>
    <div class="top-actions">
      <button id="btnIssues" style="display:none" title="查看模型校验结果"></button>
      <button id="btnReset" title="退出聚焦、恢复全部类型">全景</button>
      <button id="btnRelayout" title="重新计算力导向布局">重新布局</button>
      <button id="btnFit" title="重置缩放与平移">适应窗口</button>
      <button id="btnPng" class="primary" title="导出当前视图为 PNG">导出 PNG</button>
    </div>
  </header>

  <div id="main">
    <aside id="sidebar">
      <div class="block">
        <div class="block-title"><span>节点类型</span><span class="type-count" id="visibleCount"></span></div>
        <div id="typeFilters"></div>
      </div>
      <div class="block">
        <div class="block-title"><span>检索定位</span><span class="type-count" id="searchCount"></span></div>
        <input id="search" placeholder="ID / 名称 / 别名…" autocomplete="off">
        <div id="searchResult"></div>
      </div>
      <div class="block">
        <div class="block-title"><span>布局形式</span><span class="type-count" id="layoutHint"></span></div>
        <div id="layoutPicker"></div>
      </div>
      <div class="block">
        <div class="block-title"><span>标签密度</span></div>
        <select id="labelMode" class="mini-select">
          <option value="main" selected>主要节点</option>
          <option value="all">全部节点</option>
          <option value="hover">仅悬停</option>
        </select>
      </div>
      <div class="block">
        <div class="block-title"><span>关系图例</span></div>
        <div id="edgeLegend"></div>
      </div>
      <div class="block tips">
        <div class="block-title"><span>操作提示</span></div>
        <p>滚轮缩放，拖拽平移，点击节点看详情。</p>
        <p>点击<b>场景</b>进入流程视图并逐阶段播放。</p>
        <p>点击<b>报表</b>查看指标归因树。</p>
      </div>
    </aside>

    <div id="stage">
      <div id="chart"></div>
      <div class="hint hidden" id="focusHint"></div>
      <div id="alert" class="hidden"></div>
      <div class="playbar hidden" id="playbar">
        <div class="pb-left">
          <button class="pb-btn main" id="pbPlay" title="播放 / 暂停">▶</button>
          <button class="pb-btn" id="pbPrev" title="上一步">◀</button>
          <button class="pb-btn" id="pbNext" title="下一步">▶❙</button>
        </div>
        <div class="pb-mid">
          <div class="pb-label" id="pbLabel">未开始</div>
          <div class="pb-track">
            <input type="range" id="pbRange" min="0" max="0" value="0" step="1">
            <span class="pb-count" id="pbCount">0 / 0</span>
            <select id="pbSpeed">
              <option value="0.5">0.5×</option>
              <option value="1" selected>1×</option>
              <option value="2">2×</option>
            </select>
          </div>
        </div>
      </div>
    </div>

    <section id="panel">
      <div class="p-head">
        <div class="p-badge" id="pBadge">对象</div>
        <div class="p-title-wrap">
          <h2 id="pTitle">—</h2>
          <p id="pSub"></p>
        </div>
        <button class="p-close" id="pClose" title="关闭">×</button>
      </div>
      <div class="p-chips" id="pChips"></div>
      <div class="p-body" id="pBody"></div>
    </section>
  </div>
</div>

<script id="model-data" type="application/json">__DATA__</script>
__LIBS__
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("model-data").textContent);
  var CAT = {}, EDGE = {}, PANELS = DATA.panels || {}, SCN = {}, RPT = {};
  DATA.categories.forEach(function (c, i) { CAT[c.key] = { name: c.name, color: c.color, index: i }; });
  DATA.edgeTypes.forEach(function (e) { EDGE[e.key] = e; });
  (DATA.scenarios || []).forEach(function (s) { SCN[s.id] = s; });
  (DATA.reports || []).forEach(function (r) { RPT[r.id] = r; });

  var TYPE_ORDER = ["object", "behavior", "rule", "event", "scenario", "report"];
  var NODE_SIZE = 27;   // 所有节点统一半径（直径，px），不再按连接数缩放
  // 布局预设：三种力导向 + 分组力导向（按类型给初值）+ 环形排列
  var LAYOUTS = {
    std: {
      name: "力导向 · 标准", layout: "force",
      force: { initLayout: "circular", repulsion: 70, edgeLength: [38, 95], gravity: 0.09, friction: 0.18 }
    },
    wide: {
      name: "力导向 · 宽松", layout: "force",
      force: { initLayout: "circular", repulsion: 115, edgeLength: [52, 122], gravity: 0.06, friction: 0.16 }
    },
    tight: {
      name: "力导向 · 紧凑", layout: "force",
      force: { initLayout: "circular", repulsion: 32, edgeLength: [24, 60], gravity: 0.2, friction: 0.22 }
    },
    ring: { name: "环形排列", layout: "circular", circular: { rotateLabel: false } },
    grp: {
      name: "分组力导向", layout: "force",
      force: { initLayout: "none", repulsion: 120, edgeLength: [55, 125], gravity: 0.05, friction: 0.16 },
      initByType: true
    }
  };
  var LAYOUT_ORDER = ["wide", "std", "tight", "grp", "ring"];
  var state = {
    types: {}, focus: null, focusKind: null, focusId: null,
    playIndex: -1, playing: false, timer: null, speed: 1,
    selected: null, chart: null, nodesIdx: {}, mmdSeq: 0, searchHits: [], searchPos: -1,
    labelMode: "main", needFit: true, layout: "wide"
  };
  TYPE_ORDER.forEach(function (t) { state.types[t] = true; });

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var short = function (s, n) {
    s = String(s || ""); n = n || 11;
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };

  /* ---------------- 侧栏 ---------------- */
  function buildLayoutPicker() {
    var box = $("layoutPicker");
    if (!box) return;
    box.innerHTML = "";
    LAYOUT_ORDER.forEach(function (key) {
      var b = document.createElement("button");
      b.className = "lp-btn" + (key === state.layout ? " on" : "");
      b.setAttribute("data-layout", key);
      b.innerHTML = "<i></i><span>" + esc(LAYOUTS[key].name) + "</span>";
      b.addEventListener("click", function () { setLayout(key); });
      box.appendChild(b);
    });
    syncLayoutPicker();
  }

  function syncLayoutPicker() {
    document.querySelectorAll(".lp-btn").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-layout") === state.layout);
    });
    var h = $("layoutHint");
    if (h) h.textContent = LAYOUTS[state.layout] ? LAYOUTS[state.layout].name.replace(/^.*·\s*/, "") : "";
  }

  function setLayout(key) {
    if (!LAYOUTS[key] || key === state.layout) return;
    state.layout = key;
    syncLayoutPicker();
    render(false);   // 重新计算布局并自适应
  }

  function buildSidebar() {
    buildLayoutPicker();
    var box = $("typeFilters");
    var counts = {};
    DATA.nodes.forEach(function (n) { counts[n.type] = (counts[n.type] || 0) + 1; });
    TYPE_ORDER.forEach(function (t) {
      var row = document.createElement("label");
      row.className = "type-row";
      row.innerHTML = '<input type="checkbox" checked data-type="' + t + '">' +
        '<span class="dot" style="background:' + CAT[t].color + '"></span>' +
        '<span class="type-name">' + CAT[t].name + '</span>' +
        '<span class="type-count">' + (counts[t] || 0) + '</span>';
      box.appendChild(row);
    });
    box.addEventListener("change", function (e) {
      var t = e.target.getAttribute("data-type");
      if (!t) return;
      state.types[t] = e.target.checked;
      if (!e.target.checked && state.selected && (byId(state.selected) || {}).type === t) state.selected = null;
      render();
    });
    var lg = $("edgeLegend");
    DATA.edgeTypes.forEach(function (e) {
      var d = document.createElement("div");
      d.className = "legend-row";
      d.innerHTML = '<span class="legend-line" style="border-top-color:' + e.color + '"></span>' + esc(e.name);
      lg.appendChild(d);
    });
    var V = DATA.validation || { errors: 0, warnings: 0, items: [] };
    if (V.errors > 0 || V.warnings > 0) {
      var a = $("alert"), btn = $("btnIssues");
      var items = (V.items || []).slice(0, 60).map(function (i) {
        return '<div class="aitem">· <i>' + esc(i.code) + "</i> " + esc(i.message) +
          (i.refs && i.refs.length ? ' <i>(' + esc(i.refs.join(", ")) + ")</i>" : "") + "</div>";
      }).join("");
      a.innerHTML = "<b>模型校验：" + V.errors + " 个错误 / " + V.warnings + " 个警告</b>" +
        '<div style="margin:3px 0 5px;opacity:.8">' +
        (V.errors > 0 ? "图已按容错模式生成（缺失引用以浅色节点占位），"
          : "无阻断性错误，图完整生成，下列为规范提示项，") +
        "点击此处关闭；完整记录见 validation-report.json。</div>" + items;
      if (V.warnings > 60) a.innerHTML += '<div style="opacity:.7">… 其余 ' + (V.warnings - 60) + " 条见报告</div>";
      a.classList.add("warn");
      if (V.errors > 0) a.classList.remove("warn");
      // 有错误时默认展开；仅警告时收在顶栏入口里，避免干扰阅读
      if (V.errors > 0) a.classList.remove("hidden");
      btn.style.display = "";
      btn.textContent = (V.errors > 0 ? "⚠ " + V.errors + " 错误" : "") +
        (V.warnings > 0 ? (V.errors > 0 ? " / " : "") + "提示 " + V.warnings : "");
      btn.title = "查看 " + (V.errors + V.warnings) + " 条模型校验结果";
      btn.addEventListener("click", function () { a.classList.toggle("hidden"); });
      a.addEventListener("click", function () { a.classList.add("hidden"); });
    }
  }

  function byId(id) { return state.nodesIdx[id]; }

  /* ---------------- 视图数据 ---------------- */
  function buildView() {
    var allow = state.types;
    var focusSet = state.focus ? {} : null;
    if (focusSet) state.focus.forEach(function (id) { focusSet[id] = 1; });
    var nodes = DATA.nodes.filter(function (n) {
      return allow[n.type] && (!focusSet || focusSet[n.id]);
    });
    var ids = {};
    nodes.forEach(function (n) { ids[n.id] = 1; });
    var links = [];
    var seen = {};
    DATA.edges.forEach(function (e) {
      if (!ids[e.source] || !ids[e.target]) return;
      var k = e.source + "|" + e.target + "|" + e.type;
      if (seen[k]) return;
      seen[k] = 1;
      links.push({ source: e.source, target: e.target, type: e.type, label: e.label });
    });
    if (state.focusKind === "scenario" && SCN[state.focusId]) {
      SCN[state.focusId].sequence.forEach(function (st) {
        if (!st.refId || !ids[st.refId]) return;
        var k = state.focusId + "|" + st.refId + "|step";
        if (seen[k]) return;
        seen[k] = 1;
        links.push({ source: state.focusId, target: st.refId, type: "step", label: "编排" });
      });
    }
    var deg = {};
    nodes.forEach(function (n) { deg[n.id] = 0; });
    links.forEach(function (l) {
      deg[l.source] = (deg[l.source] || 0) + 1;
      deg[l.target] = (deg[l.target] || 0) + 1;
    });
    return { nodes: nodes, links: links, deg: deg };
  }

  /* ---------------- 渲染图 ---------------- */
  function stepRefs(st) {
    if (!st) return [];
    if (st.refs && st.refs.length) return st.refs;
    return st.refId ? [st.refId] : [];
  }

  /* 分组初值：每类节点占一段圆弧作为力导向的起始位置，松弛后自然成簇 */
  function typeInitialPositions(nodes) {
    var byType = {}, pos = {};
    nodes.forEach(function (n) { (byType[n.type] = byType[n.type] || []).push(n); });
    var types = TYPE_ORDER.filter(function (t) { return byType[t] && byType[t].length; });
    if (!types.length) return pos;
    var step = Math.PI * 2 / types.length;
    types.forEach(function (t, ti) {
      var arr = byType[t];
      var base = -Math.PI / 2 + ti * step;
      var spread = step * 0.66;
      arr.forEach(function (n, j) {
        var t01 = arr.length <= 1 ? 0.5 : j / (arr.length - 1);
        var ang = base + (t01 - 0.5) * spread;
        var rad = 300 + 46 * (j % 4);
        pos[n.id] = [Math.cos(ang) * rad, Math.sin(ang) * rad];
      });
    });
    return pos;
  }

  function render(keepLayout) {
    if (!state.chart) return;
    var view = buildView();
    var LP = LAYOUTS[state.layout] || LAYOUTS.wide;
    var seq = (state.focusKind === "scenario" && SCN[state.focusId]) ? SCN[state.focusId].sequence : [];
    // 主标签阈值：取度数排名前 ~28 的节点，保证不同规模全景图的标签密度都稳定
    var degs = view.nodes.map(function (n) { return view.deg[n.id] || 0; })
      .sort(function (a, b) { return b - a; });
    var labelCut = degs.length ? Math.max(1, degs[Math.min(27, degs.length - 1)]) : 1;
    var visited = {}, activeSet = {}, activeObj = null;
    if (state.playIndex >= 0 && seq.length) {
      for (var i = 0; i <= state.playIndex && i < seq.length; i++) {
        stepRefs(seq[i]).forEach(function (r) { visited[r] = 1; });
        if (seq[i].objectId) visited[seq[i].objectId] = 1;
      }
      var cur = seq[Math.min(state.playIndex, seq.length - 1)];
      stepRefs(cur).forEach(function (r) { activeSet[r] = 1; });
      activeObj = cur.objectId;
    }
    var playing = state.playIndex >= 0;
    // 播放至最后一步：所有已播放节点与其之间的连线整体高亮
    var allDone = playing && seq.length > 0 && state.playIndex >= seq.length - 1;
    var neighbor = {};
    if (state.selected && !playing) {
      neighbor[state.selected] = 1;
      view.links.forEach(function (l) {
        if (l.source === state.selected) neighbor[l.target] = 1;
        if (l.target === state.selected) neighbor[l.source] = 1;
      });
    }
    var dataNodes = view.nodes.map(function (n) {
      var deg = view.deg[n.id] || 0;
      var isActive = !!activeSet[n.id];
      var isVisited = !!visited[n.id];
      var isRelated = activeObj && n.id === activeObj;
      var dim = false;
      if (playing && !allDone && !isVisited) dim = true;
      if (state.selected && !playing && !neighbor[n.id]) dim = true;
      if (n.missing) dim = false;
      var border = "#FFFFFF", bw = 2, shadow = 0, shadowColor = "transparent";
      if (isActive) { border = "#1F2937"; bw = 3.2; shadow = 18; shadowColor = CAT[n.type].color; }
      else if (isRelated) { border = CAT[n.type].color; bw = 3; shadow = 10; shadowColor = CAT[n.type].color; }
      else if (isVisited) { border = "#12805A"; bw = 2.5; }
      if (n.missing) { border = "#E4573D"; }
      var showLabel = state.labelMode === "all" ? true
        : state.labelMode === "hover" ? (isActive || isRelated || state.selected === n.id)
        : (isActive || isRelated || state.selected === n.id || (state.focus ? true : deg >= labelCut));
      return {
        id: n.id, name: n.name, category: n.category, value: deg,
        symbol: "circle",
        symbolSize: NODE_SIZE,
        itemStyle: {
          color: n.missing ? "#F3B7AC" : CAT[n.type].color,
          borderColor: border, borderWidth: bw,
          opacity: dim ? 0.16 : 1,
          shadowBlur: shadow, shadowColor: shadowColor
        },
        label: {
          show: showLabel, position: "right", distance: 5,
          formatter: short(n.name, isActive ? 15 : 11),
          fontSize: n.type === "object" ? 11.5 : 10.5,
          fontWeight: (isActive || isVisited) ? 700 : 400,
          color: dim ? "#B9C2D0" : "#2A3444",
          textBorderColor: "#FFFFFF", textBorderWidth: 2
        },
        labelLayout: { hideOverlap: true },
        _meta: n
      };
    });
    if (LP.initByType) {   // 分组力导向：给出按类型分布的起始坐标
      var initPos = typeInitialPositions(view.nodes);
      dataNodes.forEach(function (d) {
        var p = initPos[d.id];
        if (p) { d.x = p[0]; d.y = p[1]; }
      });
    }
    var dataLinks = view.links.map(function (l) {
      var meta = EDGE[l.type] || { color: "#C3CCDB", name: "关系" };
      var on = false;
      if (allDone) {
        // 播放完成：整个场景子图（含所有节点与连线）全部点亮
        on = true;
      } else if (playing) {
        if (l.source === state.focusId && activeSet[l.target]) on = true;
        if (l.target === state.focusId && activeSet[l.source]) on = true;
        if (activeSet[l.source] || activeSet[l.target]) on = true;
      }
      if (state.selected && !playing && (l.source === state.selected || l.target === state.selected)) on = true;
      var opacity = allDone ? 1
        : playing ? (on ? 0.95 : 0.06)
        : (state.selected && !on ? 0.12 : 0.5);
      return {
        source: l.source, target: l.target,
        value: meta.name + (l.label && l.label !== meta.name ? "：" + l.label : ""),
        lineStyle: {
          color: meta.color,
          width: allDone ? 1.8 : (on ? 2.3 : 1.1),
          opacity: opacity,
          curveness: 0
        },
        emphasis: { lineStyle: { width: 3.2, opacity: 1 } },
        symbol: ["none", "arrow"], symbolSize: 4
      };
    });
    var forceCfg = null;
    if (LP.force) {
      forceCfg = {
        initLayout: LP.force.initLayout, repulsion: LP.force.repulsion,
        edgeLength: LP.force.edgeLength.slice(), gravity: LP.force.gravity,
        friction: LP.force.friction, layoutAnimation: false
      };
      if (view.nodes.length <= 30) {   // 聚焦子图节点少，按比例放大间距
        forceCfg.repulsion = Math.round(forceCfg.repulsion * 2.2);
        forceCfg.edgeLength = [Math.round(forceCfg.edgeLength[0] * 1.3),
                               Math.round(forceCfg.edgeLength[1] * 1.3)];
      }
    }
    var seriesConf = {
      type: "graph", layout: LP.layout, roam: true, draggable: true,
      top: 24, bottom: 24, left: 24, right: 24,
      categories: DATA.categories.map(function (c) { return { name: c.name, itemStyle: { color: c.color } }; }),
      emphasis: { focus: "adjacency", scale: false, label: { show: true, fontWeight: 700 } },
      labelLayout: { hideOverlap: true },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 4,
      data: dataNodes, links: dataLinks
    };
    if (forceCfg) seriesConf.force = forceCfg;
    if (LP.circular) seriesConf.circular = LP.circular;
    var option = {
      backgroundColor: "transparent",
      animationDuration: 420,
      animationDurationUpdate: 320,
      tooltip: {
        confine: true,
        backgroundColor: "rgba(255,255,255,.98)",
        borderColor: "#E4E9F2", borderWidth: 1,
        padding: [8, 11],
        textStyle: { color: "#1F2937", fontSize: 12 },
        extraCssText: "box-shadow:0 8px 24px rgba(31,41,55,.12);border-radius:10px;",
        formatter: function (p) {
          if (p.dataType === "edge") {
            return '<div style="font-size:11px;color:#8A94A6">' + esc(p.data.value || "") + "</div>" +
              '<div style="margin-top:2px">' + esc(p.data.source) + " → " + esc(p.data.target) + "</div>";
          }
          var m = (p.data && p.data._meta) || {};
          var t = CAT[m.type] || { name: "", color: "#999" };
          return '<div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
            t.color + ';margin-right:6px"></span><b>' + esc(m.name || p.name) + "</b></div>" +
            '<div style="font-size:10.5px;color:#8A94A6;margin-top:3px">' + esc(t.name) +
            " · 度数 " + (m.degree || 0) + "</div>" +
            '<div style="font-size:10px;color:#A7B0C0;font-family:Consolas,monospace;margin-top:2px">' +
            esc(m.id) + "</div>" + (m.missing ? '<div style="color:#E4573D;font-size:10.5px;margin-top:3px">⚠ 缺失引用节点</div>' : "");
        }
      },
      series: [seriesConf]
    };
    if (!keepLayout) state.needFit = true;
    state.chart.setOption(option, { notMerge: !keepLayout, lazyUpdate: false });
    $("visibleCount").textContent = view.nodes.length + " / " + DATA.nodes.length;
    if (!keepLayout) scheduleFit();
  }

  /* ---------------- 面板 ---------------- */
  function openPanel(panel) {
    if (!panel) return;
    $("pBadge").textContent = panel.badge || "";
    $("pBadge").className = "p-badge " + (panel.kind || "");
    $("pTitle").textContent = panel.title || "";
    $("pSub").textContent = panel.subtitle || "";
    var chips = (panel.chips || []).filter(function (c) { return c.label; }).map(function (c) {
      return '<span class="chip ' + (c.kind === "stage" ? "stage" : "") + '">' + esc(c.label) + "</span>";
    }).join("");
    $("pChips").innerHTML = chips;
    $("pBody").innerHTML = (panel.sections || []).map(renderSection).join("");
    $("panel").classList.add("open");
    bindPanelEvents(panel);
    if (panel.kind === "scenario") syncStepHighlight();
    setTimeout(function () { if (state.chart) state.chart.resize(); }, 190);
  }

  function closePanel() {
    $("panel").classList.remove("open");
    setTimeout(function () { if (state.chart) state.chart.resize(); }, 190);
  }

  function renderSection(s) {
    var head = s.title ? "<h3>" + esc(s.title) + "</h3>" : "";
    if (s.type === "kv") {
      return '<div class="sec">' + head + '<dl class="kv">' + s.rows.map(function (r) {
        return "<dt>" + esc(r[0]) + "</dt><dd>" + esc(r[1]) + "</dd>";
      }).join("") + "</dl></div>";
    }
    if (s.type === "text") {
      return '<div class="sec">' + head + '<div class="txt">' + esc(s.text) + "</div></div>";
    }
    if (s.type === "list") {
      return '<div class="sec">' + head + '<ul class="lst">' + (s.items || []).map(function (i) {
        return "<li>" + esc(i) + "</li>";
      }).join("") + "</ul></div>";
    }
    if (s.type === "table") {
      return '<div class="sec">' + head + '<table class="tb"><thead><tr>' + (s.head || []).map(function (h) {
        return "<th>" + esc(h) + "</th>";
      }).join("") + "</tr></thead><tbody>" + (s.rows || []).map(function (r) {
        return "<tr>" + r.map(function (c) { return "<td>" + esc(c) + "</td>"; }).join("") + "</tr>";
      }).join("") + "</tbody></table></div>";
    }
    if (s.type === "steps") {
      return '<div class="sec">' + head + '<div class="steps" id="stepList">' + (s.items || []).map(function (it, i) {
        var cls = it.refType || "object";
        var evs = (it.events || []).map(function (e) { return esc(e.name); }).join("、");
        return '<div class="step" data-step="' + i + '" title="源步骤 ' + esc(it.sourceStepId || it.index) + '">' +
          '<div class="step-no">' + esc(it.seqNo || it.index) + "</div>" +
          '<div class="step-main"><div class="step-line">' +
          '<span class="tag ' + cls + '">' + esc(it.typeName) + "</span>" +
          "<b>" + esc(it.refName) + "</b>" +
          (evs ? '<span class="step-ev">＋事件 ' + evs + "</span>" : "") +
          (it.objectName ? '<span class="step-obj">作用于 ' + esc(it.objectName) + "</span>" : "") +
          '</div><div class="step-detail">' + esc(it.detail || "") + "</div></div></div>";
      }).join("") + "</div></div>";
    }
    if (s.type === "metrics") {
      var items = s.items || [];
      if (!items.length) return '<div class="sec">' + head + '<div class="txt">该报表未定义结果列。</div></div>';
      var tabs = items.map(function (m, i) {
        return '<button class="mtab' + (i === 0 ? " on" : "") + '" data-m="' + i + '">' + esc(m.label) + "</button>";
      }).join("");
      return '<div class="sec">' + head + '<div class="mtabs" id="mtabs">' + tabs + "</div>" +
        '<div class="mmeta" id="mmeta"></div><div class="mermaid-box" id="mmdBox"></div></div>';
    }
    return "";
  }

  function bindPanelEvents(panel) {
    var body = $("pBody");
    var steps = body.querySelectorAll(".step");
    if (steps.length) {
      steps.forEach(function (el) {
        el.addEventListener("click", function () {
          var i = parseInt(el.getAttribute("data-step"), 10);
          state.playing = false; clearTimer();
          jumpTo(i);
        });
      });
    }
    var tabs = body.querySelectorAll(".mtab");
    if (tabs.length && panel.kind === "report") {
      var rep = RPT[panel.subtitle];
      var showMetric = function (i) {
        tabs.forEach(function (t) { t.classList.toggle("on", parseInt(t.getAttribute("data-m"), 10) === i); });
        var m = rep.metrics[i];
        $("mmeta").innerHTML = "口径 <code>" + esc(m.agg) + "</code> · 类型 <code>" + esc(m.dataType) +
          "</code><br>公式 <code>" + esc(m.formula) + "</code>";
        renderMermaid(rep.id + "-" + m.key, m.mermaid);
      };
      tabs.forEach(function (t) {
        t.addEventListener("click", function () { showMetric(parseInt(t.getAttribute("data-m"), 10)); });
      });
      if (rep && rep.metrics.length) showMetric(0);
    }
  }

  function renderMermaid(key, code) {
    var box = $("mmdBox");
    if (!box) return;
    box.innerHTML = "";
    if (typeof mermaid === "undefined") {
      box.innerHTML = '<div class="mermaid-err">Mermaid 未加载（离线且无本地依赖），无法渲染指标树。</div>';
      return;
    }
    var id = "mmd" + (++state.mmdSeq);
    try {
      var out = mermaid.render(id, code);
      if (out && typeof out.then === "function") {
        out.then(function (res) {
          box.innerHTML = res.svg || "";
        }).catch(function (err) {
          box.innerHTML = '<div class="mermaid-err">指标树渲染失败：' + esc(err && err.message || err) +
            '</div><pre style="font-size:10px;color:#8A94A6;white-space:pre-wrap">' + esc(code) + "</pre>";
        });
      } else if (typeof out === "string") {
        box.innerHTML = out;
      }
    } catch (err) {
      box.innerHTML = '<div class="mermaid-err">指标树渲染失败：' + esc(err && err.message || err) +
        '</div><pre style="font-size:10px;color:#8A94A6;white-space:pre-wrap">' + esc(code) + "</pre>";
    }
  }

  /* ---------------- 聚焦 ---------------- */
  function enterFocus(kind, id) {
    var obj = kind === "scenario" ? SCN[id] : RPT[id];
    if (!obj) return;
    state.focusKind = kind; state.focusId = id;
    state.focus = obj.focus || [id];
    state.selected = null;
    state.playIndex = -1; state.playing = false; clearTimer();
    $("focusHint").classList.remove("hidden");
    $("focusHint").innerHTML = "<span>聚焦" + (kind === "scenario" ? "场景" : "报表") + "：<b>" +
      esc(obj.name) + "</b> · " + state.focus.length + " 个节点</span>" +
      '<button id="hintExit">返回全景</button>';
    $("hintExit").addEventListener("click", exitFocus);
    $("playbar").classList.toggle("hidden", kind !== "scenario");
    if (kind === "scenario") setupPlaybar(obj);
    render();
  }

  function exitFocus() {
    state.focus = null; state.focusKind = null; state.focusId = null;
    state.playIndex = -1; state.playing = false; clearTimer();
    $("focusHint").classList.add("hidden");
    $("playbar").classList.add("hidden");
    render();
  }

  /* ---------------- 播放 ---------------- */
  function setupPlaybar(scn) {
    var max = scn.sequence.length - 1;
    var r = $("pbRange");
    r.max = max < 0 ? 0 : max;
    r.value = 0;
    $("pbCount").textContent = "0 / " + scn.sequence.length;
    $("pbLabel").textContent = "准备播放：" + scn.name + "，共 " + scn.sequence.length + " 个阶段";
    $("pbPlay").textContent = "▶";
  }

  function clearTimer() { if (state.timer) { clearTimeout(state.timer); state.timer = null; } }

  function jumpTo(i) {
    var scn = SCN[state.focusId]; if (!scn) return;
    state.playIndex = Math.max(0, Math.min(i, scn.sequence.length - 1));
    applyStep();
  }

  function applyStep() {
    var scn = SCN[state.focusId]; if (!scn) return;
    var st = scn.sequence[state.playIndex];
    if (!st) return;
    $("pbRange").value = state.playIndex;
    $("pbCount").textContent = (state.playIndex + 1) + " / " + scn.sequence.length;
    var isLast = state.playIndex >= scn.sequence.length - 1;
    var evTxt = (st.events && st.events.length)
      ? ' <span class="pb-ev">＋事件 ' + st.events.map(function (e) { return esc(e.name); }).join("、") + "</span>"
      : "";
    $("pbLabel").innerHTML = (isLast ? '<span style="color:#12805A;font-weight:600">✓ 全部完成</span> · ' : "") +
      '<span class="tag ' + (st.refType || "object") + '">' + esc(st.typeName) + "</span>" +
      esc(st.seqNo || st.index) + " · " + esc(st.refName) + evTxt +
      (st.objectName ? ' <span style="color:#8A94A6">→ ' + esc(st.objectName) + "</span>" : "");
    render(true);
    syncStepHighlight();
    var el = document.querySelector('.step[data-step="' + state.playIndex + '"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function syncStepHighlight() {
    document.querySelectorAll(".step").forEach(function (el) {
      var i = parseInt(el.getAttribute("data-step"), 10);
      el.classList.toggle("active", i === state.playIndex);
      el.classList.toggle("done", state.playIndex >= 0 && i < state.playIndex);
    });
  }

  function togglePlay() {
    var scn = SCN[state.focusId]; if (!scn) return;
    if (state.playing) {
      state.playing = false; clearTimer(); $("pbPlay").textContent = "▶"; return;
    }
    if (state.playIndex >= scn.sequence.length - 1) state.playIndex = -1;
    state.playing = true;
    $("pbPlay").textContent = "❚❚";
    step();
  }

  function step() {
    var scn = SCN[state.focusId]; if (!scn) return;
    if (!state.playing) return;
    var next = state.playIndex + 1;
    if (next >= scn.sequence.length) {
      state.playing = false; $("pbPlay").textContent = "▶"; clearTimer();
      $("pbLabel").innerHTML = '<span style="color:#12805A;font-weight:600">✓ 场景播放完成</span>';
      return;
    }
    state.playIndex = next;
    applyStep();
    state.timer = setTimeout(step, 1700 / state.speed);
  }

  /* ---------------- 点击 ---------------- */
  function onNodeClick(id) {
    var n = byId(id) || DATA.nodes.filter(function (x) { return x.id === id; })[0];
    if (!n) return;
    if (n.type === "scenario") {
      enterFocus("scenario", id);
      openPanel(PANELS[id]);
      return;
    }
    if (n.type === "report") {
      enterFocus("report", id);
      openPanel(PANELS[id]);
      return;
    }
    state.selected = id;
    render(true);
    openPanel(PANELS[id]);
  }

  /* ---------------- 检索 ---------------- */
  function doSearch() {
    var q = ($("search").value || "").trim().toLowerCase();
    var box = $("searchResult");
    box.innerHTML = "";
    state.searchHits = []; state.searchPos = -1;
    if (!q) { $("searchCount").textContent = ""; return; }
    var hits = DATA.nodes.filter(function (n) {
      return n.id.toLowerCase().indexOf(q) >= 0 ||
        String(n.name).toLowerCase().indexOf(q) >= 0 ||
        String(n.alias || "").toLowerCase().indexOf(q) >= 0;
    }).slice(0, 24);
    state.searchHits = hits;
    $("searchCount").textContent = hits.length + " 个命中";
    if (!hits.length) {
      box.innerHTML = '<div style="font-size:11.5px;color:#8A94A6;padding:6px">无匹配节点</div>';
      return;
    }
    hits.forEach(function (n, i) {
      var d = document.createElement("div");
      d.className = "sr-item";
      d.innerHTML = '<span class="dot" style="background:' + CAT[n.type].color + '"></span>' +
        '<span class="nm">' + esc(short(n.name, 12)) + '</span><span class="id">' + esc(short(n.id, 16)) + "</span>";
      d.addEventListener("click", function () { focusNode(n.id); });
      box.appendChild(d);
    });
  }

  function focusNode(id) {
    var n = byId(id) || DATA.nodes.filter(function (x) { return x.id === id; })[0];
    if (!n) return;
    if (!state.types[n.type]) {
      state.types[n.type] = true;
      var cb = document.querySelector('input[data-type="' + n.type + '"]');
      if (cb) cb.checked = true;
    }
    if (state.focus && state.focus.indexOf(id) < 0) exitFocus();
    state.selected = id;
    render(true);
    locate(id);
    openPanel(PANELS[id]);
  }

  function locate(id) {
    var view = buildView();
    var idx = -1;
    view.nodes.forEach(function (n, i) { if (n.id === id) idx = i; });
    if (idx < 0) return;
    try {
      var ser = state.chart.getModel().getSeriesByIndex(0);
      var layout = ser.getData().getItemLayout(idx);
      if (layout && layout.length >= 2) {
        state.chart.setOption({ series: [{ center: [layout[0], layout[1]], zoom: 1.35 }] });
      }
    } catch (e) { /* 布局尚未完成时忽略 */ }
  }

  /* ---------------- 初始化 ---------------- */
  function autoFit() {
    var ser = state.chart.getModel().getSeriesByIndex(0);
    if (!ser) return;
    var d = ser.getData(), n = d.count(), minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (var i = 0; i < n; i++) {
      var l = d.getItemLayout(i);
      if (!l || !isFinite(l[0]) || !isFinite(l[1])) continue;
      minX = Math.min(minX, l[0]); maxX = Math.max(maxX, l[0]);
      minY = Math.min(minY, l[1]); maxY = Math.max(maxY, l[1]);
    }
    if (!isFinite(minX)) return;
    var padX = state.chart.getWidth() < 900 ? 90 : 150;
    var pbOn = $("playbar") && !$("playbar").classList.contains("hidden");
    var padY = pbOn ? 190 : 120;
    var w = Math.max(state.chart.getWidth() - padX, 200);
    var h = Math.max(state.chart.getHeight() - padY, 200);
    var spanX = Math.max(maxX - minX, 60), spanY = Math.max(maxY - minY, 60);
    var z = Math.min(w / spanX, h / spanY) * 1.02;
    z = Math.max(0.45, Math.min(z, 3.0));
    state.chart.setOption({ series: [{ zoom: z, center: [(minX + maxX) / 2, (minY + maxY) / 2] }] });
  }

  /* 力导向布局是逐帧收敛的，首次 finished 时坐标往往还没稳定，
     因此在渲染后的一小段时间内重复校正视图，避免出现「一小团缩在中间」。 */
  function scheduleFit() {
    state.fitUntil = Date.now() + 2400;
    if (state.fitTimer) { try { autoFit(); } catch (e) {} return; }
    try { autoFit(); } catch (e) { /* 忽略 */ }
    state.fitTimer = setInterval(function () {
      if (Date.now() > state.fitUntil) {
        clearInterval(state.fitTimer); state.fitTimer = null;
        state.needFit = false;
        return;
      }
      try { autoFit(); } catch (e) { /* 忽略 */ }
    }, 320);
  }

  function initChart() {
    if (typeof echarts === "undefined") {
      $("chart").innerHTML = '<div style="padding:40px;text-align:center;color:#A33521;font-size:13px">' +
        "ECharts 未能加载（离线环境且无本地依赖）。请将 echarts.min.js 放入 vendor/ 目录后重新生成。</div>";
      return;
    }
    state.chart = echarts.init($("chart"), null, { renderer: "canvas" });
    state.chart.on("click", function (p) {
      if (p.dataType === "node" && p.data && p.data.id) onNodeClick(p.data.id);
      else if (p.dataType === "edge") { state.selected = null; render(true); }
    });
    state.chart.on("finished", function () {
      if (!state.needFit) return;
      state.needFit = false;
      try { autoFit(); } catch (e) { /* 布局异常时保持默认视图 */ }
    });
    window.addEventListener("resize", function () { state.chart.resize(); });
    render();
  }

  function bindUI() {
    $("pClose").addEventListener("click", function () { closePanel(); state.selected = null; render(true); });
    $("btnReset").addEventListener("click", function () {
      TYPE_ORDER.forEach(function (t) {
        state.types[t] = true;
        var cb = document.querySelector('input[data-type="' + t + '"]');
        if (cb) cb.checked = true;
      });
      state.selected = null;
      exitFocus();
      if (state.chart) state.chart.setOption({ series: [{ zoom: 1, center: null }] });
    });
    $("btnRelayout").addEventListener("click", function () { render(false); });
    $("btnFit").addEventListener("click", function () {
      if (state.chart) state.chart.setOption({ series: [{ zoom: 1, center: null }] });
    });
    $("btnPng").addEventListener("click", function () {
      if (!state.chart) return;
      var url = state.chart.getDataURL({ pixelRatio: 2, backgroundColor: "#FFFFFF" });
      var a = document.createElement("a");
      a.href = url;
      a.download = (DATA.meta.domain || "ontology") + "-graph.png";
      a.click();
    });
    $("search").addEventListener("input", doSearch);    $("search").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && state.searchHits.length) {
        state.searchPos = (state.searchPos + 1) % state.searchHits.length;
        focusNode(state.searchHits[state.searchPos].id);
      }
      if (e.key === "Escape") { $("search").value = ""; doSearch(); }
    });
    $("pbPlay").addEventListener("click", togglePlay);
    $("pbNext").addEventListener("click", function () {
      state.playing = false; clearTimer(); $("pbPlay").textContent = "▶";
      jumpTo(state.playIndex + 1);
    });
    $("pbPrev").addEventListener("click", function () {
      state.playing = false; clearTimer(); $("pbPlay").textContent = "▶";
      jumpTo(state.playIndex - 1);
    });
    $("pbRange").addEventListener("input", function (e) {
      state.playing = false; clearTimer(); $("pbPlay").textContent = "▶";
      jumpTo(parseInt(e.target.value, 10));
    });
    $("pbSpeed").addEventListener("change", function (e) { state.speed = parseFloat(e.target.value) || 1; });
    $("labelMode").addEventListener("change", function (e) {
      state.labelMode = e.target.value;
      render(true);
    });
    window.addEventListener("hashchange", applyHash);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closePanel(); state.selected = null; render(true); }
    });
  }

  function applyHash() {
    var h = (location.hash || "").replace(/^#/, "");
    var i = h.indexOf("=");
    if (i < 0) return;
    var k = h.slice(0, i), v = decodeURIComponent(h.slice(i + 1));
    if (k === "scenario" && SCN[v]) { enterFocus("scenario", v); openPanel(PANELS[v]); }
    else if (k === "report" && RPT[v]) { enterFocus("report", v); openPanel(PANELS[v]); }
    else if (k === "node" && byId(v)) { focusNode(v); }
  }

  function boot() {
    DATA.nodes.forEach(function (n) { state.nodesIdx[n.id] = n; });
    buildSidebar();
    bindUI();
    if (typeof mermaid !== "undefined") {
      try {
        mermaid.initialize({
          startOnLoad: false, theme: "neutral", securityLevel: "loose",
          themeVariables: { fontFamily: "-apple-system,'Microsoft YaHei',sans-serif", fontSize: "12px" },
          flowchart: { curve: "linear", htmlLabels: true, useMaxWidth: true,
                       nodeSpacing: 18, rankSpacing: 26, padding: 6 }
        });
      } catch (e) { /* 忽略初始化异常，渲染时再兜底 */ }
    }
    initChart();
    if (location.hash) setTimeout(applyHash, 260);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())



