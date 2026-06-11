import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import token_usage


class TokenUsageTest(unittest.TestCase):
    def test_unpriced_cost_is_always_dash(self):
        self.assertEqual(token_usage.cost_cell(12.34, False), "-")
        self.assertEqual(token_usage.cost_cell(0, False), "-")
        self.assertEqual(token_usage.cost_cell(0, True), "$0.00")

    def test_claude_prefers_non_sidechain_and_larger_record(self):
        base = token_usage.UsageRecord(
            timestamp=token_usage.datetime.now().astimezone(),
            tool="claude",
            model="claude-test",
            total_tokens=100,
            is_sidechain=True,
        )
        parent = token_usage.replace(base, total_tokens=90, is_sidechain=False)
        larger = token_usage.replace(parent, total_tokens=120)

        self.assertTrue(token_usage.prefer_claude_record(parent, base))
        self.assertTrue(token_usage.prefer_claude_record(larger, parent))

    def test_codex_splits_cached_input_without_double_counting_reasoning(self):
        record = token_usage.codex_usage_record(
            token_usage.datetime.now().astimezone(),
            "gpt-test",
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 120,
            },
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.input_tokens, 60)
        self.assertEqual(record.cache_read_tokens, 40)
        self.assertEqual(record.output_tokens, 20)
        self.assertEqual(record.reasoning_tokens, 5)
        self.assertEqual(record.total_tokens, 120)

    def test_opencode_attributes_total_fallback_to_same_model(self):
        record = token_usage.opencode_record(
            {
                "role": "assistant",
                "modelID": "model-test",
                "providerID": "provider-test",
                "time": {"created": 1_767_316_800_000},
                "tokens": {
                    "input": 100,
                    "output": 20,
                    "reasoning": 10,
                    "cache": {"read": 30, "write": 0},
                    "total": 160,
                },
            },
            "message-1",
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.model, "model-test")
        self.assertEqual(record.output_tokens, 30)
        self.assertEqual(record.total_tokens, 160)

    def test_models_dev_requires_provider_match(self):
        pricing = {
            "provider-test": {
                "models": {
                    "model-test": {
                        "cost": {"input": 1.0, "output": 2.0}
                    }
                }
            }
        }

        self.assertIsNone(token_usage.models_dev_price(pricing, "model-test"))
        matched = token_usage.models_dev_price(
            pricing, "provider_test/model-test"
        )
        self.assertEqual(matched["input_cost_per_token"], 0.000001)

    def test_opencode_database_loader_deduplicates_storage_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = sqlite3.connect(root / "opencode.db")
            database.execute(
                "CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)"
            )
            data = json.dumps(
                {
                    "role": "assistant",
                    "modelID": "model-test",
                    "providerID": "provider-test",
                    "time": {"created": 1_767_316_800_000},
                    "tokens": {"input": 100, "output": 20, "total": 120},
                }
            )
            database.execute(
                "INSERT INTO message VALUES (?, ?, ?)",
                ("message-1", "session-1", data),
            )
            database.commit()
            database.close()

            storage = root / "storage" / "message"
            storage.mkdir(parents=True)
            (storage / "message-1.json").write_text(
                json.dumps({**json.loads(data), "id": "message-1"}),
                encoding="utf-8",
            )

            original = token_usage.opencode_paths
            token_usage.opencode_paths = lambda: [root]
            try:
                records, _ = token_usage.load_opencode_records(False)
            finally:
                token_usage.opencode_paths = original

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].total_tokens, 120)


if __name__ == "__main__":
    unittest.main()
