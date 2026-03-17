#!/usr/bin/env python3
"""
bpmn2odrl8.py  —  BPMN XML → ODRL JSON-LD
==========================================
v8 change: Intermediate catch events are now emitted as odrl:Prohibition rules.

Theoretical grounding (Victor's insight):
  An intermediate catch event in BPMN means the participant is PROHIBITED from
  proceeding past that point until the triggering condition holds.
  This is not a permission (the participant has no choice) and not a duty
  (waiting is not an action the participant performs) — it is a conditional
  prohibition on the downstream task, lifted when the message/event arrives.

  Formally:
    IntermediateCatchEvent(E) receiving message M  →
      odrl:Prohibition on the task immediately downstream of E,
      with constraint  bpmn:eventReceived = <event name>,
      for the party whose lane contains E.

  This captures the "wall" semantics: you cannot cross this point until
  the world satisfies the condition.  The prohibition is lifted (i.e. the
  constraint is satisfied) when the named event fires.

v8 also incorporates all v6 fixes:
  - ConstrainedDuty distinction (mandatory within branch)
  - Correct notify duties from message flows
  - No spurious waitFor constraints on non-receiver tasks
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# 1.  BPMN XML PARSER
# ─────────────────────────────────────────────────────────────────────────────

BPMN_NS  = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_NS      = {"bpmn": BPMN_NS}

TASK_TAGS = {
    f"{{{BPMN_NS}}}task",
    f"{{{BPMN_NS}}}serviceTask",
    f"{{{BPMN_NS}}}userTask",
    f"{{{BPMN_NS}}}sendTask",
    f"{{{BPMN_NS}}}receiveTask",
    f"{{{BPMN_NS}}}scriptTask",
    f"{{{BPMN_NS}}}businessRuleTask",
    f"{{{BPMN_NS}}}manualTask",
    f"{{{BPMN_NS}}}callActivity",
    f"{{{BPMN_NS}}}subProcess",
}

GATEWAY_TAGS = {
    f"{{{BPMN_NS}}}exclusiveGateway":  "XOR",
    f"{{{BPMN_NS}}}parallelGateway":   "AND",
    f"{{{BPMN_NS}}}inclusiveGateway":  "OR",
    f"{{{BPMN_NS}}}eventBasedGateway": "EVENT",
    f"{{{BPMN_NS}}}complexGateway":    "COMPLEX",
}

EVENT_TAGS = {
    f"{{{BPMN_NS}}}startEvent",
    f"{{{BPMN_NS}}}endEvent",
    f"{{{BPMN_NS}}}intermediateCatchEvent",
    f"{{{BPMN_NS}}}intermediateThrowEvent",
    f"{{{BPMN_NS}}}boundaryEvent",
}


class BPMNGraph:
    def __init__(self):
        self.nodes:          Dict[str, dict]        = {}
        self.succ:           Dict[str, List[str]]   = defaultdict(list)
        self.pred:           Dict[str, List[str]]   = defaultdict(list)
        self.seq_edges:      List[Tuple]            = []
        self.msg_flows:      List[Tuple]            = []
        self.participants:   Dict[str, dict]        = {}
        self.node_to_part:   Dict[str, str]         = {}
        self.process_to_part:Dict[str, str]         = {}
        self.start_nodes:    List[str]              = []
        self.end_nodes:      List[str]              = []
        # NEW in v8: set of intermediate catch event node IDs
        self.catch_events:   Set[str]               = set()

    def _add_node(self, nid, label, kind, gateway_type, process_id):
        self.nodes[nid] = {
            "label":        label,
            "kind":         kind,
            "gateway_type": gateway_type,
            "process_id":   process_id,
            "participant":  self.process_to_part.get(process_id, "unknown"),
        }

    def _add_seq(self, src, tgt, condition=""):
        self.succ[src].append(tgt)
        self.pred[tgt].append(src)
        self.seq_edges.append((src, tgt, condition))

    @classmethod
    def from_xml(cls, xml_path: str, verbose: bool = False) -> "BPMNGraph":
        g = cls()
        tree = ET.parse(xml_path)
        root = tree.getroot()

        collab = root.find("bpmn:collaboration", _NS)
        if collab is not None:
            for p in collab.findall("bpmn:participant", _NS):
                pid    = p.get("id", "")
                pname  = p.get("name", pid)
                procid = p.get("processRef", "")
                g.participants[pid]        = {"name": pname, "process_id": procid}
                g.process_to_part[procid]  = pid

        for process_el in root.findall("bpmn:process", _NS):
            proc_id = process_el.get("id", "")

            for el in process_el.iter():
                tag = el.tag
                nid = el.get("id", "")
                if not nid:
                    continue
                lbl = el.get("name", nid).strip() or nid

                if tag in TASK_TAGS:
                    g._add_node(nid, lbl, "task", None, proc_id)
                elif tag in GATEWAY_TAGS:
                    gtype = GATEWAY_TAGS[tag]
                    g._add_node(nid, lbl or f"{gtype} gateway", "gateway", gtype, proc_id)
                elif tag == f"{{{BPMN_NS}}}startEvent":
                    g._add_node(nid, lbl, "startEvent", None, proc_id)
                    g.start_nodes.append(nid)
                elif tag == f"{{{BPMN_NS}}}endEvent":
                    g._add_node(nid, lbl, "endEvent", None, proc_id)
                    g.end_nodes.append(nid)
                elif tag == f"{{{BPMN_NS}}}intermediateCatchEvent":
                    g._add_node(nid, lbl, "event", None, proc_id)
                    g.catch_events.add(nid)   # v8: track these explicitly
                elif tag in EVENT_TAGS:
                    g._add_node(nid, lbl, "event", None, proc_id)

            for sf in process_el.findall("bpmn:sequenceFlow", _NS):
                src  = sf.get("sourceRef", "")
                tgt  = sf.get("targetRef", "")
                cond = sf.get("name", "")
                if src and tgt:
                    g._add_seq(src, tgt, cond)

        if collab is not None:
            for mf in collab.findall("bpmn:messageFlow", _NS):
                mid = mf.get("id", "")
                src = mf.get("sourceRef", "")
                tgt = mf.get("targetRef", "")
                if src and tgt:
                    g.msg_flows.append((src, tgt, mid))

        for nid, nd in g.nodes.items():
            proc = nd["process_id"]
            nd["participant"] = g.process_to_part.get(proc, "unknown")
            g.node_to_part[nid] = nd["participant"]

        if verbose:
            print(f"[parse] {len(g.nodes)} nodes, "
                  f"{len(g.seq_edges)} seq-flows, "
                  f"{len(g.msg_flows)} msg-flows, "
                  f"{len(g.participants)} participants, "
                  f"{len(g.catch_events)} intermediate catch events")

        return g


# ─────────────────────────────────────────────────────────────────────────────
# 2.  TARJAN SCC
# ─────────────────────────────────────────────────────────────────────────────

def tarjan_scc(nodes, succ):
    index_counter = [0]
    stack = []
    lowlink = {}
    index   = {}
    on_stack = {}
    sccs = []

    for node in nodes:
        if node not in index:
            call_stack = [(node, iter(succ.get(node, [])))]
            index[node] = index_counter[0]
            lowlink[node] = index_counter[0]
            index_counter[0] += 1
            stack.append(node)
            on_stack[node] = True

            while call_stack:
                v, children = call_stack[-1]
                try:
                    w = next(children)
                    if w not in index:
                        index[w] = index_counter[0]
                        lowlink[w] = index_counter[0]
                        index_counter[0] += 1
                        stack.append(w)
                        on_stack[w] = True
                        call_stack.append((w, iter(succ.get(w, []))))
                    elif on_stack.get(w, False):
                        lowlink[v] = min(lowlink[v], index[w])
                except StopIteration:
                    call_stack.pop()
                    if call_stack:
                        parent = call_stack[-1][0]
                        lowlink[parent] = min(lowlink[parent], lowlink[v])
                    if lowlink[v] == index[v]:
                        scc = []
                        while True:
                            w = stack.pop()
                            on_stack[w] = False
                            scc.append(w)
                            if w == v:
                                break
                        sccs.append(scc)
    return sccs


def build_dag(g, verbose=False):
    node_ids = list(g.nodes.keys())
    sccs = tarjan_scc(node_ids, g.succ)
    cyclic = [s for s in sccs if len(s) > 1]

    node_to_meta = {}
    for scc in sccs:
        if len(scc) == 1:
            node_to_meta[scc[0]] = scc[0]
        else:
            meta_id = "SCC_" + "_".join(sorted(scc))
            label = " [LOOP: " + " / ".join(
                g.nodes[n]["label"] for n in scc if g.nodes[n]["kind"] == "task"
            ) + "]"
            g.nodes[meta_id] = {
                "label": label, "kind": "meta_loop", "gateway_type": None,
                "process_id": g.nodes[scc[0]]["process_id"],
                "participant": g.node_to_part.get(scc[0], "unknown"),
                "scc_members": scc,
            }
            for n in scc:
                node_to_meta[n] = meta_id

    dag_succ = defaultdict(list)
    for src, tgt, _cond in g.seq_edges:
        ms = node_to_meta[src]
        mt = node_to_meta[tgt]
        if ms != mt and mt not in dag_succ[ms]:
            dag_succ[ms].append(mt)

    dag_nodes = list({node_to_meta[n] for n in node_ids})

    if verbose:
        print(f"[dag]   {len(dag_nodes)} DAG nodes, "
              f"{sum(len(v) for v in dag_succ.values())} DAG edges, "
              f"{len(cyclic)} cyclic SCCs")

    return dag_nodes, dag_succ, node_to_meta, cyclic


# ─────────────────────────────────────────────────────────────────────────────
# 3.  DOMINANCE TREE
# ─────────────────────────────────────────────────────────────────────────────

def build_dominance_tree(start, dag_nodes, dag_succ):
    topo = []
    visited = set()
    q = deque([start])
    while q:
        v = q.popleft()
        if v in visited:
            continue
        visited.add(v)
        topo.append(v)
        for w in dag_succ.get(v, []):
            if w not in visited:
                q.append(w)

    dag_pred = defaultdict(list)
    for v in topo:
        for w in dag_succ.get(v, []):
            if w in visited:
                dag_pred[w].append(v)

    all_nodes = set(topo)
    dom = {start: {start}}
    for n in topo[1:]:
        dom[n] = all_nodes.copy()

    changed = True
    while changed:
        changed = False
        for n in topo[1:]:
            preds = dag_pred.get(n, [])
            if not preds:
                new_dom = {n}
            else:
                new_dom = None
                for p in preds:
                    if new_dom is None:
                        new_dom = dom[p].copy()
                    else:
                        new_dom &= dom[p]
                new_dom = (new_dom or set()) | {n}
            if new_dom != dom[n]:
                dom[n] = new_dom
                changed = True

    idom = {start: None}
    for n in topo[1:]:
        doms_of_n = dom[n] - {n}
        idom_n = None
        for d in doms_of_n:
            if doms_of_n - {d} <= dom.get(d, set()):
                idom_n = d
                break
        idom[n] = idom_n

    return idom


# ─────────────────────────────────────────────────────────────────────────────
# 4.  DEONTIC CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def _bfs_reachable(start, succ, exclude=None):
    visited = set()
    q = deque([start])
    while q:
        v = q.popleft()
        if v in visited or v == exclude:
            continue
        visited.add(v)
        for w in succ.get(v, []):
            if w != exclude:
                q.append(w)
    return visited


def _branch_subgraph(split_gw, branch_tgt, merge_gw, dag_succ):
    fwd = set()
    q = deque([branch_tgt])
    while q:
        v = q.popleft()
        if v in fwd:
            continue
        fwd.add(v)
        if v == merge_gw:
            continue
        for w in dag_succ.get(v, []):
            q.append(w)
    sub = {}
    for v in fwd:
        sub[v] = [w for w in dag_succ.get(v, []) if w in fwd]
    return sub


def _find_merge_gateway(split_gw, dag_succ, all_nodes):
    branches = dag_succ.get(split_gw, [])
    if len(branches) < 2:
        return None
    reachable_per_branch = [_bfs_reachable(b, dag_succ) for b in branches]
    common = reachable_per_branch[0].copy()
    for r in reachable_per_branch[1:]:
        common &= r
    if not common:
        return None
    dist = {}
    q2 = deque([(split_gw, 0)])
    visited2 = set()
    while q2:
        v, d = q2.popleft()
        if v in visited2:
            continue
        visited2.add(v)
        dist[v] = d
        for w in dag_succ.get(v, []):
            q2.append((w, d + 1))
    return min(common, key=lambda n: dist.get(n, 999999))

def classify_deontic_type(start, dag_nodes, dag_succ, g):
    all_reachable = _bfs_reachable(start, dag_succ)
    end_nodes = {v for v in all_reachable if not dag_succ.get(v)}
    deontic = {v: "Permission" for v in all_reachable}

    if not end_nodes:
        return deontic

    globally_mandatory = set()
    for node in all_reachable:
        reachable_without = _bfs_reachable(start, dag_succ, exclude=node)
        if not any(e in reachable_without for e in end_nodes):
            globally_mandatory.add(node)
            deontic[node] = "Duty"

    globally_optional = all_reachable - globally_mandatory

    split_gateways = [
        nid for nid in all_reachable
        if g.nodes.get(nid, {}).get("kind") == "gateway"
        and g.nodes.get(nid, {}).get("gateway_type") in ("XOR", "EVENT")
        and len(dag_succ.get(nid, [])) >= 2
    ]

    for gw in split_gateways:
        merge = _find_merge_gateway(gw, dag_succ, all_reachable)
        for branch_entry in dag_succ.get(gw, []):
            sub = _branch_subgraph(gw, branch_entry, merge or "", dag_succ)
            branch_nodes = set(sub.keys())
            branch_ends = {v for v in branch_nodes if not sub.get(v)}
            if not branch_ends:
                continue

            # Find first task in branch by traversal order
            first_task = None
            bfs_q = deque([branch_entry])
            bfs_seen = set()
            while bfs_q:
                v = bfs_q.popleft()
                if v in bfs_seen:
                    continue
                bfs_seen.add(v)
                if g.nodes.get(v, {}).get("kind") == "task":
                    if first_task is None:
                        first_task = v
                for w in sub.get(v, []):
                    bfs_q.append(w)

            for node in branch_nodes & globally_optional:
                if g.nodes.get(node, {}).get("kind") != "task":
                    continue
                sub_without = {
                    v: [w for w in succs if w != node]
                    for v, succs in sub.items()
                    if v != node
                }
                reachable_in_branch = _bfs_reachable(branch_entry, sub_without, exclude=node)
                branch_end_reachable = any(e in reachable_in_branch for e in branch_ends)
                if not branch_end_reachable:
                    if node == first_task:
                        deontic[node] = "ConstrainedPermission"
                    else:
                        deontic[node] = "ConstrainedDuty"

    return deontic

# def classify_deontic_type(start, dag_nodes, dag_succ, g):
#     all_reachable = _bfs_reachable(start, dag_succ)
#     end_nodes = {v for v in all_reachable if not dag_succ.get(v)}
#     deontic = {v: "Permission" for v in all_reachable}

#     if not end_nodes:
#         return deontic

#     globally_mandatory = set()
#     for node in all_reachable:
#         reachable_without = _bfs_reachable(start, dag_succ, exclude=node)
#         if not any(e in reachable_without for e in end_nodes):
#             globally_mandatory.add(node)
#             deontic[node] = "Duty"

#     globally_optional = all_reachable - globally_mandatory

#     split_gateways = [
#         nid for nid in all_reachable
#         if g.nodes.get(nid, {}).get("kind") == "gateway"
#         and g.nodes.get(nid, {}).get("gateway_type") in ("XOR", "EVENT")
#         and len(dag_succ.get(nid, [])) >= 2
#     ]

#     for gw in split_gateways:
#         merge = _find_merge_gateway(gw, dag_succ, all_reachable)
#         for branch_entry in dag_succ.get(gw, []):
#             sub = _branch_subgraph(gw, branch_entry, merge or "", dag_succ)
#             branch_nodes = set(sub.keys())
#             branch_ends = {v for v in branch_nodes if not sub.get(v)}
#             if not branch_ends:
#                 continue
#             for node in branch_nodes & globally_optional:
#                 if g.nodes.get(node, {}).get("kind") != "task":
#                     continue
#                 sub_without = {
#                     v: [w for w in succs if w != node]
#                     for v, succs in sub.items()
#                     if v != node
#                 }
#                 reachable_in_branch = _bfs_reachable(branch_entry, sub_without, exclude=node)
#                 branch_end_reachable = any(e in reachable_in_branch for e in branch_ends)
#                 task_nodes_in_branch = [
#                     n for n in branch_nodes if g.nodes.get(n, {}).get("kind") == "task"
#                 ]
#                 # if not branch_end_reachable and len(task_nodes_in_branch) > 1:
#                 #     deontic[node] = "ConstrainedDuty"
#                 # Find first task in branch by traversal order
#                 first_task = None
#                 visited_order = []
#                 bfs_q = deque([branch_entry])
#                 bfs_seen = set()
#                 while bfs_q:
#                     v = bfs_q.popleft()
#                     if v in bfs_seen:
#                         continue
#                     bfs_seen.add(v)
#                     if g.nodes.get(v, {}).get("kind") == "task":
#                         if first_task is None:
#                             first_task = v
#                         visited_order.append(v)
#                     for w in sub.get(v, []):
#                         bfs_q.append(w)

#                 for node in branch_nodes & globally_optional:
#                     if g.nodes.get(node, {}).get("kind") != "task":
#                         continue
#                     sub_without = {
#                         v: [w for w in succs if w != node]
#                         for v, succs in sub.items()
#                         if v != node
#                     }
#                     reachable_in_branch = _bfs_reachable(branch_entry, sub_without, exclude=node)
#                     branch_end_reachable = any(e in reachable_in_branch for e in branch_ends)
#                     if not branch_end_reachable:
#                         if node == first_task:
#                             deontic[node] = "ConstrainedPermission"
#                         else:
#                             deontic[node] = "ConstrainedDuty"

#                     return deontic


# ─────────────────────────────────────────────────────────────────────────────
# 4b.  v8 NEW: WAIT PROHIBITIONS
#      Intermediate catch events → odrl:Prohibition on the downstream task
# ─────────────────────────────────────────────────────────────────────────────

def build_wait_prohibitions(g: BPMNGraph,
                             node_to_meta: Dict[str, str],
                             verbose: bool = False) -> List[dict]:
    """
    For every intermediateCatchEvent E in the BPMN:

      The participant whose lane contains E is PROHIBITED from performing
      the task immediately downstream of E, unless the event E has fired
      (i.e. the message has been received).

    This implements the "wait as wall" semantics:
      - Not a duty  (the participant doesn't DO waiting)
      - Not a permission (the participant has no choice about waiting)
      - A prohibition on proceeding, lifted when the condition is satisfied

    ODRL representation:
      odrl:Prohibition {
        odrl:assignee:  <the lane's party>
        odrl:action:    bpmn:perform "<downstream task label>"
        odrl:constraint: bpmn:eventReceived = "<event name>"
        rdfs:comment:   "Party is prohibited from performing <task> until
                         event '<event name>' is received."
      }

    The constraint encodes the lifting condition: the prohibition applies
    UNLESS bpmn:eventReceived = <event name>.  In ODRL semantics this means:
    the action is forbidden when the constraint is NOT satisfied.

    Returns a list of prohibition rule dicts (same shape as task rules).
    """
    # Build message flow target lookup: target_node_id → source party name
    # (who sends the message that satisfies the catch event)
    msg_sender_for_target: Dict[str, str] = {}
    for (src, tgt, fid) in g.msg_flows:
        src_part = g.nodes.get(src, {}).get("participant", "")
        src_nm   = g.participants.get(src_part, {}).get("name", src_part)
        if tgt in g.nodes:
            msg_sender_for_target[tgt] = src_nm
        # Also check if tgt is itself in nodes (some message flows target participants)

    wait_prohibitions: List[dict] = []

    for ce_id in g.catch_events:
        ce_node = g.nodes.get(ce_id, {})
        ce_label = ce_node.get("label", ce_id).strip()
        part_id  = ce_node.get("participant", "unknown")
        part_nm  = g.participants.get(part_id, {}).get("name", part_id)

        # Find the immediately downstream task(s) after this catch event
        # (follow sequence flows: catch event → [gateway?] → task)
        downstream_tasks = []
        visited_fw = set()
        q = deque(list(g.succ.get(ce_id, [])))
        while q:
            nxt = q.popleft()
            if nxt in visited_fw:
                continue
            visited_fw.add(nxt)
            nxt_nd = g.nodes.get(nxt, {})
            if nxt_nd.get("kind") == "task":
                downstream_tasks.append((nxt, nxt_nd))
            elif nxt_nd.get("kind") in ("gateway", "event", "startEvent", "endEvent"):
                # Pass through gateways/merge points to find the real task
                q.extend(g.succ.get(nxt, []))

        # Who sends the message that triggers this catch event?
        sender_nm = msg_sender_for_target.get(ce_id, None)

        for task_id, task_nd in downstream_tasks:
            task_label = task_nd.get("label", task_id)
            uid = "wait_" + slugify(ce_label) + "_before_" + slugify(task_label) + "_" + ce_id[-6:]

            constraint_str = f"event: {ce_label}"

            inter = []
            if sender_nm and sender_nm != part_nm:
                inter.append({
                    "duty_type":   "sentBy",
                    "other_party": sender_nm,
                    "flow_id":     "",
                })

            wait_prohibitions.append({
                "uid":           uid,
                "type":          "WaitProhibition",   # sub-type; emits as odrl:Prohibition
                "catch_event":   ce_label,
                "action":        task_label,
                "assignee":      part_nm,
                "precondition":  [],
                "constraints":   [constraint_str],
                "inter_party":   inter,
                "comment": (
                    f"'{part_nm}' is prohibited from performing '{task_label}' "
                    f"until event '{ce_label}' is received"
                    + (f" (sent by '{sender_nm}')" if sender_nm else "") + "."
                ),
            })

            if verbose:
                print(f"  [WaitProhibition] {part_nm!r} cannot do '{task_label}' " + f"until '{ce_label}' received")

    return wait_prohibitions


# ─────────────────────────────────────────────────────────────────────────────
# 5.  ROLE-PARTITIONED DFS
# ─────────────────────────────────────────────────────────────────────────────

def _simplify_conditions(raw_conds, g):
    if raw_conds is None:
        return []

    by_gateway = defaultdict(set)
    ungrouped = []
    for c in raw_conds:
        if " = " in c:
            gw, branch = c.split(" = ", 1)
            by_gateway[gw.strip()].add(branch.strip())
        elif c.startswith("event: "):
            by_gateway["__event__"].add(c)
        else:
            ungrouped.append(c)

    gw_total_branches = {}
    event_gw_branch_counts = {}
    for nid, nd in g.nodes.items():
        lbl = nd.get("label", "").strip()
        gtype = nd.get("gateway_type", "")
        if nd.get("kind") == "gateway":
            if gtype == "EVENT":
                event_gw_branch_counts[nid] = len(g.succ.get(nid, []))
            elif lbl:
                gw_total_branches[lbl] = len(g.succ.get(nid, []))

    surviving = list(ungrouped)

    for gw_label, seen_branches in by_gateway.items():
        if gw_label == "__event__":
            continue
        total = gw_total_branches.get(gw_label, len(seen_branches) + 1)
        if len(seen_branches) < total:
            for branch in sorted(seen_branches):
                surviving.append(f"{gw_label} = {branch}")

    if "__event__" in by_gateway:
        event_conds = by_gateway["__event__"]
        total_event_branches = max(event_gw_branch_counts.values(), default=0)
        if not (len(event_conds) >= total_event_branches and total_event_branches > 0):
            surviving.extend(sorted(event_conds))

    return sorted(surviving)


def role_partitioned_dfs(start, g, dag_succ, node_to_meta, deontic_map, idom, verbose=False):

    # Message flow lookups — only attach to genuine sender/receiver tasks.
    # v8 fix: do NOT attach waitFor to tasks that merely have a catch event
    # before them; that is now handled by WaitProhibitions.
    msg_out: Dict[str, List[Tuple]] = defaultdict(list)
    # msg_in intentionally left empty for tasks — catch events handle wait semantics.
    # We keep it for tasks that directly receive message flows (rare in these BPMNs).
    msg_in_direct: Dict[str, List[Tuple]] = defaultdict(list)

    for (src, tgt, fid) in g.msg_flows:
        ms = node_to_meta.get(src, src)
        mt = node_to_meta.get(tgt, tgt)
        # Only attach msg_out to actual task nodes (not pools/participants)
        if ms in g.nodes and g.nodes[ms].get("kind") == "task":
            msg_out[ms].append((mt, fid))
        # Only attach direct waitFor to task nodes that directly receive messages
        # (i.e. target is a task, not a catch event — catch events are WaitProhibitions)
        if mt in g.nodes and g.nodes[mt].get("kind") == "task":
            src_nd = g.nodes.get(ms, {})
            if src_nd.get("kind") == "task":
                msg_in_direct[mt].append((ms, fid))

    # Condition map: (meta_src, meta_tgt) → qualified condition string
    cond_map: Dict[Tuple, str] = {}
    for (src, tgt, branch_label) in g.seq_edges:
        ms = node_to_meta.get(src, src)
        mt = node_to_meta.get(tgt, tgt)
        src_nd   = g.nodes.get(ms, {})
        gw_type  = src_nd.get("gateway_type", "")
        gw_label = src_nd.get("label", "").strip()

        if gw_type == "EVENT":
            tgt_nd  = g.nodes.get(mt, {})
            tgt_lbl = tgt_nd.get("label", "").strip()
            if tgt_nd.get("kind") == "event" and tgt_lbl:
                cond_map[(ms, mt)] = f"event: {tgt_lbl}"
            elif branch_label:
                cond_map[(ms, mt)] = branch_label
        elif branch_label:
            if gw_label:
                cond_map[(ms, mt)] = f"{gw_label} = {branch_label}"
            else:
                cond_map[(ms, mt)] = branch_label

    arrived: Dict[str, Set[frozenset]] = defaultdict(set)
    node_conditions: Dict[str, Optional[List[str]]] = {}
    emit_order: List[str] = []
    uid_map: Dict[str, str] = {}

    def dfs(v, path_conds):
        if path_conds in arrived[v]:
            return
        arrived[v].add(path_conds)

        nd   = g.nodes.get(v, {})
        kind = nd.get("kind", "unknown")

        if kind == "gateway":
            for w in dag_succ.get(v, []):
                edge_cond = cond_map.get((v, w), "")
                new_conds = frozenset(path_conds | {edge_cond}) if edge_cond else path_conds
                dfs(w, new_conds)
            return

        if kind in ("startEvent", "endEvent", "event"):
            for w in dag_succ.get(v, []):
                dfs(w, path_conds)
            return

        # Task / meta_loop
        if v not in node_conditions:
            node_conditions[v] = None if not path_conds else list(path_conds)
            emit_order.append(v)
        else:
            current = node_conditions[v]
            if current is None:
                pass
            elif not path_conds:
                node_conditions[v] = None
            else:
                existing = set(current)
                for c in path_conds:
                    if c not in existing:
                        current.append(c)
                        existing.add(c)

        for w in dag_succ.get(v, []):
            dfs(w, path_conds)

    dfs(start, frozenset())

    rules = []
    for v in emit_order:
        nd      = g.nodes.get(v, {})
        label   = nd.get("label", v)
        part_id = nd.get("participant", "unknown")
        part_nm = g.participants.get(part_id, {}).get("name", part_id)

        base_type = deontic_map.get(v, "Permission")
        uid       = slugify(label) + "_" + v[-6:]
        uid_map[v] = uid

        idom_v  = idom.get(v)
        pre_uid = uid_map.get(idom_v) if idom_v else None

        raw_conds   = node_conditions.get(v)
        constraints = _simplify_conditions(raw_conds, g)

        rule_type = base_type
        if rule_type == "Permission" and constraints:
            rule_type = "ConstrainedPermission"

        # Inter-party: only notify (outgoing message flows from tasks).
        # waitFor is no longer attached here — it became WaitProhibitions.
        inter = []
        for (mt, fid) in msg_out.get(v, []):
            tgt_pid = g.nodes.get(mt, {}).get("participant", "unknown")
            tgt_nm  = g.participants.get(tgt_pid, {}).get("name", tgt_pid)
            inter.append({"duty_type": "notify", "other_party": tgt_nm,
                          "trigger": "onCompletion", "flow_id": fid})

        # Direct task-to-task message receives (rare; synchronous pattern)
        for (ms, fid) in msg_in_direct.get(v, []):
            src_pid = g.nodes.get(ms, {}).get("participant", "unknown")
            src_nm  = g.participants.get(src_pid, {}).get("name", src_pid)
            inter.append({"duty_type": "waitFor", "other_party": src_nm,
                          "trigger": "onReceipt", "flow_id": fid})

        rule = {
            "uid":          uid,
            "type":         rule_type,
            "action":       label,
            "assignee":     part_nm,
            "precondition": [pre_uid] if pre_uid else [],
            "constraints":  constraints,
            "inter_party":  inter,
        }
        rules.append(rule)

        if verbose:
            cond_str = ", ".join(constraints) if constraints else "(unconditional)"
            print(f"  [{rule_type:10s}] {label!r:40s}  assignee={part_nm!r}  "
                  f"conditions={cond_str}")

    return rules


# ─────────────────────────────────────────────────────────────────────────────
# 6a. IMPLICIT ROLE PROHIBITIONS  (one per role covering all other-party tasks)
# ─────────────────────────────────────────────────────────────────────────────

def build_role_prohibitions(rules):
    tasks_by_party = defaultdict(set)
    for r in rules:
        tasks_by_party[r["assignee"]].add(r["action"])

    all_parties = set(tasks_by_party.keys())
    prohibitions = []
    for party in sorted(all_parties):
        own = tasks_by_party[party]
        forbidden = set()
        for other, other_tasks in tasks_by_party.items():
            if other != party:
                forbidden |= other_tasks - own
        if not forbidden:
            continue
        prohibitions.append({
            "uid":          slugify(party) + "_prohibited",
            "type":         "Prohibition",
            "action":       sorted(forbidden),
            "assignee":     party,
            "precondition": [],
            "constraints":  [],
            "inter_party":  [],
            "comment":      f"Implicit prohibition: {party} may not perform tasks owned by other parties.",
        })
    return prohibitions


# ─────────────────────────────────────────────────────────────────────────────
# 6.  ODRL JSON-LD EMISSION
# ─────────────────────────────────────────────────────────────────────────────

ODRL_CONTEXT = {
    "odrl": "http://www.w3.org/ns/odrl/2/",
    "rdf":  "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
    "bpmn": "http://bpmn.io/schema/bpmn#",
    "ex":   "http://example.org/policy/",
}

ODRL_TYPE_MAP = {
    "Duty":                  "odrl:Duty",
    "ConstrainedDuty":       "odrl:Duty",
    "Permission":            "odrl:Permission",
    "ConstrainedPermission": "odrl:Permission",
    "Prohibition":           "odrl:Prohibition",
    "WaitProhibition":       "odrl:Prohibition",
}


def emit_odrl(task_rules, wait_prohibitions, role_prohibitions, g, process_label="CreditScoring"):
    permissions  = []
    obligations  = []
    prohibitions = []

    # ── Task rules ──────────────────────────────────────────────────────────
    for r in task_rules:
        assignee_uri = "ex:" + slugify(r["assignee"])

        constraints = []
        for cond in r["constraints"]:
            constraints.append({
                "@type":             "odrl:Constraint",
                "odrl:leftOperand":  {"@id": "bpmn:gatewayCondition"},
                "odrl:operator":     {"@id": "odrl:eq"},
                "odrl:rightOperand": cond,
            })

        duties = []
        for pre in r["precondition"]:
            duties.append({
                "@type":        "odrl:Duty",
                "@id":          "ex:" + pre,
                "rdfs:comment": f"Must complete '{pre}' before this action.",
            })
        for ip in r["inter_party"]:
            if ip["duty_type"] == "notify":
                duties.append({
                    "@type":         "odrl:Duty",
                    "odrl:action":   {"@id": "odrl:inform"},
                    "odrl:assignee": {"@id": "ex:" + slugify(ip["other_party"])},
                    "rdfs:comment":  f"Notify '{ip['other_party']}' on completion.",
                })
            elif ip["duty_type"] == "waitFor":
                constraints.append({
                    "@type":             "odrl:Constraint",
                    "odrl:leftOperand":  {"@id": "bpmn:messageReceived"},
                    "odrl:operator":     {"@id": "odrl:eq"},
                    "odrl:rightOperand": f"messageFrom:{slugify(ip['other_party'])}",
                    "rdfs:comment":      f"Wait for message from '{ip['other_party']}'.",
                })

        rule_obj = {
            "@type":         ODRL_TYPE_MAP.get(r["type"], "odrl:Permission"),
            "@id":           "ex:" + r["uid"],
            "odrl:action":   {"@id": "bpmn:perform", "rdfs:label": r["action"]},
            "odrl:assignee": {"@id": assignee_uri},
        }
        if constraints:
            rule_obj["odrl:constraint"] = constraints
        if duties:
            rule_obj["odrl:duty"] = duties

        if r["type"] in ("Duty", "ConstrainedDuty"):
            obligations.append(rule_obj)
        else:
            permissions.append(rule_obj)

    # ── Wait prohibitions (v8 new) ──────────────────────────────────────────
    for wp in wait_prohibitions:
        assignee_uri = "ex:" + slugify(wp["assignee"])
        constraints = [
            {
                "@type":             "odrl:Constraint",
                "odrl:leftOperand":  {"@id": "bpmn:eventReceived"},
                "odrl:operator":     {"@id": "odrl:eq"},
                "odrl:rightOperand": wp["constraints"][0],  # "event: <name>"
                "rdfs:comment":      (
                    f"Prohibition is lifted when event "
                    f"'{wp['catch_event']}' is received."
                ),
            }
        ]
        # Add sender info as an additional constraint if known
        for ip in wp.get("inter_party", []):
            if ip["duty_type"] == "sentBy":
                constraints.append({
                    "@type":             "odrl:Constraint",
                    "odrl:leftOperand":  {"@id": "bpmn:messageSender"},
                    "odrl:operator":     {"@id": "odrl:eq"},
                    "odrl:rightOperand": ip["other_party"],
                    "rdfs:comment":      f"Message must be sent by '{ip['other_party']}'.",
                })

        prohibitions.append({
            "@type":         "odrl:Prohibition",
            "@id":           "ex:" + wp["uid"],
            "rdfs:comment":  wp["comment"],
            "odrl:assignee": {"@id": assignee_uri},
            "odrl:action":   {"@id": "bpmn:perform", "rdfs:label": wp["action"]},
            "odrl:constraint": constraints,
        })

    # ── Role prohibitions (cross-party task access) ─────────────────────────
    for pr in role_prohibitions:
        assignee_uri = "ex:" + slugify(pr["assignee"])
        prohibitions.append({
            "@type":         "odrl:Prohibition",
            "@id":           "ex:" + pr["uid"],
            "rdfs:comment":  pr["comment"],
            "odrl:assignee": {"@id": assignee_uri},
            "odrl:action":   [
                {"@id": "bpmn:perform", "rdfs:label": a}
                for a in pr["action"]
            ],
        })

    policy = {
        "@context":     ODRL_CONTEXT,
        "@type":        "odrl:Set",
        "@id":          f"ex:{slugify(process_label)}Policy",
        "rdfs:label":   f"{process_label} BPMN Policy",
        "odrl:profile": {"@id": "bpmn:BPMNDeonticProfile"},
    }
    if permissions:
        policy["odrl:permission"] = permissions
    if obligations:
        policy["odrl:obligation"] = obligations
    if prohibitions:
        policy["odrl:prohibition"] = prohibitions

    all_rules = task_rules + wait_prohibitions + role_prohibitions
    parties = sorted({r["assignee"] for r in all_rules})
    policy["odrl:parties"] = [
        {"@type": "odrl:Party", "@id": "ex:" + slugify(p), "rdfs:label": p}
        for p in parties
    ]

    return policy


# ─────────────────────────────────────────────────────────────────────────────
# 7.  HUMAN-READABLE SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(task_rules, wait_prohibitions, role_prohibitions, g):
    print("\n" + "═" * 72)
    print("  ODRL POLICY SUMMARY")
    print("═" * 72)

    by_party = defaultdict(list)
    for r in task_rules:
        by_party[r["assignee"]].append(r)

    for party, prules in sorted(by_party.items()):
        print(f"\n  Party: {party}")
        print("  " + "─" * 60)
        for r in prules:
            conds = ", ".join(r["constraints"]) if r["constraints"] else "(unconditional)"
            pre   = ", ".join(r["precondition"]) if r["precondition"] else "—"
            print(f"    [{r['type']:25s}]  {r['action']}")
            print(f"                                    conditions : {conds}")
            print(f"                                    after      : {pre}")
            for ip in r["inter_party"]:
                print(f"                                    {ip['duty_type']:10s}: {ip['other_party']}")

    print(f"\n  ── Wait Prohibitions (intermediate catch events) ──")
    if wait_prohibitions:
        for wp in wait_prohibitions:
            print(f"    [WaitProhibition]  {wp['assignee']} cannot proceed to "
                  f"'{wp['action']}' until event '{wp['catch_event']}' received")
            for ip in wp.get("inter_party", []):
                if ip["duty_type"] == "sentBy":
                    print(f"                       (message sent by '{ip['other_party']}')")
    else:
        print("    (none)")

    print(f"\n  ── Role Prohibitions (cross-party task access) ──")
    for pr in role_prohibitions:
        print(f"\n    {pr['assignee']} may NOT perform:")
        for a in pr["action"]:
            print(f"      ✗  {a}")

    duties  = [r for r in task_rules if r["type"] == "Duty"]
    cduts   = [r for r in task_rules if r["type"] == "ConstrainedDuty"]
    perms   = [r for r in task_rules if r["type"] == "Permission"]
    cperms  = [r for r in task_rules if r["type"] == "ConstrainedPermission"]
    print(f"\n  Totals: {len(duties)} Duties | "
          f"{len(cduts)} ConstrainedDuties | "
          f"{len(perms)} Permissions | "
          f"{len(cperms)} ConstrainedPermissions | "
          f"{len(wait_prohibitions)} WaitProhibitions | "
          f"{len(role_prohibitions)} RoleProhibitions")
    print("═" * 72 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def slugify(text):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "node"


def run_pipeline(xml_path, output_path, verbose=False):
    print(f"[1/6] Parsing BPMN XML: {xml_path}")
    g = BPMNGraph.from_xml(xml_path, verbose=verbose)
    if not g.nodes:
        sys.exit("ERROR: No nodes found in BPMN file.")

    print(f"[2/6] Tarjan SCC decomposition")
    dag_nodes, dag_succ, node_to_meta, cyclic_sccs = build_dag(g, verbose=verbose)
    if cyclic_sccs:
        print(f"      ⚠  {len(cyclic_sccs)} cycle(s) detected and collapsed.")
    else:
        print(f"      ✓  No cycles — graph is already a DAG.")

    dag_pred_cnt = defaultdict(int)
    for v in dag_nodes:
        for w in dag_succ.get(v, []):
            dag_pred_cnt[w] += 1
    dag_roots = [v for v in dag_nodes if dag_pred_cnt[v] == 0]

    start_metas = []
    for sn in g.start_nodes:
        meta = node_to_meta.get(sn, sn)
        if meta in dag_nodes and meta not in start_metas:
            start_metas.append(meta)
    for r in dag_roots:
        if r not in start_metas:
            start_metas.append(r)

    all_idom    = {}
    all_deontic = {}
    for sm in start_metas:
        all_idom.update(build_dominance_tree(sm, dag_nodes, dag_succ))
        all_deontic.update(classify_deontic_type(sm, dag_nodes, dag_succ, g))

    counts = {t: sum(1 for n, dt in all_deontic.items()
                     if dt == t and g.nodes.get(n, {}).get("kind") == "task")
              for t in ("Duty", "ConstrainedDuty", "Permission")}
    print(f"[3/6] Dominance-tree construction ({len(start_metas)} root(s))")
    print(f"[4/6] Deontic classification — "
          f"{counts['Duty']} Duties, "
          f"{counts['ConstrainedDuty']} ConstrainedDuties, "
          f"{counts['Permission']}+ Permissions")

    print(f"[5/6] Role-partitioned DFS + inter-party duties")
    all_task_rules = []
    for sm in start_metas:
        rules = role_partitioned_dfs(
            sm, g, dag_succ, node_to_meta, all_deontic, all_idom, verbose=verbose
        )
        all_task_rules.extend(rules)

    # v8: build wait prohibitions from intermediate catch events
    print(f"[5b]  Building wait prohibitions from {len(g.catch_events)} intermediate catch event(s)")
    wait_prohibitions = build_wait_prohibitions(g, node_to_meta, verbose=verbose)

    role_prohibitions = build_role_prohibitions(all_task_rules)

    print(f"[6/6] Emitting ODRL JSON-LD → {output_path}")
    policy = emit_odrl(all_task_rules, wait_prohibitions, role_prohibitions, g,
                       process_label="CreditScoring")

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, indent=2, ensure_ascii=False)

    print_summary(all_task_rules, wait_prohibitions, role_prohibitions, g)
    print(f"Done. ODRL policy written to: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY PIPELINE  (used by FastAPI — no file I/O)
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline_in_memory(xml_bytes: bytes, process_label: str = "Process",
                           verbose: bool = False) -> dict:
    """
    Pure in-memory pipeline: accepts raw BPMN XML bytes, returns ODRL policy dict.
    No file I/O — suitable for use as a library / FastAPI backend.
    """
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".bpmn", delete=False) as tmp:
        tmp.write(xml_bytes)
        tmp_path = tmp.name
    try:
        g = BPMNGraph.from_xml(tmp_path, verbose=verbose)
    finally:
        os.unlink(tmp_path)

    if not g.nodes:
        raise ValueError("No BPMN nodes found in uploaded file.")

    dag_nodes, dag_succ, node_to_meta, cyclic_sccs = build_dag(g, verbose=verbose)

    dag_pred_cnt = defaultdict(int)
    for v in dag_nodes:
        for w in dag_succ.get(v, []):
            dag_pred_cnt[w] += 1
    dag_roots = [v for v in dag_nodes if dag_pred_cnt[v] == 0]

    start_metas = []
    for sn in g.start_nodes:
        meta = node_to_meta.get(sn, sn)
        if meta in dag_nodes and meta not in start_metas:
            start_metas.append(meta)
    for r in dag_roots:
        if r not in start_metas:
            start_metas.append(r)

    all_idom    = {}
    all_deontic = {}
    for sm in start_metas:
        all_idom.update(build_dominance_tree(sm, dag_nodes, dag_succ))
        all_deontic.update(classify_deontic_type(sm, dag_nodes, dag_succ, g))

    all_task_rules = []
    for sm in start_metas:
        rules = role_partitioned_dfs(
            sm, g, dag_succ, node_to_meta, all_deontic, all_idom, verbose=verbose
        )
        all_task_rules.extend(rules)

    wait_prohibitions = build_wait_prohibitions(g, node_to_meta, verbose=verbose)
    role_prohibitions = build_role_prohibitions(all_task_rules)

    policy = emit_odrl(all_task_rules, wait_prohibitions, role_prohibitions, g,
                       process_label=process_label)

    duties  = sum(1 for r in all_task_rules if r["type"] == "Duty")
    cduts   = sum(1 for r in all_task_rules if r["type"] == "ConstrainedDuty")
    perms   = sum(1 for r in all_task_rules if r["type"] in ("Permission", "ConstrainedPermission"))
    policy["_meta"] = {
        "duties": duties,
        "constrained_duties": cduts,
        "permissions": perms,
        "wait_prohibitions": len(wait_prohibitions),
        "role_prohibitions": len(role_prohibitions),
        "cyclic_sccs": len(cyclic_sccs),
    }
    return policy


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI SERVICE
# ─────────────────────────────────────────────────────────────────────────────

from typing import Annotated
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

app = FastAPI(
    title="BPMN → ODRL Policy Extraction API",
    description=(
        "Automatically extracts ODRL deontic policies (obligations, permissions, "
        "prohibitions) from BPMN 2.0 XML process models. Based on bpmn2odrl v9."
    ),
    version="0.9.0",
    contact={"name": "OEG – Universidad Politécnica de Madrid"},
    license_info={"name": "Apache 2.0"},
)


@app.get("/", summary="Health check", tags=["meta"])
def root():
    return {
        "service": "bpmn2odrl",
        "version": "0.9.0",
        "status": "ok",
        "endpoints": {
            "POST /convert":          "Upload BPMN → receive ODRL JSON-LD in response body",
            "POST /convert/download": "Upload BPMN → download ODRL .jsonld file",
            "GET  /docs":             "Interactive Swagger UI",
        },
    }


@app.post("/convert", summary="Convert BPMN to ODRL (JSON response)", tags=["conversion"])
async def convert(
    file: Annotated[UploadFile, File(description="BPMN 2.0 XML file (.bpmn or .xml)")],
    process_label: Annotated[str, Form()] = "Process",
    verbose: Annotated[bool, Form()] = False,
):
    _validate_upload(file)
    xml_bytes = await file.read()
    try:
        policy = run_pipeline_in_memory(xml_bytes, process_label=process_label, verbose=verbose)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc
    return JSONResponse(content=policy, media_type="application/ld+json")


@app.post("/convert/download", summary="Convert BPMN to ODRL (file download)", tags=["conversion"])
async def convert_download(
    file: Annotated[UploadFile, File(description="BPMN 2.0 XML file (.bpmn or .xml)")],
    process_label: Annotated[str, Form()] = "Process",
    verbose: Annotated[bool, Form()] = False,
):
    _validate_upload(file)
    xml_bytes = await file.read()
    original_stem = file.filename.rsplit(".", 1)[0] if file.filename else "policy"
    output_filename = f"{original_stem}.odrl.jsonld"
    try:
        policy = run_pipeline_in_memory(xml_bytes, process_label=process_label, verbose=verbose)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc
    json_bytes = json.dumps(policy, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        content=json_bytes,
        media_type="application/ld+json",
        headers={"Content-Disposition": f'attachment; filename="{output_filename}"'},
    )


def _validate_upload(file: UploadFile):
    if file.filename and not file.filename.lower().endswith((".bpmn", ".xml")):
        raise HTTPException(status_code=415, detail="Only .bpmn or .xml files are accepted.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (still works: python main.py myfile.bpmn -o out.jsonld)
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert BPMN XML to ODRL JSON-LD (v9)."
    )
    parser.add_argument("bpmn_file")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    out = args.output or args.bpmn_file.rsplit(".", 1)[0] + ".odrl.jsonld"

    g = BPMNGraph.from_xml(args.bpmn_file, verbose=args.verbose)
    if not g.nodes:
        sys.exit("ERROR: No nodes found in BPMN file.")

    dag_nodes, dag_succ, node_to_meta, cyclic_sccs = build_dag(g, verbose=args.verbose)

    dag_pred_cnt = defaultdict(int)
    for v in dag_nodes:
        for w in dag_succ.get(v, []):
            dag_pred_cnt[w] += 1
    dag_roots = [v for v in dag_nodes if dag_pred_cnt[v] == 0]

    start_metas = []
    for sn in g.start_nodes:
        meta = node_to_meta.get(sn, sn)
        if meta in dag_nodes and meta not in start_metas:
            start_metas.append(meta)
    for r in dag_roots:
        if r not in start_metas:
            start_metas.append(r)

    all_idom    = {}
    all_deontic = {}
    for sm in start_metas:
        all_idom.update(build_dominance_tree(sm, dag_nodes, dag_succ))
        all_deontic.update(classify_deontic_type(sm, dag_nodes, dag_succ, g))

    all_task_rules = []
    for sm in start_metas:
        rules = role_partitioned_dfs(
            sm, g, dag_succ, node_to_meta, all_deontic, all_idom, verbose=args.verbose
        )
        all_task_rules.extend(rules)

    wait_prohibitions = build_wait_prohibitions(g, node_to_meta, verbose=args.verbose)
    role_prohibitions = build_role_prohibitions(all_task_rules)

    policy = emit_odrl(all_task_rules, wait_prohibitions, role_prohibitions, g,
                       process_label="Process")

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, indent=2, ensure_ascii=False)

    print_summary(all_task_rules, wait_prohibitions, role_prohibitions, g)
    print(f"Done. ODRL policy written to: {out}")


if __name__ == "__main__":
    main()