from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from app.event_stream.models import CanonicalEvent


@dataclass
class EventCluster:
    primary: CanonicalEvent
    duplicates: List[CanonicalEvent]
    matched_on: str


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    # 2026-10-09 F5 修复: naive (如 Federal Register 的纯日期) 一律按 UTC 解释,
    #   与 store._published_to_seen_ms 同口径; 否则 naive/aware 相减直接 TypeError。
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


# 2026-10-17 审计修复: SEC Atom 的标题形如 "8-K - Company (CIK) (Filer)",
#   _normalize_title 先截掉 " - " 之后的内容, 所有 8-K 都归一成 "8 k" ⇒ 186 条
#   不同公司的 8-K 被聚成 1 条。表单型标题没有可区分实体, 只允许 URL 去重;
#   标题去重遇到这类裸表单号必须放行 (宁可少合并, 不可误合并)。
_FORM_ONLY_TITLE = re.compile(
    r"^(8 k|10 q|10 k|6 k|20 f|40 f|s 1|s 3|s 4|def 14a|sc 13d|sc 13g|form 3|form 4|form 5)( a)?$"
)


def _normalize_title(title: str) -> str:
    title = title.split(" - ", 1)[0]
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))


def _titles_similar(left: str, right: str) -> bool:
    normalized_left = _normalize_title(left)
    normalized_right = _normalize_title(right)
    if not normalized_left or not normalized_right:
        return False
    if _FORM_ONLY_TITLE.fullmatch(normalized_left) or _FORM_ONLY_TITLE.fullmatch(normalized_right):
        return False
    if normalized_left == normalized_right:
        return True
    ratio = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    return ratio >= 0.60


def _same_url(left: CanonicalEvent, right: CanonicalEvent) -> bool:
    return bool(left.source_url and right.source_url and
                _normalize_url(left.source_url) == _normalize_url(right.source_url))


def _close_in_time(left: CanonicalEvent, right: CanonicalEvent, max_hours: int) -> bool:
    left_time = _parse_time(left.event_time)
    right_time = _parse_time(right.event_time)
    if left_time is None or right_time is None:
        # 2026-10-09 F5 修复: 时间缺失时不合并 (宁可漏合并也不误合并,
        #   与 §18.2 自述口径对齐; 此前缺时间恒 True = 无视时间窗)。
        return False
    return abs((left_time - right_time).total_seconds()) <= max_hours * 3600


def _is_duplicate(
    left: CanonicalEvent,
    right: CanonicalEvent,
    max_hours: int,
) -> Optional[str]:
    if _same_url(left, right):
        return "url"
    if _titles_similar(left.headline, right.headline) and _close_in_time(left, right, max_hours):
        return "title"
    return None


def deduplicate_events(
    events: Iterable[CanonicalEvent],
    max_hours: int = 24,
) -> List[EventCluster]:
    tier_order = {"T0": 0, "T1": 1, "T2": 2}
    clusters: List[Tuple[CanonicalEvent, List[CanonicalEvent], str]] = []
    for event in sorted(
        events,
        key=lambda item: (
            tier_order.get(item.source_tier, 9),
            item.source_id,
            item.event_id,
        ),
    ):
        for cluster_index, (primary, duplicates, matched_on) in enumerate(clusters):
            duplicate_reason = _is_duplicate(primary, event, max_hours)
            if duplicate_reason:
                duplicates.append(event)
                if matched_on == "primary":
                    clusters[cluster_index] = (
                        primary,
                        duplicates,
                        duplicate_reason,
                    )
                break
        else:
            clusters.append((event, [], "primary"))

    return [
        EventCluster(primary=primary, duplicates=duplicates, matched_on=matched_on)
        for primary, duplicates, matched_on in clusters
    ]


def deduplicate_events_exact(events: Iterable[CanonicalEvent]) -> List[EventCluster]:
    tier_order = {"T0": 0, "T1": 1, "T2": 2}
    clusters: List[Tuple[CanonicalEvent, List[CanonicalEvent], str]] = []
    url_index: Dict[str, List[int]] = {}
    title_index: Dict[str, List[int]] = {}
    for event in sorted(
        events,
        key=lambda item: (
            tier_order.get(item.source_tier, 9),
            item.source_id,
            item.event_id,
        ),
    ):
        url_key = _normalize_url(event.source_url) if event.source_url else ""
        title_key = _normalize_title(event.headline)
        candidate_ids = set(url_index.get(url_key, []) if url_key else [])
        candidate_ids.update(title_index.get(title_key, []) if title_key else [])
        for cluster_index in sorted(candidate_ids):
            primary, duplicates, matched_on = clusters[cluster_index]
            duplicate_reason = None
            if _same_url(primary, event):
                duplicate_reason = "url"
            elif title_key and _titles_similar(primary.headline, event.headline) and _close_in_time(
                primary, event, 24
            ):
                duplicate_reason = "title"
            if duplicate_reason:
                duplicates.append(event)
                if matched_on == "primary":
                    clusters[cluster_index] = (primary, duplicates, duplicate_reason)
                break
        else:
            cluster_index = len(clusters)
            clusters.append((event, [], "primary"))
            if url_key:
                url_index.setdefault(url_key, []).append(cluster_index)
            if title_key:
                title_index.setdefault(title_key, []).append(cluster_index)
    return [
        EventCluster(primary=primary, duplicates=duplicates, matched_on=matched_on)
        for primary, duplicates, matched_on in clusters
    ]
