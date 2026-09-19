"""
agent.py — "Disruption Bot", the AIP-Logic-style agent.

The important property is not that an LLM is involved. It is that the agent's
tools are *Ontology operations* — object reads, link traversals, typed queries,
scenario simulation and governed action proposals — so its reasoning is over a
connected model of the plant rather than retrieval over documents.

Two execution modes, same tools and same trace format:
  • deterministic  — a planner that calls the real tools in a fixed order.
                     No API key needed; the demo always works and always tells
                     the same story.
  • hf             — set HF_TOKEN and the model drives tool selection.

Every tool call is recorded with its arguments, latency and result so the trace
can be shown to an auditor.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import logic
from logic import (ScenarioOption, check_parts_availability, compare_options,
                   compute_revenue_at_risk, default_options, find_maintenance_windows,
                   match_failure_mode, search_after_action_reports)
from ontology import LINK_TYPES, OBJECT_TYPES, OntologyStore, iso, now, outgoing_links
from simulator import PlantSimulator, predict_rul

AGENT_NAME = "Disruption Bot"
AGENT_RID = "ri.aip-logic.main.function.disruption-bot-v3"


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: Any
    latency_ms: float
    summary: str

    def to_row(self) -> list:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items() if v is not None)
        return [self.name, args[:90], self.summary, f"{self.latency_ms:.0f} ms"]


@dataclass
class ProposedAction:
    action_type: str
    parameters: dict
    rationale: str
    risk: str
    reversible: bool = True
    sequence: int = 0


@dataclass
class Recommendation:
    alert_id: str
    headline: str
    diagnosis: str
    confidence: float
    rationale: list[str]
    risks: list[str]
    proposed_actions: list[ProposedAction]
    option_table: list[dict]
    evidence_rids: list[str]
    trace: list[ToolCall] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    mode: str = "deterministic"

    def to_markdown(self) -> str:
        lines = [f"### {self.headline}", "",
                 f"**Diagnosis:** {self.diagnosis}  ",
                 f"**Confidence:** {self.confidence:.0%} · "
                 f"**Reasoned in:** {self.elapsed_seconds:.2f}s across {len(self.trace)} Ontology tool calls "
                 f"· **Mode:** {self.mode}", "", "**Why:**"]
        lines += [f"- {r}" for r in self.rationale]
        if self.risks:
            lines += ["", "**Risks and what would change my answer:**"]
            lines += [f"- {r}" for r in self.risks]
        if self.proposed_actions:
            lines += ["", "**Proposed action plan:**"]
            for a in self.proposed_actions:
                lines.append(f"{a.sequence}. `{a.action_type}` — {a.rationale}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class OntologyTools:
    """The agent's entire surface area. Nothing else is reachable."""

    def __init__(self, store: OntologyStore, sim: PlantSimulator, branch: str = "master"):
        self.store = store
        self.sim = sim
        self.branch = branch
        self.trace: list[ToolCall] = []
        self.touched: set[str] = set()

    # -- infra -------------------------------------------------------------
    def _record(self, name, args, result, t0, summary) -> Any:
        self.trace.append(ToolCall(name, args, result, (time.perf_counter() - t0) * 1000, summary))
        return result

    def call(self, name: str, arguments: dict) -> Any:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return {"error": f"Unknown tool '{name}'"}
        return fn(**arguments)

    # -- object access -----------------------------------------------------
    def tool_list_object_types(self) -> dict:
        t0 = time.perf_counter()
        res = {k: {"displayName": v["displayName"], "primaryKey": v["primaryKey"],
                   "links": sorted(outgoing_links(k))} for k, v in OBJECT_TYPES.items()}
        return self._record("list_object_types", {}, res, t0, f"{len(res)} object types")

    def tool_get_object(self, objectType: str, primaryKey: str,
                        expandLinks: list[str] | None = None) -> dict:
        t0 = time.perf_counter()
        obj = self.store.get(objectType, primaryKey, self.branch)
        if obj is None:
            return self._record("get_object", {"objectType": objectType, "primaryKey": primaryKey},
                                {"error": "not found"}, t0, "not found")
        out = {k: v for k, v in obj.items()}
        self.touched.add(obj["__rid"])
        for link in (expandLinks or []):
            try:
                rows = self.store.linked(objectType, primaryKey, link, self.branch)
            except KeyError as e:
                out[f"links.{link}"] = {"error": str(e)}
                continue
            out[f"links.{link}"] = rows
            self.touched.update(r["__rid"] for r in rows)
        return self._record("get_object",
                            {"objectType": objectType, "primaryKey": primaryKey,
                             "expandLinks": expandLinks},
                            out, t0,
                            f"{objectType} {primaryKey}" +
                            (f" + {len(expandLinks)} link(s)" if expandLinks else ""))

    def tool_search_objects(self, objectType: str, where: dict | None = None,
                            limit: int = 25) -> dict:
        t0 = time.perf_counter()
        rows = self.store.search(objectType, where=where, limit=limit, branch=self.branch)
        self.touched.update(r["__rid"] for r in rows)
        return self._record("search_objects", {"objectType": objectType, "where": where},
                            {"data": rows, "count": len(rows)}, t0, f"{len(rows)} {objectType}(s)")

    def tool_traverse_link(self, objectType: str, primaryKey: str, link: str) -> dict:
        t0 = time.perf_counter()
        try:
            rows = self.store.linked(objectType, primaryKey, link, self.branch)
        except KeyError as e:
            return self._record("traverse_link", {"objectType": objectType, "link": link},
                                {"error": str(e)}, t0, "invalid link")
        self.touched.update(r["__rid"] for r in rows)
        return self._record("traverse_link",
                            {"objectType": objectType, "primaryKey": primaryKey, "link": link},
                            {"data": rows, "count": len(rows)}, t0,
                            f"{objectType}.{link} → {len(rows)} object(s)")

    # -- typed ontology queries -------------------------------------------
    def tool_run_query(self, queryApiName: str, parameters: dict | None = None) -> dict:
        t0 = time.perf_counter()
        p = parameters or {}
        q = {
            "predictRemainingUsefulLife":
                lambda: predict_rul(self.sim, p["sensorId"]),
            "matchFailureMode":
                lambda: match_failure_mode(self.store, p["signature"], p["assetClass"], self.branch),
            "checkPartsAvailability":
                lambda: check_parts_availability(self.store, p["partSkus"], p["siteId"], self.branch),
            "findMaintenanceWindows":
                lambda: find_maintenance_windows(self.store, self.sim, p["machineId"],
                                                 float(p.get("repairHours", 3.0)), self.branch),
            "computeRevenueAtRisk":
                lambda: compute_revenue_at_risk(self.store, p["machineId"],
                                                float(p["downtimeHours"]), self.branch),
        }.get(queryApiName)
        if q is None:
            return self._record("run_query", {"queryApiName": queryApiName},
                                {"error": f"Unknown query '{queryApiName}'"}, t0, "unknown query")
        res = q()
        summaries = {
            "predictRemainingUsefulLife": lambda r: (
                f"RUL {r.get('remainingUsefulLifeHours')}h @ conf {r.get('confidence')}"
                if r.get("status") == "degrading" else r.get("status")),
            "matchFailureMode": lambda r: (
                f"{r['bestMatch']['name']} ({r['bestMatch']['matchScore']:.0%})"
                if r.get("bestMatch") else "no match"),
            "checkPartsAvailability": lambda r: (
                "all parts on site" if r["allAvailableOnSite"] else f"blocked on {r['blockers']}"),
            "findMaintenanceWindows": lambda r: f"{len(r['windows'])} candidate window(s)",
            "computeRevenueAtRisk": lambda r: f"${r['totalRevenueAtRiskUsd']:,.0f} at risk",
        }
        return self._record("run_query", {"queryApiName": queryApiName, "parameters": p},
                            res, t0, summaries[queryApiName](res))

    def tool_search_after_action_reports(self, signature: str | None = None,
                                         machineId: str | None = None,
                                         failureModeId: str | None = None) -> dict:
        t0 = time.perf_counter()
        res = search_after_action_reports(self.store, signature, machineId, failureModeId, self.branch)
        return self._record("search_after_action_reports",
                            {"signature": signature, "machineId": machineId,
                             "failureModeId": failureModeId},
                            res, t0, f"{res['count']} prior case(s)")

    def tool_simulate_scenario(self, machineId: str, sensorId: str,
                               failureModeId: str) -> dict:
        t0 = time.perf_counter()
        rul = predict_rul(self.sim, sensorId)
        fm = self.store.get("FailureMode", failureModeId, self.branch)
        windows = find_maintenance_windows(self.store, self.sim, machineId,
                                           float(fm["repairHours"]), self.branch)
        opts = default_options(self.sim, self.store, machineId, windows)
        rows = compare_options(self.store, self.sim, machineId, rul, fm, opts)
        best = next(r for r in rows if r["recommended"])
        return self._record("simulate_scenario",
                            {"machineId": machineId, "failureModeId": failureModeId},
                            {"options": rows, "recommended": best["scenarioId"],
                             "windows": windows},
                            t0,
                            f"{len(rows)} options, best={best['label']} "
                            f"(${best['totalExpectedCostUsd']:,.0f})")

    def tool_propose_action(self, actionType: str, parameters: dict,
                            rationale: str, risk: str = "") -> dict:
        t0 = time.perf_counter()
        return self._record("propose_action", {"actionType": actionType},
                            {"actionType": actionType, "parameters": parameters,
                             "rationale": rationale, "risk": risk},
                            t0, f"proposed {actionType}")


# Provider-compatible tool schemas (also documents the surface for readers).
TOOL_SCHEMAS = [
    {"name": "list_object_types",
     "description": "List every Ontology object type with its primary key and outgoing links.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_object",
     "description": "Fetch one Ontology object by primary key, optionally expanding linked objects.",
     "input_schema": {"type": "object", "properties": {
         "objectType": {"type": "string"}, "primaryKey": {"type": "string"},
         "expandLinks": {"type": "array", "items": {"type": "string"}}},
         "required": ["objectType", "primaryKey"]}},
    {"name": "search_objects",
     "description": "Search objects of a type with exact-match property filters.",
     "input_schema": {"type": "object", "properties": {
         "objectType": {"type": "string"}, "where": {"type": "object"},
         "limit": {"type": "integer"}}, "required": ["objectType"]}},
    {"name": "traverse_link",
     "description": "Follow a link from one object to its related objects.",
     "input_schema": {"type": "object", "properties": {
         "objectType": {"type": "string"}, "primaryKey": {"type": "string"},
         "link": {"type": "string"}}, "required": ["objectType", "primaryKey", "link"]}},
    {"name": "run_query",
     "description": ("Execute a typed Ontology Query. Available: predictRemainingUsefulLife"
                     "{sensorId}, matchFailureMode{signature,assetClass}, checkPartsAvailability"
                     "{partSkus,siteId}, findMaintenanceWindows{machineId,repairHours}, "
                     "computeRevenueAtRisk{machineId,downtimeHours}."),
     "input_schema": {"type": "object", "properties": {
         "queryApiName": {"type": "string"}, "parameters": {"type": "object"}},
         "required": ["queryApiName"]}},
    {"name": "search_after_action_reports",
     "description": "Retrieve outcomes of prior similar decisions (closed-loop memory).",
     "input_schema": {"type": "object", "properties": {
         "signature": {"type": "string"}, "machineId": {"type": "string"},
         "failureModeId": {"type": "string"}}}},
    {"name": "simulate_scenario",
     "description": "Compare intervention options on a sandboxed branch and return expected costs.",
     "input_schema": {"type": "object", "properties": {
         "machineId": {"type": "string"}, "sensorId": {"type": "string"},
         "failureModeId": {"type": "string"}},
         "required": ["machineId", "sensorId", "failureModeId"]}},
    {"name": "propose_action",
     "description": ("Propose one governed Ontology Action for human review. Available action types: "
                     "acknowledgeAlert, createWorkOrder, reservePartsForWorkOrder, "
                     "rescheduleProductionOrder, deferMaintenance, notifyCustomer, "
                     "recordAfterActionReport. You may never apply actions yourself."),
     "input_schema": {"type": "object", "properties": {
         "actionType": {"type": "string"}, "parameters": {"type": "object"},
         "rationale": {"type": "string"}, "risk": {"type": "string"}},
         "required": ["actionType", "parameters", "rationale"]}},
]

SYSTEM_PROMPT = f"""You are {AGENT_NAME}, a maintenance decision agent operating on a
Palantir-style Ontology for a machining plant.

You reason over connected objects, not documents. Always ground every claim in a tool
result. Never invent an object, part number, cost or date.

Method:
1. Read the alert and its machine, including linked sensors, line and production orders.
2. Run predictRemainingUsefulLife on the alerting sensor.
3. Match the observed multi-sensor signature to a FailureMode. Do not assume the worst
   case: a temperature-only excursion is not a bearing failure.
4. Retrieve after action reports for the same signature. Say explicitly what happened
   last time and whether the RUL model ran optimistic or conservative.
5. Check parts availability and maintenance windows before proposing any schedule.
6. Simulate the options and compare expected cost.
7. Propose a sequenced action plan with propose_action. You have no authority to apply
   actions; a human approves anything that stops a critical machine, moves a P1 order,
   defers a flagged degradation, or contacts a customer.

Be concise and quantitative. State your confidence and what evidence would change your
recommendation."""


# ---------------------------------------------------------------------------
# Deterministic planner
# ---------------------------------------------------------------------------

def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "unknown"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h/24:.1f} days"


def diagnose(store: OntologyStore, sim: PlantSimulator, alert_id: str,
             mode: str = "deterministic", branch: str = "master") -> Recommendation:
    """Entry point used by the UI. Falls back to deterministic if Hugging Face is unavailable."""
    if mode == "hf" and (os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")):
        try:
            return _diagnose_with_hf(store, sim, alert_id, branch)
        except Exception as exc:  # noqa: BLE001 - demo must never hard-fail
            rec = _diagnose_deterministic(store, sim, alert_id, branch)
            rec.risks.append(f"Hugging Face-backed reasoning unavailable ({type(exc).__name__}); "
                             f"fell back to the deterministic planner.")
            return rec
    return _diagnose_deterministic(store, sim, alert_id, branch)


def _diagnose_deterministic(store: OntologyStore, sim: PlantSimulator, alert_id: str,
                            branch: str = "master") -> Recommendation:
    t_start = time.perf_counter()
    tools = OntologyTools(store, sim, branch)

    alert = tools.tool_get_object("Alert", alert_id, ["alertMachine"])
    machine = tools.tool_get_object("Machine", alert["machineId"],
                                    ["sensors", "line", "workOrders"])
    tools.tool_traverse_link("ProductionLine", machine["lineId"], "productionOrders")

    rul = tools.tool_run_query("predictRemainingUsefulLife", {"sensorId": alert["sensorId"]})
    fm_match = tools.tool_run_query("matchFailureMode",
                                    {"signature": alert["signature"],
                                     "assetClass": machine["assetClass"]})
    best_fm = fm_match["bestMatch"]
    history = tools.tool_search_after_action_reports(signature=alert["signature"],
                                                     machineId=machine["machineId"],
                                                     failureModeId=best_fm["failureModeId"])
    parts = tools.tool_run_query("checkPartsAvailability",
                                 {"partSkus": best_fm["requiredPartSkus"],
                                  "siteId": machine["siteId"]})
    windows = tools.tool_run_query("findMaintenanceWindows",
                                   {"machineId": machine["machineId"],
                                    "repairHours": best_fm["repairHours"]})
    unplanned_risk = tools.tool_run_query("computeRevenueAtRisk",
                                          {"machineId": machine["machineId"],
                                           "downtimeHours": machine["unplannedRepairHours"]})
    planned_risk = tools.tool_run_query("computeRevenueAtRisk",
                                        {"machineId": machine["machineId"],
                                         "downtimeHours": best_fm["repairHours"]})
    sim_result = tools.tool_simulate_scenario(machine["machineId"], alert["sensorId"],
                                              best_fm["failureModeId"])
    options = sim_result["options"]
    best = next(r for r in options if r["recommended"])
    worst = max(options, key=lambda r: r["totalExpectedCostUsd"])

    rul_h = rul.get("remainingUsefulLifeHours")
    low_severity = best_fm["matchScore"] < 0.5 or rul.get("status") != "degrading"

    # ---- rationale, all of it sourced from the tool results above ---------
    rationale = [
        f"**Signal.** {alert['sensorId']} is at {rul.get('currentValue', '—')} "
        f"{store.get('Sensor', alert['sensorId'])['unit']} against a warn limit of "
        f"{rul.get('warnThreshold')} and a critical limit of {rul.get('critThreshold')}. "
        f"The excess over baseline is doubling every "
        f"{rul.get('doublingTimeHours', '—')}h (R²={rul.get('confidence')}), which projects "
        f"a critical breach in **{_fmt_hours(rul_h)}**"
        + (f" at {rul.get('predictedFailureAt')}." if rul_h else "."),
        f"**Diagnosis.** The multi-sensor signature `{alert['signature']}` matches "
        f"*{best_fm['name']}* at {best_fm['matchScore']:.0%}. "
        f"{best_fm['detectionNotes']}",
        f"**Business exposure.** {machine['name']} sits on line {machine['lineId']} carrying "
        f"{len(unplanned_risk['orders'])} open production order(s). At "
        f"${unplanned_risk['contributionMarginPerHourUsd']:,.0f}/h of contribution margin, an "
        f"unplanned {machine['unplannedRepairHours']:.0f}h stop costs "
        f"${unplanned_risk['totalRevenueAtRiskUsd']:,.0f} versus "
        f"${planned_risk['totalRevenueAtRiskUsd']:,.0f} for a planned "
        f"{best_fm['repairHours']:.1f}h repair.",
        f"**Parts.** " + ("; ".join(
            f"{l['partSku']} — {l['availableOnSite']} on site at {l['binLocation']}"
            + (f" (plus {l['otherSites'][0]['available']} at {l['otherSites'][0]['siteId']})"
               if l.get("otherSites") else "")
            + f", otherwise {l['standardLeadTimeDays']:.0f}-day lead from {l['supplier']}"
            for l in parts["lines"] if l.get("found")) or "no parts required"),
    ]

    if history["count"]:
        worst_case = max(history["matches"], key=lambda r: float(r.get("downtimeHours") or 0))
        rationale.append(
            f"**Precedent.** {history['count']} prior case(s) with this signature. "
            f"When the recommendation was accepted, mean downtime was "
            f"{history['avgDowntimeWhenAccepted']}h at ${history['avgCostWhenAccepted']:,.0f}; "
            f"when it was overridden, {history['avgDowntimeWhenOverridden']}h at "
            f"${history['avgCostWhenOverridden']:,.0f}. {worst_case['aarId']}: "
            f"{worst_case['notes']}"
            + (f" RUL model calibration: {history['rulModelBias']}."
               if history["rulModelBias"] else ""))

    breached = ", ".join(best["ordersBreached"]) or "none"
    rationale.append(
        f"**Options compared.** " + " · ".join(
            f"{o['label']}: P(fail)={o['failureProbability']:.0%}, expected downtime "
            f"{o['expectedDowntimeHours']:.1f}h, expected cost ${o['totalExpectedCostUsd']:,.0f}"
            for o in options)
        + f". Recommended option avoids ${best['deltaVsWorstUsd']:,.0f} versus the worst "
          f"course of action; orders breached under the recommendation: {breached}.")

    risks = []
    if rul.get("confidence", 0) < 0.85:
        risks.append(f"RUL fit confidence is {rul.get('confidence')}. If the trend flattens over "
                     f"the next 4 hours the case for stopping early weakens materially.")
    if history.get("rulModelBias") and "optimistic" in history["rulModelBias"]:
        risks.append("Historically this model has failed *sooner* than predicted on this asset — "
                     "treat the RUL as an upper bound.")
    if not parts["allAvailableOnSite"]:
        risks.append(f"Parts {parts['blockers']} are not on site; the plan depends on an expedite.")
    if machine["criticality"] == "critical":
        risks.append("Machine is criticality=critical, so no production-affecting action here can "
                     "be auto-applied — a named planner must approve.")
    onsite_line = next((l for l in parts["lines"] if l.get("availableOnSite") == 1), None)
    if onsite_line:
        risks.append(f"Only one unreserved {onsite_line['partSku']} remains on site; if it is "
                     f"consumed elsewhere the lead time becomes "
                     f"{onsite_line['standardLeadTimeDays']:.0f} days "
                     f"({onsite_line['expediteLeadTimeDays']:.0f} expedited, "
                     f"${onsite_line['expediteFeeUsd']:,.0f} fee).")

    # ---- proposed action plan --------------------------------------------
    proposals: list[ProposedAction] = []
    seq = 1
    tools.tool_propose_action("acknowledgeAlert", {"alertId": alert_id},
                              "Stamp triage latency and stop duplicate paging.")
    proposals.append(ProposedAction("acknowledgeAlert", {"alertId": alert_id},
                                    "Stamp triage latency and take ownership of the alert.",
                                    "None — reversible, no external writeback.", True, seq))
    seq += 1

    if low_severity:
        proposals.append(ProposedAction(
            "deferMaintenance",
            {"alertId": alert_id,
             "justification": f"Signature matches {best_fm['name']} at only "
                              f"{best_fm['matchScore']:.0%} and the trend is not exponential; "
                              f"monitor and batch with the next PM."},
            "Evidence does not support a production-affecting stop.",
            "Requires human sign-off: deferral accepts run-to-failure risk.", True, seq))
        headline = f"Monitor — no stop justified on {machine['name']}"
    else:
        window = next(w for w in sim_result["windows"]["windows"]
                      if abs(w["hoursFromNow"] - (best["interventionHoursFromNow"] or 0)) < 0.51)
        start_at = window["startsAt"]
        parts_cost = sum(l["unitCostUsd"] for l in parts["lines"] if l.get("found"))
        wo_params = {
            "machineId": machine["machineId"],
            "summary": f"CM — replace spindle bearing set ({best_fm['name']})",
            "scheduledStart": start_at,
            "durationHours": best_fm["repairHours"],
            "orderType": "CM", "priority": "urgent",
            "technicianId": window.get("technicianId"),
            "alertId": alert_id, "failureModeId": best_fm["failureModeId"],
            "partsCostUsd": parts_cost,
        }
        tools.tool_propose_action("createWorkOrder", wo_params,
                                  "Intervene before the predicted critical breach.")
        proposals.append(ProposedAction(
            "createWorkOrder", wo_params,
            f"Repair at {start_at} ({best['label']}), {best_fm['repairHours']:.1f}h, "
            f"{window.get('technicianName', 'unassigned')}. Expected cost "
            f"${best['totalExpectedCostUsd']:,.0f} versus ${worst['totalExpectedCostUsd']:,.0f} "
            f"for the worst option.",
            "Stops a critical machine — requires planner approval. Writes back to SAP PM.",
            False, seq))
        seq += 1

        for line in parts["lines"]:
            if line.get("found") and line.get("inventoryId") and line["availableOnSite"] >= 1:
                p = {"inventoryId": line["inventoryId"], "quantity": 1}
                tools.tool_propose_action("reservePartsForWorkOrder", p,
                                          "Hard-allocate the last on-site unit.")
                proposals.append(ProposedAction(
                    "reservePartsForWorkOrder", p,
                    f"Reserve 1 × {line['partSku']} from {line['binLocation']} now "
                    f"({line['availableOnSite']} unreserved on site) so it cannot be consumed "
                    f"by another job while the work order waits for approval.",
                    "Low blast radius and reversible, so this auto-approves under policy.",
                    True, seq))
                seq += 1

        low_value = sorted(
            [o for o in unplanned_risk["orders"] if not o["contractualNotice"]],
            key=lambda o: float(o["revenueUsd"]))
        if low_value and best["ordersBreached"]:
            po = low_value[0]
            p = {"productionOrderId": po["productionOrderId"], "targetLineId": "L3",
                 "reason": f"Protect the {best_fm['repairHours']:.1f}h maintenance window on "
                           f"{machine['lineId']} without touching P1 work."}
            proposals.append(ProposedAction(
                "rescheduleProductionOrder", p,
                f"Move {po['productionOrderId']} ({po['customer']}, "
                f"${float(po['revenueUsd']):,.0f}, {po['priority']}) to L3 so the P1 aerospace "
                f"order keeps the line.",
                "Reallocation assumes L3 capacity — validate in the scenario before committing.",
                True, seq))
            seq += 1

        headline = (f"Stop {machine['name']} inside the next "
                    f"{_fmt_hours(best['interventionHoursFromNow'])} — "
                    f"${best['deltaVsWorstUsd']:,.0f} of avoidable exposure")

    confidence = round(min(0.97, 0.45 + 0.35 * float(rul.get("confidence") or 0)
                           + 0.25 * float(best_fm["matchScore"])), 2)

    return Recommendation(
        alert_id=alert_id,
        headline=headline,
        diagnosis=(f"{best_fm['name']} on {machine['name']} — "
                   f"{_fmt_hours(rul_h)} of remaining useful life"),
        confidence=confidence,
        rationale=rationale,
        risks=risks,
        proposed_actions=proposals,
        option_table=options,
        evidence_rids=sorted(tools.touched),
        trace=tools.trace,
        elapsed_seconds=time.perf_counter() - t_start,
        mode="deterministic planner (Ontology tool use)",
    )


# ---------------------------------------------------------------------------
# Hugging Face-backed mode
# ---------------------------------------------------------------------------

def _diagnose_with_hf(store: OntologyStore, sim: PlantSimulator, alert_id: str,
                      branch: str = "master", model: str = "meta-llama/Llama-3.1-8B-Instruct",
                      max_turns: int = 14) -> Recommendation:
    from huggingface_hub import InferenceClient  # imported lazily so the app runs without the SDK

    t_start = time.perf_counter()
    tools = OntologyTools(store, sim, branch)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")
    client = InferenceClient(model=model, token=token)

    messages = [{"role": "user", "content":
                 f"Alert {alert_id} has just been raised. Diagnose it and propose an action plan. "
                 f"Finish with a short written recommendation that states your confidence and "
                 f"the evidence that would change it."}]
    proposals: list[ProposedAction] = []
    final_text = ""

    for _ in range(max_turns):
        resp = client.chat_completion(messages=messages, tools=TOOL_SCHEMAS,
                                     max_tokens=2000, temperature=0.2)
        if not resp.get("choices"):
            break
        choice = resp["choices"][0]
        msg = choice.get("message", {})
        if "tool_calls" not in msg:
            final_text = msg.get("content", "")
            break
        results = []
        for tool_call in msg["tool_calls"]:
            fn = tool_call.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if not name:
                continue
            out = tools.call(name, args)
            if name == "propose_action":
                proposals.append(ProposedAction(
                    args["actionType"], args.get("parameters", {}),
                    args.get("rationale", ""), args.get("risk", ""),
                    sequence=len(proposals) + 1))
            results.append({"tool_call_id": tool_call.get("id"), "role": "tool",
                            "name": name, "content": json.dumps(out, default=str)[:12000]})
        messages.append({"role": "assistant", "content": msg.get("content", "")})
        messages.extend(results)

    option_table = next((c.result["options"] for c in tools.trace
                         if c.name == "simulate_scenario" and isinstance(c.result, dict)
                         and "options" in c.result), [])
    alert = store.get("Alert", alert_id, branch)
    machine = store.get("Machine", alert["machineId"], branch)

    return Recommendation(
        alert_id=alert_id,
        headline=f"Disruption Bot recommendation — {machine['name']}",
        diagnosis=final_text.split("\n")[0][:200] if final_text else "see rationale",
        confidence=0.0,
        rationale=[final_text or "(no text returned)"],
        risks=[],
        proposed_actions=proposals,
        option_table=option_table,
        evidence_rids=sorted(tools.touched),
        trace=tools.trace,
        elapsed_seconds=time.perf_counter() - t_start,
        mode=f"Hugging Face ({model}) with Ontology tool use",
    )
