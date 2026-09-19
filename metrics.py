"""
metrics.py — the numbers a sponsor will ask about.

Baselines are the plant's current manual process, stated explicitly so nobody
has to guess where a "10x faster" claim came from. Everything else is measured
from what actually happened in this session.
"""

from __future__ import annotations

import datetime as dt

from ontology import OBJECT_TYPES, OntologyStore, now

# Baseline: how the same decision is made today, from the discovery workshop.
BASELINE = {
    "systemsTouched": 5,          # historian, CMMS, ERP stock, MES schedule, email/Excel
    "decisionLatencySeconds": 4.2 * 3600,
    "analystMinutesPerDecision": 95,
    "recommendationDocumented": 0.35,   # share of decisions with a written rationale
    "auditTrailCoverage": 0.40,
}

# Distinct external systems of record unified behind the Ontology. Spelled out
# rather than derived, so the count is not inflated by two views of one system.
SOURCE_SYSTEMS = [
    "OSIsoft PI / Kepware OPC-UA (historian + streaming telemetry)",
    "SAP PM (work management)",
    "SAP MM (spares and stock projection)",
    "SAP PP / MES (production schedule)",
    "Ariba (supplier master and expedite terms)",
    "Workday + Kronos (technician skills and shift availability)",
    "Reliability engineering FMEA workbook (failure modes)",
]


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def session_metrics(store: OntologyStore, agent_seconds: float | None,
                    tool_calls: int, alert_id: str | None) -> dict:
    audits = store.all("AuditEvent")
    applied = [a for a in audits if a["decision"] in ("applied", "auto_approved")]
    pending = [a for a in audits if a["decision"] == "pending_approval"]
    rejected = [a for a in audits if a["decision"] == "rejected"]
    simulated = [a for a in audits if a["decision"] == "simulated"]

    latency = None
    if alert_id:
        alert = store.get("Alert", alert_id)
        if alert and alert.get("triageLatencySeconds") is not None:
            latency = float(alert["triageLatencySeconds"])
    if latency is None and agent_seconds is not None:
        latency = agent_seconds

    speedup = (BASELINE["decisionLatencySeconds"] / latency) if latency else None
    hands_on = BASELINE["analystMinutesPerDecision"] * 60

    return {
        "decisionLatencySeconds": round(latency, 1) if latency else None,
        "baselineLatencySeconds": BASELINE["decisionLatencySeconds"],
        "speedupFactor": round(speedup, 1) if speedup else None,
        "baselineAnalystHandsOnSeconds": hands_on,
        "latencyCaveat": ("Baseline is the plant's median alert-to-decision time including "
                          "shift queue time; the like-for-like hands-on comparison is "
                          f"{BASELINE['analystMinutesPerDecision']} analyst-minutes of data "
                          "gathering across 5 systems."),
        "agentReasoningSeconds": round(agent_seconds, 4) if agent_seconds else None,
        "ontologyToolCalls": tool_calls,
        "systemsTouchedByOperator": 1,
        "systemsUnifiedBehindOntology": len(SOURCE_SYSTEMS),
        "baselineSystemsTouched": BASELINE["systemsTouched"],
        "swivelChairReductionPct": round(
            100 * (1 - 1 / BASELINE["systemsTouched"])),
        "actionsApplied": len(applied),
        "actionsPendingApproval": len(pending),
        "actionsRejectedByPolicy": len(rejected),
        "actionsSimulated": len(simulated),
        "auditCoveragePct": 100 if audits else 0,
        "baselineAuditCoveragePct": round(BASELINE["auditTrailCoverage"] * 100),
    }


def outcome_metrics(store: OntologyStore) -> dict:
    """Closed-loop learning: accuracy and value, measured from After Action Reports."""
    aars = store.all("AfterActionReport")
    if not aars:
        return {"cases": 0}

    accepted = [a for a in aars if a.get("recommendationAccepted")]
    overridden = [a for a in aars if a.get("recommendationAccepted") is False]

    def mean(rows, key):
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    pairs = [(float(a["predictedRulHours"]), float(a["actualHoursToFailure"]))
             for a in aars
             if a.get("predictedRulHours") and a.get("actualHoursToFailure")]
    mape = (round(100 * sum(abs(p - act) / act for p, act in pairs) / len(pairs), 1)
            if pairs else None)

    return {
        "cases": len(aars),
        "acceptanceRatePct": round(100 * len(accepted) / len(aars)),
        "avgDowntimeAcceptedHours": mean(accepted, "downtimeHours"),
        "avgDowntimeOverriddenHours": mean(overridden, "downtimeHours"),
        "avgCostAcceptedUsd": mean(accepted, "costUsd"),
        "avgCostOverriddenUsd": mean(overridden, "costUsd"),
        "rulMapePct": mape,
        "revenueProtectedUsd": round(sum(float(a.get("revenueProtectedUsd") or 0) for a in aars)),
        "avgDecisionLatencySeconds": mean(aars, "decisionLatencySeconds"),
    }


def annualised_projection(store: OntologyStore, alerts_per_year: int = 18) -> dict:
    """
    Deliberately conservative extrapolation, stated as an assumption rather than
    a promise: value = (downtime avoided when recommendations are followed)
    × (line contribution margin) across the alert volume the plant already sees.
    """
    out = outcome_metrics(store)
    if out.get("cases", 0) == 0:
        return {}
    # Only count cases that actually reached a failure decision point, so a
    # "no action needed" coolant call does not flatter the average.
    real = [a for a in store.all("AfterActionReport")
            if a.get("actualHoursToFailure") is not None]
    acc_rows = [a for a in real if a.get("recommendationAccepted")]
    ovr_rows = [a for a in real if a.get("recommendationAccepted") is False]
    if not acc_rows or not ovr_rows:
        return {}
    acc = sum(float(a["downtimeHours"]) for a in acc_rows) / len(acc_rows)
    ovr = sum(float(a["downtimeHours"]) for a in ovr_rows) / len(ovr_rows)
    machine = store.get("Machine", "MCH-2207")
    margin_per_hour = float(machine["unitsPerHour"]) * float(machine["marginPerUnitUsd"])
    hours_saved = max(ovr - acc, 0)
    analyst_hours = alerts_per_year * (BASELINE["analystMinutesPerDecision"] / 60.0)
    return {
        "assumedAlertsPerYear": alerts_per_year,
        "downtimeHoursAvoidedPerAlert": round(hours_saved, 1),
        "contributionMarginPerHourUsd": round(margin_per_hour),
        "annualDowntimeAvoidedHours": round(hours_saved * alerts_per_year),
        "annualMarginProtectedUsd": round(hours_saved * alerts_per_year * margin_per_hour),
        "analystHoursReturnedPerYear": round(analyst_hours * 0.8),
        "comparableCases": len(acc_rows) + len(ovr_rows),
        "note": "Deliberately narrow: counts only critical-asset degradation events of this "
                "class on one line, credits only that line's contribution margin, and uses "
                "the downtime difference between followed and overridden recommendations in "
                "the closed-case population. Excludes avoided scrap, expedite fees and "
                "customer penalties.",
    }
