"""
app.py — Gradio front end for the Plant Operations Ontology PoC.

One workflow, end to end: a vibration alert on a critical spindle becomes a
diagnosed, costed, approved, written-back maintenance decision with a full audit
trail — without the operator leaving this screen.

Run:  python app.py        (or: gradio app.py)
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

import gradio as gr
import pandas as pd

import agent
import foundry_api as api
import metrics as M
from actions import ACTION_TYPES, ActionEngine
from logic import ScenarioManager, compute_revenue_at_risk
from ontology import (LINK_TYPES, OBJECT_TYPES, ONTOLOGY_API_NAME, OntologyStore,
                      build_ontology, iso, now, outgoing_links)
from simulator import PlantSimulator, predict_rul

USERS = {
    "R. Delgado — Maintenance Technician (operator)": ("r.delgado", "operator"),
    "J. Reyes — Maintenance Planner (approver)": ("j.reyes", "maintenance_planner"),
}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    store: OntologyStore
    sim: PlantSimulator
    engine: ActionEngine
    scenarios: ScenarioManager
    recommendation: agent.Recommendation | None = None
    active_alert: str | None = None
    active_scenario: str | None = None
    log: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.log.insert(0, f"`{dt.datetime.now().strftime('%H:%M:%S')}`  {text}")
        self.log = self.log[:40]


def new_session() -> Session:
    store = build_ontology()
    sim = PlantSimulator(store)
    sim.bootstrap()
    s = Session(store, sim, ActionEngine(store), ScenarioManager(store))
    open_alerts = [a for a in store.all("Alert") if a["status"] == "open"]
    s.active_alert = open_alerts[0]["alertId"] if open_alerts else None
    s.note("Ontology loaded, 72h of telemetry replayed from the streaming dataset.")
    return s


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _sev_badge(sev: str) -> str:
    return {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(sev, "⚪")


def render_plant(s: Session) -> str:
    rows = []
    for m in sorted(s.store.all("Machine"), key=lambda x: x["healthScore"]):
        bar = "█" * int(m["healthScore"] / 10) + "░" * (10 - int(m["healthScore"] / 10))
        alerts = [a for a in s.store.linked("Machine", m["machineId"], "alerts")
                  if a["status"] in ("open", "triaged")]
        flag = _sev_badge(alerts[0]["severity"]) if alerts else "🟢"
        rows.append(f"| {flag} **{m['name']}** | {m['lineId']} | {m['criticality']} | "
                    f"`{bar}` {m['healthScore']:.0f} | {m['status']} |")
    header = ("| Asset | Line | Criticality | Health | Status |\n"
              "|---|---|---|---|---|\n")
    clock = s.sim.clock().strftime("%a %d %b, %H:%M UTC")
    return f"**Plant clock:** {clock} · Columbus Plant 3\n\n" + header + "\n".join(rows)


def render_alerts(s: Session) -> pd.DataFrame:
    rows = []
    for a in sorted(s.store.all("Alert"), key=lambda x: x["raisedAt"], reverse=True):
        m = s.store.get("Machine", a["machineId"])
        rows.append({
            "": _sev_badge(a["severity"]),
            "Alert": a["alertId"],
            "Asset": m["name"],
            "Signal": a["sensorId"],
            "Signature": a["signature"],
            "Raised": a["raisedAt"][11:16],
            "Status": a["status"],
        })
    return pd.DataFrame(rows or [{"": "", "Alert": "no alerts", "Asset": "", "Signal": "",
                                  "Signature": "", "Raised": "", "Status": ""}])


def render_series(s: Session, sensor_id: str) -> pd.DataFrame:
    sensor = s.store.get("Sensor", sensor_id)
    rows = []
    for r in s.sim.series(sensor_id, hours=72):
        t = pd.Timestamp(r["timestamp"])
        rows.append({"time": t, "value": r["value"], "series": sensor["measure"]})
        rows.append({"time": t, "value": sensor["warnThreshold"], "series": "warn limit"})
        rows.append({"time": t, "value": sensor["critThreshold"], "series": "critical limit"})
    return pd.DataFrame(rows)


def sensor_choices(s: Session) -> list[str]:
    return [f"{x['sensorId']} — {x['machineId']} {x['measure']}" for x in s.store.all("Sensor")]


def render_impact(s: Session) -> str:
    """The 360° view: what this alert means in money, not millivolts."""
    if not s.active_alert:
        return "_No alert selected._"
    a = s.store.get("Alert", s.active_alert)
    m = s.store.get("Machine", a["machineId"])
    rul = predict_rul(s.sim, a["sensorId"])
    unplanned = compute_revenue_at_risk(s.store, m["machineId"], m["unplannedRepairHours"])
    lines = [
        f"### {_sev_badge(a['severity'])} {a['title']}",
        f"`{a['alertId']}` · signature `{a['signature']}` · raised {a['raisedAt'][11:16]}",
        "",
        f"**Predicted RUL:** {rul.get('remainingUsefulLifeHours', '—')} h "
        f"(confidence {rul.get('confidence', '—')}) → critical breach "
        f"{rul.get('predictedFailureAt', '—')}",
        f"**Contribution margin at stake:** "
        f"${unplanned['contributionMarginPerHourUsd']:,.0f}/h on line {unplanned['lineId']}",
        f"**Unplanned {m['unplannedRepairHours']:.0f}h stop would cost:** "
        f"${unplanned['totalRevenueAtRiskUsd']:,.0f}",
        "",
        "**Production orders on this line**",
        "| Order | Customer | Priority | Revenue | Due | Slack | Would breach |",
        "|---|---|---|---|---|---|---|",
    ]
    for o in unplanned["orders"]:
        lines.append(f"| {o['productionOrderId']} | {o['customer']} | {o['priority']} | "
                     f"${float(o['revenueUsd']):,.0f} | {o['dueAt'][:10]} | "
                     f"{o['slackHours']:.0f}h | {'⚠️ yes' if o['wouldBreach'] else 'no'} |")
    return "\n".join(lines)


def render_trace(rec: agent.Recommendation | None) -> pd.DataFrame:
    if not rec:
        return pd.DataFrame([{"Tool": "—", "Arguments": "", "Result": "", "Latency": ""}])
    return pd.DataFrame([dict(zip(["Tool", "Arguments", "Result", "Latency"], c.to_row()))
                         for c in rec.trace])


def render_options(rec: agent.Recommendation | None) -> pd.DataFrame:
    if not rec or not rec.option_table:
        return pd.DataFrame([{"Option": "—"}])
    rows = []
    for o in rec.option_table:
        rows.append({
            "": "✅" if o.get("recommended") else "",
            "Option": o["label"],
            "P(failure)": f"{o['failureProbability']:.0%}",
            "Expected downtime": f"{o['expectedDowntimeHours']:.1f} h",
            "Parts": f"${o['partsCostUsd']:,.0f}",
            "Production loss": f"${o['productionMarginLossUsd']:,.0f}",
            "Late penalty": f"${o['latePenaltyUsd']:,.0f}",
            "Escalation risk": f"${o['escalationCostUsd']:,.0f}",
            "Expected total": f"${o['totalExpectedCostUsd']:,.0f}",
        })
    return pd.DataFrame(rows)


def render_plan(rec: agent.Recommendation | None) -> pd.DataFrame:
    if not rec:
        return pd.DataFrame([{"#": "", "Action": "—", "What it does": "", "Governance": ""}])
    return pd.DataFrame([{
        "#": a.sequence, "Action": a.action_type,
        "What it does": a.rationale, "Governance": a.risk,
    } for a in rec.proposed_actions])


def render_pending(s: Session) -> pd.DataFrame:
    rows = [{"Approval": p["approvalId"], "Action": p["actionType"],
             "Requested by": p["requestedBy"], "At": p["requestedAt"][11:16],
             "Why held": p["reason"],
             "Parameters": json.dumps(p["parameters"], default=str)[:160]}
            for p in s.engine.pending.values()]
    return pd.DataFrame(rows or [{"Approval": "—", "Action": "nothing awaiting approval",
                                  "Requested by": "", "At": "", "Why held": "", "Parameters": ""}])


def render_audit(s: Session) -> pd.DataFrame:
    rows = []
    for e in sorted(s.store.all("AuditEvent"), key=lambda x: x["timestamp"], reverse=True):
        rows.append({
            "Event": e["eventId"], "Time": e["timestamp"][11:19],
            "Actor": f"{e['actor']} ({e['actorType']})", "Action": e["actionType"],
            "Decision": e["decision"], "Branch": e["branch"],
            "Objects": ", ".join(e["affectedObjectRids"])[:70],
            "Writeback": e["writebackTarget"], "Justification": e["justification"][:120],
        })
    return pd.DataFrame(rows or [{"Event": "—", "Time": "", "Actor": "", "Action": "",
                                  "Decision": "", "Branch": "", "Objects": "",
                                  "Writeback": "", "Justification": ""}])


def render_webhooks(s: Session) -> str:
    if not s.engine.webhook_log:
        return "_No writebacks yet. Apply an action to see the outbound payload._"
    return "\n\n".join(
        f"**→ {w['target']}**  ·  `{w['mode']}`  ·  idempotency `{w['idempotencyKey']}`\n"
        f"```json\n{json.dumps(w['body'], indent=2, default=str)[:1400]}\n```\n"
        f"response: `{json.dumps(w['response'])}`"
        for w in reversed(s.engine.webhook_log[-4:]))


def _dur(seconds: float | None) -> str:
    if not seconds:
        return "—"
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"


def render_metrics(s: Session) -> str:
    tool_calls = len(s.recommendation.trace) if s.recommendation else 0
    secs = s.recommendation.elapsed_seconds if s.recommendation else None
    sm = M.session_metrics(s.store, secs, tool_calls, s.active_alert)
    om = M.outcome_metrics(s.store)
    pr = M.annualised_projection(s.store)

    lat = sm["decisionLatencySeconds"]
    lat_txt = ("—" if not lat else f"{max(lat, 1):.0f} s" if lat < 300
               else f"{lat / 60:.1f} min")
    speed = sm["speedupFactor"]
    hands_on = (sm["baselineAnalystHandsOnSeconds"] / max(lat, 1)) if lat else None
    if not speed:
        speed_txt = "—"
    elif speed > 1000:
        speed_txt = ("**>1000×** in this session — the operator acted the moment the alert "
                     "landed. A live plant still queues the human approval, so plan on "
                     "50–200× end to end.")
    else:
        speed_txt = f"**{speed:,.0f}×** end to end, **{hands_on:,.0f}×** against hands-on analyst time"

    out = [
        "### Decision speed",
        "| Measure | This PoC | Manual baseline |",
        "|---|---|---|",
        f"| Alert → decision | **{lat_txt}** | 4.2 h (median) |",
        f"| Agent reasoning | {_dur(sm['agentReasoningSeconds'])} over "
        f"{sm['ontologyToolCalls']} Ontology tool calls | 95 analyst-minutes |",
        f"| Speed-up | {speed_txt} | — |",
        "",
        f"> {sm['latencyCaveat']}",
        "",
        "### Swivel-chair work",
        f"- Systems the operator opened: **{sm['systemsTouchedByOperator']}** "
        f"(this screen) versus **{sm['baselineSystemsTouched']}** today — "
        f"**{sm['swivelChairReductionPct']}% reduction**.",
        f"- Systems of record unified behind the Ontology: "
        f"**{sm['systemsUnifiedBehindOntology']}**",
        "  - " + "\n  - ".join(M.SOURCE_SYSTEMS),
        "",
        "### Governance",
        f"- Actions applied: **{sm['actionsApplied']}** · held for approval: "
        f"**{sm['actionsPendingApproval']}** · blocked by policy or validation: "
        f"**{sm['actionsRejectedByPolicy']}** · simulated on a scenario branch: "
        f"**{sm['actionsSimulated']}**",
        f"- Audit coverage: **{sm['auditCoveragePct']}%** of actions "
        f"(baseline {sm['baselineAuditCoveragePct']}%) — every application, rejection and "
        f"approval carries actor, parameters, affected object RIDs and writeback target.",
    ]

    if om.get("cases"):
        out += [
            "",
            "### Recommendation accuracy (closed-loop)",
            f"- Closed cases in the Ontology: **{om['cases']}** · recommendation acceptance "
            f"rate **{om['acceptanceRatePct']}%**",
            f"- Mean downtime when followed: **{om['avgDowntimeAcceptedHours']} h** "
            f"(${om['avgCostAcceptedUsd']:,.0f}) versus **{om['avgDowntimeOverriddenHours']} h** "
            f"(${om['avgCostOverriddenUsd']:,.0f}) when overridden",
            f"- RUL model error: **{om['rulMapePct']}% MAPE** against observed time-to-failure",
        ]
    if pr:
        out += [
            "",
            "### Conservative annual projection (one line)",
            f"- {pr['assumedAlertsPerYear']} comparable events/year × "
            f"{pr['downtimeHoursAvoidedPerAlert']} h avoided = "
            f"**{pr['annualDowntimeAvoidedHours']} h** of downtime",
            f"- At ${pr['contributionMarginPerHourUsd']:,}/h → "
            f"**${pr['annualMarginProtectedUsd']:,} margin protected**, plus "
            f"~{pr['analystHoursReturnedPerYear']} analyst-hours returned",
            f"- _{pr['note']}_",
        ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def boot():
    s = new_session()
    default_sensor = next(c for c in sensor_choices(s) if c.startswith("SEN-2207-VIB"))
    return (s, render_plant(s), render_alerts(s), render_impact(s),
            gr.update(choices=sensor_choices(s), value=default_sensor),
            render_series(s, "SEN-2207-VIB"), "\n".join(s.log), render_metrics(s))


def advance(s: Session, minutes: int, sensor_label: str):
    raised = s.sim.tick(minutes)
    for a in raised:
        s.note(f"🚨 **{a['alertId']}** {a['title']}")
        if s.active_alert is None:
            s.active_alert = a["alertId"]
    if not raised:
        s.note(f"Plant advanced {minutes} min — telemetry ingested, no new alerts.")
    sid = sensor_label.split(" — ")[0]
    return (s, render_plant(s), render_alerts(s), render_impact(s),
            render_series(s, sid), "\n".join(s.log))


def pick_sensor(s: Session, sensor_label: str):
    return render_series(s, sensor_label.split(" — ")[0])


def pick_alert(s: Session, alert_id: str):
    s.active_alert = alert_id.strip() or s.active_alert
    return s, render_impact(s)


def run_agent(s: Session, mode_label: str):
    if not s.active_alert:
        return s, "_No open alert to diagnose._", render_trace(None), render_options(None), \
               render_plan(None), "\n".join(s.log)
    mode = "hf" if mode_label.startswith("Hugging Face") else "deterministic"
    rec = agent.diagnose(s.store, s.sim, s.active_alert, mode=mode)
    s.recommendation = rec
    s.note(f"🤖 Disruption Bot diagnosed **{s.active_alert}** in {rec.elapsed_seconds:.2f}s "
           f"using {len(rec.trace)} Ontology tool calls.")
    return (s, rec.to_markdown(), render_trace(rec), render_options(rec), render_plan(rec),
            "\n".join(s.log))


def submit_plan(s: Session, user_label: str):
    if not s.recommendation:
        return s, "_Run the agent first._", render_pending(s), render_audit(s), \
               render_webhooks(s), render_metrics(s), "\n".join(s.log)
    actor, role = USERS[user_label]
    out = []
    for p in s.recommendation.proposed_actions:
        r = s.engine.apply(p.action_type, p.parameters, actor=actor, actor_role=role,
                           actor_type="human", justification=p.rationale)
        icon = {"auto_approved": "✅", "applied": "✅", "pending_approval": "⏸️",
                "rejected": "⛔", "simulated": "🧪"}[r.decision]
        out.append(f"{icon} **{p.action_type}** — {r.message}")
        if r.writeback:
            out.append(f"    ↳ writeback `{r.writeback['target']}` → "
                       f"`{json.dumps(r.writeback['response'])}`")
    s.note(f"Submitted the {len(s.recommendation.proposed_actions)}-step plan as **{actor}** "
           f"({role}).")
    return (s, "\n\n".join(out), render_pending(s), render_audit(s), render_webhooks(s),
            render_metrics(s), "\n".join(s.log))


def approve(s: Session, approval_id: str, user_label: str):
    actor, role = USERS[user_label]
    if role != "maintenance_planner":
        return (s, f"⛔ {actor} does not hold approval rights. Switch identity to the "
                   f"maintenance planner.", render_pending(s), render_audit(s),
                render_webhooks(s), render_metrics(s), "\n".join(s.log))
    r = s.engine.approve(approval_id.strip(), actor)
    s.note(f"✍️ **{actor}** approved `{approval_id.strip()}` → {r.decision}.")
    detail = r.message
    if r.writeback:
        detail += (f"\n\nWriteback to **{r.writeback['target']}** → "
                   f"`{json.dumps(r.writeback['response'])}`")
    return (s, detail, render_pending(s), render_audit(s), render_webhooks(s),
            render_metrics(s), "\n".join(s.log))


def reject(s: Session, approval_id: str, user_label: str, reason: str):
    actor, role = USERS[user_label]
    r = s.engine.reject(approval_id.strip(), actor, reason or "no reason given")
    s.note(f"🚫 **{actor}** rejected `{approval_id.strip()}`.")
    return (s, r.message, render_pending(s), render_audit(s), render_webhooks(s),
            render_metrics(s), "\n".join(s.log))


# -- object explorer -------------------------------------------------------

def explorer_objects(s: Session, otype: str):
    pk = OBJECT_TYPES[otype]["primaryKey"]
    choices = [str(o[pk]) for o in s.store.all(otype)]
    return gr.update(choices=choices, value=choices[0] if choices else None)


def explore(s: Session, otype: str, pk: str):
    obj = s.store.get(otype, pk)
    if not obj:
        return "_Select an object._", pd.DataFrame([{"Link": "—"}])
    meta = OBJECT_TYPES[otype]
    lines = [f"### {meta['displayName']} · {obj.get(meta['titleProperty'], pk)}",
             f"`{obj['__rid']}`", "",
             f"_Source system: {meta['sourceSystem']}_", "",
             "| Property | Value |", "|---|---|"]
    for k, v in obj.items():
        if k.startswith("__"):
            continue
        lines.append(f"| `{k}` | {v} |")
    rows = []
    for link in outgoing_links(otype):
        related = s.store.linked(otype, pk, link)
        tgt = LINK_TYPES[link][1]
        tgt_pk = OBJECT_TYPES[tgt]["primaryKey"]
        title = OBJECT_TYPES[tgt]["titleProperty"]
        rows.append({"Link": link, "Target type": tgt, "Count": len(related),
                     "Objects": ", ".join(f"{r[tgt_pk]} ({r.get(title)})"
                                          for r in related[:6])[:160]})
    return "\n".join(lines), pd.DataFrame(rows or [{"Link": "no outgoing links"}])


# -- scenarios -------------------------------------------------------------

def scenario_create(s: Session, label: str):
    sid = s.scenarios.create(label or "What-if", "Sandboxed branch of the live Ontology")
    s.active_scenario = sid
    s.note(f"🧪 Created scenario **{sid}** — a copy-on-write branch of the Ontology.")
    return (s, f"Scenario **{sid}** created from `master`. Actions applied here are simulated: "
               f"no live objects change and no external system is called.",
            gr.update(choices=list(s.scenarios.active), value=sid), "\n".join(s.log))


def scenario_apply(s: Session, sid: str, action_type: str, params_json: str):
    if not sid:
        return s, "_Create a scenario first._", pd.DataFrame([{"Change": "—"}]), "\n".join(s.log)
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as e:
        return s, f"⛔ Invalid JSON: {e}", pd.DataFrame([{"Change": "—"}]), "\n".join(s.log)
    r = s.engine.apply(action_type, params, actor="j.reyes",
                       actor_role="maintenance_planner", branch=sid)
    s.scenarios.record(sid, r)
    diff = s.scenarios.diff(sid)
    s.note(f"🧪 {action_type} simulated on **{sid}**.")
    return (s, r.message + ("\n\nValidation errors:\n- " + "\n- ".join(r.validation_errors)
                            if r.validation_errors else ""),
            pd.DataFrame(diff or [{"Change": "no differences from master"}]), "\n".join(s.log))


def scenario_compare(s: Session, sid: str):
    if not s.recommendation or not s.recommendation.option_table:
        return "_Run the agent first so there are options to compare._"
    rows = s.recommendation.option_table
    best = next(r for r in rows if r["recommended"])
    out = ["### Option comparison (expected-value model)", "",
           "| Option | P(failure) | Expected downtime | Expected total cost | Δ vs worst |",
           "|---|---|---|---|---|"]
    for o in rows:
        mark = " ✅" if o["recommended"] else ""
        out.append(f"| {o['label']}{mark} | {o['failureProbability']:.0%} | "
                   f"{o['expectedDowntimeHours']:.1f} h | "
                   f"${o['totalExpectedCostUsd']:,.0f} | ${o['deltaVsWorstUsd']:,.0f} |")
    out += ["", f"**Recommended:** {best['label']} — {best['description']}", "",
            "Every term is visible and auditable: expected downtime = "
            "(1−P) × planned repair + P × unplanned repair; escalation cost = "
            "P(failure) × P(secondary damage) × replacement cost; production loss and late "
            "penalties come from the ProductionOrder objects linked to this line."]
    return "\n".join(out)


def scenario_commit(s: Session, sid: str):
    if not sid:
        return s, "_No scenario selected._", render_audit(s), "\n".join(s.log)
    changed = s.scenarios.commit(sid)
    s.note(f"⬆️ Scenario **{sid}** committed to master ({len(changed)} object(s)).")
    return (s, f"Committed **{sid}** onto `master`: {len(changed)} object(s) updated — "
               f"{', '.join(changed[:8])}. The audit trail records each edit as originating "
               f"from a scenario merge.",
            render_audit(s), "\n".join(s.log))


def scenario_discard(s: Session, sid: str):
    if sid:
        s.scenarios.discard(sid)
        s.note(f"🗑️ Scenario **{sid}** discarded. Live Ontology untouched.")
    return (s, "Scenario discarded. Nothing reached the live Ontology or any source system.",
            gr.update(choices=list(s.scenarios.active), value=None), "\n".join(s.log))


# -- closed loop -----------------------------------------------------------

def close_loop(s: Session, outcome: str, actual_hours: float, downtime: float, cost: float):
    if not s.active_alert:
        return s, "_No alert in play._", render_metrics(s), "\n".join(s.log)
    rec = s.recommendation
    alert = s.store.get("Alert", s.active_alert)
    machine = s.store.get("Machine", alert["machineId"])
    rul = predict_rul(s.sim, alert["sensorId"])
    accepted = outcome.startswith("Followed")
    protected = 0.0
    if accepted:
        protected = max(0.0, (machine["unplannedRepairHours"] - downtime)) * \
                    machine["unitsPerHour"] * machine["marginPerUnitUsd"]
    r = s.engine.apply("recordAfterActionReport", {
        "alertId": s.active_alert, "machineId": machine["machineId"],
        "failureModeId": "FM-BRG-OR-SPALL", "signature": alert["signature"],
        "decision": outcome, "recommendationAccepted": accepted,
        "predictedRulHours": rul.get("remainingUsefulLifeHours"),
        "actualHoursToFailure": actual_hours, "downtimeHours": downtime,
        "costUsd": cost, "revenueProtectedUsd": round(protected, 0),
        "decisionLatencySeconds": alert.get("triageLatencySeconds"),
        "notes": f"Recorded from the operator console. Agent mode: "
                 f"{rec.mode if rec else 'n/a'}.",
    }, actor="j.reyes", actor_role="maintenance_planner")
    s.note("🔁 After Action Report written — the next matching alert will retrieve it.")
    return (s, r.message + "\n\nThis AAR is now part of the Ontology. Re-run the agent on a "
               "similar alert and it will cite this case, and the RUL model's measured error "
               "will shift accordingly.",
            render_metrics(s), "\n".join(s.log))


# -- foundry api tab -------------------------------------------------------

API_ENDPOINTS = {
    "GET /ontologies/{ontology}": lambda s: (api.curl_for(""), api.get_ontology()),
    "GET /objectTypes": lambda s: (api.curl_for("/objectTypes"),
                                   {"data": api.list_object_types()["data"][:2],
                                    "…": f"{len(OBJECT_TYPES)} object types total"}),
    "GET /objectTypes/Machine": lambda s: (api.curl_for("/objectTypes/Machine"),
                                           api.get_object_type("Machine")),
    "GET /objectTypes/Machine/outgoingLinkTypes":
        lambda s: (api.curl_for("/objectTypes/Machine/outgoingLinkTypes"),
                   api.list_outgoing_link_types("Machine")),
    "GET /objects/Machine/MCH-2207": lambda s: (api.curl_for("/objects/Machine/MCH-2207"),
                                                api.get_object(s.store, "Machine", "MCH-2207")),
    "GET /objects/Machine/MCH-2207/links/productionOrders":
        lambda s: (api.curl_for("/objects/ProductionLine/L2/links/productionOrders"),
                   api.list_linked_objects(s.store, "ProductionLine", "L2", "productionOrders")),
    "POST /objects/Alert/search": lambda s: (
        api.curl_for("/objects/Alert/search", "POST", {"where": {"status": "open"}}),
        api.search_objects(s.store, "Alert", {"status": "open"})),
    "GET /actionTypes": lambda s: (api.curl_for("/actionTypes"),
                                   api.list_action_types(ACTION_TYPES)),
    "POST /actions/createWorkOrder/apply": lambda s: (
        api.curl_for("/actions/createWorkOrder/apply", "POST",
                     api.apply_action_request("createWorkOrder", {"machineId": "MCH-2207"})),
        api.apply_action_response(s.engine.apply(
            "createWorkOrder",
            {"machineId": "MCH-2207", "summary": "API example — dry run",
             "scheduledStart": iso(now() + dt.timedelta(hours=6)), "durationHours": 3.5},
            branch=_api_scratch(s)))),
}


def _api_scratch(s: Session) -> str:
    if "api-scratch" not in s.store.branches:
        s.store.fork("api-scratch", "master", description="Read-only API demo branch")
    return "api-scratch"


def call_api(s: Session, endpoint: str):
    curl, body = API_ENDPOINTS[endpoint](s)
    return curl, json.dumps(body, indent=2, default=str)[:12000]


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

CSS = """
.gradio-container {max-width: 1180px !important}
footer {display:none !important}
"""

EXTENSIBILITY = """
## The same Ontology, the next six workflows

Nothing below needs a new data pipeline. Each one adds objects, links or actions to
the model that already exists — which is the compounding-value argument for the
Ontology over another dashboard.

| Next workflow | What it adds | What it reuses |
|---|---|---|
| **Supplier disruption response** | `Shipment`, `PurchaseOrder` objects; a delay event feed | Part → Supplier → InventoryItem links, revenue-at-risk query, approval policy |
| **Spares optimisation** | A stocking-policy model, `ReorderProposal` action | Failure modes, consumption history from work orders, lead times |
| **Energy and OEE** | Power meter series on `Machine` | The same asset objects, line links and margin-per-hour query |
| **Quality escapes** | `InspectionResult`, `LotGenealogy` | Machine → ProductionOrder → Customer traversal for containment scope |
| **Labour planning** | Skills matrix, certification expiry on `Technician` | Maintenance windows query, shift availability |
| **Capital planning** | `AssetLifecyclePlan` | Run hours, failure history, After Action Reports as the evidence base |

**Why the cost curve bends.** The first workflow paid for the semantic layer: assets,
lines, orders, parts, suppliers, technicians and the links between them. The second
workflow inherits that layer and only pays for its own objects and actions. Ontology
Queries, the approval policy, the audit trail, the writeback plumbing and the agent's
tool surface are all shared. In practice that means workflow two is weeks rather than
quarters, and every closed decision keeps improving the ones already live.

**What makes this an operating system rather than an analytics layer**

1. It is *writable*. The screen you make the decision on is the screen that changes SAP.
2. It is *governed*. Every action is typed, validated, permissioned, approvable and audited.
3. It is *simulatable*. Scenarios fork the model so you can be wrong safely.
4. It *learns*. Outcomes land back as objects and are retrieved on the next decision.
5. It is *agent-ready*. The agent's tools are ontology operations, so its reasoning is
   over a connected model of the business rather than retrieval over documents.
"""

ARCHITECTURE = """
## How the loop closes

```
  Historian / OPC-UA          SAP PM · SAP MM · MES · Ariba · Workday
        │  stream                        │  batch + writeback
        ▼                                ▼
  ┌────────────────────────────────────────────────────┐
  │                 ONTOLOGY (objects + links)         │
  │  Machine ─ Sensor ─ Alert ─ FailureMode            │
  │     │        │                  │                  │
  │  ProductionLine ─ ProductionOrder     Part ─ Supplier
  │     │                                  │           │
  │  WorkOrder ─ Technician ─────── InventoryItem      │
  │     └──────── AfterActionReport ── AuditEvent      │
  └───────┬───────────────┬──────────────┬─────────────┘
          │ queries       │ scenarios    │ actions
          ▼               ▼              ▼
   RUL · revenue-at-  copy-on-write   typed · validated
   risk · parts ·     branches for    permissioned ·
   windows            what-if         approved · audited
          └───────────────┴──────────────┘
                          │
                   Disruption Bot (AIP Logic pattern)
                   reasons over objects, proposes actions
                          │
                   Operator console → approve / override
                          │
                   Writeback → SAP PM / MM / MES
                          │
                   Outcome → AfterActionReport → next decision
```

**Data → logic → action → learning, with a human in the loop at exactly the points
where the blast radius justifies one.**
"""

THEME = gr.themes.Soft(primary_hue="blue", neutral_hue="slate")

with gr.Blocks(title="Plant Ops Ontology — Predictive Maintenance PoC",
               fill_width=True) as demo:

    S = gr.State()

    gr.Markdown(
        "# 🏭 Plant Operations Ontology — predictive maintenance digital twin\n"
        "One decision workflow, end to end: **streaming alert → agent diagnosis → "
        "costed options → governed work order and parts reservation → writeback → "
        "closed-loop learning.** Synthetic data, Foundry-shaped APIs.")

    with gr.Row():
        user = gr.Dropdown(list(USERS), value=list(USERS)[1], label="Signed in as",
                           scale=3, info="Role changes what you are allowed to apply.")
        mode = gr.Dropdown(["Deterministic planner (no API key needed)",
                            "Hugging Face (set HF_TOKEN)"],
                           value="Deterministic planner (no API key needed)",
                           label="Agent reasoning mode", scale=3)

    with gr.Tabs():
        # ---------------- Ops console ----------------
        with gr.Tab("1 · Ops console"):
            with gr.Row():
                with gr.Column(scale=5):
                    plant_md = gr.Markdown()
                    with gr.Row():
                        tick_btn = gr.Button("▶ Advance plant 30 min", variant="secondary")
                        tick_btn_4 = gr.Button("⏩ Advance 4 h")
                        stream = gr.Checkbox(False, label="Live stream (auto-tick)")
                    alerts_df = gr.Dataframe(label="Alert feed", interactive=False, wrap=True)
                    sensor_dd = gr.Dropdown(label="Telemetry channel")
                    chart = gr.LinePlot(x="time", y="value", color="series",
                                        title="Sensor trend vs thresholds (72 h)",
                                        height=260, x_title="", y_title="")
                with gr.Column(scale=4):
                    impact_md = gr.Markdown()
                    with gr.Row():
                        alert_box = gr.Textbox(label="Focus alert", value="ALR-0001", scale=2)
                        focus_btn = gr.Button("Focus", scale=1)
                    gr.Markdown("**Session log**")
                    log_md = gr.Markdown()

        # ---------------- Object explorer ----------------
        with gr.Tab("2 · Object explorer"):
            gr.Markdown("Semantic unification: every object carries its source system, "
                        "and every link is a traversal you can follow without writing a join.")
            with gr.Row():
                otype_dd = gr.Dropdown(list(OBJECT_TYPES), value="Machine", label="Object type")
                opk_dd = gr.Dropdown(label="Object")
            with gr.Row():
                obj_md = gr.Markdown()
                links_df = gr.Dataframe(label="Outgoing links (360° view)",
                                        interactive=False, wrap=True)

        # ---------------- Agent ----------------
        with gr.Tab("3 · Disruption Bot"):
            with gr.Row():
                run_btn = gr.Button("🤖 Diagnose the focused alert", variant="primary", scale=2)
                submit_btn = gr.Button("📤 Submit the proposed plan (governed)", scale=2)
            rec_md = gr.Markdown("_Run the agent to see a recommendation._")
            with gr.Accordion("Ontology tool-use trace (what the agent actually called)",
                              open=False):
                trace_df = gr.Dataframe(interactive=False, wrap=True)
            gr.Markdown("### Options the agent compared")
            options_df = gr.Dataframe(interactive=False, wrap=True)
            gr.Markdown("### Proposed action plan")
            plan_df = gr.Dataframe(interactive=False, wrap=True)
            submit_md = gr.Markdown()

        # ---------------- Scenarios ----------------
        with gr.Tab("4 · Scenarios"):
            gr.Markdown("Scenarios fork the Ontology copy-on-write. Actions applied to a "
                        "branch are validated and audited but never touch live objects or "
                        "call a source system.")
            with gr.Row():
                scn_label = gr.Textbox(label="Scenario name",
                                       value="Wait for the changeover window")
                scn_new = gr.Button("🧪 Create scenario")
                scn_dd = gr.Dropdown(label="Active scenario", choices=[])
            with gr.Row():
                scn_action = gr.Dropdown(list(ACTION_TYPES), value="rescheduleProductionOrder",
                                         label="Action to simulate")
                scn_params = gr.Textbox(
                    label="Parameters (JSON)", lines=3,
                    value='{"productionOrderId": "PO-8843", "targetLineId": "L3",\n'
                          ' "reason": "Free L2 for the maintenance window"}')
            with gr.Row():
                scn_apply = gr.Button("Simulate on branch", variant="primary")
                scn_cmp = gr.Button("Compare options")
                scn_commit = gr.Button("⬆️ Commit to live Ontology")
                scn_discard = gr.Button("🗑️ Discard")
            scn_msg = gr.Markdown()
            scn_diff = gr.Dataframe(label="Branch diff vs master", interactive=False, wrap=True)
            scn_cmp_md = gr.Markdown()

        # ---------------- Approvals ----------------
        with gr.Tab("5 · Actions & approvals"):
            gr.Markdown("### Awaiting human decision")
            pending_df = gr.Dataframe(interactive=False, wrap=True)
            with gr.Row():
                apr_id = gr.Textbox(label="Approval ID", value="APR-0001", scale=2)
                apr_reason = gr.Textbox(label="Rejection reason (if rejecting)", scale=3)
                apr_ok = gr.Button("✅ Approve & write back", variant="primary", scale=1)
                apr_no = gr.Button("🚫 Reject", scale=1)
            apr_msg = gr.Markdown()
            with gr.Accordion("Action type catalogue (validation, policy, writeback target)",
                              open=False):
                gr.Dataframe(
                    pd.DataFrame([{
                        "Action": a.api_name, "Modifies": ", ".join(a.modifies),
                        "Parameters": ", ".join(
                            f"{p.name}{'' if p.required else '?'}" for p in a.parameters),
                        "Writeback": a.writeback_target,
                    } for a in ACTION_TYPES.values()]),
                    interactive=False, wrap=True)
            gr.Markdown("### Simulated writeback payloads")
            hooks_md = gr.Markdown()

        # ---------------- Audit ----------------
        with gr.Tab("6 · Audit trail"):
            gr.Markdown("Every application, rejection, approval and simulation, with actor, "
                        "parameters, affected object RIDs, branch and writeback target.")
            audit_refresh = gr.Button("🔄 Refresh")
            audit_df = gr.Dataframe(interactive=False, wrap=True)

        # ---------------- Metrics / closed loop ----------------
        with gr.Tab("7 · Metrics & closed loop"):
            metrics_md = gr.Markdown()
            gr.Markdown("### Close the loop")
            gr.Markdown("Record what actually happened. The report becomes an Ontology object "
                        "that the agent retrieves on the next matching signature.")
            with gr.Row():
                outcome = gr.Dropdown(
                    ["Followed recommendation — repaired in window",
                     "Overridden — deferred, ran to failure"],
                    value="Followed recommendation — repaired in window", label="Outcome")
                actual_h = gr.Number(value=16.0, label="Actual hours to failure (observed)")
                down_h = gr.Number(value=3.6, label="Downtime hours")
                cost = gr.Number(value=1380.0, label="Total cost (USD)")
            loop_btn = gr.Button("🔁 Record After Action Report", variant="primary")
            loop_md = gr.Markdown()

        # ---------------- API ----------------
        with gr.Tab("8 · Foundry v2 API"):
            gr.Markdown(
                "The backing store is a stand-in, but the request and response shapes follow "
                f"`/api/v2/ontologies/{ONTOLOGY_API_NAME}/…` so this PoC can be repointed at a "
                "real Foundry stack by swapping one module.")
            ep = gr.Dropdown(list(API_ENDPOINTS), value=list(API_ENDPOINTS)[0],
                             label="Endpoint")
            call_btn = gr.Button("Send", variant="primary")
            curl_code = gr.Code(label="Request", language="shell")
            resp_code = gr.Code(label="Response", language="json")

        # ---------------- Story ----------------
        with gr.Tab("9 · Architecture & extensibility"):
            gr.Markdown(ARCHITECTURE)
            gr.Markdown(EXTENSIBILITY)

    # -- wiring ------------------------------------------------------------
    demo.load(boot, outputs=[S, plant_md, alerts_df, impact_md, sensor_dd, chart,
                             log_md, metrics_md])

    tick_btn.click(lambda s, sl: advance(s, 30, sl), [S, sensor_dd],
                   [S, plant_md, alerts_df, impact_md, chart, log_md])
    tick_btn_4.click(lambda s, sl: advance(s, 240, sl), [S, sensor_dd],
                     [S, plant_md, alerts_df, impact_md, chart, log_md])
    timer = gr.Timer(4.0, active=False)
    stream.change(lambda on: gr.Timer(4.0, active=on), stream, timer)
    timer.tick(lambda s, sl: advance(s, 30, sl), [S, sensor_dd],
               [S, plant_md, alerts_df, impact_md, chart, log_md])
    sensor_dd.change(pick_sensor, [S, sensor_dd], chart)
    focus_btn.click(pick_alert, [S, alert_box], [S, impact_md])

    otype_dd.change(explorer_objects, [S, otype_dd], opk_dd)
    opk_dd.change(explore, [S, otype_dd, opk_dd], [obj_md, links_df])

    run_btn.click(run_agent, [S, mode],
                  [S, rec_md, trace_df, options_df, plan_df, log_md])
    submit_btn.click(submit_plan, [S, user],
                     [S, submit_md, pending_df, audit_df, hooks_md, metrics_md, log_md])

    apr_ok.click(approve, [S, apr_id, user],
                 [S, apr_msg, pending_df, audit_df, hooks_md, metrics_md, log_md])
    apr_no.click(reject, [S, apr_id, user, apr_reason],
                 [S, apr_msg, pending_df, audit_df, hooks_md, metrics_md, log_md])
    audit_refresh.click(lambda s: render_audit(s), S, audit_df)

    scn_new.click(scenario_create, [S, scn_label], [S, scn_msg, scn_dd, log_md])
    scn_apply.click(scenario_apply, [S, scn_dd, scn_action, scn_params],
                    [S, scn_msg, scn_diff, log_md])
    scn_cmp.click(scenario_compare, [S, scn_dd], scn_cmp_md)
    scn_commit.click(scenario_commit, [S, scn_dd], [S, scn_msg, audit_df, log_md])
    scn_discard.click(scenario_discard, [S, scn_dd], [S, scn_msg, scn_dd, log_md])

    loop_btn.click(close_loop, [S, outcome, actual_h, down_h, cost],
                   [S, loop_md, metrics_md, log_md])
    call_btn.click(call_api, [S, ep], [curl_code, resp_code])


if __name__ == "__main__":
    demo.launch(theme=THEME, css=CSS)
