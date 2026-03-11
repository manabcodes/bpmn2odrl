# BPMN → ODRL Policy Extraction

## Viewing BPMN Diagrams

Use **https://demo.bpmn.io/** — drag and drop any `.bpmn` file directly into the browser. No install required.

---

## Running the Code

```bash
python3 bpmn2odrl8.py credit-scoring-asynchronous.bpmn -o policy_credit_async.jsonld --verbose
```

Generates an ODRL JSON-LD policy file from any BPMN XML input.

**General usage:**
```bash
python3 bpmn2odrl8.py <input.bpmn> -o <output.jsonld> [--verbose]
```

---

## What the Code Produces

An **ODRL Set Policy** in JSON-LD with four rule types per participant:

| Rule type | ODRL emission | Meaning |
|---|---|---|
| `Duty` | `odrl:obligation` | Task on every path — no complete execution bypasses it |
| `ConstrainedDuty` | `odrl:obligation` | Mandatory within a branch, but the branch itself is optional |
| `Permission` / `ConstrainedPermission` | `odrl:permission` | Task that may be performed — at least one complete path skips it |
| `WaitProhibition` | `odrl:prohibition` | Party cannot proceed past an intermediate catch event until that event fires |
| Role prohibition | `odrl:prohibition` | Tasks belonging exclusively to another participant — one compact prohibition per role |

---

## Theoretical Grounding

BPMN is a deontic specification. Every well-formed BPMN model encodes obligations, permissions, and prohibitions as a structural consequence of its control flow (Natschläger 2011). This pipeline extracts that normative content automatically.

### Gateway → deontic modality mapping

| Gateway type | Deontic reading |
|---|---|
| XOR split | Disjunctive permission — one branch taken, participant has a choice |
| AND split | Conjunctive obligation — all branches taken, no choice exists |
| EVENT split | Disjunctive permission conditioned on which external event fires |
| OR (inclusive) split | Open problem — not handled in current pipeline |

**Note on AND gateways:** Natschläger (2011) reads AND splits as permissions. This pipeline disagrees. A parallel gateway forces all branches to execute — there is no choice, so there is no permission. Each parallel branch is classified as `odrl:Duty`.

### Intermediate catch events as prohibitions (v8)

Prior versions (v4–v7) treated intermediate catch events as transparent pass-through nodes, attaching `bpmn:messageReceived` constraints to downstream tasks as an afterthought. This was architecturally wrong.

The correct reading: an intermediate catch event encodes a **conditional prohibition on proceeding**. The participant is *prohibited* from executing the downstream task until the triggering condition holds.

- Not a **duty** — waiting is not an action the participant performs.
- Not a **permission** — the participant has no choice about waiting.
- A **prohibition on continuation**, lifted when the event fires.

**Formal mapping:**
```
IntermediateCatchEvent(E) with downstream task T
→ odrl:Prohibition on T
    odrl:constraint: bpmn:eventReceived = "<event name>"
    rdfs:comment: "Party cannot perform T until E is received"
```

---

## Pipeline Stages

The code runs seven stages in sequence.

### Stage 1: Parse BPMN XML
Reads the XML and builds an in-memory directed graph. Extracts tasks, gateways, events, sequence flows (with condition labels), message flows, participants, and swim-lane assignments. Intermediate catch events are tracked explicitly in a dedicated set for Stage 4b.

### Stage 2: Tarjan SCC + DAG Normalisation

**Tarjan SCC (iterative DFS):** Detects cycles in O(V+E). Each cycle is collapsed into a single `meta_loop` node, guaranteeing the output is a DAG. ODRL has no loop construct so cycles must be resolved before policy extraction.

**In-degree scan:** Finds nodes with no incoming edges — the entry point per pool/process.

### Stage 3: Dominance Tree (BFS + fixed-point iteration)
Determines structural preconditions. Node A *dominates* node B if every path from the start to B passes through A. Produces the `after:` field in the output — a task cannot be reached without its dominator having been performed first.

### Stage 4: Deontic Classification (BFS per node)
Classifies each task using two-level reachability:

**Global check:** Remove task T; BFS from start. If no end node is reachable → **Duty**. If any end node is still reachable → **Permission**.

**Branch-local check:** For tasks inside XOR/EVENT gateway branches, repeat the check within the branch subgraph. A task mandatory within its branch (even if the branch itself is optional) → **ConstrainedDuty**.

AND gateway branches are forced to **Duty** regardless of the reachability check result.

### Stage 4b: Wait Prohibitions (v8 — new)
Walks all intermediate catch events. For each catch event E with downstream task(s) T:
- Emits an `odrl:Prohibition` on T for the party whose lane contains E.
- The constraint (`bpmn:eventReceived = <event name>`) encodes the lifting condition.
- If a message flow identifies the sender, a `bpmn:messageSender` constraint is added.

This stage replaces the `waitFor` constraint mechanism from earlier versions entirely.

### Stage 5: Role-Partitioned DFS (path-aware condition accumulation)
Traverses every execution path through each pool, accumulating gateway conditions per task.

Key design decisions:
- One rule per task; conditions from all paths are **unioned**.
- A task reachable unconditionally on any path → unconditional overall.
- XOR gateway conditions are qualified: `"score available? = no"` not just `"no"`.
- EVENT gateway conditions use the catch event name: `"event: delay information received"`.
- When all branches of a gateway appear in the union, they cancel out to unconditional.

Message flows are handled as: outgoing from task → `odrl:duty inform` (notify duty); incoming to intermediate catch event → handled by Stage 4b, not attached as `waitFor` constraints.

**Implicit role prohibitions** are then derived: for each participant, one compact `odrl:Prohibition` lists all tasks belonging exclusively to other participants. Produces M prohibitions (one per role), not M×N individual rules.

### Stage 6: ODRL JSON-LD Emission
Assembles the final policy document:
- One `odrl:Set` policy per BPMN collaboration.
- `odrl:obligation` for Duties and ConstrainedDuties.
- `odrl:permission` for Permissions and ConstrainedPermissions.
- `odrl:prohibition` for: (a) event-derived conditional prohibitions (WaitProhibitions), (b) implicit cross-role prohibitions.
- Gateway conditions as `odrl:constraint` with `bpmn:gatewayCondition` operand.
- Party declarations for all participants.

---

## Graph Algorithms Used

| Algorithm | Stage | Purpose |
|---|---|---|
| Iterative DFS (Tarjan SCC) | 2 | Detect and collapse cycles → DAG |
| In-degree scan | 2 | Find entry point per pool |
| BFS + fixed-point iteration | 3 | Dominance tree → `after:` preconditions |
| BFS per node (with exclusion) | 4 | Global Duty vs Permission classification |
| Branch-subgraph BFS | 4 | ConstrainedDuty detection within branches |
| DAG walk (event extraction) | 4b | Intermediate catch event → WaitProhibition |
| DFS (path-aware, condition union) | 5 | Qualified conditions + role assignment |

---

## Example Output (Asynchronous Credit Scoring)

```
Party: credit scoring (bank)
  [Duty             ]  request credit score          conditions: (unconditional)
  [Permission       ]  report delay                  conditions: event: delay information received
  [Duty             ]  send credit score             conditions: (unconditional)

Party: scoring service
  [Duty             ]  compute credit score (level 1)  conditions: (unconditional)
  [Permission       ]  report delay                    conditions: score available? = no
  [Permission       ]  compute credit score (level 2)  conditions: score available? = no
  [Permission       ]  send credit score               conditions: score available? = no
  [Permission       ]  send credit score               conditions: score available? = yes

── Wait Prohibitions (intermediate catch events) ──
  credit scoring (bank) cannot proceed to 'send credit score'
    until event 'credit score received' received (sent by 'scoring service')
  credit scoring (bank) cannot proceed to 'send credit score'
    until event 'delay information received' received (sent by 'scoring service')

── Role Prohibitions (cross-party task access) ──
  credit scoring (bank)  may NOT perform:
    ✗  compute credit score (level 1)
    ✗  compute credit score (level 2)
  scoring service  may NOT perform:
    ✗  request credit score
```

---

## Changes from v4–v7

| Aspect | v4–v7 | v8 |
|---|---|---|
| Intermediate catch events | Dropped or bolted on as `waitFor` constraint | Dedicated `odrl:Prohibition` with lifting constraint (Stage 4b) |
| AND gateway branches | `odrl:Permission` | `odrl:Duty` (no choice = obligation) |
| `waitFor` constraint on tasks | Added as `bpmn:messageReceived` | Removed; handled entirely by WaitProhibition stage |
| ConstrainedDuty | Not distinguished | Separate classification for branch-mandatory tasks |
| Double-counted event triggers | Present in v7 (regression) | Eliminated by clean separation of stages |
| Rule type totals | Duties / Permissions | Duties / ConstrainedDuties / Permissions / ConstrainedPermissions / WaitProhibitions / RoleProhibitions |

---

## Known Open Problems

**Loop semantics:** Collapsed SCCs lose iterative semantics. Whether a loop-derived obligation means "perform once" or "perform repeatedly while condition holds" is not distinguishable in core ODRL. Profile extension required.

**OR (inclusive) gateway:** BPMN's inclusive gateway has state-dependent merge behaviour. Its deontic interpretation is an open research question. Not handled in current pipeline.

**Temporal sequencing:** BPMN sequence flows encode task ordering that ODRL constraints handle only partially. Full sequential semantics would require an ODRL profile extension or integration with a temporal ontology.

**Parallel obligation concurrency:** Two AND-branch duties are both `odrl:Duty` but ODRL has no native construct for mandatory concurrent execution. Currently emitted as two independent duties.

**Prohibition lifting semantics:** The lifting condition for timer and signal catch events (as opposed to message catch events) needs a profile extension or hybrid representation to express precisely.

---

## Novelty

This pipeline is the first published approach to combine:
- BPMN graph traversal for deontic norm extraction.
- ODRL as the policy target, with a direct structural correspondence via Natschläger's deontic BPMN result.
- Reachability-based Duty/Permission classification rather than longest-path heuristics.
- Intermediate catch events mapped to `odrl:Prohibition` with lifted constraints ("wait as prohibition").
- Compact implicit prohibition generation (M prohibitions, one per role).

---

## Key References

- Natschläger, C. (2011). Deontic BPMN. DEXA 2011 / SoSyM ~2013.
- Tarjan, R.E. (1972). Depth-first search and linear graph algorithms. SIAM J. Computing 1(2).
- Lengauer, T., Tarjan, R.E. (1979). A fast algorithm for finding dominators in a flowgraph. ACM TOPLAS 1(1).
- W3C ODRL Community Group (2018). ODRL Information Model 2.2. W3C Recommendation.
- Skersys, T. et al. (2022). Transforming BPMN Processes to SBVR Process Rules with Deontic Modalities. Applied Sciences 12(18).
- Annane, A. et al. (2019). BBO: BPMN 2.0 Based Ontology for Business Process Representation.
- De Vos, M., Kirrane, S., Padget, J., Satoh, K. (2019). ODRL Policy Modelling and Compliance Checking. RuleML+RR 2019.
- Colombo Tosatto, S., Governatori, G., van Beest, N. (2019). Checking Regulatory Compliance: Will We Live to See It? BPM 2019.
