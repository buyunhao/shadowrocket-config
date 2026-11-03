#!/usr/bin/env python3
"""Build an offline candidate; publication is deliberately handled by the caller."""

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess

BASELINES = (
    'rules/adblock.list', 'shadowrocket_gpt_maintain.conf',
    'shadowrocket_gpt_maintain-mobile.conf',
    'shadowrocket_gpt_maintain-mobile-adblock.conf',
)
TARGET = 'modules/gmoogway-reject-complement.module'
TYPES = {'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD'}


class Trie:
    def __init__(self, domains=()):
        self.root = {}
        for domain in domains:
            node = self.root
            for label in reversed(domain.split('.')):
                node = node.setdefault(label, {})
            node[''] = True

    def above(self, domain):
        node = self.root
        for label in reversed(domain.split('.')):
            if label not in node:
                return False
            node = node[label]
            if '' in node:
                return True
        return False

    def below(self, domain):
        node = self.root
        for label in reversed(domain.split('.')):
            if label not in node:
                return False
            node = node[label]
        return bool(node)


def keyword_overlap(keyword, domain):
    """Existence of a hostname in the suffix scope containing the keyword.

    Try a whole keyword in a child prefix, and all placements crossing the
    separator before the fixed suffix. Validate hostname and DNS length limits.
    """
    if keyword in domain:
        return True
    tail = '.' + domain
    prefixes = [keyword]
    for size in range(1, min(len(keyword), len(tail)) + 1):
        if keyword.endswith(tail[:size]):
            prefixes.append(keyword[:-size])
    for prefix in prefixes:
        for left, right in (('', ''), ('x', ''), ('', 'x'), ('x', 'x')):
            host = left + prefix + right + tail
            if len(host) <= 253 and all(
                re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', label)
                for label in host.split('.')
            ):
                return True
    return False


def parse(content, kind):
    result = []
    in_rule = kind == 'adblock'
    for line_number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            in_rule = line.lower() == '[rule]'
            continue
        if not in_rule:
            if kind == 'source':
                raise ValueError(f'源规则不在 [Rule] 内: {line_number}')
            continue
        parts = [p.strip() for p in line.split(',')]
        typ = parts[0].upper()
        if typ not in TYPES:
            if kind == 'source':
                raise ValueError(f'不支持的源规则: {line}')
            if typ in {'IP-CIDR', 'IP-CIDR6', 'GEOIP', 'USER-AGENT', 'FINAL', 'RULE-SET'}:
                continue
            raise ValueError(f'未知规则类型: {line}')
        if len(parts) != (2 if kind == 'adblock' else 3):
            raise ValueError(f'域名规则字段数错误: {line}')
        domain = parts[1].lower().rstrip('.')
        if not domain or not re.fullmatch(r'[a-z0-9_.-]+', domain) or '..' in domain:
            raise ValueError(f'不支持的域名/关键词: {line}')
        action = 'REJECT' if kind == 'adblock' else parts[2].upper()
        if kind == 'source' and (typ != 'DOMAIN-SUFFIX' or action != 'REJECT'):
            raise ValueError(f'源文件仅允许 DOMAIN-SUFFIX + REJECT: {line}')
        result.append((typ, domain, action))
    if not result:
        raise ValueError(f'{kind} 没有域名规则')
    return result


def select(source, adblock, routes):
    suffix = Trie(d for t, d, a in adblock if t == 'DOMAIN-SUFFIX')
    exact = Trie(d for t, d, a in adblock if t == 'DOMAIN')
    keywords = {d for t, d, a in adblock if t == 'DOMAIN-KEYWORD'}
    indexes = {}
    for action in ('DIRECT', 'PROXY'):
        rows = {(t, d) for t, d, a in routes if a == action}
        indexes[action] = (
            Trie(d for t, d in rows if t == 'DOMAIN-SUFFIX'),
            Trie(d for t, d in rows if t == 'DOMAIN'),
            {d for t, d in rows if t == 'DOMAIN-KEYWORD'},
        )

    def conflict(domain, action):
        s, e, k = indexes[action]
        return (s.above(domain) or s.below(domain) or e.below(domain)
                or any(keyword_overlap(x, domain) for x in k))

    full, partial, missing, direct, proxy, kept = set(), set(), set(), set(), set(), set()
    for domain in source:
        if suffix.above(domain) or any(k in domain for k in keywords):
            full.add(domain)
            continue
        if (suffix.below(domain) or exact.below(domain)
                or any(keyword_overlap(k, domain) for k in keywords)):
            partial.add(domain)
        else:
            missing.add(domain)
        if conflict(domain, 'DIRECT'):
            direct.add(domain)
        if conflict(domain, 'PROXY'):
            proxy.add(domain)
        if domain not in direct and domain not in proxy:
            kept.add(domain)
    stats = dict(source=len(source), covered=len(full), partial=len(partial), missing=len(missing),
                 direct=len(direct), proxy=len(proxy), both=len(direct & proxy), final=len(kept))
    return sorted(kept), stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--baseline-commit', required=True)
    parser.add_argument('--date', required=True, help='Asia/Taipei YYYY-MM-DD')
    parser.add_argument('--output', type=Path, required=True, help='Temporary candidate, not the published target')
    parser.add_argument('--stats', type=Path, required=True)
    args = parser.parse_args()
    if not all(re.fullmatch(r'[0-9a-f]{40}', x) for x in (args.source_commit, args.baseline_commit)):
        raise ValueError('提交号必须为40位小写十六进制')
    date.fromisoformat(args.date)
    protected = {args.source.resolve()} | { (args.repo / p).resolve() for p in (*BASELINES, TARGET)}
    if args.output.resolve() in protected or args.stats.resolve() in protected or args.output.resolve() == args.stats.resolve():
        raise ValueError('候选/统计输出不可覆盖源文件、比较基线或正式模块')
    snapshots = {}
    for name in BASELINES:
        data = (args.repo / name).read_bytes()
        committed = subprocess.check_output(['git', 'show', args.baseline_commit + ':' + name], cwd=args.repo)
        if data != committed:
            raise ValueError('工作区与指定基线不同: ' + name)
        snapshots[name] = data
    raw = args.source.read_bytes()
    source = {d for t, d, a in parse(raw.decode(), 'source')}
    adblock = parse(snapshots[BASELINES[0]].decode(), 'adblock')
    routes = [rule for name in BASELINES[1:] for rule in parse(snapshots[name].decode(), 'config')]
    kept, stats = select(source, adblock, routes)
    sha = hashlib.sha256(raw).hexdigest()
    url = f'https://raw.githubusercontent.com/GMOogway/shadowrocket-rules/{args.source_commit}/sr_reject_list.module'
    header = ('#!name=GMOogway REJECT complement\n'
              '#!homepage=https://github.com/GMOogway/shadowrocket-rules\n'
              f'#!desc=Rules:{len(kept)} Created:{args.date} Source:GMOogway@{args.source_commit} '
              f'Local@{args.baseline_commit} Filter:not fully covered and no DIRECT/PROXY overlap\n'
              f'# Source URL: {url}\n# Source SHA-256: {sha}\n[Rule]\n')
    candidate = header + ''.join(f'DOMAIN-SUFFIX,{d},REJECT\n' for d in kept)
    stats.update(source_commit=args.source_commit, baseline_commit=args.baseline_commit,
                 source_url=url, source_sha256=sha,
                 baseline_sha256={name: hashlib.sha256(data).hexdigest() for name, data in snapshots.items()},
                 module_sha256=hashlib.sha256(candidate.encode()).hexdigest())
    args.output.write_text(candidate, encoding='utf-8')
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
