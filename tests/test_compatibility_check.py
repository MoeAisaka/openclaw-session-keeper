import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "compatibility_check.py"
SPEC = importlib.util.spec_from_file_location("compatibility_check", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class CompatibilityCheckTests(unittest.TestCase):
    def base_keeper(self):
        return {
            "thresholds": {"rolloverTokens": 235000},
            "sessions": {"agent:main:project-test": {}},
        }

    def oauth_sessions(self, *, context_tokens=372000, profile="openai:user@example.invalid"):
        return {
            "agent:main:project-test": {
                "modelProvider": "openai",
                "model": "gpt-example",
                "authProfileOverride": profile,
                "contextTokens": context_tokens,
            }
        }

    def test_flags_provider_override_for_native_codex_oauth(self):
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {
                "provider": MODULE.DETERMINISTIC_PROVIDER,
                "reserveTokensFloor": 100000,
            }}}},
            self.base_keeper(),
            self.oauth_sessions(),
        )
        codes = {item["code"] for item in result["findings"]}
        self.assertIn("native_codex_compaction_override_ignored", codes)
        self.assertIn("compaction_rollover_race_headroom", codes)

    def test_safe_native_configuration_passes(self):
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {
                "reserveTokensFloor": 50000,
                "reserveTokens": 20000,
            }}}},
            self.base_keeper(),
            self.oauth_sessions(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["effectiveReserveTokens"], 50000)

    def test_native_default_reserve_is_used_when_unconfigured(self):
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {}}}},
            self.base_keeper(),
            self.oauth_sessions(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["effectiveReserveTokens"], 20000)

    def test_explicit_zero_floor_disables_native_default(self):
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"reserveTokensFloor": 0}}}},
            self.base_keeper(),
            self.oauth_sessions(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["effectiveReserveTokens"], 0)

    def test_session_override_is_used_for_headroom(self):
        keeper = self.base_keeper()
        keeper["sessions"]["agent:main:project-test"] = {
            "thresholds": {"rolloverTokens": 300000}
        }
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"reserveTokensFloor": 50000}}}},
            keeper,
            self.oauth_sessions(),
        )
        finding = next(item for item in result["findings"] if item["code"] == "compaction_rollover_race_headroom")
        self.assertEqual(finding["rolloverTokens"], 300000)
        self.assertEqual(finding["headroomTokens"], 22000)

    def test_auth_profile_value_is_never_returned(self):
        secret_marker = "openai:private-address@example.invalid"
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"provider": MODULE.DETERMINISTIC_PROVIDER}}}},
            self.base_keeper(),
            self.oauth_sessions(profile=secret_marker),
        )
        self.assertNotIn(secret_marker, str(result))

    def test_non_oauth_session_does_not_trigger_native_warning(self):
        sessions = self.oauth_sessions()
        sessions["agent:main:project-test"].pop("authProfileOverride")
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"provider": MODULE.DETERMINISTIC_PROVIDER}}}},
            self.base_keeper(),
            sessions,
        )
        self.assertTrue(result["ok"])

    def test_missing_managed_session_state_fails_closed(self):
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"reserveTokensFloor": 50000}}}},
            self.base_keeper(),
            {},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["findings"][0]["code"], "managed_session_state_missing")

    def test_missing_headroom_input_fails_closed(self):
        sessions = self.oauth_sessions()
        sessions["agent:main:project-test"].pop("contextTokens")
        result = MODULE.analyze_compatibility(
            {"agents": {"defaults": {"compaction": {"reserveTokensFloor": 50000}}}},
            self.base_keeper(),
            sessions,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["findings"][0]["code"], "compaction_headroom_input_missing")
        self.assertEqual(result["findings"][0]["missingFields"], ["contextTokens"])


if __name__ == "__main__":
    unittest.main()
