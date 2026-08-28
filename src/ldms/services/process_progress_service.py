# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Set
import re
import unicodedata

from src.ldms.process_flow import (
    PROCESS_FLOW,
    SWAPPABLE_PROCESS_GROUPS,
    get_process_flow,
    normalize_process_name as flow_normalize_process_name,
    normalize_model_code,
    DEFAULT_MODEL_CODE,
)


def normalize_module_no(raw: str) -> str:
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw)).strip()
    if not s:
        return ""

    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9A-Za-z]", "", s)
    s = s.upper()

    m = re.match(r"^([A-Z]+)(\d+)$", s)
    if not m:
        return s

    prefix, num_str = m.group(1), m.group(2)
    width = max(3, len(num_str))
    try:
        num_val = int(num_str)
    except Exception:
        return s

    return f"{prefix}{num_val:0{width}d}"


def _base_norm(name: str) -> str:
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"\s+", "", s)
    s = s.upper()
    return s


def canonical_process_name(name: str) -> str:
    """
    PMS 엑셀/구버전 공정명과 현재 공정명을 최대한 통일.
    진행현황 계산은 이 canonical 이름 기준으로만 비교한다.
    """
    s = _base_norm(name)

    alias = {
        "미러정렬": "MIRROR1정렬",
        "MIRROR정렬": "MIRROR1정렬",
        "MIRROR1": "MIRROR1정렬",
        "MIRROR1정렬": "MIRROR1정렬",
        "MIRROR 1정렬": "MIRROR1정렬",
        "MIRROR1ALIGN": "MIRROR1정렬",
        "MIRRORALIGN": "MIRROR1정렬",

        "리듀서정렬": "REDUCER정렬",
        "REDUCER정렬": "REDUCER정렬",
        "REDUCERALIGN": "REDUCER정렬",

        "파이버정렬": "파이버정렬",
        "FIBER정렬": "파이버정렬",
        "FIBERALIGN": "파이버정렬",

        "LID부착": "LID부착",
        "LID착": "LID부착",

        "침본딩": "칩본딩",
        "칩본딩": "칩본딩",
        "CHIPBONDING": "칩본딩",

        "전극바부착": "전극바부착",
        "패키지마킹": "패키지마킹",
        "리플로우": "리플로우",
        "REFLOW": "리플로우",

        "FAC정렬": "FAC정렬",
        "SAC정렬": "SAC정렬",
        "PBS정렬": "PBS정렬",
        "VBG정렬": "VBG정렬",
    }

    if s in alias:
        return alias[s]

    # process_flow.py의 normalize_process_name alias도 한 번 더 태움
    try:
        return flow_normalize_process_name(s)
    except Exception:
        return s


@dataclass(frozen=True)
class ModuleProgress:
    module_no: str
    last_process: str
    next_process: str
    progress_index: int
    progress_ratio: float
    model: str = DEFAULT_MODEL_CODE
    done_count: int = 0
    total_count: int = 0
    last_date: str = ""


def _index_of(proc: str, flow: List[str]) -> int:
    try:
        return flow.index(proc)
    except ValueError:
        return -1


def _get_row_value(row: Dict[str, str], *keys: str) -> str:
    for k in keys:
        if k in row and row.get(k) not in (None, ""):
            return str(row.get(k))
    return ""


def _normalize_flow(flow: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for p in flow or []:
        pn = canonical_process_name(p)
        if pn and pn not in seen:
            seen.add(pn)
            out.append(pn)
    return out


def _swappable_groups_for_flow(flow: List[str]) -> List[Set[str]]:
    out: List[Set[str]] = []
    flow_set = set(flow)
    for group in SWAPPABLE_PROCESS_GROUPS:
        g = {canonical_process_name(p) for p in group}
        g = {p for p in g if p in flow_set}
        if len(g) >= 2:
            out.append(g)
    return out


def compute_progress_from_daily_records(
    daily_records: List[Dict[str, str]],
    model: str = "",
    process_flow: Optional[List[str]] = None,
) -> Tuple[Dict[str, ModuleProgress], Dict[str, List[str]]]:
    """
    모듈별 진행현황 계산.

    핵심 규칙:
    - 입력/작성 순서가 아니라, 모델별 공정순서에서 가장 뒤에 있는 완료 공정을 현재공정으로 판단한다.
    - 따라서 Reducer 정렬을 먼저 입력하고 나중에 VBG 정렬을 입력해도,
      진행현황은 더 뒤 공정인 Reducer 기준으로 유지된다.
    - process_flow 인자가 들어오면 DB/Parameter에서 관리 중인 모델별 공정순서를 우선 사용한다.
    """
    default_model = normalize_model_code(model or DEFAULT_MODEL_CODE)

    if process_flow:
        default_flow = _normalize_flow(process_flow)
    else:
        default_flow = _normalize_flow(get_process_flow(default_model))

    if not default_flow:
        default_flow = _normalize_flow(PROCESS_FLOW)

    done_by_module: Dict[str, set] = {}
    model_by_module: Dict[str, str] = {}
    last_date_by_module: Dict[str, str] = {}

    for row in daily_records:
        m = normalize_module_no(
            _get_row_value(row, "module_no", "Module", "module", "모듈번호")
        )
        p = canonical_process_name(
            _get_row_value(row, "process", "Process", "process_name", "공정명")
        )
        row_model = normalize_model_code(
            _get_row_value(row, "model", "Model", "model_code", "모델", "모델명")
        ) or default_model
        row_date = _get_row_value(row, "date", "work_date", "날짜")

        if not m or not p:
            continue

        # 현재 호출 모델과 다른 모델은 제외
        if row_model != default_model:
            continue

        flow = default_flow
        if p not in flow:
            # 공정순서에 없는 기록은 진행률 판단에서 제외
            continue

        done_by_module.setdefault(m, set()).add(p)
        model_by_module[m] = row_model

        if row_date:
            old = last_date_by_module.get(m, "")
            if not old or row_date > old:
                last_date_by_module[m] = row_date

    module_map: Dict[str, ModuleProgress] = {}
    last_index = len(default_flow) - 1
    swap_groups = _swappable_groups_for_flow(default_flow)

    for m, done in done_by_module.items():
        best_proc = ""
        best_idx = -1

        for p in done:
            idx = _index_of(p, default_flow)
            if idx > best_idx:
                best_idx = idx
                best_proc = p

        if best_idx < 0:
            module_map[m] = ModuleProgress(
                module_no=m,
                last_process="UNKNOWN",
                next_process="UNKNOWN",
                progress_index=-1,
                progress_ratio=0.0,
                model=model_by_module.get(m, default_model),
                done_count=0,
                total_count=len(default_flow),
                last_date=last_date_by_module.get(m, ""),
            )
            continue

        eff_idx = best_idx
        next_proc = "DONE" if best_idx >= last_index else default_flow[best_idx + 1]

        # 스왑 공정 처리: 전극바/칩본딩 둘 중 하나만 완료면 다음공정을 남은 하나로 표시
        for group in swap_groups:
            done_group = done & group
            if best_proc in group and len(done_group) == 1:
                remain = list(group - done_group)
                if remain:
                    next_proc = remain[0]
                    eff_idx = best_idx
            elif len(done_group) == len(group):
                max_group_idx = max(_index_of(p, default_flow) for p in group)
                if best_idx <= max_group_idx:
                    eff_idx = max_group_idx
                    next_proc = "DONE" if max_group_idx >= last_index else default_flow[max_group_idx + 1]

        ratio = 1.0 if str(next_proc).upper() == "DONE" else (eff_idx + 1) / max(1, len(default_flow))
        done_count = len([p for p in done if p in default_flow])

        module_map[m] = ModuleProgress(
            module_no=m,
            last_process=best_proc,
            next_process=next_proc,
            progress_index=eff_idx,
            progress_ratio=ratio,
            model=model_by_module.get(m, default_model),
            done_count=done_count,
            total_count=len(default_flow),
            last_date=last_date_by_module.get(m, ""),
        )

    waiting_by_process: Dict[str, List[str]] = {p: [] for p in default_flow}
    for m, prog in module_map.items():
        nxt = prog.next_process
        if nxt and nxt != "DONE":
            waiting_by_process.setdefault(nxt, []).append(m)

    for p in waiting_by_process:
        waiting_by_process[p] = sorted(set(waiting_by_process[p]))

    return module_map, waiting_by_process
