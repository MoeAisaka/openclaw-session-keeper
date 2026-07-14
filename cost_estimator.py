#!/usr/bin/env python3
"""Estimate token and tiered-pricing impact of physical-session rollover.

The estimator is deliberately provider-configurable. Its defaults mirror the
public GPT-5.6 Sol Codex credit card and long-context multipliers published by
OpenAI on 2026-07-14, but callers can replace every rate and threshold.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Sequence


MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Pricing:
    input_per_million: Decimal = Decimal("125")
    cached_input_per_million: Decimal = Decimal("12.5")
    output_per_million: Decimal = Decimal("750")
    long_context_threshold: int = 272_000
    long_input_multiplier: Decimal = Decimal("2")
    long_output_multiplier: Decimal = Decimal("1.5")
    unit: str = "credits"


@dataclass(frozen=True)
class Workload:
    turns: int = 40
    initial_prompt_tokens: int = 48_000
    growth_per_turn: int = 8_000
    output_per_turn: int = 2_000
    rollover_total_tokens: int = 235_000
    post_rollover_prompt_tokens: int = 48_000


@dataclass(frozen=True)
class Scenario:
    turns: int
    rollovers: int
    long_context_requests: int
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: Decimal


@dataclass(frozen=True)
class Comparison:
    without_keeper: Scenario
    with_keeper: Scenario
    token_savings: int
    token_savings_percent: Decimal
    cost_savings: Decimal
    cost_savings_percent: Decimal
    pricing_unit: str


def _non_negative(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name}_must_be_non_negative")


def validate(pricing: Pricing, workload: Workload) -> None:
    if workload.turns < 1:
        raise ValueError("turns_must_be_positive")
    for name in (
        "initial_prompt_tokens",
        "growth_per_turn",
        "output_per_turn",
        "post_rollover_prompt_tokens",
    ):
        _non_negative(name, int(getattr(workload, name)))
    if workload.rollover_total_tokens < 1:
        raise ValueError("rollover_total_tokens_must_be_positive")
    if pricing.long_context_threshold < 1:
        raise ValueError("long_context_threshold_must_be_positive")
    for name in (
        "input_per_million",
        "cached_input_per_million",
        "output_per_million",
        "long_input_multiplier",
        "long_output_multiplier",
    ):
        if getattr(pricing, name) < 0:
            raise ValueError(f"{name}_must_be_non_negative")
    if not pricing.unit.strip():
        raise ValueError("unit_must_not_be_empty")


def _request_cost(
    *,
    prompt_tokens: int,
    uncached_input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    pricing: Pricing,
) -> Decimal:
    is_long = prompt_tokens > pricing.long_context_threshold
    input_multiplier = pricing.long_input_multiplier if is_long else Decimal(1)
    output_multiplier = pricing.long_output_multiplier if is_long else Decimal(1)
    return (
        Decimal(uncached_input_tokens)
        * pricing.input_per_million
        * input_multiplier
        / MILLION
        + Decimal(cached_input_tokens)
        * pricing.cached_input_per_million
        * input_multiplier
        / MILLION
        + Decimal(output_tokens)
        * pricing.output_per_million
        * output_multiplier
        / MILLION
    )


def simulate(*, workload: Workload, pricing: Pricing, keeper_enabled: bool) -> Scenario:
    validate(pricing, workload)
    prompt_tokens = workload.initial_prompt_tokens
    first_request_in_physical_session = True
    rollovers = 0
    long_context_requests = 0
    uncached_input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    cost = Decimal(0)

    for turn_index in range(workload.turns):
        if first_request_in_physical_session:
            uncached = prompt_tokens
        else:
            uncached = min(workload.growth_per_turn, prompt_tokens)
        cached = prompt_tokens - uncached
        is_long = prompt_tokens > pricing.long_context_threshold

        uncached_input_tokens += uncached
        cached_input_tokens += cached
        output_tokens += workload.output_per_turn
        long_context_requests += int(is_long)
        cost += _request_cost(
            prompt_tokens=prompt_tokens,
            uncached_input_tokens=uncached,
            cached_input_tokens=cached,
            output_tokens=workload.output_per_turn,
            pricing=pricing,
        )

        should_rollover = (
            keeper_enabled
            and turn_index < workload.turns - 1
            and prompt_tokens + workload.output_per_turn
            >= workload.rollover_total_tokens
        )
        if should_rollover:
            rollovers += 1
            prompt_tokens = workload.post_rollover_prompt_tokens
            first_request_in_physical_session = True
        else:
            prompt_tokens += workload.growth_per_turn
            first_request_in_physical_session = False

    total_tokens = uncached_input_tokens + cached_input_tokens + output_tokens
    return Scenario(
        turns=workload.turns,
        rollovers=rollovers,
        long_context_requests=long_context_requests,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost=cost,
    )


def compare(*, workload: Workload, pricing: Pricing) -> Comparison:
    without_keeper = simulate(workload=workload, pricing=pricing, keeper_enabled=False)
    with_keeper = simulate(workload=workload, pricing=pricing, keeper_enabled=True)
    token_savings = without_keeper.total_tokens - with_keeper.total_tokens
    cost_savings = without_keeper.cost - with_keeper.cost
    token_savings_percent = (
        Decimal(token_savings) / Decimal(without_keeper.total_tokens) * 100
        if without_keeper.total_tokens
        else Decimal(0)
    )
    cost_savings_percent = (
        cost_savings / without_keeper.cost * 100
        if without_keeper.cost
        else Decimal(0)
    )
    return Comparison(
        without_keeper=without_keeper,
        with_keeper=with_keeper,
        token_savings=token_savings,
        token_savings_percent=token_savings_percent,
        cost_savings=cost_savings,
        cost_savings_percent=cost_savings_percent,
        pricing_unit=pricing.unit,
    )


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return round(float(value), 6)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _format_int(value: int) -> str:
    return f"{value:,}"


def _format_decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,}"


def render_text(result: Comparison, workload: Workload, pricing: Pricing) -> str:
    baseline = result.without_keeper
    keeper = result.with_keeper
    lines = [
        "Session Keeper tiered-cost estimate",
        "",
        (
            f"Workload: {workload.turns} turns, "
            f"{_format_int(workload.initial_prompt_tokens)} starting prompt tokens, "
            f"+{_format_int(workload.growth_per_turn)} tokens/turn"
        ),
        (
            f"Rollover: {_format_int(workload.rollover_total_tokens)} total tokens; "
            f"post-rollover prompt {_format_int(workload.post_rollover_prompt_tokens)} tokens"
        ),
        (
            f"Long-context tier: >{_format_int(pricing.long_context_threshold)} prompt tokens; "
            f"input x{pricing.long_input_multiplier}, output x{pricing.long_output_multiplier}"
        ),
        "",
        "Metric                         Without Keeper      With Keeper",
        f"Total tokens                   {_format_int(baseline.total_tokens):>14}  {_format_int(keeper.total_tokens):>15}",
        f"Long-context requests          {_format_int(baseline.long_context_requests):>14}  {_format_int(keeper.long_context_requests):>15}",
        f"Physical rollovers             {_format_int(baseline.rollovers):>14}  {_format_int(keeper.rollovers):>15}",
        f"Estimated {pricing.unit:<17} {_format_decimal(baseline.cost):>14}  {_format_decimal(keeper.cost):>15}",
        "",
        f"Token reduction: {_format_int(result.token_savings)} ({result.token_savings_percent.quantize(Decimal('0.1'))}%)",
        f"Cost reduction: {_format_decimal(result.cost_savings)} {pricing.unit} ({result.cost_savings_percent.quantize(Decimal('0.1'))}%)",
        "",
        "This is a scenario estimate, not a billing guarantee. Replace the defaults with your provider rates and observed workload.",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare long-session token and tiered-pricing cost with and without Session Keeper."
    )
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--initial-prompt-tokens", type=int, default=48_000)
    parser.add_argument("--growth-per-turn", type=int, default=8_000)
    parser.add_argument("--output-per-turn", type=int, default=2_000)
    parser.add_argument("--rollover-total-tokens", type=int, default=235_000)
    parser.add_argument("--post-rollover-prompt-tokens", type=int, default=48_000)
    parser.add_argument("--long-context-threshold", type=int, default=272_000)
    parser.add_argument("--input-rate", type=_decimal, default=Decimal("125"))
    parser.add_argument("--cached-input-rate", type=_decimal, default=Decimal("12.5"))
    parser.add_argument("--output-rate", type=_decimal, default=Decimal("750"))
    parser.add_argument("--long-input-multiplier", type=_decimal, default=Decimal("2"))
    parser.add_argument("--long-output-multiplier", type=_decimal, default=Decimal("1.5"))
    parser.add_argument("--unit", default="credits")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workload = Workload(
        turns=args.turns,
        initial_prompt_tokens=args.initial_prompt_tokens,
        growth_per_turn=args.growth_per_turn,
        output_per_turn=args.output_per_turn,
        rollover_total_tokens=args.rollover_total_tokens,
        post_rollover_prompt_tokens=args.post_rollover_prompt_tokens,
    )
    pricing = Pricing(
        input_per_million=args.input_rate,
        cached_input_per_million=args.cached_input_rate,
        output_per_million=args.output_rate,
        long_context_threshold=args.long_context_threshold,
        long_input_multiplier=args.long_input_multiplier,
        long_output_multiplier=args.long_output_multiplier,
        unit=args.unit,
    )
    try:
        result = compare(workload=workload, pricing=pricing)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.as_json:
        payload = {
            "workload": asdict(workload),
            "pricing": asdict(pricing),
            "comparison": asdict(result),
        }
        print(json.dumps(_json_value(payload), indent=2, sort_keys=True))
    else:
        print(render_text(result, workload, pricing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
