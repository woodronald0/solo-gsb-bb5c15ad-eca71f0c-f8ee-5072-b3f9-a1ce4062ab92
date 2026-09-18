"""内容协商逻辑与 HTTP 接口测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.negotiation import (
    AcceptSyntaxError,
    audit_accept,
    negotiate,
    parse_accept,
)


client = TestClient(app)


def post(accept, candidates):
    return client.post(
        "/negotiate", json={"accept": accept, "candidates": candidates}
    )


def post_audit(accept, candidates):
    return client.post(
        "/audit-accept", json={"accept": accept, "candidates": candidates}
    )


def post_compare(baseline, trial, candidates):
    return client.post(
        "/compare-accept",
        json={
            "baseline_accept": baseline,
            "trial_accept": trial,
            "candidates": candidates,
        },
    )


# ---------- parse_accept ----------


def test_parse_default_q_and_case_insensitive():
    ranges = parse_accept("Application/JSON, text/*;q=0.5, */*; q=0.1")
    assert [(r.raw, r.q, r.specificity) for r in ranges] == [
        ("application/json", 1.0, 2),
        ("text/*", 0.5, 1),
        ("*/*", 0.1, 0),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "application",
        "application/",
        "/json",
        "*/json",
        "application/j*son",
        "application/json;q",
        "application/json;q=",
        "application/json;q=1.5",
        "application/json;q=-0.1",
        "application/json;q=0.1234",
        "application/json;q=1.001",
        "application/json;level=1",
        "application/json;q=0.5;charset=utf-8",
        "application/json,",
        ",application/json",
        "application/json,,*/*",
        "app lication/json",
        "application/json; q=abc",
    ],
)
def test_parse_invalid(header):
    with pytest.raises(AcceptSyntaxError):
        parse_accept(header)


def test_parse_q_boundaries_valid():
    ranges = parse_accept("a/b;q=0, c/d;q=1, e/f;q=0.999, g/h;q=1.000")
    assert [r.q for r in ranges] == [0.0, 1.0, 0.999, 1.0]


# ---------- negotiate 核心规则 ----------


def test_most_specific_range_wins():
    # application/json 同时命中精确和 */*，取精确范围的 q。
    idx, results, _ = negotiate(
        "*/*;q=0.1, application/json;q=0.5",
        [("application/json", 0)],
    )
    assert idx == 0
    r = results[0]
    assert r.matched_range == "application/json"
    assert r.q == 0.5
    assert r.specificity == 2
    assert r.eliminated_reason is None


def test_equal_specificity_earliest_in_header_wins():
    # 两个同具体度的精确范围，取头中最早项。
    idx, results, _ = negotiate(
        "application/json;q=0.3, application/json;q=0.9",
        [("application/json", 0)],
    )
    assert results[0].q == 0.3
    assert idx == 0


def test_q_zero_excludes_even_if_wildcard_accepts():
    # 精确 q=0 优先于 */* 的 q=1（最具体范围决定）。
    idx, results, _ = negotiate(
        "*/*;q=1, text/html;q=0",
        [("text/html", 0), ("application/json", 0)],
    )
    html, json_ = results
    assert html.eligible is False
    assert html.eliminated_reason == "q_zero"
    assert json_.eligible is True
    assert idx == 1


def test_ordering_q_then_specificity_then_priority_then_input():
    idx, results, _ = negotiate(
        "*/*;q=0.5, application/*;q=0.8, application/json;q=0.8",
        [
            ("application/xml", 10),   # q0.8, spec1,  p10
            ("application/json", 1),  # q0.8, spec2,  p1  -> 胜
            ("text/plain", 100),      # q0.5
        ],
    )
    assert idx == 1
    assert [r.eliminated_reason for r in results] == ["outranked", None, "outranked"]


def test_q_dominates_specificity_and_priority():
    idx, results, _ = negotiate(
        "application/json;q=0.1, */*;q=0.9",
        [("application/json", 100), ("text/plain", 0)],
    )
    # text/plain q=0.9（via */*）胜过精确匹配但 q=0.1 的 json
    assert idx == 1
    assert results[0].matched_range == "application/json"
    assert results[1].matched_range == "*/*"


def test_tie_input_order():
    idx, _, _ = negotiate(
        "*/*",
        [("a/b", 0), ("c/d", 0)],
    )
    assert idx == 0


def test_no_match():
    idx, results, _ = negotiate("text/html", [("application/json", 0)])
    assert idx is None
    assert results[0].eliminated_reason == "no_match"
    assert results[0].matched_range is None


def test_all_q_zero():
    idx, results, _ = negotiate("*/*;q=0", [("a/b", 0), ("c/d", 0)])
    assert idx is None
    assert all(r.eliminated_reason == "q_zero" for r in results)


def test_concrete_candidate_required():
    with pytest.raises(AcceptSyntaxError):
        negotiate("*/*", [("application/*", 0)])
    with pytest.raises(AcceptSyntaxError):
        negotiate("*/*", [("application/json; charset=utf-8", 0)])


# ---------- audit_accept 静态审计 ----------


def test_audit_lists_all_hits_in_header_order_and_marks_effective():
    audits, shadowed, ranges = audit_accept(
        "text/*;q=0.5, */*;q=0.1, text/html;q=0.9",
        [("text/html", 0)],
    )
    cand = audits[0]
    # 命中范围按头部顺序列出，与具体度无关
    assert [h.range for h in cand.matched_ranges] == ["text/*", "*/*", "text/html"]
    assert [h.effective for h in cand.matched_ranges] == [False, False, True]
    assert cand.effective_range["range"] == "text/html"
    assert cand.effective_range["index"] == 2
    assert cand.eligible is True
    assert cand.eliminated_reason is None
    assert cand.conflicts == []
    # text/* 与 */* 命中但从未生效
    assert [r.raw for r in shadowed] == ["text/*", "*/*"]
    assert [r.index for r in shadowed] == [0, 1]
    assert len(ranges) == 3


def test_audit_shadowed_includes_ranges_matching_nothing():
    _, shadowed, _ = audit_accept(
        "image/png, text/html",
        [("text/html", 0)],
    )
    assert [r.raw for r in shadowed] == ["image/png"]


def test_audit_conflict_same_specificity_different_q():
    audits, shadowed, _ = audit_accept(
        "application/json;q=0.3, application/json;q=0.9",
        [("application/json", 0)],
    )
    cand = audits[0]
    # 同具体度取头中最早项生效
    assert cand.effective_range["index"] == 0
    assert cand.effective_range["q"] == 0.3
    assert len(cand.conflicts) == 1
    conflict = cand.conflicts[0]
    assert conflict.specificity == 2
    assert conflict.specificity_name == "exact"
    assert [(r["index"], r["q"]) for r in conflict.ranges] == [(0, 0.3), (1, 0.9)]
    # 后出现的重复范围从未生效 -> shadowed
    assert [r.index for r in shadowed] == [1]


def test_audit_no_conflict_when_same_q():
    audits, _, _ = audit_accept(
        "application/json;q=0.5, application/json;q=0.5",
        [("application/json", 0)],
    )
    assert audits[0].conflicts == []


def test_audit_conflict_only_within_same_specificity():
    # 不同具体度的 q 差异不算冲突；仅精确组内 q 不同构成冲突。
    audits, _, _ = audit_accept(
        "text/*;q=0.1, text/html;q=0.9, text/html;q=0.7",
        [("text/html", 0)],
    )
    cand = audits[0]
    assert len(cand.conflicts) == 1
    assert cand.conflicts[0].specificity == 2
    assert [r["index"] for r in cand.conflicts[0].ranges] == [1, 2]


def test_audit_q_zero_semantics_preserved():
    audits, shadowed, _ = audit_accept(
        "*/*;q=1, text/html;q=0",
        [("text/html", 0), ("application/json", 0)],
    )
    html, json_ = audits
    assert html.eligible is False
    assert html.eliminated_reason == "q_zero"
    # 生效范围仍是导致排除的精确范围
    assert html.effective_range["range"] == "text/html"
    assert json_.eligible is True
    # */* 对 application/json 生效，text/html 对 text/html 生效 -> 无 shadowed
    assert shadowed == []


def test_audit_no_match_semantics_preserved():
    audits, shadowed, _ = audit_accept("text/html", [("application/json", 0)])
    cand = audits[0]
    assert cand.eligible is False
    assert cand.eliminated_reason == "no_match"
    assert cand.effective_range is None
    assert cand.matched_ranges == []
    assert [r.raw for r in shadowed] == ["text/html"]


def test_audit_candidate_order_stable():
    audits, _, _ = audit_accept(
        "*/*",
        [("b/b", 0), ("a/a", 0), ("c/c", 0)],
    )
    assert [a.media_type for a in audits] == ["b/b", "a/a", "c/c"]


def test_audit_invalid_inputs():
    with pytest.raises(AcceptSyntaxError):
        audit_accept("application/json;q=2", [("a/b", 0)])
    with pytest.raises(AcceptSyntaxError):
        audit_accept("*/*", [("text/*", 0)])


# ---------- HTTP 接口 ----------


def test_endpoint_success():
    resp = post(
        "application/json;q=0.9, text/*;q=0.5, */*;q=0.1",
        [
            {"media_type": "text/html", "server_priority": 10},
            {"media_type": "application/json", "server_priority": 1},
            {"media_type": "image/png", "server_priority": 5},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected"]["media_type"] == "application/json"
    assert body["selected"]["matched_range"] == "application/json"
    assert body["selected"]["q"] == 0.9
    reasons = {c["media_type"]: c["eliminated_reason"] for c in body["candidates"]}
    assert reasons["text/html"] == "outranked"
    assert reasons["image/png"] == "outranked"
    assert len(body["parsed_accept"]) == 3


def test_endpoint_400_bad_accept():
    resp = post("application/json;q=2.0", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_endpoint_400_wildcard_candidate():
    resp = post("*/*", [{"media_type": "text/*"}])
    assert resp.status_code == 400


def test_endpoint_400_empty_candidates():
    resp = post("*/*", [])
    assert resp.status_code == 400


def test_endpoint_400_malformed_body():
    resp = client.post("/negotiate", json={"accept": "*/*"})
    assert resp.status_code == 400


def test_endpoint_406():
    resp = post("*/*;q=0", [{"media_type": "a/b", "server_priority": 0}])
    assert resp.status_code == 406
    body = resp.json()
    assert body["error"] == "not_acceptable"
    assert body["candidates"][0]["eliminated_reason"] == "q_zero"


def test_endpoint_case_insensitive():
    resp = post(
        "APPLICATION/JSON",
        [{"media_type": "Application/Json", "server_priority": 0}],
    )
    assert resp.status_code == 200
    assert resp.json()["selected"]["media_type"] == "application/json"


def test_health():
    assert client.get("/health").status_code == 200


# ---------- /audit-accept HTTP 接口 ----------


def test_audit_endpoint_success():
    resp = post_audit(
        "text/*;q=0.5, text/html;q=0.9, image/png;q=0.2",
        [
            {"media_type": "text/html", "server_priority": 1},
            {"media_type": "image/png", "server_priority": 1},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    # 审计不返回单一选定项
    assert "selected" not in body
    html, png = body["candidates"]
    assert html["media_type"] == "text/html"
    assert [h["range"] for h in html["matched_ranges"]] == ["text/*", "text/html"]
    assert [h["effective"] for h in html["matched_ranges"]] == [False, True]
    assert html["effective_range"]["range"] == "text/html"
    assert png["effective_range"]["range"] == "image/png"
    # text/* 命中过候选但从未对任何候选生效
    assert [r["range"] for r in body["shadowed"]] == ["text/*"]
    assert len(body["parsed_accept"]) == 3


def test_audit_endpoint_conflict_reported_with_header_indices():
    resp = post_audit(
        "application/json;q=0.3, application/json;q=0.9",
        [{"media_type": "application/json"}],
    )
    assert resp.status_code == 200
    cand = resp.json()["candidates"][0]
    assert cand["effective_range"]["q"] == 0.3
    assert cand["conflicts"] == [
        {
            "specificity": 2,
            "specificity_name": "exact",
            "ranges": [
                {"index": 0, "range": "application/json", "q": 0.3},
                {"index": 1, "range": "application/json", "q": 0.9},
            ],
        }
    ]


def test_audit_endpoint_406_keeps_semantics():
    resp = post_audit("*/*;q=0", [{"media_type": "a/b", "server_priority": 0}])
    assert resp.status_code == 406
    body = resp.json()
    assert body["error"] == "not_acceptable"
    assert body["candidates"][0]["eliminated_reason"] == "q_zero"
    assert body["candidates"][0]["effective_range"]["range"] == "*/*"
    assert body["shadowed"] == []


def test_audit_endpoint_400_bad_accept():
    resp = post_audit("application/json;q=2.0", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_audit_endpoint_400_wildcard_candidate():
    resp = post_audit("*/*", [{"media_type": "text/*"}])
    assert resp.status_code == 400


def test_audit_endpoint_400_empty_candidates():
    resp = post_audit("*/*", [])
    assert resp.status_code == 400


def test_audit_endpoint_400_malformed_body():
    resp = client.post("/audit-accept", json={"accept": "*/*"})
    assert resp.status_code == 400


# ---------- /compare-accept HTTP 接口 ----------


def test_compare_endpoint_reports_selection_change_and_alignment():
    resp = post_compare(
        "application/json;q=0.9, text/*;q=0.5, */*;q=0.1",
        "application/json;q=0.1, text/*;q=0.9, */*;q=0.1",
        [
            {"media_type": "application/json", "server_priority": 1},
            {"media_type": "text/html", "server_priority": 5},
            {"media_type": "image/png", "server_priority": 9},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()

    baseline, trial = body["baseline"], body["trial"]
    assert baseline["status"] == 200
    assert trial["status"] == 200
    assert baseline["error"] is None and trial["error"] is None
    assert baseline["selected"]["media_type"] == "application/json"
    assert trial["selected"]["media_type"] == "text/html"
    assert body["selection_changed"] is True

    # 两侧 parsed_accept 各自反映自己的 Accept 头。
    assert [r["range"] for r in baseline["parsed_accept"]] == [
        "application/json",
        "text/*",
        "*/*",
    ]
    assert trial["parsed_accept"][0]["q"] == 0.1

    alignment = body["candidate_alignment"]
    assert [row["index"] for row in alignment] == [0, 1, 2]
    assert [row["media_type"] for row in alignment] == [
        "application/json",
        "text/html",
        "image/png",
    ]

    json_row, html_row, png_row = alignment
    # json：q 0.9->0.1，由选中变为淘汰
    assert json_row["baseline"]["q"] == 0.9
    assert json_row["baseline"]["selected"] is True
    assert json_row["baseline"]["eliminated_reason"] is None
    assert json_row["trial"]["q"] == 0.1
    assert json_row["trial"]["selected"] is False
    assert json_row["trial"]["eliminated_reason"] == "outranked"
    assert json_row["changed"] is True
    # html：由淘汰变为选中
    assert html_row["baseline"]["selected"] is False
    assert html_row["trial"]["selected"] is True
    assert html_row["changed"] is True
    # png：两侧 q/具体度/原因均未变
    assert png_row["baseline"]["q"] == png_row["trial"]["q"] == 0.1
    assert png_row["baseline"]["specificity"] == png_row["trial"]["specificity"] == 0
    assert (
        png_row["baseline"]["eliminated_reason"]
        == png_row["trial"]["eliminated_reason"]
        == "outranked"
    )
    assert png_row["changed"] is False


def test_compare_endpoint_identical_headers_no_change():
    resp = post_compare(
        "*/*",
        "*/*",
        [{"media_type": "a/b", "server_priority": 1}, {"media_type": "c/d"}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selection_changed"] is False
    assert body["baseline"]["selected"]["index"] == 0
    assert body["trial"]["selected"]["index"] == 0
    assert all(row["changed"] is False for row in body["candidate_alignment"])
    # 对齐行中仅一个 selected，且两侧一致。
    row = body["candidate_alignment"][0]
    assert row["baseline"]["selected"] is True
    assert row["trial"]["selected"] is True
    assert body["candidate_alignment"][1]["baseline"]["selected"] is False


def test_compare_endpoint_specificity_change_marks_row_changed():
    # 基线只有 */*（具体度 0）；试验增加精确范围（具体度 2），选择不变但行发生变化。
    resp = post_compare(
        "*/*",
        "*/*;q=0.1, application/json",
        [{"media_type": "application/json", "server_priority": 0}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selection_changed"] is False
    row = body["candidate_alignment"][0]
    assert row["baseline"]["specificity"] == 0
    assert row["baseline"]["matched_range"] == "*/*"
    assert row["trial"]["specificity"] == 2
    assert row["trial"]["matched_range"] == "application/json"
    assert row["changed"] is True


def test_compare_endpoint_one_side_406_keeps_its_result():
    resp = post_compare(
        "*/*;q=0",
        "*/*",
        [{"media_type": "a/b", "server_priority": 0}],
    )
    # 一侧 406 不影响整体：HTTP 仍为 200，由侧内 status 标记。
    assert resp.status_code == 200
    body = resp.json()

    baseline, trial = body["baseline"], body["trial"]
    assert baseline["status"] == 406
    assert baseline["error"] == "not_acceptable"
    assert baseline["detail"] == "没有可接受的候选表示"
    assert baseline["selected"] is None
    assert baseline["candidates"][0]["eliminated_reason"] == "q_zero"
    assert baseline["parsed_accept"][0]["q"] == 0.0

    assert trial["status"] == 200
    assert trial["error"] is None
    assert trial["selected"]["media_type"] == "a/b"
    assert trial["selected"]["index"] == 0
    assert trial["candidates"][0]["eliminated_reason"] is None

    assert body["selection_changed"] is True
    row = body["candidate_alignment"][0]
    assert row["baseline"]["eligible"] is False
    assert row["baseline"]["selected"] is False
    assert row["baseline"]["eliminated_reason"] == "q_zero"
    assert row["trial"]["eligible"] is True
    assert row["trial"]["selected"] is True
    assert row["changed"] is True


def test_compare_endpoint_both_sides_406_no_selection_change():
    resp = post_compare(
        "text/html",
        "image/png",
        [{"media_type": "application/json", "server_priority": 0}],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline"]["status"] == 406
    assert body["trial"]["status"] == 406
    assert body["selection_changed"] is False
    assert body["candidate_alignment"][0]["baseline"]["eliminated_reason"] == "no_match"
    assert body["candidate_alignment"][0]["trial"]["eliminated_reason"] == "no_match"


def test_compare_endpoint_400_bad_baseline_accept():
    resp = post_compare("application/json;q=2.0", "*/*", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_compare_endpoint_400_bad_trial_accept():
    resp = post_compare("*/*", "application/json,", [{"media_type": "a/b"}])
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


def test_compare_endpoint_400_wildcard_candidate():
    resp = post_compare("*/*", "*/*", [{"media_type": "text/*"}])
    assert resp.status_code == 400


def test_compare_endpoint_400_empty_candidates():
    resp = post_compare("*/*", "*/*", [])
    assert resp.status_code == 400


def test_compare_endpoint_400_malformed_body():
    resp = client.post(
        "/compare-accept", json={"baseline_accept": "*/*", "candidates": []}
    )
    assert resp.status_code == 400
    # 缺字段时由 Pydantic 校验统一 400。
    assert resp.json()["error"] == "bad_request"
