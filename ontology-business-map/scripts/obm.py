#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""obm.py — ontology-business-map 工具链。

核心约定（不要打破）：
    * 模型只产出 **严格 JSON 分片**（_parts/*.json）；
    * YAML **只由本脚本 `toyaml` 生成**，模型与人都不要手写 YAML；
    * 校验发生在 JSON 阶段：`check`（片级）→ `merge`（合并级）→ `render`（全量，复用 viewer）。

典型用法：
    python obm.py init   <workdir> --industry 手机制造 --domain 产品研发
    python obm.py status <workdir>
    python obm.py check  <workdir> [--part m1-01] [--json]
    python obm.py merge  <workdir>
    python obm.py quality<workdir>
    python obm.py toyaml <workdir>
    python obm.py render <workdir>
    python obm.py selftest <workdir>
    python obm.py fromyaml <workdir>      # 已有 YAML 反向生成分片（回归基线用）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(SKILL_ROOT, "schema", "closed-fields.json")
VIEWER_DIR = os.path.join(SKILL_ROOT, "scripts", "viewer")
ASSETS_DIR = os.path.join(SKILL_ROOT, "assets")

SPEC = json.load(open(SPEC_PATH, encoding="utf-8"))
MODELS = SPEC["models"]
BUDGETS = SPEC["budgets"]
PREFIXES = SPEC["prefixes"]
KIND_BY_LETTER = {"m1": "OBJECT", "m2": "BEHAVIOR", "m3": "RULE", "me": "EVENT", "m4": "SCENARIO", "m7": "REPORT"}
LETTER_BY_KIND = {v: k for k, v in KIND_BY_LETTER.items()}

ENUM_FIELDS = {
    "behaviorType": "behaviorType", "triggerType": "triggerType", "ruleType": "ruleType",
    "executionMode": "executionMode", "producerType": "producerType", "subscriberType": "subscriberType",
    "ordering": "ordering", "objectType": "objectType", "aggregateFunction": "aggregateFunction",
    "dataType": "dataType", "stepType": "stepType", "associationType": "associationType",
    "cardinality": "cardinality", "operator": "operator", "enforcedAt": "enforcedAt",
    "aggregateType": "aggregateType",
}

REF_FIELDS = {
    "behaviors": {"ownerEntity": "one", "appliedRules": "many", "producedEvents": "many",
                  "queryReportRef": "one"},
    "aggregate_associations": {"sourceAggregate": "one", "targetAggregate": "one"},
    "rules": {"reusedBy": "many"},
    "events": {"producerBehaviorRef": "one", "producerEntityRef": "one"},
    "event_scenarios": {"sourceObjectRef": "one", "targetObjectRefs": "many", "triggerEventRef": "one"},
    "query_reports": {"behaviorRef": "one"},
}
SUB_REF_FIELDS = {
    ("event_scenarios", "steps"): {"behaviorRef": "one", "ruleRef": "one", "eventRef": "one"},
    ("events", "subscribers"): {"subscriberBehaviorRef": "one", "subscriberRuleRef": "one"},
    ("query_reports", "sourceObjects"): {"objectRef": "one"},
}
FIELD_REF_SUBS = {("rules", "inputParams"): "sourceField", ("events", "payload"): "sourceField"}


# ------------------------------------------------------------------ 基础设施

class Issues:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg, where="", ptr="", refs=None):
        self.items.append({"level": level, "code": code, "message": msg,
                           "where": where, "ptr": ptr, "refs": refs or []})

    def error(self, *a, **kw):
        self.add("error", *a, **kw)

    def warn(self, *a, **kw):
        self.add("warn", *a, **kw)

    def info(self, *a, **kw):
        self.add("info", *a, **kw)

    @property
    def errors(self):
        return [i for i in self.items if i["level"] == "error"]

    @property
    def warns(self):
        return [i for i in self.items if i["level"] == "warn"]

    def dump(self, limit=40):
        for i in self.items[:limit]:
            tag = {"error": "E", "warn": "W", "info": "i"}[i["level"]]
            print("  [%s][%s] %s%s" % (tag, i["code"], i["message"],
                                       ("  <- " + i["where"]) if i["where"] else ""))
        if len(self.items) > limit:
            print("  ... 其余 %d 条见报告" % (len(self.items) - limit))


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def die(msg):
    sys.stderr.write("错误：%s\n" % msg)
    sys.exit(2)


def norm(workdir):
    workdir = os.path.abspath(workdir)
    if not os.path.isdir(workdir):
        die("工作目录不存在 -> %s" % workdir)
    return workdir


def config_of(workdir):
    cfg = load_json(os.path.join(workdir, "config.json"))
    if not cfg:
        die("缺少 config.json，请先执行：obm.py init <workdir>")
    return cfg


def viewer():
    if VIEWER_DIR not in sys.path:
        sys.path.insert(0, VIEWER_DIR)
    import ontology_viewer  # noqa: E402
    return ontology_viewer


def enum_of(field):
    """枚举来源优先级：schema 的 enums（规范权威）> viewer 的 ENUMS（渲染器兜底）。"""
    spec = SPEC.get("enums") or {}
    if field in spec:
        return set(spec[field])
    key = ENUM_FIELDS.get(field)
    if not key:
        return None
    return viewer().ENUMS.get(key)


def type_of_attr():
    """属性允许的类型全集（规范权威）。"""
    return set((SPEC.get("enums") or {}).get("attributeType")
               or ["String", "Integer", "Decimal", "Date", "DateTime", "Boolean",
                   "Enum", "Money", "Object", "Void", "ValueObject",
                   "AggregateRootRef", "DictionaryRef"])


# ------------------------------------------------------------------ 分片装配

def load_parts(workdir, only=None):
    files = sorted(glob.glob(os.path.join(workdir, "_parts", "*.json")))
    out = []
    for f in files:
        name = os.path.basename(f)
        if name.startswith("_"):
            continue
        if only and not name.startswith(only):
            continue
        data = load_json(f)
        if not isinstance(data, dict):
            continue
        out.append((name, data))
    return out


def part_kind(part):
    kind = part.get("model") or KIND_BY_LETTER.get(str(part.get("part", ""))[:2])
    return kind if kind in MODELS else None


def part_collections(part):
    cols = part.get("collections")
    if isinstance(cols, dict):
        return cols
    # 兼容手写的扁平写法：{"aggregates": [...]}
    return {k: v for k, v in part.items() if isinstance(v, list)}


def skeleton_ids(workdir):
    sk = load_json(os.path.join(workdir, "skeleton.json"), {})
    return {n["id"] for n in (sk.get("nodes") or []) if n.get("id")}


def collect_ids(workdir, only=None):
    """运行中已知的全部 ID：骨架 + 已落盘分片。"""
    ids = set(skeleton_ids(workdir))
    for _name, part in load_parts(workdir, only):
        for col, items in part_collections(part).items():
            for it in items or []:
                nid = it.get("id") or it.get("eventId")
                if nid:
                    ids.add(nid)
    return ids


def node_id(item):
    return item.get("eventId") or item.get("id") or ""


def strip_sidecar(item):
    """把 _cap / _evidence 等旁挂键摘出来（不进入 YAML）。"""
    side = {k: v for k, v in item.items() if k.startswith("_")}
    return side


def _all_attrs(agg):
    """聚合下的全部属性：聚合本体 + 实体 + 值对象（字典引用三类都要算）。"""
    out = list(agg.get("attributes") or [])
    for key in ("entities", "valueObjects"):
        for sub in (agg.get(key) or []):
            out.extend(sub.get("attributes") or [])
    return out


# ------------------------------------------------------------------ 校验：片级

def check_collection(kind, col, spec, items, rep, where_prefix, known_ids, idx_fields):
    """校验一个集合下的所有节点：字段封闭性 / ID / 枚举 / 引用 / 字段级。"""
    seen = {}
    for n, item in enumerate(items or []):
        if not isinstance(item, dict):
            rep.error("ITEM_SHAPE", "%s[%d] 不是对象" % (col, n), where_prefix,
                      "collections/%s/%d" % (col, n))
            continue
        nid = node_id(item) or "?"
        where = "%s#%s[%d]" % (where_prefix, col, n)
        ptr = "collections/%s/%d" % (col, n)

        for k in spec.get("required", []):
            if k not in item or item[k] in (None, "", [], {}):
                rep.error("FIELD_REQUIRED", "%s 缺少必填字段 %s" % (nid, k), where, ptr, [nid])
        allowed = set(spec.get("allowed") or [])
        for k in item:
            if k.startswith("_"):
                continue
            if allowed and k not in allowed:
                rep.error("FIELD_UNKNOWN", "%s 出现封闭字段集之外的键 %s" % (nid, k), where, ptr, [nid])

        pre = spec.get("id_prefix")
        if pre:
            # 允许字符串或字符串数组（如报表 QR- 与 RPT- 并存）
            pres = [pre] if isinstance(pre, str) else list(pre)
            if nid and not any(nid.startswith(p) for p in pres):
                rep.error("ID_PREFIX", "%s 的 ID 前缀应为 %s" % (nid, " 或 ".join(pres)),
                          where, ptr, [nid])
        if nid in seen:
            rep.error("ID_DUP", "%s 在本片内重复（第 %d 与第 %d 项）" % (nid, seen[nid] + 1, n + 1),
                      where, ptr, [nid])
        seen.setdefault(nid, n)

        for field, ekey in ENUM_FIELDS.items():
            if field in item and item[field] not in (None, ""):
                allowed_vals = enum_of(field)
                if allowed_vals and item[field] not in allowed_vals:
                    rep.error("ENUM", "%s 的 %s=%r 不在枚举内（允许：%s）"
                              % (nid, field, item[field], "/".join(sorted(allowed_vals))),
                              where, ptr, [nid])

        for field, mode in (REF_FIELDS.get(col) or {}).items():
            val = item.get(field)
            values = val if isinstance(val, list) else ([val] if val else [])
            for v in values:
                if v and v not in known_ids:
                    rep.error("REF_UNKNOWN", "%s 的 %s=%s 不在骨架/已生成分片中（骨架先行，先登记再引用）"
                              % (nid, field, v), where, ptr, [nid, v])

        for sub_name, sub_spec in (spec.get("sub") or {}).items():
            rows = item.get(sub_name)
            if not isinstance(rows, list):
                continue
            for m, row in enumerate(rows):
                if not isinstance(row, dict):
                    rep.error("ITEM_SHAPE", "%s 的 %s[%d] 不是对象" % (nid, sub_name, m), where,
                              "%s/%s/%d" % (ptr, sub_name, m), [nid])
                    continue
                for k in sub_spec.get("required", []):
                    if k not in row or row[k] in (None, ""):
                        rep.error("SUB_REQUIRED", "%s 的 %s[%d] 缺少 %s" % (nid, sub_name, m, k),
                                  where, "%s/%s/%d" % (ptr, sub_name, m), [nid])
                sub_allowed = set(sub_spec.get("allowed") or [])
                for k in row:
                    if k.startswith("_"):
                        continue
                    if sub_allowed and k not in sub_allowed:
                        rep.error("SUB_UNKNOWN", "%s 的 %s[%d] 出现未知键 %s" % (nid, sub_name, m, k),
                                  where, "%s/%s/%d" % (ptr, sub_name, m), [nid])
                for field, mode in (SUB_REF_FIELDS.get((col, sub_name)) or {}).items():
                    v = row.get(field)
                    if v and v not in known_ids:
                        rep.error("REF_UNKNOWN", "%s 的 %s[%d].%s=%s 指向不存在的节点"
                                  % (nid, sub_name, m, field, v), where,
                                  "%s/%s/%d" % (ptr, sub_name, m), [nid, v])
                fkey = FIELD_REF_SUBS.get((col, sub_name))
                if fkey:
                    v = row.get(fkey)
                    if v and "." in str(v):
                        head, _, tail = str(v).partition(".")
                        if head != "SYSTEM":
                            names = idx_fields.get(head)
                            if names is None:
                                rep.error("FIELD_OBJ", "%s 的 %s[%d].%s=%s 指向不存在的对象"
                                          % (nid, sub_name, m, fkey, v), where, ptr, [nid, head])
                            elif tail not in names:
                                rep.error("FIELD_MISSING", "%s 的 %s[%d].%s=%s 的对象上无字段 %s"
                                          % (nid, sub_name, m, fkey, v, tail), where, ptr, [nid, head])
                for deep_name, deep_spec in (sub_spec.get("sub") or {}).items():
                    rows2 = row.get(deep_name)
                    for p, row2 in enumerate(rows2 or []):
                        if not isinstance(row2, dict):
                            continue
                        for k in deep_spec.get("required", []):
                            if k not in row2 or row2[k] in (None, ""):
                                rep.error("SUB_REQUIRED", "%s 的 %s[%d].%s[%d] 缺少 %s"
                                          % (nid, sub_name, m, deep_name, p, k), where, ptr, [nid])
                        d_allowed = set(deep_spec.get("allowed") or [])
                        for k in row2:
                            if k.startswith("_"):
                                continue
                            if d_allowed and k not in d_allowed:
                                rep.error("SUB_UNKNOWN", "%s 的 %s[%d].%s[%d] 出现未知键 %s"
                                          % (nid, sub_name, m, deep_name, p, k), where, ptr, [nid])


def check_attribute_types(aggs, rep):
    """属性级校验：type 必须在规范类型集内，且引用型/枚举型字段配套齐全。
    规范口径：type 取 String|Integer|Decimal|Date|DateTime|Boolean|Enum|Money|Object|Void|
    ValueObject|AggregateRootRef|DictionaryRef；外键必须用 AggregateRootRef，字典必须用 DictionaryRef。
    """
    allowed = type_of_attr()
    for a in aggs:
        aid = a.get("id") or ""
        holders = [("attributes", a.get("attributes") or [])]
        for key in ("entities", "valueObjects"):
            for sub in (a.get(key) or []):
                holders.append(("%s.%s.attributes" % (key, sub.get("name")), sub.get("attributes") or []))
        for where, rows in holders:
            for at in rows:
                if not isinstance(at, dict):
                    continue
                t = at.get("type")
                ptr = "%s#%s.%s" % (aid, where, at.get("name"))
                if t and t not in allowed:
                    rep.error("ATTR_TYPE", "%s 的属性 %s 类型 %r 不在规范类型集内（允许：%s）"
                              % (aid, at.get("name"), t, "/".join(sorted(allowed))), "", ptr, [aid])
                if t == "AggregateRootRef" and not at.get("targetAggregate"):
                    rep.error("ATTR_REF", "%s 的属性 %s 是 AggregateRootRef 但缺 targetAggregate"
                              % (aid, at.get("name")), "", ptr, [aid])
                if t == "DictionaryRef" and not at.get("dictionaryRef"):
                    rep.error("ATTR_DICT", "%s 的属性 %s 是 DictionaryRef 但缺 dictionaryRef"
                              % (aid, at.get("name")), "", ptr, [aid])
                if t == "Enum" and not (at.get("enumValues") or []):
                    rep.error("ATTR_ENUM", "%s 的属性 %s 是 Enum 但没有 enumValues"
                              % (aid, at.get("name")), "", ptr, [aid])
                if at.get("enumValues") and len(set(at["enumValues"])) != len(at["enumValues"]):
                    rep.error("ATTR_ENUM_DUP", "%s 的属性 %s 的 enumValues 有重复值"
                              % (aid, at.get("name")), "", ptr, [aid])


def cmd_check(args):
    workdir = norm(args.workdir)
    config_of(workdir)
    rep = Issues()
    parts = load_parts(workdir, args.part)
    if not parts:
        die("没有找到任何分片（_parts/*.json）")

    all_ids = collect_ids(workdir)
    aggs = []
    for _n, part in load_parts(workdir):
        for a in part_collections(part).get("aggregates") or []:
            aggs.append(a)
    idx_fields = viewer().field_index(aggs) if aggs else {}

    for name, part in parts:
        kind = part_kind(part)
        if not kind:
            rep.error("MODEL_UNKNOWN", "%s 的 model 字段无法识别" % name, name)
            continue
        spec = MODELS[kind]
        cols = part_collections(part)
        if not cols:
            rep.error("PART_EMPTY", "%s 没有任何集合内容" % name, name)
        for col, items in cols.items():
            if col not in spec["collections"]:
                rep.error("COLLECTION_UNKNOWN", "%s 出现 %s 模型不允许的集合 %s" % (name, kind, col), name)
                continue
            check_collection(kind, col, spec["collections"][col], items, rep, name, all_ids, idx_fields)

    if aggs:
        check_attribute_types(aggs, rep)

    total_items = sum(len(v or []) for _n, p in parts for v in part_collections(p).values())
    print("片级校验：%d 个分片 / %d 个节点 -> error %d / warn %d"
          % (len(parts), total_items, len(rep.errors), len(rep.warns)))
    rep.dump()
    if args.json:
        save_json(os.path.join(workdir, "check-report.json"),
                  {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "parts": [n for n, _ in parts], "summary": {
                       "errors": len(rep.errors), "warnings": len(rep.warns)}, "items": rep.items})
        print("  报告：check-report.json")
    if rep.errors:
        sys.exit(1)


# ------------------------------------------------------------------ 合并 + 双向同步

def sync_bidirectional(agg, rep):
    beh = {b["id"]: b for b in agg.get("behaviors") or []}
    for r in agg.get("rules") or []:
        reused = r.setdefault("reusedBy", [])
        for bid in list(reused):
            b = beh.get(bid)
            if not b:
                rep.error("REF_UNKNOWN", "规则 %s 的 reusedBy 引用了不存在的行为 %s" % (r["id"], bid),
                          "merge", refs=[r["id"], bid])
                continue
            applied = b.setdefault("appliedRules", [])
            if r["id"] not in applied:
                applied.append(r["id"])
    for e in agg.get("events") or []:
        bid = e.get("producerBehaviorRef")
        b = beh.get(bid)
        if b:
            produced = b.setdefault("producedEvents", [])
            if node_id(e) not in produced:
                produced.append(node_id(e))
    for rp in agg.get("query_reports") or []:
        b = beh.get(rp.get("behaviorRef"))
        if b:
            b["queryReportRef"] = rp["id"]
    # 反向补齐：行为侧写了、规则侧漏写
    rule_by_id = {r["id"]: r for r in agg.get("rules") or []}
    for b in agg.get("behaviors") or []:
        for rid in b.get("appliedRules") or []:
            r = rule_by_id.get(rid)
            if r is not None and b["id"] not in (r.get("reusedBy") or []):
                r.setdefault("reusedBy", []).append(b["id"])
                rep.info("BIDIRECT_FIX", "规则 %s 的 reusedBy 补上行为 %s" % (rid, b["id"]), "merge")
        for eid in b.get("producedEvents") or []:
            ev = next((x for x in agg.get("events") or [] if node_id(x) == eid), None)
            if ev is not None and ev.get("producerBehaviorRef") != b["id"]:
                rep.warn("EVENT_PRODUCER", "事件 %s 的 producer 与行为 %s 的 producedEvents 不一致"
                         % (eid, b["id"]), "merge")


def cmd_merge(args):
    workdir = norm(args.workdir)
    cfg = config_of(workdir)
    rep = Issues()
    merged = {kind: {} for kind in MODELS}
    evidence = {}
    owner = {}

    for name, part in load_parts(workdir):
        kind = part_kind(part)
        if not kind:
            rep.error("MODEL_UNKNOWN", "%s 的 model 无法识别" % name, name)
            continue
        bucket = merged[kind]
        for col, items in part_collections(part).items():
            if col not in MODELS[kind]["collections"]:
                rep.error("COLLECTION_UNKNOWN", "%s 的集合 %s 不属于 %s" % (name, col, kind), name)
                continue
            target = bucket.setdefault(col, [])
            for item in items or []:
                nid = node_id(item)
                if nid in owner:
                    rep.error("ID_DUP", "%s 在 %s 与 %s 中重复定义" % (nid, owner[nid], name),
                              name, refs=[nid])
                    continue
                owner[nid] = "%s#%s" % (name, col)
                side = strip_sidecar(item)
                if side:
                    evidence[nid] = side
                target.append(item)

    bundle = {}
    for kind in MODELS:
        spec = MODELS[kind]
        data = {"model_type": kind, "version": SPEC["versions"][kind], "domain": cfg["domain"]}
        for col in spec["collections"]:
            if merged[kind].get(col):
                data[col] = merged[kind][col]
        # 空集合也要落，保证下游按固定结构读取
        for col in spec["root_required"]:
            if col not in data:
                data[col] = []
        missing = [c for c in spec["root_required"] if c not in data]
        if missing:
            rep.error("MODEL_INCOMPLETE", "%s 模型缺少根字段 %s" % (kind, missing), "merge")
        bundle[kind] = data

    sync_bidirectional(bundle, rep)

    # 全量引用复核：合并后所有引用必须能落地
    known = set(owner)
    for kind, data in bundle.items():
        for col, items in data.items():
            if not isinstance(items, list):
                continue
            spec = MODELS[kind]["collections"].get(col, {})
            for item in items:
                nid = node_id(item)
                for field in (REF_FIELDS.get(col) or {}):
                    val = item.get(field)
                    for v in (val if isinstance(val, list) else [val] if val else []):
                        if v and v not in known:
                            rep.error("REF_UNRESOLVED", "%s 的 %s=%s 合并后仍不存在" % (nid, field, v),
                                      "merge", refs=[nid, v])
                for sub_name, sub_spec in (spec.get("sub") or {}).items():
                    for row in item.get(sub_name) or []:
                        if not isinstance(row, dict):
                            continue
                        for field in (SUB_REF_FIELDS.get((col, sub_name)) or {}):
                            v = row.get(field)
                            if v and v not in known:
                                rep.error("REF_UNRESOLVED", "%s 的 %s.%s=%s 合并后仍不存在"
                                          % (nid, sub_name, field, v), "merge", refs=[nid, v])

    out_dir = os.path.join(workdir, "merged")
    for kind, data in bundle.items():
        save_json(os.path.join(out_dir, "%s.json" % LETTER_BY_KIND[kind]), data)
    save_json(os.path.join(out_dir, "_evidence.json"),
              {"note": "节点级旁挂信息（能力归属/文档证据），不进入 YAML", "nodes": evidence})
    save_json(os.path.join(out_dir, "merge-report.json"),
              {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "summary": {"errors": len(rep.errors), "warnings": len(rep.warns),
                           "nodes": len(owner)},
               "items": rep.items})

    counts = ", ".join("%s %d" % (c, len(v)) for k, d in bundle.items()
                       for c, v in d.items() if isinstance(v, list) and v)
    print("合并：%d 个节点（error %d / warn %d）" % (len(owner), len(rep.errors), len(rep.warns)))
    print("  " + counts)
    rep.dump()
    if rep.errors and args.strict:
        sys.exit(1)


# ------------------------------------------------------------------ 质量体检

CRUD_RE = re.compile(r"^(create|update|delete|remove|get|list|query|save|edit)[A-Z_-]", re.I)


def cmd_quality(args):
    workdir = norm(args.workdir)
    cfg = config_of(workdir)
    rep = Issues()
    merged_dir = os.path.join(workdir, "merged")
    bundle = {}
    for letter, kind in KIND_BY_LETTER.items():
        d = load_json(os.path.join(merged_dir, "%s.json" % letter))
        if d:
            bundle[kind] = d
    if not bundle:
        die("没有 merged/*.json，请先执行：obm.py merge <workdir>")

    aggs = bundle.get("OBJECT", {}).get("aggregates") or []
    assocs = bundle.get("OBJECT", {}).get("aggregate_associations") or []
    dicts = bundle.get("OBJECT", {}).get("data_dictionaries") or []
    behs = bundle.get("BEHAVIOR", {}).get("behaviors") or []
    rules = bundle.get("RULE", {}).get("rules") or []
    events = bundle.get("EVENT", {}).get("events") or []
    scns = bundle.get("SCENARIO", {}).get("event_scenarios") or []
    rpts = bundle.get("REPORT", {}).get("query_reports") or []
    counts = {"aggregates": len(aggs), "aggregate_associations": len(assocs), "data_dictionaries": len(dicts),
              "behaviors": len(behs), "rules": len(rules), "events": len(events),
              "event_scenarios": len(scns), "query_reports": len(rpts)}

    # 1) 预算区间
    for key, (lo, hi, hard) in BUDGETS.items():
        n = counts.get(key, 0)
        if n > hard:
            rep.error("BUDGET_HARD", "%s=%d 超过硬上限 %d" % (key, n, hard))
        elif n > hi:
            rep.warn("BUDGET", "%s=%d 高于建议上限 %d" % (key, n, hi))
        elif n < lo:
            if key in ("data_dictionaries",):
                continue
            rep.warn("BUDGET_LOW", "%s=%d 低于建议下限 %d（模型可能过度简化）" % (key, n, lo))

    # 2) 反模式
    crud = [b for b in behs if CRUD_RE.match(str(b.get("name") or ""))]
    if behs and len(crud) / len(behs) > 0.4:
        rep.warn("AP_CRUD", "%d/%d 个行为是 CRUD 式命名，业务动作抽象不足" % (len(crud), len(behs)))
    empty_cmd = [b for b in behs if b.get("behaviorType") == "COMMAND"
                 and not b.get("appliedRules") and not b.get("producedEvents")]
    if empty_cmd:
        rep.info("AP_THIN", "%d 个 COMMAND 行为既无规则也无事件（纯搬运）: %s"
                 % (len(empty_cmd), ", ".join(b["id"] for b in empty_cmd[:5])))
    agg_map = {a["id"]: a.get("alias") or a.get("name") for a in aggs}
    for s in scns:
        steps = s.get("steps") or []
        objs = {s.get("sourceObjectRef")} | set(s.get("targetObjectRefs") or [])
        for st in steps:
            for ref in (st.get("behaviorRef"), st.get("ruleRef")):
                for b in behs:
                    if b["id"] == ref:
                        objs.add(b.get("ownerEntity"))
        if len(steps) < 5:
            rep.warn("AP_SCN_SHORT", "场景 %s 只有 %d 步，端到端感不足" % (s["id"], len(steps)))
        if len([o for o in objs if o]) < 3:
            rep.warn("AP_SCN_NARROW", "场景 %s 只涉及 %d 个对象，不构成跨对象流转"
                     % (s["id"], len([o for o in objs if o])))
    for rp in rpts:
        srcs = rp.get("sourceObjects") or []
        aggs_used = [c for c in (rp.get("resultColumns") or []) if (c.get("aggregateFunction") or "NONE") != "NONE"]
        if len(srcs) <= 1 and not aggs_used:
            rep.warn("AP_RPT_TRIVIAL", "报表 %s 单源对象且无聚合，近乎原始清单" % rp["id"])

    # 3) 覆盖率
    beh_owner = {b.get("ownerEntity") for b in behs}
    for a in aggs:
        if a["id"] not in beh_owner:
            rep.warn("COV_OBJ_NO_BEHAVIOR", "对象 %s（%s）没有任何行为" % (a["id"], agg_map.get(a["id"], "")))
    used_rules = {r for r in [x for b in behs for x in (b.get("appliedRules") or [])]}
    for r in rules:
        if r["id"] not in used_rules:
            rep.warn("COV_RULE_UNUSED", "规则 %s 未被任何行为引用" % r["id"])
    subbed = {s.get("subscriberBehaviorRef") or s.get("subscriberRuleRef")
              for e in events for s in (e.get("subscribers") or [])}
    for e in events:
        if not (e.get("subscribers") or []):
            rep.warn("COV_EVENT_NO_SUB", "事件 %s 没有订阅者" % node_id(e))
    beh_ids = {b["id"] for b in behs}
    for rp in rpts:
        if rp.get("behaviorRef") not in beh_ids:
            rep.warn("COV_RPT_NO_BEHAVIOR", "报表 %s 未绑定行为" % rp["id"])
    used_dicts = {at.get("dictionaryRef") for a in aggs for at in _all_attrs(a)
                  if at.get("dictionaryRef")}
    for d in dicts:
        if d["id"] not in used_dicts:
            rep.warn("COV_DICT_UNUSED", "字典 %s 未被任何属性引用" % d["id"])

    # 4) 证据/能力覆盖（P3）
    ev = load_json(os.path.join(merged_dir, "_evidence.json"), {}).get("nodes") or {}
    sk = load_json(os.path.join(workdir, "skeleton.json"), {})
    caps = sk.get("capabilities") or []
    covered = {}
    for nid, meta in ev.items():
        for c in ([meta.get("_cap")] if isinstance(meta.get("_cap"), str) else meta.get("_cap") or []):
            covered.setdefault(c, []).append(nid)
    uncovered = [c for c in caps if c.get("id") not in covered]
    for c in uncovered:
        rep.warn("COV_CAP", "能力 %s（%s）没有绑定任何模型节点" % (c.get("id"), c.get("name")))
    ev_nodes = [nid for nid, m in ev.items() if m.get("_evidence")]
    if ev:
        rep.info("EVIDENCE", "%d/%d 个节点带文档证据锚点" % (len(ev_nodes), len(ev)))

    # 5) 评分
    n_err, n_warn = len(rep.errors), len(rep.warns)
    score = max(0, min(100, 100 - n_err * 12 - n_warn * 2))
    summary = {"score": score, "errors": n_err, "warnings": n_warn, "counts": counts,
               "evidenceNodes": len(ev_nodes), "capabilities": {"total": len(caps), "covered": len(covered)}}
    save_json(os.path.join(workdir, "quality-report.json"),
              {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "summary": summary, "items": rep.items})

    print("质量体检：得分 %d / 100（error %d / warn %d）" % (score, n_err, n_warn))
    print("  规模：" + " / ".join("%s %d" % (k, v) for k, v in counts.items() if v))
    if caps:
        print("  能力覆盖：%d/%d" % (len(covered), len(caps)))
    rep.dump()
    if args.strict and n_err:
        sys.exit(1)


# ------------------------------------------------------------------ YAML / 渲染

def order_dict(d, allowed):
    if not allowed:
        return {k: v for k, v in d.items() if not k.startswith("_")}
    out = {}
    for k in allowed:
        if k in d and not k.startswith("_"):
            out[k] = d[k]
    return out


def cmd_toyaml(args):
    import yaml
    workdir = norm(args.workdir)
    merged_dir = os.path.join(workdir, "merged")
    out_dir = os.path.join(workdir, "yaml")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for letter, kind in KIND_BY_LETTER.items():
        data = load_json(os.path.join(merged_dir, "%s.json" % letter))
        if not data:
            continue
        spec = MODELS[kind]
        out = {"model_type": data["model_type"], "version": data["version"], "domain": data["domain"]}
        for col, colspec in spec["collections"].items():
            rows = data.get(col) or []
            ordered = []
            for item in rows:
                node = order_dict(item, colspec.get("allowed"))
                for sub_name, sub_spec in (colspec.get("sub") or {}).items():
                    if sub_name in node and isinstance(node[sub_name], list):
                        new_rows = []
                        for row in node[sub_name]:
                            if not isinstance(row, dict):
                                new_rows.append(row)
                                continue
                            clean = order_dict(row, sub_spec.get("allowed"))
                            for deep, deep_spec in (sub_spec.get("sub") or {}).items():
                                if deep in clean and isinstance(clean[deep], list):
                                    clean[deep] = [order_dict(r2, deep_spec.get("allowed"))
                                                   if isinstance(r2, dict) else r2 for r2 in clean[deep]]
                            new_rows.append(clean)
                        node[sub_name] = new_rows
                ordered.append(node)
            if ordered or col in spec["root_required"]:
                out[col] = ordered
        path = os.path.join(out_dir, spec["file"])
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, width=200)
        written.append((spec["file"], sum(len(v) for v in out.values() if isinstance(v, list))))
    print("YAML 落盘（唯一序列化出口）：")
    for f, n in written:
        print("  yaml/%-28s %d 条" % (f, n))


def cmd_fromyaml(args):
    """把已有 YAML 反解成分片，用于回归对比或接手别人的模型。"""
    import yaml
    workdir = norm(args.workdir)
    yaml_dir = args.yaml_dir or os.path.join(workdir, "yaml")
    parts_dir = os.path.join(workdir, "_parts")
    os.makedirs(parts_dir, exist_ok=True)
    nodes = []
    written = []
    for f in sorted(glob.glob(os.path.join(yaml_dir, "*.yaml"))):
        data = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("model_type"):
            continue
        kind = data["model_type"]
        spec = MODELS.get(kind)
        if not spec:
            continue
        letter = LETTER_BY_KIND[kind]
        cols = {c: data.get(c) for c in spec["collections"] if data.get(c) is not None}
        save_json(os.path.join(parts_dir, "%s-00.json" % letter),
                  {"part": "%s-00" % letter, "model": kind, "source": os.path.basename(f),
                   "collections": cols})
        written.append(os.path.basename(f))
        for col, rows in cols.items():
            for row in rows:
                nid = node_id(row)
                if nid:
                    nodes.append({"id": nid, "kind": col})
    sk_path = os.path.join(workdir, "skeleton.json")
    if not os.path.exists(sk_path):
        save_json(sk_path, {"domain": bundle_domain(yaml_dir), "nodes": nodes, "capabilities": []})
    print("反解完成：%d 个 YAML -> _parts（%s）" % (len(written), ", ".join(written)))
    if nodes:
        print("  骨架：skeleton.json（%d 个 ID）" % len(nodes))


def bundle_domain(yaml_dir):
    import yaml
    for f in sorted(glob.glob(os.path.join(yaml_dir, "*.yaml"))):
        d = yaml.safe_load(open(f, encoding="utf-8"))
        if isinstance(d, dict) and d.get("domain"):
            return d["domain"]
    return "未命名业务域"


def cmd_render(args):
    workdir = norm(args.workdir)
    yaml_dir = args.yaml_dir or os.path.join(workdir, "yaml")
    viewer_py = os.path.join(VIEWER_DIR, "ontology_viewer.py")
    cmd = [sys.executable, viewer_py, yaml_dir, "-o", yaml_dir, "--vendor", ASSETS_DIR]
    if args.title:
        cmd += ["--title", args.title]
    if args.strict:
        cmd += ["--strict"]
    print("渲染：" + " ".join(os.path.basename(x) for x in cmd[:3]) + " ...")
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(rc)


def cmd_selftest(args):
    workdir = norm(args.workdir)
    yaml_dir = args.yaml_dir or os.path.join(workdir, "yaml")
    html = os.path.join(yaml_dir, "ontology-graph.html")
    model = os.path.join(yaml_dir, "ontology-model.json")
    rep = Issues()
    if not os.path.exists(html):
        die("找不到 %s，请先 render" % html)
    size = os.path.getsize(html)
    text = open(html, encoding="utf-8", errors="replace").read()
    if size < 500 * 1024:
        rep.error("SMOKE_SIZE", "HTML 只有 %.0f KB，依赖可能没内联" % (size / 1024.0))
    # (检测项, 错误码, 说明, 期望是否出现)；占位符必须"不出现"才算替换成功
    for token, code, msg, want in [
            ("typeof echarts", "SMOKE_ECHARTS", "缺少 echarts 内联", True),
            ("mermaid", "SMOKE_MERMAID", "缺少 mermaid 内联", True),
            ('id="chart"', "SMOKE_CHART", "缺少图表容器", True),
            ("__DATA__", "SMOKE_PLACEHOLDER", "数据占位符未替换", False)]:
        if (token in text) != want:
            rep.error(code, msg)
    if "未能加载" in text and "typeof echarts === \"undefined\"" not in text:
        rep.warn("SMOKE_TEXT", "HTML 中残留降级提示文案")
    data = load_json(model, {})
    nodes = len(data.get("nodes") or [])
    if nodes == 0:
        rep.error("SMOKE_NODES", "ontology-model.json 里没有节点")
    if not data.get("scenarios"):
        rep.warn("SMOKE_SCN", "没有场景数据，交互面板将为空")
    if not data.get("reports"):
        rep.warn("SMOKE_RPT", "没有报表数据，指标树将为空")
    print("自检：节点 %d / 边 %d / 场景 %d / 报表 %d / HTML %.1f MB -> error %d / warn %d"
          % (nodes, len(data.get("edges") or []), len(data.get("scenarios") or []),
             len(data.get("reports") or []), size / 1024.0 / 1024.0,
             len(rep.errors), len(rep.warns)))
    rep.dump()
    save_json(os.path.join(workdir, "selftest-report.json"),
              {"generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "summary": {"errors": len(rep.errors), "warnings": len(rep.warns),
                           "nodes": nodes, "bytes": size}, "items": rep.items})
    if rep.errors:
        sys.exit(1)


# ------------------------------------------------------------------ init / status

def cmd_init(args):
    workdir = os.path.abspath(args.workdir)
    for sub in ("_parts", "merged", "yaml"):
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    cfg_path = os.path.join(workdir, "config.json")
    if os.path.exists(cfg_path) and not args.force:
        print("已存在 config.json，保持不变（--force 覆盖）")
    else:
        save_json(cfg_path, {"industry": args.industry, "domain": args.domain,
                             "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "budgets": {k: {"min": v[0], "max": v[1], "hard": v[2]}
                                         for k, v in BUDGETS.items()}})
    sk_path = os.path.join(workdir, "skeleton.json")
    if not os.path.exists(sk_path):
        save_json(sk_path, {"domain": args.domain, "industry": args.industry,
                            "note": "先填 nodes（全局 ID 清单）再写分片", "capabilities": [], "nodes": []})
    print("工作目录就绪：%s" % workdir)
    print("  下一步：填 skeleton.json（ID + 中文名 + 职责），再按 prompts/ 产出 _parts/*.json")


def cmd_status(args):
    workdir = norm(args.workdir)
    cfg = config_of(workdir)
    sk = load_json(os.path.join(workdir, "skeleton.json"), {})
    parts = load_parts(workdir)
    per_model = {}
    for name, part in parts:
        kind = part_kind(part) or "?"
        n = sum(len(v or []) for v in part_collections(part).values())
        per_model.setdefault(kind, []).append((name, n))
    print("业务域：%s（%s）" % (cfg.get("domain"), cfg.get("industry")))
    print("骨架：%d 个 ID / %d 项能力" % (len(sk.get("nodes") or []), len(sk.get("capabilities") or [])))
    total = 0
    for kind in MODELS:
        rows = per_model.get(kind) or []
        n = sum(x[1] for x in rows)
        total += n
        budget = BUDGETS.get(_collection_of(kind), [0, 0, 0])
        print("  %-9s %2d 片 %4d 条   建议区间 %d-%d" %
              (kind, len(rows), n, budget[0], budget[1]))
    print("合计节点：%d" % total)
    for stage, path in (("check", "check-report.json"), ("merge", "merged/merge-report.json"),
                        ("quality", "quality-report.json"), ("yaml", "yaml/m1-object-model.yaml"),
                        ("html", "yaml/ontology-graph.html")):
        print("  %-8s %s" % (stage, "✓" if os.path.exists(os.path.join(workdir, path)) else "—"))


def _collection_of(kind):
    return {"OBJECT": "aggregates", "BEHAVIOR": "behaviors", "RULE": "rules",
            "EVENT": "events", "SCENARIO": "event_scenarios", "REPORT": "query_reports"}[kind]


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(prog="obm.py", description="本体建模工具链（骨架→分片→校验→YAML→HTML）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化工作目录")
    p.add_argument("workdir")
    p.add_argument("--industry", default="未指定行业")
    p.add_argument("--domain", default="未命名业务域")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="查看进度")
    p.add_argument("workdir")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", help="片级校验（字段封闭性/ID/枚举/引用/字段级）")
    p.add_argument("workdir")
    p.add_argument("--part", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("merge", help="合并分片 + 双向引用同步")
    p.add_argument("workdir")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("quality", help="质量体检（预算/反模式/覆盖/证据）")
    p.add_argument("workdir")
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("toyaml", help="JSON -> YAML（唯一序列化出口）")
    p.add_argument("workdir")
    p.set_defaults(func=cmd_toyaml)

    p = sub.add_parser("render", help="生成可视化 HTML")
    p.add_argument("workdir")
    p.add_argument("--yaml-dir", dest="yaml_dir", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--strict", action="store_true")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("selftest", help="HTML 冒烟验收")
    p.add_argument("workdir")
    p.add_argument("--yaml-dir", dest="yaml_dir", default=None)
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("fromyaml", help="YAML 反解为分片（接手/回归用）")
    p.add_argument("workdir")
    p.add_argument("--yaml-dir", dest="yaml_dir", default=None)
    p.set_defaults(func=cmd_fromyaml)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
