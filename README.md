---
title: Plant Operations Ontology — Predictive Maintenance PoC
emoji: 🏭
colorFrom: blue
colorTo: gray
sdk: gradio
sdk_version: "6.27.0"
app_file: app.py
pinned: false
---

# Plant Operations Ontology — predictive maintenance digital twin

A proof of concept for the argument that an Ontology turns fragmented enterprise data
into an **actionable, AI-ready operating system** rather than another analytics layer.

Scope is deliberately narrow — one decision — and deliberately deep: the full
data → logic → action → learning loop, including governance and writeback.

> **The decision.** A vibration channel on a critical CNC spindle crosses its warning
> limit. Somebody has to decide, in the next few hours, whether to stop a line that is
> carrying a $500k aerospace order. Today that answer takes about four hours and five
> systems. Here it takes one screen.

---

## Run it

```bash
pip install -r requirements.txt
python app.py                 # http://127.0.0.1:7860
```

Run the test suite (33 tests covering the store, RUL model, logic, governance,
agent, metrics, API envelopes and the app handlers end to end):

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

No API key, no database, no network egress required. The agent runs a deterministic
planner by default, so the demo always works and always tells the same story.

**Optional — Hugging Face-backed reasoning.** Set `HF_TOKEN` and switch *Agent
reasoning mode* to Hugging Face. The model then drives tool selection itself over the
same Ontology tool surface, and the trace tab shows exactly which tools it chose.

**Deploy to Hugging Face Spaces.** The YAML header above is the Space config. Create a
Gradio Space, push `app.py`, `ontology.py`, `simulator.py`, `logic.py`, `agent.py`,
`actions.py`, `metrics.py`, `foundry_api.py`, `requirements.txt` and this README
(its YAML header is the Space config; if the Space rejects `sdk_version: 6.27.0`,
use the nearest Gradio 6.x it offers), and add `HF_TOKEN` as a repository secret if
you want Hugging Face mode.

---

## Five-minute demo script

| # | Tab | Do this | The point |
|---|---|---|---|
| 1 | Ops console | Note VMC-07 at health 45 with an open warning. Read the right-hand panel. | The alert arrives already framed in money: $1,596/h of contribution margin, two live production orders, a predicted RUL. Nobody opened a second system. |
| 2 | Object explorer | Open `Machine / MCH-2207`, look at the outgoing links. | Semantic unification. Seven systems of record; one object with links you traverse instead of joins you write. |
| 3 | Disruption Bot | Click **Diagnose**. Open the tool-use trace. | The agent made ~15 calls: object reads, link traversals, four typed Ontology Queries, a scenario simulation, then proposals. It reasoned over connected objects, not retrieved documents. Note that it cites AAR-0002 — the last time this exact signature was overridden, the spindle was scrapped. |
| 4 | Disruption Bot | Read the options table. | Three courses of action, each with P(failure), expected downtime, parts, production loss, escalation risk and expected total. Every term is visible and auditable. |
| 5 | Disruption Bot | Switch identity to **operator**, click **Submit plan**. | The parts reservation auto-approves. The work order is refused: the operator role cannot apply it. Security policy is in the Ontology, not the UI. |
| 6 | Disruption Bot | Switch to **planner**, submit again. | Now the work order is *held for approval* — machine criticality is `critical`. The agent never had the authority to stop a line. |
| 7 | Actions & approvals | Approve `APR-0001`. | Work order moves to `scheduled`, gets `approvedBy`, and the simulated SAP PM writeback returns an order reference. Scroll to the payload. |
| 8 | Scenarios | Create a scenario, simulate `rescheduleProductionOrder`, view the diff, then discard. | Copy-on-write branch. Validated, audited, and nothing reached the live Ontology or any external system. |
| 9 | Audit trail | Scan the table. | Every application, rejection, approval and simulation, with actor, actor type, parameters, affected object RIDs, branch and writeback target. |
| 10 | Metrics & closed loop | Record an After Action Report, then re-run the agent. | The outcome is now an Ontology object. The next matching alert retrieves it, and the RUL model's measured error moves. The loop closes. |

Extra beats if you have time: tick the plant forward with **Advance 4 h** and watch the
spindle temperature raise a second alert; try to reserve the last bearing twice and
watch the Ontology constraint reject it; open the **Foundry v2 API** tab to show the
request and response shapes.

---

## What the PoC demonstrates

**1 · Semantic unification and visibility.** Fourteen object types, twenty link types,
seven external systems of record. Machine → Sensor → Alert → FailureMode on one side;
Machine → ProductionLine → ProductionOrder → Customer on the other; Part → InventoryItem
→ Supplier and WorkOrder → Technician underneath. `computeRevenueAtRisk` walks that graph
to answer "which high-revenue orders are at risk from this vibration reading" in a single
call.

**2 · Logic and what-if.** Five typed Ontology Queries (`predictRemainingUsefulLife`,
`matchFailureMode`, `checkPartsAvailability`, `findMaintenanceWindows`,
`computeRevenueAtRisk`) plus an expected-value model over three courses of action.
Scenarios fork the Ontology copy-on-write so reallocations can be tested and thrown away.

**3 · AI agent.** Disruption Bot's entire tool surface is Ontology operations — object
reads, link traversal, typed queries, scenario simulation and action *proposals*. It
retrieves prior After Action Reports, quantifies its own model's historical bias, states
its confidence, and names the evidence that would change its recommendation. It has no
authority to apply anything that stops a machine.

**4 · Governed actions and writeback.** Seven Action Types with typed parameters,
Ontology-level validation (you cannot reserve stock that does not exist), role-based
permissions, per-action approval policy, and simulated webhook writeback to SAP PM,
SAP MM, MES and Salesforce with idempotency keys. 100% audit coverage.

**5 · Closed loop.** Outcomes are written back as `AfterActionReport` objects. The agent
retrieves them by signature on the next alert; the accuracy metrics recompute from them.

**6 · UI.** Operator console with live telemetry, a focused 360° impact panel, agent chat
output, scenario sandbox and one-click governed actions. Renders fine on a plant-floor
tablet.

---

## Headline metrics

| | This PoC | Manual baseline |
|---|---|---|
| Alert → decision | seconds to a couple of minutes | 4.2 h median |
| Data gathering | 15 Ontology tool calls, ~2 ms | ~95 analyst-minutes |
| Systems the operator opens | 1 | 5 |
| Audit coverage | 100% | ~40% |
| Downtime when the recommendation is followed | 3.8 h | 16.5 h when overridden |
| Conservative annual value, one line | ~$365k margin protected | — |

The annual figure counts only 18 comparable events per year on one line, credits only
that line's contribution margin, and excludes avoided scrap, expedite fees and customer
penalties. Baselines come from the discovery workshop and are stated in `metrics.py`
where anyone can argue with them.

---

## What is real and what is simulated

Being explicit about this is what makes the PoC credible in the room.

**Real.** The object model and its links. The RUL model — log-linear regression on
excess-over-baseline, auditable line by line. The failure-mode signature matching. The
economics. Parameter validation and Ontology constraints. Role-based permissions and the
approval policy. The audit trail. The agent's tool-use loop. The Foundry v2 request and
response shapes.

**Simulated.** The telemetry (deterministic synthetic generator with a planted bearing
fault — no customer data). The writeback: payloads are built, logged and shown, but no
external HTTP call is made. The source systems themselves.

**Not included.** Real streaming ingest, Foundry authentication and marking-based
security, Workshop or OSDK front-end, geospatial views, multi-site scale.

---

## Repointing at a real Foundry stack

`foundry_api.py` is the seam. It already produces the v2 envelopes
(`OntologyV2`, `ObjectTypeV2`, `LinkTypeSideV2`, `SyncApplyActionResponseV2`) documented
at `palantir.com/docs/foundry/api/v2/ontologies-v2-resources/`. Replacing its function
bodies with authenticated `requests` calls, or with the generated OSDK, leaves
`agent.py`, `logic.py`, `actions.py` and `app.py` untouched — which is the point of
building against the Ontology's API surface rather than a database.

In a real deployment the mapping is direct: object types become Foundry object types
backed by your datasets; the queries become Ontology Functions (with the RUL model in
Model Studio); the actions become Action Types with their own submission criteria and
webhooks; the agent becomes AIP Logic; and this UI becomes Workshop or an OSDK app.

---

## Files

| File | What it holds |
|---|---|
| `ontology.py` | Object types, properties, link types, branch-aware store, seed data |
| `simulator.py` | Telemetry generation, degradation profile, alerting, RUL query |
| `logic.py` | Ontology Queries and the Scenario branching manager |
| `agent.py` | Disruption Bot: tool surface, deterministic planner, Hugging Face mode |
| `actions.py` | Action Types, validation, approval policy, writeback, audit |
| `metrics.py` | Baselines and measured outcomes |
| `foundry_api.py` | Foundry v2 REST envelopes |
| `app.py` | Gradio application |
| `test_core.py` | Store, simulator, RUL and logic-layer tests |
| `test_workflow.py` | Governance, agent, metrics, API and headless app tests |

---

## Where it goes next

The first workflow paid for the semantic layer. Every workflow after it inherits that
layer and pays only for its own objects and actions: supplier disruption response,
spares optimisation, energy and OEE, quality escapes and containment scope, labour
planning, capital planning. The Ontology Queries, approval policy, audit trail,
writeback plumbing and agent tool surface are all shared — which is why workflow two is
weeks rather than quarters. Tab 9 in the app lays this out for a business audience.
