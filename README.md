# BPMN → ODRL Policy Extraction

## Viewing BPMN Diagrams

Use **https://demo.bpmn.io/** — drag and drop any `.bpmn` file directly into the browser. No install required.

---

## Running the Code

```bash
python3 bpmn2odrl4.py credit-scoring-asynchronous.bpmn -o policy_credit_async_5.jsonld --verbose
```

Generates an ODRL JSON-LD policy file from any BPMN XML input.

**General usage:**
```bash
python3 bpmn2odrl4.py <input.bpmn> -o <output.jsonld> [--verbose]
```

---

## What the Code Produces

An **ODRL Set Policy** in JSON-LD with three rule types per participant:

| Rule type | Meaning |
|---|---|
| `odrl:obligation` (Duty) | Task that must be performed — no complete path through the process bypasses it |
| `odrl:permission` (Permission) | Task that may be performed — at least one complete path skips it |
| `odrl:prohibition` (Prohibition) | Tasks a role is forbidden from performing — they belong exclusively to another pool |

---

## Work Pipeline

The code runs six stages in sequence.

### Stage 1 — Parse BPMN XML
Reads the XML and builds an in-memory directed graph.
Extracts: tasks, gateways, events, sequence flows (with condition labels), message flows, participants, and swim-lane assignments.

### Stage 2 — Tarjan SCC + DAG Root Discovery

**Tarjan SCC (iterative DFS):** Detects cycles in the BPMN graph. Each cycle is collapsed into a single meta-node, guaranteeing the output is a DAG. ODRL has no loop construct so cycles must be resolved before policy extraction.

**In-degree scan:** Scans all edges to find nodes with no incoming edges. These are the entry points — one per pool/process.

### Stage 3 — Dominance Tree (BFS + fixed-point iteration)
Determines which tasks are structural preconditions for other tasks. Node A *dominates* node B if every path from the start to B passes through A. This produces the `after:` field in the output — a task cannot be reached without its dominator having been performed first.

### Stage 4 — Deontic Classification (BFS per node)
Classifies each task as **Duty** or **Permission** using a reachability check:

> For each task T: temporarily remove T from the graph and run BFS from start.
> - If no end node is reachable → T is on every path → **Duty**
> - If any end node is still reachable → T can be skipped → **Permission**

This is semantically correct: it directly asks "can the process complete without this task?" rather than using an approximate heuristic like longest path.

### Stage 5 — Role-Partitioned DFS (path-aware condition accumulation)
Traverses every execution path through each pool, accumulating gateway conditions per task.

**Key design decisions:**
- One rule per task — conditions from all paths are **unioned**
- A task reachable unconditionally on any path → unconditional overall
- XOR gateway conditions are qualified: `"score available? = no"` not just `"no"`
- Event-Based gateway conditions use the catch event name: `"event: delay information received"`
- When all branches of a gateway are represented in the union, they cancel out to unconditional

**Implicit prohibitions** are then derived: for each participant, one compact `odrl:Prohibition` lists all tasks belonging exclusively to other participants. This produces **M prohibitions** (one per role), not M×N individual rules.

### Stage 6 — ODRL JSON-LD Emission
Assembles the final policy document:
- One `odrl:Set` policy per BPMN collaboration
- Obligations, permissions, and prohibitions per participant
- Gateway conditions as `odrl:constraint` with `bpmn:gatewayCondition` operand
- Message flows as `odrl:duty inform` (outgoing) and `bpmn:messageReceived` constraints (incoming)
- Party declarations for all participants

---

## Graph Traversals Used

| Algorithm | Stage | Purpose |
|---|---|---|
| Iterative DFS (Tarjan SCC) | 2 | Detect and collapse cycles → DAG |
| Edge scan (in-degree) | 2 | Find entry point per pool |
| BFS + fixed-point iteration | 3 | Dominance tree → `after:` preconditions |
| BFS per node (with exclusion) | 4 | Duty vs Permission classification |
| DFS (path-aware, condition union) | 5 | Qualified conditions + role assignment |

---

## Example Output (Asynchronous Credit Scoring)

```
Party: credit scoring (bank)
  [Duty      ]  request credit score          conditions: (unconditional)
  [Permission]  report delay                  conditions: event: delay information received
  [Duty      ]  send credit score             conditions: (unconditional)

Party: scoring service
  [Duty      ]  compute credit score (level 1)  conditions: (unconditional)
  [Permission]  report delay                    conditions: score available? = no
  [Permission]  compute credit score (level 2)  conditions: score available? = no
  [Permission]  send credit score               conditions: score available? = no
  [Permission]  send credit score               conditions: score available? = yes

Implicit prohibitions:
  credit scoring (bank)  may NOT perform: compute credit score (level 1), compute credit score (level 2)
  scoring service        may NOT perform: request credit score
```

---

## Novelty

This pipeline is the first published approach to combine:
- BPMN graph traversal for deontic norm extraction
- ODRL as the policy target (direct structural correspondence via Natschläger's deontic BPMN result)
- Reachability-based Duty/Permission classification (vs. prior longest-path heuristics)
- Compact implicit prohibition generation

Closest related work: Annane et al. (2019) BBO: BPMN 2.0 Based Ontology for Business Process Representation; Roy et al. (2018) Modeling industrial business processes for querying and retrieving using OWL+SWRL; Skersys et al. (2022) Transforming BPMN Processes to SBVR Process Rules with Deontic Modalities; Kowalski & Datoo (2022) Logical English meets legal English for swaps and derivatives; Natschläger (2021) Deontic BPMN.
