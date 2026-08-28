# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any

SETTINGS_KEY_PATHS = "path_settings"


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


@dataclass
class PathSettings:
    """앱 전반에서 사용하는 저장/검색 경로 설정."""

    # ===== 출력/저장 폴더 =====
    export_dir_measure: str = ""
    export_dir_burnin: str = ""
    export_dir_process: str = ""

    # 비어 있으면 export_dir_process를 사용한다.
    reports_dir: str = ""

    logs_dir: str = ""
    import_dir_default: str = ""

    # 20A 등급분류 엑셀 저장 기본 폴더
    export_dir_20a_grading: str = ""

    # 검색/비교 탭 Stage/Group 스캔 RAW ROOT
    # 비어 있으면 SearchTab에서 export_dir_measure를 사용한다.
    search_compare_raw_root: str = ""


def load_path_settings(repo) -> PathSettings:
    try:
        raw = repo.get_setting(SETTINGS_KEY_PATHS, "{}")
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    settings = PathSettings()
    if isinstance(data, dict):
        # 기존 DB에 더 이상 사용하지 않는 구형 키가 남아 있어도
        # 현재 PathSettings 필드만 읽고 나머지는 자동으로 무시한다.
        for key in asdict(settings).keys():
            if key in data:
                setattr(
                    settings,
                    key,
                    _safe_str(data.get(key), getattr(settings, key)),
                )
    return settings


def save_path_settings(repo, settings: PathSettings) -> None:
    # 저장할 때 현재 필드만 기록하므로 구형 Graphtec/StarLab 경로 키는 제거된다.
    repo.set_setting(
        SETTINGS_KEY_PATHS,
        json.dumps(asdict(settings), ensure_ascii=False),
    )


def expand_default_documents_path(*parts: str) -> str:
    """EXE에서도 안전한 기본 루트: Documents\\LDModuleSystem\\..."""
    home = os.path.expanduser("~")
    documents = os.path.join(home, "Documents")
    return os.path.join(documents, "LDModuleSystem", *parts)


def ensure_dir(path: str) -> str:
    path = path.strip()
    if not path:
        return path
    os.makedirs(path, exist_ok=True)
    return path


def normalize_and_fill_defaults(settings: PathSettings) -> PathSettings:
    """비어 있는 출력 경로를 Documents 기반 기본값으로 채운다."""
    if not settings.export_dir_measure:
        settings.export_dir_measure = expand_default_documents_path(
            "data", "measurements", "raw"
        )
    if not settings.export_dir_burnin:
        settings.export_dir_burnin = expand_default_documents_path(
            "data", "burnin", "raw"
        )
    if not settings.export_dir_process:
        settings.export_dir_process = expand_default_documents_path(
            "data", "process"
        )

    if not settings.reports_dir:
        settings.reports_dir = settings.export_dir_process

    if not settings.logs_dir:
        settings.logs_dir = expand_default_documents_path("logs")
    if not settings.import_dir_default:
        settings.import_dir_default = os.path.expanduser("~")

    if not settings.export_dir_20a_grading:
        settings.export_dir_20a_grading = expand_default_documents_path(
            "data", "measurements", "grading"
        )

    # search_compare_raw_root는 비어 있으면 SearchTab이
    # export_dir_measure를 사용하므로 강제로 채우지 않는다.
    return settings
