"""Versioned, auditable storage for live signal outcomes.

The ledger deliberately owns no market-data or model clients.  Callers record a
point-in-time signal snapshot, then pass dated daily OHLCV bars to
``update_outcome``.  This keeps the outcome engine deterministic and makes it
straightforward to integrate with the existing bot without giving the ledger
access to API credentials.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "vn-equity.signal-ledger.v1"
LEDGER_VERSION = 1
OPEN_STATUS = "open"
RESOLVED_STATUSES = frozenset({"win", "loss", "timeout"})

_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|password|authorization|cookie|"
    r"credential|private_?key|client_?secret|bearer)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE_RES = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{25,}"),
)


class SignalLedgerError(RuntimeError):
    """Raised when persisted ledger data violates the public contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        magnitude = abs(float(value))
        seconds = float(value) / 1000.0 if magnitude > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError(f"{field} is not a valid epoch timestamp") from exc
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 date or datetime") from exc


def _datetime_text(value: Any, field: str) -> str:
    parsed = _parse_datetime(value, field)
    # Keep the source offset: the calendar date is the exchange/session date.
    # Converting a post-midnight Asia time to UTC can move it to the previous
    # day and make a same-session bar look like the required next session.
    return parsed.isoformat(timespec="seconds")


def _secret_free(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            folded = re.sub(r"[^a-z0-9]+", "_", key_text.casefold()).strip("_")
            if _SECRET_KEY_RE.search(folded):
                raise ValueError(f"secret-like field is not allowed at {path}.{key_text}")
            _secret_free(item, f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            _secret_free(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE_RES):
            raise ValueError(f"secret-like value is not allowed at {path}")


def _canonical(value: Any) -> Any:
    """Convert a safe snapshot into stable JSON-compatible data."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, datetime):
        return _datetime_text(value, "datetime")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid signal features")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported snapshot value: {type(value).__name__}")


def features_hash(features: Any) -> str:
    """Return the stable content hash used to version a feature snapshot."""

    _secret_free(features, "features")
    payload = json.dumps(
        _canonical(features),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _positive_number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a positive number")
    return result


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    number = _optional_number(value, field)
    if number is None or number < 1 or not number.is_integer():
        raise ValueError(f"{field} must be a positive integer")
    return int(number)


def _normalize_entry_plan(entry_plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(entry_plan, Mapping):
        raise TypeError("entry_plan must be a mapping")
    _secret_free(entry_plan, "entry_plan")
    plan = _canonical(entry_plan)
    method = str(plan.get("method") or "next_open").strip().lower()
    if method != "next_open":
        raise ValueError("entry_plan.method must be 'next_open'")
    entry_low = (
        _positive_number(plan["entry_low"], "entry_plan.entry_low")
        if plan.get("entry_low") is not None
        else None
    )
    entry_high = (
        _positive_number(plan["entry_high"], "entry_plan.entry_high")
        if plan.get("entry_high") is not None
        else None
    )
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        raise ValueError("entry_plan.entry_low must not exceed entry_plan.entry_high")
    reference = plan.get("reference_price", plan.get("planned_price", plan.get("price")))
    if reference is None and entry_low is not None and entry_high is not None:
        reference = (entry_low + entry_high) / 2.0
    reference_price = _positive_number(reference, "entry_plan.reference_price")
    if entry_low is not None and reference_price < entry_low:
        raise ValueError("entry_plan.reference_price must be inside the entry range")
    if entry_high is not None and reference_price > entry_high:
        raise ValueError("entry_plan.reference_price must be inside the entry range")
    max_entry_sessions = _positive_integer(
        plan.get("max_entry_sessions", 1),
        "entry_plan.max_entry_sessions",
    )
    normalized = dict(plan)
    normalized["method"] = "next_open"
    normalized["reference_price"] = reference_price
    normalized["max_entry_sessions"] = max_entry_sessions
    if entry_low is not None:
        normalized["entry_low"] = entry_low
    if entry_high is not None:
        normalized["entry_high"] = entry_high
    normalized.pop("planned_price", None)
    normalized.pop("price", None)
    return normalized


def _normalize_targets(targets: Sequence[Any]) -> list[dict[str, Any]]:
    if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence) or not targets:
        raise ValueError("targets must contain at least one target")
    normalized: list[dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        if isinstance(target, Mapping):
            _secret_free(target, f"targets[{index - 1}]")
            item = _canonical(target)
            price = _positive_number(item.get("price"), f"targets[{index - 1}].price")
            label = str(item.get("label") or f"T{index}").strip() or f"T{index}"
        else:
            price = _positive_number(target, f"targets[{index - 1}]")
            label = f"T{index}"
            item = {}
        normalized_item = dict(item)
        normalized_item.update({"label": label, "price": price})
        normalized.append(normalized_item)
    return normalized


def _normalize_reviews(model_reviews: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if model_reviews is None:
        return []
    if isinstance(model_reviews, Mapping):
        reviews: Iterable[Any] = [model_reviews]
    else:
        reviews = model_reviews
    normalized: list[dict[str, Any]] = []
    for index, review in enumerate(reviews):
        if not isinstance(review, Mapping):
            raise TypeError("each model review must be a mapping")
        _secret_free(review, f"model_reviews[{index}]")
        item = _canonical(review)
        if not any(str(item.get(key) or "").strip() for key in ("provider", "model", "summary", "verdict")):
            raise ValueError("a model review needs provider/model/summary/verdict")
        normalized.append(item)
    return normalized


def _signal_id(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sig_" + hashlib.sha256(raw).hexdigest()[:24]


def _bar_value(bar: Any, names: Sequence[str]) -> Any:
    if isinstance(bar, Mapping):
        lowered = {str(key).casefold(): value for key, value in bar.items()}
        for name in names:
            if name.casefold() in lowered:
                return lowered[name.casefold()]
        return None
    for name in names:
        if hasattr(bar, name):
            return getattr(bar, name)
    # Named tuples and data-provider row objects often expose ``Date``/``Open``
    # rather than lowercase fields.  Resolve those attributes case-insensitively
    # without requiring a provider-specific adapter.
    available = {attribute.casefold(): attribute for attribute in dir(bar)}
    for name in names:
        attribute = available.get(name.casefold())
        if attribute is not None:
            return getattr(bar, attribute)
    return None


def _bar_number(bar: Any, names: Sequence[str], field: str, required: bool = False) -> float | None:
    raw = _bar_value(bar, names)
    if raw is None:
        if required:
            raise ValueError(f"bar {field} is required")
        return None
    result = _optional_number(raw, f"bar {field}")
    if result is None or result <= 0:
        if required:
            raise ValueError(f"bar {field} must be positive")
        return None
    return result


def _normalize_bars(bars: Iterable[Any]) -> list[dict[str, Any]]:
    parsed: list[tuple[tuple[int, ...], int, dict[str, float | str | None]]] = []
    for index, bar in enumerate(bars):
        raw_date = _bar_value(bar, ("date", "datetime", "time", "timestamp", "trading_date", "as_of"))
        timestamp = _parse_datetime(raw_date, f"bars[{index}].date")
        local_order = (
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
            timestamp.microsecond,
        )
        parsed.append(
            (
                local_order,
                index,
                {
                    "date": timestamp.date().isoformat(),
                    "open": _bar_number(bar, ("open", "open_price", "o"), "open"),
                    "high": _bar_number(bar, ("high", "h"), "high"),
                    "low": _bar_number(bar, ("low", "l"), "low"),
                    "close": _bar_number(bar, ("close", "c"), "close"),
                },
            )
        )
    parsed.sort(key=lambda item: (item[0], item[1]))

    daily: dict[str, dict[str, Any]] = {}
    for _, _, row in parsed:
        day = str(row["date"])
        aggregate = daily.get(day)
        if aggregate is None:
            daily[day] = dict(row)
            continue
        if aggregate["open"] is None and row["open"] is not None:
            aggregate["open"] = row["open"]
        highs = [value for value in (aggregate["high"], row["high"]) if value is not None]
        lows = [value for value in (aggregate["low"], row["low"]) if value is not None]
        aggregate["high"] = max(highs) if highs else None
        aggregate["low"] = min(lows) if lows else None
        if row["close"] is not None:
            aggregate["close"] = row["close"]

    output: list[dict[str, Any]] = []
    for row in daily.values():
        open_price = row["open"]
        close = row["close"]
        if open_price is None:
            raise ValueError(f"bar {row['date']} needs an open for next-open evaluation")
        if close is None:
            close = open_price
            row["close"] = close
        if row["high"] is None:
            row["high"] = max(open_price, close)
        if row["low"] is None:
            row["low"] = min(open_price, close)
        if row["high"] < max(open_price, close, row["low"]):
            raise ValueError(f"bar {row['date']} has an invalid high")
        if row["low"] > min(open_price, close, row["high"]):
            raise ValueError(f"bar {row['date']} has an invalid low")
        output.append(row)
    return output


def _empty_outcome(
    cost_bps: float,
    timeout_sessions: int,
    entry_timeout_sessions: int,
) -> dict[str, Any]:
    return {
        "status": OPEN_STATUS,
        "entry_at": None,
        "entry_price": None,
        "exit_at": None,
        "exit_price": None,
        "exit_reason": None,
        "target_label": None,
        "entry_sessions_observed": 0,
        "sessions_observed": 0,
        "last_bar_at": None,
        "planned_risk_per_share": None,
        "gross_return_pct": None,
        "net_return_pct": None,
        "gross_r_multiple": None,
        "r_multiple": None,
        "round_trip_cost_bps": cost_bps,
        "entry_timeout_sessions": entry_timeout_sessions,
        "timeout_sessions": timeout_sessions,
    }


def _resolved_outcome(
    outcome: dict[str, Any],
    *,
    status: str,
    exit_at: str,
    exit_price: float,
    exit_reason: str,
) -> dict[str, Any]:
    entry = float(outcome["entry_price"])
    planned_risk = float(outcome["planned_risk_per_share"])
    cost_bps = float(outcome["round_trip_cost_bps"])
    gross_pnl = exit_price - entry
    cost_per_share = entry * cost_bps / 10_000.0
    net_pnl = gross_pnl - cost_per_share
    outcome.update(
        {
            "status": status,
            "exit_at": exit_at,
            "exit_price": round(exit_price, 8),
            "exit_reason": exit_reason,
            "gross_return_pct": round(gross_pnl / entry * 100.0, 6),
            "net_return_pct": round(net_pnl / entry * 100.0, 6),
            "gross_r_multiple": round(gross_pnl / planned_risk, 6),
            "r_multiple": round(net_pnl / planned_risk, 6),
        }
    )
    return outcome


def evaluate_outcome(
    signal: Mapping[str, Any],
    bars: Iterable[Any],
    *,
    timeout_sessions: int = 20,
    round_trip_cost_bps: float = 30.0,
) -> dict[str, Any]:
    """Evaluate a long signal using deterministic next-open semantics.

    Starting with the first daily bar strictly after the signal date, an order
    may fill only when its open is inside the optional entry range, above stop,
    and below T1.  The order expires after ``entry_plan.max_entry_sessions``
    (default one) and resolves as ``timeout/entry_not_filled`` without invented
    P&L.  On every holding bar, stop is evaluated before target.  A gap below
    stop exits at the worse opening price; a gap above T1 is capped at T1.  If
    no barrier resolves within ``timeout_sessions`` after entry, the Nth close
    resolves as a timeout.  ``r_multiple`` is net of round-trip costs and uses
    the original planned-entry-to-stop distance as one R.
    """

    if timeout_sessions < 1:
        raise ValueError("timeout_sessions must be at least 1")
    cost_bps = _optional_number(round_trip_cost_bps, "round_trip_cost_bps")
    if cost_bps is None or cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")

    normalized_bars = _normalize_bars(bars)
    signal_day = _parse_datetime(signal.get("signal_at"), "signal_at").date()
    eligible = [row for row in normalized_bars if date.fromisoformat(row["date"]) > signal_day]

    entry_plan = _normalize_entry_plan(signal.get("entry_plan") or {})
    reference_price = float(entry_plan["reference_price"])
    stop = _positive_number(signal.get("stop"), "stop")
    if stop >= reference_price:
        raise ValueError("stop must be below the planned reference price for a long signal")
    targets = signal.get("targets") or []
    if not targets:
        raise ValueError("signal needs at least one target")
    first_target = targets[0]
    target_price = _positive_number(
        first_target.get("price") if isinstance(first_target, Mapping) else first_target,
        "targets[0].price",
    )
    target_label = (
        str(first_target.get("label") or "T1")
        if isinstance(first_target, Mapping)
        else "T1"
    )
    entry_low = entry_plan.get("entry_low")
    entry_high = entry_plan.get("entry_high")
    if entry_low is not None and stop >= float(entry_low):
        raise ValueError("stop must be below entry_plan.entry_low")
    if entry_high is not None and target_price <= float(entry_high):
        raise ValueError("T1 must be above entry_plan.entry_high")
    entry_timeout_sessions = int(entry_plan["max_entry_sessions"])
    outcome = _empty_outcome(
        float(cost_bps),
        int(timeout_sessions),
        entry_timeout_sessions,
    )
    outcome["target_label"] = target_label
    if not eligible:
        return outcome

    entry_index: int | None = None
    entry_attempts = eligible[:entry_timeout_sessions]
    for index, bar in enumerate(entry_attempts):
        open_price = float(bar["open"])
        outcome["entry_sessions_observed"] = index + 1
        outcome["last_bar_at"] = bar["date"]
        in_explicit_range = (
            (entry_low is None or open_price >= float(entry_low))
            and (entry_high is None or open_price <= float(entry_high))
        )
        if in_explicit_range and stop < open_price < target_price:
            entry_index = index
            break

    if entry_index is None:
        if len(entry_attempts) == entry_timeout_sessions:
            outcome.update(
                {
                    "status": "timeout",
                    "exit_at": entry_attempts[-1]["date"],
                    "exit_reason": "entry_not_filled",
                }
            )
        return outcome

    planned_risk = reference_price - stop
    entry_bar = eligible[entry_index]
    entry_price = float(entry_bar["open"])
    outcome.update(
        {
            "entry_at": entry_bar["date"],
            "entry_price": round(entry_price, 8),
            "planned_risk_per_share": round(planned_risk, 8),
        }
    )

    observed = eligible[entry_index : entry_index + timeout_sessions]
    for session_number, bar in enumerate(observed, start=1):
        open_price = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        outcome["sessions_observed"] = session_number
        outcome["last_bar_at"] = bar["date"]
        stop_hit = low <= stop
        target_hit = high >= target_price
        if stop_hit:
            # Stop-first also covers a daily candle that touched both barriers.
            fill = open_price if open_price < stop else stop
            reason = "gap_stop" if open_price < stop else "stop"
            return _resolved_outcome(
                outcome,
                status="loss",
                exit_at=bar["date"],
                exit_price=fill,
                exit_reason=reason,
            )
        if target_hit:
            # Do not grant favorable slippage when the market gaps above T1.
            return _resolved_outcome(
                outcome,
                status="win",
                exit_at=bar["date"],
                exit_price=target_price,
                exit_reason="target",
            )
        if session_number == timeout_sessions:
            return _resolved_outcome(
                outcome,
                status="timeout",
                exit_at=bar["date"],
                exit_price=float(bar["close"]),
                exit_reason="timeout_close",
            )
    return outcome


class SignalLedger:
    """Thread-safe JSON signal ledger with atomic replacement writes."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._new_document()
        self._load()

    @staticmethod
    def _new_document() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ledger_version": LEDGER_VERSION,
            "revision": 0,
            "updated_at": None,
            "signals": {},
        }

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SignalLedgerError(f"cannot read signal ledger: {self.path}") from exc
            if not isinstance(raw, dict):
                raise SignalLedgerError("signal ledger root must be an object")
            if raw.get("schema_version") != SCHEMA_VERSION or raw.get("ledger_version") != LEDGER_VERSION:
                raise SignalLedgerError("unsupported signal ledger schema version")
            if not isinstance(raw.get("signals"), dict):
                raise SignalLedgerError("signal ledger signals must be an object keyed by id")
            self._data = raw

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["revision"] = int(self._data.get("revision", 0)) + 1
        self._data["updated_at"] = _utc_now()
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def record_signal(
        self,
        *,
        symbol: str,
        signal_at: str | date | datetime,
        entry_plan: Mapping[str, Any],
        targets: Sequence[Any],
        stop: float,
        score_version: str,
        features: Any,
        model_reviews: Iterable[Mapping[str, Any]] | None = None,
        score: float | None = None,
        signal_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an immutable point-in-time signal snapshot.

        Repeating the same call returns the existing record without writing.
        A caller-supplied id may be used to join another system's identifier;
        otherwise a deterministic id is derived from immutable signal facts.
        """

        normalized_symbol = str(symbol or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", normalized_symbol):
            raise ValueError("symbol is invalid")
        normalized_signal_at = _datetime_text(signal_at, "signal_at")
        normalized_plan = _normalize_entry_plan(entry_plan)
        normalized_targets = _normalize_targets(targets)
        normalized_stop = _positive_number(stop, "stop")
        if normalized_stop >= normalized_plan["reference_price"]:
            raise ValueError("stop must be below entry_plan.reference_price")
        if any(item["price"] <= normalized_plan["reference_price"] for item in normalized_targets):
            raise ValueError("targets must be above entry_plan.reference_price")
        if (
            normalized_plan.get("entry_low") is not None
            and normalized_stop >= normalized_plan["entry_low"]
        ):
            raise ValueError("stop must be below entry_plan.entry_low")
        if (
            normalized_plan.get("entry_high") is not None
            and any(item["price"] <= normalized_plan["entry_high"] for item in normalized_targets)
        ):
            raise ValueError("targets must be above entry_plan.entry_high")
        normalized_version = str(score_version or "").strip()
        if not normalized_version:
            raise ValueError("score_version is required")
        normalized_features = _canonical(features)
        _secret_free(normalized_features, "features")
        feature_digest = features_hash(normalized_features)
        reviews = _normalize_reviews(model_reviews)
        normalized_score = _optional_number(score, "score")

        identity = {
            "symbol": normalized_symbol,
            "signal_at": normalized_signal_at,
            "entry_plan": normalized_plan,
            "targets": normalized_targets,
            "stop": normalized_stop,
            "score_version": normalized_version,
            "features_hash": feature_digest,
        }
        normalized_id = str(signal_id or _signal_id(identity)).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{6,96}", normalized_id):
            raise ValueError("signal_id is invalid")
        record = {
            "id": normalized_id,
            **identity,
            "features": normalized_features,
            "score": normalized_score,
            "model_reviews": reviews,
            "recorded_at": _utc_now(),
            "outcome": _empty_outcome(
                0.0,
                20,
                normalized_plan["max_entry_sessions"],
            ),
        }

        with self._lock:
            existing = self._data["signals"].get(normalized_id)
            if existing is not None:
                immutable_keys = (
                    "id",
                    "symbol",
                    "signal_at",
                    "entry_plan",
                    "targets",
                    "stop",
                    "score_version",
                    "features_hash",
                    "features",
                    "score",
                )
                if any(existing.get(key) != record.get(key) for key in immutable_keys):
                    raise SignalLedgerError(f"signal id {normalized_id} already exists with different data")
                changed = False
                for review in reviews:
                    if review not in existing["model_reviews"]:
                        existing["model_reviews"].append(review)
                        changed = True
                if changed:
                    self._write_locked()
                return copy.deepcopy(existing)
            self._data["signals"][normalized_id] = record
            self._write_locked()
            return copy.deepcopy(record)

    def add_model_review(self, signal_id: str, review: Mapping[str, Any]) -> dict[str, Any]:
        """Attach one secret-free structured review, idempotently."""

        normalized = _normalize_reviews([review])[0]
        with self._lock:
            record = self._data["signals"].get(signal_id)
            if record is None:
                raise KeyError(signal_id)
            if normalized not in record["model_reviews"]:
                record["model_reviews"].append(normalized)
                self._write_locked()
            return copy.deepcopy(record)

    def update_outcome(
        self,
        signal_id: str,
        bars: Iterable[Any],
        *,
        timeout_sessions: int = 20,
        round_trip_cost_bps: float = 30.0,
        recompute: bool = False,
    ) -> dict[str, Any]:
        """Update one signal from complete dated OHLCV history.

        Resolved records are immutable unless ``recompute=True`` is requested
        for a deliberate data correction.  Repeating an evaluation with the
        same bars performs no write and leaves the revision unchanged.
        """

        with self._lock:
            record = self._data["signals"].get(signal_id)
            if record is None:
                raise KeyError(signal_id)
            if record.get("outcome", {}).get("status") in RESOLVED_STATUSES and not recompute:
                return copy.deepcopy(record)
            result = evaluate_outcome(
                record,
                bars,
                timeout_sessions=timeout_sessions,
                round_trip_cost_bps=round_trip_cost_bps,
            )
            if result != record.get("outcome"):
                record["outcome"] = result
                self._write_locked()
            return copy.deepcopy(record)

    def update_outcomes(
        self,
        histories: Mapping[str, Iterable[Any]],
        *,
        timeout_sessions: int = 20,
        round_trip_cost_bps: float = 30.0,
        recompute: bool = False,
    ) -> list[dict[str, Any]]:
        """Batch-update all signals whose symbol exists in ``histories``."""

        # Materialize once so a generator can safely be reused by several
        # signals for the same symbol.
        normalized_histories = {
            str(symbol).strip().upper(): tuple(bars)
            for symbol, bars in histories.items()
        }
        updated: list[dict[str, Any]] = []
        with self._lock:
            changed = False
            for record in self._data["signals"].values():
                bars = normalized_histories.get(record["symbol"])
                if bars is None:
                    continue
                if record.get("outcome", {}).get("status") in RESOLVED_STATUSES and not recompute:
                    updated.append(copy.deepcopy(record))
                    continue
                result = evaluate_outcome(
                    record,
                    bars,
                    timeout_sessions=timeout_sessions,
                    round_trip_cost_bps=round_trip_cost_bps,
                )
                if result != record.get("outcome"):
                    record["outcome"] = result
                    changed = True
                updated.append(copy.deepcopy(record))
            if changed:
                self._write_locked()
        return updated

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._data["signals"].get(signal_id)
            return copy.deepcopy(record) if record is not None else None

    def list_signals(self, *, symbol: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._data["signals"].values())
            if symbol is not None:
                normalized_symbol = symbol.strip().upper()
                records = [record for record in records if record["symbol"] == normalized_symbol]
            if status is not None:
                records = [record for record in records if record.get("outcome", {}).get("status") == status]
            records.sort(key=lambda record: (record["signal_at"], record["id"]))
            return copy.deepcopy(records)

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._data.get("revision", 0))

    @staticmethod
    def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        counts = {"win": 0, "loss": 0, "timeout": 0, "open": 0}
        resolved_r: list[float] = []
        resolved_returns: list[float] = []
        for record in records:
            outcome = record.get("outcome") or {}
            status = str(outcome.get("status") or OPEN_STATUS)
            if status not in counts:
                status = OPEN_STATUS
            counts[status] += 1
            if status in RESOLVED_STATUSES:
                if outcome.get("r_multiple") is not None:
                    resolved_r.append(float(outcome["r_multiple"]))
                if outcome.get("net_return_pct") is not None:
                    resolved_returns.append(float(outcome["net_return_pct"]))
        barrier_resolved = counts["win"] + counts["loss"]
        resolved = barrier_resolved + counts["timeout"]
        hit_rate = counts["win"] / barrier_resolved * 100.0 if barrier_resolved else None
        all_resolved_win_rate = counts["win"] / resolved * 100.0 if resolved else None
        expectancy_r = sum(resolved_r) / len(resolved_r) if resolved_r else None
        average_net_return = (
            sum(resolved_returns) / len(resolved_returns) if resolved_returns else None
        )
        return {
            "total": len(records),
            "resolved": resolved,
            "open": counts["open"],
            "win": counts["win"],
            "loss": counts["loss"],
            "timeout": counts["timeout"],
            "hit_rate_pct": round(hit_rate, 4) if hit_rate is not None else None,
            "resolved_win_rate_pct": (
                round(all_resolved_win_rate, 4) if all_resolved_win_rate is not None else None
            ),
            "expectancy_r": round(expectancy_r, 6) if expectancy_r is not None else None,
            "average_net_return_pct": (
                round(average_net_return, 6) if average_net_return is not None else None
            ),
        }

    def summary(self) -> dict[str, Any]:
        """Return live aggregate metrics, including score-version cohorts."""

        with self._lock:
            records = list(self._data["signals"].values())
            by_version: dict[str, list[Mapping[str, Any]]] = {}
            for record in records:
                by_version.setdefault(str(record["score_version"]), []).append(record)
            return {
                "schema_version": SCHEMA_VERSION,
                "ledger_revision": int(self._data.get("revision", 0)),
                "generated_at": _utc_now(),
                **self._metrics(records),
                "by_score_version": {
                    version: self._metrics(items)
                    for version, items in sorted(by_version.items())
                },
            }


__all__ = [
    "LEDGER_VERSION",
    "SCHEMA_VERSION",
    "SignalLedger",
    "SignalLedgerError",
    "evaluate_outcome",
    "features_hash",
]
