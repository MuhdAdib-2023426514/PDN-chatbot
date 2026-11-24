"""Rotating-asset bounded pattern proposal generator for Gate A review."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryItem,
    ProposedPatternRule,
)
from pdn_sync.domain.policy import Disposition, ResourceType, SupersessionPolicy

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_PROPOSABLE_CATEGORIES: frozenset[DiscoveryCategory] = frozenset(
    {
        DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
        DiscoveryCategory.POSTER_BANNER,
        DiscoveryCategory.FACILITY_PHOTO,
        DiscoveryCategory.DOCUMENT_PDF,
        DiscoveryCategory.DOCUMENT_OFFICE,
    }
)


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:48] or "assets"


def propose_rotating_asset_patterns(
    items: list[InventoryItem],
    *,
    allowed_path_prefix: str = "/v2",
) -> list[ProposedPatternRule]:
    """Group candidate posters & docs by directory + extension into bounded single-dir patterns.

    Never emits broad cross-directory `/**` wildcards or top-level `/v2/*` patterns.
    """
    prefix = allowed_path_prefix.rstrip("/")
    grouped: dict[tuple[str, str, ResourceType, DiscoveryCategory], list[InventoryItem]] = (
        defaultdict(list)
    )

    for item in items:
        if item.disposition == Disposition.EXCLUDED:
            continue
        if item.category not in _PROPOSABLE_CATEGORIES:
            continue
        if item.resource_type not in {ResourceType.IMAGE_POSTER, ResourceType.DOCUMENT}:
            continue

        path = urlsplit(item.url).path
        posix = PurePosixPath(path)
        parent_dir = str(posix.parent)
        ext = posix.suffix.lower()

        # Never propose overly broad top-level prefix patterns
        if parent_dir in {"/", prefix} or not parent_dir.startswith(f"{prefix}/"):
            continue
        if not ext:
            continue

        grouped[(parent_dir, ext, item.resource_type, item.category)].append(item)

    proposals: list[ProposedPatternRule] = []
    seen_ids: set[str] = set()

    for (parent_dir, ext, resource_type, category), group_items in sorted(grouped.items()):
        # Always propose rotating blood-stock & poster directories even with 1 observed file;
        # for general docs/facility directories, propose when >= 2 files share the template.
        is_known_rotating = category in {
            DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            DiscoveryCategory.POSTER_BANNER,
        }
        if not is_known_rotating and len(group_items) < 2:
            continue

        pattern = f"{parent_dir}/*{ext}"
        rel_dir = parent_dir[len(prefix) :].strip("/")
        base_id = (
            f"pattern-{category.value.replace('_', '-')}-{_slugify(rel_dir)}-{ext.lstrip('.')}"
        )
        rule_id = base_id
        suffix_idx = 2
        while rule_id in seen_ids:
            rule_id = f"{base_id}-{suffix_idx}"
            suffix_idx += 1
        seen_ids.add(rule_id)

        if category == DiscoveryCategory.BLOOD_STOCK_GRAPHIC:
            supersession = SupersessionPolicy.LATEST_WINS
            check_interval = "3h"
            reason = "Rotating blood-stock infographic (latest_wins; always Gate B quarantine)"
            max_matches = min(500, max(24, len(group_items) * 4))
        elif category == DiscoveryCategory.POSTER_BANNER:
            supersession = SupersessionPolicy.NONE
            check_interval = "3h"
            reason = "Rotating PDN announcement/SmartSlider poster assets"
            max_matches = min(500, max(50, len(group_items) * 3))
        else:
            supersession = SupersessionPolicy.NONE
            check_interval = "24h"
            reason = f"Grouped {category.value} assets under {parent_dir}"
            max_matches = min(500, max(30, len(group_items) * 3))

        examples = sorted({it.url for it in group_items})[:5]
        proposals.append(
            ProposedPatternRule(
                id=rule_id,
                pattern=pattern,
                resource_type=resource_type,
                category=category,
                reason=reason,
                max_matches=max_matches,
                check_interval=check_interval,
                supersession=supersession,
                match_count=len(group_items),
                examples=examples,
            )
        )

    return proposals
