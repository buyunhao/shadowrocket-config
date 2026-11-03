#!/usr/bin/env python3
"""Independently reconstruct a complement using sorted DNS-label indexes."""

import argparse
import bisect
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

FILES = ('rules/adblock.list', 'shadowrocket_gpt_maintain.conf',
         'shadowrocket_gpt_maintain-mobile.conf', 'shadowrocket_gpt_maintain-mobile-adblock.conf')
MODULE = 'modules/gmoogway-reject-complement.module'


def read_rules(text, flavor):
    table = {'DOMAIN': set(), 'DOMAIN-SUFFIX': set(), 'DOMAIN-KEYWORD': set()}
    rows = []
    section = flavor == 'list'
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('['):
            section = line.casefold() == '[rule]'
            continue
        if not section:
            if flavor == 'source':
                raise ValueError('源规则出现在 [Rule] 外')
            continue
        cells = [part.strip() for part in line.split(',')]
        typ = cells[0].upper()
        if typ not in table:
            if flavor != 'source' and typ in ('IP-CIDR', 'IP-CIDR6', 'GEOIP', 'USER-AGENT', 'FINAL', 'RULE-SET'):
                continue
            raise ValueError('未知规则: ' + line)
        if len(cells) != (2 if flavor == 'list' else 3):
            raise ValueError('字段格式错误: ' + line)
        value = cells[1].lower().rstrip('.')
        if not re.fullmatch('[a-z0-9_.-]+', value) or '..' in value:
            raise ValueError('不支持的域名: ' + line)
        policy = 'REJECT' if flavor == 'list' else cells[2].upper()
        if flavor == 'source' and (typ, policy) != ('DOMAIN-SUFFIX', 'REJECT'):
            raise ValueError('不支持的源规则: ' + line)
        table[typ].add(value)
        rows.append((typ, value, policy))
    if not rows:
        raise ValueError('没有域名规则')
    return table, rows


def reverse_labels(domain):
    return tuple(domain.split('.')[::-1])


def has_child(index, domain):
    prefix = reverse_labels(domain)
    pos = bisect.bisect_left(index, prefix)
    return pos < len(index) and index[pos][:len(prefix)] == prefix


def has_parent(domains, domain):
    labels = domain.split('.')
    return any('.'.join(labels[i:]) in domains for i in range(len(labels)))


def keyword_intersects(suffix, keyword):
    if keyword in suffix:
        return True
    # Place the keyword completely before, or across, the final separator.
    # Recheck each actual hostname as an independent witness of intersection.
    before = {keyword}
    ending = '.' + suffix
    for cut in range(len(keyword)):
        if ending.startswith(keyword[cut:]):
            before.add(keyword[:cut])
    for prefix in before:
        for l in ('', 'x'):
            for r in ('', 'x'):
                host = l + prefix + r + ending
                labels = host.split('.')
                if (len(host) <= 253 and all(1 <= len(x) <= 63 for x in labels)
                        and all(re.fullmatch('[a-z0-9_][a-z0-9_-]*', x) and not x.endswith('-') for x in labels)):
                    return True
    return False


def reconstruct(source, adblock, route_rows):
    suffixes = adblock['DOMAIN-SUFFIX']
    suffix_index = sorted(map(reverse_labels, suffixes))
    exact_index = sorted(map(reverse_labels, adblock['DOMAIN']))
    keywords = adblock['DOMAIN-KEYWORD']
    routes = {}
    for policy in ('DIRECT', 'PROXY'):
        suffix = {d for t, d, a in route_rows if a == policy and t == 'DOMAIN-SUFFIX'}
        exact = {d for t, d, a in route_rows if a == policy and t == 'DOMAIN'}
        kw = {d for t, d, a in route_rows if a == policy and t == 'DOMAIN-KEYWORD'}
        routes[policy] = (suffix, sorted(map(reverse_labels, suffix)), sorted(map(reverse_labels, exact)), kw)

    def conflicts(domain, policy):
        s, si, ei, k = routes[policy]
        return (has_parent(s, domain) or has_child(si, domain) or has_child(ei, domain)
                or any(keyword_intersects(domain, x) for x in k))

    full, partial, missing, direct, proxy, expected = set(), set(), set(), set(), set(), set()
    for domain in source:
        if has_parent(suffixes, domain) or any(k in domain for k in keywords):
            full.add(domain)
            continue
        if (has_child(suffix_index, domain) or has_child(exact_index, domain)
                or any(keyword_intersects(domain, x) for x in keywords)):
            partial.add(domain)
        else:
            missing.add(domain)
        if conflicts(domain, 'DIRECT'):
            direct.add(domain)
        if conflicts(domain, 'PROXY'):
            proxy.add(domain)
        if domain not in direct and domain not in proxy:
            expected.add(domain)
    stats = dict(source=len(source), covered=len(full), partial=len(partial), missing=len(missing),
                 direct=len(direct), proxy=len(proxy), both=len(direct & proxy), final=len(expected))
    return expected, stats, full, direct, proxy


def verify(repo, source_path, candidate, source_commit, baseline_commit, generated_date):
    if not all(re.fullmatch('[0-9a-f]{40}', c) for c in (source_commit, baseline_commit)):
        raise ValueError('提交号必须是40位小写十六进制')
    date.fromisoformat(generated_date)
    snapshots = {}
    for name in FILES:
        snapshots[name] = (repo / name).read_bytes()
        committed = subprocess.check_output(['git', 'show', baseline_commit + ':' + name], cwd=repo)
        if snapshots[name] != committed:
            raise ValueError('比较基线已变化: ' + name)
    raw_source = source_path.read_bytes()
    source_table, _ = read_rules(raw_source.decode(), 'source')
    ad, _ = read_rules(snapshots[FILES[0]].decode(), 'list')
    routes = [row for name in FILES[1:] for row in read_rules(snapshots[name].decode(), 'config')[1]]
    expected, stats, covered, direct, proxy = reconstruct(source_table['DOMAIN-SUFFIX'], ad, routes)
    raw = candidate.read_bytes()
    lines = raw.decode().splitlines()
    if not raw.endswith(b'\n') or any(line != line.rstrip() for line in lines):
        raise ValueError('模块缺少末尾换行或含行尾空白')
    if lines.count('[Rule]') != 1:
        raise ValueError('模块必须且只能有一个 [Rule]')
    actual = []
    in_rule = False
    for line in lines:
        if line == '[Rule]':
            in_rule = True
        elif line and not line.startswith('#'):
            match = re.fullmatch('DOMAIN-SUFFIX,([a-z0-9_.-]+),REJECT', line)
            if not in_rule or not match:
                raise ValueError('模块规则格式错误: ' + line)
            actual.append(match[1])
    if actual != sorted(set(actual)):
        raise ValueError('模块未排序或有重复规则')
    extra, absent = set(actual) - expected, expected - set(actual)
    if extra or absent:
        raise ValueError(f'模块集合不一致: 多出{len(extra)} 缺失{len(absent)}; {sorted(extra)[:3]} / {sorted(absent)[:3]}')
    text = raw.decode()
    source_sha = hashlib.sha256(raw_source).hexdigest()
    expected_desc = (f'#!desc=Rules:{len(actual)} Created:{generated_date} Source:GMOogway@{source_commit} '
                     f'Local@{baseline_commit} Filter:not fully covered and no DIRECT/PROXY overlap')
    source_url = f'https://raw.githubusercontent.com/GMOogway/shadowrocket-rules/{source_commit}/sr_reject_list.module'
    if (expected_desc not in lines or '# Source URL: ' + source_url not in lines
            or '# Source SHA-256: ' + source_sha not in lines
            or '#!name=GMOogway REJECT complement' not in lines):
        raise ValueError('模块元数据不一致')
    stats.update(extra=len(extra), absent=len(absent), duplicates=len(actual) - len(set(actual)),
                 covered_remaining=len(set(actual) & covered), direct_remaining=len(set(actual) & direct),
                 proxy_remaining=len(set(actual) & proxy), source_sha256=source_sha,
                 module_sha256=hashlib.sha256(raw).hexdigest(),
                 baseline_sha256={name: hashlib.sha256(data).hexdigest() for name, data in snapshots.items()})
    return raw, stats


def install(repo, raw):
    target = repo / MODULE
    if target.read_bytes() == raw:
        return False
    descriptor, name = tempfile.mkstemp(prefix='.gmoogway-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, target.stat().st_mode & 0o777)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--baseline-commit', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--module', type=Path, required=True)
    parser.add_argument('--stats', type=Path, required=True)
    parser.add_argument('--install', action='store_true', help='Atomically install only after independent verification')
    args = parser.parse_args()
    protected = {args.source.resolve(), args.module.resolve()} | {(args.repo / p).resolve() for p in (*FILES, MODULE)}
    if args.stats.resolve() in protected:
        raise ValueError('统计输出不可覆盖输入或正式产物')
    raw, stats = verify(args.repo, args.source, args.module, args.source_commit, args.baseline_commit, args.date)
    # Write the validation report before changing the target, so IO errors leave it intact.
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.install:
        for name, digest in stats['baseline_sha256'].items():
            if hashlib.sha256((args.repo / name).read_bytes()).hexdigest() != digest:
                raise ValueError('验证期间基线变化，停止安装: ' + name)
        original = subprocess.check_output(['git', 'show', args.baseline_commit + ':' + MODULE], cwd=args.repo)
        if (args.repo / MODULE).read_bytes() != original:
            raise ValueError('正式模块与阶段起始版本不同，停止覆盖')
        stats['installed_changed'] = install(args.repo, raw)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
