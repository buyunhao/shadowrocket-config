from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
