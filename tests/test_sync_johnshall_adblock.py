from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import sync_johnshall_adblock as sync


class LocalRuleTests(unittest.TestCase):
    def write_rules(self, directory: str, content: str) -> Path:
        path = Path(directory) / "rules.list"
        path.write_text(content, encoding="utf-8")
        return path

    def test_normalize_local_rules_ignores_comments_and_deduplicates(self):
        with TemporaryDirectory() as directory:
            path = self.write_rules(
                directory,
                "# comment\n\nDOMAIN,Ads.Example.com\n"
                "domain,ads.example.com\nIP-CIDR,192.0.2.0/24,no-resolve\n",
            )

            self.assertEqual(
                sync.normalize_local_rules(path),
                ["DOMAIN,ads.example.com", "IP-CIDR,192.0.2.0/24,no-resolve"],
            )

    def test_normalize_local_rules_rejects_policy_field(self):
        with TemporaryDirectory() as directory:
            path = self.write_rules(directory, "DOMAIN,ads.example.com,REJECT\n")

            with self.assertRaisesRegex(RuntimeError, "不应包含策略字段"):
                sync.normalize_local_rules(path)

    def test_normalize_local_rules_rejects_unknown_type(self):
        with TemporaryDirectory() as directory:
            path = self.write_rules(directory, "URL-REGEX,example.com\n")

            with self.assertRaisesRegex(RuntimeError, "未知规则类型"):
                sync.normalize_local_rules(path)


class MergeRuleTests(unittest.TestCase):
    def test_merge_appends_custom_deduplicates_and_applies_exact_exceptions(self):
        final, custom_new, matched, unmatched = sync.merge_rules(
            ["DOMAIN,a.example", "DOMAIN-SUFFIX,b.example"],
            ["DOMAIN,a.example", "DOMAIN,c.example"],
            ["DOMAIN,a.example", "DOMAIN,missing.example"],
        )

        self.assertEqual(
            final, ["DOMAIN-SUFFIX,b.example", "DOMAIN,c.example"]
        )
        self.assertEqual(custom_new, 1)
        self.assertEqual(matched, 1)
        self.assertEqual(unmatched, 1)

    def test_exception_matching_is_not_hierarchical(self):
        final, _, matched, _ = sync.merge_rules(
            ["DOMAIN-SUFFIX,example.com"],
            [],
            ["DOMAIN,api.example.com"],
        )

        self.assertEqual(final, ["DOMAIN-SUFFIX,example.com"])
        self.assertEqual(matched, 0)


class JohnshallGuardTests(unittest.TestCase):
    def state(self, date: str, count: int) -> sync.JohnshallState:
        return sync.JohnshallState(datetime.fromisoformat(date), count)

    def metadata(self, date: str = "2026-10-24 07:00:00", count: str = "10") -> str:
        return (
            f"# {sync.JOHNSHALL_DATE_HEADER}: {date}\n"
            f"# {sync.JOHNSHALL_COUNT_HEADER}: {count}\n"
        )

    def test_source_date_is_read_from_source_comment(self):
        state = sync.johnshall_source_state(
            "[Rule]\n# adblock rules refresh time: 2026-10-24 07:00:00\n", 10
        )
        self.assertEqual(state, self.state("2026-10-24 07:00:00", 10))

    def test_missing_invalid_or_duplicate_source_date_is_rejected(self):
        prefix = "# adblock rules refresh time: "
        sources = [
            "[Rule]\n",
            prefix + "not-a-date\n",
            prefix + "2026-02-30 07:00:00\n",
            prefix + "2026-1-1 07:00:00\n",
            (prefix + "2026-10-24 07:00:00\n") * 2,
        ]
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(RuntimeError):
                    sync.johnshall_source_state(source, 10)

    def test_date_regression_is_rejected_even_when_rule_count_grows(self):
        with self.assertRaisesRegex(RuntimeError, "源日期倒退"):
            sync.validate_johnshall_source(
                self.state("2025-02-08 06:13:06", 20),
                self.state("2026-10-23 23:04:11", 10),
            )

    def test_known_rollback_reports_both_regressions(self):
        with self.assertRaisesRegex(
            RuntimeError, "源日期倒退.*单源规则从 60599 降至 38575"
        ):
            sync.validate_johnshall_source(
                self.state("2025-02-08 06:13:06", 38575),
                self.state("2026-10-23 23:04:11", 60599),
            )

    def test_equal_or_newer_dates_are_allowed(self):
        baseline = self.state("2026-10-24 07:00:00", 10000)
        for date in ("2026-10-24 07:00:00", "2026-10-25 07:00:00"):
            with self.subTest(date=date):
                sync.validate_johnshall_source(self.state(date, 10000), baseline)

    def test_single_source_shrink_uses_exact_30_percent_boundary(self):
        baseline = self.state("2026-10-24 07:00:00", 10000)
        for count in (7000, 7001):
            with self.subTest(count=count):
                sync.validate_johnshall_source(
                    self.state("2026-10-25 07:00:00", count), baseline
                )
        with self.assertRaisesRegex(RuntimeError, "单源规则.*缩减超过 30%"):
            sync.validate_johnshall_source(
                self.state("2026-10-25 07:00:00", 6999), baseline
            )

    def test_new_install_has_no_previous_baseline(self):
        with TemporaryDirectory() as directory:
            self.assertIsNone(
                sync.load_johnshall_baseline(Path(directory) / "new.list")
            )

    def test_legacy_output_cannot_silently_accept_new_baseline(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "adblock.list"
            output.write_text("# legacy output\nDOMAIN,old.example\n")
            with self.assertRaisesRegex(RuntimeError, "--johnshall-baseline"):
                sync.load_johnshall_baseline(output)

    def test_source_metadata_roundtrips_through_generated_output(self):
        state = self.state("2026-10-24 07:00:00", 2)
        content = sync.render(
            b"source",
            "etag",
            ["DOMAIN,a.example", "DOMAIN,b.example"],
            johnshall_state=state,
            custom_count=0,
            exception_count=0,
            matched_exception_count=0,
        )
        with TemporaryDirectory() as directory:
            output = Path(directory) / "adblock.list"
            output.write_text(content)
            self.assertEqual(sync.load_johnshall_baseline(output), state)

    def test_corrupt_or_partial_saved_metadata_is_rejected(self):
        invalid = [
            f"# {sync.JOHNSHALL_DATE_HEADER}: 2026-10-24 07:00:00\n",
            f"# {sync.JOHNSHALL_COUNT_HEADER}: 10\n",
            self.metadata(count="0"),
            self.metadata(count="invalid"),
            self.metadata(date="2026-02-30 07:00:00"),
            self.metadata() + self.metadata(),
        ]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "adblock.list"
            for content in invalid:
                with self.subTest(content=content):
                    output.write_text(content)
                    with self.assertRaises(RuntimeError):
                        sync.load_johnshall_baseline(output)

    def test_trusted_source_bootstraps_legacy_format_with_unique_rule_count(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "adblock.list"
            output.write_text("# legacy output\nDOMAIN,old.example\n")
            baseline = Path(directory) / "trusted.conf"
            baseline.write_text(
                "[Rule]\n# adblock rules refresh time: 2026-10-24 07:00:00\n"
                "DOMAIN,A.example,REJECT\nDOMAIN,a.example,REJECT\n"
            )
            with patch.object(sync, "MIN_RULE_COUNT", 1):
                self.assertEqual(
                    sync.load_johnshall_baseline(output, baseline),
                    self.state("2026-10-24 07:00:00", 1),
                )

    def test_trusted_source_cannot_override_existing_metadata(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "adblock.list"
            output.write_text(self.metadata())
            with self.assertRaisesRegex(RuntimeError, "不能覆盖已有基线"):
                sync.load_johnshall_baseline(
                    output, Path(directory) / "other.conf"
                )


class JohnshallSafetyIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.files = {
            name: root / name
            for name in ("output", "custom", "exceptions", "base", "derived")
        }
        self.files["output"].write_text(
            f"# {sync.JOHNSHALL_DATE_HEADER}: 2026-10-24 07:00:00\n"
            f"# {sync.JOHNSHALL_COUNT_HEADER}: 10\n"
            + "".join(f"DOMAIN-SUFFIX,old-{i}.example\n" for i in range(10))
        )
        self.files["custom"].write_text(
            "# manual rule\nDOMAIN,manual.example\n"
            f"{sync.AWAVENUE_BLOCK_BEGIN}\n{sync.AWAVENUE_BLOCK_END}\n"
            f"{sync.ANTI_AD_BLOCK_BEGIN}\n{sync.ANTI_AD_BLOCK_END}\n"
        )
        self.files["exceptions"].write_text("# exact exceptions\n")
        self.files["base"].write_bytes(sync.DEFAULT_BASE_CONFIG.read_bytes())
        self.files["derived"].write_text("# existing derived output\n")
        self.initial = self.snapshot()

    def snapshot(self):
        return {name: path.read_bytes() for name, path in self.files.items()}

    def source(self, date: str = "2026-10-25 07:00:00", count: int = 10):
        return (
            f"[Rule]\n# adblock rules refresh time: {date}\n"
            + "".join(f"DOMAIN-SUFFIX,new-{i}.example,REJECT\n" for i in range(count))
        ).encode()

    def run_sync(self, source: bytes, *extra_args: str):
        args = [
            "sync_johnshall_adblock.py",
            "--output", str(self.files["output"]),
            "--custom-list", str(self.files["custom"]),
            "--exceptions-list", str(self.files["exceptions"]),
            "--base-config", str(self.files["base"]),
            "--adblock-config", str(self.files["derived"]),
            *extra_args,
        ]
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", args))
            stack.enter_context(redirect_stdout(StringIO()))
            for name in (
                "MIN_RULE_COUNT", "AWAVENUE_MIN_RULE_COUNT", "ANTI_AD_MIN_RULE_COUNT"
            ):
                stack.enter_context(patch.object(sync, name, 1))
            self.johnshall_fetch = stack.enter_context(
                patch.object(
                    sync, "fetch_source", return_value=(source, "johnshall-etag")
                )
            )
            self.awavenue_fetch = stack.enter_context(
                patch.object(
                    sync,
                    "fetch_awavenue_source",
                    return_value=(AWAvenueRuleTests.SOURCE.encode(), "awa-etag"),
                )
            )
            self.anti_ad_fetch = stack.enter_context(
                patch.object(
                    sync,
                    "fetch_anti_ad_source",
                    return_value=(AntiAdRuleTests.SOURCE.encode(), "anti-etag"),
                )
            )
            self.write = stack.enter_context(
                patch.object(sync, "write_atomic", wraps=sync.write_atomic)
            )
            return sync.main()

    def assert_stopped_without_writes(self):
        self.write.assert_not_called()
        self.awavenue_fetch.assert_not_called()
        self.anti_ad_fetch.assert_not_called()
        self.assertEqual(self.snapshot(), self.initial)

    def test_date_rollback_stops_before_all_writes(self):
        with self.assertRaisesRegex(RuntimeError, "源日期倒退"):
            self.run_sync(self.source("2025-02-08 06:13:06"))
        self.assert_stopped_without_writes()

    def test_single_source_shrink_cannot_be_masked_or_overridden(self):
        source = self.source(count=6)
        # Raw duplicate rows must not inflate the unique source rule count.
        source += source.split(b"\n", 2)[2]
        with self.assertRaisesRegex(RuntimeError, "单源规则从 10 降至 6"):
            self.run_sync(source, "--allow-large-shrink")
        self.assert_stopped_without_writes()

    def test_bad_source_date_preserves_files(self):
        with self.assertRaisesRegex(RuntimeError, "源刷新时间格式异常"):
            self.run_sync(self.source("not-a-date"))
        self.assert_stopped_without_writes()

    def test_valid_boundary_update_records_baseline_and_is_idempotent(self):
        source = self.source(count=7)
        self.assertEqual(self.run_sync(source), 0)
        state = sync.load_johnshall_baseline(self.files["output"])
        self.assertEqual(state.rule_count, 7)
        self.assertEqual(state.updated_at, datetime(2026, 10, 25, 7))
        self.assertIn("DOMAIN,manual.example", self.files["custom"].read_text())
        self.assertEqual(self.files["exceptions"].read_bytes(), self.initial["exceptions"])
        self.assertEqual(self.files["base"].read_bytes(), self.initial["base"])
        self.assertEqual(
            self.files["derived"].read_text(),
            sync.render_adblock_config(self.files["base"].read_text()),
        )
        after_first_sync = self.snapshot()
        self.assertEqual(self.run_sync(source), 0)
        self.assertEqual(self.snapshot(), after_first_sync)

    def test_legacy_migration_rejects_rollback_before_any_writes(self):
        self.files["output"].write_text("# legacy output\nDOMAIN,old.example\n")
        self.initial = self.snapshot()
        baseline = self.files["output"].parent / "trusted.conf"
        baseline.write_bytes(self.source("2026-10-24 07:00:00"))
        with self.assertRaisesRegex(RuntimeError, "源日期倒退"):
            self.run_sync(
                self.source("2025-02-08 06:13:06", 6),
                "--johnshall-baseline", str(baseline),
            )
        self.assert_stopped_without_writes()

    def test_valid_legacy_migration_persists_metadata_for_next_run(self):
        self.files["output"].write_text("# legacy output\nDOMAIN,old.example\n")
        baseline = self.files["output"].parent / "trusted.conf"
        baseline.write_bytes(self.source("2026-10-24 07:00:00"))
        source = self.source()
        self.assertEqual(
            self.run_sync(source, "--johnshall-baseline", str(baseline)), 0
        )
        self.assertEqual(
            sync.load_johnshall_baseline(self.files["output"]).rule_count, 10
        )
        self.assertEqual(self.run_sync(source), 0)

    def test_missing_legacy_metadata_exits_nonzero_and_preserves_files(self):
        self.files["output"].write_text("# legacy output\nDOMAIN,old.example\n")
        before = self.snapshot()
        result = subprocess.run(
            [sys.executable, sync.__file__, "--output", str(self.files["output"])],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--johnshall-baseline", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.snapshot(), before)


class AWAvenueRuleTests(unittest.TestCase):
    SOURCE = """\
#Title: AWAvenue Ads Rule
#Total lines: 3
#Version: test-release
#Update time: 2026-07-14 12:00:00 UTC+8

DOMAIN,Ads.Example.com,reject
DOMAIN-SUFFIX,Tracker.Example,reject
DOMAIN-KEYWORD,-sponsor-,reject
"""

    def test_normalize_awavenue_rules_validates_metadata_and_rules(self):
        with patch.object(sync, "AWAVENUE_MIN_RULE_COUNT", 3):
            rules, metadata = sync.normalize_awavenue_rules(self.SOURCE)

        self.assertEqual(
            rules,
            [
                "DOMAIN,ads.example.com",
                "DOMAIN-SUFFIX,tracker.example",
                "DOMAIN-KEYWORD,-sponsor-",
            ],
        )
        self.assertEqual(metadata["version"], "test-release")

    def test_normalize_awavenue_rules_rejects_declared_count_mismatch(self):
        source = self.SOURCE.replace("#Total lines: 3", "#Total lines: 4")

        with patch.object(sync, "AWAVENUE_MIN_RULE_COUNT", 3):
            with self.assertRaisesRegex(RuntimeError, "规则数量与声明不符"):
                sync.normalize_awavenue_rules(source)

    def test_semantic_deduplication_uses_johnshall_suffixes_and_keywords(self):
        imported, covered = sync.deduplicate_rules(
            [
                "DOMAIN,exact.example",
                "DOMAIN-SUFFIX,example.com",
                "DOMAIN-KEYWORD,tracker",
                "DOMAIN-KEYWORD,-ad-",
            ],
            [
                "DOMAIN,exact.example",
                "DOMAIN,api.example.com",
                "DOMAIN-SUFFIX,sub.example.com",
                "DOMAIN,metrics-tracker.example",
                "DOMAIN-KEYWORD,-ad-network",
                "DOMAIN,keep.example",
                "DOMAIN-SUFFIX,keep.example.org",
            ],
        )

        self.assertEqual(
            imported,
            ["DOMAIN,keep.example", "DOMAIN-SUFFIX,keep.example.org"],
        )
        self.assertEqual(covered, 5)

    def test_replace_awavenue_block_preserves_manual_content(self):
        existing = "\n".join(
            [
                "# manual rules",
                "DOMAIN,manual.example",
                sync.AWAVENUE_BLOCK_BEGIN,
                "DOMAIN,old.example",
                sync.AWAVENUE_BLOCK_END,
                "# trailing comment",
                "",
            ]
        )
        replacement = "\n".join(
            [
                sync.AWAVENUE_BLOCK_BEGIN,
                "DOMAIN,new.example",
                sync.AWAVENUE_BLOCK_END,
            ]
        )

        result = sync.replace_awavenue_block(existing, replacement)

        self.assertIn("DOMAIN,manual.example", result)
        self.assertIn("DOMAIN,new.example", result)
        self.assertIn("# trailing comment", result)
        self.assertNotIn("DOMAIN,old.example", result)

    def test_replace_awavenue_block_requires_unique_markers(self):
        with self.assertRaisesRegex(RuntimeError, "起始标记"):
            sync.replace_awavenue_block("DOMAIN,manual.example\n", "block")

    def test_replace_awavenue_block_rejects_reversed_markers(self):
        existing = "\n".join(
            [sync.AWAVENUE_BLOCK_END, sync.AWAVENUE_BLOCK_BEGIN, ""]
        )

        with self.assertRaisesRegex(RuntimeError, "标记顺序异常"):
            sync.replace_awavenue_block(existing, "block")


class AntiAdRuleTests(unittest.TestCase):
    SOURCE = """\
#TITLE=anti-AD
#VER=20260717033840
#URL=https://github.com/privacy-protection-tools/anti-AD
#TOTAL_LINES=3
DOMAIN-SUFFIX,Ads.Example.com
DOMAIN-SUFFIX,Tracker.Example
DOMAIN-SUFFIX,ads.example.com
"""

    def test_normalize_anti_ad_rules_validates_metadata_and_deduplicates(self):
        with patch.object(sync, "ANTI_AD_MIN_RULE_COUNT", 2):
            rules, metadata = sync.normalize_anti_ad_rules(self.SOURCE)

        self.assertEqual(
            rules,
            [
                "DOMAIN-SUFFIX,ads.example.com",
                "DOMAIN-SUFFIX,tracker.example",
            ],
        )
        self.assertEqual(metadata["ver"], "20260717033840")

    def test_normalize_anti_ad_rules_rejects_declared_count_mismatch(self):
        source = self.SOURCE.replace("#TOTAL_LINES=3", "#TOTAL_LINES=4")

        with patch.object(sync, "ANTI_AD_MIN_RULE_COUNT", 2):
            with self.assertRaisesRegex(RuntimeError, "规则数量与声明不符"):
                sync.normalize_anti_ad_rules(source)

    def test_anti_ad_is_deduplicated_after_johnshall_and_awavenue(self):
        anti_ad_rules = [
            "DOMAIN-SUFFIX,ads.example.com",
            "DOMAIN-SUFFIX,sub.tracker.example",
            "DOMAIN-SUFFIX,keep.example",
        ]
        after_johnshall, johnshall_covered = sync.deduplicate_rules(
            ["DOMAIN-SUFFIX,example.com"], anti_ad_rules
        )
        imported, awavenue_covered = sync.deduplicate_rules(
            ["DOMAIN-SUFFIX,tracker.example"], after_johnshall
        )

        self.assertEqual(imported, ["DOMAIN-SUFFIX,keep.example"])
        self.assertEqual(johnshall_covered, 1)
        self.assertEqual(awavenue_covered, 1)

    def test_replace_anti_ad_block_preserves_other_content(self):
        existing = "\n".join(
            [
                sync.AWAVENUE_BLOCK_BEGIN,
                "DOMAIN,awavenue.example",
                sync.AWAVENUE_BLOCK_END,
                sync.ANTI_AD_BLOCK_BEGIN,
                "DOMAIN-SUFFIX,old.example",
                sync.ANTI_AD_BLOCK_END,
                "DOMAIN,manual.example",
                "",
            ]
        )
        replacement = "\n".join(
            [
                sync.ANTI_AD_BLOCK_BEGIN,
                "DOMAIN-SUFFIX,new.example",
                sync.ANTI_AD_BLOCK_END,
            ]
        )

        result = sync.replace_anti_ad_block(existing, replacement)

        self.assertIn("DOMAIN,awavenue.example", result)
        self.assertIn("DOMAIN-SUFFIX,new.example", result)
        self.assertIn("DOMAIN,manual.example", result)
        self.assertNotIn("DOMAIN-SUFFIX,old.example", result)


class DerivedConfigTests(unittest.TestCase):
    def test_eastmoney_direct_rules_remain_before_adblock(self):
        base = sync.DEFAULT_BASE_CONFIG.read_text(encoding="utf-8")
        result = sync.render_adblock_config(base)

        self.assertLess(
            result.index("DOMAIN-SUFFIX,eastmoney.com,DIRECT"),
            result.index(sync.ADBLOCK_RULE),
        )
        self.assertEqual(result.count(sync.ADBLOCK_RULE), 1)
        self.assertIn(
            "Eastmoney stock-data APIs intentionally remain DIRECT above this block.",
            result,
        )


if __name__ == "__main__":
    unittest.main()
