#!/usr/bin/env python3
"""Generate a Shadowrocket RULE-SET list from Johnshall's ad-only config."""

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
CHUNK_SIZE = 512 * 1024
MIN_RULE_COUNT = 10_000
ALLOWED_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-KEYWORD",
    "DOMAIN-SUFFIX",
    "IP-CIDR",
    "IP-CIDR6",
}
USER_AGENT = "shadowrocket-config-adblock-sync/1.0"


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


def existing_rule_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def render(source_bytes: bytes, etag: str, rules: list[str]) -> str:
    digest = hashlib.sha256(source_bytes).hexdigest()
    header = [
        "# Generated file. Do not edit manually.",
        f"# Source: {SOURCE_URL}",
        f"# Source-ETag: {etag or 'unknown'}",
        f"# Source-SHA256: {digest}",
        "# Source project: Johnshall/Shadowrocket-ADBlock-Rules-Forever",
        f"# Source license: CC BY-SA 4.0 ({SOURCE_LICENSE})",
        "# Policy is supplied by the parent RULE-SET as REJECT.",
        "",
    ]
    return "\n".join([*header, *rules, ""])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_output = Path(__file__).resolve().parents[1] / "rules" / "adblock.list"
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument(
        "--allow-large-shrink",
        action="store_true",
        help="允许新规则数量低于现有文件的 70%%",
    )
    args = parser.parse_args()

    source_bytes, etag = fetch_source()
    source = source_bytes.decode("utf-8-sig")
    rules = normalize_rules(source)

    old_count = existing_rule_count(args.output)
    if (
        old_count
        and len(rules) < old_count * 0.7
        and not args.allow_large_shrink
    ):
        raise RuntimeError(
            f"规则数量从 {old_count} 降至 {len(rules)}，超过 30%，已停止覆盖"
        )

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(render(source_bytes, etag, rules), encoding="utf-8")
    temporary.replace(output)

    print(f"已生成 {output}")
    print(f"上游大小：{len(source_bytes)} 字节")
    print(f"去重后规则：{len(rules)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
