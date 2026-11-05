"""Atomic admission reservations and observed-usage circuit breakers.

CLI usage is reported at turn boundaries. These are NOT provider billing caps.
Unknown usage retains its reservation and blocks subsequent inference.
"""
from __future__ import annotations
import json
from decimal import Decimal
from pathlib import Path
from .config import money
from .contracts import BudgetError


def validate_usage(usage: dict) -> dict:
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= set(usage):
        raise BudgetError("Incomplete token telemetry")
    result = {}
    for key, value in usage.items():
        if type(value) is not int or value < 0:
            raise BudgetError(f"Invalid token telemetry: {key}")
        result[key] = value
    if result.get("cached_input_tokens", 0) > result["input_tokens"]:
        raise BudgetError("Cached input exceeds input tokens")
    if result.get("reasoning_output_tokens", 0) > result["output_tokens"]:
        raise BudgetError("Reasoning output exceeds output tokens")
    return result

def tokens(usage: dict) -> int:
    return usage["input_tokens"] + usage["output_tokens"]

def price(usage: dict, rates: dict | None) -> Decimal | None:
    if rates is None:
        return None
    cached = usage.get("cached_input_tokens", 0)
    return ((usage["input_tokens"] - cached) * money(rates["input"]) + cached * money(rates["cached_input"]) +
            usage["output_tokens"] * money(rates["output"])) / Decimal(1000000)

class Ledger:
    def __init__(self, config: dict, records: list, lock, persist):
        self.config, self.records, self.lock, self.persist = config, records, lock, persist

    @staticmethod
    def charge(record: dict) -> tuple[int, Decimal]:
        observed = tokens(record["usage"]) if record.get("usage") else 0
        cost = Decimal(record.get("cost_usd") or "0")
        if record.get("usage_complete"):
            return observed, cost
        return max(record["reserved_tokens"], observed), max(Decimal(record["reserved_cost_usd"]), cost)

    def totals(self, records=None) -> tuple[int, Decimal]:
        charges = [self.charge(r) for r in (self.records if records is None else records)]
        return sum(x[0] for x in charges), sum((x[1] for x in charges), Decimal(0))

    def check(self) -> None:
        cfg = self.config["budgets"]
        def check_group(items, token_limit, cost_limit, name):
            count, usd = self.totals(items)
            if count > token_limit or (cost_limit and usd > money(cost_limit)):
                raise BudgetError(f"Budget exhausted for {name}: {count} charged/reserved tokens, USD {usd}")
        check_group(self.records, cfg["run_tokens"], cfg["run_cost_usd"], "run")
        for ancestor in {a for r in self.records for a in r["ancestors"]}:
            check_group([r for r in self.records if ancestor in r["ancestors"]], cfg["task_tokens"], cfg["task_cost_usd"], ancestor)
        for role, limit in cfg["model_tokens"].items():
            check_group([r for r in self.records if r["role"] == role], limit, "", role)

    def reserve(self, role: str, task_id: str, ancestors: list[str], mode: str) -> dict:
        with self.lock:
            if self.config["budgets"]["mode"] == "strict":
                raise BudgetError("Codex CLI does not expose a verified hard per-request billing cap; strict mode refuses inference")
            if any(r["status"] == "unknown" for r in self.records):
                raise BudgetError("Unresolved token telemetry; no further model is admitted")
            if len(self.records) >= self.config["runtime"]["max_calls"]:
                raise BudgetError("Model call budget exhausted")
            for ancestor in ancestors:
                if sum(ancestor in r["ancestors"] for r in self.records) >= self.config["budgets"]["task_calls"]:
                    raise BudgetError(f"Task/subtree call budget exhausted: {ancestor}")
            cap = self.config["budgets"]["call_tokens"]
            rates = self.config["prices"].get(role)
            reserve_cost = max(map(money, rates.values())) * cap / Decimal(1000000) if rates else Decimal(0)
            record = {"number": len(self.records) + 1, "task_id": task_id, "ancestors": sorted(set(ancestors)),
                      "role": role, "model": self.config["models"][role]["model"], "mode": mode,
                      "status": "reserved", "reserved_tokens": cap, "reserved_cost_usd": str(reserve_cost),
                      "usage": None, "usage_complete": False, "cost_usd": None}
            self.records.append(record)
            try:
                self.check()
            except BudgetError:
                self.records.pop()  # no process started, no cost incurred
                raise
            self.persist()
            return record

    def observe(self, record: dict, usage: dict) -> None:
        with self.lock:
            record["usage"] = validate_usage(usage)
            usd = price(usage, self.config["prices"].get(record["role"]))
            record["cost_usd"] = str(usd) if usd is not None else None
            self.persist()
            if tokens(usage) > record["reserved_tokens"]:
                raise BudgetError("Per-call token ceiling exceeded; cancelling local process")
            self.check()

    def finish(self, record: dict, usage: dict | None, complete: bool, status: str):
        with self.lock:
            if usage is not None:
                record["usage"] = validate_usage(usage)
                usd = price(usage, self.config["prices"].get(record["role"]))
                record["cost_usd"] = str(usd) if usd is not None else None
            record["usage_complete"] = bool(complete and usage is not None)
            record["status"] = status if record["usage_complete"] else "unknown"
            self.persist()
            self.check()
            if not record["usage_complete"]:
                raise BudgetError("Missing/incomplete token telemetry; reservation retained, run blocked")
            if tokens(usage) > record["reserved_tokens"]:
                raise BudgetError("Per-call token ceiling exceeded")

class UsageObserver:
    def __init__(self, path: Path, ledger: Ledger, record: dict, request_path: Path | None = None):
        self.request_path, self.request_offset, self.request_buffer, self.request_usage = request_path, 0, b"", None
        self.path, self.ledger, self.record = path, ledger, record
        self.offset, self.buffer, self.usage, self.failed = 0, b"", None, False

    def best_usage(self):
        candidates = [x for x in (self.usage, self.request_usage) if x is not None]
        return max(candidates, key=tokens) if candidates else None

    def requests(self):
        if self.request_path is None or not self.request_path.exists():
            return
        with self.request_path.open("rb") as stream:
            stream.seek(self.request_offset)
            data = stream.read()
            self.request_offset += len(data)
        self.request_buffer += data
        while b"\n" in self.request_buffer:
            line, self.request_buffer = self.request_buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
                usage = event.get("usage")
                if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= set(usage):
                    continue  # unsupported optional hook shape; authoritative CLI telemetry remains required
                usage = validate_usage(usage)
            except (ValueError, AttributeError):
                continue
            if self.request_usage is None:
                self.request_usage = {}
            for key, value in usage.items():
                self.request_usage[key] = self.request_usage.get(key, 0) + value
            self.ledger.observe(self.record, self.best_usage().copy())
        if len(self.request_buffer) > 65536:
            raise BudgetError("Oversized request telemetry event")

    def __call__(self):
        self.requests()
        if not self.path.exists():
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read()
            self.offset += len(data)
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 1024 * 1024:
                raise BudgetError("Oversized event")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"turn.failed", "error"}:
                self.failed = True
            if event.get("type") == "turn.completed":
                part = validate_usage(event.get("usage"))
                if self.usage is None:
                    self.usage = {}
                for key, value in part.items():
                    self.usage[key] = self.usage.get(key, 0) + value
                self.ledger.observe(self.record, self.best_usage().copy())
        if len(self.buffer) > 1024 * 1024:
            raise BudgetError("Oversized unterminated event")
