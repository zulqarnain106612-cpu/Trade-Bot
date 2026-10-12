"""Durable, causal event-to-label path for configured horizon experts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import statistics
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _ensure_column(
    db: Any, table: str, name: str, sql_type: str
) -> None:
    """Add a missing column safely when concurrent startup migrates the same DB."""
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name in columns:
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).casefold():
            raise
        # Another process may have added this column after our initial PRAGMA.
        # Only suppress the error if a fresh schema read confirms that outcome.
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            raise


class HorizonLearningStore:
    """Persist feature snapshots and train only after their horizon matures.

    The first received event at or after `event_ts + horizon_seconds` supplies
    the close used for the continuous log-return label. Event IDs and learner
    sample IDs make replay idempotent, including a restart between checkpoint
    replacement and the SQLite acknowledgement.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db_path = root / "horizon_learning.sqlite3"
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS horizon_samples (
                    sample_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon_id INTEGER NOT NULL,
                    event_ts REAL NOT NULL,
                    due_ts REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    source_timeframe TEXT,
                    event_schema_version INTEGER NOT NULL DEFAULT 1,
                    regime_id INTEGER,
                    outcome REAL,
                    matured_at REAL,
                    trained INTEGER NOT NULL DEFAULT 0,
                    trained_at REAL,
                    error TEXT
                )"""
            )
            for name, sql_type in (
                ("provider", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("source_timeframe", "TEXT"),
                ("event_schema_version", "INTEGER NOT NULL DEFAULT 1"),
                ("regime_id", "INTEGER"),
                ("update_duration_ms", "REAL"),
                ("prediction_return", "REAL"),
                ("prediction_model_version", "TEXT"),
                ("prediction_ts", "REAL"),
                ("oos_correct", "INTEGER"),
                ("matured_at", "REAL"),
                ("candidate_prediction_return", "REAL"),
                ("candidate_model_version", "TEXT"),
                ("candidate_schema_hash", "TEXT"),
                ("candidate_prediction_ts", "REAL"),
                ("candidate_oos_correct", "INTEGER"),
                ("candidate_oos_squared_error", "REAL"),
            ):
                _ensure_column(db, "horizon_samples", name, sql_type)
            db.execute(
                "CREATE INDEX IF NOT EXISTS horizon_samples_due "
                "ON horizon_samples(symbol, due_ts, outcome)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS horizon_samples_training "
                "ON horizon_samples(symbol, trained, matured_at, event_ts)"
            )
            _ensure_column(db, "horizon_samples", "trained_at", "REAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS horizon_events (
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    event_ts REAL NOT NULL,
                    receive_ts REAL NOT NULL,
                    close REAL NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    quality TEXT NOT NULL DEFAULT 'unverified',
                    source_timeframe TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    regime_id INTEGER
                )"""
            )
            for name, sql_type in (
                ("provider", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("quality", "TEXT NOT NULL DEFAULT 'unverified'"),
                ("source_timeframe", "TEXT"),
                ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("regime_id", "INTEGER"),
            ):
                _ensure_column(db, "horizon_events", name, sql_type)
            db.execute(
                "CREATE INDEX IF NOT EXISTS horizon_events_source_order "
                "ON horizon_events(symbol, event_ts DESC)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS horizon_candidate_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    source_timeframe TEXT NOT NULL,
                    horizon_id INTEGER NOT NULL,
                    candidate_model_version TEXT,
                    evaluated_at REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS horizon_promotions (
                    component_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    horizon_id INTEGER NOT NULL,
                    candidate_version TEXT NOT NULL,
                    previous_pointer_json TEXT,
                    state TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path, timeout=5.0)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _features(features: dict[str, Any]) -> tuple[dict[str, float], str]:
        normalized: dict[str, float] = {}
        for name, value in sorted(features.items()):
            number = float(value)
            if not name or not math.isfinite(number):
                raise ValueError(f"invalid feature {name!r}")
            normalized[name] = number
        if not normalized:
            raise ValueError("feature snapshot cannot be empty")
        digest = hashlib.sha256("\0".join(normalized).encode()).hexdigest()
        return normalized, digest

    def observe_event(
        self,
        *,
        event_id: str,
        symbol: str,
        event_ts: float,
        close: float,
        features: dict[str, Any],
        horizons: list[dict[str, Any]],
        model_root: Path,
        provider: str = "unknown",
        quality: str = "unverified",
        event_payload: dict[str, Any] | None = None,
        regime_id: int | None = None,
        defer_training: bool = False,
        source_cadence_seconds: float | None = None,
        source_timeframe: str | None = None,
    ) -> dict[str, int]:
        """Mature old snapshots and persist this event; optionally fit inline."""
        ts = float(event_ts)
        price = float(close)
        if (
            not event_id
            or not symbol
            or not math.isfinite(ts)
            or not math.isfinite(price)
            or price <= 0
        ):
            raise ValueError("event identity, source timestamp, and positive close are required")
        normalized, schema_hash = self._features(features)
        payload = json.dumps(event_payload or {}, sort_keys=True, separators=(",", ":"))
        event_key = hashlib.sha256(f"{symbol}\0{event_id}".encode()).hexdigest()
        matured = updates = rejected = 0
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM horizon_events WHERE event_id=?", (event_key,)).fetchone():
                return {"duplicate": 1, "matured": 0, "updated": 0, "rejected": 0}
            prior = db.execute(
                "SELECT event_ts FROM horizon_events WHERE symbol=? AND source_timeframe IS ? "
                "ORDER BY event_ts DESC LIMIT 1",
                (symbol, source_timeframe),
            ).fetchone()
            if prior and ts < float(prior[0]):
                raise ValueError("out-of-order source event rejected")
            inserted = db.execute(
                """INSERT OR IGNORE INTO horizon_events
                   (event_id, symbol, event_ts, receive_ts, close, provider,
                    quality, source_timeframe, schema_version, payload_json, regime_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    event_key,
                    symbol,
                    ts,
                    time.time(),
                    price,
                    provider,
                    quality,
                    source_timeframe,
                    payload,
                    regime_id,
                ),
            ).rowcount
            if not inserted:
                return {"duplicate": 1, "matured": 0, "updated": 0, "rejected": 0}

            if defer_training:
                pending = db.execute(
                    """SELECT sample_id, horizon_id, entry_price, features_json, schema_hash, outcome
                       FROM horizon_samples
                       WHERE symbol=? AND outcome IS NULL AND due_ts<=? AND source_timeframe IS ?
                       ORDER BY due_ts, event_ts""",
                    (symbol, ts, source_timeframe),
                ).fetchall()
            else:
                pending = db.execute(
                    """SELECT sample_id, horizon_id, entry_price, features_json, schema_hash, outcome
                       FROM horizon_samples
                       WHERE symbol=? AND (
                           (outcome IS NULL AND due_ts<=? AND source_timeframe IS ?) OR
                           (outcome IS NOT NULL AND trained=0 AND source_timeframe IS ?)
                       )
                       ORDER BY due_ts, event_ts""",
                    (symbol, ts, source_timeframe, source_timeframe),
                ).fetchall()
            for sample_id, horizon_id, entry, raw_features, old_schema, stored_outcome in pending:
                outcome = (
                    float(stored_outcome)
                    if stored_outcome is not None
                    else math.log(price / float(entry))
                )
                if stored_outcome is None:
                    db.execute(
                        """UPDATE horizon_samples SET outcome=?,
                           matured_at=?,
                           oos_correct=CASE WHEN prediction_return IS NULL OR
                             prediction_return=0 OR ?=0 THEN NULL
                             WHEN (prediction_return > 0 AND ?>0) OR
                                  (prediction_return < 0 AND ?<0) THEN 1 ELSE 0 END
                           , candidate_oos_correct=CASE
                             WHEN candidate_prediction_return IS NULL OR
                                  candidate_prediction_return=0 OR ?=0 THEN NULL
                             WHEN (candidate_prediction_return > 0 AND ?>0) OR
                                  (candidate_prediction_return < 0 AND ?<0) THEN 1 ELSE 0 END,
                           candidate_oos_squared_error=CASE
                             WHEN candidate_prediction_return IS NULL THEN NULL
                             ELSE (candidate_prediction_return-?)*(candidate_prediction_return-?) END
                           WHERE sample_id=? AND outcome IS NULL""",
                        (
                            outcome,
                            ts,
                            outcome,
                            outcome,
                            outcome,
                            outcome,
                            outcome,
                            outcome,
                            outcome,
                            outcome,
                            sample_id,
                        ),
                    )
                    matured += 1
                if defer_training:
                    continue
                spec = next((h for h in horizons if int(h["id"]) == int(horizon_id)), None)
                if spec is None or "mlp" not in {str(x).lower() for x in spec.get("models", [])}:
                    continue
                update_started = time.perf_counter()
                trained_update = False
                try:
                    from src.models.horizon_mlp import HorizonMLPExpert

                    old_values = json.loads(raw_features)
                    names = tuple(sorted(old_values))
                    instrument_key = hashlib.sha256(symbol.encode()).hexdigest()[:16]
                    expert_path = (
                        model_root
                        / f"h{int(horizon_id) + 1}"
                        / instrument_key
                        / "candidate"
                        / "mlp.joblib"
                    )
                    if HorizonMLPExpert.artifact_available(expert_path):
                        expert = HorizonMLPExpert.load(
                            expert_path,
                            str(old_schema),
                            expected_horizon_id=int(horizon_id),
                            expected_instrument_hash=instrument_key,
                        )
                    else:
                        expert = HorizonMLPExpert(
                            names,
                            horizon_id=int(horizon_id),
                            instrument_hash=instrument_key,
                        )
                    if expert.partial_fit(old_values, outcome, sample_id=str(sample_id)):
                        expert.save(expert_path)
                        updates += 1
                        trained_update = True
                    db.execute(
                        "UPDATE horizon_samples SET trained=1, trained_at=?, "
                        "update_duration_ms=?, error=NULL WHERE sample_id=?",
                        (
                            (time.time() if trained_update else None),
                            (time.perf_counter() - update_started) * 1000.0,
                            sample_id,
                        ),
                    )
                except Exception as exc:
                    rejected += 1
                    db.execute(
                        "UPDATE horizon_samples SET error=?, update_duration_ms=? WHERE sample_id=?",
                        (
                            str(exc)[:500],
                            (time.perf_counter() - update_started) * 1000.0,
                            sample_id,
                        ),
                    )
                    log.warning(
                        "horizon_label_update_failed",
                        horizon_id=horizon_id,
                        sample_id=sample_id,
                        error=str(exc),
                    )

            serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
            for spec in horizons:
                duration = int(spec["seconds"])
                if source_cadence_seconds is not None and duration < source_cadence_seconds:
                    continue
                horizon_id = int(spec["id"])
                sample_id = hashlib.sha256(f"{event_key}\0{horizon_id}".encode()).hexdigest()
                db.execute(
                    """INSERT OR IGNORE INTO horizon_samples
                       (sample_id, event_id, symbol, horizon_id, event_ts, due_ts,
                        entry_price, features_json, schema_hash, provider,
                        event_schema_version, regime_id, source_timeframe)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        sample_id,
                        event_id,
                        symbol,
                        horizon_id,
                        ts,
                        ts + duration,
                        price,
                        serialized,
                        schema_hash,
                        provider,
                        regime_id,
                        source_timeframe,
                    ),
                )
        return {"duplicate": 0, "matured": matured, "updated": updates, "rejected": rejected}

    def train_pending(
        self,
        *,
        symbol: str,
        horizons: list[dict[str, Any]],
        model_root: Path,
        limit: int = 100,
        source_timeframe: str | None = None,
        shadow_minimum_training_samples: int = 100,
    ) -> dict[str, int]:
        """Train a bounded batch of durable matured labels outside ingestion."""
        bounded_limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as db:
            pending = db.execute(
                """SELECT sample_id, horizon_id, features_json, schema_hash, outcome
                   FROM horizon_samples WHERE symbol=? AND outcome IS NOT NULL AND trained=0
                     AND source_timeframe IS ?
                   ORDER BY matured_at, event_ts LIMIT ?""",
                (symbol, source_timeframe, bounded_limit),
            ).fetchall()

        updates = rejected = 0
        specs_by_id = {int(spec["id"]): spec for spec in horizons}
        grouped: dict[int, list[tuple[Any, ...]]] = {}
        for row in pending:
            grouped.setdefault(int(row[1]), []).append(row)
        for horizon_id, samples in grouped.items():
            spec = specs_by_id.get(horizon_id)
            if spec is None or "mlp" not in {str(x).lower() for x in spec.get("models", [])}:
                rejected += len(samples)
                continue
            update_started = time.perf_counter()
            try:
                from src.models.horizon_mlp import HorizonMLPExpert

                values = json.loads(samples[0][2])
                names = tuple(sorted(values))
                instrument_key = hashlib.sha256(symbol.encode()).hexdigest()[:16]
                expert_path = (
                    model_root / f"h{horizon_id + 1}" / instrument_key / "candidate" / "mlp.joblib"
                )
                if HorizonMLPExpert.artifact_available(expert_path):
                    expert = HorizonMLPExpert.load(
                        expert_path,
                        str(samples[0][3]),
                        expected_horizon_id=horizon_id,
                        expected_instrument_hash=instrument_key,
                    )
                else:
                    expert = HorizonMLPExpert(
                        names,
                        horizon_id=horizon_id,
                        instrument_hash=instrument_key,
                    )
                sample_updates = 0
                for sample_id, _, raw_features, _, outcome in samples:
                    sample_updates += int(
                        expert.partial_fit(
                            json.loads(raw_features), float(outcome), sample_id=str(sample_id)
                        )
                    )
                if sample_updates:
                    expert.save(expert_path)
                    updates += sample_updates
                shadow_path = expert_path.parent.parent / "shadow" / expert_path.name
                previous_evaluation = self.latest_candidate_evaluation(
                    symbol=symbol,
                    horizon_id=horizon_id,
                    source_timeframe=source_timeframe or "",
                )
                refresh_shadow = not HorizonMLPExpert.artifact_available(shadow_path) or (
                    previous_evaluation is not None
                    and previous_evaluation.get("outcome") == "REJECTED"
                )
                if refresh_shadow and expert.n_samples >= int(shadow_minimum_training_samples):
                    expert.save(shadow_path)
                update_duration = (time.perf_counter() - update_started) * 1000.0
                trained_at = time.time() if sample_updates else None
                with self._lock, self._connect() as db:
                    db.executemany(
                        "UPDATE horizon_samples SET trained=1, trained_at=?, "
                        "update_duration_ms=?, error=NULL WHERE sample_id=? AND outcome IS NOT NULL",
                        [(trained_at, update_duration, row[0]) for row in samples],
                    )
            except Exception as exc:
                rejected += len(samples)
                update_duration = (time.perf_counter() - update_started) * 1000.0
                with self._lock, self._connect() as db:
                    db.executemany(
                        "UPDATE horizon_samples SET error=?, update_duration_ms=? WHERE sample_id=?",
                        [(str(exc)[:500], update_duration, row[0]) for row in samples],
                    )
                log.warning(
                    "horizon_label_update_failed",
                    horizon_id=horizon_id,
                    sample_count=len(samples),
                    error=str(exc),
                )
        return {"updated": updates, "rejected": rejected, "attempted": len(pending)}

    def record_predictions(
        self,
        *,
        event_id: str,
        symbol: str,
        predictions: list[Any],
    ) -> int:
        """Attach real model forecasts to their exact pending event samples."""
        recorded = 0
        with self._lock, self._connect() as db:
            for prediction in predictions:
                if (
                    prediction.status
                    not in {"TRAINED", "PROBATION", "PROMOTED", "CANDIDATE_SHADOW"}
                    or prediction.error
                ):
                    continue
                value = float(prediction.magnitude_mu)
                ts = float(prediction.prediction_ts or 0.0)
                if not math.isfinite(value) or not math.isfinite(ts) or ts <= 0:
                    continue
                event_key = hashlib.sha256(f"{symbol}\0{event_id}".encode()).hexdigest()
                sample_id = hashlib.sha256(
                    f"{event_key}\0{int(prediction.horizon_id)}".encode()
                ).hexdigest()
                recorded += db.execute(
                    """UPDATE horizon_samples SET prediction_return=?,
                       prediction_model_version=?, prediction_ts=?
                       WHERE sample_id=? AND symbol=? AND outcome IS NULL
                         AND prediction_return IS NULL""",
                    (
                        value,
                        prediction.model_version,
                        ts,
                        sample_id,
                        symbol,
                    ),
                ).rowcount
                candidate_value = getattr(prediction, "candidate_magnitude_mu", None)
                candidate_version = getattr(prediction, "candidate_model_version", None)
                candidate_schema = getattr(prediction, "candidate_schema_hash", None)
                if (
                    candidate_value is not None
                    and candidate_version
                    and candidate_schema == getattr(prediction, "feature_schema_hash", None)
                    and math.isfinite(float(candidate_value))
                ):
                    db.execute(
                        """UPDATE horizon_samples SET candidate_prediction_return=?,
                           candidate_model_version=?, candidate_schema_hash=?, candidate_prediction_ts=?
                           WHERE sample_id=? AND symbol=? AND outcome IS NULL
                             AND candidate_prediction_return IS NULL AND schema_hash=?""",
                        (
                            float(candidate_value),
                            candidate_version,
                            candidate_schema,
                            ts,
                            sample_id,
                            symbol,
                            candidate_schema,
                        ),
                    )
        return recorded

    def evaluate_candidate(
        self,
        *,
        symbol: str,
        horizon_id: int,
        source_timeframe: str,
        candidate_version: str | None = None,
        incumbent_version: str | None = None,
        minimum_samples: int = 100,
        max_evidence_age_seconds: float = 30 * 86400,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Compare paired incumbent/challenger forecasts against the same labels."""
        instant = time.time() if now is None else float(now)
        if (
            minimum_samples < 1
            or not math.isfinite(max_evidence_age_seconds)
            or max_evidence_age_seconds <= 0
        ):
            raise ValueError("candidate evaluation limits must be positive and finite")
        with self._connect() as db:
            if candidate_version is None or incumbent_version is None:
                latest_version = db.execute(
                    """SELECT candidate_model_version, prediction_model_version
                       FROM horizon_samples
                       WHERE symbol=? AND horizon_id=? AND source_timeframe=?
                         AND candidate_prediction_return IS NOT NULL
                         AND prediction_return IS NOT NULL
                         AND (? IS NULL OR candidate_model_version=?)
                       ORDER BY candidate_prediction_ts DESC LIMIT 1""",
                    (
                        symbol,
                        horizon_id,
                        source_timeframe,
                        candidate_version,
                        candidate_version,
                    ),
                ).fetchone()
                if candidate_version is None:
                    candidate_version = None if latest_version is None else str(latest_version[0])
                if incumbent_version is None and latest_version is not None:
                    incumbent_version = str(latest_version[1])
            row = db.execute(
                """SELECT COUNT(*), AVG(oos_correct), AVG(candidate_oos_correct),
                          AVG((prediction_return-outcome)*(prediction_return-outcome)),
                          AVG(candidate_oos_squared_error), MAX(matured_at),
                          MIN(schema_hash), MAX(schema_hash)
                   FROM horizon_samples WHERE symbol=? AND horizon_id=?
                     AND source_timeframe=? AND outcome IS NOT NULL
                     AND prediction_return IS NOT NULL
                     AND candidate_prediction_return IS NOT NULL
                     AND candidate_oos_correct IS NOT NULL
                     AND (?='zero-baseline-v1' OR oos_correct IS NOT NULL)
                     AND candidate_schema_hash IS NOT NULL
                     AND schema_hash=candidate_schema_hash
                     AND candidate_model_version=? AND prediction_model_version=?
                     AND prediction_ts<=? AND candidate_prediction_ts<=?
                     AND ABS(candidate_prediction_ts-prediction_ts)<=60""",
                (
                    symbol,
                    horizon_id,
                    source_timeframe,
                    incumbent_version,
                    candidate_version,
                    incumbent_version,
                    instant,
                    instant,
                ),
            ).fetchone()
        (
            count,
            incumbent_accuracy,
            candidate_accuracy,
            incumbent_mse,
            candidate_mse,
            latest,
            schema_min,
            schema_max,
        ) = row
        reason = None
        if int(count or 0) < minimum_samples:
            reason = "insufficient paired forward samples"
        elif schema_min != schema_max:
            reason = "inconsistent feature schemas in paired evidence"
        elif candidate_version == incumbent_version:
            reason = "candidate and incumbent artifacts are identical"
        elif (
            latest is None
            or float(latest) > instant
            or instant - float(latest) > max_evidence_age_seconds
        ):
            reason = "paired evaluation evidence is stale"
        elif candidate_accuracy is None or candidate_mse is None or incumbent_mse is None:
            reason = "paired metrics are incomplete"
        elif (
            float(candidate_accuracy) <= 0.5
            if incumbent_version == "zero-baseline-v1" and candidate_accuracy is not None
            else incumbent_accuracy is None or float(candidate_accuracy) < float(incumbent_accuracy)
        ) or float(candidate_mse) >= float(incumbent_mse):
            reason = "candidate did not meet both incumbent skill metrics"
        outcome = (
            "PASS"
            if reason is None
            else "CONTINUE_SHADOW"
            if reason
            in {"insufficient paired forward samples", "paired evaluation evidence is stale"}
            else "REJECTED"
        )
        evaluation = {
            "eligible": reason is None,
            "outcome": outcome,
            "reason": reason,
            "paired_samples": int(count or 0),
            "minimum_samples": int(minimum_samples),
            "incumbent_directional_accuracy": incumbent_accuracy,
            "candidate_directional_accuracy": candidate_accuracy,
            "incumbent_rmse": None if incumbent_mse is None else math.sqrt(float(incumbent_mse)),
            "candidate_rmse": None if candidate_mse is None else math.sqrt(float(candidate_mse)),
            "last_matured_at": latest,
            "feature_schema_hash": schema_min if schema_min == schema_max else None,
            "candidate_model_version": candidate_version,
            "incumbent_model_version": incumbent_version,
        }
        metrics_json = json.dumps(evaluation, sort_keys=True, separators=(",", ":"))
        evaluation_id = hashlib.sha256(
            f"{symbol}\0{source_timeframe}\0{horizon_id}\0{candidate_version}\0{metrics_json}".encode()
        ).hexdigest()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO horizon_candidate_evaluations
                   (evaluation_id, symbol, source_timeframe, horizon_id,
                    candidate_model_version, evaluated_at, outcome, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evaluation_id,
                    symbol,
                    source_timeframe,
                    horizon_id,
                    candidate_version,
                    instant,
                    str(evaluation["outcome"]),
                    metrics_json,
                ),
            )
        evaluation["evaluation_id"] = evaluation_id
        evaluation["evaluated_at"] = instant
        return evaluation

    def latest_candidate_evaluation(
        self, *, symbol: str, horizon_id: int, source_timeframe: str
    ) -> dict[str, Any] | None:
        """Return the latest persisted evaluation with its durable identity."""
        with self._connect() as db:
            row = db.execute(
                """SELECT evaluation_id, evaluated_at, metrics_json
                   FROM horizon_candidate_evaluations
                   WHERE symbol=? AND horizon_id=? AND source_timeframe=?
                   ORDER BY evaluated_at DESC, rowid DESC LIMIT 1""",
                (symbol, horizon_id, source_timeframe),
            ).fetchone()
        if row is None:
            return None
        evaluation = json.loads(str(row[2]))
        evaluation["evaluation_id"] = str(row[0])
        evaluation["evaluated_at"] = float(row[1])
        return evaluation

    def activate_candidate(
        self,
        *,
        component_id: str,
        symbol: str,
        horizon_id: int,
        candidate_version: str,
        model_root: Path,
        source_timeframe: str = "1m",
        minimum_samples: int = 100,
    ) -> None:
        """Atomically point the active model at an evaluated candidate in probation."""
        from src.models.horizon_mlp import HorizonMLPExpert
        from src.models.provenance import manifest_path_for

        instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
        active_path = model_root / f"h{horizon_id + 1}" / instrument / "mlp.joblib"
        candidate_path = active_path.parent / "shadow" / active_path.name
        evaluation = self.evaluate_candidate(
            symbol=symbol,
            horizon_id=horizon_id,
            source_timeframe=source_timeframe,
            candidate_version=candidate_version,
            minimum_samples=minimum_samples,
        )
        if not evaluation["eligible"] or evaluation["candidate_model_version"] != candidate_version:
            raise ValueError("candidate has no current, passing paired evaluation")
        candidate = HorizonMLPExpert.load(
            candidate_path,
            expected_horizon_id=horizon_id,
            expected_instrument_hash=instrument,
        )
        if candidate.artifact_sha256 != candidate_version:
            raise ValueError("candidate artifact changed after its evaluation")
        source = HorizonMLPExpert.active_artifact_path(candidate_path)
        source_manifest = manifest_path_for(source)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT previous_pointer_json, state FROM horizon_promotions WHERE component_id=?",
                (component_id,),
            ).fetchone()
            if prior and prior[1] in {"PREPARED", "PROBATION", "PROMOTED", "ROLLING_BACK"}:
                raise ValueError("a promotion for this horizon candidate is already active")
            open_promotion = db.execute(
                "SELECT component_id FROM horizon_promotions WHERE symbol=? AND horizon_id=? "
                "AND state IN ('PREPARED','PROBATION','ROLLING_BACK') LIMIT 1",
                (symbol, horizon_id),
            ).fetchone()
            if open_promotion is not None:
                raise ValueError("another promotion is active for this horizon and instrument")
            previous = HorizonMLPExpert.activation_pointer(active_path)
            previous_json = None if previous is None else json.dumps(previous, sort_keys=True)
            db.execute(
                """INSERT INTO horizon_promotions
                   (component_id, symbol, horizon_id, candidate_version,
                    previous_pointer_json, state, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'PREPARED', ?)
                   ON CONFLICT(component_id) DO UPDATE SET
                     symbol=excluded.symbol, horizon_id=excluded.horizon_id,
                     candidate_version=excluded.candidate_version,
                     previous_pointer_json=excluded.previous_pointer_json,
                     state='PREPARED', updated_at=excluded.updated_at""",
                (component_id, symbol, horizon_id, candidate_version, previous_json, time.time()),
            )
            db.commit()

        versions = active_path.parent / f".{active_path.name}.versions"
        version_dir = versions / candidate_version
        if not version_dir.exists():
            versions.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(tempfile.mkdtemp(prefix=".horizon-activate-", dir=versions))
            try:
                target = temp_dir / active_path.name
                target.write_bytes(source.read_bytes())
                manifest_path_for(target).write_bytes(source_manifest.read_bytes())
                with suppress(FileExistsError):
                    os.replace(temp_dir, version_dir)
            finally:
                if temp_dir.exists():
                    import shutil

                    shutil.rmtree(temp_dir)
        pointer = {
            "artifact": str((version_dir / active_path.name).relative_to(active_path.parent)),
            "sha256": candidate_version,
            "format_version": 1,
            "lifecycle_status": "PROBATION",
            "component_id": component_id,
        }
        HorizonMLPExpert.write_activation_pointer(active_path, pointer)
        with self._connect() as db:
            db.execute(
                "UPDATE horizon_promotions SET state='PROBATION', updated_at=? "
                "WHERE component_id=? AND candidate_version=? AND state='PREPARED'",
                (time.time(), component_id, candidate_version),
            )

    def complete_promotion(self, *, component_id: str, model_root: Path) -> None:
        from src.models.horizon_mlp import HorizonMLPExpert

        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT symbol, horizon_id, candidate_version, state FROM horizon_promotions "
                "WHERE component_id=?",
                (component_id,),
            ).fetchone()
            if row is None or row[3] not in {"PROBATION", "PROMOTED"}:
                raise ValueError("horizon candidate is not in promotion probation")
            symbol, horizon_id, version, _ = row
            instrument = hashlib.sha256(str(symbol).encode()).hexdigest()[:16]
            active_path = model_root / f"h{int(horizon_id) + 1}" / instrument / "mlp.joblib"
            pointer = HorizonMLPExpert.activation_pointer(active_path)
            if pointer is None or pointer.get("sha256") != version:
                raise ValueError("active horizon pointer changed before promotion completion")
            pointer["lifecycle_status"] = "PROMOTED"
            db.execute(
                "UPDATE horizon_promotions SET state='PROMOTED', updated_at=? WHERE component_id=?",
                (time.time(), component_id),
            )
        HorizonMLPExpert.write_activation_pointer(active_path, pointer)

    def rollback_promotion(self, *, component_id: str, model_root: Path) -> None:
        from src.models.horizon_mlp import HorizonMLPExpert

        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT symbol, horizon_id, previous_pointer_json, state FROM horizon_promotions "
                "WHERE component_id=?",
                (component_id,),
            ).fetchone()
            if row is None or row[3] not in {"PREPARED", "PROBATION", "PROMOTED", "ROLLING_BACK"}:
                raise ValueError("no reversible horizon promotion is recorded")
            symbol, horizon_id, previous_json, _ = row
            db.execute(
                "UPDATE horizon_promotions SET state='ROLLING_BACK', updated_at=? WHERE component_id=?",
                (time.time(), component_id),
            )
        instrument = hashlib.sha256(str(symbol).encode()).hexdigest()[:16]
        active_path = model_root / f"h{int(horizon_id) + 1}" / instrument / "mlp.joblib"
        pointer_path = active_path.with_suffix(active_path.suffix + ".active.json")
        if previous_json is None:
            pointer_path.unlink(missing_ok=True)
        else:
            HorizonMLPExpert.write_activation_pointer(active_path, json.loads(previous_json))
        with self._connect() as db:
            db.execute(
                "UPDATE horizon_promotions SET state='ROLLED_BACK', updated_at=? WHERE component_id=?",
                (time.time(), component_id),
            )

    def recover_promotions(self, model_root: Path) -> list[str]:
        """Repair interrupted pointer transitions before inference starts."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT component_id, symbol, horizon_id, candidate_version, state FROM horizon_promotions "
                "WHERE state IN ('PREPARED','PROBATION','ROLLING_BACK','PROMOTED')"
            ).fetchall()
        recovered = []
        from src.models.horizon_mlp import HorizonMLPExpert

        for component_id, symbol, horizon_id, candidate_version, state in rows:
            if state in {"PREPARED", "PROBATION", "ROLLING_BACK"}:
                self.rollback_promotion(component_id=component_id, model_root=model_root)
                recovered.append(str(component_id))
            else:
                instrument = hashlib.sha256(str(symbol).encode()).hexdigest()[:16]
                active_path = model_root / f"h{int(horizon_id) + 1}" / instrument / "mlp.joblib"
                pointer = HorizonMLPExpert.activation_pointer(active_path)
                if pointer is not None and pointer.get("sha256") == candidate_version:
                    self.complete_promotion(component_id=str(component_id), model_root=model_root)
                    recovered.append(str(component_id))
        return recovered

    def stats(
        self, symbol: str | None = None, source_timeframe: str | None = None
    ) -> dict[int, dict[str, Any]]:
        """Return per-horizon pending/matured/learned counts and last update."""
        with self._connect() as db:
            query = """SELECT horizon_id, COUNT(*),
                          SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END),
                          SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END),
                          SUM(trained), MAX(event_ts), MAX(matured_at),
                          MAX(trained_at), MAX(error), MAX(update_duration_ms),
                          SUM(CASE WHEN oos_correct IS NOT NULL THEN 1 ELSE 0 END),
                          AVG(oos_correct),
                          SUM(CASE WHEN outcome IS NOT NULL AND trained=0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN candidate_oos_correct IS NOT NULL THEN 1 ELSE 0 END),
                          AVG(candidate_oos_correct)
                   FROM horizon_samples"""
            query += " WHERE source_timeframe IS ?"
            params: tuple[Any, ...] = (source_timeframe,)
            if symbol is not None:
                query += " AND symbol=?"
                params += (symbol,)
            rows = db.execute(query + " GROUP BY horizon_id", params).fetchall()
            regime_query = """SELECT horizon_id, regime_id, COUNT(*), AVG(oos_correct)
                              FROM horizon_samples
                              WHERE regime_id IS NOT NULL AND oos_correct IS NOT NULL
                                AND source_timeframe IS ?"""
            regime_params: tuple[Any, ...] = (source_timeframe,)
            if symbol is not None:
                regime_query += " AND symbol=?"
                regime_params += (symbol,)
            regime_query += " GROUP BY horizon_id, regime_id"
            regime_rows = db.execute(regime_query, regime_params).fetchall()
            latest_rows = []
            for row in rows:
                horizon_id = row[0]
                latest_query = """SELECT matured_at, outcome FROM horizon_samples
                                  WHERE horizon_id=? AND matured_at IS NOT NULL
                                    AND source_timeframe IS ?"""
                latest_params: tuple[Any, ...] = (horizon_id, source_timeframe)
                if symbol is not None:
                    latest_query += " AND symbol=?"
                    latest_params += (symbol,)
                latest_query += " ORDER BY matured_at DESC, event_ts DESC LIMIT 1"
                latest = db.execute(latest_query, latest_params).fetchone()
                if latest is not None:
                    latest_rows.append((horizon_id, latest[0], latest[1]))
        last_labels: dict[int, tuple[float, float]] = {}
        for horizon_id, matured_at, outcome in latest_rows:
            last_labels.setdefault(int(horizon_id), (float(matured_at), float(outcome)))
        oos_by_regime: dict[int, dict[int, float]] = {}
        for horizon_id, regime_id, count, accuracy in regime_rows:
            oos_by_regime.setdefault(int(horizon_id), {})[int(regime_id)] = {
                "samples": int(count),
                "directional_accuracy": float(accuracy),
            }
        return {
            int(horizon_id): {
                "training_samples": int(trained or 0),
                "training_pending_samples": int(training_pending or 0),
                "matured_samples": int(matured or 0),
                "pending_samples": int(pending or 0),
                "last_event_ts": last_event,
                "last_matured_event_ts": last_matured,
                "last_matured_label_at": last_labels.get(int(horizon_id), (None, None))[0],
                "last_matured_label": last_labels.get(int(horizon_id), (None, None))[1],
                "last_update": last_update,
                "last_error": last_error,
                "last_update_duration_ms": update_duration_ms,
                "oos_samples": int(oos_count or 0),
                "oos_directional_accuracy": float(oos_accuracy)
                if oos_accuracy is not None
                else None,
                "candidate_oos_samples": int(candidate_oos_count or 0),
                "candidate_oos_directional_accuracy": (
                    float(candidate_oos_accuracy) if candidate_oos_accuracy is not None else None
                ),
                "oos_by_regime": oos_by_regime.get(int(horizon_id), {}),
            }
            for (
                horizon_id,
                _,
                pending,
                matured,
                trained,
                last_event,
                last_matured,
                last_update,
                last_error,
                update_duration_ms,
                oos_count,
                oos_accuracy,
                training_pending,
                candidate_oos_count,
                candidate_oos_accuracy,
            ) in rows
        }

    def source_cadence_seconds(
        self, symbol: str, source_timeframe: str | None = None, window: int = 64
    ) -> float | None:
        """Median observed interval between distinct source events for a symbol."""
        limit = max(3, min(int(window), 256))
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_ts FROM horizon_events WHERE symbol=? "
                "AND source_timeframe IS ? "
                "ORDER BY event_ts DESC LIMIT ?",
                (symbol, source_timeframe, limit),
            ).fetchall()
        timestamps = [float(row[0]) for row in rows]
        gaps = [
            newer - older
            for newer, older in zip(timestamps, timestamps[1:], strict=False)
            if math.isfinite(newer - older) and newer > older
        ]
        return statistics.median(gaps) if len(gaps) >= 5 else None
