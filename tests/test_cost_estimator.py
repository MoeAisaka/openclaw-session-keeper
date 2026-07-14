from decimal import Decimal
import json
import subprocess
import sys
import unittest

from cost_estimator import Pricing, Workload, compare, simulate


class CostEstimatorTests(unittest.TestCase):
    def test_long_context_multiplier_applies_to_full_request(self):
        pricing = Pricing(
            input_per_million=Decimal("100"),
            cached_input_per_million=Decimal("10"),
            output_per_million=Decimal("200"),
            long_context_threshold=100,
            long_input_multiplier=Decimal("2"),
            long_output_multiplier=Decimal("1.5"),
        )
        workload = Workload(
            turns=1,
            initial_prompt_tokens=200,
            growth_per_turn=0,
            output_per_turn=20,
            rollover_total_tokens=150,
            post_rollover_prompt_tokens=20,
        )
        result = simulate(workload=workload, pricing=pricing, keeper_enabled=False)
        expected = Decimal(200) * Decimal(100) * Decimal(2) / Decimal(1_000_000)
        expected += Decimal(20) * Decimal(200) * Decimal("1.5") / Decimal(1_000_000)
        self.assertEqual(result.cost, expected)
        self.assertEqual(result.long_context_requests, 1)

    def test_reference_scenario_is_reproducible(self):
        result = compare(workload=Workload(), pricing=Pricing())
        self.assertEqual(result.without_keeper.total_tokens, 8_240_000)
        self.assertEqual(result.with_keeper.total_tokens, 5_240_000)
        self.assertEqual(result.without_keeper.long_context_requests, 11)
        self.assertEqual(result.with_keeper.long_context_requests, 0)
        self.assertEqual(result.with_keeper.rollovers, 1)
        self.assertEqual(result.without_keeper.cost, Decimal("264.6500"))
        self.assertEqual(result.with_keeper.cost, Decimal("169.5000"))
        self.assertEqual(result.token_savings_percent.quantize(Decimal("0.1")), Decimal("36.4"))
        self.assertEqual(result.cost_savings_percent.quantize(Decimal("0.1")), Decimal("36.0"))

    def test_rollover_cold_start_is_charged_as_uncached_input(self):
        workload = Workload(
            turns=3,
            initial_prompt_tokens=100,
            growth_per_turn=50,
            output_per_turn=0,
            rollover_total_tokens=100,
            post_rollover_prompt_tokens=20,
        )
        result = simulate(workload=workload, pricing=Pricing(), keeper_enabled=True)
        self.assertEqual(result.rollovers, 1)
        self.assertEqual(result.uncached_input_tokens, 170)
        self.assertEqual(result.cached_input_tokens, 20)

    def test_invalid_workload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "turns_must_be_positive"):
            compare(workload=Workload(turns=0), pricing=Pricing())

    def test_json_cli_output(self):
        completed = subprocess.run(
            [sys.executable, "cost_estimator.py", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["comparison"]["with_keeper"]["rollovers"], 1)
        self.assertEqual(payload["comparison"]["cost_savings_percent"], 35.953146)


if __name__ == "__main__":
    unittest.main()
