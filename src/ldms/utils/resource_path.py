# src/ldms/utils/resource_path.py
from __future__ import annotations
import os
import sys

def resource_path(relative_path: str) -> str:
    """
    PyInstaller --onefile 환경에서도 assets 같은 리소스를 찾기 위한 경로 헬퍼
    """
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS  # PyInstaller가 임시로 풀어놓는 경로
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)
