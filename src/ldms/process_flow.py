# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Dict


def _base_norm(name: str) -> str:
    """
    공정명 기본 정규화:
    - 유니코드 정규화(NFKC)
    - 공백 제거
    - 구분기호 제거
    - 영문 대문자 통일
    """
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[·•\-_\/]+", "", s)
    s = s.upper()
    return s


# 과거/별칭 공정명 치환 맵
_ALIAS_MAP: dict[str, str] = {
    _base_norm("어셈블리"): _base_norm("Mirror 1 정렬"),
    _base_norm("미러정렬"): _base_norm("Mirror 1 정렬"),
    _base_norm("MIRROR정렬"): _base_norm("Mirror 1 정렬"),
    _base_norm("MIRROR1"): _base_norm("Mirror 1 정렬"),
    _base_norm("MIRROR 1"): _base_norm("Mirror 1 정렬"),
    _base_norm("MIRROR1정렬"): _base_norm("Mirror 1 정렬"),
    _base_norm("MIRROR 1정렬"): _base_norm("Mirror 1 정렬"),
    _base_norm("리듀서정렬"): _base_norm("Reducer 정렬"),
    _base_norm("REDUCERALIGN"): _base_norm("Reducer 정렬"),
    _base_norm("FIBER정렬"): _base_norm("파이버 정렬"),
    _base_norm("FIBERALIGN"): _base_norm("파이버 정렬"),
    _base_norm("침본딩"): _base_norm("칩 본딩"),
}


def normalize_process_name(name: str) -> str:
    """
    공정명 정규화(진행현황/DB 기준):
    - 기본 정규화 후 alias 치환
    """
    s = _base_norm(name)
    return _ALIAS_MAP.get(s, s)


PROCESS_FLOW = [
    normalize_process_name("패키지 마킹"),
    normalize_process_name("리플로우"),
    normalize_process_name("전극바 부착"),
    normalize_process_name("칩 본딩"),
    normalize_process_name("FAC 정렬"),
    normalize_process_name("SAC 정렬"),
    normalize_process_name("PBS 정렬"),
    normalize_process_name("Mirror 1 정렬"),
    normalize_process_name("VBG 정렬"),
    normalize_process_name("Reducer 정렬"),
    normalize_process_name("파이버 정렬"),
    normalize_process_name("LID 부착"),
]

# 전극바부착/칩본딩은 순서가 바뀔 수 있는 "스왑 가능 구간"
SWAPPABLE_PROCESS_GROUPS = [
    {normalize_process_name("전극바 부착"), normalize_process_name("칩 본딩")},
]


DEFAULT_MODEL_CODE = "F976370W"


def normalize_model_code(model: str) -> str:
    s = "" if model is None else str(model).strip().upper()
    s = re.sub(r"\s+", "", s)
    return s or DEFAULT_MODEL_CODE


@dataclass(frozen=True)
class ModelRecipe:
    model_code: str
    display_name: str
    max_current_a: float
    # 하위호환/완성품 1A 간격용. PBS 측정은 pbs_points를 사용한다.
    pbs_step_a: float
    pbs_points: List[float]
    vbg_points: List[float]
    burnin_current_a: float
    process_flow: List[str]
    # 자동측정 시작 시 처음 빔/파장 확인에 사용하는 전류
    beam_check_current_a: float = 0.8
    # 자동측정 타이밍 기본값
    # - measure_step_wait_s: 측정 지점에서 대기 후 데이터 저장
    # - max_hold_wait_s: Max A 지점에서 대기 후 데이터 저장/전류내림
    measure_step_wait_s: float = 10.0
    max_hold_wait_s: float = 15.0


# 모델별 기본 Recipe
# 주의: Parameter 탭의 "기본 모델 생성/복구"는 이 값을 기준으로 DB를 복구한다.
MODEL_RECIPES: Dict[str, ModelRecipe] = {
    "F9769W": ModelRecipe(
        model_code="F9769W",
        display_name="F9769W / 9W",
        max_current_a=13.0,
        pbs_step_a=1.0,
        pbs_points=list(range(1, 14)),
        vbg_points=[1, 2, 3, 5, 8, 10, 13],
        burnin_current_a=13.0,
        process_flow=PROCESS_FLOW,
        beam_check_current_a=0.8,
        measure_step_wait_s=10.0,
        max_hold_wait_s=15.0,
    ),
    "F97645W": ModelRecipe(
        model_code="F97645W",
        display_name="F97645W / 45W",
        max_current_a=13.0,
        pbs_step_a=1.0,
        pbs_points=list(range(1, 14)),
        vbg_points=[1, 2, 3, 5, 8, 10, 13],
        burnin_current_a=13.0,
        process_flow=PROCESS_FLOW,
        beam_check_current_a=0.8,
        measure_step_wait_s=10.0,
        max_hold_wait_s=15.0,
    ),
    "F976370W": ModelRecipe(
        model_code="F976370W",
        display_name="F976370W / 370W",
        max_current_a=20.0,
        pbs_step_a=1.0,
        pbs_points=list(range(1, 21)),
        vbg_points=[2, 5, 10, 15, 16, 17, 20],
        burnin_current_a=20.0,
        process_flow=PROCESS_FLOW,
        beam_check_current_a=0.8,
        measure_step_wait_s=10.0,
        max_hold_wait_s=15.0,
    ),
    "F976550W": ModelRecipe(
        model_code="F976550W",
        display_name="F976550W / 550W",
        max_current_a=25.0,
        pbs_step_a=1.0,
        pbs_points=list(range(1, 26)),
        vbg_points=[2, 5, 10, 15, 20, 25],
        burnin_current_a=25.0,
        process_flow=PROCESS_FLOW,
        beam_check_current_a=0.8,
        measure_step_wait_s=10.0,
        max_hold_wait_s=15.0,
    ),
    "F976700W": ModelRecipe(
        model_code="F976700W",
        display_name="F976700W / 700W",
        max_current_a=30.0,
        pbs_step_a=1.0,
        pbs_points=list(range(1, 31)),
        vbg_points=[2, 5, 10, 15, 20, 25, 30],
        burnin_current_a=30.0,
        process_flow=PROCESS_FLOW,
        beam_check_current_a=0.8,
        measure_step_wait_s=10.0,
        max_hold_wait_s=15.0,
    ),
}


def list_model_codes() -> List[str]:
    return list(MODEL_RECIPES.keys())


def get_model_recipe(model_code: str) -> ModelRecipe:
    code = normalize_model_code(model_code)
    return MODEL_RECIPES.get(code, MODEL_RECIPES[DEFAULT_MODEL_CODE])


def get_process_flow(model_code: str = DEFAULT_MODEL_CODE) -> List[str]:
    recipe = get_model_recipe(model_code)
    return list(recipe.process_flow or PROCESS_FLOW)
