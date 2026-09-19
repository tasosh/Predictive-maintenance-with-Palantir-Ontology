"""Tests for the ontology store, simulator/RUL model and logic layer."""

import math

import pytest

from logic import (check_parts_availability, compare_options, compute_revenue_at_risk,
                   default_options, failure_probability, find_maintenance_windows,
                   match_failure_mode, search_after_action_reports, ScenarioManager)
from ontology import LINK_TYPES, OBJECT_TYPES, build_ontology, outgoing_links
from simulator import PlantSimulator, predict_rul


@pytest.fixture()
def world():
    store = build_ontology()
    sim = PlantSimulator(store)
    sim.bootstrap()
    return store, sim


# ---------------------------------------------------------------------------
# Ontology store
# ---------------------------------------------------------------------------

def test_seed_counts(world):
    store, _ = world
    counts = store.counts()
    assert counts["Machine"] == 3
    assert counts["Sensor"] == 7
    assert counts["FailureMode"] == 3
    assert counts["AfterActionReport"] == 3
    assert counts["SensorReading"] > 900          # 72h of 30-min samples x 7 sensors


def test_every_link_type_is_consistent():
    for link, (src, tgt, card, fk, inverse) in LINK_TYPES.items():
        assert src in OBJECT_TYPES and tgt in OBJECT_TYPES
        # FK lives on the MANY side's target for ONE links, on target for MANY links
        holder = src if card == "ONE" else tgt
        assert fk in OBJECT_TYPES[holder]["properties"], (link, fk, holder)
        if inverse:
            assert inverse in LINK_TYPES


def test_link_traversal_both_cardinalities(world):
    store, _ = world
    sensors = store.linked("Machine", "MCH-2207", "sensors")
    assert {s["sensorId"] for s in sensors} >= {"SEN-2207-VIB", "SEN-2207-TMP"}
    back = store.linked("Sensor", "SEN-2207-VIB", "machine")
    assert len(back) == 1 and back[0]["machineId"] == "MCH-2207"
    with pytest.raises(KeyError):
        store.linked("Machine", "MCH-2207", "not_a_link")


def test_fork_is_copy_on_write(world):
    store, _ = world
    store.fork("b1")
    store.patch("Machine", "MCH-2207", {"status": "down"}, branch="b1")
    assert store.get("Machine", "MCH-2207")["status"] == "running"
    assert store.get("Machine", "MCH-2207", "b1")["status"] == "down"
    changed = store.merge("b1")
    assert "Machine:MCH-2207" in changed
    assert store.get("Machine", "MCH-2207")["status"] == "down"


def test_search_and_aggregate(world):
    store, _ = world
    open_alerts = store.search("Alert", where={"status": "open"})
    assert all(a["status"] == "open" for a in open_alerts)
    by_line = store.aggregate("Machine", "lineId")
    assert by_line == {"L2": 2, "L3": 1}


# ---------------------------------------------------------------------------
# Simulator and RUL
# ---------------------------------------------------------------------------

def test_bootstrap_raises_exactly_the_planted_alert(world):
    store, _ = world
    alerts = store.all("Alert")
    assert len(alerts) == 1
    a = alerts[0]
    assert a["sensorId"] == "SEN-2207-VIB"
    assert a["severity"] == "warning"
    assert a["signature"] == ("vibration_rms:rising_exponential+temperature_c:rising"
                              "+acoustic_db:rising")


def test_rul_model_is_plausible_and_confident(world):
    _, sim = world
    rul = predict_rul(sim, "SEN-2207-VIB")
    assert rul["status"] == "degrading"
    assert 5.0 <= rul["remainingUsefulLifeHours"] <= 40.0
    assert rul["confidence"] > 0.9
    assert rul["growthRatePerHour"] > 0
    assert math.isclose(math.log(2) / rul["growthRatePerHour"],
                        rul["doublingTimeHours"], rel_tol=0.02)


def test_rul_stable_on_healthy_sensor(world):
    _, sim = world
    rul = predict_rul(sim, "SEN-2208-VIB")
    assert rul["status"] in ("stable", "insufficient_data")
    assert rul.get("remainingUsefulLifeHours") is None


def test_health_scores_bounded_and_ordered(world):
    store, _ = world
    scores = {m["machineId"]: m["healthScore"] for m in store.all("Machine")}
    assert all(0 <= v <= 100 for v in scores.values())
    assert scores["MCH-2207"] == min(scores.values())
    assert scores["MCH-2207"] < 50 < scores["MCH-2208"]


def test_tick_advances_clock_appends_readings_and_escalates(world):
    store, sim = world
    before_clock = sim.clock()
    before_n = store.counts()["SensorReading"]
    for _ in range(12):                     # +6 h
        sim.tick(30)
    assert (sim.clock() - before_clock).total_seconds() == 6 * 3600
    assert store.counts()["SensorReading"] == before_n + 12 * 7
    # fault keeps growing and drags temperature over its warn limit eventually
    for _ in range(16):                     # +8 h more
        sim.tick(30)
    tmp = store.get("Sensor", "SEN-2207-TMP")
    assert tmp["latestValue"] > 68.0
    assert any(a["sensorId"] == "SEN-2207-TMP" for a in store.all("Alert"))


# ---------------------------------------------------------------------------
# Logic layer
# ---------------------------------------------------------------------------

def test_failure_mode_matching_is_discriminative(world):
    store, _ = world
    sig = store.all("Alert")[0]["signature"]
    m = match_failure_mode(store, sig, "cnc_vmc_spindle")
    assert m["bestMatch"]["failureModeId"] == "FM-BRG-OR-SPALL"
    assert m["bestMatch"]["matchScore"] == 1.0
    others = [c["matchScore"] for c in m["candidates"][1:]]
    assert all(s <= 0.5 for s in others)


def test_parts_availability_and_blockers(world):
    store, _ = world
    r = check_parts_availability(store, ["BRG-7208-P4", "SEAL-SP-32"], "SITE-COLUMBUS")
    assert r["allAvailableOnSite"] is True
    brg = next(l for l in r["lines"] if l["partSku"] == "BRG-7208-P4")
    assert brg["availableOnSite"] == 1                       # 2 on hand, 1 reserved
    assert brg["otherSites"][0]["siteId"] == "SITE-TOLEDO"
    store.patch("InventoryItem", "INV-COL-BRG7208", {"reserved": 2})
    r2 = check_parts_availability(store, ["BRG-7208-P4"], "SITE-COLUMBUS")
    assert r2["allAvailableOnSite"] is False
    assert r2["blockers"] == ["BRG-7208-P4"]


def test_revenue_at_risk_scales_with_downtime(world):
    store, _ = world
    small = compute_revenue_at_risk(store, "MCH-2207", 3.5)
    big = compute_revenue_at_risk(store, "MCH-2207", 14.0)
    assert big["marginLossUsd"] > small["marginLossUsd"] * 3.5
    assert big["contributionMarginPerHourUsd"] == small["contributionMarginPerHourUsd"]
    assert {o["productionOrderId"] for o in big["orders"]} == {"PO-8841", "PO-8843"}


def test_failure_probability_curve():
    assert failure_probability(20, None, 0.9) == 0.95
    early = failure_probability(20, 5, 0.95)
    mid = failure_probability(20, 15, 0.95)
    late = failure_probability(20, 26, 0.95)
    assert early < 0.1 < mid < late
    assert late >= 0.90
    # lower confidence pulls risk earlier
    assert failure_probability(20, 12, 0.5) > failure_probability(20, 12, 0.99)


def test_option_comparison_recommends_early_intervention(world):
    store, sim = world
    rul = predict_rul(sim, "SEN-2207-VIB")
    fm = store.get("FailureMode", "FM-BRG-OR-SPALL")
    windows = find_maintenance_windows(store, sim, "MCH-2207", fm["repairHours"])
    rows = compare_options(store, sim, "MCH-2207", rul, fm,
                           default_options(sim, store, "MCH-2207", windows))
    assert len(rows) == 3
    best = next(r for r in rows if r["recommended"])
    assert best["scenarioId"] == "SC-EARLY"
    assert best["totalExpectedCostUsd"] == min(r["totalExpectedCostUsd"] for r in rows)
    assert best["deltaVsWorstUsd"] > 10000


def test_aar_retrieval_reports_calibration(world):
    store, _ = world
    sig = store.all("Alert")[0]["signature"]
    h = search_after_action_reports(store, signature=sig, failure_mode_id="FM-BRG-OR-SPALL")
    assert h["count"] == 2
    assert h["avgDowntimeWhenOverridden"] > h["avgDowntimeWhenAccepted"]
    assert "conservative" in h["rulModelBias"]


def test_scenario_manager_diff_commit_discard(world):
    store, _ = world
    mgr = ScenarioManager(store)
    sid = mgr.create("test")
    store.patch("ProductionOrder", "PO-8843", {"lineId": "L3"}, branch=sid)
    diff = mgr.diff(sid)
    assert diff and diff[0]["primaryKey"] == "PO-8843"
    assert store.get("ProductionOrder", "PO-8843")["lineId"] == "L2"   # master untouched
    mgr.commit(sid)
    assert store.get("ProductionOrder", "PO-8843")["lineId"] == "L3"
    sid2 = mgr.create("throwaway")
    mgr.discard(sid2)
    assert sid2 not in store.branches
