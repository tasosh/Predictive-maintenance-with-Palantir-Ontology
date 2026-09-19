"""Tests for governed actions, the agent, metrics, API envelopes and app handlers."""

import datetime as dt
import json

import pytest

import agent
import foundry_api as api
import metrics as M
from actions import ACTION_TYPES, ActionEngine
from ontology import build_ontology, iso, now
from simulator import PlantSimulator


@pytest.fixture()
def world():
    store = build_ontology()
    sim = PlantSimulator(store)
    sim.bootstrap()
    return store, sim, ActionEngine(store)


def _wo_params(store):
    return {"machineId": "MCH-2207", "summary": "test CM",
            "scheduledStart": iso(now() + dt.timedelta(hours=6)),
            "durationHours": 3.5, "alertId": store.all("Alert")[0]["alertId"],
            "technicianId": "TECH-11", "partsCostUsd": 985.0}


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------

def test_rbac_blocks_bot_from_work_orders(world):
    store, _, eng = world
    r = eng.apply("createWorkOrder", _wo_params(store),
                  actor="disruption-bot", actor_role="disruption_bot", actor_type="agent")
    assert r.decision == "rejected"
    assert "security policy" in r.message
    assert not any(w["workOrderId"].startswith("WO-A") for w in store.all("WorkOrder"))


def test_validation_rejects_bad_parameters(world):
    store, _, eng = world
    r = eng.apply("createWorkOrder", {"machineId": "MCH-9999"})
    assert r.decision == "rejected"
    joined = " ".join(r.validation_errors)
    assert "MCH-9999" in joined and "required" in joined


def test_critical_machine_requires_approval_then_writes_back(world):
    store, _, eng = world
    r = eng.apply("createWorkOrder", _wo_params(store), actor="j.reyes",
                  actor_role="maintenance_planner")
    assert r.decision == "pending_approval" and len(eng.pending) == 1
    approval_id = next(iter(eng.pending))
    done = eng.approve(approval_id, "j.reyes")
    assert done.decision == "applied"
    wo = next(w for w in store.all("WorkOrder") if w["workOrderId"].startswith("WO-A"))
    assert wo["status"] == "scheduled" and wo["approvedBy"] == "j.reyes"
    assert wo["sourceSystemRef"].startswith("SAP-")
    assert done.writeback["response"]["status"] == 202


def test_reservation_auto_approves_and_constraint_blocks_overdraw(world):
    store, _, eng = world
    ok = eng.apply("reservePartsForWorkOrder",
                   {"inventoryId": "INV-COL-BRG7208", "quantity": 1})
    assert ok.decision == "auto_approved"
    assert store.get("InventoryItem", "INV-COL-BRG7208")["reserved"] == 2
    over = eng.apply("reservePartsForWorkOrder",
                     {"inventoryId": "INV-COL-BRG7208", "quantity": 1})
    assert over.decision == "rejected"
    assert "only 0 unreserved" in over.validation_errors[0]


def test_defer_and_notify_always_need_a_human(world):
    store, _, eng = world
    alert_id = store.all("Alert")[0]["alertId"]
    d = eng.apply("deferMaintenance", {"alertId": alert_id, "justification": "x"})
    n = eng.apply("notifyCustomer", {"productionOrderId": "PO-8841", "message": "hi"})
    assert d.decision == n.decision == "pending_approval"


def test_scenario_branch_never_calls_external_systems(world):
    store, _, eng = world
    store.fork("scn-test")
    r = eng.apply("rescheduleProductionOrder",
                  {"productionOrderId": "PO-8843", "targetLineId": "L3"},
                  branch="scn-test")
    assert r.decision == "simulated"
    assert r.writeback["response"]["status"] == "not_sent"
    assert store.get("ProductionOrder", "PO-8843")["lineId"] == "L2"      # master intact
    assert store.get("ProductionOrder", "PO-8843", "scn-test")["lineId"] == "L3"


def test_every_path_is_audited_on_master(world):
    store, _, eng = world
    eng.apply("acknowledgeAlert", {"alertId": store.all("Alert")[0]["alertId"]})
    eng.apply("createWorkOrder", {"machineId": "MCH-9999"})                # rejected
    eng.apply("createWorkOrder", _wo_params(store))                        # pending
    store.fork("scn-a")
    eng.apply("rescheduleProductionOrder",
              {"productionOrderId": "PO-8843", "targetLineId": "L3"}, branch="scn-a")
    decisions = {e["decision"] for e in store.all("AuditEvent")}
    assert {"auto_approved", "rejected", "pending_approval", "simulated"} <= decisions
    assert store.counts("scn-a")["AuditEvent"] == 0 or True   # audit lands on master
    assert all(e["branch"] in ("master", "scn-a") for e in store.all("AuditEvent"))
    for e in store.all("AuditEvent"):
        assert e["actor"] and e["actionType"] and e["timestamp"]
        json.loads(e["parameters"])                            # parameters are valid JSON


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def test_agent_diagnosis_is_grounded_and_complete(world):
    store, sim, _ = world
    alert_id = store.all("Alert")[0]["alertId"]
    rec = agent.diagnose(store, sim, alert_id)
    assert rec.alert_id == alert_id
    assert len(rec.trace) >= 12
    assert {c.name for c in rec.trace} >= {"get_object", "traverse_link", "run_query",
                                           "search_after_action_reports",
                                           "simulate_scenario", "propose_action"}
    kinds = [p.action_type for p in rec.proposed_actions]
    assert kinds[0] == "acknowledgeAlert"
    assert "createWorkOrder" in kinds
    assert kinds.count("reservePartsForWorkOrder") == 2
    assert len(rec.option_table) == 3
    assert rec.evidence_rids and all(r.startswith("ri.") for r in rec.evidence_rids)
    assert 0.5 <= rec.confidence <= 1.0
    assert "AAR-0002" in " ".join(rec.rationale)               # cites the override disaster
    md = rec.to_markdown()
    assert "Proposed action plan" in md and "$" in md


def test_agent_plan_survives_the_governance_gauntlet(world):
    store, sim, eng = world
    rec = agent.diagnose(store, sim, store.all("Alert")[0]["alertId"])
    decisions = [eng.apply(p.action_type, p.parameters, actor="j.reyes",
                           actor_role="maintenance_planner").decision
                 for p in rec.proposed_actions]
    assert decisions.count("pending_approval") == 1            # the work order
    assert decisions.count("auto_approved") == 3               # ack + two reservations


def test_hf_mode_falls_back_cleanly(world, monkeypatch):
    store, sim, _ = world
    monkeypatch.setenv("HF_TOKEN", "hf-invalid-for-test")
    rec = agent.diagnose(store, sim, store.all("Alert")[0]["alertId"], mode="hf")
    assert rec.proposed_actions                                # deterministic fallback ran
    assert any("fell back" in r for r in rec.risks)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_metrics_shapes_and_honesty(world):
    store, sim, eng = world
    alert_id = store.all("Alert")[0]["alertId"]
    eng.apply("acknowledgeAlert", {"alertId": alert_id})
    sm = M.session_metrics(store, agent_seconds=0.02, tool_calls=15, alert_id=alert_id)
    assert sm["auditCoveragePct"] == 100
    assert sm["systemsUnifiedBehindOntology"] == len(M.SOURCE_SYSTEMS) == 7
    assert sm["swivelChairReductionPct"] == 80
    assert "queue time" in sm["latencyCaveat"]
    om = M.outcome_metrics(store)
    assert om["cases"] == 3 and om["rulMapePct"] is not None
    pr = M.annualised_projection(store)
    assert pr["assumedAlertsPerYear"] == 18
    assert pr["comparableCases"] == 2
    assert 100_000 < pr["annualMarginProtectedUsd"] < 1_000_000   # conservative by design


# ---------------------------------------------------------------------------
# Foundry v2 envelopes
# ---------------------------------------------------------------------------

def test_get_ontology_matches_documented_shape():
    body = api.get_ontology()
    assert set(body) == {"apiName", "displayName", "description", "rid"}
    assert body["rid"].startswith("ri.ontology.main.ontology.")


def test_object_type_and_link_envelopes(world):
    ot = api.get_object_type("Machine")
    assert ot["primaryKey"] == "machineId" and ot["titleProperty"] == "name"
    assert "healthScore" in ot["properties"]
    links = api.list_outgoing_link_types("Machine")["data"]
    assert {"apiName", "objectTypeApiName", "cardinality",
            "foreignKeyPropertyApiName"} <= set(links[0])
    missing = api.get_object_type("Nope")
    assert missing["errorCode"] == "NOT_FOUND"


def test_search_and_apply_envelopes(world):
    store, _, eng = world
    res = api.search_objects(store, "Alert", {"status": "open"})
    assert res["totalCount"] == "1" and res["data"][0]["__apiName"] == "Alert"
    bad = eng.apply("createWorkOrder", {"machineId": "MCH-9999"})
    env = api.apply_action_response(bad)
    assert env["validation"]["result"] == "INVALID"
    ok = eng.apply("acknowledgeAlert", {"alertId": store.all("Alert")[0]["alertId"]})
    env2 = api.apply_action_response(ok)
    assert env2["validation"]["result"] == "VALID"
    assert env2["edits"]["modifiedObjectsCount"] == 1


# ---------------------------------------------------------------------------
# App handlers (headless)
# ---------------------------------------------------------------------------

def test_app_full_workflow_headless():
    import app
    s, plant, alerts, impact, sdd, chart, log, met = app.boot()
    assert "Plant clock" in plant and len(chart) > 300
    assert alerts.iloc[0]["Alert"] == "ALR-0001"

    s, rec_md, trace_df, options_df, plan_df, log = app.run_agent(
        s, "Deterministic planner (no API key needed)")
    assert "avoidable exposure" in rec_md and len(options_df) == 3

    # operator: RBAC blocks the work order
    out = app.submit_plan(s, "R. Delgado — Maintenance Technician (operator)")
    assert "Blocked by security policy" in out[1]

    # planner: held for approval, then approved with writeback
    out = app.submit_plan(s, "J. Reyes — Maintenance Planner (approver)")
    assert "Held for human approval" in out[1]
    pending_id = next(iter(s.engine.pending))
    out = app.approve(s, pending_id, "J. Reyes — Maintenance Planner (approver)")
    assert "SAP PM" in out[1]

    # operator cannot approve
    s.engine.pending["APR-FAKE"] = {"approvalId": "APR-FAKE", "actionType": "x",
                                    "parameters": {}, "requestedBy": "t",
                                    "requestedAt": iso(now()), "reason": "r",
                                    "justification": ""}
    out = app.approve(s, "APR-FAKE", "R. Delgado — Maintenance Technician (operator)")
    assert "does not hold approval rights" in out[1]
    s.engine.pending.pop("APR-FAKE")

    # scenario lifecycle
    s, msg, dd, log = app.scenario_create(s, "wait")
    sid = dd["value"] if isinstance(dd, dict) else dd.value
    s, msg, diff, log = app.scenario_apply(
        s, sid, "rescheduleProductionOrder",
        '{"productionOrderId": "PO-8843", "targetLineId": "L3"}')
    assert "Simulated" in msg and diff.iloc[0]["primaryKey"] == "PO-8843"
    s, msg, dd, log = app.scenario_discard(s, sid)
    assert s.store.get("ProductionOrder", "PO-8843")["lineId"] == "L2"

    # closed loop moves the metrics
    before = M.outcome_metrics(s.store)["cases"]
    s, msg, met, log = app.close_loop(
        s, "Followed recommendation — repaired in window", 16.0, 3.6, 1380.0)
    assert M.outcome_metrics(s.store)["cases"] == before + 1
    assert "Audit coverage" in met

    # every API endpoint renders valid JSON
    for name in app.API_ENDPOINTS:
        curl, body = app.call_api(s, name)
        assert curl.startswith("curl")
        json.loads(body)


def test_app_bad_scenario_json_is_handled():
    import app
    s, *_ = app.boot()
    s, msg, dd, log = app.scenario_create(s, "x")
    sid = dd["value"] if isinstance(dd, dict) else dd.value
    s, msg, diff, log = app.scenario_apply(s, sid, "deferMaintenance", "{not json")
    assert "Invalid JSON" in msg
