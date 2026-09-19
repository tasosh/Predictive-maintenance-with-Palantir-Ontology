"""
simulator.py — synthetic plant-floor telemetry with a planted, physically
plausible fault, plus the RUL model the agent calls as an Ontology Query.

No customer data required. The fault is deterministic (fixed seed) so the demo
tells the same story every time, but the numbers on screen are all *computed*
from the generated series rather than hardcoded.
"""

from __future__ import annotations

import datetime as dt
import math
import random

from ontology import OntologyStore, iso, now

HISTORY_HOURS = 72
SAMPLE_MINUTES = 30

# Fault profile for MCH-2207 spindle bearing: flat, then an exponential knee.
FAULT_SENSOR = "SEN-2207-VIB"
FAULT_ONSET_H = 24.0      # hours into the 72h window
FAULT_A = 1.594           # amplitude
FAULT_K = 0.0216          # growth rate per hour


MEASURE_LABELS = {
    "vibration_rms": "Vibration RMS", "temperature_c": "Spindle temperature",
    "spindle_load_pct": "Spindle load", "acoustic_db": "Acoustic emission",
}


def _healthy_profile(measure: str, h: float, rng: random.Random) -> float:
    """Baseline behaviour with shift-correlated variation."""
    shift = math.sin(h / 8.0 * math.pi)
    if measure == "vibration_rms":
        return 1.90 + 0.09 * shift + rng.gauss(0, 0.055)
    if measure == "temperature_c":
        return 58.0 + 2.4 * shift + rng.gauss(0, 0.5)
    if measure == "spindle_load_pct":
        return 62.0 + 5.0 * shift + rng.gauss(0, 1.6)
    if measure == "acoustic_db":
        return 78.0 + 1.5 * shift + rng.gauss(0, 0.7)
    return rng.gauss(0, 1)


def _fault_delta(measure: str, h: float) -> float:
    """Coupled fault contribution. Vibration leads, temperature and acoustics follow."""
    if h < FAULT_ONSET_H:
        return 0.0
    growth = FAULT_A * (math.exp(FAULT_K * (h - FAULT_ONSET_H)) - 1.0)
    if measure == "vibration_rms":
        return growth
    if measure == "temperature_c":
        return 3.1 * growth            # friction heating, lags in amplitude
    if measure == "acoustic_db":
        return 3.4 * growth
    if measure == "spindle_load_pct":
        return 1.2 * growth            # barely moves — this is why single-signal alarms miss it
    return 0.0


class PlantSimulator:
    """Generates history, then advances the plant in ticks."""

    def __init__(self, store: OntologyStore, seed: int = 7) -> None:
        self.store = store
        self.rng = random.Random(seed)
        self.sim_hours = float(HISTORY_HOURS)   # 'now' sits at the end of history
        self.t0 = now() - dt.timedelta(hours=HISTORY_HOURS)
        self._reading_seq = 0
        self.frozen = True                      # live streaming off by default

    # -- generation --------------------------------------------------------
    def _value(self, sensor: dict, h: float) -> float:
        measure = sensor["measure"]
        base = _healthy_profile(measure, h, self.rng)
        if sensor["sensorId"] == FAULT_SENSOR:
            return base + _fault_delta(measure, h)
        if sensor["machineId"] == "MCH-2207":
            return base + _fault_delta(measure, h)
        return base

    def _emit(self, sensor: dict, h: float) -> dict:
        ts = self.t0 + dt.timedelta(hours=h)
        self._reading_seq += 1
        value = round(self._value(sensor, h), 3)
        reading = self.store.put("SensorReading", {
            "readingId": f"RD-{self._reading_seq:06d}",
            "sensorId": sensor["sensorId"], "timestamp": iso(ts), "value": value,
        })
        self.store.patch("Sensor", sensor["sensorId"],
                         {"latestValue": value, "latestTimestamp": iso(ts)})
        return reading

    def bootstrap(self) -> None:
        step = SAMPLE_MINUTES / 60.0
        steps = int(HISTORY_HOURS / step)
        for sensor in self.store.all("Sensor"):
            for i in range(steps + 1):
                self._emit(sensor, i * step)
        self._refresh_health()
        self.evaluate_alerts()

    def tick(self, minutes: int = SAMPLE_MINUTES) -> list[dict]:
        """Advance simulated time and return any newly raised alerts."""
        self.sim_hours += minutes / 60.0
        for sensor in self.store.all("Sensor"):
            self._emit(sensor, self.sim_hours)
        self._refresh_health()
        return self.evaluate_alerts()

    def clock(self) -> dt.datetime:
        return self.t0 + dt.timedelta(hours=self.sim_hours)

    # -- derived state -----------------------------------------------------
    def _refresh_health(self) -> None:
        for machine in self.store.all("Machine"):
            scores = []
            for sensor in self.store.linked("Machine", machine["machineId"], "sensors"):
                v, warn, crit = sensor["latestValue"], sensor["warnThreshold"], sensor["critThreshold"]
                if v is None:
                    continue
                # 100 = comfortably below warn, 50 = at the warn limit, 0 = at critical
                if v <= warn:
                    score = 50.0 + 50.0 * (warn - v) / max(warn * 0.25, 1e-6)
                else:
                    score = 50.0 * (crit - v) / max(crit - warn, 1e-6)
                scores.append(max(0.0, min(100.0, score)))
            if scores:
                self.store.patch("Machine", machine["machineId"],
                                 {"healthScore": round(min(scores), 1)})

    def series(self, sensor_id: str, hours: int = HISTORY_HOURS) -> list[dict]:
        rows = self.store.search("SensorReading", where={"sensorId": sensor_id},
                                 order_by="timestamp")
        cutoff = self.clock() - dt.timedelta(hours=hours)
        return [r for r in rows if dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) >= cutoff]

    # -- alerting ----------------------------------------------------------
    def _signature(self, machine_id: str) -> str:
        """Classify the multi-sensor trend into a FailureMode signature string."""
        parts = []
        for sensor in self.store.linked("Machine", machine_id, "sensors"):
            rows = self.series(sensor["sensorId"], hours=24)
            if len(rows) < 6:
                continue
            first = sum(r["value"] for r in rows[:4]) / 4
            last = sum(r["value"] for r in rows[-4:]) / 4
            span = max(abs(first), 1e-6)
            rel = (last - first) / span
            if sensor["measure"] == "vibration_rms":
                trend = "rising_exponential" if rel > 0.18 else ("rising" if rel > 0.05 else "flat")
            else:
                trend = "rising" if rel > 0.05 else "flat"
            parts.append(f"{sensor['measure']}:{trend}")
        order = {"vibration_rms": 0, "temperature_c": 1, "acoustic_db": 2, "spindle_load_pct": 3}
        parts.sort(key=lambda p: order.get(p.split(":")[0], 9))
        return "+".join(p for p in parts if not p.endswith(":flat")) or "nominal"

    def evaluate_alerts(self) -> list[dict]:
        raised = []
        for sensor in self.store.all("Sensor"):
            v = sensor["latestValue"]
            if v is None:
                continue
            severity = ("critical" if v >= sensor["critThreshold"]
                        else "warning" if v >= sensor["warnThreshold"] else None)
            if severity is None:
                continue
            existing = [a for a in self.store.search("Alert", where={"sensorId": sensor["sensorId"]})
                        if a["status"] in ("open", "triaged")]
            if existing:
                if severity == "critical" and existing[0]["severity"] != "critical":
                    self.store.patch("Alert", existing[0]["alertId"], {"severity": "critical"})
                continue
            machine = self.store.get("Machine", sensor["machineId"])
            alert = self.store.put("Alert", {
                "alertId": self.store.next_pk("Alert", "ALR"),
                "title": f"{MEASURE_LABELS.get(sensor['measure'], sensor['measure'])} "
                         f"{severity} on {machine['name']}",
                "machineId": sensor["machineId"], "sensorId": sensor["sensorId"],
                "severity": severity, "status": "open", "raisedAt": iso(self.clock()),
                "acknowledgedAt": None, "resolvedAt": None,
                "signature": self._signature(sensor["machineId"]),
                "triageLatencySeconds": None,
            })
            raised.append(alert)
        return raised


# ---------------------------------------------------------------------------
# Ontology Query: remaining useful life
# ---------------------------------------------------------------------------

def predict_rul(sim: PlantSimulator, sensor_id: str, window_hours: int = 24) -> dict:
    """
    Log-linear fit on the recent trend, extrapolated to the critical threshold.

    In Foundry this is an Ontology Query backed by a model in Model Studio; the
    agent calls it by apiName and never sees the implementation. Kept explainable
    on purpose — a reliability engineer can audit every number.
    """
    sensor = sim.store.get("Sensor", sensor_id)
    rows = sim.series(sensor_id, hours=window_hours)
    if len(rows) < 8:
        return {"status": "insufficient_data", "sensorId": sensor_id}

    baseline = min(r["value"] for r in sim.series(sensor_id, hours=72)[:8])
    xs, ys = [], []
    t_end = dt.datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
    for r in rows:
        excess = r["value"] - baseline
        if excess <= 0.02:
            continue
        t = dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        xs.append((t - t_end).total_seconds() / 3600.0)
        ys.append(math.log(excess))
    if len(xs) < 6:
        return {"status": "stable", "sensorId": sensor_id,
                "remainingUsefulLifeHours": None, "confidence": 0.0,
                "note": "No sustained trend above baseline — threshold breach looks transient."}

    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1e-9
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - k * mx
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-9
    ss_res = sum((y - (k * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = max(0.0, 1 - ss_res / ss_tot)

    crit_excess = sensor["critThreshold"] - baseline
    if k <= 1e-6 or crit_excess <= 0 or r2 < 0.6:
        return {"status": "stable", "sensorId": sensor_id,
                "remainingUsefulLifeHours": None, "confidence": round(r2, 3),
                "note": f"No statistically significant degradation trend (R²={r2:.2f}); "
                        f"refusing to extrapolate noise."}

    hours = (math.log(crit_excess) - b) / k
    doubling = math.log(2) / k
    return {
        "status": "degrading",
        "sensorId": sensor_id,
        "measure": sensor["measure"],
        "machineId": sensor["machineId"],
        "currentValue": sensor["latestValue"],
        "warnThreshold": sensor["warnThreshold"],
        "critThreshold": sensor["critThreshold"],
        "remainingUsefulLifeHours": round(max(hours, 0.0), 1),
        "doublingTimeHours": round(doubling, 1),
        "growthRatePerHour": round(k, 4),
        "confidence": round(r2, 3),
        "method": "log-linear regression on excess-over-baseline, 24h window",
        "predictedFailureAt": iso(sim.clock() + dt.timedelta(hours=max(hours, 0.0))),
    }
