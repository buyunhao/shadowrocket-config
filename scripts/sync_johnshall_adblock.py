#!/usr/bin/env python3
"""Refresh upstream ad rules and rebuild the derived mobile config.

The generated list is the normalized Johnshall upstream plus an AWAvenue
complement and local custom rules, minus exactly matching local exceptions.
Only the marked AWAvenue block in the custom list is rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path


SOURCE_URL = (
    "https://raw.githubusercontent.com/Johnshall/"
    "Shadowrocket-ADBlock-Rules-Forever/release/sr_ad_only.conf"
)
SOURCE_LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"
AWAVENUE_SOURCE_URL = (
    "https://raw.githubusercontent.com/TG-Twilight/"
    "AWAvenue-Ads-Rule/main/Filters/AWAvenue-Ads-Rule-QuantumultX.list"
)
AWAVENUE_SOURCE_PROJECT = "https://github.com/TG-Twilight/AWAvenue-Ads-Rule"
AWAVENUE_SOURCE_LICENSE = (
    "https://github.com/TG-Twilight/AWAvenue-Ads-Rule/blob/main/LICENSE"
)
CHUNK_SIZE = 512 * 1024
MIN_RULE_COUNT = 10_000
AWAVENUE_MIN_RULE_COUNT = 500
ALLOWED_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-KEYWORD",
    "DOMAIN-SUFFIX",
    "IP-CIDR",
    "IP-CIDR6",
}
AWAVENUE_ALLOWED_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-KEYWORD",
    "DOMAIN-SUFFIX",
}
USER_AGENT = "shadowrocket-config-adblock-sync/1.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIST = REPO_ROOT / "rules" / "adblock.list"
DEFAULT_CUSTOM_LIST = REPO_ROOT / "rules" / "adblock-custom.list"
DEFAULT_EXCEPTIONS_LIST = REPO_ROOT / "rules" / "adblock-exceptions.list"
DEFAULT_BASE_CONFIG = REPO_ROOT / "shadowrocket_gpt_maintain-mobile.conf"
DEFAULT_ADBLOCK_CONFIG = REPO_ROOT / "shadowrocket_gpt_maintain-mobile-adblock.conf"
ADBLOCK_RULE_URL = (
    "https://raw.githubusercontent.com/buyunhao/shadowrocket-config/"
    "main/rules/adblock.list"
)
ADBLOCK_RULE = f"RULE-SET,{ADBLOCK_RULE_URL},REJECT"
AWAVENUE_BLOCK_BEGIN = "# BEGIN AUTO-GENERATED AWAvenue RULES"
AWAVENUE_BLOCK_END = "# END AUTO-GENERATED AWAvenue RULES"
BILIBILI_HEADING = "# 3. Bilibili direct: keep domestic CDN fast."
DOMESTIC_HEADING = (
    "# 4. Main domestic services direct. "
    "Keep explicit app/domain rules above the mobile GEOIP fallback."
)
FALLBACK_HEADING = "# 5. Mobile-friendly fallback."
ADBLOCK_BLOCK = [
    "# 3. Ad blocking: earlier account, LAN, and explicitly preserved data-service rules take precedence.",
    "# Eastmoney stock-data APIs intentionally remain DIRECT above this block.",
    "# The generated list combines Johnshall, the AWAvenue complement, local custom rules, and exact exceptions.",
    ADBLOCK_RULE,
    "",
]
HEADING_REPLACEMENTS = {
    BILIBILI_HEADING: "# 4. Bilibili direct: keep domestic CDN fast.",
    DOMESTIC_HEADING: DOMESTIC_HEADING.replace("# 4.", "# 5."),
    FALLBACK_HEADING: "# 6. Mobile-friendly fallback.",
}


def request(url: str, *, method: str = "GET", byte_range: str | None = None):
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if byte_range:
        headers["Range"] = byte_range
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers, method=method), timeout=60
    )


def fetch_source() -> tuple[bytes, str]:
    """Download the complete source with ranged requests and size validation."""
    with request(SOURCE_URL, method="HEAD") as response:
        content_length = response.headers.get("Content-Length")
        etag = response.headers.get("ETag", "").strip('"')

    if not content_length or not content_length.isdigit():
        raise RuntimeError("上游响应缺少有效的 Content-Length，已停止同步")

    total_size = int(content_length)
    content = bytearray()

    for start in range(0, total_size, CHUNK_SIZE):
        end = min(start + CHUNK_SIZE - 1, total_size - 1)
        expected_range = f"bytes {start}-{end}/{total_size}"
        with request(SOURCE_URL, byte_range=f"bytes={start}-{end}") as response:
            chunk = response.read()
            content_range = response.headers.get("Content-Range", "")

        if content_range != expected_range:
            raise RuntimeError(
                f"上游分段响应异常：期望 {expected_range}，实际 {content_range or '缺失'}"
            )
        if len(chunk) != end - start + 1:
            raise RuntimeError(
                f"上游分段下载不完整：{start}-{end}，实际收到 {len(chunk)} 字节"
            )
        content.extend(chunk)

    if len(content) != total_size:
        raise RuntimeError(
            f"上游下载不完整：期望 {total_size} 字节，实际 {len(content)} 字节"
        )

    return bytes(content), etag


def fetch_awavenue_source() -> tuple[bytes, str]:
    """Download the smaller AWAvenue source; rule validation guards truncation."""
    with request(AWAVENUE_SOURCE_URL) as response:
        content = response.read()
        etag = response.headers.get("ETag", "").strip('"')
    return content, etag


def normalize_rules(source: str) -> list[str]:
    sections: set[str] = set()
    rules: list[str] = []
    seen: set[str] = set()

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sections.add(line)
            continue

        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            raise RuntimeError(f"第 {line_number} 行不是完整规则：{line}")

        rule_type = parts[0].upper()
        value = parts[1]
        policy = parts[2].lower()
        modifiers = [modifier for modifier in parts[3:] if modifier]

        if rule_type not in ALLOWED_RULE_TYPES:
            raise RuntimeError(f"第 {line_number} 行包含未知规则类型：{rule_type}")
        if policy != "reject":
            raise RuntimeError(f"第 {line_number} 行不是 Reject 规则：{line}")
        if not value:
            raise RuntimeError(f"第 {line_number} 行缺少匹配值")

        if rule_type.startswith("DOMAIN"):
            value = value.lower()

        normalized = ",".join([rule_type, value, *modifiers])
        if normalized not in seen:
            seen.add(normalized)
            rules.append(normalized)

    if sections != {"[Rule]"}:
        raise RuntimeError(f"上游配置区段异常：{sorted(sections)}")
    if len(rules) < MIN_RULE_COUNT:
        raise RuntimeError(
            f"规则数量异常：仅 {len(rules)} 条，低于安全阈值 {MIN_RULE_COUNT}"
        )

    return rules


def normalize_awavenue_rules(source: str) -> tuple[list[str], dict[str, str]]:
    """Normalize and validate the AWAvenue QuantumultX rule source."""
    metadata: dict[str, str] = {}
    rules: list[str] = []
    seen: set[str] = set()
    source_rule_count = 0

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, separator, value = line[1:].partition(":")
            if separator:
                metadata[key.strip().lower()] = value.strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            raise RuntimeError(f"AWAvenue 第 {line_number} 行不应包含配置区段：{line}")

        source_rule_count += 1
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            raise RuntimeError(f"AWAvenue 第 {line_number} 行不是完整规则：{line}")

        rule_type = parts[0].upper()
        value = parts[1]
        policy = parts[2].lower()
        modifiers = [modifier for modifier in parts[3:] if modifier]
        if rule_type not in AWAVENUE_ALLOWED_RULE_TYPES:
            raise RuntimeError(
                f"AWAvenue 第 {line_number} 行包含未知规则类型：{rule_type}"
            )
        if policy != "reject":
            raise RuntimeError(f"AWAvenue 第 {line_number} 行不是 Reject 规则：{line}")
        if not value:
            raise RuntimeError(f"AWAvenue 第 {line_number} 行缺少匹配值")

        value = value.lower()
        normalized = ",".join([rule_type, value, *modifiers])
        if normalized not in seen:
            seen.add(normalized)
            rules.append(normalized)

    if metadata.get("title") != "AWAvenue Ads Rule":
        raise RuntimeError("AWAvenue 上游缺少预期的标题")
    try:
        declared_count = int(metadata["total lines"])
    except (KeyError, ValueError) as error:
        raise RuntimeError("AWAvenue 上游缺少有效的 Total lines") from error
    if declared_count != source_rule_count:
        raise RuntimeError(
            f"AWAvenue 规则数量与声明不符：声明 {declared_count} 条，"
            f"实际 {source_rule_count} 条"
        )
    if len(rules) < AWAVENUE_MIN_RULE_COUNT:
        raise RuntimeError(
            f"AWAvenue 规则数量异常：仅 {len(rules)} 条，"
            f"低于安全阈值 {AWAVENUE_MIN_RULE_COUNT}"
        )
    if not metadata.get("version") or not metadata.get("update time"):
        raise RuntimeError("AWAvenue 上游缺少版本或更新时间")

    return rules, metadata


def deduplicate_awavenue_rules(
    johnshall_rules: list[str], awavenue_rules: list[str]
) -> tuple[list[str], int]:
    """Remove AWAvenue rules already covered by Johnshall match semantics."""
    johnshall_exact = set(johnshall_rules)
    johnshall_suffixes: set[str] = set()
    johnshall_keywords: set[str] = set()

    for rule in johnshall_rules:
        parts = rule.split(",")
        if len(parts) != 2:
            continue
        if parts[0] == "DOMAIN-SUFFIX":
            johnshall_suffixes.add(parts[1])
        elif parts[0] == "DOMAIN-KEYWORD":
            johnshall_keywords.add(parts[1])

    def covered(rule: str) -> bool:
        if rule in johnshall_exact:
            return True

        parts = rule.split(",")
        if len(parts) != 2:
            return False
        rule_type, value = parts
        if rule_type in {"DOMAIN", "DOMAIN-SUFFIX"}:
            labels = value.split(".")
            if any(
                ".".join(labels[index:]) in johnshall_suffixes
                for index in range(len(labels))
            ):
                return True
            return any(keyword in value for keyword in johnshall_keywords)
        if rule_type == "DOMAIN-KEYWORD":
            return any(keyword in value for keyword in johnshall_keywords)
        return False

    deduplicated = [rule for rule in awavenue_rules if not covered(rule)]
    return deduplicated, len(awavenue_rules) - len(deduplicated)


def render_awavenue_block(
    source_bytes: bytes,
    etag: str,
    metadata: dict[str, str],
    source_rules: list[str],
    imported_rules: list[str],
) -> str:
    """Render the generated AWAvenue complement inside the custom list."""
    digest = hashlib.sha256(source_bytes).hexdigest()
    type_counts = {
        rule_type: sum(rule.startswith(f"{rule_type},") for rule in imported_rules)
        for rule_type in sorted(AWAVENUE_ALLOWED_RULE_TYPES)
    }
    count_summary = ", ".join(
        f"{count} {rule_type}" for rule_type, count in type_counts.items() if count
    )
    header = [
        AWAVENUE_BLOCK_BEGIN,
        "# Managed by scripts/sync_johnshall_adblock.py. Do not edit this block manually.",
        "# Imported source: AWAvenue Ads Rule (QuantumultX format)",
        f"# Source project: {AWAVENUE_SOURCE_PROJECT}",
        f"# Source file: {AWAVENUE_SOURCE_URL}",
        f"# Source version: {metadata['version']}",
        f"# Source updated: {metadata['update time']}",
        f"# Source-ETag: {etag or 'unknown'}",
        f"# Source-SHA256: {digest}",
        f"# Source license: GPL-3.0 ({AWAVENUE_SOURCE_LICENSE})",
        "# Deduplication: exact duplicates and rules semantically covered by a broader",
        "# Johnshall DOMAIN-SUFFIX or DOMAIN-KEYWORD rule are omitted.",
        f"# Source rules: {len(source_rules)}",
        f"# Imported rules: {len(imported_rules)} ({count_summary}).",
        "",
    ]
    return "\n".join([*header, *imported_rules, AWAVENUE_BLOCK_END])


def replace_awavenue_block(custom_text: str, block: str) -> str:
    """Replace exactly one generated block while preserving manual content."""
    if custom_text.count(AWAVENUE_BLOCK_BEGIN) != 1:
        raise RuntimeError("adblock-custom.list 缺少唯一的 AWAvenue 起始标记")
    if custom_text.count(AWAVENUE_BLOCK_END) != 1:
        raise RuntimeError("adblock-custom.list 缺少唯一的 AWAvenue 结束标记")

    start = custom_text.index(AWAVENUE_BLOCK_BEGIN)
    end_start = custom_text.index(AWAVENUE_BLOCK_END)
    if end_start < start:
        raise RuntimeError("adblock-custom.list 的 AWAvenue 标记顺序异常")
    end = end_start + len(AWAVENUE_BLOCK_END)
    return custom_text[:start] + block + custom_text[end:]


def normalize_local_rule_text(source: str, source_name: str) -> list[str]:
    """Normalize policy-free local rules from already loaded text."""
    rules: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            raise RuntimeError(
                f"{source_name} 第 {line_number} 行不应包含配置区段：{line}"
            )

        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            raise RuntimeError(f"{source_name} 第 {line_number} 行不是完整规则：{line}")

        rule_type = parts[0].upper()
        value = parts[1]
        modifiers = parts[2:]
        if rule_type not in ALLOWED_RULE_TYPES:
            raise RuntimeError(
                f"{source_name} 第 {line_number} 行包含未知规则类型：{rule_type}"
            )
        if not value:
            raise RuntimeError(f"{source_name} 第 {line_number} 行缺少匹配值")
        if any(not modifier for modifier in modifiers):
            raise RuntimeError(f"{source_name} 第 {line_number} 行包含空修饰符：{line}")
        if modifiers and modifiers[0].upper() in {"DIRECT", "PROXY", "REJECT"}:
            raise RuntimeError(
                f"{source_name} 第 {line_number} 行不应包含策略字段；"
                "最终 adblock.list 由父配置统一应用 REJECT"
            )

        if rule_type.startswith("DOMAIN"):
            value = value.lower()
        normalized = ",".join([rule_type, value, *modifiers])
        if normalized not in seen:
            seen.add(normalized)
            rules.append(normalized)

    return rules


def normalize_local_rules(path: Path) -> list[str]:
    """Read a policy-free local rule list and return unique normalized rules."""
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f"本地规则文件不存在：{path}")
    return normalize_local_rule_text(
        path.read_text(encoding="utf-8-sig"), str(path)
    )


def merge_rules(
    upstream_rules: list[str],
    custom_rules: list[str],
    exception_rules: list[str],
) -> tuple[list[str], int, int, int]:
    """Merge ordered inputs and apply exact normalized exceptions.

    Returns the final rules, custom rules newly added beyond upstream, matched
    exceptions, and unmatched exceptions.
    """
    upstream_set = set(upstream_rules)
    custom_new_count = len(set(custom_rules) - upstream_set)
    combined = [*upstream_rules, *custom_rules]
    combined_set = set(combined)
    exception_set = set(exception_rules)
    matched_exception_count = len(combined_set & exception_set)
    unmatched_exception_count = len(exception_set - combined_set)

    final_rules: list[str] = []
    seen: set[str] = set()
    for rule in combined:
        if rule in exception_set or rule in seen:
            continue
        seen.add(rule)
        final_rules.append(rule)

    return (
        final_rules,
        custom_new_count,
        matched_exception_count,
        unmatched_exception_count,
    )


def existing_rule_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def render(
    source_bytes: bytes,
    etag: str,
    rules: list[str],
    *,
    custom_count: int,
    exception_count: int,
    matched_exception_count: int,
) -> str:
    digest = hashlib.sha256(source_bytes).hexdigest()
    header = [
        "# Generated file. Do not edit manually.",
        f"# Source: {SOURCE_URL}",
        f"# Source-ETag: {etag or 'unknown'}",
        f"# Source-SHA256: {digest}",
        "# Source project: Johnshall/Shadowrocket-ADBlock-Rules-Forever",
        f"# Source license: CC BY-SA 4.0 ({SOURCE_LICENSE})",
        "# Secondary source: AWAvenue Ads Rule (managed in rules/adblock-custom.list)",
        "# Local custom source: rules/adblock-custom.list (content outside the AWAvenue block)",
        "# Local exact exceptions: rules/adblock-exceptions.list",
        "# Generated result: normalized Johnshall + AWAvenue complement + custom - exact exceptions.",
        f"# Custom-list rules (including AWAvenue complement): {custom_count}",
        f"# Local exceptions: {exception_count} ({matched_exception_count} matched)",
        "# Policy is supplied by the parent RULE-SET as REJECT.",
        "",
    ]
    return "\n".join([*header, *rules, ""])


def render_adblock_config(base_text: str) -> str:
    """Rebuild the adblock config from the maintained mobile base config."""
    trailing = "\n" if base_text.endswith("\n") else ""
    lines = base_text.splitlines()
    if not lines or not lines[0].startswith("# Shadowrocket mobile: "):
        raise RuntimeError("移动端基础配置缺少预期的版本标题")
    if ADBLOCK_RULE in lines:
        raise RuntimeError("移动端基础配置不应直接包含广告 RULE-SET")
    if lines.count(BILIBILI_HEADING) != 1:
        raise RuntimeError("移动端基础配置缺少唯一的 Bilibili 规则区段锚点")

    lines[0] = "# Shadowrocket mobile adblock"
    lines.insert(
        1,
        "# Generated from shadowrocket_gpt_maintain-mobile.conf; do not edit manually.",
    )

    for old_heading, new_heading in HEADING_REPLACEMENTS.items():
        if lines.count(old_heading) != 1:
            raise RuntimeError(f"移动端基础配置区段异常：{old_heading}")
        index = lines.index(old_heading)
        lines[index] = new_heading

    insert_at = lines.index(HEADING_REPLACEMENTS[BILIBILI_HEADING])
    lines[insert_at:insert_at] = ADBLOCK_BLOCK
    result = "\n".join(lines) + trailing

    if result.count(ADBLOCK_RULE) != 1:
        raise RuntimeError("派生配置中的广告 RULE-SET 数量异常")
    result_lines = result.splitlines()
    if result_lines.count("[Rule]") != 1 or result_lines.count("FINAL,PROXY") != 1:
        raise RuntimeError("派生配置的核心区段或兜底规则异常")
    return result


def write_atomic(path: Path, content: str) -> bool:
    """Write a file atomically and report whether its content changed."""
    path = path.resolve()
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_LIST)
    parser.add_argument("--custom-list", type=Path, default=DEFAULT_CUSTOM_LIST)
    parser.add_argument(
        "--exceptions-list", type=Path, default=DEFAULT_EXCEPTIONS_LIST
    )
    parser.add_argument("--base-config", type=Path, default=DEFAULT_BASE_CONFIG)
    parser.add_argument("--adblock-config", type=Path, default=DEFAULT_ADBLOCK_CONFIG)
    parser.add_argument(
        "--allow-large-shrink",
        action="store_true",
        help="允许新规则数量低于现有文件的 70%%",
    )
    args = parser.parse_args()

    exception_rules = normalize_local_rules(args.exceptions_list)
    source_bytes, etag = fetch_source()
    source = source_bytes.decode("utf-8-sig")
    upstream_rules = normalize_rules(source)
    awavenue_source_bytes, awavenue_etag = fetch_awavenue_source()
    awavenue_source = awavenue_source_bytes.decode("utf-8-sig")
    awavenue_rules, awavenue_metadata = normalize_awavenue_rules(awavenue_source)
    awavenue_custom_rules, awavenue_covered_count = deduplicate_awavenue_rules(
        upstream_rules, awavenue_rules
    )

    custom_path = args.custom_list.resolve()
    if not custom_path.is_file():
        raise RuntimeError(f"本地规则文件不存在：{custom_path}")
    existing_custom_content = custom_path.read_text(encoding="utf-8-sig")
    awavenue_block = render_awavenue_block(
        awavenue_source_bytes,
        awavenue_etag,
        awavenue_metadata,
        awavenue_rules,
        awavenue_custom_rules,
    )
    custom_content = replace_awavenue_block(
        existing_custom_content, awavenue_block
    )
    custom_rules = normalize_local_rule_text(custom_content, str(custom_path))
    (
        rules,
        custom_new_count,
        matched_exception_count,
        unmatched_exception_count,
    ) = merge_rules(upstream_rules, custom_rules, exception_rules)

    old_count = existing_rule_count(args.output)
    if (
        old_count
        and len(rules) < old_count * 0.7
        and not args.allow_large_shrink
    ):
        raise RuntimeError(
            f"规则数量从 {old_count} 降至 {len(rules)}，超过 30%，已停止覆盖"
        )

    list_content = render(
        source_bytes,
        etag,
        rules,
        custom_count=len(custom_rules),
        exception_count=len(exception_rules),
        matched_exception_count=matched_exception_count,
    )
    base_text = args.base_config.resolve().read_text(encoding="utf-8")
    config_content = render_adblock_config(base_text)
    custom_changed = write_atomic(custom_path, custom_content)
    list_changed = write_atomic(args.output, list_content)
    config_changed = write_atomic(args.adblock_config, config_content)

    print(
        f"已同步 {custom_path} changed={str(custom_changed).lower()}"
    )
    print(f"已同步 {args.output.resolve()} changed={str(list_changed).lower()}")
    print(
        f"已同步 {args.adblock_config.resolve()} changed={str(config_changed).lower()}"
    )
    print(f"上游大小：{len(source_bytes)} 字节")
    print(f"Johnshall 去重规则：{len(upstream_rules)} 条")
    print(
        f"AWAvenue 去重规则：{len(awavenue_rules)} 条"
        f"（写入 custom {len(awavenue_custom_rules)} 条，"
        f"由 Johnshall 覆盖 {awavenue_covered_count} 条）"
    )
    print(
        f"custom 列表规则：{len(custom_rules)} 条"
        f"（新增 {custom_new_count} 条，其余与上游重复）"
    )
    print(
        f"精确例外：{len(exception_rules)} 条"
        f"（命中 {matched_exception_count} 条，未命中 {unmatched_exception_count} 条）"
    )
    print(f"最终规则：{len(rules)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
