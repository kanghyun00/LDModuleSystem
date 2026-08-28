# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Tuple


def create_resource_manager():
    """
    VISA ResourceManager 생성.

    우선순위:
    1) PC에 NI-VISA / Keysight VISA 등 시스템 VISA가 있으면 사용
    2) 없으면 배포본에 포함된 pyvisa-py(@py)로 자동 fallback

    따라서 PyInstaller 배포본에 pyvisa + pyvisa-py + pyserial을 포함하면
    배포 PC에 Python/pip/NI-VISA를 따로 설치하지 않아도
    Serial(PSU) 및 TCPIP(예: OSA) 리소스를 열 수 있다.

    주의:
    Windows가 장비를 인식하기 위한 제조사/USB 드라이버는 별도일 수 있다.
    """
    import pyvisa

    errors = []

    try:
        return pyvisa.ResourceManager()
    except Exception as e:
        errors.append(f"시스템 VISA: {e}")

    try:
        return pyvisa.ResourceManager("@py")
    except Exception as e:
        errors.append(f"pyvisa-py: {e}")

    raise RuntimeError(
        "사용 가능한 VISA backend를 열 수 없습니다.\n"
        + "\n".join(f"- {msg}" for msg in errors)
    )
