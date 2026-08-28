#!/usr/bin/env python3
"""Build a self-contained REJECT module for an all-DIRECT base config."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADBLOCK_LIST = REPO_ROOT / "rules" / "adblock.list"
DEFAULT_COMPLEMENT_MODULE = (
    REPO_ROOT / "modules" / "gmoogway-reject-complement.module"
)
DEFAULT_OUTPUT_MODULE = REPO_ROOT / "modules" / "adblock-only-combined.module"
HOMEPAGE = "https://github.com/buyunhao/shadowrocket-config"
BASE_CONFIG_URL = (
    "https://raw.githubusercontent.com/buyunhao/shadowrocket-config/"
    "main/shadowrocket_adblock-only.conf"
)
ALLOWED_ADBLOCK_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-KEYWORD",
    "DOMAIN-SUFFIX",
    "IP-CIDR",
    "IP-CIDR6",
    "USER-AGENT",
}
GMOOGWAY_SOURCE_RE = re.compile(r"\bSource:GMOogway@([0-9a-f]{40})\b")


@dataclass(frozen=True)
class Rule:
    key: str
    rule_type: str
    value: str
    output: str


@dataclass(frozen=True)
class BuildStats:
    adblock_rules: int
    complement_source_rules: int
    complement_retained_rules: int
    complement_covered_rules: int
    final_rules: int
    gmoogway_source_commit: str
    adblock_sha256: str
    complement_sha256: str


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_adblock_rules(content: str) -> list[Rule]:
    rules: list[Rule] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            raise RuntimeError(
                f"adblock.list 第 {line_number} 行不是完整规则：{line}"
            )

        rule_type = parts[0].upper()
        value = parts[1].lower()
        modifiers = [part.lower() for part in parts[2:] if part]

        if rule_type not in ALLOWED_ADBLOCK_RULE_TYPES:
            raise RuntimeError(
                f"adblock.list 第 {line_number} 行包含未知规则类型：{rule_type}"
            )
        if not value:
            raise RuntimeError(
                f"adblock.list 第 {line_number} 行缺少匹配值"
            )
        if any(modifier in {"direct", "proxy", "reject"} for modifier in modifiers):
            raise RuntimeError(
                f"adblock.list 第 {line_number} 行不应包含策略字段：{line}"
            )
        if modifiers and not (
            rule_type in {"IP-CIDR", "IP-CIDR6"}
            and modifiers == ["no-resolve"]
        ):
            raise RuntimeError(
                f"adblock.list 第 {line_number} 行包含未知修饰字段：{line}"
            )

        key = ",".join([rule_type, value, *modifiers])
        if key in seen:
            raise RuntimeError(f"adblock.list 包含重复规则：{key}")
        seen.add(key)

        output_parts = [rule_type, value, "REJECT", *modifiers]
        rules.append(
            Rule(
                key=key,
                rule_type=rule_type,
                value=value,
                output=",".join(output_parts),
            )
        )

    if not rules:
        raise RuntimeError("adblock.list 没有可用规则")
    return rules


def parse_complement_rules(content: str) -> tuple[list[Rule], str]:
    if "Filter:not fully covered and no DIRECT/PROXY overlap" not in content:
        raise RuntimeError(
            "GMOogway 补集缺少 DIRECT/PROXY 冲突过滤标记"
        )

    source_match = GMOOGWAY_SOURCE_RE.search(content)
    if not source_match:
        raise RuntimeError("GMOogway 补集缺少有效的上游提交记录")

    in_rule_section = False
    rules: list[Rule] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#!"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if line != "[Rule]":
                raise RuntimeError(
                    f"GMOogway 补集包含非 Rule 区块：{line}"
                )
            in_rule_section = True
            continue
        if not in_rule_section:
            raise RuntimeError(
                f"GMOogway 补集第 {line_number} 行位于 Rule 区块之外：{line}"
            )

        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            raise RuntimeError(
                f"GMOogway 补集第 {line_number} 行格式异常：{line}"
            )
        rule_type = parts[0].upper()
        value = parts[1].lower()
        policy = parts[2].upper()
        if rule_type != "DOMAIN-SUFFIX" or not value or policy != "REJECT":
            raise RuntimeError(
                f"GMOogway 补集第 {line_number} 行不是 DOMAIN-SUFFIX REJECT：{line}"
            )

        key = f"{rule_type},{value}"
        if key in seen:
            raise RuntimeError(f"GMOogway 补集包含重复规则：{key}")
        seen.add(key)
        rules.append(
            Rule(
                key=key,
                rule_type=rule_type,
                value=value,
                output=f"{rule_type},{value},REJECT",
            )
        )

    if not rules:
        raise RuntimeError("GMOogway 补集没有可用规则")
    return rules, source_match.group(1)


def suffix_is_covered(
    domain: str, suffixes: set[str], keywords: tuple[str, ...]
) -> bool:
    labels = domain.split(".")
    if any(".".join(labels[index:]) in suffixes for index in range(len(labels))):
        return True
    return any(keyword in domain for keyword in keywords)


def build_module(
    adblock_content: str, complement_content: str
) -> tuple[str, BuildStats]:
    adblock_rules = parse_adblock_rules(adblock_content)
    complement_rules, source_commit = parse_complement_rules(complement_content)

    adblock_suffixes = {
        rule.value
        for rule in adblock_rules
        if rule.rule_type == "DOMAIN-SUFFIX"
    }
    adblock_keywords = tuple(
        rule.value
        for rule in adblock_rules
        if rule.rule_type == "DOMAIN-KEYWORD"
    )

    retained_complement: list[Rule] = []
    covered = 0
    for rule in complement_rules:
        if suffix_is_covered(
            rule.value, adblock_suffixes, adblock_keywords
        ):
            covered += 1
        else:
            retained_complement.append(rule)

    all_outputs = [
        *(rule.output for rule in adblock_rules),
        *(rule.output for rule in retained_complement),
    ]
    if len(all_outputs) != len(set(all_outputs)):
        raise RuntimeError("组合模块仍包含精确重复规则")

    stats = BuildStats(
        adblock_rules=len(adblock_rules),
        complement_source_rules=len(complement_rules),
        complement_retained_rules=len(retained_complement),
        complement_covered_rules=covered,
        final_rules=len(all_outputs),
        gmoogway_source_commit=source_commit,
        adblock_sha256=sha256_text(adblock_content),
        complement_sha256=sha256_text(complement_content),
    )

    header = [
        "#!name=Adblock-only combined",
        f"#!homepage={HOMEPAGE}",
        (
            "#!desc="
            f"Rules:{stats.final_rules} "
            f"Adblock:{stats.adblock_rules} "
            f"GMOogway:{stats.complement_source_rules} "
            f"Retained:{stats.complement_retained_rules} "
            f"Covered:{stats.complement_covered_rules}"
        ),
        f"#!adblock-sha256={stats.adblock_sha256}",
        f"#!gmoogway-source={stats.gmoogway_source_commit}",
        f"#!gmoogway-module-sha256={stats.complement_sha256}",
        (
            "#!sources=Johnshall, AWAvenue, anti-AD, "
            "GMOogway/shadowrocket-rules"
        ),
        "#!licenses=CC-BY-SA-4.0, GPL-3.0, MIT, GPL-3.0",
        f"#!base-config={BASE_CONFIG_URL}",
        "#!policy=REJECT only; unmatched traffic stays direct through the base config",
        "[Rule]",
    ]
    return "\n".join([*header, *all_outputs, ""]), stats


def write_if_changed(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the all-DIRECT combined adblock module."
    )
    parser.add_argument(
        "--adblock-list", type=Path, default=DEFAULT_ADBLOCK_LIST
    )
    parser.add_argument(
        "--complement-module", type=Path, default=DEFAULT_COMPLEMENT_MODULE
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_MODULE
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the generated output is missing or stale.",
    )
    args = parser.parse_args()

    adblock_content = args.adblock_list.read_text(encoding="utf-8")
    complement_content = args.complement_module.read_text(encoding="utf-8")
    output, stats = build_module(adblock_content, complement_content)

    if args.check:
        if not args.output.exists():
            raise RuntimeError(f"组合模块不存在：{args.output}")
        if args.output.read_text(encoding="utf-8") != output:
            raise RuntimeError(f"组合模块不是当前源文件的生成结果：{args.output}")
        changed = False
    else:
        changed = write_if_changed(args.output, output)

    print(f"组合模块：{args.output} changed={str(changed).lower()}")
    print(
        "规则统计："
        f"三源 {stats.adblock_rules} 条；"
        f"GMOogway 输入 {stats.complement_source_rules} 条；"
        f"被三源覆盖 {stats.complement_covered_rules} 条；"
        f"保留补集 {stats.complement_retained_rules} 条；"
        f"最终 {stats.final_rules} 条"
    )
    print(f"GMOogway 上游：{stats.gmoogway_source_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
