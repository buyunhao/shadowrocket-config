from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import build_adblock_only_module as build


class CombinedModuleTests(unittest.TestCase):
    def test_build_module_applies_policy_and_semantic_coverage(self):
        adblock = """\
# generated
DOMAIN-SUFFIX,example.com
DOMAIN-KEYWORD,tracker
DOMAIN,exact-only.example
IP-CIDR,192.0.2.0/24,no-resolve
"""
        complement = """\
#!name=test
#!desc=Source:GMOogway@0123456789abcdef0123456789abcdef01234567 Filter:not fully covered and no DIRECT/PROXY overlap
[Rule]
DOMAIN-SUFFIX,sub.example.com,REJECT
DOMAIN-SUFFIX,metrics-tracker.test,REJECT
DOMAIN-SUFFIX,exact-only.example,REJECT
DOMAIN-SUFFIX,keep.example,REJECT
"""

        output, stats = build.build_module(adblock, complement)

        self.assertEqual(stats.adblock_rules, 4)
        self.assertEqual(stats.complement_source_rules, 4)
        self.assertEqual(stats.complement_covered_rules, 2)
        self.assertEqual(stats.complement_retained_rules, 2)
        self.assertEqual(stats.final_rules, 6)
        self.assertIn("DOMAIN-SUFFIX,example.com,REJECT", output)
        self.assertIn("IP-CIDR,192.0.2.0/24,REJECT,no-resolve", output)
        self.assertIn("DOMAIN-SUFFIX,exact-only.example,REJECT", output)
        self.assertIn("DOMAIN-SUFFIX,keep.example,REJECT", output)
        self.assertNotIn("DOMAIN-SUFFIX,sub.example.com,REJECT", output)
        self.assertNotIn("DOMAIN-SUFFIX,metrics-tracker.test,REJECT", output)
        rule_lines = output.split("[Rule]\n", maxsplit=1)[1].splitlines()
        self.assertFalse(any(",PROXY" in line for line in rule_lines))
        self.assertFalse(any(",DIRECT" in line for line in rule_lines))

    def test_adblock_input_rejects_policy_fields(self):
        with self.assertRaisesRegex(RuntimeError, "不应包含策略字段"):
            build.parse_adblock_rules("DOMAIN,ads.example,REJECT\n")

    def test_complement_requires_conflict_filter_marker(self):
        complement = """\
#!name=test
#!desc=Source:GMOogway@0123456789abcdef0123456789abcdef01234567
[Rule]
DOMAIN-SUFFIX,ads.example,REJECT
"""
        with self.assertRaisesRegex(RuntimeError, "冲突过滤标记"):
            build.parse_complement_rules(complement)

    def test_write_if_changed_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "module"
            self.assertTrue(build.write_if_changed(path, "content\n"))
            self.assertFalse(build.write_if_changed(path, "content\n"))

    def test_repository_module_matches_current_sources(self):
        adblock = build.DEFAULT_ADBLOCK_LIST.read_text(encoding="utf-8")
        complement = build.DEFAULT_COMPLEMENT_MODULE.read_text(encoding="utf-8")
        expected, stats = build.build_module(adblock, complement)
        actual = build.DEFAULT_OUTPUT_MODULE.read_text(encoding="utf-8")

        self.assertEqual(actual, expected)
        rule_lines = [
            line
            for line in actual.splitlines()
            if line and not line.startswith("#!") and line != "[Rule]"
        ]
        self.assertEqual(len(rule_lines), stats.final_rules)
        self.assertEqual(len(rule_lines), len(set(rule_lines)))


class AdblockOnlyConfigTests(unittest.TestCase):
    def test_base_config_has_direct_fallback_and_no_proxy_policy(self):
        config = (
            build.REPO_ROOT / "shadowrocket_adblock-only.conf"
        ).read_text(encoding="utf-8")
        rule_lines = [
            line.strip()
            for line in config.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertIn("FINAL,DIRECT", rule_lines)
        self.assertNotIn("FINAL,PROXY", rule_lines)
        self.assertFalse(any(",PROXY" in line for line in rule_lines))
        self.assertFalse(any(line.startswith("[Proxy") for line in rule_lines))
        self.assertNotIn("[MITM]", rule_lines)
        self.assertNotIn("[Script]", rule_lines)
        self.assertNotIn("[URL Rewrite]", rule_lines)


if __name__ == "__main__":
    unittest.main()
