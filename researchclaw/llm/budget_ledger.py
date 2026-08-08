"""Durable transport admission and independently replayable budget ledger."""
from __future__ import annotations
import contextlib, contextvars, fcntl, functools, hashlib, json, os, uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator
STOP_BUDGET_UNVERIFIABLE = "STOP_BUDGET_UNVERIFIABLE"
_HELD_PATHS: contextvars.ContextVar[frozenset[Path]] = contextvars.ContextVar("transport_ledger_held_paths", default=frozenset())
class BudgetUnverifiable(RuntimeError): pass
def _stop(reason: str) -> BudgetUnverifiable:
    return BudgetUnverifiable(f"{STOP_BUDGET_UNVERIFIABLE}: {reason}")
_COMMON = {"schema_version", "event", "seq", "prev_sha256", "event_sha256"}
_INTENT = _COMMON | {"attempt_id", "provider", "model", "request_sha256", "reserved_input_tokens", "reserved_output_tokens", "reserved_tokens", "reserved_cost_usd", "price_snapshot_version", "price_snapshot_hash"}
_RESPONSE = _COMMON | {"attempt_id", "provider", "model", "request_sha256", "input_tokens", "output_tokens", "cached_tokens", "usage_source", "price_snapshot_version", "price_snapshot_hash", "price_usd_per_million", "cost_usd", "result_class"}
class TransportBudgetLedger:
    def __init__(self, run_dir: Path, config: Any) -> None:
        self.path = Path(run_dir).resolve() / "transport_ledger.jsonl"; self.anchor = self.path.with_suffix(".head"); self.poison = self.path.with_suffix(".stop")
        self.provider = str(getattr(config, "provider", "") or "")
        self.caps = (int(getattr(config, "transport_request_cap", 0) or 0), int(getattr(config, "transport_token_cap", 0) or 0), Decimal(str(getattr(config, "transport_cost_cap_usd", 0) or 0)))
        self.reserve = int(getattr(config, "transport_input_token_reserve", 0) or 0); self.version = str(getattr(config, "transport_price_version", "") or "")
        self.raw_prices = {str(row[0]): tuple(row[1:]) for row in (getattr(config, "transport_prices", ()) or ()) if isinstance(row, (tuple, list)) and row}
        snapshot = {"version": self.version, "prices": [[model, *rates] for model, rates in sorted(self.raw_prices.items())]}
        self.snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def _poison(self, locked: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not locked:
            with self.path.open("a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try: return self._poison(True)
                finally: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            fd = os.open(self.poison, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: return
        with os.fdopen(fd, "w") as stream: json.dump({"schema_version": 1, "stop": STOP_BUDGET_UNVERIFIABLE}, stream, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY); os.fsync(directory); os.close(directory)
    def _fail(self, reason: str) -> BudgetUnverifiable:
        self._poison(self.path in _HELD_PATHS.get()); return _stop(reason)
    def _rates(self, model: str) -> tuple[Decimal, Decimal, Decimal]:
        try:
            raw = self.raw_prices[model]; rates = tuple(Decimal(str(value)) for value in raw)
            if not self.version or len(rates) != 3 or any(value is None for value in raw) or min(rates) < 0 or rates[1] > rates[0]: raise ValueError
            return rates  # type: ignore[return-value]
        except (KeyError, ValueError, InvalidOperation): raise self._fail(f"missing price snapshot for {model}")
    @staticmethod
    def _cost(tokens: tuple[int, int, int], rates: tuple[Decimal, Decimal, Decimal]) -> Decimal:
        return ((tokens[0] - tokens[2]) * rates[0] + tokens[2] * rates[1] + tokens[1] * rates[2]) / Decimal(1_000_000)
    @staticmethod
    def _digest(event: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps({k: v for k, v in event.items() if k != "event_sha256"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def _replay(self, events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        intents: dict[str, dict[str, Any]] = {}; responses: dict[str, dict[str, Any]] = {}; previous = "0" * 64
        for seq, event in enumerate(events, 1):
            kind = event.get("event"); expected = _INTENT if kind == "request_intent" else _RESPONSE if kind == "response" else set()
            if set(event) != expected or event.get("schema_version") != 1 or event.get("seq") != seq or event.get("prev_sha256") != previous or event.get("event_sha256") != self._digest(event): raise ValueError("schema or hash chain mismatch")
            previous = str(event["event_sha256"]); attempt = event.get("attempt_id")
            if event.get("price_snapshot_version") != self.version or event.get("price_snapshot_hash") != self.snapshot_hash or not isinstance(attempt, str): raise ValueError("snapshot or attempt mismatch")
            if kind == "request_intent" and attempt not in intents:
                model = str(event["model"]); reserved = (event["reserved_input_tokens"], event["reserved_output_tokens"], 0)
                if event["provider"] != self.provider or not all(type(v) is int and v >= 0 for v in reserved) or event["reserved_tokens"] != sum(reserved[:2]) or len(str(event["request_sha256"])) != 64 or Decimal(str(event["reserved_cost_usd"])) != self._cost(reserved, self._rates(model)): raise ValueError("invalid reservation")
                intents[attempt] = event; continue
            if kind != "response" or attempt not in intents or attempt in responses: raise ValueError("invalid event order")
            intent = intents[attempt]; model = str(event["model"]); rates = self._rates(model); tokens = tuple(event[name] for name in ("input_tokens", "output_tokens", "cached_tokens"))
            if not all(type(v) is int and v >= 0 for v in tokens) or tokens[2] > tokens[0] or event["provider"] != intent["provider"] or model != intent["model"] or event["request_sha256"] != intent["request_sha256"] or event["price_usd_per_million"] != list(map(str, rates)) or Decimal(str(event["cost_usd"])) != self._cost(tokens, rates) or (event["result_class"], event["usage_source"]) not in {("success", "provider"), ("transport_error", "conservative_reservation")}: raise ValueError("invalid response")
            responses[attempt] = event
        anchor = json.loads(self.anchor.read_text()) if self.anchor.exists() else None
        if (events or anchor) and anchor != {"schema_version": 1, "count": len(events), "head_sha256": previous}: raise ValueError("head anchor mismatch")
        return intents, responses
    @contextlib.contextmanager
    def _locked(self) -> Iterator[tuple[Any, dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX); token = _HELD_PATHS.set(_HELD_PATHS.get() | {self.path}); stream.seek(0)
            try:
                if self.poison.exists(): raise _stop("terminal poison")
                events = [json.loads(line) for line in stream if line.strip()]; intents, responses = self._replay(events); yield stream, intents, responses, events
            except BudgetUnverifiable: raise
            except (OSError, ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError) as exc: raise self._fail(f"ledger corrupt: {exc}") from exc
            finally: _HELD_PATHS.reset(token); fcntl.flock(stream, fcntl.LOCK_UN)
    def _write(self, stream: Any, event: dict[str, Any], events: list[dict[str, Any]]) -> None:
        event.update(schema_version=1, seq=len(events) + 1, prev_sha256=events[-1]["event_sha256"] if events else "0" * 64); event["event_sha256"] = self._digest(event)
        stream.seek(0, 2); stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"); stream.flush(); os.fsync(stream.fileno())
        tmp = self.anchor.with_suffix(".head.tmp")
        with tmp.open("w") as head: json.dump({"schema_version": 1, "count": event["seq"], "head_sha256": event["event_sha256"]}, head, sort_keys=True, separators=(",", ":")); head.flush(); os.fsync(head.fileno())
        os.replace(tmp, self.anchor)
        directory = os.open(self.path.parent, os.O_RDONLY); os.fsync(directory); os.close(directory); events.append(event)
    def begin(self, model: str, payload: bytes, max_output_tokens: int) -> str:
        rates = self._rates(model); request_cap, token_cap, cost_cap = self.caps
        # Frozen DeepSeek OpenAI-compatible invariant: UTF-8 wire bytes conservatively upper-bound input tokens; configuration may only raise it.
        reserved = (max(self.reserve, len(payload)), max_output_tokens, 0); reserved_cost = self._cost(reserved, rates)
        if min(request_cap, token_cap, reserved[0]) <= 0 or cost_cap <= 0 or max_output_tokens < 0: raise self._fail("invalid budget configuration")
        with self._locked() as (stream, intents, responses, events):
            used_tokens = used_cost = 0
            for attempt, intent in intents.items():
                response = responses.get(attempt); used_tokens += sum(response[n] for n in ("input_tokens", "output_tokens")) if response else int(intent["reserved_tokens"]); used_cost += Decimal(str(response["cost_usd"] if response else intent["reserved_cost_usd"]))
            if len(intents) + 1 > request_cap or used_tokens + sum(reserved[:2]) > token_cap or used_cost + reserved_cost > cost_cap: raise self._fail("next outbound exceeds transport budget")
            attempt = uuid.uuid4().hex; self._write(stream, {"event": "request_intent", "attempt_id": attempt, "provider": self.provider, "model": model, "request_sha256": hashlib.sha256(payload).hexdigest(), "reserved_input_tokens": reserved[0], "reserved_output_tokens": reserved[1], "reserved_tokens": sum(reserved[:2]), "reserved_cost_usd": str(reserved_cost), "price_snapshot_version": self.version, "price_snapshot_hash": self.snapshot_hash}, events); return attempt
    def response(self, attempt: str, model: Any, input_tokens: Any, output_tokens: Any, cached_tokens: Any = 0, *, result_class: str = "success", usage_source: str = "provider") -> None:
        try:
            if not all(type(v) is int for v in (input_tokens, output_tokens, cached_tokens)): raise ValueError("non-integer usage")
            tokens = (input_tokens, output_tokens, cached_tokens); rates = self._rates(str(model))
            if min(tokens) < 0 or tokens[2] > tokens[0]: raise ValueError("invalid usage")
            with self._locked() as (stream, intents, responses, events):
                intent = intents.get(attempt)
                if intent is None or attempt in responses or model != intent["model"] or tokens[0] > intent["reserved_input_tokens"] or tokens[1] > intent["reserved_output_tokens"]: raise ValueError("response exceeds or mismatches reservation")
                self._append_response(stream, intent, tokens, rates, result_class, usage_source, events)
        except BudgetUnverifiable: raise
        except (TypeError, ValueError, KeyError) as exc: raise self._fail(f"provider usage unavailable: {exc}") from exc
    def _append_response(self, stream: Any, intent: dict[str, Any], tokens: tuple[int, int, int], rates: tuple[Decimal, Decimal, Decimal], result: str, source: str, events: list[dict[str, Any]]) -> None:
        if (result, source) not in {("success", "provider"), ("transport_error", "conservative_reservation")}: raise self._fail("invalid result classification")
        self._write(stream, {"event": "response", "attempt_id": intent["attempt_id"], "provider": self.provider, "model": intent["model"], "request_sha256": intent["request_sha256"], "input_tokens": tokens[0], "output_tokens": tokens[1], "cached_tokens": tokens[2], "usage_source": source, "price_snapshot_version": self.version, "price_snapshot_hash": self.snapshot_hash, "price_usd_per_million": list(map(str, rates)), "cost_usd": str(self._cost(tokens, rates)), "result_class": result}, events)
    def transport_error(self, attempt: str, model: str) -> None:
        with self._locked() as (stream, intents, responses, events):
            intent = intents.get(attempt)
            if intent is None or model != intent["model"] or attempt in responses: raise self._fail("transport response mismatch")
            self._append_response(stream, intent, (intent["reserved_input_tokens"], intent["reserved_output_tokens"], 0), self._rates(model), "transport_error", "conservative_reservation", events)
    def cumulative_cost(self) -> float:
        with self._locked() as (_stream, intents, responses, _events): return float(sum((Decimal(str(responses.get(a, i).get("cost_usd", i["reserved_cost_usd"]))) for a, i in intents.items()), Decimal(0)))
    def call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        with transport_ledger_scope(self): return function(*args, **kwargs)
_ACTIVE: contextvars.ContextVar[TransportBudgetLedger | None] = contextvars.ContextVar("transport_budget_ledger", default=None)
@contextlib.contextmanager
def transport_ledger_scope(ledger: TransportBudgetLedger) -> Iterator[None]:
    token = _ACTIVE.set(ledger)
    try: yield
    finally: _ACTIVE.reset(token)
def current_transport_ledger() -> TransportBudgetLedger | None: return _ACTIVE.get()
def transport_budgeted(function: Any) -> Any:
    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        explicit = kwargs.pop("_transport_ledger", None); active = current_transport_ledger(); ledger = active or explicit or TransportBudgetLedger(kwargs["run_dir"], kwargs["config"].llm)
        if active is ledger: return function(*args, **kwargs)
        with transport_ledger_scope(ledger): return function(*args, **kwargs)
    return wrapped
