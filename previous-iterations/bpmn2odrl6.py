"""
BPMN → ODRL Converter
=====================
Implements the graph-traversal pipeline from:
  "BPMN as a Source of ODRL Policies: Theoretical Grounding and Graph Traversal Pipeline"

Pipeline stages
---------------
  1. Parse BPMN XML
       → nodes (tasks, gateways, events), edges (sequence flows, message flows)
       → swim-lane / participant assignments
  2. Tarjan SCC decomposition  — detect cycles, collapse to DAG meta-nodes
  3. Dominance-tree construction (Lengauer-Tarjan simple version)
       — obligation nesting / precondition chaining
  4. Critical-path classification
       — critical-path tasks  → odrl:Duty
       — non-critical tasks   → odrl:Permission
  5. Role-partitioned DFS
       — per swim-lane party assignment
       — message flows → inter-party duties / constraints
  6. ODRL JSON-LD emission

Usage
-----
  python bpmn_to_odrl.py credit_scoring.bpmn
  python bpmn_to_odrl.py credit_scoring.bpmn -o policy.jsonld
  python bpmn_to_odrl.py credit_scoring.bpmn --verbose

Dependencies: Python 3.8+ standard library only.
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
    """
    Holds the parsed BPMN as a directed graph.

    Attributes
    ----------
    nodes        : {id: {label, kind, gateway_type, participant, process_id}}
    succ         : {id: [id, ...]}   sequence-flow successors
    pred         : {id: [id, ...]}   sequence-flow predecessors
    seq_edges    : [(src, tgt, condition_label)]
    msg_flows    : [(src, tgt, id)]
    participants : {participant_id: {name, process_id}}
    node_to_part : {node_id: participant_id}
    """

    def __init__(self):
        self.nodes: Dict[str, dict]         = {}
        self.succ:  Dict[str, List[str]]    = defaultdict(list)
        self.pred:  Dict[str, List[str]]    = defaultdict(list)
        self.seq_edges: List[Tuple]         = []   # (src, tgt, condition)
        self.msg_flows: List[Tuple]         = []   # (src, tgt, id)
        self.participants: Dict[str, dict]  = {}
        self.node_to_part: Dict[str, str]   = {}
        self.process_to_part: Dict[str, str]= {}
        self.start_nodes: List[str]         = []
        self.end_nodes:   List[str]         = []

    # ── helpers ──────────────────────────────────────────────────────────────

    def _add_node(self, nid: str, label: str, kind: str,
                  gateway_type: Optional[str], process_id: str):
        self.nodes[nid] = {
            "label":        label,
            "kind":         kind,           # task | gateway | startEvent | endEvent | event
            "gateway_type": gateway_type,   # XOR / AND / OR / None
            "process_id":   process_id,
            "participant":  self.process_to_part.get(process_id, "unknown"),
        }

    def _add_seq(self, src: str, tgt: str, condition: str = ""):
        self.succ[src].append(tgt)
        self.pred[tgt].append(src)
        self.seq_edges.append((src, tgt, condition))

    # ── main parser ──────────────────────────────────────────────────────────

    @classmethod
    def from_xml(cls, xml_path: str, verbose: bool = False) -> "BPMNGraph":
        g = cls()
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # ── 1. Participants & process mapping ──────────────────────────────
        collab = root.find("bpmn:collaboration", _NS)
        if collab is not None:
            for p in collab.findall("bpmn:participant", _NS):
                pid    = p.get("id", "")
                pname  = p.get("name", pid)
                procid = p.get("processRef", "")
                g.participants[pid]          = {"name": pname, "process_id": procid}
                g.process_to_part[procid]    = pid

        # ── 2. Walk every process ──────────────────────────────────────────
        for process_el in root.findall("bpmn:process", _NS):
            proc_id = process_el.get("id", "")

            # Collect condition labels on sequence flows inside this process
            cond_map: Dict[str, str] = {}   # flow_id → condition text
            for sf in process_el.findall("bpmn:sequenceFlow", _NS):
                fid   = sf.get("id", "")
                fname = sf.get("name", "")
                cond_map[fid] = fname

            # Tasks
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

                elif tag in EVENT_TAGS:
                    g._add_node(nid, lbl, "event", None, proc_id)

            # Sequence flows
            for sf in process_el.findall("bpmn:sequenceFlow", _NS):
                src  = sf.get("sourceRef", "")
                tgt  = sf.get("targetRef", "")
                cond = sf.get("name", "")
                if src and tgt:
                    g._add_seq(src, tgt, cond)

        # ── 3. Message flows ───────────────────────────────────────────────
        if collab is not None:
            for mf in collab.findall("bpmn:messageFlow", _NS):
                mid = mf.get("id", "")
                src = mf.get("sourceRef", "")
                tgt = mf.get("targetRef", "")
                if src and tgt:
                    # If the endpoint is a participant (pool), map to its start/end
                    # (common BPMN notation — keep as-is, resolve later)
                    g.msg_flows.append((src, tgt, mid))

        # ── 4. Populate node_to_part ───────────────────────────────────────
        for nid, nd in g.nodes.items():
            proc = nd["process_id"]
            nd["participant"] = g.process_to_part.get(proc, "unknown")
            g.node_to_part[nid] = nd["participant"]

        if verbose:
            print(f"[parse] {len(g.nodes)} nodes, "
                  f"{len(g.seq_edges)} seq-flows, "
                  f"{len(g.msg_flows)} msg-flows, "
                  f"{len(g.participants)} participants")

        return g


# ─────────────────────────────────────────────────────────────────────────────
# 2.  TARJAN SCC  (cycle detection / DAG normalisation)
# ─────────────────────────────────────────────────────────────────────────────

def tarjan_scc(nodes: List[str],
               succ:  Dict[str, List[str]]) -> List[List[str]]:
    """
    Returns list of SCCs in reverse topological order.
    Each SCC is a list of node-ids. Singletons = acyclic nodes.
    """
    index_counter = [0]
    stack         = []
    lowlink:  Dict[str, int]  = {}
    index:    Dict[str, int]  = {}
    on_stack: Dict[str, bool] = {}
    sccs:     List[List[str]] = []

    def strongconnect(v: str):
        index[v]    = index_counter[0]
        lowlink[v]  = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True

        for w in succ.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w, False):
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    # Use iterative DFS to avoid Python recursion limit
    for node in nodes:
        if node not in index:
            # iterative version
            call_stack = [(node, iter(succ.get(node, [])))]
            index[node]    = index_counter[0]
            lowlink[node]  = index_counter[0]
            index_counter[0] += 1
            stack.append(node)
            on_stack[node] = True

            while call_stack:
                v, children = call_stack[-1]
                try:
                    w = next(children)
                    if w not in index:
                        index[w]    = index_counter[0]
                        lowlink[w]  = index_counter[0]
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


def build_dag(g: BPMNGraph, verbose: bool = False
              ) -> Tuple[List[str], Dict[str, List[str]], Dict[str, str], List[List[str]]]:
    """
    Collapse cycles via Tarjan SCC.
    Returns:
      dag_nodes   : list of node-ids (singletons) + meta-node ids (SCCs)
      dag_succ    : successor map on the DAG
      node_to_meta: original-node-id → meta-node-id  (identity for singletons)
      cyclic_sccs : SCCs with >1 member
    """
    node_ids  = list(g.nodes.keys())
    sccs      = tarjan_scc(node_ids, g.succ)
    cyclic    = [s for s in sccs if len(s) > 1]

    node_to_meta: Dict[str, str] = {}
    meta_labels:  Dict[str, str] = {}
    meta_parts:   Dict[str, str] = {}

    for scc in sccs:
        if len(scc) == 1:
            node_to_meta[scc[0]] = scc[0]
        else:
            # Create a meta-node id
            meta_id = "SCC_" + "_".join(sorted(scc))
            # Label = joined task labels
            label = " [LOOP: " + " / ".join(
                g.nodes[n]["label"] for n in scc if g.nodes[n]["kind"] == "task"
            ) + "]"
            meta_labels[meta_id] = label
            meta_parts[meta_id]  = g.node_to_part.get(scc[0], "unknown")
            for n in scc:
                node_to_meta[n] = meta_id
            # Register in g.nodes so later stages can find it
            g.nodes[meta_id] = {
                "label":        label,
                "kind":         "meta_loop",
                "gateway_type": None,
                "process_id":   g.nodes[scc[0]]["process_id"],
                "participant":  meta_parts[meta_id],
                "scc_members":  scc,
            }

    # Build DAG successor map
    dag_succ: Dict[str, List[str]] = defaultdict(list)
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
# 3.  DOMINANCE TREE  (simple iterative algorithm)
# ─────────────────────────────────────────────────────────────────────────────

def build_dominance_tree(start: str,
                         dag_nodes: List[str],
                         dag_succ:  Dict[str, List[str]]
                         ) -> Dict[str, Optional[str]]:
    """
    Returns idom[n] = immediate dominator of n (None for start node).
    Uses the simple O(n²) iterative bit-vector algorithm — sufficient for
    the sizes of BPMN models encountered in practice.
    """
    # Topological order (BFS from start)
    topo: List[str] = []
    visited: Set[str] = set()
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

    # Build pred map restricted to reachable nodes
    dag_pred: Dict[str, List[str]] = defaultdict(list)
    for v in topo:
        for w in dag_succ.get(v, []):
            if w in visited:
                dag_pred[w].append(v)

    # Dominator sets as frozensets (iterative fixed-point)
    all_nodes = set(topo)
    dom: Dict[str, Set[str]] = {}
    dom[start] = {start}
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

    # Derive immediate dominator
    idom: Dict[str, Optional[str]] = {start: None}
    for n in topo[1:]:
        doms_of_n = dom[n] - {n}
        # idom = the dominator of n that is dominated by all other dominators of n
        idom_n = None
        for d in doms_of_n:
            if doms_of_n - {d} <= dom.get(d, set()):
                idom_n = d
                break
        idom[n] = idom_n

    return idom


# ─────────────────────────────────────────────────────────────────────────────
# 4.  DEONTIC CLASSIFICATION VIA REACHABILITY
# ─────────────────────────────────────────────────────────────────────────────

def _bfs_reachable(start: str,
                   succ:  Dict[str, List[str]],
                   exclude: Optional[str] = None) -> Set[str]:
    """
    BFS from start, optionally excluding one node.
    Returns the set of all reachable nodes.
    """
    visited: Set[str] = set()
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


def _branch_subgraph(split_gw:  str,
                     branch_tgt: str,
                     merge_gw:   str,
                     dag_succ:   Dict[str, List[str]]) -> Dict[str, List[str]]:
    """
    Return the subgraph containing only nodes reachable from branch_tgt
    that can reach merge_gw (or an end node if no merge exists), without
    crossing back through split_gw.  Used for branch-local reachability.
    """
    # Forward BFS from branch_tgt, stopping at merge_gw boundary
    fwd: Set[str] = set()
    q = deque([branch_tgt])
    while q:
        v = q.popleft()
        if v in fwd:
            continue
        fwd.add(v)
        if v == merge_gw:
            continue          # don't go past the merge point
        for w in dag_succ.get(v, []):
            q.append(w)

    # Build restricted successor map
    sub: Dict[str, List[str]] = {}
    for v in fwd:
        sub[v] = [w for w in dag_succ.get(v, []) if w in fwd]
    return sub


def _find_merge_gateway(split_gw:   str,
                        dag_succ:   Dict[str, List[str]],
                        all_nodes:  Set[str]) -> Optional[str]:
    """
    Find the immediate post-dominator of split_gw — the node where all
    branches of the split converge.  Uses a simple heuristic: the first
    node reachable from ALL direct successors of split_gw via BFS.
    Returns None if no merge point is found (branches go to end directly).
    """
    branches = dag_succ.get(split_gw, [])
    if len(branches) < 2:
        return None

    # For each branch, collect all reachable nodes
    reachable_per_branch = []
    for b in branches:
        reachable_per_branch.append(_bfs_reachable(b, dag_succ))

    # Merge node = node reachable from all branches (intersection)
    common = reachable_per_branch[0].copy()
    for r in reachable_per_branch[1:]:
        common &= r

    if not common:
        return None

    # Pick the closest one (minimum BFS distance from split_gw)
    dist: Dict[str, int] = {}
    q2: deque = deque([(split_gw, 0)])
    visited2: Set[str] = set()
    while q2:
        v, d = q2.popleft()
        if v in visited2:
            continue
        visited2.add(v)
        dist[v] = d
        for w in dag_succ.get(v, []):
            q2.append((w, d + 1))

    return min(common, key=lambda n: dist.get(n, 999999))


def classify_deontic_type(start:     str,
                          dag_nodes: List[str],
                          dag_succ:  Dict[str, List[str]],
                          g:         "BPMNGraph",
                          ) -> Dict[str, str]:
    """
    Returns a dict mapping node-id → deontic type string:
      "Duty"               — mandatory on every global path, unconditional
      "ConstrainedDuty"    — mandatory within its branch, but branch is optional
      "Permission"         — skippable, no gateway condition guards it
      "ConstrainedPermission" — skippable AND guarded by a gateway condition

    Two-level algorithm
    -------------------
    LEVEL 1 — Global reachability (one BFS per node):
      Remove node T from full graph.
      If no end node reachable from start → T is globally mandatory → Duty candidate.
      Else → T is globally optional → Permission candidate.

    LEVEL 2 — Branch-local reachability (for globally optional nodes only):
      For each XOR/EVENT gateway G with branches B1..Bn:
        Extract subgraph of each branch Bi (nodes between G and its merge point).
        For each task T in that subgraph:
          Remove T from the branch subgraph.
          If no end of branch reachable → T is branch-mandatory → ConstrainedDuty.
          Else → T is branch-optional → ConstrainedPermission (if has conditions)
                                         or Permission (if unconditional).

    Complexity: O(V x (V+E)) for level 1 + O(B x V x (V+E)) for level 2
    where B = number of XOR branches.  Fine for BPMN sizes.
    """
    all_reachable = _bfs_reachable(start, dag_succ)
    end_nodes = {v for v in all_reachable if not dag_succ.get(v)}

    # Default: everything starts as Permission
    deontic: Dict[str, str] = {v: "Permission" for v in all_reachable}

    if not end_nodes:
        return deontic

    # ── Level 1: global reachability ──────────────────────────────────────
    globally_mandatory: Set[str] = set()
    for node in all_reachable:
        reachable_without = _bfs_reachable(start, dag_succ, exclude=node)
        if not any(e in reachable_without for e in end_nodes):
            globally_mandatory.add(node)
            deontic[node] = "Duty"

    # ── Level 2: branch-local reachability ───────────────────────────────
    # Only examine nodes not already classified as globally mandatory
    globally_optional = all_reachable - globally_mandatory

    # Find all XOR and EVENT split gateways (those with 2+ outgoing edges)
    split_gateways = [
        nid for nid in all_reachable
        if g.nodes.get(nid, {}).get("kind") == "gateway"
        and g.nodes.get(nid, {}).get("gateway_type") in ("XOR", "EVENT")
        and len(dag_succ.get(nid, [])) >= 2
    ]

    for gw in split_gateways:
        merge = _find_merge_gateway(gw, dag_succ, all_reachable)

        for branch_entry in dag_succ.get(gw, []):
            # Build the subgraph for this branch
            sub = _branch_subgraph(gw, branch_entry, merge or "", dag_succ)
            branch_nodes = set(sub.keys())
            branch_ends = {v for v in branch_nodes if not sub.get(v)}

            if not branch_ends:
                continue

            for node in branch_nodes & globally_optional:
                if g.nodes.get(node, {}).get("kind") != "task":
                    continue
                # Branch-local reachability: remove node from branch subgraph
                sub_without = {
                    v: [w for w in succs if w != node]
                    for v, succs in sub.items()
                    if v != node
                }
                # BFS within branch subgraph excluding node
                reachable_in_branch = _bfs_reachable(
                    branch_entry, sub_without, exclude=node
                )
                branch_end_reachable = any(
                    e in reachable_in_branch for e in branch_ends
                )
                # Count task nodes in this branch (excluding gateways/events)
                task_nodes_in_branch = [
                    n for n in branch_nodes
                    if g.nodes.get(n, {}).get("kind") == "task"
                ]
                if not branch_end_reachable and len(task_nodes_in_branch) > 1:
                    # Mandatory within a multi-task branch → ConstrainedDuty
                    # (cannot be skipped once the branch is entered, and there
                    #  are other tasks in the branch that provide context)
                    deontic[node] = "ConstrainedDuty"
                # else: single-task branch or skippable within branch
                # → remains Permission; ConstrainedPermission assigned in stage 5

    return deontic


# ─────────────────────────────────────────────────────────────────────────────
# 5.  ROLE-PARTITIONED DFS  (party assignment + inter-party duties)
# ─────────────────────────────────────────────────────────────────────────────

def _simplify_conditions(raw_conds: Optional[List[str]], g: BPMNGraph) -> List[str]:
    """
    Given the union of conditions accumulated across all paths to a task,
    remove any gateway whose ALL outgoing branches are represented — that
    gateway is fully covered, so its conditions cancel out to unconditional.

    E.g. {"score received? = no", "score received? = yes"} → []  (unconditional)
         {"score available? = no"}                          → ["score available? = no"]
    """
    if raw_conds is None:
        return []   # already known unconditional

    # Group conditions by gateway label (the prefix before " = ")
    # Event-Based gateway conditions use format "event: <catch event name>"
    # and are grouped under the sentinel key "event" for cancellation purposes.
    from collections import defaultdict as _dd
    by_gateway: Dict[str, Set[str]] = _dd(set)
    ungrouped: List[str] = []
    for c in raw_conds:
        if " = " in c:
            gw, branch = c.split(" = ", 1)
            by_gateway[gw.strip()].add(branch.strip())
        elif c.startswith("event: "):
            # Group all event-branch conditions under their gateway node
            # Key = "EVENT:<gateway_id>" found by matching catch event names
            by_gateway["__event__"].add(c)
        else:
            ungrouped.append(c)

    # For XOR/AND/OR gateways: count outgoing branches by gateway label
    gw_total_branches: Dict[str, int] = {}
    event_gw_branch_counts: Dict[str, int] = {}   # gateway_id → branch count
    for nid, nd in g.nodes.items():
        lbl = nd.get("label", "").strip()
        gtype = nd.get("gateway_type", "")
        if nd.get("kind") == "gateway":
            if gtype == "EVENT":
                event_gw_branch_counts[nid] = len(g.succ.get(nid, []))
            elif lbl:
                gw_total_branches[lbl] = len(g.succ.get(nid, []))

    surviving: List[str] = list(ungrouped)

    # Cancel XOR/AND/OR conditions when all branches covered
    for gw_label, seen_branches in by_gateway.items():
        if gw_label == "__event__":
            continue
        total = gw_total_branches.get(gw_label, len(seen_branches) + 1)
        if len(seen_branches) >= total:
            pass   # all branches covered → unconditional
        else:
            for branch in sorted(seen_branches):
                surviving.append(f"{gw_label} = {branch}")

    # Cancel Event-Based conditions when all branches of any EVENT gateway covered
    if "__event__" in by_gateway:
        event_conds = by_gateway["__event__"]   # set of "event: <name>" strings
        # Find which EVENT gateway owns these catch events
        # by checking whether all successors of that gateway are represented
        total_event_branches = max(event_gw_branch_counts.values(), default=0)
        if len(event_conds) >= total_event_branches and total_event_branches > 0:
            pass   # all event branches covered → unconditional
        else:
            surviving.extend(sorted(event_conds))

    return sorted(surviving)


def role_partitioned_dfs(
    start:        str,
    g:            BPMNGraph,
    dag_succ:     Dict[str, List[str]],
    node_to_meta: Dict[str, str],
    deontic_map:  Dict[str, str],
    idom:         Dict[str, Optional[str]],
    verbose:      bool = False,
) -> List[dict]:
    """
    Path-aware DFS over the DAG.

    Key design decisions
    --------------------
    * ONE rule per task — all paths that reach a task are explored; the
      conditions from every path are UNIONED into a single rule's constraint
      list.  A task reachable unconditionally on at least one path gets an
      empty constraint list (unconditional).
    * Condition strings are qualified with their gateway label:
        "score available? = no"   not just "no"
    * The visited set is replaced by a per-node accumulated-conditions dict
      so every path is walked, but we stop re-entering a node once we have
      already seen the exact same condition set from the same direction
      (cycle guard for the DAG case).

    Each returned dict has:
      uid         : unique rule identifier (URI fragment)
      type        : "Duty" | "Permission" | "Prohibition"
      action      : natural-language label of the task
      assignee    : participant name
      precondition: list of rule-uids (from dominance tree)
      constraints : list of qualified condition strings, empty = unconditional
      inter_party : cross-lane notify / waitFor duties
    """

    # ── message-flow lookups ──────────────────────────────────────────────
    msg_out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    msg_in:  Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for (src, tgt, fid) in g.msg_flows:
        ms = node_to_meta.get(src, src)
        mt = node_to_meta.get(tgt, tgt)
        if ms in g.nodes and mt in g.nodes:
            msg_out[ms].append((mt, fid))
            msg_in[mt].append((ms, fid))

    # ── condition map: (meta_src, meta_tgt) → qualified condition string ─
    # For XOR/AND/OR gateways: "gateway label = branch label"
    #   e.g. "score available? = no"
    # For Event-Based gateways: the branch has no text label — the condition
    #   is the name of the intermediate catch event the branch leads to,
    #   qualified as "event: <event name>"
    #   e.g. "event: delay information received"
    cond_map: Dict[Tuple[str, str], str] = {}
    for (src, tgt, branch_label) in g.seq_edges:
        ms = node_to_meta.get(src, src)
        mt = node_to_meta.get(tgt, tgt)
        src_nd  = g.nodes.get(ms, {})
        gw_type = src_nd.get("gateway_type", "")
        gw_label = src_nd.get("label", "").strip()

        if gw_type == "EVENT":
            # Condition = name of the catch event this branch leads to
            tgt_nd   = g.nodes.get(mt, {})
            tgt_kind = tgt_nd.get("kind", "")
            tgt_lbl  = tgt_nd.get("label", "").strip()
            if tgt_kind == "event" and tgt_lbl:
                cond_map[(ms, mt)] = f"event: {tgt_lbl}"
            elif branch_label:
                cond_map[(ms, mt)] = branch_label
        elif branch_label:
            if gw_label:
                cond_map[(ms, mt)] = f"{gw_label} = {branch_label}"
            else:
                cond_map[(ms, mt)] = branch_label

    # ── per-node accumulated conditions ──────────────────────────────────
    # node_id → set of frozensets of conditions seen so far on arriving paths.
    # A frozenset() means "arrived unconditionally on this path".
    # We stop recursing into a node if we have already processed the exact
    # same frozenset of conditions for it (prevents infinite loops / redundant
    # work on DAGs with converging paths).
    arrived: Dict[str, Set[frozenset]] = defaultdict(set)

    # Accumulator: node_id → union of all condition sets across all paths.
    # None means "at least one unconditional path exists → unconditional".
    node_conditions: Dict[str, Optional[List[str]]] = {}

    # Emission order (topological)
    emit_order: List[str] = []

    # uid_map needed for precondition lookup (idom)
    uid_map: Dict[str, str] = {}

    def dfs(v: str, path_conds: frozenset):
        """
        path_conds: frozenset of qualified condition strings active on this
                    path from the start node to v.
        """
        # Cycle / redundant-path guard
        if path_conds in arrived[v]:
            return
        arrived[v].add(path_conds)

        nd      = g.nodes.get(v, {})
        kind    = nd.get("kind", "unknown")

        # ── Gateways: propagate qualified conditions, emit nothing ────────
        if kind == "gateway":
            for w in dag_succ.get(v, []):
                edge_cond = cond_map.get((v, w), "")
                if edge_cond:
                    new_conds = frozenset(path_conds | {edge_cond})
                else:
                    new_conds = path_conds          # merge gateway — no new cond
                dfs(w, new_conds)
            return

        # ── Start / end / intermediate events: pass through ──────────────
        if kind in ("startEvent", "endEvent", "event"):
            for w in dag_succ.get(v, []):
                dfs(w, path_conds)
            return

        # ── Task / meta_loop: accumulate conditions, then recurse ─────────
        if v not in node_conditions:
            # First time we reach this node
            node_conditions[v] = None if not path_conds else list(path_conds)
            emit_order.append(v)
        else:
            current = node_conditions[v]
            if current is None:
                # Already unconditional — stays unconditional
                pass
            elif not path_conds:
                # New unconditional path found — mark unconditional
                node_conditions[v] = None
            else:
                # Union: add any new conditions not already present
                existing = set(current)
                for c in path_conds:
                    if c not in existing:
                        current.append(c)
                        existing.add(c)

        # Recurse — passing path_conds unchanged (condition applies to all
        # downstream tasks on this path until a merge clears it)
        for w in dag_succ.get(v, []):
            dfs(w, path_conds)

    dfs(start, frozenset())

    # ── Build rules in emission order ────────────────────────────────────
    rules: List[dict] = []

    for v in emit_order:
        nd      = g.nodes.get(v, {})
        label   = nd.get("label", v)
        part_id = nd.get("participant", "unknown")
        part_nm = g.participants.get(part_id, {}).get("name", part_id)

        # Base type from deontic_map; upgrade Permission→ConstrainedPermission
        # if the node has non-empty conditions after simplification
        base_type = deontic_map.get(v, "Permission")
        rule_type = base_type   # will be refined below after constraints computed
        uid       = slugify(label) + "_" + v[-6:]
        uid_map[v] = uid

        # Precondition from immediate dominator
        idom_v  = idom.get(v)
        pre_uid = uid_map.get(idom_v) if idom_v else None

        # Conditions: None → unconditional (empty list), else sorted list
        # Simplification: if every branch of any gateway is covered,
        # that gateway's conditions cancel out → unconditional.
        raw_conds = node_conditions.get(v)
        constraints = _simplify_conditions(raw_conds, g)

        # Refine Permission → ConstrainedPermission if conditions exist
        if rule_type == "Permission" and constraints:
            rule_type = "ConstrainedPermission"

        # Inter-party message-flow duties
        inter: List[dict] = []
        for (mt, fid) in msg_out.get(v, []):
            tgt_pid = g.nodes.get(mt, {}).get("participant", "unknown")
            tgt_nm  = g.participants.get(tgt_pid, {}).get("name", tgt_pid)
            inter.append({"duty_type": "notify",  "other_party": tgt_nm,
                          "trigger": "onCompletion", "flow_id": fid})
        for (ms, fid) in msg_in.get(v, []):
            src_pid = g.nodes.get(ms, {}).get("participant", "unknown")
            src_nm  = g.participants.get(src_pid, {}).get("name", src_pid)
            inter.append({"duty_type": "waitFor", "other_party": src_nm,
                          "trigger": "onReceipt",  "flow_id": fid})

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
# 6a. IMPLICIT PROHIBITIONS  (compact: one rule per role, not one per task)
# ─────────────────────────────────────────────────────────────────────────────

def build_prohibitions(rules: List[dict], g: BPMNGraph) -> List[dict]:
    """
    For each participant P, emit ONE odrl:Prohibition covering all tasks
    that belong to other participants.

    This uses the closed-world assumption: if a role has no Duty or
    Permission for a task, it is implicitly prohibited from performing it.
    Rather than MxN individual prohibitions (M roles × N tasks), we emit
    M prohibitions, each carrying a list of forbidden action labels.

    Returns a list of prohibition dicts (same shape as task rules, but
    type="Prohibition" and constraints/precondition are empty).
    """
    # Collect tasks assigned to each participant, from the rules already emitted
    tasks_by_party: Dict[str, Set[str]] = defaultdict(set)
    for r in rules:
        tasks_by_party[r["assignee"]].add(r["action"])

    all_parties = set(tasks_by_party.keys())
    prohibitions: List[dict] = []

    for party in sorted(all_parties):
        own_tasks = tasks_by_party[party]
        forbidden = set()
        for other_party, other_tasks in tasks_by_party.items():
            if other_party != party:
                forbidden |= other_tasks - own_tasks   # tasks the other party owns that this party doesn't

        if not forbidden:
            continue

        uid = slugify(party) + "_prohibited"
        prohibitions.append({
            "uid":          uid,
            "type":         "Prohibition",
            "action":       sorted(forbidden),   # list of labels
            "assignee":     party,
            "precondition": [],
            "constraints":  [],
            "inter_party":  [],
        })

    return prohibitions


# ─────────────────────────────────────────────────────────────────────────────
# 6.  ODRL JSON-LD EMISSION
# ─────────────────────────────────────────────────────────────────────────────

ODRL_CONTEXT = {
    "odrl":  "http://www.w3.org/ns/odrl/2/",
    "rdf":   "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs":  "http://www.w3.org/2000/01/rdf-schema#",
    "xsd":   "http://www.w3.org/2001/XMLSchema#",
    "bpmn":  "http://bpmn.io/schema/bpmn#",
    "ex":    "http://example.org/policy/",
}

ODRL_TYPE_MAP = {
    "Duty":                 "odrl:Duty",
    "ConstrainedDuty":      "odrl:Duty",
    "Permission":           "odrl:Permission",
    "ConstrainedPermission":"odrl:Permission",
    "Prohibition":          "odrl:Prohibition",
}


def emit_odrl(rules: List[dict],
              g: BPMNGraph,
              process_label: str = "CreditScoring") -> dict:
    """
    Build an ODRL Set Policy as a JSON-LD document.

    Structure
    ---------
    {
      "@context": ...,
      "@type":    "odrl:Set",
      "@id":      "ex:CreditScoringPolicy",
      "odrl:profile": "bpmn:BPMNDeonticProfile",
      "odrl:permission": [...],
      "odrl:prohibition": [...],
      "odrl:obligation": [...]        ← duties in ODRL vocab are 'obligation'
    }
    """
    permissions:  List[dict] = []
    obligations:  List[dict] = []
    prohibitions: List[dict] = []

    for r in rules:
        assignee_uri = "ex:" + slugify(r["assignee"])

        # Build constraint list
        constraints = []
        for cond in r["constraints"]:
            constraints.append({
                "@type":             "odrl:Constraint",
                "odrl:leftOperand":  {"@id": "bpmn:gatewayCondition"},
                "odrl:operator":     {"@id": "odrl:eq"},
                "odrl:rightOperand": cond,
            })

        # Precondition chain as odrl:duty (nested inside permission) or
        # as a separate duty reference
        duties = []
        for pre in r["precondition"]:
            duties.append({
                "@type":   "odrl:Duty",
                "@id":     "ex:" + pre,
                "rdfs:comment": f"Must complete '{pre}' before this action.",
            })

        # Inter-party duties / constraints
        for ip in r["inter_party"]:
            if ip["duty_type"] == "notify":
                duties.append({
                    "@type":           "odrl:Duty",
                    "odrl:action":     {"@id": "odrl:inform"},
                    "odrl:assignee":   {"@id": "ex:" + slugify(ip["other_party"])},
                    "rdfs:comment":    f"Notify '{ip['other_party']}' on completion.",
                })
            elif ip["duty_type"] == "waitFor":
                constraints.append({
                    "@type":             "odrl:Constraint",
                    "odrl:leftOperand":  {"@id": "bpmn:messageReceived"},
                    "odrl:operator":     {"@id": "odrl:eq"},
                    "odrl:rightOperand": f"messageFrom:{slugify(ip['other_party'])}",
                    "rdfs:comment":      f"Wait for message from '{ip['other_party']}'.",
                })

        rule_obj: dict = {
            "@type":       ODRL_TYPE_MAP.get(r["type"], "odrl:Permission"),
            "@id":         "ex:" + r["uid"],
            "odrl:action": {
                "@id":        "bpmn:perform",
                "rdfs:label": r["action"],
            },
            "odrl:assignee": {"@id": assignee_uri},
        }

        if constraints:
            rule_obj["odrl:constraint"] = constraints
        if duties:
            rule_obj["odrl:duty"] = duties

        if r["type"] in ("Duty", "ConstrainedDuty"):
            obligations.append(rule_obj)
        elif r["type"] in ("Permission", "ConstrainedPermission"):
            permissions.append(rule_obj)
        # Prohibitions are built separately by build_prohibitions()

    # Build compact implicit prohibitions (M rules, not MxN)
    prohibition_rules = build_prohibitions(rules, g)
    for pr in prohibition_rules:
        assignee_uri = "ex:" + slugify(pr["assignee"])
        # Each action in the list becomes a separate action entry
        prohibitions.append({
            "@type":       "odrl:Prohibition",
            "@id":         "ex:" + pr["uid"],
            "rdfs:comment": f"Implicit prohibition: {pr['assignee']} may not perform tasks owned by other parties.",
            "odrl:assignee": {"@id": assignee_uri},
            "odrl:action": [
                {"@id": "bpmn:perform", "rdfs:label": a}
                for a in pr["action"]
            ],
        })

    policy = {
        "@context":      ODRL_CONTEXT,
        "@type":         "odrl:Set",
        "@id":           f"ex:{slugify(process_label)}Policy",
        "rdfs:label":    f"{process_label} BPMN Policy",
        "odrl:profile":  {"@id": "bpmn:BPMNDeonticProfile"},
    }

    if permissions:
        policy["odrl:permission"] = permissions
    if obligations:
        policy["odrl:obligation"] = obligations
    if prohibitions:
        policy["odrl:prohibition"] = prohibitions

    # Add party declarations
    parties = list({r["assignee"] for r in rules})
    policy["odrl:parties"] = [
        {
            "@type":     "odrl:Party",
            "@id":       "ex:" + slugify(p),
            "rdfs:label": p,
        }
        for p in sorted(parties)
    ]

    return policy, prohibition_rules


# ─────────────────────────────────────────────────────────────────────────────
# 7.  HUMAN-READABLE SUMMARY  (optional)
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(rules: List[dict], g: BPMNGraph):
    print("\n" + "═" * 72)
    print("  ODRL POLICY SUMMARY")
    print("═" * 72)

    by_party: Dict[str, List[dict]] = defaultdict(list)
    for r in rules:
        by_party[r["assignee"]].append(r)

    for party, prules in sorted(by_party.items()):
        print(f"\n  Party: {party}")
        print("  " + "─" * 60)
        for r in prules:
            conds = ", ".join(r["constraints"]) if r["constraints"] else "(unconditional)"
            pre   = ", ".join(r["precondition"]) if r["precondition"] else "—"
            print(f"    [{r['type']:10s}]  {r['action']}")
            print(f"               conditions  : {conds}")
            print(f"               after       : {pre}")
            for ip in r["inter_party"]:
                print(f"               {ip['duty_type']:10s}: {ip['other_party']}")

    duties   = [r for r in rules if r["type"] == "Duty"]
    cduts    = [r for r in rules if r["type"] == "ConstrainedDuty"]
    perms    = [r for r in rules if r["type"] == "Permission"]
    cperms   = [r for r in rules if r["type"] == "ConstrainedPermission"]
    print(f"\n  Totals: {len(duties)} Duties | "
          f"{len(cduts)} Constrained Duties | "
          f"{len(perms)} Permissions | "
          f"{len(cperms)} Constrained Permissions")
    print("═" * 72 + "\n")


def print_prohibition_summary(prohibition_rules: List[dict]):
    print("  IMPLICIT PROHIBITIONS  (one per role)")
    print("  " + "─" * 60)
    for pr in prohibition_rules:
        print(f"\n  Party: {pr['assignee']} may NOT perform:")
        for a in pr["action"]:
            print(f"    ✗  {a}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "node"


def run_pipeline(xml_path: str,
                 output_path: str,
                 verbose: bool = False):

    print(f"[1/6] Parsing BPMN XML: {xml_path}")
    g = BPMNGraph.from_xml(xml_path, verbose=verbose)

    if not g.nodes:
        sys.exit("ERROR: No nodes found in BPMN file.")

    print(f"[2/6] Tarjan SCC decomposition (cycle detection / DAG normalisation)")
    dag_nodes, dag_succ, node_to_meta, cyclic_sccs = build_dag(g, verbose=verbose)

    if cyclic_sccs:
        print(f"      ⚠  {len(cyclic_sccs)} cycle(s) detected and collapsed to meta-nodes.")
    else:
        print(f"      ✓  No cycles — graph is already a DAG.")

    # Collect all start meta-nodes (one per pool/process)
    dag_pred_cnt: Dict[str, int] = defaultdict(int)
    for v in dag_nodes:
        for w in dag_succ.get(v, []):
            dag_pred_cnt[w] += 1
    dag_roots = [v for v in dag_nodes if dag_pred_cnt[v] == 0]

    # Map BPMN startEvent nodes to their meta-ids
    start_metas = []
    for sn in g.start_nodes:
        meta = node_to_meta.get(sn, sn)
        if meta in dag_nodes and meta not in start_metas:
            start_metas.append(meta)
    # Add any remaining roots not already covered
    for r in dag_roots:
        if r not in start_metas:
            start_metas.append(r)

    if verbose:
        labels = [g.nodes.get(s, {}).get("label", s) for s in start_metas]
        print(f"      Start nodes: {labels}")

    # Run dominance tree and deontic classification per root, then merge
    all_idom:    Dict[str, Optional[str]] = {}
    all_deontic: Dict[str, str] = {}
    for sm in start_metas:
        all_idom.update(build_dominance_tree(sm, dag_nodes, dag_succ))
        all_deontic.update(classify_deontic_type(sm, dag_nodes, dag_succ, g))

    if verbose:
        for dtype in ("Duty", "ConstrainedDuty", "Permission"):
            tasks = [g.nodes[n]["label"] for n in all_deontic
                     if all_deontic[n] == dtype
                     and g.nodes.get(n, {}).get("kind") == "task"]
            if tasks:
                print(f"      {dtype:25s}: {tasks}")

    counts = {t: sum(1 for n, dt in all_deontic.items()
                     if dt == t and g.nodes.get(n, {}).get("kind") == "task")
              for t in ("Duty", "ConstrainedDuty", "Permission")}
    print(f"[3/6] Dominance-tree construction ({len(start_metas)} root(s))")
    print(f"[4/6] Deontic classification — "
          f"{counts['Duty']} Duties, "
          f"{counts['ConstrainedDuty']} ConstrainedDuties, "
          f"{counts['Permission']}+ Permissions (ConstrainedPermission assigned in stage 5)")

    print(f"[5/6] Role-partitioned DFS (party assignment + inter-party duties)")
    all_rules: List[dict] = []
    for sm in start_metas:
        rules = role_partitioned_dfs(
            sm, g, dag_succ, node_to_meta, all_deontic, all_idom, verbose=verbose
        )
        all_rules.extend(rules)

    print(f"[6/6] Emitting ODRL JSON-LD → {output_path}")
    policy, prohibition_rules = emit_odrl(all_rules, g, process_label="CreditScoring")

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, indent=2, ensure_ascii=False)

    print_summary(all_rules, g)
    print_prohibition_summary(prohibition_rules)
    print(f"Done. ODRL policy written to: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert BPMN XML to ODRL JSON-LD using graph-traversal pipeline."
    )
    parser.add_argument("bpmn_file",  help="Path to input .bpmn / .xml file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output .jsonld file (default: <input>.odrl.jsonld)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print detailed per-node trace")
    args = parser.parse_args()

    out = args.output or args.bpmn_file.rsplit(".", 1)[0] + ".odrl.jsonld"
    run_pipeline(args.bpmn_file, out, verbose=args.verbose)


if __name__ == "__main__":
    main()
