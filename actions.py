"""
actions.py — governed Ontology Action Types.

Mirrors Foundry's Action semantics: each Action Type declares parameters with
validation, an approval policy, the object edits it performs, and the system of
record it writes back to. Every application emits an immutable AuditEvent,
whether it was applied, auto-approved, held for a human, rejected or simulated
on a Scenario branch.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ontology import OntologyStore, iso, now

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

AUTO_APPROVE_COST_LIMIT_USD = 5000.0

ROLES = {
    "operator": {"apply:reservePartsForWorkOrder", "apply:acknowledgeAlert",
                 "apply:recordAfterActionReport"},
    "maintenance_planner": {"apply:createWorkOrder", "apply:reservePartsForWorkOrder",
                            "apply:acknowledgeAlert", "apply:rescheduleProductionOrder",
                            "apply:recordAfterActionReport", "apply:deferMaintenance",
                            "apply:notifyCustomer", "approve:*"},
    "disruption_bot": {"apply:acknowledgeAlert", "apply:reservePartsForWorkOrder",
                       "propose:*"},
}


@dataclass
class ActionParameter:
    name: str
    data_type: str
    required: bool = True
    description: str = ""
    validation: Callable[[Any, dict, OntologyStore, str], str | None] | None = None


@dataclass
class ActionType:
    api_name: str
    display_name: str
    description: str
    parameters: list[ActionParameter]
    writeback_target: str
    requires_approval_if: Callable[[dict, OntologyStore, str], str | None]
    modifies: list[str]
    apply_fn: Callable[[dict, OntologyStore, str, str], dict] = field(repr=False, default=None)

    def rid(self) -> str:
        return f"ri.actions.main.action-type.{self.api_name}"


@dataclass
class ActionResult:
    action_type: str
    decision: str                 # applied | auto_approved | pending_approval | rejected | simulated
    validation_errors: list[str]
    parameters: dict
    edits: list[dict]
    audit_event_id: str | None
    writeback: dict | None
    message: str

    @property
    def ok(self) -> bool:
        return self.decision in ("applied", "auto_approved", "simulated", "pending_approval")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _object_exists(otype: str):
    def _v(value, params, store, branch):
        if value is None:
            return None
        return None if store.get(otype, str(value), branch) else f"{otype} '{value}' not found in Ontology"
    return _v


def _positive(value, params, store, branch):
    try:
        return None if float(value) > 0 else "must be greater than zero"
    except (TypeError, ValueError):
        return "must be a number"


def _future_timestamp(value, params, store, branch):
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "must be an ISO-8601 timestamp"
    return None if t > now() - dt.timedelta(hours=73) else "must not be in the past"


def _parts_available(value, params, store, branch):
    """Ontology-level constraint: you cannot reserve stock that isn't there."""
    item = store.get("InventoryItem", str(value), branch)
    if not item:
        return f"InventoryItem '{value}' not found"
    qty = int(params.get("quantity") or 1)
    available = int(item["onHand"]) - int(item["reserved"])
    if qty > available:
        return (f"only {available} unreserved unit(s) of {item['partSku']} at "
                f"{item['siteId']} (onHand={item['onHand']}, reserved={item['reserved']})")
    return None


# ---------------------------------------------------------------------------
# Apply functions — these produce the object edits
# ---------------------------------------------------------------------------

def _apply_acknowledge_alert(p, store, branch, actor):
    alert = store.get("Alert", p["alertId"], branch)
    raised = dt.datetime.fromisoformat(alert["raisedAt"].replace("Z", "+00:00"))
    latency = (now() - raised).total_seconds()
    store.patch("Alert", p["alertId"], {
        "status": "triaged", "acknowledgedAt": iso(now()),
        "triageLatencySeconds": round(latency, 1),
    }, branch)
    return [{"objectType": "Alert", "primaryKey": p["alertId"], "type": "modifyObject"}]


def _apply_create_work_order(p, store, branch, actor):
    wo_id = store.next_pk("WorkOrder", "WO-A", 4)
    machine = store.get("Machine", p["machineId"], branch)
    tech = store.get("Technician", p.get("technicianId"), branch) if p.get("technicianId") else None
    hours = float(p["durationHours"])
    labour = hours * (tech["hourlyRateUsd"] if tech else 65.0)
    store.put("WorkOrder", {
        "workOrderId": wo_id,
        "summary": p["summary"],
        "machineId": p["machineId"],
        "alertId": p.get("alertId"),
        "failureModeId": p.get("failureModeId"),
        "orderType": p.get("orderType", "CM"),
        "priority": p.get("priority", "high"),
        "status": "pending_approval",
        "scheduledStart": p["scheduledStart"],
        "durationHours": hours,
        "technicianId": p.get("technicianId"),
        "estimatedCostUsd": round(labour + float(p.get("partsCostUsd") or 0.0), 2),
        "createdBy": actor,
        "approvedBy": None,
        "sourceSystemRef": None,
    }, branch)
    if p.get("alertId"):
        store.patch("Alert", p["alertId"], {"status": "triaged"}, branch)
    return [{"objectType": "WorkOrder", "primaryKey": wo_id, "type": "createObject"}]


def _apply_reserve_parts(p, store, branch, actor):
    item = store.get("InventoryItem", p["inventoryId"], branch)
    qty = int(p.get("quantity") or 1)
    store.patch("InventoryItem", p["inventoryId"], {"reserved": int(item["reserved"]) + qty}, branch)
    edits = [{"objectType": "InventoryItem", "primaryKey": p["inventoryId"], "type": "modifyObject"}]
    if p.get("workOrderId") and store.get("WorkOrder", p["workOrderId"], branch):
        wo = store.get("WorkOrder", p["workOrderId"], branch)
        part = store.get("Part", item["partSku"], branch)
        store.patch("WorkOrder", p["workOrderId"], {
            "estimatedCostUsd": round(float(wo["estimatedCostUsd"] or 0)
                                      + qty * float(part["unitCostUsd"]), 2)
        }, branch)
        edits.append({"objectType": "WorkOrder", "primaryKey": p["workOrderId"], "type": "modifyObject"})
    return edits


def _apply_reschedule_production(p, store, branch, actor):
    store.patch("ProductionOrder", p["productionOrderId"], {
        "lineId": p["targetLineId"], "status": "rescheduled",
    }, branch)
    return [{"objectType": "ProductionOrder", "primaryKey": p["productionOrderId"],
             "type": "modifyObject"}]


def _apply_defer_maintenance(p, store, branch, actor):
    store.patch("Alert", p["alertId"], {"status": "suppressed"}, branch)
    return [{"objectType": "Alert", "primaryKey": p["alertId"], "type": "modifyObject"}]


def _apply_notify_customer(p, store, branch, actor):
    store.patch("ProductionOrder", p["productionOrderId"], {"status": "customer_notified"}, branch)
    return [{"objectType": "ProductionOrder", "primaryKey": p["productionOrderId"],
             "type": "modifyObject"}]


def _apply_record_aar(p, store, branch, actor):
    aar_id = store.next_pk("AfterActionReport", "AAR", 4)
    store.put("AfterActionReport", {
        "aarId": aar_id,
        "alertId": p["alertId"],
        "machineId": p["machineId"],
        "failureModeId": p.get("failureModeId"),
        "signature": p.get("signature"),
        "decision": p["decision"],
        "recommendationAccepted": bool(p.get("recommendationAccepted", True)),
        "predictedRulHours": p.get("predictedRulHours"),
        "actualHoursToFailure": p.get("actualHoursToFailure"),
        "downtimeHours": p.get("downtimeHours"),
        "costUsd": p.get("costUsd"),
        "revenueProtectedUsd": p.get("revenueProtectedUsd"),
        "decisionLatencySeconds": p.get("decisionLatencySeconds"),
        "notes": p.get("notes", ""),
        "recordedAt": iso(now()),
    }, branch)
    if p.get("alertId") and store.get("Alert", p["alertId"], branch):
        store.patch("Alert", p["alertId"], {"status": "resolved", "resolvedAt": iso(now())}, branch)
    return [{"objectType": "AfterActionReport", "primaryKey": aar_id, "type": "createObject"}]


# ---------------------------------------------------------------------------
# Approval policy
# ---------------------------------------------------------------------------

def _wo_approval(p, store, branch):
    machine = store.get("Machine", p.get("machineId"), branch)
    cost = float(p.get("partsCostUsd") or 0) + float(p.get("durationHours") or 0) * 70
    if machine and machine["criticality"] == "critical":
        return ("Machine criticality is 'critical' — plant policy requires a named "
                "maintenance planner to approve any production-affecting stop.")
    if cost > AUTO_APPROVE_COST_LIMIT_USD:
        return f"Estimated cost ${cost:,.0f} exceeds the ${AUTO_APPROVE_COST_LIMIT_USD:,.0f} auto-approval limit."
    return None


def _reschedule_approval(p, store, branch):
    po = store.get("ProductionOrder", p.get("productionOrderId"), branch)
    if po and po.get("priority") in ("P1",):
        return "P1 customer order — reallocation requires planner approval."
    if po and float(po.get("revenueUsd") or 0) > 250000:
        return "Order revenue exceeds $250k — requires planner approval."
    return None


def _defer_approval(p, store, branch):
    return ("Deferring a flagged degradation accepts run-to-failure risk and must be "
            "signed off by a human. The agent may never auto-approve this.")


ACTION_TYPES: dict[str, ActionType] = {}


def _register(a: ActionType) -> None:
    ACTION_TYPES[a.api_name] = a


_register(ActionType(
    api_name="acknowledgeAlert",
    display_name="Acknowledge Alert",
    description="Mark an alert as triaged and stamp the triage latency.",
    parameters=[ActionParameter("alertId", "string", True, "Alert to triage",
                                _object_exists("Alert"))],
    writeback_target="Foundry only (no external system)",
    requires_approval_if=lambda p, s, b: None,
    modifies=["Alert"],
    apply_fn=_apply_acknowledge_alert,
))

_register(ActionType(
    api_name="createWorkOrder",
    display_name="Create Work Order",
    description="Open a corrective or preventive maintenance order and schedule it.",
    parameters=[
        ActionParameter("machineId", "string", True, "Asset to service", _object_exists("Machine")),
        ActionParameter("summary", "string", True, "One-line job description"),
        ActionParameter("scheduledStart", "timestamp", True, "Planned start", _future_timestamp),
        ActionParameter("durationHours", "double", True, "Planned duration", _positive),
        ActionParameter("orderType", "string", False, "PM | CM | EM"),
        ActionParameter("priority", "string", False),
        ActionParameter("technicianId", "string", False, "Assigned technician",
                        _object_exists("Technician")),
        ActionParameter("alertId", "string", False, "Originating alert", _object_exists("Alert")),
        ActionParameter("failureModeId", "string", False, "Diagnosed failure mode",
                        _object_exists("FailureMode")),
        ActionParameter("partsCostUsd", "double", False),
    ],
    writeback_target="SAP PM — BAPI_ALM_ORDER_MAINTAIN",
    requires_approval_if=_wo_approval,
    modifies=["WorkOrder", "Alert"],
    apply_fn=_apply_create_work_order,
))

_register(ActionType(
    api_name="reservePartsForWorkOrder",
    display_name="Reserve Parts",
    description="Hard-allocate spare parts stock against a work order.",
    parameters=[
        ActionParameter("inventoryId", "string", True, "Stock record", _parts_available),
        ActionParameter("quantity", "integer", True, "Units to reserve", _positive),
        ActionParameter("workOrderId", "string", False, "Work order to charge"),
    ],
    writeback_target="SAP MM — reservation document (MB21)",
    requires_approval_if=lambda p, s, b: None,   # low blast radius, reversible
    modifies=["InventoryItem", "WorkOrder"],
    apply_fn=_apply_reserve_parts,
))

_register(ActionType(
    api_name="rescheduleProductionOrder",
    display_name="Reschedule Production Order",
    description="Move a production order to a different line to protect the maintenance window.",
    parameters=[
        ActionParameter("productionOrderId", "string", True, "Order to move",
                        _object_exists("ProductionOrder")),
        ActionParameter("targetLineId", "string", True, "Destination line",
                        _object_exists("ProductionLine")),
        ActionParameter("reason", "string", False),
    ],
    writeback_target="MES — schedule adjustment API",
    requires_approval_if=_reschedule_approval,
    modifies=["ProductionOrder"],
    apply_fn=_apply_reschedule_production,
))

_register(ActionType(
    api_name="deferMaintenance",
    display_name="Defer Maintenance (accept risk)",
    description="Suppress the alert and run the asset to the next planned window.",
    parameters=[
        ActionParameter("alertId", "string", True, "Alert to suppress", _object_exists("Alert")),
        ActionParameter("justification", "string", True, "Required risk rationale"),
    ],
    writeback_target="Foundry only — risk acceptance record",
    requires_approval_if=_defer_approval,
    modifies=["Alert"],
    apply_fn=_apply_defer_maintenance,
))

_register(ActionType(
    api_name="notifyCustomer",
    display_name="Notify Customer",
    description="Send contractual delay notice for an affected production order.",
    parameters=[
        ActionParameter("productionOrderId", "string", True, "Affected order",
                        _object_exists("ProductionOrder")),
        ActionParameter("message", "string", True),
    ],
    writeback_target="Salesforce — case + customer email",
    requires_approval_if=lambda p, s, b: "Outbound customer communication always requires human sign-off.",
    modifies=["ProductionOrder"],
    apply_fn=_apply_notify_customer,
))

_register(ActionType(
    api_name="recordAfterActionReport",
    display_name="Record After Action Report",
    description="Close the loop: write the outcome back so the next decision is better.",
    parameters=[
        ActionParameter("alertId", "string", True, "Originating alert"),
        ActionParameter("machineId", "string", True, "Asset", _object_exists("Machine")),
        ActionParameter("decision", "string", True),
        ActionParameter("failureModeId", "string", False),
        ActionParameter("signature", "string", False),
        ActionParameter("recommendationAccepted", "boolean", False),
        ActionParameter("predictedRulHours", "double", False),
        ActionParameter("actualHoursToFailure", "double", False),
        ActionParameter("downtimeHours", "double", False),
        ActionParameter("costUsd", "double", False),
        ActionParameter("revenueProtectedUsd", "double", False),
        ActionParameter("decisionLatencySeconds", "double", False),
        ActionParameter("notes", "string", False),
    ],
    writeback_target="Foundry only — feeds the next agent run",
    requires_approval_if=lambda p, s, b: None,
    modifies=["AfterActionReport", "Alert"],
    apply_fn=_apply_record_aar,
))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ActionEngine:
    """Validates, authorises, applies and audits Ontology Actions."""

    def __init__(self, store: OntologyStore) -> None:
        self.store = store
        self.pending: dict[str, dict] = {}       # approvalId -> request
        self.webhook_log: list[dict] = []

    # -- helpers -----------------------------------------------------------
    def _authorised(self, actor_role: str, api_name: str) -> bool:
        perms = ROLES.get(actor_role, set())
        return f"apply:{api_name}" in perms or "apply:*" in perms

    def validate(self, api_name: str, params: dict, branch: str = "master") -> list[str]:
        at = ACTION_TYPES[api_name]
        errors: list[str] = []
        for p in at.parameters:
            value = params.get(p.name)
            if p.required and (value is None or value == ""):
                errors.append(f"{p.name}: required parameter missing")
                continue
            if value is None or value == "":
                continue
            if p.validation:
                err = p.validation(value, params, self.store, branch)
                if err:
                    errors.append(f"{p.name}: {err}")
        return errors

    def _audit(self, api_name, actor, actor_type, params, decision, branch,
               edits, justification, writeback) -> str:
        event_id = self.store.next_pk("AuditEvent", "AUD", 5)
        self.store.put("AuditEvent", {
            "eventId": event_id,
            "timestamp": iso(now()),
            "actor": actor,
            "actorType": actor_type,
            "actionType": api_name,
            "parameters": json.dumps(params, default=str),
            "affectedObjectRids": [f"{e['objectType']}:{e['primaryKey']}" for e in edits],
            "branch": branch,
            "decision": decision,
            "justification": justification or "",
            "writebackTarget": writeback or "",
        }, "master")  # audit always lands on master, even for scenario work
        return event_id

    def _writeback(self, at: ActionType, params: dict, edits: list[dict],
                   branch: str, simulated: bool) -> dict:
        payload = {
            "target": at.writeback_target,
            "mode": "simulated_webhook" if simulated else "outbound_webhook",
            "idempotencyKey": f"{at.api_name}:{hash(json.dumps(params, sort_keys=True, default=str)) & 0xffffff:06x}",
            "sentAt": iso(now()),
            "body": {"actionType": at.api_name, "parameters": params,
                     "ontologyEdits": edits, "branch": branch},
            "response": ({"status": "not_sent", "reason": "scenario branch — no external call"}
                         if simulated else
                         {"status": "not_sent", "reason": "no external system of record"}
                         if at.writeback_target.startswith("Foundry only") else
                         {"status": 202, "ref": f"SAP-{abs(hash(str(params))) % 9000000 + 1000000}"}),
        }
        self.webhook_log.append(payload)
        return payload

    # -- main entry point --------------------------------------------------
    def apply(self, api_name: str, params: dict, actor: str = "operator",
              actor_role: str = "maintenance_planner", actor_type: str = "human",
              branch: str = "master", force_approved_by: str | None = None,
              justification: str = "") -> ActionResult:
        if api_name not in ACTION_TYPES:
            return ActionResult(api_name, "rejected", [f"Unknown action type '{api_name}'"],
                                params, [], None, None, f"Unknown action type '{api_name}'")
        at = ACTION_TYPES[api_name]

        if not self._authorised(actor_role, api_name):
            eid = self._audit(api_name, actor, actor_type, params, "rejected", branch, [],
                              f"Role '{actor_role}' lacks apply:{api_name}", None)
            return ActionResult(api_name, "rejected", [f"Role '{actor_role}' is not permitted to apply this action"],
                                params, [], eid, None,
                                f"Blocked by security policy: role '{actor_role}' cannot apply {api_name}.")

        errors = self.validate(api_name, params, branch)
        if errors:
            eid = self._audit(api_name, actor, actor_type, params, "rejected", branch, [],
                              "; ".join(errors), None)
            return ActionResult(api_name, "rejected", errors, params, [], eid, None,
                                "Validation failed:\n- " + "\n- ".join(errors))

        simulated = branch != "master"
        approval_reason = at.requires_approval_if(params, self.store, branch)

        if approval_reason and not force_approved_by and not simulated:
            approval_id = self.store.next_id("APR", 4)
            self.pending[approval_id] = {
                "approvalId": approval_id, "actionType": api_name, "parameters": params,
                "requestedBy": actor, "requestedAt": iso(now()), "reason": approval_reason,
                "justification": justification,
            }
            eid = self._audit(api_name, actor, actor_type, params, "pending_approval", branch,
                              [], approval_reason, at.writeback_target)
            return ActionResult(api_name, "pending_approval", [], params, [], eid, None,
                                f"Held for human approval ({approval_id}): {approval_reason}")

        edits = at.apply_fn(params, self.store, branch, actor)

        if force_approved_by and api_name == "createWorkOrder":
            for e in edits:
                if e["objectType"] == "WorkOrder":
                    self.store.patch("WorkOrder", e["primaryKey"],
                                     {"status": "scheduled", "approvedBy": force_approved_by}, branch)

        writeback = self._writeback(at, params, edits, branch, simulated)
        if not simulated and writeback["response"].get("ref"):
            for e in edits:
                if e["objectType"] == "WorkOrder":
                    self.store.patch("WorkOrder", e["primaryKey"],
                                     {"sourceSystemRef": writeback["response"]["ref"]}, branch)

        decision = ("simulated" if simulated
                    else "applied" if force_approved_by
                    else "auto_approved")
        eid = self._audit(api_name, actor, actor_type, params, decision, branch, edits,
                          justification or (approval_reason or "Within auto-approval policy"),
                          at.writeback_target)
        verb = {"simulated": "Simulated on scenario branch",
                "applied": "Applied after approval",
                "auto_approved": "Auto-approved and applied"}[decision]
        return ActionResult(api_name, decision, [], params, edits, eid, writeback,
                            f"{verb}: {at.display_name} → {len(edits)} object edit(s).")

    def approve(self, approval_id: str, approver: str) -> ActionResult:
        req = self.pending.pop(approval_id, None)
        if not req:
            return ActionResult("?", "rejected", ["Unknown or already-resolved approval"],
                                {}, [], None, None, "Approval request not found.")
        return self.apply(req["actionType"], req["parameters"], actor=approver,
                          actor_role="maintenance_planner", actor_type="human",
                          force_approved_by=approver,
                          justification=f"Approved by {approver} ({req['reason']})")

    def reject(self, approval_id: str, approver: str, reason: str) -> ActionResult:
        req = self.pending.pop(approval_id, None)
        if not req:
            return ActionResult("?", "rejected", ["Unknown approval"], {}, [], None, None,
                                "Approval request not found.")
        eid = self._audit(req["actionType"], approver, "human", req["parameters"],
                          "rejected", "master", [], f"Rejected by {approver}: {reason}", None)
        return ActionResult(req["actionType"], "rejected", [], req["parameters"], [], eid, None,
                            f"Rejected by {approver}: {reason}")
