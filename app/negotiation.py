"""HTTP Accept 内容协商核心逻辑。

支持的 Accept 头语法（受限子集，便于调试）：

    Accept      = #( range *( OWS ";" OWS parameter ) )
    range       = ( token "/" token ) / ( token "/*" ) / "*/*"
    parameter   = "q=" qvalue        （仅此一个参数，且不允许媒体参数）
    qvalue      = ( "0" [ "." 0*3DIGIT ] ) / ( "1" [ "." 0*3("0") ] )

- 仅支持无媒体参数的 type/subtype、type/*、*/*；
- 大小写不敏感；
- q 缺省为 1；q=0 表示显式拒绝；
- 任意非法语法均抛出 AcceptSyntaxError。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# RFC 9110 tchar 字符集，但去掉单独的 "*"（通配符只能作为完整的一段）。
_TOKEN = r"[!#$%&'+\-.^_`|~0-9A-Za-z]+"
_QVALUE = r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)"

# 具体媒体类型：type/subtype，两侧均为名称 token，不允许 "*"。
_CONCRETE_RE = re.compile(r"^" + _TOKEN + r"/" + _TOKEN + r"$")
# Accept 单个条目：范围 + 可选的 q（不允许其它参数）。
_ENTRY_RE = re.compile(
    r"^(?P<range>(?:" + _TOKEN + r"/(?:" + _TOKEN + r"|\*)|\*/\*))"
    r"(?:[ \t]*;[ \t]*q=(?P<q>" + _QVALUE + r"))?$"
)
_PARAM_SPLIT_RE = re.compile(r"[ \t]*;[ \t]*")
_QVALUE_RE = re.compile(r"^" + _QVALUE + r"$")


class AcceptSyntaxError(ValueError):
    """Accept 头存在非法语法。"""


@dataclass(frozen=True)
class AcceptRange:
    """Accept 头中的一个条目（按出现顺序）。"""

    raw: str
    type: str
    subtype: str
    q: float
    index: int

    @property
    def is_wildcard(self) -> bool:
        return self.type == "*"

    @property
    def is_subtype_wildcard(self) -> bool:
        return self.subtype == "*" and self.type != "*"

    @property
    def specificity(self) -> int:
        """匹配具体度：2=精确 type/subtype，1=type/*，0=*/*。"""
        if self.is_wildcard:
            return 0
        if self.is_subtype_wildcard:
            return 1
        return 2

    def matches(self, media_type: str, subtype: str) -> bool:
        if self.is_wildcard:
            return True
        if self.type != media_type:
            return False
        return self.subtype == "*" or self.subtype == subtype


def validate_concrete_media_type(value: str) -> None:
    """候选媒体类型必须是具体的 type/subtype（不允许通配符或参数）。"""
    if not isinstance(value, str) or _CONCRETE_RE.match(value) is None:
        raise AcceptSyntaxError(
            f"候选媒体类型必须是具体的 type/subtype（不允许通配符或参数）: {value!r}"
        )


def parse_accept(header: str) -> list[AcceptRange]:
    """解析 Accept 头字符串。

    非法（空串、空白条目、非法 token、多余/未知参数、q 越界等）一律抛
    AcceptSyntaxError。
    """
    if not isinstance(header, str) or header.strip() == "":
        raise AcceptSyntaxError("Accept 头不能为空")

    ranges: list[AcceptRange] = []
    for position, chunk in enumerate(header.split(",")):
        entry = chunk.strip()
        if entry == "":
            raise AcceptSyntaxError(f"Accept 存在空条目: {header!r}")

        match = _ENTRY_RE.match(entry)
        if match is None:
            # 区分“带了 q 之外的参数”与其它语法错误，信息更直观。
            parts = _PARAM_SPLIT_RE.split(entry)
            if len(parts) > 2:
                raise AcceptSyntaxError(
                    f"Accept 仅支持单个 q 参数，不支持媒体参数: {chunk!r}"
                )
            raise AcceptSyntaxError(f"Accept 条目语法非法: {chunk!r}")

        range_part = match.group("range")
        type_, subtype = range_part.split("/", 1)
        if type_ == "*" and subtype != "*":
            raise AcceptSyntaxError(f"非法的通配范围: {chunk!r}")

        q = 1.0
        q_text = match.group("q")
        if q_text is not None:
            if _QVALUE_RE.match(q_text) is None:
                raise AcceptSyntaxError(
                    f"q 值非法（需为 0-1，最多 3 位小数）: {chunk!r}"
                )
            q = float(q_text)

        ranges.append(
            AcceptRange(
                raw=range_part.lower(),
                type=type_.lower(),
                subtype=subtype.lower(),
                q=q,
                index=position,
            )
        )

    return ranges


SPECIFICITY_NAMES = {0: "wildcard", 1: "type_subtype_wildcard", 2: "exact"}


def _matching_ranges(
    accept_ranges: list[AcceptRange], media_type: str, subtype: str
) -> list[AcceptRange]:
    """命中某候选的全部范围：具体度降序，同具体度按头部位置升序。"""
    hits = [r for r in accept_ranges if r.matches(media_type, subtype)]
    hits.sort(key=lambda r: (-r.specificity, r.index))
    return hits


@dataclass
class CandidateResult:
    index: int
    media_type: str
    server_priority: float
    matched_range: str | None
    q: float | None
    specificity: int | None
    specificity_name: str | None
    eligible: bool
    eliminated_reason: str | None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "media_type": self.media_type,
            "server_priority": self.server_priority,
            "matched_range": self.matched_range,
            "q": self.q,
            "specificity": self.specificity,
            "specificity_name": self.specificity_name,
            "eligible": self.eligible,
            "eliminated_reason": self.eliminated_reason,
        }


def negotiate(
    accept_header: str,
    candidates: list[tuple[str, float]],
) -> tuple[int | None, list[CandidateResult], list[AcceptRange]]:
    """执行协商。

    candidates 为 (media_type, server_priority)，按输入顺序排列。
    返回 (selected_index, 每个候选的评估结果, 解析后的 Accept 范围)。

    匹配规则：取与候选匹配的最具体范围；具体度相同时取头中最早出现的范围。
    选择排序：q 降序 → 匹配具体度降序 → server_priority 降序 → 输入顺序。
    """
    accept_ranges = parse_accept(accept_header)

    results: list[CandidateResult] = []
    for index, (raw_media_type, server_priority) in enumerate(candidates):
        media_type = raw_media_type.strip().lower()
        validate_concrete_media_type(media_type)
        mtype, msub = media_type.split("/", 1)

        # 所有命中的范围；先按具体度降序，同具体度按头中位置取最早。
        hits = _matching_ranges(accept_ranges, mtype, msub)

        if not hits:
            results.append(
                CandidateResult(
                    index=index,
                    media_type=media_type,
                    server_priority=server_priority,
                    matched_range=None,
                    q=None,
                    specificity=None,
                    specificity_name=None,
                    eligible=False,
                    eliminated_reason="no_match",
                )
            )
            continue

        best = hits[0]
        if best.q == 0:
            results.append(
                CandidateResult(
                    index=index,
                    media_type=media_type,
                    server_priority=server_priority,
                    matched_range=best.raw,
                    q=0.0,
                    specificity=best.specificity,
                    specificity_name=SPECIFICITY_NAMES[best.specificity],
                    eligible=False,
                    eliminated_reason="q_zero",
                )
            )
            continue

        results.append(
            CandidateResult(
                index=index,
                media_type=media_type,
                server_priority=server_priority,
                matched_range=best.raw,
                q=best.q,
                specificity=best.specificity,
                specificity_name=SPECIFICITY_NAMES[best.specificity],
                eligible=True,
                eliminated_reason=None,
            )
        )

    eligible = [r for r in results if r.eligible]
    if not eligible:
        return None, results, accept_ranges

    eligible.sort(
        key=lambda r: (
            -r.q,  # type: ignore[operator]
            -(r.specificity if r.specificity is not None else -1),
            -r.server_priority,
            r.index,
        )
    )
    selected = eligible[0].index
    for r in results:
        if r.eligible and r.index != selected:
            r.eliminated_reason = "outranked"
    return selected, results, accept_ranges


# ---------- 静态审计（不做最终选定） ----------


@dataclass
class RangeHitView:
    """审计视图：候选命中的某个范围（按头部顺序列出）。"""

    index: int
    range: str
    q: float
    specificity: int
    specificity_name: str
    effective: bool

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "range": self.range,
            "q": self.q,
            "specificity": self.specificity,
            "specificity_name": self.specificity_name,
            "effective": self.effective,
        }


@dataclass
class RangeConflictView:
    """同一候选上具体度相同但 q 不同的一组冲突范围。"""

    specificity: int
    specificity_name: str
    ranges: list[dict]

    def to_dict(self) -> dict:
        return {
            "specificity": self.specificity,
            "specificity_name": self.specificity_name,
            "ranges": self.ranges,
        }


@dataclass
class CandidateAudit:
    """单个候选的静态审计结果。"""

    index: int
    media_type: str
    server_priority: float
    eligible: bool
    eliminated_reason: str | None
    effective_range: dict | None
    matched_ranges: list[RangeHitView]
    conflicts: list[RangeConflictView]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "media_type": self.media_type,
            "server_priority": self.server_priority,
            "eligible": self.eligible,
            "eliminated_reason": self.eliminated_reason,
            "effective_range": self.effective_range,
            "matched_ranges": [h.to_dict() for h in self.matched_ranges],
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def _range_brief(r: AcceptRange) -> dict:
    return {
        "index": r.index,
        "range": r.raw,
        "q": r.q,
        "specificity": r.specificity,
        "specificity_name": SPECIFICITY_NAMES[r.specificity],
    }


def _find_conflicts(hits: list[AcceptRange]) -> list[RangeConflictView]:
    """按具体度分组，同组内 q 取值不唯一即构成冲突。

    组内范围按头部位置升序；组间按组内最早头部位置排序，保证输出稳定。
    """
    by_specificity: dict[int, list[AcceptRange]] = {}
    for r in hits:
        by_specificity.setdefault(r.specificity, []).append(r)

    conflicts: list[RangeConflictView] = []
    for specificity, group in by_specificity.items():
        if len({r.q for r in group}) < 2:
            continue
        ordered = sorted(group, key=lambda r: r.index)
        conflicts.append(
            RangeConflictView(
                specificity=specificity,
                specificity_name=SPECIFICITY_NAMES[specificity],
                ranges=[
                    {"index": r.index, "range": r.raw, "q": r.q} for r in ordered
                ],
            )
        )
    conflicts.sort(key=lambda c: c.ranges[0]["index"])
    return conflicts


def audit_accept(
    accept_header: str,
    candidates: list[tuple[str, float]],
) -> tuple[list[CandidateAudit], list[AcceptRange], list[AcceptRange]]:
    """对协商规则做静态审计，不返回单一选定项。

    返回 (每个候选的审计结果, shadowed 范围, 解析后的 Accept 范围)。

    - 每个候选列出全部命中范围（按头部顺序）及最终生效范围（与协商规则一致：
      最具体优先，同具体度取头中最早）；
    - shadowed：合法但从未对任何候选生效的范围，按头部顺序排列；
    - q=0 排除与无命中语义与 negotiate 一致（但不产生 outranked）。
    """
    accept_ranges = parse_accept(accept_header)

    audits: list[CandidateAudit] = []
    effective_indices: set[int] = set()

    for index, (raw_media_type, server_priority) in enumerate(candidates):
        media_type = raw_media_type.strip().lower()
        validate_concrete_media_type(media_type)
        mtype, msub = media_type.split("/", 1)

        hits = _matching_ranges(accept_ranges, mtype, msub)
        best = hits[0] if hits else None

        if best is None:
            eligible, reason = False, "no_match"
        elif best.q == 0:
            eligible, reason = False, "q_zero"
        else:
            eligible, reason = True, None

        if best is not None:
            effective_indices.add(best.index)

        matched = [
            RangeHitView(
                index=r.index,
                range=r.raw,
                q=r.q,
                specificity=r.specificity,
                specificity_name=SPECIFICITY_NAMES[r.specificity],
                effective=(best is not None and r.index == best.index),
            )
            for r in sorted(hits, key=lambda r: r.index)
        ]

        audits.append(
            CandidateAudit(
                index=index,
                media_type=media_type,
                server_priority=server_priority,
                eligible=eligible,
                eliminated_reason=reason,
                effective_range=_range_brief(best) if best is not None else None,
                matched_ranges=matched,
                conflicts=_find_conflicts(hits),
            )
        )

    shadowed = [r for r in accept_ranges if r.index not in effective_indices]
    return audits, shadowed, accept_ranges
