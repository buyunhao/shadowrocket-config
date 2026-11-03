import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import build_gmoogway_complement as build
from scripts import verify_gmoogway_complement as verify


class SemanticsTests(unittest.TestCase):
    def check(self, source, adblock, routes, expected):
        kept, stats = build.select(set(source), adblock, routes)
        table = {typ: {d for t, d, a in adblock if t == typ} for typ in build.TYPES}
        independent, independent_stats, *_ = verify.reconstruct(set(source), table, routes)
        self.assertEqual(set(expected), set(kept))
        self.assertEqual(set(expected), independent)
        self.assertEqual(stats, independent_stats)
        return stats

    def test_two_field_adblock_and_three_field_source(self):
        self.assertEqual(build.parse('DOMAIN-SUFFIX,example.com\n', 'adblock'),
                         [('DOMAIN-SUFFIX', 'example.com', 'REJECT')])
        self.assertEqual(verify.read_rules('DOMAIN,ads.example.com\n', 'list')[0]['DOMAIN'],
                         {'ads.example.com'})
        source = '[Rule]\nDOMAIN-SUFFIX,example.com,REJECT\n'
        self.assertEqual(len(build.parse(source, 'source')), 1)
        self.assertEqual(len(verify.read_rules(source, 'source')[1]), 1)

    def test_source_unknown_or_outside_rule_section_is_rejected(self):
        for text in ('DOMAIN-SUFFIX,x.com,REJECT\n', '[Rule]\nDOMAIN,x.com,REJECT\n',
                     '[Rule]\nIP-CIDR,192.0.2.0/24,REJECT\n'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    build.parse(text, 'source')
                with self.assertRaises(ValueError):
                    verify.read_rules(text, 'source')

    def test_parent_covers_source_with_dns_boundary(self):
        self.check(['example.com', 'child.example.com', 'badexample.com'],
                   [('DOMAIN-SUFFIX', 'example.com', 'REJECT')], [], ['badexample.com'])

    def test_partial_overlap_keeps_wider_source(self):
        stats = self.check(['example.com'], [('DOMAIN-SUFFIX', 'child.example.com', 'REJECT')], [], ['example.com'])
        self.assertEqual((stats['partial'], stats['covered']), (1, 0))

    def test_exact_domain_is_only_partial_suffix_coverage(self):
        stats = self.check(['example.com'], [('DOMAIN', 'example.com', 'REJECT')], [], ['example.com'])
        self.assertEqual(stats['partial'], 1)

    def test_narrower_exact_host_is_partial(self):
        stats = self.check(['example.com'], [('DOMAIN', 'a.example.com', 'REJECT')], [], ['example.com'])
        self.assertEqual(stats['partial'], 1)

    def test_string_prefix_is_not_a_child_domain(self):
        stats = self.check(['metrics.abbott'], [('DOMAIN-SUFFIX', 'metrics.abbott.com', 'REJECT')], [], ['metrics.abbott'])
        self.assertEqual(stats['missing'], 1)

    def test_keyword_contained_in_fixed_suffix_fully_covers_it(self):
        stats = self.check(['x-ad.sm.cn', 'news.example.com'], [('DOMAIN-KEYWORD', '-ad.sm.cn', 'REJECT')], [], ['news.example.com'])
        self.assertEqual((stats['covered'], stats['partial'], stats['missing']), (1, 1, 0))

    def test_keyword_route_can_intersect_through_child_prefix(self):
        self.check(['example.com'], [], [('DOMAIN-KEYWORD', 'ads', 'PROXY')], [])

    def test_suffix_routes_exclude_parent_and_child_but_not_false_suffix(self):
        self.check(['example.com', 'a.child.example.com', 'badexample.com'], [],
                   [('DOMAIN-SUFFIX', 'child.example.com', 'DIRECT')], ['badexample.com'])

    def test_exact_route_only_intersects_source_at_or_above_exact_host(self):
        self.check(['example.com', 'a.example.com', 'child.a.example.com'], [],
                   [('DOMAIN', 'a.example.com', 'DIRECT')], ['child.a.example.com'])

    def test_route_conflict_counts_are_separate_and_before_exclusion(self):
        stats = self.check(['example.com'], [], [('DOMAIN-SUFFIX', 'a.example.com', 'DIRECT'),
                                                ('DOMAIN', 'b.example.com', 'PROXY')], [])
        self.assertEqual((stats['direct'], stats['proxy'], stats['both'], stats['missing']), (1, 1, 1, 1))

    def test_source_and_baseline_duplicates_do_not_change_result(self):
        self.check(['example.com', 'example.com'], [('DOMAIN', 'x.com', 'REJECT')] * 2, [], ['example.com'])


class VerificationTests(unittest.TestCase):
    def test_verifier_rejects_format_metadata_and_rule_set_corruption(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in verify.FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('DOMAIN-SUFFIX,ads.example.com\n' if name.endswith('.list')
                                else '[Rule]\nDOMAIN-SUFFIX,safe.example.net,DIRECT\n')
            source = root / 'source.module'
            source.write_text('[Rule]\nDOMAIN-SUFFIX,example.com,REJECT\n')
            candidate = root / 'candidate.module'
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            content = ('#!name=GMOogway REJECT complement\n'
                       '#!desc=Rules:1 Created:2026-11-03 Source:GMOogway@' + 'a' * 40
                       + ' Local@' + 'b' * 40 + ' Filter:not fully covered and no DIRECT/PROXY overlap\n'
                       '# Source URL: https://raw.githubusercontent.com/GMOogway/shadowrocket-rules/'
                       + 'a' * 40 + '/sr_reject_list.module\n# Source SHA-256: ' + digest
                       + '\n[Rule]\nDOMAIN-SUFFIX,example.com,REJECT\n')
            def committed(argv, **kwargs):
                return (root / argv[-1].split(':', 1)[1]).read_bytes()
            with patch.object(verify.subprocess, 'check_output', side_effect=committed):
                candidate.write_text(content)
                self.assertEqual(verify.verify(root, source, candidate, 'a' * 40, 'b' * 40, '2026-11-03')[1]['final'], 1)
                for changed in (content.rstrip('\n'), content + 'DOMAIN-SUFFIX,example.com,REJECT\n',
                                content.replace('REJECT\n', 'DIRECT\n'), content.replace('Rules:1', 'Rules:2'),
                                content.replace('example.com,REJECT', 'example.com,REJECT ')):
                    with self.subTest(candidate=changed):
                        candidate.write_text(changed)
                        with self.assertRaises(ValueError):
                            verify.verify(root, source, candidate, 'a' * 40, 'b' * 40, '2026-11-03')

    def test_invalid_candidate_never_changes_formal_module(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in verify.FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('DOMAIN-SUFFIX,ads.example.com\n' if name.endswith('.list')
                                else '[Rule]\nDOMAIN-SUFFIX,safe.example.net,DIRECT\n')
            source = root / 'source.module'
            source.write_text('[Rule]\nDOMAIN-SUFFIX,example.com,REJECT\n')
            target = root / verify.MODULE
            target.parent.mkdir()
            target.write_bytes(b'original module\n')
            candidate = root / 'candidate.module'
            candidate.write_text('[Rule]\nDOMAIN-SUFFIX,ads.example.com,REJECT\n')
            def committed(argv, **kwargs):
                return (root / argv[-1].split(':', 1)[1]).read_bytes()
            with patch.object(verify.subprocess, 'check_output', side_effect=committed):
                with self.assertRaisesRegex(ValueError, '集合不一致'):
                    verify.verify(root, source, candidate, 'a' * 40, 'b' * 40, '2026-11-03')
            self.assertEqual(target.read_bytes(), b'original module\n')

    def test_install_is_atomic_and_idempotent(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / verify.MODULE
            target.parent.mkdir()
            target.write_bytes(b'old\n')
            self.assertTrue(verify.install(root, b'new\n'))
            self.assertEqual(target.read_bytes(), b'new\n')
            self.assertFalse(verify.install(root, b'new\n'))
            self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
