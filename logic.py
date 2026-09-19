"""
logic.py — the Logic layer.

Two things live here, both of which are first-class Foundry concepts:

1. Ontology Queries: named, typed, auditable functions the agent calls by
   apiName. They traverse links rather than joining tables, which is what makes
   "a vibration reading" and "a $412k aerospace order" the same question.
2. Scenarios: copy-on-write branches of the Ontology where actions can be
   applied and compared without touching live state.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ontology import OntologyStore, iso, now
from simulator import PlantSimulator, predict_rul


def _parse(ts: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


# ---------------------------------------------------------------------------
# Ontology Queries
# ---------------------------------------------------------------------------

def match_failure_mode(store: OntologyStore, signature: str, asset_class: str,
                       branch: str = "master") -> dict:
    """Score FailureMode objects against an observed multi-sensor signature."""
    observed = {p.split(":")[0]: p.split(":")[1] for p in signature.split("+") if ":" in p}
    ranked = []
    for fm in store.search("FailureMode", where={"assetClass": asset_class}, branch=branch):
        expected = {p.split(":")[0]: p.split(":")[1] for p in fm["signature"].split("+") if ":" in p}
        keys = set(expected) | set(observed)
        hits = sum(1 for k in keys
                   if k in expected and k in observed
                   and (expected[k] == observed[k]
                        or {expected[k], observed[k]} == {"rising", "rising_exponential"}))
        score = hits / max(len(keys), 1)
        ranked.append({"failureModeId": fm["failureModeId"], "name": fm["name"],
                       "matchScore": round(score, 3), "signature": fm["signature"],
                       "typicalLeadTimeHours": fm["typicalLeadTimeHours"],
                       "repairHours": fm["repairHours"],
                       "requiredPartSkus": fm["requiredPartSkus"],
                       "escalationProbability": fm["escalationProbability"],
                       "detectionNotes": fm["detectionNotes"]})
    ranked.sort(key=lambda r: r["matchScore"], reverse=True)
    return {"observedSignature": signature, "candidates": ranked,
            "bestMatch": ranked[0] if ranked else None}


def check_parts_availability(store: OntologyStore, skus: list[str], site_id: str,
                             branch: str = "master") -> dict:
    """Link traversal: Part -> InventoryItem -> Supplier, with an expedite fallback."""
    out = []
    for sku in skus:
        part = store.get("Part", sku, branch)
        if not part:
            out.append({"partSku": sku, "found": False})
            continue
        supplier = store.get("Supplier", part["supplierId"], branch)
        rows = store.linked("Part", sku, "inventory", branch)
        onsite = [r for r in rows if r["siteId"] == site_id]
        offsite = [r for r in rows if r["siteId"] != site_id]
        available = sum(int(r["onHand"]) - int(r["reserved"]) for r in onsite)
        out.append({
            "partSku": sku, "found": True, "name": part["name"],
            "unitCostUsd": part["unitCostUsd"],
            "availableOnSite": available,
            "inventoryId": onsite[0]["inventoryId"] if onsite else None,
            "binLocation": onsite[0]["binLocation"] if onsite else None,
            "otherSites": [{"siteId": r["siteId"],
                            "available": int(r["onHand"]) - int(r["reserved"])} for r in offsite],
            "supplier": supplier["name"] if supplier else None,
            "standardLeadTimeDays": part["leadTimeDays"],
            "expediteLeadTimeDays": supplier["expediteLeadTimeDays"] if supplier else None,
            "expediteFeeUsd": supplier["expediteFeeUsd"] if supplier else None,
        })
    blockers = [r for r in out if r.get("found") and r["availableOnSite"] < 1]
    return {"siteId": site_id, "lines": out,
            "allAvailableOnSite": not blockers,
            "blockers": [b["partSku"] for b in blockers]}


def find_maintenance_windows(store: OntologyStore, sim: PlantSimulator, machine_id: str,
                             repair_hours: float, branch: str = "master") -> dict:
    """Candidate intervention times from technician availability and line changeovers."""
    machine = store.get("Machine", machine_id, branch)
    clock = sim.clock()
    options = []

    line = store.get("ProductionLine", machine["lineId"], branch)
    changeover = _parse(line["nextChangeoverAt"])
    if changeover:
        options.append({
            "windowId": "W-CHANGEOVER",
            "startsAt": iso(changeover),
            "hoursFromNow": round((changeover - clock).total_seconds() / 3600, 1),
            "type": "planned_changeover",
            "productionImpactHours": 0.0,
            "note": f"Existing {line['changeoverWindowHours']}h changeover on {line['lineId']} — "
                    f"repair fits inside it, zero incremental production loss.",
        })

    skilled = [t for t in store.all("Technician", branch)
               if "spindle_rebuild" in (t.get("skills") or [])
               and t["siteId"] == machine["siteId"]]
    for tech in sorted(skilled, key=lambda t: t["availableFrom"]):
        start = _parse(tech["availableFrom"])
        options.append({
            "windowId": f"W-{tech['technicianId']}",
            "startsAt": iso(start),
            "hoursFromNow": round((start - clock).total_seconds() / 3600, 1),
            "type": "unplanned_stop",
            "technicianId": tech["technicianId"],
            "technicianName": tech["name"],
            "shift": tech["shift"],
            "productionImpactHours": repair_hours,
            "note": f"{tech['name']} ({tech['shift']}) is the earliest spindle-qualified tech.",
        })

    options.sort(key=lambda o: o["hoursFromNow"])
    return {"machineId": machine_id, "repairHours": repair_hours, "windows": options}


def compute_revenue_at_risk(store: OntologyStore, machine_id: str, downtime_hours: float,
                            branch: str = "master") -> dict:
    """
    Machine -> ProductionLine -> ProductionOrder traversal.

    This is the query that turns maintenance into a business conversation: the
    same downtime number costs $3k on one line and $180k on another.
    """
    machine = store.get("Machine", machine_id, branch)
    line = store.get("ProductionLine", machine["lineId"], branch)
    orders = store.linked("ProductionLine", machine["lineId"], "productionOrders", branch)
    orders = [o for o in orders if o["status"] not in ("complete", "rescheduled")]

    hourly_units = float(machine["unitsPerHour"])
    margin_per_hour = hourly_units * float(machine["marginPerUnitUsd"])
    lost_units = hourly_units * downtime_hours
    margin_loss = margin_per_hour * downtime_hours

    at_risk, penalties = [], 0.0
    remaining_capacity_hours = 0.0
    for o in sorted(orders, key=lambda x: x["dueAt"]):
        hours_needed = float(o["unitsRemaining"]) / hourly_units
        remaining_capacity_hours += hours_needed
        due = _parse(o["dueAt"])
        slack_hours = (due - now()).total_seconds() / 3600 - remaining_capacity_hours
        slip = max(0.0, downtime_hours - slack_hours)
        penalty = (slip / 24.0) * float(o["latePenaltyUsdPerDay"] or 0)
        penalties += penalty
        at_risk.append({
            "productionOrderId": o["productionOrderId"], "customer": o["customer"],
            "priority": o["priority"], "revenueUsd": o["revenueUsd"],
            "dueAt": o["dueAt"], "unitsRemaining": o["unitsRemaining"],
            "slackHours": round(slack_hours, 1),
            "projectedSlipHours": round(slip, 1),
            "latePenaltyUsd": round(penalty, 0),
            "contractualNotice": o.get("contractualNotice", False),
            "wouldBreach": slip > 0,
        })

    return {
        "machineId": machine_id, "machineName": machine["name"], "lineId": line["lineId"],
        "downtimeHours": downtime_hours,
        "unitsLost": round(lost_units, 0),
        "contributionMarginPerHourUsd": round(margin_per_hour, 0),
        "marginLossUsd": round(margin_loss, 0),
        "latePenaltyUsd": round(penalties, 0),
        "totalRevenueAtRiskUsd": round(margin_loss + penalties, 0),
        "revenueExposedUsd": round(sum(float(o["revenueUsd"]) for o in orders
                                       if any(a["productionOrderId"] == o["productionOrderId"]
                                              and a["wouldBreach"] for a in at_risk)), 0),
        "orders": at_risk,
    }


def search_after_action_reports(store: OntologyStore, signature: str | None = None,
                                machine_id: str | None = None,
                                failure_mode_id: str | None = None,
                                branch: str = "master") -> dict:
    """Closed-loop memory: what happened last time this exact pattern appeared."""
    rows = store.all("AfterActionReport", branch)
    def rel(r):
        s = 0
        if signature and r.get("signature") == signature:
            s += 3
        if failure_mode_id and r.get("failureModeId") == failure_mode_id:
            s += 2
        if machine_id and r.get("machineId") == machine_id:
            s += 1
        return s
    scored = [(rel(r), r) for r in rows]
    hits = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0]

    accepted = [r for r in hits if r.get("recommendationAccepted")]
    overridden = [r for r in hits if r.get("recommendationAccepted") is False]
    def avg(rows_, key):
        vals = [float(r[key]) for r in rows_ if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "matches": hits,
        "count": len(hits),
        "avgDowntimeWhenAccepted": avg(accepted, "downtimeHours"),
        "avgDowntimeWhenOverridden": avg(overridden, "downtimeHours"),
        "avgCostWhenAccepted": avg(accepted, "costUsd"),
        "avgCostWhenOverridden": avg(overridden, "costUsd"),
        "rulModelBias": _rul_bias(hits),
    }


def _rul_bias(rows: list[dict]) -> str | None:
    pairs = [(float(r["predictedRulHours"]), float(r["actualHoursToFailure"]))
             for r in rows
             if r.get("predictedRulHours") and r.get("actualHoursToFailure")]
    if not pairs:
        return None
    err = sum(a - p for p, a in pairs) / len(pairs)
    direction = "conservative (fails later than predicted)" if err > 0 else "optimistic (fails sooner than predicted)"
    return f"mean error {err:+.1f}h across {len(pairs)} closed cases — model is {direction}"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@dataclass
class ScenarioOption:
    scenario_id: str
    label: str
    intervention_hours_from_now: float | None   # None = run to failure
    description: str


def failure_probability(rul_hours: float, intervention_h: float | None,
                        confidence: float) -> float:
    """
    Transparent risk curve: safe below half the predicted RUL, ramping to ~0.9
    at the prediction itself, widened when model confidence is low.
    """
    if intervention_h is None:
        return 0.95
    if rul_hours <= 0:
        return 0.95
    slack = 1.0 - min(max(confidence, 0.0), 1.0)      # low confidence -> earlier risk
    safe_until = rul_hours * (0.5 - 0.2 * slack)
    if intervention_h <= safe_until:
        return 0.03
    if intervention_h >= rul_hours:
        return 0.90 + 0.05 * min(1.0, (intervention_h - rul_hours) / max(rul_hours, 1))
    frac = (intervention_h - safe_until) / max(rul_hours - safe_until, 1e-6)
    return round(0.03 + 0.87 * frac ** 1.6, 3)


def evaluate_option(store: OntologyStore, sim: PlantSimulator, machine_id: str,
                    rul: dict, failure_mode: dict, option: ScenarioOption,
                    branch: str = "master") -> dict:
    """Expected-value model for one course of action. Every term is shown to the user."""
    machine = store.get("Machine", machine_id, branch)
    planned = float(failure_mode["repairHours"])
    unplanned = float(machine["unplannedRepairHours"])
    rul_h = float(rul.get("remainingUsefulLifeHours") or 999)
    p_fail = failure_probability(rul_h, option.intervention_hours_from_now,
                                 float(rul.get("confidence") or 0.5))
    p_escalate = p_fail * float(failure_mode["escalationProbability"])

    expected_downtime = (1 - p_fail) * (planned if option.intervention_hours_from_now is not None else 0.0) \
                        + p_fail * unplanned
    parts_cost = 0.0
    for sku in failure_mode["requiredPartSkus"]:
        part = store.get("Part", sku, branch)
        if part:
            parts_cost += float(part["unitCostUsd"])
    labour = expected_downtime * 68.0
    escalation_cost = p_escalate * float(machine["catastrophicCostUsd"])

    rar = compute_revenue_at_risk(store, machine_id, round(expected_downtime, 2), branch)
    total = rar["totalRevenueAtRiskUsd"] + parts_cost + labour + escalation_cost

    return {
        "scenarioId": option.scenario_id,
        "label": option.label,
        "description": option.description,
        "interventionHoursFromNow": option.intervention_hours_from_now,
        "predictedRulHours": rul_h,
        "failureProbability": round(p_fail, 3),
        "escalationProbability": round(p_escalate, 3),
        "expectedDowntimeHours": round(expected_downtime, 2),
        "partsCostUsd": round(parts_cost, 0),
        "labourCostUsd": round(labour, 0),
        "escalationCostUsd": round(escalation_cost, 0),
        "productionMarginLossUsd": rar["marginLossUsd"],
        "latePenaltyUsd": rar["latePenaltyUsd"],
        "ordersBreached": [o["productionOrderId"] for o in rar["orders"] if o["wouldBreach"]],
        "totalExpectedCostUsd": round(total, 0),
    }


def default_options(sim: PlantSimulator, store: OntologyStore, machine_id: str,
                    windows: dict) -> list[ScenarioOption]:
    opts: list[ScenarioOption] = []
    earliest = next((w for w in windows["windows"] if w["type"] == "unplanned_stop"), None)
    changeover = next((w for w in windows["windows"] if w["type"] == "planned_changeover"), None)
    if earliest:
        opts.append(ScenarioOption(
            "SC-EARLY", f"Intervene at +{earliest['hoursFromNow']:.0f}h (earliest qualified tech)",
            earliest["hoursFromNow"], earliest["note"]))
    if changeover:
        opts.append(ScenarioOption(
            "SC-CHANGEOVER", f"Wait for planned changeover at +{changeover['hoursFromNow']:.0f}h",
            changeover["hoursFromNow"], changeover["note"]))
    opts.append(ScenarioOption(
        "SC-DEFER", "Defer to next scheduled PM (+14 days)", 336.0,
        "Accept run-to-failure risk and batch with the next preventive service."))
    return opts


def compare_options(store: OntologyStore, sim: PlantSimulator, machine_id: str,
                    rul: dict, failure_mode: dict,
                    options: list[ScenarioOption]) -> list[dict]:
    rows = [evaluate_option(store, sim, machine_id, rul, failure_mode, o) for o in options]
    best = min(rows, key=lambda r: r["totalExpectedCostUsd"])
    worst = max(rows, key=lambda r: r["totalExpectedCostUsd"])
    for r in rows:
        r["recommended"] = r["scenarioId"] == best["scenarioId"]
        r["deltaVsWorstUsd"] = round(worst["totalExpectedCostUsd"] - r["totalExpectedCostUsd"], 0)
    return rows


class ScenarioManager:
    """Thin wrapper over branch forking so the UI can talk in Scenario terms."""

    def __init__(self, store: OntologyStore) -> None:
        self.store = store
        self.active: dict[str, dict] = {}

    def create(self, label: str, description: str = "") -> str:
        sid = self.store.next_id("SCN", 3)
        self.store.fork(sid, "master", description=description)
        self.active[sid] = {"scenarioId": sid, "label": label, "description": description,
                            "createdAt": iso(now()), "appliedActions": [], "status": "open"}
        return sid

    def record(self, sid: str, action_result) -> None:
        self.active[sid]["appliedActions"].append({
            "actionType": action_result.action_type,
            "decision": action_result.decision,
            "parameters": action_result.parameters,
        })

    def diff(self, sid: str) -> list[dict]:
        base = self.store.branches["master"].objects
        head = self.store.branches[sid].objects
        out = []
        for otype, objs in head.items():
            for pk, obj in objs.items():
                before = base.get(otype, {}).get(pk)
                if before is None:
                    out.append({"objectType": otype, "primaryKey": pk, "change": "created",
                                "detail": "new object"})
                elif before != obj:
                    fields = [k for k in obj
                              if not k.startswith("__") and before.get(k) != obj.get(k)]
                    out.append({"objectType": otype, "primaryKey": pk, "change": "modified",
                                "detail": ", ".join(f"{k}: {before.get(k)} → {obj.get(k)}"
                                                    for k in fields)})
        return out

    def commit(self, sid: str) -> list[str]:
        changed = self.store.merge(sid, "master")
        self.active[sid]["status"] = "committed"
        return changed

    def discard(self, sid: str) -> None:
        self.store.drop(sid)
        self.active.pop(sid, None)
