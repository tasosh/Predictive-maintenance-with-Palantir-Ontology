"""
ontology.py — a faithful, in-memory stand-in for a Palantir Foundry Ontology.

Shapes mirror the Foundry v2 REST API (ontologies-v2-resources) so that swapping
this module for the real OSDK / REST client is a drop-in change:

    ObjectTypeV2   { apiName, displayName, status, description, primaryKey,
                     titleProperty, rid, properties{} }
    LinkTypeSideV2 { apiName, displayName, status, objectTypeApiName,
                     cardinality, foreignKeyPropertyApiName }
    OntologyObjectV2 -> { "__rid", "__primaryKey", "__apiName", ...properties }

Everything is branch-aware: `master` is the live Ontology, and Scenarios fork a
copy-on-write branch so what-if analysis never touches live state.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

ONTOLOGY_RID = "ri.ontology.main.ontology.7c5a0b91-4d32-4a17-9e64-2f1d8b3c7a20"
ONTOLOGY_API_NAME = "plant-ops-ontology"
ONTOLOGY_DISPLAY_NAME = "Plant Operations Ontology"
ONTOLOGY_DESCRIPTION = (
    "Predictive maintenance and production digital twin for Columbus Plant 3. "
    "Unifies historian telemetry, CMMS work management, ERP inventory and the "
    "MES production schedule into one decision surface."
)

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def rid_for(object_type: str, pk: str) -> str:
    """Deterministic, Foundry-shaped object RID."""
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"{ONTOLOGY_RID}/{object_type}/{pk}")
    return f"ri.phonograph2-objects.main.object.{ns}"


# ---------------------------------------------------------------------------
# Metadata: object types, properties, link types
# ---------------------------------------------------------------------------

def _p(t: str, desc: str = "", **kw) -> dict:
    d = {"dataType": {"type": t}, "description": desc, "rid": None}
    d.update(kw)
    return d


OBJECT_TYPES: dict[str, dict] = {
    "Machine": {
        "displayName": "Machine",
        "pluralDisplayName": "Machines",
        "primaryKey": "machineId",
        "titleProperty": "name",
        "icon": "factory",
        "description": "A physical production asset on the plant floor.",
        "sourceSystem": "SAP PM + OSIsoft PI asset framework",
        "properties": {
            "machineId": _p("string", "Asset tag (primary key)"),
            "name": _p("string"),
            "assetClass": _p("string"),
            "lineId": _p("string", "FK -> ProductionLine.lineId"),
            "siteId": _p("string"),
            "criticality": _p("string", "critical | high | medium | low"),
            "status": _p("string", "running | idle | down | maintenance"),
            "healthScore": _p("double", "0-100, derived from sensor trends"),
            "runHours": _p("double"),
            "unitsPerHour": _p("double", "Nameplate throughput"),
            "marginPerUnitUsd": _p("double", "Contribution margin per unit"),
            "unplannedRepairHours": _p("double", "Historical mean time to repair, unplanned"),
            "plannedRepairHours": _p("double"),
            "catastrophicCostUsd": _p("double", "Cost if run-to-failure escalates"),
            "lastServiceDate": _p("timestamp"),
        },
    },
    "Sensor": {
        "displayName": "Sensor",
        "pluralDisplayName": "Sensors",
        "primaryKey": "sensorId",
        "titleProperty": "tag",
        "icon": "pulse",
        "description": "A streaming telemetry channel bound to a Machine.",
        "sourceSystem": "OSIsoft PI / Kepware OPC-UA",
        "properties": {
            "sensorId": _p("string"),
            "tag": _p("string"),
            "machineId": _p("string", "FK -> Machine.machineId"),
            "measure": _p("string", "vibration_rms | temperature_c | spindle_load_pct | acoustic_db"),
            "unit": _p("string"),
            "warnThreshold": _p("double"),
            "critThreshold": _p("double"),
            "latestValue": _p("double"),
            "latestTimestamp": _p("timestamp"),
        },
    },
    "SensorReading": {
        "displayName": "Sensor Reading",
        "pluralDisplayName": "Sensor Readings",
        "primaryKey": "readingId",
        "titleProperty": "readingId",
        "icon": "timeseries",
        "description": "Time-series point. In Foundry this is a time series property "
                       "on Sensor backed by a streaming dataset.",
        "sourceSystem": "Streaming dataset (Foundry Streams)",
        "properties": {
            "readingId": _p("string"),
            "sensorId": _p("string", "FK -> Sensor.sensorId"),
            "timestamp": _p("timestamp"),
            "value": _p("double"),
        },
    },
    "Alert": {
        "displayName": "Alert",
        "pluralDisplayName": "Alerts",
        "primaryKey": "alertId",
        "titleProperty": "title",
        "icon": "warning",
        "description": "Threshold or trend breach raised against a Machine.",
        "sourceSystem": "Foundry streaming pipeline + health checks",
        "properties": {
            "alertId": _p("string"),
            "title": _p("string"),
            "machineId": _p("string", "FK -> Machine.machineId"),
            "sensorId": _p("string", "FK -> Sensor.sensorId"),
            "severity": _p("string", "critical | warning | info"),
            "status": _p("string", "open | triaged | resolved | suppressed"),
            "raisedAt": _p("timestamp"),
            "acknowledgedAt": _p("timestamp"),
            "resolvedAt": _p("timestamp"),
            "signature": _p("string", "Normalised fault signature used to match FailureMode"),
            "triageLatencySeconds": _p("double", "Raised -> decision, the headline metric"),
        },
    },
    "FailureMode": {
        "displayName": "Failure Mode",
        "pluralDisplayName": "Failure Modes",
        "primaryKey": "failureModeId",
        "titleProperty": "name",
        "icon": "diagnosis",
        "description": "Reliability-engineering knowledge, modelled as objects rather "
                       "than PDFs, so the agent reasons over it instead of retrieving text.",
        "sourceSystem": "Reliability engineering FMEA workbook",
        "properties": {
            "failureModeId": _p("string"),
            "name": _p("string"),
            "assetClass": _p("string"),
            "signature": _p("string"),
            "typicalLeadTimeHours": _p("double", "Warning -> functional failure"),
            "repairHours": _p("double"),
            "requiredPartSkus": _p("array<string>"),
            "escalationProbability": _p("double", "P(secondary damage | run to failure)"),
            "detectionNotes": _p("string"),
        },
    },
    "WorkOrder": {
        "displayName": "Work Order",
        "pluralDisplayName": "Work Orders",
        "primaryKey": "workOrderId",
        "titleProperty": "summary",
        "icon": "wrench",
        "description": "Maintenance job. Writes back to SAP PM.",
        "sourceSystem": "SAP PM (bidirectional writeback)",
        "properties": {
            "workOrderId": _p("string"),
            "summary": _p("string"),
            "machineId": _p("string", "FK -> Machine.machineId"),
            "alertId": _p("string", "FK -> Alert.alertId"),
            "failureModeId": _p("string", "FK -> FailureMode.failureModeId"),
            "orderType": _p("string", "PM (preventive) | CM (corrective) | EM (emergency)"),
            "priority": _p("string"),
            "status": _p("string", "draft | pending_approval | scheduled | in_progress | complete | rejected"),
            "scheduledStart": _p("timestamp"),
            "durationHours": _p("double"),
            "technicianId": _p("string", "FK -> Technician.technicianId"),
            "estimatedCostUsd": _p("double"),
            "createdBy": _p("string"),
            "approvedBy": _p("string"),
            "sourceSystemRef": _p("string", "SAP PM order number once written back"),
        },
    },
    "Part": {
        "displayName": "Part",
        "pluralDisplayName": "Parts",
        "primaryKey": "partSku",
        "titleProperty": "name",
        "icon": "cog",
        "description": "Spare part master record.",
        "sourceSystem": "SAP MM",
        "properties": {
            "partSku": _p("string"),
            "name": _p("string"),
            "supplierId": _p("string", "FK -> Supplier.supplierId"),
            "unitCostUsd": _p("double"),
            "leadTimeDays": _p("double"),
            "fitsAssetClasses": _p("array<string>"),
        },
    },
    "InventoryItem": {
        "displayName": "Inventory Item",
        "pluralDisplayName": "Inventory",
        "primaryKey": "inventoryId",
        "titleProperty": "inventoryId",
        "icon": "box",
        "description": "Stock of a Part at a storage location.",
        "sourceSystem": "SAP MM stock projection",
        "properties": {
            "inventoryId": _p("string"),
            "partSku": _p("string", "FK -> Part.partSku"),
            "siteId": _p("string"),
            "binLocation": _p("string"),
            "onHand": _p("integer"),
            "reserved": _p("integer"),
            "reorderPoint": _p("integer"),
        },
    },
    "Supplier": {
        "displayName": "Supplier",
        "pluralDisplayName": "Suppliers",
        "primaryKey": "supplierId",
        "titleProperty": "name",
        "icon": "truck",
        "description": "Vendor of record for a Part.",
        "sourceSystem": "Ariba",
        "properties": {
            "supplierId": _p("string"),
            "name": _p("string"),
            "onTimeDeliveryRate": _p("double"),
            "expediteAvailable": _p("boolean"),
            "expediteFeeUsd": _p("double"),
            "expediteLeadTimeDays": _p("double"),
        },
    },
    "ProductionLine": {
        "displayName": "Production Line",
        "pluralDisplayName": "Production Lines",
        "primaryKey": "lineId",
        "titleProperty": "name",
        "icon": "conveyor",
        "description": "Sequence of machines producing a product family.",
        "sourceSystem": "MES",
        "properties": {
            "lineId": _p("string"),
            "name": _p("string"),
            "siteId": _p("string"),
            "shiftPattern": _p("string"),
            "nextChangeoverAt": _p("timestamp", "Existing planned downtime window"),
            "changeoverWindowHours": _p("double"),
        },
    },
    "ProductionOrder": {
        "displayName": "Production Order",
        "pluralDisplayName": "Production Orders",
        "primaryKey": "productionOrderId",
        "titleProperty": "productionOrderId",
        "icon": "clipboard",
        "description": "Customer demand allocated to a line. This is what makes a "
                       "vibration reading a business decision.",
        "sourceSystem": "SAP PP / MES schedule",
        "properties": {
            "productionOrderId": _p("string"),
            "customer": _p("string"),
            "lineId": _p("string", "FK -> ProductionLine.lineId"),
            "unitsRemaining": _p("integer"),
            "dueAt": _p("timestamp"),
            "revenueUsd": _p("double"),
            "latePenaltyUsdPerDay": _p("double"),
            "priority": _p("string"),
            "status": _p("string"),
            "contractualNotice": _p("boolean", "Customer must be notified if slip > 4h"),
        },
    },
    "Technician": {
        "displayName": "Technician",
        "pluralDisplayName": "Technicians",
        "primaryKey": "technicianId",
        "titleProperty": "name",
        "icon": "person",
        "description": "Maintenance labour with skills and shift availability.",
        "sourceSystem": "Workday + Kronos",
        "properties": {
            "technicianId": _p("string"),
            "name": _p("string"),
            "siteId": _p("string"),
            "shift": _p("string"),
            "skills": _p("array<string>"),
            "availableFrom": _p("timestamp"),
            "hourlyRateUsd": _p("double"),
        },
    },
    "AfterActionReport": {
        "displayName": "After Action Report",
        "pluralDisplayName": "After Action Reports",
        "primaryKey": "aarId",
        "titleProperty": "aarId",
        "icon": "history",
        "description": "Closed-loop learning record. Every decision outcome lands here "
                       "and is retrieved by the agent on the next similar alert.",
        "sourceSystem": "Written back by this application",
        "properties": {
            "aarId": _p("string"),
            "alertId": _p("string"),
            "machineId": _p("string"),
            "failureModeId": _p("string"),
            "signature": _p("string"),
            "decision": _p("string"),
            "recommendationAccepted": _p("boolean"),
            "predictedRulHours": _p("double"),
            "actualHoursToFailure": _p("double"),
            "downtimeHours": _p("double"),
            "costUsd": _p("double"),
            "revenueProtectedUsd": _p("double"),
            "decisionLatencySeconds": _p("double"),
            "notes": _p("string"),
            "recordedAt": _p("timestamp"),
        },
    },
    "AuditEvent": {
        "displayName": "Audit Event",
        "pluralDisplayName": "Audit Events",
        "primaryKey": "eventId",
        "titleProperty": "actionType",
        "icon": "shield",
        "description": "Immutable record of every action application, approval and writeback.",
        "sourceSystem": "Foundry audit log",
        "properties": {
            "eventId": _p("string"),
            "timestamp": _p("timestamp"),
            "actor": _p("string"),
            "actorType": _p("string", "human | agent | system"),
            "actionType": _p("string"),
            "parameters": _p("string"),
            "affectedObjectRids": _p("array<string>"),
            "branch": _p("string"),
            "decision": _p("string", "applied | auto_approved | pending_approval | rejected | simulated"),
            "justification": _p("string"),
            "writebackTarget": _p("string"),
        },
    },
}

# link apiName -> (sourceType, targetType, cardinality, foreignKeyProperty, inverse)
LINK_TYPES: dict[str, tuple] = {
    "sensors":            ("Machine", "Sensor", "MANY", "machineId", "machine"),
    "machine":            ("Sensor", "Machine", "ONE", "machineId", "sensors"),
    "readings":           ("Sensor", "SensorReading", "MANY", "sensorId", "sensor"),
    "sensor":             ("SensorReading", "Sensor", "ONE", "sensorId", "readings"),
    "alerts":             ("Machine", "Alert", "MANY", "machineId", "alertMachine"),
    "alertMachine":       ("Alert", "Machine", "ONE", "machineId", "alerts"),
    "workOrders":         ("Machine", "WorkOrder", "MANY", "machineId", "workOrderMachine"),
    "workOrderMachine":   ("WorkOrder", "Machine", "ONE", "machineId", "workOrders"),
    "workOrderAlert":     ("WorkOrder", "Alert", "ONE", "alertId", None),
    "workOrderTechnician": ("WorkOrder", "Technician", "ONE", "technicianId", None),
    "line":               ("Machine", "ProductionLine", "ONE", "lineId", "machines"),
    "machines":           ("ProductionLine", "Machine", "MANY", "lineId", "line"),
    "productionOrders":   ("ProductionLine", "ProductionOrder", "MANY", "lineId", "productionLine"),
    "productionLine":     ("ProductionOrder", "ProductionLine", "ONE", "lineId", "productionOrders"),
    "inventory":          ("Part", "InventoryItem", "MANY", "partSku", "part"),
    "part":               ("InventoryItem", "Part", "ONE", "partSku", "inventory"),
    "supplier":           ("Part", "Supplier", "ONE", "supplierId", "parts"),
    "parts":              ("Supplier", "Part", "MANY", "supplierId", "supplier"),
    "afterActionReports": ("Machine", "AfterActionReport", "MANY", "machineId", None),
}


def outgoing_links(object_type: str) -> dict[str, tuple]:
    return {k: v for k, v in LINK_TYPES.items() if v[0] == object_type}


# ---------------------------------------------------------------------------
# Branch-aware store
# ---------------------------------------------------------------------------

@dataclass
class Branch:
    name: str
    parent: str | None
    objects: dict[str, dict[str, dict]]
    created_at: dt.datetime = field(default_factory=now)
    description: str = ""


class OntologyStore:
    """Copy-on-write, branch-aware object store with Foundry-shaped reads."""

    def __init__(self) -> None:
        self.branches: dict[str, Branch] = {
            "master": Branch("master", None, {k: {} for k in OBJECT_TYPES}, description="Live Ontology")
        }
        self._counters: dict[str, itertools.count] = {}

    # -- branching ---------------------------------------------------------
    def fork(self, name: str, source: str = "master", description: str = "") -> Branch:
        src = self.branches[source]
        self.branches[name] = Branch(
            name=name, parent=source,
            objects=copy.deepcopy(src.objects),
            description=description,
        )
        return self.branches[name]

    def drop(self, name: str) -> None:
        if name != "master":
            self.branches.pop(name, None)

    def merge(self, name: str, into: str = "master",
              types: Iterable[str] | None = None) -> list[str]:
        """Commit a scenario branch back onto the live Ontology."""
        src, dst = self.branches[name], self.branches[into]
        changed: list[str] = []
        for otype in (types or OBJECT_TYPES.keys()):
            for pk, obj in src.objects.get(otype, {}).items():
                before = dst.objects[otype].get(pk)
                if before != obj:
                    dst.objects[otype][pk] = copy.deepcopy(obj)
                    changed.append(f"{otype}:{pk}")
        return changed

    # -- writes ------------------------------------------------------------
    def put(self, otype: str, obj: dict, branch: str = "master") -> dict:
        pk_prop = OBJECT_TYPES[otype]["primaryKey"]
        pk = str(obj[pk_prop])
        obj = dict(obj)
        obj["__apiName"] = otype
        obj["__primaryKey"] = pk
        obj["__rid"] = rid_for(otype, pk)
        self.branches[branch].objects[otype][pk] = obj
        return obj

    def patch(self, otype: str, pk: str, changes: dict, branch: str = "master") -> dict:
        obj = self.branches[branch].objects[otype][str(pk)]
        obj.update(changes)
        return obj

    def next_id(self, prefix: str, width: int = 4) -> str:
        c = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}-{next(c):0{width}d}"

    def next_pk(self, otype: str, prefix: str, width: int = 4) -> str:
        """Collision-proof primary key: skips ids already taken on any branch."""
        while True:
            pk = self.next_id(prefix, width)
            if not any(pk in br.objects.get(otype, {}) for br in self.branches.values()):
                return pk

    # -- reads -------------------------------------------------------------
    def get(self, otype: str, pk: str, branch: str = "master") -> dict | None:
        return self.branches[branch].objects.get(otype, {}).get(str(pk))

    def all(self, otype: str, branch: str = "master") -> list[dict]:
        return list(self.branches[branch].objects.get(otype, {}).values())

    def search(self, otype: str, where: dict | None = None,
               predicate: Callable[[dict], bool] | None = None,
               order_by: str | None = None, descending: bool = False,
               limit: int | None = None, branch: str = "master") -> list[dict]:
        rows = self.all(otype, branch)
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        if predicate:
            rows = [r for r in rows if predicate(r)]
        if order_by:
            rows = sorted(rows, key=lambda r: (r.get(order_by) is None, r.get(order_by)),
                          reverse=descending)
        return rows[:limit] if limit else rows

    def linked(self, otype: str, pk: str, link: str, branch: str = "master") -> list[dict]:
        if link not in LINK_TYPES:
            raise KeyError(f"Unknown link '{link}' on {otype}. "
                           f"Available: {sorted(outgoing_links(otype))}")
        src, tgt, card, fk, _ = LINK_TYPES[link]
        if src != otype:
            raise KeyError(f"Link '{link}' is defined on {src}, not {otype}")
        obj = self.get(otype, pk, branch)
        if obj is None:
            return []
        if card == "ONE":
            fk_value = obj.get(fk)
            hit = self.get(tgt, fk_value, branch) if fk_value else None
            return [hit] if hit else []
        return [r for r in self.all(tgt, branch) if str(r.get(fk)) == str(pk)]

    def aggregate(self, otype: str, group_by: str, metric: str = "count",
                  prop: str | None = None, branch: str = "master") -> dict:
        out: dict[str, float] = {}
        for r in self.all(otype, branch):
            k = str(r.get(group_by))
            if metric == "count":
                out[k] = out.get(k, 0) + 1
            else:
                out[k] = out.get(k, 0) + float(r.get(prop) or 0)
        return out

    def counts(self, branch: str = "master") -> dict[str, int]:
        return {k: len(v) for k, v in self.branches[branch].objects.items()}


# ---------------------------------------------------------------------------
# Seed data — synthetic, but dimensionally realistic for a mid-size CNC plant
# ---------------------------------------------------------------------------

def seed(store: OntologyStore) -> None:
    t0 = now()

    store.put("Supplier", {
        "supplierId": "SUP-NSK", "name": "NSK Bearings NA",
        "onTimeDeliveryRate": 0.94, "expediteAvailable": True,
        "expediteFeeUsd": 1250.0, "expediteLeadTimeDays": 2.0,
    })
    store.put("Supplier", {
        "supplierId": "SUP-HAAS", "name": "Haas Automation Service Parts",
        "onTimeDeliveryRate": 0.88, "expediteAvailable": True,
        "expediteFeeUsd": 3400.0, "expediteLeadTimeDays": 4.0,
    })

    for p in [
        {"partSku": "BRG-7208-P4", "name": "Spindle bearing set, angular contact P4",
         "supplierId": "SUP-NSK", "unitCostUsd": 840.0, "leadTimeDays": 12.0,
         "fitsAssetClasses": ["cnc_vmc_spindle"]},
        {"partSku": "SEAL-SP-32", "name": "Spindle nose seal kit", "supplierId": "SUP-HAAS",
         "unitCostUsd": 145.0, "leadTimeDays": 6.0, "fitsAssetClasses": ["cnc_vmc_spindle"]},
        {"partSku": "SPINDLE-VF4-ASM", "name": "Spindle cartridge assembly (exchange)",
         "supplierId": "SUP-HAAS", "unitCostUsd": 24500.0, "leadTimeDays": 21.0,
         "fitsAssetClasses": ["cnc_vmc_spindle"]},
        {"partSku": "BELT-DRV-88", "name": "Drive belt, 88T", "supplierId": "SUP-HAAS",
         "unitCostUsd": 95.0, "leadTimeDays": 4.0, "fitsAssetClasses": ["cnc_vmc_spindle", "press"]},
    ]:
        store.put("Part", p)

    for inv in [
        {"inventoryId": "INV-COL-BRG7208", "partSku": "BRG-7208-P4", "siteId": "SITE-COLUMBUS",
         "binLocation": "CR-04-B2", "onHand": 2, "reserved": 1, "reorderPoint": 2},
        {"inventoryId": "INV-COL-SEAL32", "partSku": "SEAL-SP-32", "siteId": "SITE-COLUMBUS",
         "binLocation": "CR-04-B3", "onHand": 6, "reserved": 0, "reorderPoint": 3},
        {"inventoryId": "INV-COL-SPINDLE", "partSku": "SPINDLE-VF4-ASM", "siteId": "SITE-COLUMBUS",
         "binLocation": "CR-01-A1", "onHand": 0, "reserved": 0, "reorderPoint": 1},
        {"inventoryId": "INV-TOL-BRG7208", "partSku": "BRG-7208-P4", "siteId": "SITE-TOLEDO",
         "binLocation": "ST-02-C1", "onHand": 5, "reserved": 0, "reorderPoint": 2},
    ]:
        store.put("InventoryItem", inv)

    store.put("ProductionLine", {
        "lineId": "L2", "name": "Line 2 — Aerospace brackets", "siteId": "SITE-COLUMBUS",
        "shiftPattern": "3x8 continuous",
        "nextChangeoverAt": iso(t0 + dt.timedelta(hours=26)), "changeoverWindowHours": 4.0,
    })
    store.put("ProductionLine", {
        "lineId": "L3", "name": "Line 3 — General machining", "siteId": "SITE-COLUMBUS",
        "shiftPattern": "2x8",
        "nextChangeoverAt": iso(t0 + dt.timedelta(hours=9)), "changeoverWindowHours": 6.0,
    })

    store.put("Machine", {
        "machineId": "MCH-2207", "name": "VMC-07 Spindle Cell", "assetClass": "cnc_vmc_spindle",
        "lineId": "L2", "siteId": "SITE-COLUMBUS", "criticality": "critical", "status": "running",
        "healthScore": 61.0, "runHours": 21840.0, "unitsPerHour": 42.0, "marginPerUnitUsd": 38.0,
        "unplannedRepairHours": 14.0, "plannedRepairHours": 3.5, "catastrophicCostUsd": 28500.0,
        "lastServiceDate": iso(t0 - dt.timedelta(days=118)),
    })
    store.put("Machine", {
        "machineId": "MCH-2208", "name": "VMC-08 Spindle Cell", "assetClass": "cnc_vmc_spindle",
        "lineId": "L2", "siteId": "SITE-COLUMBUS", "criticality": "high", "status": "running",
        "healthScore": 93.0, "runHours": 9120.0, "unitsPerHour": 38.0, "marginPerUnitUsd": 38.0,
        "unplannedRepairHours": 12.0, "plannedRepairHours": 3.0, "catastrophicCostUsd": 24000.0,
        "lastServiceDate": iso(t0 - dt.timedelta(days=24)),
    })
    store.put("Machine", {
        "machineId": "MCH-3101", "name": "Press-01 400T", "assetClass": "press",
        "lineId": "L3", "siteId": "SITE-COLUMBUS", "criticality": "medium", "status": "running",
        "healthScore": 88.0, "runHours": 40210.0, "unitsPerHour": 120.0, "marginPerUnitUsd": 9.0,
        "unplannedRepairHours": 8.0, "plannedRepairHours": 2.0, "catastrophicCostUsd": 9000.0,
        "lastServiceDate": iso(t0 - dt.timedelta(days=61)),
    })

    sensors = [
        ("SEN-2207-VIB", "MCH-2207", "vibration_rms", "mm/s", 4.5, 7.1),
        ("SEN-2207-TMP", "MCH-2207", "temperature_c", "degC", 68.0, 82.0),
        ("SEN-2207-LOAD", "MCH-2207", "spindle_load_pct", "%", 85.0, 95.0),
        ("SEN-2207-ACU", "MCH-2207", "acoustic_db", "dB", 94.0, 99.0),
        ("SEN-2208-VIB", "MCH-2208", "vibration_rms", "mm/s", 4.5, 7.1),
        ("SEN-2208-TMP", "MCH-2208", "temperature_c", "degC", 68.0, 82.0),
        ("SEN-3101-VIB", "MCH-3101", "vibration_rms", "mm/s", 6.0, 9.0),
    ]
    for sid, mid, measure, unit, warn, crit in sensors:
        store.put("Sensor", {
            "sensorId": sid, "tag": f"PI:{mid}.{measure.upper()}", "machineId": mid,
            "measure": measure, "unit": unit, "warnThreshold": warn, "critThreshold": crit,
            "latestValue": None, "latestTimestamp": None,
        })

    store.put("FailureMode", {
        "failureModeId": "FM-BRG-OR-SPALL", "name": "Spindle bearing outer-race spalling",
        "assetClass": "cnc_vmc_spindle",
        "signature": "vibration_rms:rising_exponential+temperature_c:rising+acoustic_db:rising",
        "typicalLeadTimeHours": 30.0, "repairHours": 3.5,
        "requiredPartSkus": ["BRG-7208-P4", "SEAL-SP-32"], "escalationProbability": 0.35,
        "detectionNotes": "BPFO harmonics with sidebands; RMS doubles roughly every 30 run-hours "
                          "once the knee is passed. Secondary damage to the spindle cartridge "
                          "if run past crit threshold.",
    })
    store.put("FailureMode", {
        "failureModeId": "FM-BELT-WEAR", "name": "Drive belt glazing / slip",
        "assetClass": "cnc_vmc_spindle",
        "signature": "spindle_load_pct:rising+vibration_rms:flat",
        "typicalLeadTimeHours": 120.0, "repairHours": 1.5,
        "requiredPartSkus": ["BELT-DRV-88"], "escalationProbability": 0.05,
        "detectionNotes": "Load climbs at constant vibration. Low urgency, batch with next PM.",
    })
    store.put("FailureMode", {
        "failureModeId": "FM-COOL-DEGRADE", "name": "Coolant degradation / thermal drift",
        "assetClass": "cnc_vmc_spindle",
        "signature": "temperature_c:rising+vibration_rms:flat",
        "typicalLeadTimeHours": 200.0, "repairHours": 1.0,
        "requiredPartSkus": [], "escalationProbability": 0.02,
        "detectionNotes": "Temperature-only excursion. Usually coolant concentration, not mechanical.",
    })

    for po in [
        {"productionOrderId": "PO-8841", "customer": "Acme Aerospace", "lineId": "L2",
         "unitsRemaining": 1430, "dueAt": iso(t0 + dt.timedelta(days=6)),
         "revenueUsd": 412000.0, "latePenaltyUsdPerDay": 18000.0, "priority": "P1",
         "status": "in_progress", "contractualNotice": True},
        {"productionOrderId": "PO-8843", "customer": "Midwest Tooling", "lineId": "L2",
         "unitsRemaining": 260, "dueAt": iso(t0 + dt.timedelta(days=11)),
         "revenueUsd": 48000.0, "latePenaltyUsdPerDay": 0.0, "priority": "P3",
         "status": "queued", "contractualNotice": False},
        {"productionOrderId": "PO-8852", "customer": "Northgate Medical", "lineId": "L3",
         "unitsRemaining": 900, "dueAt": iso(t0 + dt.timedelta(days=4)),
         "revenueUsd": 96000.0, "latePenaltyUsdPerDay": 4000.0, "priority": "P2",
         "status": "in_progress", "contractualNotice": False},
    ]:
        store.put("ProductionOrder", po)

    for tech in [
        {"technicianId": "TECH-11", "name": "R. Delgado", "siteId": "SITE-COLUMBUS",
         "shift": "nights (22:00-06:00)", "skills": ["spindle_rebuild", "vibration_analysis"],
         "availableFrom": iso(t0 + dt.timedelta(hours=6)), "hourlyRateUsd": 68.0},
        {"technicianId": "TECH-04", "name": "S. Okafor", "siteId": "SITE-COLUMBUS",
         "shift": "days (06:00-14:00)", "skills": ["hydraulics", "press_maintenance"],
         "availableFrom": iso(t0 + dt.timedelta(hours=2)), "hourlyRateUsd": 61.0},
        {"technicianId": "TECH-19", "name": "K. Brandt", "siteId": "SITE-COLUMBUS",
         "shift": "swing (14:00-22:00)", "skills": ["spindle_rebuild", "electrical"],
         "availableFrom": iso(t0 + dt.timedelta(hours=19)), "hourlyRateUsd": 72.0},
    ]:
        store.put("Technician", tech)

    # Historical closed-loop memory the agent will retrieve.
    for aar in [
        {"aarId": "AAR-0001", "alertId": "ALR-HIST-1", "machineId": "MCH-2208",
         "failureModeId": "FM-BRG-OR-SPALL",
         "signature": "vibration_rms:rising_exponential+temperature_c:rising+acoustic_db:rising",
         "decision": "scheduled_cm_before_crit", "recommendationAccepted": True,
         "predictedRulHours": 26.0, "actualHoursToFailure": 31.0, "downtimeHours": 3.8,
         "costUsd": 1420.0, "revenueProtectedUsd": 15200.0, "decisionLatencySeconds": 5400,
         "recordedAt": iso(t0 - dt.timedelta(days=96)),
         "notes": "Bearing replaced in changeover window. Teardown confirmed outer-race spall. "
                  "RUL model was 5h conservative."},
        {"aarId": "AAR-0002", "alertId": "ALR-HIST-2", "machineId": "MCH-2207",
         "failureModeId": "FM-BRG-OR-SPALL",
         "signature": "vibration_rms:rising_exponential+temperature_c:rising+acoustic_db:rising",
         "decision": "deferred_to_next_pm", "recommendationAccepted": False,
         "predictedRulHours": 22.0, "actualHoursToFailure": 19.0, "downtimeHours": 16.5,
         "costUsd": 31200.0, "revenueProtectedUsd": 0.0, "decisionLatencySeconds": 19800,
         "recordedAt": iso(t0 - dt.timedelta(days=214)),
         "notes": "Deferred to next PM against recommendation. Failed mid-shift 19h later, "
                  "spindle cartridge scrapped, 16.5h unplanned downtime, PO-7712 shipped late."},
        {"aarId": "AAR-0003", "alertId": "ALR-HIST-3", "machineId": "MCH-3101",
         "failureModeId": "FM-COOL-DEGRADE", "signature": "temperature_c:rising+vibration_rms:flat",
         "decision": "no_action_monitor", "recommendationAccepted": True,
         "predictedRulHours": 180.0, "actualHoursToFailure": None, "downtimeHours": 0.0,
         "costUsd": 180.0, "revenueProtectedUsd": 0.0, "decisionLatencySeconds": 900,
         "recordedAt": iso(t0 - dt.timedelta(days=40)),
         "notes": "Coolant concentration corrected on shift. No mechanical issue. "
                  "Avoided an unnecessary 3h teardown."},
    ]:
        store.put("AfterActionReport", aar)

    # A couple of historic work orders for context.
    store.put("WorkOrder", {
        "workOrderId": "WO-1188", "summary": "PM — 500h spindle lubrication service",
        "machineId": "MCH-2207", "alertId": None, "failureModeId": None, "orderType": "PM",
        "priority": "routine", "status": "complete",
        "scheduledStart": iso(t0 - dt.timedelta(days=118)), "durationHours": 2.0,
        "technicianId": "TECH-11", "estimatedCostUsd": 340.0, "createdBy": "sap.pm.scheduler",
        "approvedBy": "j.reyes", "sourceSystemRef": "SAP-PM-4410188",
    })


def build_ontology() -> OntologyStore:
    store = OntologyStore()
    seed(store)
    return store
