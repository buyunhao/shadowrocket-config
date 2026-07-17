from pathlib import Path
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
