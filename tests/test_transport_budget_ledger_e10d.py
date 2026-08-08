from __future__ import annotations
import fcntl, json, threading, urllib.error
from pathlib import Path
from types import SimpleNamespace
import pytest
from researchclaw.llm.budget_ledger import STOP_BUDGET_UNVERIFIABLE, TransportBudgetLedger, current_transport_ledger, transport_budgeted, transport_ledger_scope
from researchclaw.llm.client import LLMClient, LLMConfig
class _Response:
    def __init__(self, payload: dict[str, object]) -> None: self.payload = payload
    def __enter__(self) -> "_Response": return self
    def __exit__(self, *_args: object) -> None: return None
    def read(self) -> bytes: return json.dumps(self.payload).encode()
def _settings(**overrides: object) -> SimpleNamespace:
    values = {"provider": "openai-compatible", "transport_request_cap": 8, "transport_token_cap": 800, "transport_cost_cap_usd": 1.0, "transport_input_token_reserve": 1, "transport_price_version": "test-v1", "transport_prices": (("m", 1.0, .5, 2.0),)}
    values.update(overrides); return SimpleNamespace(**values)
def _client(**overrides: object) -> LLMClient:
    values = {"base_url": "https://primary.invalid/v1", "api_key": "top-secret-key", "primary_model": "m", "fallback_models": [], "max_tokens": 20, "max_retries": 1, "retry_base_delay": 0}
    values.update(overrides); return LLMClient(LLMConfig(**values))
def _events(path: Path) -> list[dict[str, object]]: return [json.loads(line) for line in path.read_text().splitlines()]
def _reply(**usage: object) -> _Response:
    return _Response({"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 4, **usage}})
def test_every_physical_attempt_is_reserved_and_secret_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings()); seen: list[int] = []
    def send(*_a: object, **_k: object) -> _Response:
        seen.append(len(_events(ledger.path)))
        if len(seen) == 1: raise urllib.error.URLError("down")
        return _reply()
    monkeypatch.setattr("urllib.request.urlopen", send)
    with transport_ledger_scope(ledger): assert _client(fallback_url="https://fallback.invalid/v1").chat([{"role": "user", "content": "private prompt"}]).content == "ok"
    events = _events(ledger.path)
    assert seen == [1, 3] and [e["event"] for e in events] == ["request_intent", "response"] * 2
    assert events[-1]["usage_source"] == "provider" and events[-1]["cost_usd"] == "0.000018"
    assert "top-secret-key" not in ledger.path.read_text() and "private prompt" not in ledger.path.read_text()
def test_wire_bytes_raise_configured_reserve_and_stop_before_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    def send(*_a: object, **_k: object) -> _Response:
        nonlocal calls; calls += 1; return _reply()
    monkeypatch.setattr("urllib.request.urlopen", send); ledger = TransportBudgetLedger(tmp_path, _settings(transport_token_cap=25))
    with transport_ledger_scope(ledger), pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): _client().chat([{"role": "user", "content": "payload larger than configured reserve"}])
    assert calls == 0 and ledger.poison.exists()
@pytest.mark.parametrize("failure", ["cap", "price", "corrupt"])
def test_any_stop_is_durable_when_caller_swallows(tmp_path: Path, failure: str) -> None:
    settings = _settings(transport_request_cap=1) if failure == "cap" else _settings(transport_prices=()) if failure == "price" else _settings()
    ledger = TransportBudgetLedger(tmp_path, settings)
    if failure == "corrupt": ledger.path.parent.mkdir(exist_ok=True); ledger.path.write_text("not-json\n")
    try:
        if failure == "cap": ledger.begin("m", b"x", 1)
        ledger.begin("m", b"y", 1)
    except RuntimeError: pass
    assert ledger.poison.exists()
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.begin("m", b"z", 1)
def _complete(tmp_path: Path) -> TransportBudgetLedger:
    ledger = TransportBudgetLedger(tmp_path, _settings()); attempt = ledger.begin("m", b"payload", 4); ledger.response(attempt, "m", 3, 2); return ledger
@pytest.mark.parametrize("mutation", ["delete", "usage_cost", "result", "source", "extra", "prompt", "anchor"])
def test_exact_schema_hash_chain_and_head_reject_tampering(tmp_path: Path, mutation: str) -> None:
    ledger = _complete(tmp_path); events = _events(ledger.path)
    if mutation == "delete": events = events[:-2]
    elif mutation == "anchor": ledger.anchor.write_text('{}')
    else:
        target = events[-1] if mutation not in {"extra", "prompt"} else events[0]
        if mutation == "usage_cost": target["input_tokens"] = 4; target["cost_usd"] = "0.000008"
        elif mutation == "result": target["result_class"] = "anything"
        elif mutation == "source": target["usage_source"] = "anything"
        elif mutation == "extra": target["prompt"] = "injected"
        else: target["request_sha256"] = "f" * 64
    if mutation != "anchor": ledger.path.write_text("\n".join(json.dumps(e) for e in events) + ("\n" if events else ""))
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.cumulative_cost()
    assert ledger.poison.exists()
@pytest.mark.parametrize("usage", [(1.5, 1, 0), (1, 1.5, 0), (1, 1, .5)])
def test_fraction_usage_poisoned(tmp_path: Path, usage: tuple[object, object, object]) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings()); attempt = ledger.begin("m", b"x", 2)
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.response(attempt, "m", *usage)
    assert ledger.poison.exists()
@pytest.mark.parametrize("actual", [None, "other"])
def test_actual_response_model_must_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actual: object) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response({"model": actual, "choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    ledger = TransportBudgetLedger(tmp_path, _settings())
    with transport_ledger_scope(ledger), pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): _client().chat([{"role": "user", "content": "x"}])
def test_deepseek_top_level_cache_is_charged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _reply(prompt_cache_hit_tokens=6))
    ledger = TransportBudgetLedger(tmp_path, _settings())
    with transport_ledger_scope(ledger): _client().chat([{"role": "user", "content": "x"}])
    event = _events(ledger.path)[-1]; assert event["cached_tokens"] == 6 and event["cost_usd"] == "0.000015"
def test_missing_price_field_is_not_zero(tmp_path: Path) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings(transport_prices=(("m", 1, None, 2),)))
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.begin("m", b"x", 1)
@pytest.mark.parametrize("result,source", [("success", "conservative_reservation"), ("transport_error", "provider")])
def test_result_source_pair_is_frozen_on_write_and_replay(tmp_path: Path, result: str, source: str) -> None:
    ledger = TransportBudgetLedger(tmp_path / "write", _settings()); attempt = ledger.begin("m", b"x", 2)
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.response(attempt, "m", 1, 1, result_class=result, usage_source=source)
    replay = _complete(tmp_path / "replay"); events = _events(replay.path); events[-1]["result_class"] = result; events[-1]["usage_source"] = source; events[-1]["event_sha256"] = replay._digest(events[-1])
    replay.path.write_text("\n".join(json.dumps(e) for e in events) + "\n"); replay.anchor.write_text(json.dumps({"schema_version": 1, "count": len(events), "head_sha256": events[-1]["event_sha256"]}))
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): replay.cumulative_cost()
@pytest.mark.parametrize("exact", [{"transport_request_cap": 1}, {"transport_token_cap": 2}, {"transport_cost_cap_usd": .000003}])
def test_equal_cap_then_plus_one_stops(tmp_path: Path, exact: dict[str, object]) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings(**exact)); ledger.begin("m", b"x", 1)
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): ledger.begin("m", b"x", 1)
def test_concurrent_admission_has_no_over_send(tmp_path: Path) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings(transport_request_cap=1)); barrier = threading.Barrier(2); outcomes: list[str] = []
    def worker() -> None:
        barrier.wait()
        try: ledger.begin("m", b"x", 1); outcomes.append("sent")
        except RuntimeError: outcomes.append("stopped")
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes) == ["sent", "stopped"]
def test_terminal_poison_linearizes_before_intent_across_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request_ledger = TransportBudgetLedger(tmp_path, _settings()); poison_ledger = TransportBudgetLedger(tmp_path, _settings())
    at_flock = threading.Event(); release = threading.Event(); calls = 0; errors: list[BaseException] = []
    real_flock = fcntl.flock
    def controlled_flock(fd: object, operation: int) -> None:
        if threading.current_thread().name == "request" and operation == fcntl.LOCK_EX:
            at_flock.set(); assert release.wait(5)
        real_flock(fd, operation)
    def send(*_a: object, **_k: object) -> _Response:
        nonlocal calls; calls += 1; return _reply()
    def request() -> None:
        try:
            with transport_ledger_scope(request_ledger): _client().chat([{"role": "user", "content": "x"}])
        except BaseException as exc: errors.append(exc)
    monkeypatch.setattr("researchclaw.llm.budget_ledger.fcntl.flock", controlled_flock)
    monkeypatch.setattr("urllib.request.urlopen", send)
    thread = threading.Thread(target=request, name="request"); thread.start(); assert at_flock.wait(5)
    assert isinstance(poison_ledger._fail("forced terminal stop"), RuntimeError)
    assert poison_ledger.poison.exists(); release.set(); thread.join(5)
    assert not thread.is_alive() and calls == 0
    assert errors and STOP_BUDGET_UNVERIFIABLE in str(errors[0])
    assert not request_ledger.path.exists() or _events(request_ledger.path) == []
def test_committed_intent_may_send_then_poison_stops_future_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request_ledger = TransportBudgetLedger(tmp_path, _settings()); poison_ledger = TransportBudgetLedger(tmp_path, _settings())
    sent = threading.Event(); release = threading.Event(); calls = 0; errors: list[BaseException] = []
    def send(*_a: object, **_k: object) -> _Response:
        nonlocal calls; calls += 1; sent.set(); assert release.wait(5); return _reply()
    def request() -> None:
        try:
            with transport_ledger_scope(request_ledger): _client().chat([{"role": "user", "content": "x"}])
        except BaseException as exc: errors.append(exc)
    monkeypatch.setattr("urllib.request.urlopen", send)
    thread = threading.Thread(target=request); thread.start(); assert sent.wait(5)
    poison = threading.Thread(target=lambda: poison_ledger._fail("forced terminal stop")); poison.start(); poison.join(5)
    assert not poison.is_alive() and poison_ledger.poison.exists()
    release.set(); thread.join(5)
    assert not thread.is_alive() and calls == 1 and errors
    assert [event["event"] for event in _events(request_ledger.path)] == ["request_intent"]
    with pytest.raises(RuntimeError, match=STOP_BUDGET_UNVERIFIABLE): request_ledger.begin("m", b"next", 1)
def test_cli_preflight_and_pipeline_share_resolved_ledger() -> None:
    source = Path("researchclaw/cli.py").read_text()
    assert source.index("transport_ledger =") < source.index("transport_ledger.call(client.preflight)")
    assert "_transport_ledger=transport_ledger" in source
def test_decorator_reuses_active_ledger(tmp_path: Path) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings())
    @transport_budgeted
    def probe(**_kwargs: object) -> object: return current_transport_ledger()
    with transport_ledger_scope(ledger): assert probe(run_dir=tmp_path, config=SimpleNamespace(llm=_settings()), _transport_ledger=TransportBudgetLedger(tmp_path / "other", _settings())) is ledger
@pytest.mark.parametrize("payload", [[], None, 1, "scalar"])
def test_non_object_json_response_terminally_poisons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    ledger = TransportBudgetLedger(tmp_path, _settings()); calls = 0; errors: list[Exception] = []
    def send(*_a: object, **_k: object) -> _Response:
        nonlocal calls
        calls += 1; return _Response(payload)  # type: ignore[arg-type]
    monkeypatch.setattr("urllib.request.urlopen", send)
    with transport_ledger_scope(ledger):
        for _ in range(2):
            try: _client().chat([{"role": "user", "content": "x"}])
            except Exception as exc: errors.append(exc)
    assert calls == 1 and ledger.poison.exists()
    assert len(errors) == 2 and all(STOP_BUDGET_UNVERIFIABLE in str(exc) for exc in errors)
    assert [event["event"] for event in _events(ledger.path)] == ["request_intent"]
