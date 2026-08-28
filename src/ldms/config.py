from __future__ import annotations

import os
import sys
from pathlib import Path

# =========================
# 앱 기본 정보
# =========================
APP_TITLE = "신사업부 LD 모듈팀 통합 시스템"
APP_VERSION = "0.1.0"


# =========================
# 내부 유틸
# =========================
def _documents_root() -> Path:
    """
    ✅ exe에서 저장 실패 시 fallback 루트:
    Documents/LDModuleSystem
    """
    return Path.home() / "Documents" / "LDModuleSystem"


def _is_writable_dir(p: Path) -> bool:
    """
    폴더가 실제로 쓰기 가능한지 테스트
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
        test = p / ".__writetest__"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return True
    except Exception:
        return False


# =========================
# 경로 처리 (개발 / 배포 공통)
# =========================
def get_app_root() -> Path:
    """
    개발 환경: config.py 기준 프로젝트 루트
    PyInstaller exe: 실행파일(.exe) 기준 폴더

    ✅ 단, exe 폴더가 쓰기 불가한 경우를 대비해
    data 저장은 get_data_dir()에서 판단한다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_data_dir() -> Path:
    """
    data 폴더 (원칙: exe 옆 or 프로젝트 루트)

    ✅ exe 빌드 환경에서:
    - exe 옆 data가 쓰기 가능하면 그대로 사용(포터블)
    - 쓰기 불가하면 Documents/LDModuleSystem/data 로 fallback
    """
    base = get_app_root()
    portable = base / "data"

    # 개발환경은 기존대로 프로젝트 루트 data 사용
    if not getattr(sys, "frozen", False):
        portable.mkdir(parents=True, exist_ok=True)
        return portable

    # exe 환경: portable 먼저 시도
    if _is_writable_dir(portable):
        return portable

    # fallback
    fallback = _documents_root() / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_db_path() -> Path:
    """
    SQLite DB 경로 (배포 시 data 폴더)
    """
    return get_data_dir() / "ldms.sqlite3"


# =========================
# logging_conf.py 호환용
# =========================
def get_log_dir() -> Path:
    """
    로그 폴더 경로: data/logs
    """
    log_dir = get_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


LOG_DIR = get_log_dir()


def ensure_dirs() -> None:
    """
    로그/데이터 디렉토리 생성 보장
    """
    get_data_dir().mkdir(parents=True, exist_ok=True)
    get_log_dir().mkdir(parents=True, exist_ok=True)
    get_pms_dir().mkdir(parents=True, exist_ok=True)
    get_reports_dir().mkdir(parents=True, exist_ok=True)


# =========================
# PMS (날짜별 엑셀) 경로
# =========================
def get_pms_dir() -> Path:
    """
    PMS 엑셀 저장 폴더: data/pms
    """
    p = get_data_dir() / "pms"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_pms_daily_xlsx_path(work_date: str) -> Path:
    """
    PMS 날짜별 엑셀 파일: data/pms/YYYY-MM-DD.xlsx
    """
    work_date = str(work_date).strip()
    return get_pms_dir() / f"{work_date}.xlsx"


# =========================
# Reports (주/월간 등 리포트) 경로
# =========================
def get_reports_dir() -> Path:
    """
    리포트 저장 폴더: data/reports
    """
    p = get_data_dir() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p
