"""FastAPI 服务：HTTP 内容协商（Accept）调试器。"""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .negotiation import (
    AcceptSyntaxError,
    AcceptRange,
    CandidateResult,
    SPECIFICITY_NAMES,
    audit_accept,
    negotiate,
)

logger = logging.getLogger("content-negotiation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动初始化：仅记录就绪日志；依赖安装已在镜像构建阶段完成。
    logger.info(
        "内容协商调试服务已就绪：POST /negotiate, POST /audit-accept, POST /compare-accept"
    )
    yield
    logger.info("内容协商调试服务关闭")


app = FastAPI(
    title="HTTP 内容协商调试器",
    description="调试 Accept 头与候选表示之间的匹配、q 值与排序过程；支持静态审计。",
    version="1.0.0",
    lifespan=lifespan,
)


class CandidateIn(BaseModel):
    media_type: str = Field(..., description="具体的候选媒体类型，如 application/json")
    server_priority: float = Field(
        0.0, description="服务端优先级，数值越大越优先"
    )


class NegotiateRequest(BaseModel):
    accept: str = Field(..., description="原始 Accept 头字符串")
    candidates: list[CandidateIn] = Field(..., description="候选表示列表（有序）")


class SelectedOut(BaseModel):
    index: int
    media_type: str
    server_priority: float
    matched_range: str
    q: float
    specificity: int
    specificity_name: str


class NegotiateResponse(BaseModel):
    selected: SelectedOut
    candidates: list[dict]
    parsed_accept: list[dict]


class RangeHitOut(BaseModel):
    index: int
    range: str
    q: float
    specificity: int
    specificity_name: str
    effective: bool


class ConflictOut(BaseModel):
    specificity: int
    specificity_name: str
    ranges: list[dict]


class AuditCandidateOut(BaseModel):
    index: int
    media_type: str
    server_priority: float
    eligible: bool
    eliminated_reason: str | None
    effective_range: dict | None
    matched_ranges: list[RangeHitOut]
    conflicts: list[ConflictOut]


class AuditResponse(BaseModel):
    candidates: list[AuditCandidateOut]
    shadowed: list[dict]
    parsed_accept: list[dict]


class CompareAcceptRequest(BaseModel):
    baseline_accept: str = Field(..., description="基线（规则调整前）原始 Accept 头字符串")
    trial_accept: str = Field(..., description="试验（规则调整后）原始 Accept 头字符串")
    candidates: list[CandidateIn] = Field(..., description="两侧共用的候选表示列表（有序）")


class AlignedCandidateSideOut(BaseModel):
    """按候选 index 对齐后的单侧评估视图。"""

    matched_range: str | None
    q: float | None
    specificity: int | None
    specificity_name: str | None
    eligible: bool
    eliminated_reason: str | None
    selected: bool


class AlignedCandidateOut(BaseModel):
    index: int
    media_type: str
    baseline: AlignedCandidateSideOut
    trial: AlignedCandidateSideOut
    changed: bool


class CompareSideOut(BaseModel):
    # 单侧协商结果：200 时 error/detail 为 null；406 时保留与 /negotiate 一致的错误体。
    status: int
    error: str | None
    detail: str | None
    selected: SelectedOut | None
    candidates: list[dict]
    parsed_accept: list[dict]


class CompareAcceptResponse(BaseModel):
    baseline: CompareSideOut
    trial: CompareSideOut
    candidate_alignment: list[AlignedCandidateOut]
    selection_changed: bool


class ErrorResponse(BaseModel):
    error: str
    detail: str


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "detail": message},
    )


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 请求体结构/类型错误统一返回 400（非法请求语法）。
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "detail": "请求体格式非法", "errors": exc.errors()},
    )


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "http-content-negotiation-debugger",
        "docs": "/docs",
        "endpoints": [
            "POST /negotiate",
            "POST /audit-accept",
            "POST /compare-accept",
        ],
    }


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


def _accept_to_dict(r: AcceptRange) -> dict:
    return {
        "index": r.index,
        "range": r.raw,
        "type": r.type,
        "subtype": r.subtype,
        "q": r.q,
        "specificity": r.specificity,
        "specificity_name": SPECIFICITY_NAMES[r.specificity],
    }


def _validate_candidates(candidates: list[CandidateIn]) -> JSONResponse | None:
    # 业务层校验：候选列表不能为空，server_priority 必须为有限数。
    if not candidates:
        return _bad_request("candidates 不能为空")
    for i, cand in enumerate(candidates):
        if not math.isfinite(cand.server_priority):
            return _bad_request(f"candidates[{i}].server_priority 必须是有限数")
    return None


def _selected_payload(chosen: CandidateResult) -> dict:
    return {
        "index": chosen.index,
        "media_type": chosen.media_type,
        "server_priority": chosen.server_priority,
        "matched_range": chosen.matched_range,
        "q": chosen.q,
        "specificity": chosen.specificity,
        "specificity_name": chosen.specificity_name,
    }


def _negotiate_side(
    accept: str, raw_candidates: list[tuple[str, float]]
) -> dict[str, Any]:
    """按现有规则对单侧 Accept 执行协商并组装完整结果体。

    返回含 status/error/detail/selected/candidates/parsed_accept 的 dict，
    供 /negotiate 与 /compare-accept 复用；Accept 非法由调用方统一转 400。
    """
    selected_index, results, accept_ranges = negotiate(accept, raw_candidates)
    candidate_payload = [r.to_dict() for r in results]
    parsed_accept = [_accept_to_dict(r) for r in accept_ranges]

    if selected_index is None:
        # q=0 排除或无范围命中：协商失败，保留候选明细。
        return {
            "status": 406,
            "error": "not_acceptable",
            "detail": "没有可接受的候选表示",
            "selected": None,
            "candidates": candidate_payload,
            "parsed_accept": parsed_accept,
        }

    return {
        "status": 200,
        "error": None,
        "detail": None,
        "selected": _selected_payload(results[selected_index]),
        "candidates": candidate_payload,
        "parsed_accept": parsed_accept,
    }


def _aligned_view(result: dict, selected_index: int | None) -> dict:
    """单侧单个候选的对齐视图：在协商明细上附加 selected 标记。"""
    return {
        "matched_range": result["matched_range"],
        "q": result["q"],
        "specificity": result["specificity"],
        "specificity_name": result["specificity_name"],
        "eligible": result["eligible"],
        "eliminated_reason": result["eliminated_reason"],
        "selected": result["index"] == selected_index,
    }


def _alignment_fields(view: dict) -> tuple:
    """参与逐候选变化比较的字段。"""
    return (
        view["matched_range"],
        view["q"],
        view["specificity"],
        view["eligible"],
        view["eliminated_reason"],
        view["selected"],
    )


def _build_alignment(baseline: dict, trial: dict, n: int) -> list[dict]:
    """按候选 index 对齐两侧评估，逐候选标注 changed。"""
    base_by_index = {c["index"]: c for c in baseline["candidates"]}
    trial_by_index = {c["index"]: c for c in trial["candidates"]}
    base_selected = baseline["selected"]["index"] if baseline["selected"] else None
    trial_selected = trial["selected"]["index"] if trial["selected"] else None

    alignment: list[dict] = []
    for index in range(n):
        base_view = _aligned_view(base_by_index[index], base_selected)
        trial_view = _aligned_view(trial_by_index[index], trial_selected)
        alignment.append(
            {
                "index": index,
                "media_type": base_by_index[index]["media_type"],
                "baseline": base_view,
                "trial": trial_view,
                "changed": _alignment_fields(base_view)
                != _alignment_fields(trial_view),
            }
        )
    return alignment


@app.post(
    "/negotiate",
    response_model=NegotiateResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Accept 头或候选非法"},
        406: {"model": ErrorResponse, "description": "无可用候选表示"},
    },
)
async def do_negotiate(payload: NegotiateRequest):
    invalid = _validate_candidates(payload.candidates)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]
    try:
        side = _negotiate_side(payload.accept, raw_candidates)
    except AcceptSyntaxError as exc:
        return _bad_request(str(exc))

    if side["status"] == 406:
        # q=0 排除或无范围命中：协商失败。
        return JSONResponse(
            status_code=406,
            content={
                "error": side["error"],
                "detail": side["detail"],
                "candidates": side["candidates"],
                "parsed_accept": side["parsed_accept"],
            },
        )

    return {
        "selected": side["selected"],
        "candidates": side["candidates"],
        "parsed_accept": side["parsed_accept"],
    }


@app.post(
    "/audit-accept",
    response_model=AuditResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Accept 头或候选非法"},
        406: {"model": ErrorResponse, "description": "无可用候选表示"},
    },
)
async def do_audit_accept(payload: NegotiateRequest):
    # 静态审计：与 /negotiate 相同的校验与匹配语义，但不返回单一选定项。
    invalid = _validate_candidates(payload.candidates)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]
    try:
        audits, shadowed, accept_ranges = audit_accept(payload.accept, raw_candidates)
    except AcceptSyntaxError as exc:
        return _bad_request(str(exc))

    body = {
        "candidates": [a.to_dict() for a in audits],
        "shadowed": [_accept_to_dict(r) for r in shadowed],
        "parsed_accept": [_accept_to_dict(r) for r in accept_ranges],
    }

    if not any(a.eligible for a in audits):
        # 与 /negotiate 一致：无可用候选（全部 q=0 或无命中）时返回 406，
        # 响应体仍带完整审计明细。
        return JSONResponse(
            status_code=406,
            content={
                "error": "not_acceptable",
                "detail": "没有可接受的候选表示",
                **body,
            },
        )
    return body


@app.post(
    "/compare-accept",
    response_model=CompareAcceptResponse,
    responses={
        400: {"model": ErrorResponse, "description": "任一 Accept 头或候选非法"},
    },
)
async def do_compare_accept(payload: CompareAcceptRequest):
    # 同一组候选表示上分别套用两条 Accept；任一输入非法（含候选）沿用 400，
    # 不返回任何一侧结果。
    invalid = _validate_candidates(payload.candidates)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]
    try:
        baseline = _negotiate_side(payload.baseline_accept, raw_candidates)
        trial = _negotiate_side(payload.trial_accept, raw_candidates)
    except AcceptSyntaxError as exc:
        return _bad_request(str(exc))

    # 某一侧 406 时保留该侧结果与候选明细（由 status/error/detail 标记），
    # 不影响另一侧比较，整体响应仍为 200。
    baseline_selected = (
        baseline["selected"]["index"] if baseline["selected"] is not None else None
    )
    trial_selected = (
        trial["selected"]["index"] if trial["selected"] is not None else None
    )

    return {
        "baseline": baseline,
        "trial": trial,
        "candidate_alignment": _build_alignment(
            baseline, trial, len(raw_candidates)
        ),
        "selection_changed": baseline_selected != trial_selected,
    }
