# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import math
import time
import threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import numpy as np
import pandas as pd

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QInputDialog, QFileDialog, QMessageBox, QComboBox, QLineEdit
)

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.ticker import MultipleLocator, AutoMinorLocator

from scipy.signal import find_peaks

# ✅ Parameter 탭에서 저장한 기본경로 적용
from src.ldms.app_config import load_path_settings, normalize_and_fill_defaults
from src.ldms.process_flow import DEFAULT_MODEL_CODE, get_model_recipe, list_model_codes, normalize_model_code
from src.ldms.utils.visa_backend import create_resource_manager
from src.ldms.ui.tools.direct_measurement_devices import (
    read_nova_power_direct,
    read_graphtec_temperatures_direct,
)


MEASURE_STAGE_FOLDERS = [
    "1. PBS 정렬 후",
    "2. VBG 정렬 후 오븐 전",
    "3. VBG 정렬 후 오븐 후",
    "4. beam reducer 오븐 후",
    "5. Fiber 정렬 후_오븐 전",
    "6. Fiber 정렬 후_오븐 후",
    "7. 완성품",
]


def _project_measure_raw_root() -> str:
    """
    코드 하드코딩 경로가 아니라 현재 프로젝트 기준 기본 raw 경로.
    Parameter 탭 경로가 비어있을 때만 fallback으로 사용.
    """
    try:
        return str(Path(__file__).resolve().parents[4] / "data" / "measurements" / "raw")
    except Exception:
        return str(Path.cwd() / "data" / "measurements" / "raw")


def _extract_module_prefix(module_no: str) -> str:
    m = re.match(r"^([A-Z]+)", (module_no or "").strip().upper())
    return m.group(1) if m else ""


def _default_group_from_module(module_no: str) -> str:
    """
    기존 운영 방식 호환:
    A월/B월/C월처럼 모듈번호 앞 알파벳 기준으로 월별 그룹 폴더 생성.
    예: E092 -> 2026-E

    모듈번호가 비어 있으면 강제로 2026 같은 그룹을 만들지 않고,
    기존 폴더 목록을 그대로 보여준다.
    """
    prefix = _extract_module_prefix(module_no)
    year = datetime.now().year
    return f"{year}-{prefix}" if prefix else ""


try:
    from scipy.integrate import simpson as _simpson
except Exception:  # pragma: no cover
    _simpson = None

try:
    from scipy.integrate import simps as _simps_legacy
except Exception:  # pragma: no cover
    _simps_legacy = None


# OSA 기본 주소
DEFAULT_OSA_ADDR = "TCPIP0::192.168.0.10::inst0::INSTR"


def dbm_to_nw(dbm):
    return 10 ** (np.array(dbm) / 10) * 1e6


def _integrate(y: np.ndarray, x: np.ndarray) -> float:
    if _simpson is not None:
        return float(_simpson(y, x))
    if _simps_legacy is not None:
        return float(_simps_legacy(y, x))
    return float(np.trapz(y, x))


def calc_power_fraction(wavelengths: np.ndarray, powers: np.ndarray, wl_min: float, wl_max: float) -> float:
    total_power = _integrate(powers, wavelengths)
    mask = (wavelengths >= wl_min) & (wavelengths <= wl_max)
    if not np.any(mask):
        return 0.0
    power_in_range = _integrate(powers[mask], wavelengths[mask])
    return (power_in_range / total_power) * 100.0 if total_power != 0 else 0.0


# =========================================================
# ✅ LIV Resample (엑셀 저장 시 0~ampsA, 1A 포인트 강제 생성)
#    - 0A, 1A Power는 무조건 0W
# =========================================================
def _resample_liv_0toMax_step1(
    cur_arr: np.ndarray,
    pow_arr: np.ndarray,
    volt_arr: np.ndarray,
    max_a: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    엑셀 LIV 커브용 데이터 재샘플링:
      - 0~max_a A를 1A 간격으로 생성 (예: max_a=16 -> 0..16)
      - 측정 포인트가 듬성듬성이어도 선형보간
      - NaN/비정상 값은 제외하고 보간
      - 0A, 1A Power(W)는 무조건 0W로 고정 (요청사항)

    반환: (cur_grid, pow_grid, volt_grid)
    """
    max_a = int(max_a)
    cur_grid = np.arange(0, max_a + 1, 1, dtype=float)

    if cur_arr is None or pow_arr is None or volt_arr is None:
        pow_grid = np.zeros_like(cur_grid)
        volt_grid = np.zeros_like(cur_grid)
        return cur_grid, pow_grid, volt_grid

    cur_arr = np.array(cur_arr, dtype=float)
    pow_arr = np.array(pow_arr, dtype=float)
    volt_arr = np.array(volt_arr, dtype=float)

    # 유효 포인트(전류/파워) 필터: -1000 같은 에러값 제외
    valid_p = (~np.isnan(cur_arr)) & (~np.isnan(pow_arr)) & (pow_arr > -1000)
    valid_v = (~np.isnan(cur_arr)) & (~np.isnan(volt_arr))

    def _dedup_last(x, y):
        tmp = {}
        for xi, yi in zip(x, y):
            tmp[float(xi)] = float(yi)
        xs = np.array(sorted(tmp.keys()), dtype=float)
        ys = np.array([tmp[k] for k in xs], dtype=float)
        return xs, ys

    # --- Power 보간 ---
    x_p = cur_arr[valid_p]
    y_p = pow_arr[valid_p]
    if x_p.size >= 1:
        x_p, y_p = _dedup_last(x_p, y_p)

        # ✅ 0A, 1A 기준점 강제 추가 + 0W 강제
        # (없으면 추가 / 있으면 값 강제)
        def _ensure_point(xv, yv, x0, y0):
            if np.any(np.isclose(xv, x0)):
                idx = int(np.where(np.isclose(xv, x0))[0][-1])
                yv[idx] = float(y0)
                return xv, yv
            xv = np.insert(xv, 0, float(x0))
            yv = np.insert(yv, 0, float(y0))
            # 다시 정렬
            order = np.argsort(xv)
            return xv[order], yv[order]

        x_p, y_p = _ensure_point(x_p, y_p, 0.0, 0.0)
        x_p, y_p = _ensure_point(x_p, y_p, 1.0, 0.0)

        pow_grid = np.interp(cur_grid, x_p, y_p, left=y_p[0], right=y_p[-1])
    else:
        pow_grid = np.zeros_like(cur_grid)

    # ✅ 0A, 1A Power 무조건 0W (최종 클램프)
    pow_grid[cur_grid <= 1.0] = 0.0

    # --- Voltage 보간(요청은 0/1A Power만, Voltage는 기존처럼 보간 유지) ---
    x_v = cur_arr[valid_v]
    y_v = volt_arr[valid_v]
    if x_v.size >= 1:
        x_v, y_v = _dedup_last(x_v, y_v)

        # 0A가 없으면 0V 기준점만 넣어줌(안정)
        if not np.any(np.isclose(x_v, 0.0)):
            x_v = np.insert(x_v, 0, 0.0)
            y_v = np.insert(y_v, 0, 0.0)
            order = np.argsort(x_v)
            x_v, y_v = x_v[order], y_v[order]

        volt_grid = np.interp(cur_grid, x_v, y_v, left=y_v[0], right=y_v[-1])
    else:
        volt_grid = np.zeros_like(cur_grid)

    return cur_grid, pow_grid, volt_grid


class PowerSupplyController:
    """
    PSU VISA/Serial 컨트롤러.

    핵심 개선점
    - TekVISA(system VISA)가 ASRL 포트를 못 찾는 경우 pyvisa-py(@py)로 자동 재시도.
    - 포트가 단순히 열리는 것만으로 연결 성공 처리하지 않음.
    - 9600 / 8N1 / LF / timeout 설정 후 실제 MEAS:CURR? / MEAS:VOLT? 응답 확인.
    - 첫 쿼리 timeout을 고려해 자동 재시도.
    - 측정 중 일시적인 timeout에도 current/voltage 읽기를 자동 재시도.
    """

    BAUD_RATE = 9600
    TIMEOUT_MS = 3000
    OPEN_SETTLE_S = 0.8
    QUERY_RETRIES = 3
    QUERY_RETRY_DELAY_S = 0.35
    WRITE_RETRIES = 3
    WRITE_RETRY_DELAY_S = 0.20

    def __init__(self, visa_addr: str):
        self.visa_addr = self._normalize_addr(visa_addr)
        self.rm = None
        self.inst = None
        self._closed = True
        self.backend_name = ""
        self.last_verified_current = float("nan")
        self.last_verified_voltage = float("nan")
        # 하나의 VISA 세션을 여러 스레드가 동시에 건드리지 않도록 직렬화
        self._io_lock = threading.RLock()

        try:
            (
                self.rm,
                self.inst,
                self.backend_name,
                current_a,
                voltage_v,
                detail,
            ) = self._open_verified_session(self.visa_addr)

            self._closed = False
            self.last_verified_current = current_a
            self.last_verified_voltage = voltage_v

            # 보호상태 해제는 지원되는 장비에서만 수행.
            try:
                self.inst.write("OUTP:PROT:CLE")
            except Exception:
                pass

            print(
                f"PSU 연결 확인 성공: {self.visa_addr} | "
                f"I={self._fmt_number(current_a)}A | "
                f"V={self._fmt_number(voltage_v)}V | "
                f"Backend={self.backend_name}"
            )
            if detail:
                print(f"PSU 연결 상세: {detail}")

        except Exception as e:
            self._cleanup_resources()
            raise RuntimeError(f"PSU 연결 실패: {e}") from e

    @staticmethod
    def _normalize_addr(user_input: str) -> str:
        s = (user_input or "").strip().upper().replace(" ", "")
        if s.startswith("ASRL") and s.endswith("::INSTR"):
            return s
        if s.startswith("ASRL") and not s.endswith("::INSTR"):
            m = re.search(r"ASRL(\d+)", s)
            if m:
                return f"ASRL{m.group(1)}::INSTR"
        m = re.search(r"(\d+)", s)
        if m:
            return f"ASRL{m.group(1)}::INSTR"
        return s

    @classmethod
    def _configure_serial_resource(cls, inst, pyvisa_module) -> None:
        """PSU에서 사용하는 기존 시리얼 조건을 동일하게 적용."""
        inst.baud_rate = cls.BAUD_RATE
        inst.data_bits = 8
        inst.stop_bits = pyvisa_module.constants.StopBits.one
        inst.parity = pyvisa_module.constants.Parity.none
        inst.write_termination = "\n"
        inst.read_termination = "\n"
        inst.timeout = cls.TIMEOUT_MS

    @staticmethod
    def _parse_numeric_response(response) -> float:
        text = "" if response is None else str(response)
        cleaned = re.sub(r"[^\d\.\-eE+]", "", text)
        if cleaned in ("", "+", "-", ".", "+.", "-."):
            raise ValueError(f"숫자 응답이 아닙니다: {response!r}")
        return float(cleaned)

    @staticmethod
    def _fmt_number(value: float) -> str:
        try:
            x = float(value)
            if np.isnan(x):
                return "NaN"
            return f"{x:.6g}"
        except Exception:
            return "NaN"

    @classmethod
    def _resource_manager_candidates(cls):
        """
        PSU용 VISA backend 후보를 반환한다.

        1) 시스템 VISA(TekVISA/NI-VISA 등)
        2) pyvisa-py(@py)

        시스템 VISA 자체는 열리지만 ASRL5를 못 찾는 경우가 있으므로,
        ResourceManager 생성 성공만으로 끝내지 않고 실제 포트 open/응답까지
        backend별로 확인한다.
        """
        import pyvisa

        candidates = []
        errors = []
        seen = set()

        for label, factory in (
            ("system VISA", lambda: pyvisa.ResourceManager()),
            ("pyvisa-py", lambda: pyvisa.ResourceManager("@py")),
        ):
            rm = None
            try:
                rm = factory()
                backend = str(getattr(rm, "visalib", ""))
                key = backend or label
                if key in seen:
                    try:
                        rm.close()
                    except Exception:
                        pass
                    continue
                seen.add(key)
                candidates.append((label, backend, rm))
            except Exception as e:
                errors.append(f"{label} ResourceManager 생성 실패: {e}")
                try:
                    if rm is not None:
                        rm.close()
                except Exception:
                    pass

        return candidates, errors

    @classmethod
    def list_serial_resources(cls):
        """system VISA와 pyvisa-py 양쪽에서 감지되는 ASRL 리소스를 합쳐 반환."""
        found = []
        backend_notes = []
        candidates, init_errors = cls._resource_manager_candidates()
        backend_notes.extend(init_errors)

        for label, backend, rm in candidates:
            try:
                resources = tuple(rm.list_resources())
                serials = [r for r in resources if str(r).upper().startswith("ASRL")]
                backend_notes.append(
                    f"{label} ({backend or '-'}) -> {serials if serials else 'ASRL 없음'}"
                )
                for r in serials:
                    if r not in found:
                        found.append(r)
            except Exception as e:
                backend_notes.append(f"{label} 리소스 조회 실패: {e}")
            finally:
                try:
                    rm.close()
                except Exception:
                    pass

        return found, backend_notes

    @classmethod
    def _query_resource_float(
        cls,
        inst,
        command: str,
        retries: int | None = None,
        delay_s: float | None = None,
    ):
        """
        특정 VISA resource에 query를 보내 숫자 응답을 얻는다.
        반환: (value, last_error)
        """
        import time

        tries = max(1, int(retries if retries is not None else cls.QUERY_RETRIES))
        delay = float(delay_s if delay_s is not None else cls.QUERY_RETRY_DELAY_S)
        last_error = None

        for attempt in range(1, tries + 1):
            try:
                response = inst.query(command)
                value = cls._parse_numeric_response(response)
                return float(value), None
            except Exception as e:
                last_error = e
                print(f"PSU {command} {attempt}/{tries} 실패: {e}")
                try:
                    inst.clear()
                except Exception:
                    pass
                if attempt < tries:
                    time.sleep(delay)

        return float("nan"), last_error

    @classmethod
    def _open_verified_session(cls, visa_addr: str):
        """
        backend를 순서대로 시도해 실제 PSU 응답이 확인되는 세션을 반환한다.

        반환:
            (rm, inst, backend_name, current_a, voltage_v, detail)
        """
        import pyvisa
        import time

        addr = cls._normalize_addr(visa_addr)
        candidates, init_errors = cls._resource_manager_candidates()
        all_errors = list(init_errors)

        if not candidates:
            raise RuntimeError("사용 가능한 VISA backend가 없습니다. " + " | ".join(all_errors))

        for label, backend, rm in candidates:
            inst = None
            resources = ()
            try:
                try:
                    resources = tuple(rm.list_resources())
                except Exception as e:
                    all_errors.append(f"{label} 리소스 조회 실패: {e}")

                print(
                    f"PSU backend 시도: {label} | Backend={backend or '-'} | "
                    f"Resources={resources}"
                )

                # 목록에 없더라도 일부 VISA 구현은 직접 open이 가능하므로 일단 시도한다.
                inst = rm.open_resource(addr)
                cls._configure_serial_resource(inst, pyvisa)

                try:
                    inst.clear()
                except Exception:
                    pass
                time.sleep(cls.OPEN_SETTLE_S)

                current_a, err_i = cls._query_resource_float(inst, "MEAS:CURR?")
                voltage_v, err_v = cls._query_resource_float(inst, "MEAS:VOLT?")

                ok = (not np.isnan(current_a)) or (not np.isnan(voltage_v))
                detail_parts = [
                    f"{label}",
                    f"Backend={backend or '-'}",
                    f"I={cls._fmt_number(current_a)}A",
                    f"V={cls._fmt_number(voltage_v)}V",
                ]
                if err_i is not None:
                    detail_parts.append(f"CURR 오류={err_i}")
                if err_v is not None:
                    detail_parts.append(f"VOLT 오류={err_v}")
                detail = " | ".join(detail_parts)

                if ok:
                    return rm, inst, backend or label, current_a, voltage_v, detail

                all_errors.append(f"{label}: 실제 응답 없음 ({detail})")

            except Exception as e:
                serials = [r for r in resources if str(r).upper().startswith("ASRL")]
                all_errors.append(
                    f"{label} ({backend or '-'})에서 {addr} 연결 실패: {e} | "
                    f"감지 ASRL={serials}"
                )

            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
            try:
                rm.close()
            except Exception:
                pass

        raise RuntimeError(
            f"{addr}에서 PSU 실제 응답을 확인하지 못했습니다.\n- "
            + "\n- ".join(all_errors)
        )

    @classmethod
    def probe_address(cls, visa_addr: str):
        """
        포트 입력 단계에서 system VISA -> pyvisa-py 순서로 실제 PSU 응답까지 검사.

        반환: (ok, current_a, voltage_v, detail)
        """
        addr = cls._normalize_addr(visa_addr)
        rm = None
        inst = None
        current_a = float("nan")
        voltage_v = float("nan")

        try:
            rm, inst, backend, current_a, voltage_v, detail = cls._open_verified_session(addr)
            return True, current_a, voltage_v, detail
        except Exception as e:
            return False, current_a, voltage_v, str(e)
        finally:
            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
            try:
                if rm is not None:
                    rm.close()
            except Exception:
                pass

    def is_open(self) -> bool:
        return (self.inst is not None) and (self.rm is not None) and (not self._closed)

    def verify_connection(self):
        """현재 열린 세션에서 실제 PSU 응답을 확인한다."""
        if not self.is_open():
            return False, float("nan"), float("nan"), "세션이 열려 있지 않습니다."

        current_a, err_i = self._query_resource_float(self.inst, "MEAS:CURR?")
        voltage_v, err_v = self._query_resource_float(self.inst, "MEAS:VOLT?")

        ok = (not np.isnan(current_a)) or (not np.isnan(voltage_v))
        parts = [
            f"I={self._fmt_number(current_a)}A",
            f"V={self._fmt_number(voltage_v)}V",
        ]
        if err_i is not None:
            parts.append(f"CURR 오류={err_i}")
        if err_v is not None:
            parts.append(f"VOLT 오류={err_v}")
        return ok, current_a, voltage_v, " | ".join(parts)

    def _cleanup_resources(self) -> None:
        try:
            if self.inst is not None:
                try:
                    self.inst.close()
                except Exception:
                    pass
        finally:
            self.inst = None

        try:
            if self.rm is not None:
                try:
                    self.rm.close()
                except Exception:
                    pass
        finally:
            self.rm = None
            self._closed = True

    def _mark_dead(self, e: Exception):
        msg = str(e)
        if (
            "Invalid session handle" in msg
            or "resource might be closed" in msg.lower()
            or "VI_ERROR_INV_OBJECT" in msg
            or "VI_ERROR_RSRC_NFOUND" in msg
        ):
            print("PSU 세션 종료/리소스 오류 감지 -> 정리/닫힘 처리")
            self._cleanup_resources()

    def _write_command_with_retry(self, command: str, label: str) -> None:
        """
        PSU write 명령을 직렬화하고 일시적 timeout이면 짧게 재시도한다.

        - SOUR:CURR / SOUR:VOLT / OUTP 명령은 동일 명령 재전송이 안전한(idempotent) 명령이다.
        - 같은 VISA 세션에 측정/쓰기 명령이 겹치지 않도록 RLock을 사용한다.
        """
        last_error = None
        for attempt in range(1, self.WRITE_RETRIES + 1):
            if not self.is_open():
                raise RuntimeError("PSU 세션이 열려 있지 않습니다.")
            try:
                with self._io_lock:
                    self.inst.write(command)
                return
            except Exception as e:
                last_error = e
                print(f"{label} 오류 ({attempt}/{self.WRITE_RETRIES}): {e}")
                self._mark_dead(e)
                if not self.is_open():
                    break
                if attempt < self.WRITE_RETRIES:
                    time.sleep(self.WRITE_RETRY_DELAY_S)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{label} 실패")

    def set_current(self, value: float):
        self._write_command_with_retry(f"SOUR:CURR {value}", "set_current")

    def set_voltage(self, value: float):
        self._write_command_with_retry(f"SOUR:VOLT {value}", "set_voltage")

    def output_on(self):
        self._write_command_with_retry("OUTP ON", "output_on")

    def output_off(self):
        self._write_command_with_retry("OUTP OFF", "output_off")

    def measure_voltage(self) -> float:
        if not self.is_open():
            return float("nan")
        with self._io_lock:
            value, last_error = self._query_resource_float(self.inst, "MEAS:VOLT?")
        if last_error is not None:
            print(f"measure_voltage 최종 실패: {last_error}")
            self._mark_dead(last_error)
        return float(value)

    def measure_current(self) -> float:
        if not self.is_open():
            return float("nan")
        with self._io_lock:
            value, last_error = self._query_resource_float(self.inst, "MEAS:CURR?")
        if last_error is not None:
            print(f"measure_current 최종 실패: {last_error}")
            self._mark_dead(last_error)
        return float(value)

    def close(self):
        """
        PSU 해제 버튼에서 호출.
        - OUTP OFF
        - LOCAL 복귀 시도
        - VISA 세션 종료
        """
        if self._closed and self.inst is None and self.rm is None:
            return

        try:
            if self.inst is not None:
                try:
                    self.inst.write("OUTP OFF")
                except Exception:
                    pass

                for cmd in ("SYST:LOC", "SYST:LOCAL"):
                    try:
                        self.inst.write(cmd)
                        break
                    except Exception:
                        pass
        finally:
            self._cleanup_resources()





def _prepare_osa_trace(x, y) -> Tuple[np.ndarray, np.ndarray]:
    """OSA X/Y를 같은 길이의 유효한 1차원 배열로 정리한다."""
    try:
        x_arr = np.asarray(x, dtype=float).reshape(-1)
        y_arr = np.asarray(y, dtype=float).reshape(-1)
    except Exception:
        return np.array([], dtype=float), np.array([], dtype=float)

    n = min(x_arr.size, y_arr.size)
    if n < 10:
        return np.array([], dtype=float), np.array([], dtype=float)

    x_arr = x_arr[:n]
    y_arr = y_arr[:n]
    mask = np.isfinite(x_arr) & np.isfinite(y_arr)
    x_arr = x_arr[mask]
    y_arr = y_arr[mask]
    if x_arr.size < 10:
        return np.array([], dtype=float), np.array([], dtype=float)

    order = np.argsort(x_arr)
    x_arr = x_arr[order]
    y_arr = y_arr[order]
    return x_arr, y_arr


def _interpolate_crossing(x1: float, y1: float, x2: float, y2: float, target: float) -> float:
    if abs(y2 - y1) < 1e-15:
        return float(x1)
    ratio = (target - y1) / (y2 - y1)
    return float(x1 + ratio * (x2 - x1))


def _calc_osa_metrics(x, y) -> Tuple[float, float, float, float]:
    """
    동일한 OSA Trace 한 장에서 Peak1, Peak2, FWHM, SMSR를 함께 계산한다.

    - X/Y 길이 불일치와 NaN을 먼저 제거한다.
    - 보조 피크가 없을 때 주 피크를 재사용하지 않고, 주 피크 ±1.2 nm 밖의
      최대값을 fallback으로 사용한다.
    - FWHM은 주 피크 -3 dB 좌우 교차점을 선형 보간한다.
    """
    x_arr, y_arr = _prepare_osa_trace(x, y)
    if x_arr.size == 0:
        return 0.0, 0.0, float("nan"), 0.0

    idx_main = int(np.argmax(y_arr))
    wl_main = float(x_arr[idx_main])
    main_dbm = float(y_arr[idx_main])

    try:
        dx = float(np.median(np.diff(x_arr))) if x_arr.size > 1 else 0.01
        distance = max(3, int(round(0.03 / max(abs(dx), 1e-9))))
        peaks, _ = find_peaks(y_arr, distance=distance, prominence=0.05)
    except Exception:
        peaks = np.array([], dtype=int)

    if peaks.size and idx_main not in peaks:
        peaks = np.append(peaks, idx_main)

    side_mask_all = np.abs(x_arr - wl_main) >= 1.2
    idx_side = None
    if peaks.size:
        side_peaks = peaks[np.abs(x_arr[peaks] - wl_main) >= 1.2]
        if side_peaks.size:
            idx_side = int(side_peaks[np.argmax(y_arr[side_peaks])])

    if idx_side is None and np.any(side_mask_all):
        outside = np.flatnonzero(side_mask_all)
        idx_side = int(outside[np.argmax(y_arr[outside])])

    if idx_side is None:
        wl_side = 0.0
        smsr = 0.0
    else:
        wl_side = float(x_arr[idx_side])
        smsr = max(0.0, float(main_dbm - float(y_arr[idx_side])))

    half_dbm = main_dbm - 3.0
    left_x = None
    for i in range(idx_main - 1, -1, -1):
        y1, y2 = float(y_arr[i]), float(y_arr[i + 1])
        if (y1 - half_dbm) * (y2 - half_dbm) <= 0 and y1 != y2:
            left_x = _interpolate_crossing(float(x_arr[i]), y1, float(x_arr[i + 1]), y2, half_dbm)
            break

    right_x = None
    for i in range(idx_main, y_arr.size - 1):
        y1, y2 = float(y_arr[i]), float(y_arr[i + 1])
        if (y1 - half_dbm) * (y2 - half_dbm) <= 0 and y1 != y2:
            right_x = _interpolate_crossing(float(x_arr[i]), y1, float(x_arr[i + 1]), y2, half_dbm)
            break

    fwhm = float("nan")
    if left_x is not None and right_x is not None and right_x >= left_x:
        fwhm = float(right_x - left_x)

    return wl_main, wl_side, fwhm, smsr

class Worker(QThread):
    data_collected = pyqtSignal(np.ndarray, np.ndarray)
    status_changed = pyqtSignal(str)
    communication_error = pyqtSignal(str)

    QUERY_RETRIES = 3
    RECONNECT_AFTER_FAILURES = 3

    def __init__(self, visa_addr: str):
        super().__init__()
        self.visa_addr = visa_addr
        self.running = False
        self.osa = None
        self.rm = None

    def _close_session(self):
        try:
            if self.osa is not None:
                self.osa.close()
        except Exception:
            pass
        self.osa = None
        try:
            if self.rm is not None:
                self.rm.close()
        except Exception:
            pass
        self.rm = None

    def _open_session(self):
        self._close_session()
        self.rm = create_resource_manager()
        self.osa = self.rm.open_resource(self.visa_addr)
        self.osa.timeout = 5000
        self.osa.write_termination = "\n"
        self.osa.read_termination = "\n"
        try:
            self.osa.clear()
        except Exception:
            pass
        try:
            self.osa.write(":FORMat:DATA ASCii")
        except Exception as e:
            print(f"OSA ASCII 형식 설정 경고: {e}")
        idn = self.osa.query("*IDN?").strip()
        self.status_changed.emit(f"OSA 연결 성공: {idn}")

    def _read_trace_once(self) -> Tuple[np.ndarray, np.ndarray]:
        if self.osa is None:
            raise RuntimeError("OSA 세션이 열려 있지 않습니다.")

        vals = self.osa.query_ascii_values(":TRACe:DATA:Y:DCA?", separator=",")
        if len(vals) < 2:
            raise ValueError(f"OSA DCA 응답값 부족: {vals}")

        start_wavelength = float(vals[0])
        end_wavelength = float(vals[1])
        if not np.isfinite(start_wavelength) or not np.isfinite(end_wavelength):
            raise ValueError(f"OSA 파장 범위 비정상: {vals[:3]}")
        if end_wavelength <= start_wavelength:
            raise ValueError(f"OSA 파장 범위 역전: {start_wavelength}~{end_wavelength}")

        y_values = self.osa.query_ascii_values(":TRACe:DATA:Y? TRA", separator=",")
        y = np.asarray(y_values, dtype=float).reshape(-1)
        if y.size < 10 or np.count_nonzero(np.isfinite(y)) < 10:
            raise ValueError(f"OSA Trace 데이터 부족: {y.size} points")

        x = np.linspace(start_wavelength, end_wavelength, y.size, dtype=float)
        x, y = _prepare_osa_trace(x, y)
        if x.size < 10:
            raise ValueError("OSA Trace 유효 데이터가 부족합니다.")
        return x, y

    def _read_trace_with_retry(self) -> Tuple[np.ndarray, np.ndarray]:
        last_error = None
        for attempt in range(1, self.QUERY_RETRIES + 1):
            if not self.running:
                raise RuntimeError("OSA 수집 중지 요청")
            try:
                return self._read_trace_once()
            except Exception as e:
                last_error = e
                print(f"OSA Trace 읽기 {attempt}/{self.QUERY_RETRIES} 실패: {type(e).__name__}: {e}")
                try:
                    if self.osa is not None:
                        self.osa.clear()
                except Exception:
                    pass
                if attempt < self.QUERY_RETRIES:
                    self.msleep(250)
        raise last_error if last_error is not None else RuntimeError("OSA Trace 읽기 실패")

    def run(self):
        self.running = True
        consecutive_failures = 0
        try:
            while self.running:
                if self.osa is None:
                    try:
                        self.status_changed.emit("OSA 연결 시도 중...")
                        self._open_session()
                        consecutive_failures = 0
                    except Exception as e:
                        msg = f"OSA 연결 실패: {type(e).__name__}: {e}"
                        print(msg)
                        self.communication_error.emit(msg)
                        self._close_session()
                        for _ in range(10):
                            if not self.running:
                                break
                            self.msleep(200)
                        continue

                try:
                    x, y = self._read_trace_with_retry()
                    consecutive_failures = 0
                    self.data_collected.emit(x, y)
                except Exception as e:
                    if not self.running:
                        break
                    consecutive_failures += 1
                    msg = f"OSA 통신 오류({consecutive_failures}회): {type(e).__name__}: {e}"
                    print(msg)
                    self.communication_error.emit(msg)
                    if consecutive_failures >= self.RECONNECT_AFTER_FAILURES:
                        self.status_changed.emit("OSA 재연결 중...")
                        self._close_session()
                        consecutive_failures = 0
                    self.msleep(500)
                    continue

                self.msleep(500)
        finally:
            self.running = False
            self._close_session()
            self.status_changed.emit("OSA 수집 종료")

    def stop(self):
        self.running = False


class SoftDownThread(QThread):
    update_current = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, psu: PowerSupplyController):
        super().__init__()
        self.psu = psu

    def run(self):
        try:
            if self.psu is None or (not self.psu.is_open()):
                return
            cur = self.psu.measure_current()
            if cur is None or np.isnan(cur):
                cur = 0.0
            cur = int(cur)
            for a in range(cur, 0, -1):
                if self.psu is None or (not self.psu.is_open()):
                    break
                self.psu.set_current(a - 1)
                self.update_current.emit(float(a - 1))
                self.msleep(1000)
            if self.psu is not None and self.psu.is_open():
                self.psu.output_off()
        except Exception as e:
            print(f"SoftDownThread 오류: {e}")
        self.finished.emit()


class MoveCurrentThread(QThread):
    update_current = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, psu: PowerSupplyController, get_current_func, target_current: float):
        super().__init__()
        self.psu = psu
        self.get_current_func = get_current_func
        self.target_current = float(target_current)

    def run(self):
        try:
            if self.psu is None or (not self.psu.is_open()):
                return
            self.psu.output_on()
            self.msleep(200)
            cur = self.get_current_func()
            if cur is None or np.isnan(cur):
                cur = 0.0
            cur = float(cur)
            target = float(self.target_current)

            if abs(cur - target) < 1e-2:
                self.psu.set_current(target)
                self.update_current.emit(target)
                return

            step = 1 if target > cur else -1
            if step > 0:
                vals = [round(v, 2) for v in np.arange(math.floor(cur) + 1, target + 1, 1)]
            else:
                vals = [round(v, 2) for v in np.arange(math.ceil(cur) - 1, target - 1, -1)]

            for val in vals:
                if self.psu is None or (not self.psu.is_open()):
                    break
                self.psu.set_current(val)
                self.update_current.emit(float(val))
                self.msleep(1000)

            if self.psu is not None and self.psu.is_open():
                self.psu.set_current(target)
                self.update_current.emit(target)
        except Exception as e:
            print(f"MoveCurrentThread 오류: {e}")
        self.finished.emit()


@dataclass
class AutoTiming:
    voltage: float = 40.0
    warmup_seconds_at_0p8: int = 20
    settle_seconds_default: int = 10
    settle_seconds_last: int = 15
    beam_check_current_a: float = 0.8


def _safe_float(value, default=float("nan")) -> float:
    try:
        x = float(value)
        return x if np.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _capture_auto_measurement_snapshot(
    psu: PowerSupplyController,
    get_metrics_func,
) -> Tuple[float, float, float, float, float, float, float, float, float]:
    """
    전류 유지시간 종료 시 한 번만 스냅샷한다.

    반환 순서:
      wl1, wl2, fwhm, smsr, power, voltage,
      temp_lid(CH5), temp_base(CH3), temp_fiber(CH2)

    Power와 온도는 외부 StarLab/GL-Connection 프로그램이나 로그 파일이 아니라
    Nova II/GL840 직접 통신으로 읽는다.
    """
    started = time.monotonic()
    captured_at = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    wl1, wl2, fwhm, smsr = get_metrics_func()

    try:
        power = read_nova_power_direct(timeout_seconds=1.5)
    except Exception as e:
        print(f"[NOVA DIRECT ERROR] {type(e).__name__}: {e}")
        power = -9999.0

    try:
        graphtec_values = read_graphtec_temperatures_direct(timeout_seconds=12.0)
    except Exception as e:
        print(f"[GL840 DIRECT ERROR] {type(e).__name__}: {e}")
        graphtec_values = {}
    temp_fiber = _safe_float(graphtec_values.get("CH2")) if graphtec_values else float("nan")
    temp_base = _safe_float(graphtec_values.get("CH3")) if graphtec_values else float("nan")
    temp_lid = _safe_float(graphtec_values.get("CH5")) if graphtec_values else float("nan")
    voltage = psu.measure_voltage()

    elapsed = time.monotonic() - started
    print(
        f"[MEASURE SNAPSHOT] at={captured_at} | elapsed={elapsed:.3f}s | "
        f"Peak1={_safe_float(wl1, 0.0):.3f} | Peak2={_safe_float(wl2, 0.0):.3f} | "
        f"FWHM={_safe_float(fwhm):.4g} | SMSR={_safe_float(smsr, 0.0):.3f} | "
        f"V={_safe_float(voltage):.6g}V | Power={_safe_float(power):.6g}W | "
        f"CH5={temp_lid:.6g} | CH3={temp_base:.6g} | CH2={temp_fiber:.6g}"
    )
    return (
        _safe_float(wl1, 0.0), _safe_float(wl2, 0.0), _safe_float(fwhm),
        _safe_float(smsr, 0.0), _safe_float(power, -9999.0), _safe_float(voltage),
        temp_lid, temp_base, temp_fiber,
    )


class AutoMeasurePBSFiberThread(QThread):
    update_current = pyqtSignal(float)
    auto_row_data = pyqtSignal(float, float, float, float, float, float, float, float, float, float)
    finished = pyqtSignal()

    def __init__(
        self,
        psu: PowerSupplyController,
        get_peaks_func,
        timing: AutoTiming,
        max_current_a: float = 20.0,
        measure_points: Optional[list] = None,
    ):
        super().__init__()
        self.psu = psu
        self._running = True
        self.get_peaks_func = get_peaks_func
        self.timing = timing
        self.max_current_a = float(max_current_a or 20.0)
        self.measure_points = measure_points

    def stop(self):
        self._running = False

    def _emit_snapshot(self, current: float) -> None:
        snapshot = _capture_auto_measurement_snapshot(
            self.psu,
            self.get_peaks_func,
        )
        self.auto_row_data.emit(float(current), *snapshot)

    def run(self):
        ps = self.psu
        try:
            if not self._running or ps is None or (not ps.is_open()):
                return

            ps.set_voltage(self.timing.voltage)
            time.sleep(0.2)
            if not self._running or (not ps.is_open()):
                return

            ps.output_on()
            time.sleep(1.0)
            if not self._running or (not ps.is_open()):
                return

            beam_check_current = float(self.timing.beam_check_current_a or 0.8)
            ps.set_current(beam_check_current)
            self.update_current.emit(beam_check_current)

            for _ in range(self.timing.warmup_seconds_at_0p8):
                if (not self._running) or (not ps.is_open()):
                    return
                time.sleep(1)

            # 초기 빔 확인 구간 끝의 최신값 스냅샷(테이블 저장은 UI에서 1회 제외)
            if self._running and ps.is_open():
                self._emit_snapshot(beam_check_current)

            points = self.measure_points or [float(x) for x in range(1, int(round(self.max_current_a)) + 1)]
            points = sorted(set(float(x) for x in points if 0 < float(x) <= self.max_current_a))
            if self.max_current_a not in points:
                points.append(float(self.max_current_a))

            for i in points:
                if (not self._running) or (not ps.is_open()):
                    return

                ps.set_current(float(i))
                self.update_current.emit(float(i))

                wait = (
                    self.timing.settle_seconds_last
                    if abs(float(i) - self.max_current_a) < 1e-6
                    else self.timing.settle_seconds_default
                )
                for _ in range(wait):
                    if (not self._running) or (not ps.is_open()):
                        return
                    time.sleep(1)

                # 핵심: 유지시간 끝 -> 즉시 최신 완전 데이터 1회 스냅샷 -> 바로 다음 전류
                if self._running and ps.is_open():
                    self._emit_snapshot(float(i))

            down = int(round(self.max_current_a))
            while down > 0:
                if (not self._running) or (not ps.is_open()):
                    return
                ps.set_current(down - 1)
                self.update_current.emit(float(down - 1))
                time.sleep(1)
                down -= 1

            if ps.is_open():
                ps.output_off()

        except Exception as e:
            print(f"AutoMeasurePBSFiberThread 전체 오류: {e}")
        finally:
            self.finished.emit()


class AutoMeasureVBGThread(QThread):
    update_current = pyqtSignal(float)
    auto_row_data = pyqtSignal(float, float, float, float, float, float, float, float, float, float)
    finished = pyqtSignal()

    def __init__(
        self,
        psu: PowerSupplyController,
        get_peaks_func,
        timing: AutoTiming,
        max_current_a: float = 20.0,
        measure_points: Optional[list] = None,
    ):
        super().__init__()
        self.psu = psu
        self._running = True
        self.get_peaks_func = get_peaks_func
        self.timing = timing
        self.max_current_a = float(max_current_a or 20.0)
        self.measure_points = measure_points

    def stop(self):
        self._running = False

    def _emit_snapshot(self, current: float) -> None:
        snapshot = _capture_auto_measurement_snapshot(
            self.psu,
            self.get_peaks_func,
        )
        self.auto_row_data.emit(float(current), *snapshot)

    def run(self):
        ps = self.psu
        try:
            if not self._running or ps is None or (not ps.is_open()):
                return

            ps.set_voltage(self.timing.voltage)
            time.sleep(0.2)
            if not self._running or (not ps.is_open()):
                return

            ps.output_on()
            time.sleep(1.0)
            if not self._running or (not ps.is_open()):
                return

            beam_check_current = float(self.timing.beam_check_current_a or 0.8)
            ps.set_current(beam_check_current)
            self.update_current.emit(beam_check_current)

            for _ in range(self.timing.warmup_seconds_at_0p8):
                if (not self._running) or (not ps.is_open()):
                    return
                time.sleep(1)

            if self._running and ps.is_open():
                self._emit_snapshot(beam_check_current)

            target_points = self.measure_points or [2, 5, 10, 15, 16, 17, self.max_current_a]
            target_points = sorted(set(float(x) for x in target_points if 0 < float(x) <= self.max_current_a))
            if self.max_current_a not in target_points:
                target_points.append(float(self.max_current_a))
            target_set = set(round(float(x), 6) for x in target_points)

            i = 0
            while i <= int(round(self.max_current_a)):
                if (not self._running) or (not ps.is_open()):
                    return

                ps.set_current(i)
                self.update_current.emit(float(i))

                if round(float(i), 6) in target_set:
                    wait = (
                        self.timing.settle_seconds_last
                        if abs(float(i) - self.max_current_a) < 1e-6
                        else self.timing.settle_seconds_default
                    )
                    for _ in range(wait):
                        if (not self._running) or (not ps.is_open()):
                            return
                        time.sleep(1)

                    if (not self._running) or (not ps.is_open()):
                        return

                    self._emit_snapshot(float(i))
                else:
                    time.sleep(1)

                i += 1

            down = int(round(self.max_current_a))
            while down > 0:
                if (not self._running) or (not ps.is_open()):
                    return
                ps.set_current(down - 1)
                self.update_current.emit(float(down - 1))
                time.sleep(1)
                down -= 1

            if ps.is_open():
                ps.output_off()

        except Exception as e:
            print(f"AutoMeasureVBGThread 전체 오류: {e}")
        finally:
            self.finished.emit()




class OSAAutoMeasureWindow(QWidget):
    def __init__(self, repo=None, parent=None):
        super().__init__(parent)
        self.repo = repo

        self.selected_model_code = DEFAULT_MODEL_CODE

        self.osa_addr = DEFAULT_OSA_ADDR

        # ✅ PSU 상태
        self.ps_addr: Optional[str] = None
        self.psu: Optional[PowerSupplyController] = None

        self.worker: Optional[Worker] = None

        self.x = np.array([])
        self.y = np.array([])
        self._trace_condition = threading.Condition()
        self._trace_seq = 0
        self._last_metric_trace_seq = 0
        self._last_metric_trace = (np.array([]), np.array([]))
        self._trace_received_monotonic = 0.0
        self.current = 0.0
        self._last_plot_draw_monotonic = 0.0
        self._log_line = None
        self._lin_line = None

        self.is_auto_mode = False
        self.auto_thread = None
        self._is_stopping = False
        self._skip_next_beam_check_row = False
        self._active_measurement_source = "PSU"

        self.move_thread: Optional[MoveCurrentThread] = None
        self.softdown_thread: Optional[SoftDownThread] = None

        self.raw_data_dict: Dict[int, Tuple[np.ndarray, np.ndarray, float, float]] = {}
        self.liv_current_list = []
        self.liv_power_list = []
        self.liv_voltage_list = []
        self.liv_temp_list = []

        self.timing = AutoTiming()

        self._build_ui()
        self._wire()
        self._refresh_connection_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.status = QLabel("Idle")
        root.addWidget(self.status)

        model_line = QHBoxLayout()
        model_line.addWidget(QLabel("모델:"))
        self.cb_model = QComboBox()
        self.cb_model.setMinimumWidth(220)
        self._load_model_combo()
        model_line.addWidget(self.cb_model)
        self.lbl_model_info = QLabel("")
        model_line.addWidget(self.lbl_model_info)
        model_line.addStretch(1)
        root.addLayout(model_line)

        save_line = QHBoxLayout()
        save_line.addWidget(QLabel("저장 단계:"))
        self.cb_save_stage = QComboBox()
        self.cb_save_stage.setMinimumWidth(230)
        self.cb_save_stage.addItems(MEASURE_STAGE_FOLDERS)
        save_line.addWidget(self.cb_save_stage)

        save_line.addWidget(QLabel("모듈번호:"))
        self.ed_save_module = QLineEdit()
        self.ed_save_module.setPlaceholderText("예: E092")
        self.ed_save_module.setMinimumWidth(90)
        save_line.addWidget(self.ed_save_module)

        save_line.addWidget(QLabel("월/그룹:"))
        self.cb_save_group = QComboBox()
        self.cb_save_group.setEditable(True)
        self.cb_save_group.setMinimumWidth(160)
        save_line.addWidget(self.cb_save_group)

        self.btn_refresh_save_groups = QPushButton("그룹 새로고침")
        save_line.addWidget(self.btn_refresh_save_groups)
        save_line.addStretch(1)
        root.addLayout(save_line)

        # ✅ 장비 연결 상태 표시
        self.psu_status = QLabel("PSU: DISCONNECTED")
        root.addWidget(self.psu_status)

        cur_line = QHBoxLayout()
        cur_line.addWidget(QLabel("현재 전류:"))
        self.current_value = QLabel("0")
        self.current_value.setMinimumWidth(80)
        cur_line.addWidget(self.current_value)
        cur_line.addStretch(1)
        root.addLayout(cur_line)

        btns = QHBoxLayout()

        # ✅ PSU 연결·해제
        self.btn_psu_connect = QPushButton("PSU 연결")
        self.btn_psu_disconnect = QPushButton("PSU 해제")

        self.btn_start = QPushButton("측정시작")
        self.btn_stop = QPushButton("측정중지(자동측정중에만)")
        self.btn_softdown = QPushButton("전류내림")
        self.btn_export = QPushButton("엑셀로 저장")
        self.btn_auto1 = QPushButton("PBS 측정")
        self.btn_auto2 = QPushButton("VBG, Fiber 오븐 전,후")
        self.btn_auto3 = QPushButton("완성품 측정")
        self.btn_move_current = QPushButton("전류 이동(직접입력)")

        btns.addWidget(self.btn_psu_connect)
        btns.addWidget(self.btn_psu_disconnect)

        btns.addWidget(self.btn_start)
        btns.addWidget(self.btn_stop)
        btns.addWidget(self.btn_softdown)
        btns.addWidget(self.btn_export)
        btns.addWidget(self.btn_auto1)
        btns.addWidget(self.btn_auto2)
        btns.addWidget(self.btn_auto3)
        btns.addWidget(self.btn_move_current)
        root.addLayout(btns)

        self.log_canvas = FigureCanvas(plt.Figure())
        self.log_ax = self.log_canvas.figure.subplots()
        self.lin_canvas = FigureCanvas(plt.Figure())
        self.lin_ax = self.lin_canvas.figure.subplots()

        graph_layout = QVBoxLayout()
        graph_layout.addWidget(QLabel("Log Scale (dBm)"))
        graph_layout.addWidget(self.log_canvas)
        graph_layout.addWidget(QLabel("Linear Scale (nW)"))
        graph_layout.addWidget(self.lin_canvas)

        self.table = QTableWidget()
        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels([
            "Current", "Peak1", "Peak2", "FWHM (nm)", "SMSR",
            "Power", "Voltage",
            "Lid(CH5)", "Base(CH3)", "Fiber(CH2)",
            "효율"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Data Table"))
        right_layout.addWidget(self.table)

        body = QHBoxLayout()
        body.addLayout(graph_layout, 2)
        body.addLayout(right_layout, 1)
        root.addLayout(body)

    def _load_model_combo(self):
        self.cb_model.clear()
        rows = []
        try:
            if self.repo is not None and hasattr(self.repo, "list_models"):
                rows = self.repo.list_models(include_inactive=False)
        except Exception:
            rows = []

        if rows:
            for r in rows:
                code = str(r.get("model_code") or "").strip()
                name = str(r.get("display_name") or code).strip()
                max_a = r.get("max_current_a", "")
                if code:
                    self.cb_model.addItem(f"{code} / Max {max_a:g}A" if isinstance(max_a, (int, float)) else f"{code} / {name}", code)
        else:
            for code in list_model_codes():
                recipe = get_model_recipe(code)
                self.cb_model.addItem(f"{recipe.model_code} / Max {recipe.max_current_a:g}A", recipe.model_code)

        idx = self.cb_model.findData(DEFAULT_MODEL_CODE)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)

    def _selected_model(self) -> str:
        try:
            code = self.cb_model.currentData()
            if code:
                return str(code).strip()
        except Exception:
            pass
        return DEFAULT_MODEL_CODE

    def _selected_model_settings(self) -> dict:
        code = self._selected_model()
        try:
            if self.repo is not None and hasattr(self.repo, "get_model"):
                m = self.repo.get_model(code)
                if m:
                    return m
        except Exception:
            pass
        recipe = get_model_recipe(code)
        return {
            "model_code": recipe.model_code,
            "display_name": recipe.display_name,
            "max_current_a": recipe.max_current_a,
            "pbs_step_a": recipe.pbs_step_a,
            "pbs_points_csv": ",".join(str(x) for x in recipe.pbs_points),
            "vbg_points_csv": ",".join(str(x) for x in recipe.vbg_points),
            "burnin_current_a": recipe.burnin_current_a,
            "beam_check_current_a": getattr(recipe, "beam_check_current_a", 0.8),
            "measure_step_wait_s": getattr(recipe, "measure_step_wait_s", 10.0),
            "max_hold_wait_s": getattr(recipe, "max_hold_wait_s", 15.0),
        }

    def _model_max_current(self) -> float:
        try:
            return float(self._selected_model_settings().get("max_current_a") or 20.0)
        except Exception:
            return 20.0

    def _liv_chart_x_axis_max(self) -> float:
        """LIV 차트 X축 최대값은 Parameter의 모델별 Max Current를 그대로 사용한다."""
        try:
            return max(1.0, float(self._model_max_current()))
        except Exception:
            return 20.0

    def _liv_chart_voltage_axis(self) -> Tuple[float, float]:
        """
        LIV 차트 오른쪽 전압축 설정.
        - 9W/45W: 0~10V, 1V 간격
        - 그 외 모델: 기존 0~40V, 5V 간격
        """
        try:
            code = normalize_model_code(self._selected_model()).upper().replace(" ", "")
        except Exception:
            code = str(self._selected_model() or "").upper().replace(" ", "")

        low_voltage_models = {
            "F9769W", "F976009W",
            "F97645W", "F976045W",
        }
        if code in low_voltage_models:
            return 10.0, 1.0
        return 40.0, 5.0

    def _model_beam_check_current(self) -> float:
        try:
            v = self._selected_model_settings().get("beam_check_current_a")
            return float(v if v is not None else 0.8)
        except Exception:
            return 0.8

    def _model_pbs_points(self) -> list:
        m = self._selected_model_settings()
        max_a = float(m.get("max_current_a") or 20.0)
        raw = str(m.get("pbs_points_csv") or "").strip()
        pts = []
        for part in raw.replace(";", ",").split(","):
            try:
                v = float(part.strip())
                if 0 < v <= max_a:
                    pts.append(v)
            except Exception:
                pass

        # 기존 DB에 새 컬럼이 비어 있으면 PBS Step 설정으로 하위호환한다.
        if not pts:
            step = max(1, int(round(float(m.get("pbs_step_a") or 1.0))))
            pts = [float(x) for x in range(1, int(round(max_a)) + 1, step)]
        if max_a not in pts:
            pts.append(max_a)
        return sorted(set(pts))

    def _model_final_points(self) -> list:
        """완성품 측정은 기존 동작대로 1A 간격을 유지한다."""
        max_a = float(self._selected_model_settings().get("max_current_a") or 20.0)
        pts = [float(x) for x in range(1, int(round(max_a)) + 1)]
        if max_a not in pts:
            pts.append(max_a)
        return sorted(set(pts))

    def _model_vbg_points(self) -> list:
        m = self._selected_model_settings()
        max_a = float(m.get("max_current_a") or 20.0)
        raw = str(m.get("vbg_points_csv") or "").strip()
        pts = []
        for part in raw.replace(";", ",").split(","):
            try:
                v = float(part.strip())
                if 0 < v <= max_a:
                    pts.append(v)
            except Exception:
                pass
        if not pts:
            pts = [2, 5, 10, 15, 16, 17, max_a]
        if max_a not in pts:
            pts.append(max_a)
        return sorted(set(pts))

    def _model_timing(self) -> AutoTiming:
        """
        모델별 자동측정 타이밍.
        - measure_step_wait_s: 일반 측정 지점 대기시간
        - max_hold_wait_s: Max A 지점 대기시간
        기본값은 기존 코드와 동일하게 10초 / 15초.
        """
        m = self._selected_model_settings()

        def _get_float(key: str, default: float) -> float:
            try:
                v = m.get(key)
                if v is None or str(v).strip() == "":
                    return default
                return float(v)
            except Exception:
                return default

        return AutoTiming(
            voltage=self.timing.voltage,
            beam_check_current_a=max(0.1, _get_float("beam_check_current_a", 0.8)),
            warmup_seconds_at_0p8=self.timing.warmup_seconds_at_0p8,
            settle_seconds_default=max(0, int(round(_get_float("measure_step_wait_s", 10.0)))),
            settle_seconds_last=max(0, int(round(_get_float("max_hold_wait_s", 15.0)))),
        )

    def _update_model_info(self):
        m = self._selected_model_settings()
        try:
            self.lbl_model_info.setText(
                f"Max {float(m.get('max_current_a') or 0):g}A / "
                f"Burn-in {float(m.get('burnin_current_a') or 0):g}A / "
                f"빔확인 {float(m.get('beam_check_current_a') if m.get('beam_check_current_a') is not None else 0.8):g}A / "
                f"대기 {float(m.get('measure_step_wait_s') if m.get('measure_step_wait_s') is not None else 10):g}s / "
                f"Max대기 {float(m.get('max_hold_wait_s') if m.get('max_hold_wait_s') is not None else 15):g}s"
            )
        except Exception:
            self.lbl_model_info.setText("")




    def _measure_raw_root(self) -> str:
        """
        측정 raw 저장 루트.
        1순위: Parameter > 경로/장비프로그램 > 측정 저장 폴더(export_dir_measure)
        2순위: 현재 프로젝트/data/measurements/raw
        """
        try:
            ps = normalize_and_fill_defaults(load_path_settings(self.repo))
            base_dir = (getattr(ps, "export_dir_measure", "") or "").strip()
            if base_dir:
                return str(Path(base_dir))
        except Exception:
            pass
        return _project_measure_raw_root()

    def _selected_save_stage(self) -> str:
        try:
            return (self.cb_save_stage.currentText() or MEASURE_STAGE_FOLDERS[0]).strip()
        except Exception:
            return MEASURE_STAGE_FOLDERS[0]

    def _selected_save_group(self) -> str:
        try:
            g = (self.cb_save_group.currentText() or "").strip()
        except Exception:
            g = ""
        if g:
            return g
        return _default_group_from_module(self._save_module_no())

    def _save_module_no(self) -> str:
        try:
            txt = (self.ed_save_module.text() or "").strip().upper()
        except Exception:
            txt = ""
        txt = re.sub(r"\s+", "", txt)
        txt = re.sub(r"[^0-9A-Za-z]", "", txt)
        return txt

    def _save_default_dir(self) -> str:
        """
        새 저장 구조:
        raw / 단계 / 모델 / 월별그룹 / 파일.xlsx
        예:
        raw/5. Fiber 정렬 후_오븐 후/F976370W/2026-E/E092.xlsx
        """
        base = Path(self._measure_raw_root())
        model = normalize_model_code(self._selected_model())
        stage = self._selected_save_stage()
        group = self._selected_save_group()
        return str(base / stage / model / group)

    def _refresh_save_groups(self):
        """
        현재 선택된 저장 단계/모델 기준으로 월그룹 폴더 목록을 다시 읽는다.

        새 구조:
            raw / stage / model / group

        하위호환:
            F976370W 선택 시 기존 구조 raw / stage / group 도 같이 표시
        """
        blocked = False
        try:
            base = Path(self._measure_raw_root())
            stage = self._selected_save_stage()
            model = normalize_model_code(self._selected_model())

            new_model_dir = base / stage / model
            legacy_stage_dir = base / stage

            current_module_group = _default_group_from_module(self._save_module_no())
            old = (self.cb_save_group.currentText() or "").strip()

            self.cb_save_group.blockSignals(True)
            blocked = True
            self.cb_save_group.clear()

            names = []

            # 새 모델별 구조: raw/stage/model/group
            if new_model_dir.exists() and new_model_dir.is_dir():
                for p in new_model_dir.iterdir():
                    if p.is_dir() and p.name not in names:
                        names.append(p.name)

            # 기존 370W 구조 호환: raw/stage/group
            if model == DEFAULT_MODEL_CODE and legacy_stage_dir.exists() and legacy_stage_dir.is_dir():
                for p in legacy_stage_dir.iterdir():
                    if not p.is_dir():
                        continue
                    # 모델 폴더는 제외하고 월그룹 폴더만 추가
                    if re.fullmatch(r"F\d{3,4}\d{1,4}W", p.name.strip(), flags=re.IGNORECASE):
                        continue
                    if p.name not in names:
                        names.append(p.name)

            names = sorted(names, key=lambda x: x.lower())

            # 모듈번호가 입력된 경우에만 추천 그룹 추가/우선 선택
            if current_module_group and current_module_group not in names:
                names.insert(0, current_module_group)

            self.cb_save_group.addItems(names)

            if old and self.cb_save_group.findText(old) >= 0:
                self.cb_save_group.setCurrentText(old)
            elif current_module_group:
                self.cb_save_group.setCurrentText(current_module_group)
            elif names:
                self.cb_save_group.setCurrentIndex(0)
            else:
                self.cb_save_group.setEditText("")

            try:
                self.status.setText(f"그룹 로드: {len(names)}개 | {new_model_dir}")
            except Exception:
                pass

        except Exception as e:
            try:
                self.status.setText(f"그룹 로드 실패: {e}")
            except Exception:
                pass
        finally:
            try:
                if blocked:
                    self.cb_save_group.blockSignals(False)
            except Exception:
                pass

    def _wire(self):
        # ✅ PSU 연결/해제
        self.btn_psu_connect.clicked.connect(self.on_psu_connect)
        self.btn_psu_disconnect.clicked.connect(self.on_psu_disconnect)

        self.btn_start.clicked.connect(self.start_measurement)
        self.btn_stop.clicked.connect(self.stop_measurement)
        self.btn_softdown.clicked.connect(self.soft_down_to_zero)
        self.btn_export.clicked.connect(self.export_to_excel)
        self.btn_auto1.clicked.connect(self.auto_measure_pbsfiber)
        self.btn_auto2.clicked.connect(self.auto_measure_vbg)
        self.btn_auto3.clicked.connect(self.auto_measure_final)
        self.btn_move_current.clicked.connect(self.move_to_target_current)
        self.cb_model.currentIndexChanged.connect(lambda _: (self._update_model_info(), self._refresh_save_groups()))
        self.cb_save_stage.currentIndexChanged.connect(lambda _: self._refresh_save_groups())
        self.ed_save_module.textChanged.connect(lambda _: self._refresh_save_groups())
        self.btn_refresh_save_groups.clicked.connect(self._refresh_save_groups)
        self._update_model_info()
        self._refresh_save_groups()

    def _refresh_psu_ui(self):
        connected = (self.psu is not None and self.psu.is_open())
        if connected:
            self.psu_status.setText(f"PSU: CONNECTED ({self.ps_addr})")
        else:
            self.psu_status.setText("PSU: DISCONNECTED")

        busy = bool(self.is_auto_mode)
        self.btn_psu_connect.setEnabled(not connected)
        self.btn_psu_disconnect.setEnabled(connected and (not busy))


    def _refresh_connection_ui(self):
        self._refresh_psu_ui()

    def _ask_psu_port_and_make_addr(self, parent=None) -> str:
            detected, backend_notes = PowerSupplyController.list_serial_resources()

            hint = ""
            if detected:
                hint = "\n\n감지된 포트 : " + "\n  ".join(detected)
            else:
                hint = "\n\n현재 VISA 목록에서 ASRL 포트를 찾지 못했습니다."

            while True:
                text, ok = QInputDialog.getText(
                    parent,
                    "PSU 포트 입력",
                    "PSU가 연결된 포트 번호를 입력하세요.\n"
                    "예) 5 또는 COM5 (자동 변환됨)\n\n"
                    "system VISA에서 실패하면 pyvisa-py로 자동 재시도하고,\n"
                    "실제 MEAS:CURR? / MEAS:VOLT? 응답까지 확인합니다."
                    + hint,
                )
                if not ok:
                    return ""
                if not (text or "").strip():
                    QMessageBox.warning(parent, "필수", "포트 번호를 입력하세요.")
                    continue

                candidate = PowerSupplyController._normalize_addr(text)
                ok_conn, current_a, voltage_v, detail = PowerSupplyController.probe_address(candidate)

                if ok_conn:
                    print(
                        f"PSU 포트 실제 응답 확인 성공: {candidate} | "
                        f"I={PowerSupplyController._fmt_number(current_a)}A | "
                        f"V={PowerSupplyController._fmt_number(voltage_v)}V | {detail}"
                    )
                    return candidate

                notes = "\n".join(backend_notes)
                QMessageBox.warning(
                    parent,
                    "PSU 통신 실패",
                    "포트는 입력됐지만 PSU의 실제 응답을 확인하지 못했습니다.\n\n"
                    f"입력: {text}\n"
                    f"변환 주소: {candidate}\n\n"
                    f"{detail}\n\n"
                    f"[VISA 검색 상태]\n{notes}\n\n"
                    "PSU 전원, COM 번호, USB-Serial 연결 상태를 확인한 뒤 다시 시도하세요.",
                )

    def on_psu_connect(self):
        if self.psu is not None and self.psu.is_open():
            self.status.setText("PSU 이미 연결됨")
            self._refresh_psu_ui()
            return

        ps_addr = self._ask_psu_port_and_make_addr(parent=self)
        if not ps_addr:
            self.status.setText("PSU 연결 취소")
            return

        # 기존 세션이 남아 있으면 정리
        try:
            if self.psu is not None:
                self.psu.close()
        except Exception:
            pass
        self.psu = None
        self.ps_addr = None

        try:
            self.psu = PowerSupplyController(ps_addr)
            self.ps_addr = ps_addr

            i_text = PowerSupplyController._fmt_number(self.psu.last_verified_current)
            v_text = PowerSupplyController._fmt_number(self.psu.last_verified_voltage)
            self.status.setText(
                f"PSU 연결됨: {ps_addr} | 실제응답 I={i_text}A, V={v_text}V"
            )
        except Exception as e:
            self.psu = None
            self.ps_addr = None
            QMessageBox.critical(self, "PSU 연결 실패", str(e))
            self.status.setText("PSU 연결 실패")

        self._refresh_psu_ui()

    def on_psu_disconnect(self):
        # ✅ 동작 중이면 해제 막기 (세션 꼬임 방지)
        if self.is_auto_mode:
            QMessageBox.warning(self, "불가", "자동측정 중에는 PSU 해제할 수 없습니다.\n먼저 측정중지 하세요.")
            return
        if self.softdown_thread is not None and self.softdown_thread.isRunning():
            QMessageBox.warning(self, "불가", "전류내림 진행 중에는 PSU 해제할 수 없습니다.")
            return
        if self.move_thread is not None and self.move_thread.isRunning():
            QMessageBox.warning(self, "불가", "전류이동 진행 중에는 PSU 해제할 수 없습니다.")
            return

        if self.psu is not None:
            try:
                self.psu.output_off()
            except Exception:
                pass
            try:
                self.psu.close()
            except Exception:
                pass

        self.psu = None
        self.ps_addr = None
        self.status.setText("PSU 해제 완료")
        self._refresh_psu_ui()





    def require_psu_connected(self) -> bool:
        """
        ✅ 예전처럼 기능 누르면 포트 묻고 연결하지 않음.
        PSU 미연결/세션죽음이면 안내만 하고 False.
        """
        if self.psu is None or (not self.psu.is_open()):
            QMessageBox.warning(
                self,
                "PSU 미연결",
                "PSU가 연결되어 있지 않거나 세션이 종료되었습니다.\n상단의 'PSU 연결' 버튼으로 먼저 연결하세요."
            )
            self._refresh_psu_ui()
            return False
        return True

    # -------------------------
    # 계산/유틸
    # -------------------------
    def calc_efficiency_from_psu(
        self,
        power_w: float,
        current_a: Optional[float] = None,
        fallback_voltage: Optional[float] = None,
    ):
        """
        PSU 실측값 기준 효율 계산:
            효율(%) = Power(W) / (PSU 실측 Voltage(V) * PSU 실측 Current(A)) * 100

        - 전류와 전압은 PSU의 MEAS:CURR? / MEAS:VOLT? 값을 우선 사용한다.
        - 실측 전류를 읽지 못했을 때만 현재 측정 지점의 설정 전류를 fallback으로 사용한다.
        - 실측 전압을 읽지 못했을 때만 전달받은 fallback_voltage를 사용한다.
        """
        i_meas = float("nan")
        v_meas = float("nan")

        try:
            if self.psu is not None and self.psu.is_open():
                i_meas = float(self.psu.measure_current())
                v_meas = float(self.psu.measure_voltage())
        except Exception as e:
            print(f"calc_efficiency_from_psu 예외: {e}")
            i_meas, v_meas = float("nan"), float("nan")

        try:
            if (np.isnan(v_meas) or v_meas <= 0) and fallback_voltage is not None:
                fv = float(fallback_voltage)
                if not np.isnan(fv) and fv > 0:
                    v_meas = fv
        except Exception:
            pass

        current_for_eff = float(i_meas)
        try:
            if (np.isnan(current_for_eff) or current_for_eff <= 0) and current_a is not None:
                current_for_eff = float(current_a)
        except Exception:
            current_for_eff = float("nan")

        eff = float("nan")
        try:
            pw = float(power_w)
            if (
                not np.isnan(pw)
                and not np.isnan(v_meas)
                and not np.isnan(current_for_eff)
                and v_meas > 0
                and current_for_eff > 0
            ):
                eff = pw / (v_meas * current_for_eff) * 100.0
        except Exception:
            eff = float("nan")

        return i_meas, v_meas, current_for_eff, eff



    def calc_fwhm(self, x: np.ndarray, y: np.ndarray) -> float:
        return float(_calc_osa_metrics(x, y)[2])

    # -------------------------
    # OSA 측정
    # -------------------------
    def _ensure_osa_worker_running(self):
        if self.worker is not None and self.worker.isRunning():
            return

        self.worker = Worker(self.osa_addr)
        self.worker.data_collected.connect(self.update_all)
        self.worker.status_changed.connect(self._on_osa_status)
        self.worker.communication_error.connect(self._on_osa_error)
        self.worker.finished.connect(self._on_osa_worker_finished)
        self.worker.start()

    def _on_osa_status(self, message: str):
        print(message)
        if not self.is_auto_mode and not self._is_stopping:
            self.status.setText(message)

    def _on_osa_error(self, message: str):
        print(message)
        if not self.is_auto_mode and not self._is_stopping:
            self.status.setText(message)

    def _on_osa_worker_finished(self):
        with self._trace_condition:
            self._trace_condition.notify_all()

    def stop_osa_collection(self):
        """페이지 전환 시 자동측정 OSA 세션만 안전하게 종료한다."""
        worker = self.worker
        if worker is None:
            return

        try:
            if worker.isRunning():
                worker.stop()
                worker.wait(6000)
        except Exception as e:
            print(f"자동측정 OSA 종료 경고: {e}")

        if not worker.isRunning():
            self.worker = None

        with self._trace_condition:
            self._trace_condition.notify_all()

    def is_device_busy(self) -> bool:
        if self.is_auto_mode:
            return True
        if self.softdown_thread is not None and self.softdown_thread.isRunning():
            return True
        if self.move_thread is not None and self.move_thread.isRunning():
            return True
        return False

    def release_devices_for_page_switch(self) -> bool:
        """진행 중이 아닐 때 자동측정 화면이 가진 OSA/PSU 세션을 모두 반환한다."""
        if self.is_device_busy():
            return False

        self.stop_osa_collection()

        try:
            if self.psu is not None:
                try:
                    if self.psu.is_open():
                        self.psu.output_off()
                except Exception:
                    pass
                try:
                    self.psu.close()
                except Exception:
                    pass
        finally:
            self.psu = None
            self.ps_addr = None
            self._refresh_connection_ui()
        return True

    def start_measurement(self):
        self._is_stopping = False
        self.status.setText("OSA 수집 시작/데이터 대기 중...")
        self._ensure_osa_worker_running()

    def stop_measurement(self):
        self.status.setText("Idle")
        self._is_stopping = True

        self.stop_osa_collection()

        if self.is_auto_mode and self.auto_thread:
            try:
                self.auto_thread.stop()
            except Exception:
                pass
            try:
                if not self.auto_thread.wait(5000):
                    print("자동측정 Thread 종료 대기시간 초과: UI는 계속 진행합니다.")
            except Exception:
                pass
            self.is_auto_mode = False
            self.status.setText("자동측정 중지")

        self._is_stopping = False
        self.btn_start.setEnabled(True)
        self.btn_auto1.setEnabled(True)
        self.btn_auto2.setEnabled(True)
        self.btn_auto3.setEnabled(True)
        self._refresh_connection_ui()

    def soft_down_to_zero(self):
        if not self.require_psu_connected():
            return
        if self.softdown_thread is not None and self.softdown_thread.isRunning():
            self.status.setText("전류 내림 진행 중...")
            return
        self.status.setText("전류 0A로 소프트다운 중...")
        self.softdown_thread = SoftDownThread(self.psu)
        self.softdown_thread.update_current.connect(lambda v: self.current_value.setText(str(v)))
        self.softdown_thread.finished.connect(self._softdown_finished)
        self.softdown_thread.start()

    def _softdown_finished(self):
        self.status.setText("전류 0A로 소프트다운 완료")
        self.current_value.setText("0")
        self._refresh_psu_ui()

    def move_to_target_current(self):
        if not self.require_psu_connected():
            return
        target, ok = QInputDialog.getDouble(self, "전류 이동", "이동할 목표 전류(A) 입력:", decimals=2)
        if not ok:
            self.status.setText("전류 이동 취소")
            return
        if self.move_thread is not None and self.move_thread.isRunning():
            self.status.setText("전류 이동 중입니다.")
            return
        self.status.setText(f"{target}A로 전류 이동 중...")
        self.move_thread = MoveCurrentThread(self.psu, self.psu.measure_current, target)
        self.move_thread.update_current.connect(lambda v: self.current_value.setText(str(v)))
        self.move_thread.finished.connect(lambda: self.status.setText(f"{target}A 도달"))
        self.move_thread.finished.connect(self._refresh_psu_ui)
        self.move_thread.start()

    # -------------------------
    # 그래프 업데이트
    # -------------------------
    def update_all(self, x: np.ndarray, y: np.ndarray):
        """유효한 OSA Trace를 X/Y 한 쌍으로 원자적으로 갱신한다."""
        x_arr, y_arr = _prepare_osa_trace(x, y)
        if x_arr.size == 0:
            print("OSA Trace 무시: 유효 데이터 부족")
            return

        with self._trace_condition:
            self.x = x_arr
            self.y = y_arr
            self._trace_seq += 1
            self._trace_received_monotonic = time.monotonic()
            self._trace_condition.notify_all()

        now = time.monotonic()
        if (now - self._last_plot_draw_monotonic) < 0.9:
            return
        self._last_plot_draw_monotonic = now

        y_nw = dbm_to_nw(y_arr)
        if self._log_line is None:
            (self._log_line,) = self.log_ax.plot(x_arr, y_arr, color="gold")
            self.log_ax.set_ylabel("Power (dBm)")
            self.log_ax.set_title("Log Scale")
            self.log_ax.set_ylim(-105, -30)
            self.log_ax.set_xlim(966, 986)
            self.log_ax.yaxis.set_major_locator(MultipleLocator(10))
            self.log_ax.yaxis.set_minor_locator(AutoMinorLocator(2))
            self.log_ax.set_xlabel("Wavelength (nm)")
            self.log_ax.grid(True, which="major", color="k", linewidth=0.5, alpha=0.3)
            self.log_ax.grid(True, which="minor", color="gray", linestyle=":", linewidth=0.3, alpha=0.2)
        else:
            self._log_line.set_data(x_arr, y_arr)

        if self._lin_line is None:
            (self._lin_line,) = self.lin_ax.plot(x_arr, y_nw, color="gold")
            self.lin_ax.set_ylabel("Power (nW)")
            self.lin_ax.set_title("Linear Scale")
            self.lin_ax.set_ylim(0, 550)
            self.lin_ax.set_xlim(966, 986)
            self.lin_ax.yaxis.set_major_locator(MultipleLocator(100))
            self.lin_ax.yaxis.set_minor_locator(AutoMinorLocator(5))
            self.lin_ax.set_xlabel("Wavelength (nm)")
            self.lin_ax.grid(True, which="major", color="k", linewidth=0.5, alpha=0.3)
            self.lin_ax.grid(True, which="minor", color="gray", linestyle=":", linewidth=0.3, alpha=0.2)
        else:
            self._lin_line.set_data(x_arr, y_nw)

        self.log_canvas.draw_idle()
        self.lin_canvas.draw_idle()

    def _get_trace_snapshot(self, wait_for_new: bool = False, timeout_seconds: float = 0.0):
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        with self._trace_condition:
            baseline = self._last_metric_trace_seq
            while wait_for_new and self._trace_seq <= baseline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._trace_condition.wait(min(0.25, remaining))

            x = self.x.copy()
            y = self.y.copy()
            seq = int(self._trace_seq)
            age = time.monotonic() - self._trace_received_monotonic if self._trace_received_monotonic else float("inf")

            if wait_for_new and x.size:
                self._last_metric_trace_seq = max(self._last_metric_trace_seq, seq)
                self._last_metric_trace = (x.copy(), y.copy())

        return x, y, seq, age

    def get_osa_metrics(self):
        x, y, _seq, _age = self._get_trace_snapshot(wait_for_new=False)
        metrics = _calc_osa_metrics(x, y)
        if x.size:
            with self._trace_condition:
                self._last_metric_trace = (x.copy(), y.copy())
        return metrics

    def get_fresh_osa_metrics(self):
        x, y, _seq, age = self._get_trace_snapshot(wait_for_new=True, timeout_seconds=3.5)
        if x.size == 0 or age > 8.0:
            print(f"OSA 새 Trace 확보 실패 또는 오래된 Trace: age={age:.2f}s")
            return 0.0, 0.0, float("nan"), 0.0
        return _calc_osa_metrics(x, y)

    def get_peaks(self):
        wl1, wl2, _fwhm, smsr = self.get_osa_metrics()
        return wl1, wl2, smsr

    def save_auto_row_data(
        self,
        current,
        wl1,
        wl2,
        fwhm,
        smsr,
        power,
        voltage,
        temp_lid,
        temp_base,
        temp_fiber,
    ):
        """자동측정 Worker가 같은 시점에 잡은 스냅샷을 저장한다."""
        if self._skip_next_beam_check_row:
            beam_a = self._model_beam_check_current()
            if abs(float(current) - float(beam_a)) < 1e-3:
                self._skip_next_beam_check_row = False
                return

        self.save_current_data(
            current=current,
            wl1=wl1,
            wl2=wl2,
            fwhm=fwhm,
            smsr=smsr,
            power=power,
            voltage=voltage,
            measurement_source="PSU",
            temp_lid=temp_lid,
            temp_base=temp_base,
            temp_fiber=temp_fiber,
            snapshot_only=True,
        )

    def save_current_data(
        self,
        current=None,
        wl1=None,
        wl2=None,
        fwhm=None,
        smsr=None,
        power=None,
        voltage=None,
        measurement_source: str = "PSU",
        temp_lid=None,
        temp_base=None,
        temp_fiber=None,
        snapshot_only: bool = False,
    ):
        if current is None:
            current, ok = QInputDialog.getDouble(
                self,
                "Current",
                "인가한 전류(A) 입력:",
                decimals=2,
            )
            if not ok:
                self.status.setText("전류 입력 취소")
                return

        self.current = float(current)
        self.current_value.setText(str(self.current))

        if wl1 is None or wl2 is None or fwhm is None or smsr is None:
            auto_wl1, auto_wl2, auto_fwhm, auto_smsr = self.get_osa_metrics()
            wl1 = auto_wl1 if wl1 is None else wl1
            wl2 = auto_wl2 if wl2 is None else wl2
            fwhm = auto_fwhm if fwhm is None else fwhm
            smsr = auto_smsr if smsr is None else smsr

        wl1 = _safe_float(wl1, 0.0)
        wl2 = _safe_float(wl2, 0.0)
        fwhm = _safe_float(fwhm)
        smsr = _safe_float(smsr, 0.0)

        if power is None:
            try:
                power = read_nova_power_direct(timeout_seconds=1.5)
            except Exception as e:
                print(f"[NOVA DIRECT ERROR] {type(e).__name__}: {e}")
                power = float("nan")
        power = float(power)

        try:
            voltage_value = float(voltage) if voltage is not None else float("nan")
        except Exception:
            voltage_value = float("nan")

        if snapshot_only:
            # 자동측정에서는 Worker가 이미 같은 시점에 읽은 PSU I/V를 그대로 사용한다.
            # UI 스레드에서 VISA를 다시 질의하면 다음 전류 set_current와 경합할 수 있으므로 금지.
            i_meas = float(self.current)
            v_meas = voltage_value
            current_for_eff = float(self.current)

            eff = float("nan")
            try:
                if (
                    np.isfinite(power)
                    and np.isfinite(v_meas)
                    and np.isfinite(current_for_eff)
                    and v_meas > 0
                    and current_for_eff > 0
                ):
                    eff = power / (v_meas * current_for_eff) * 100.0
            except Exception:
                eff = float("nan")
        else:
            # 수동 입력 등에서는 기존 하위호환 동작 유지
            if voltage is None:
                try:
                    voltage_value = (
                        self.psu.measure_voltage()
                        if self.psu is not None and self.psu.is_open()
                        else float("nan")
                    )
                except Exception:
                    voltage_value = float("nan")

            i_meas, v_meas, current_for_eff, eff = self.calc_efficiency_from_psu(
                power_w=power,
                current_a=float(self.current),
                fallback_voltage=voltage_value,
            )
            if np.isfinite(v_meas):
                voltage_value = float(v_meas)

        # 자동 스냅샷에서 온도가 전달되지 않은 수동 입력일 때만 최신값 1회 읽기
        if not snapshot_only and (temp_lid is None or temp_base is None or temp_fiber is None):
            try:
                graphtec_values = read_graphtec_temperatures_direct(timeout_seconds=12.0)
            except Exception as e:
                print(f"[GL840 DIRECT ERROR] {type(e).__name__}: {e}")
                graphtec_values = {}
            if graphtec_values:
                if temp_fiber is None:
                    temp_fiber = graphtec_values.get("CH2")
                if temp_base is None:
                    temp_base = graphtec_values.get("CH3")
                if temp_lid is None:
                    temp_lid = graphtec_values.get("CH5")

        row = self.table.rowCount()
        already = False
        for r in range(row):
            try:
                val = float(self.table.item(r, 0).text())
                if abs(val - float(self.current)) < 0.01:
                    already = True
                    break
            except Exception:
                pass

        if not already:
            self.table.insertRow(row)

            def _set(r, c, val, fmt=None):
                s = val
                if isinstance(val, float):
                    if np.isnan(val):
                        s = "NaN"
                    else:
                        s = f"{val:.6g}" if fmt is None else fmt.format(val)
                self.table.setItem(r, c, QTableWidgetItem(str(s)))

            _set(row, 0, float(self.current))
            _set(row, 1, float(wl1), "{:.3f}")
            _set(row, 2, float(wl2), "{:.3f}")
            _set(row, 3, float(fwhm), "{:.3f}")
            _set(row, 4, float(smsr), "{:.2f}")
            _set(row, 5, float(power))
            _set(row, 6, float(voltage_value))
            _set(row, 7, float(temp_lid) if temp_lid is not None else float("nan"))
            _set(row, 8, float(temp_base) if temp_base is not None else float("nan"))
            _set(row, 9, float(temp_fiber) if temp_fiber is not None else float("nan"))
            _set(row, 10, float(eff), "{:.2f}")

            self.liv_temp_list.append(
                float(temp_base) if temp_base is not None else np.nan
            )

        keyA = int(round(float(self.current)))
        with self._trace_condition:
            if snapshot_only and self._last_metric_trace[0].size:
                raw_x = self._last_metric_trace[0].copy()
                raw_y = self._last_metric_trace[1].copy()
            else:
                raw_x = self.x.copy()
                raw_y = self.y.copy()
        self.raw_data_dict[keyA] = (raw_x, raw_y, float(wl1), float(wl2))

        self.liv_current_list.append(float(self.current))
        self.liv_power_list.append(float(power) if power is not None else np.nan)
        self.liv_voltage_list.append(
            float(voltage_value) if voltage_value is not None else np.nan
        )

        self.status.setText(
            f"데이터 입력[PSU]: Current {self.current}A, "
            f"Voltage:{voltage_value}, "
            f"Peak1:{wl1:.2f}, Peak2:{wl2:.2f}, "
            f"FWHM:{(fwhm if not np.isnan(fwhm) else float('nan')):.3f}nm, "
            f"SMSR:{smsr:.2f}, Power:{power}, "
            f"Lid(CH5):{temp_lid}, Base(CH3):{temp_base}, Fiber(CH2):{temp_fiber}, "
            f"EffCurrent:{current_for_eff}, Eff:{eff:.2f}%"
        )

        self._refresh_connection_ui()

    def auto_measure_pbsfiber(self):
        if self.is_auto_mode:
            return
        if not self.require_psu_connected():
            return
        self._ensure_osa_worker_running()

        max_a = self._model_max_current()
        points = self._model_pbs_points()
        self._active_measurement_source = "PSU"
        self.status.setText(
            f"자동 측정 중...({self._selected_model()} / PBS / Max {max_a:g}A / PSU Current, Voltage)"
        )
        self.is_auto_mode = True
        self.btn_auto1.setEnabled(False)
        self.btn_auto2.setEnabled(False)
        self.btn_auto3.setEnabled(False)
        self.btn_start.setEnabled(False)

        self.table.setRowCount(0)
        self.raw_data_dict = {}
        self.liv_current_list = []
        self.liv_power_list = []
        self.liv_voltage_list = []
        self.liv_temp_list = []
        self._skip_next_beam_check_row = True

        self.auto_thread = AutoMeasurePBSFiberThread(
            self.psu,
            self.get_fresh_osa_metrics,
            self._model_timing(),
            max_current_a=max_a,
            measure_points=points,
        )
        self.auto_thread.update_current.connect(self.update_current_value)
        self.auto_thread.auto_row_data.connect(self.save_auto_row_data)
        self.auto_thread.finished.connect(self.auto_measure_done)
        self.auto_thread.start()

    def auto_measure_vbg(self):
        if self.is_auto_mode:
            return
        if not self.require_psu_connected():
            return
        self._ensure_osa_worker_running()

        max_a = self._model_max_current()
        points = self._model_vbg_points()
        self._active_measurement_source = "PSU"
        self.status.setText(
            f"자동 측정 중...({self._selected_model()} / VBG·Fiber 오븐 전후 / Max {max_a:g}A / PSU Current, Voltage)"
        )
        self.is_auto_mode = True
        self.btn_auto1.setEnabled(False)
        self.btn_auto2.setEnabled(False)
        self.btn_auto3.setEnabled(False)
        self.btn_start.setEnabled(False)

        self.table.setRowCount(0)
        self.raw_data_dict = {}
        self.liv_current_list = []
        self.liv_power_list = []
        self.liv_voltage_list = []
        self.liv_temp_list = []
        self._skip_next_beam_check_row = True

        self.auto_thread = AutoMeasureVBGThread(
            self.psu,
            self.get_fresh_osa_metrics,
            self._model_timing(),
            max_current_a=max_a,
            measure_points=points,
        )
        self.auto_thread.update_current.connect(self.update_current_value)
        self.auto_thread.auto_row_data.connect(self.save_auto_row_data)
        self.auto_thread.finished.connect(self.auto_measure_done)
        self.auto_thread.start()

    def auto_measure_final(self):
        if self.is_auto_mode:
            return
        if not self.require_psu_connected():
            return
        self._ensure_osa_worker_running()

        max_a = self._model_max_current()
        points = self._model_final_points()
        self._active_measurement_source = "PSU"
        self.status.setText(
            f"자동 측정 중...({self._selected_model()} / 완성품 / Max {max_a:g}A / PSU Current, Voltage)"
        )
        print(
            f"[완성품 측정] PSU={self.ps_addr} | "
            "Power=Nova II 직접통신 | Temperature=GL840 USB 직접통신"
        )

        self.is_auto_mode = True
        self.btn_auto1.setEnabled(False)
        self.btn_auto2.setEnabled(False)
        self.btn_auto3.setEnabled(False)
        self.btn_start.setEnabled(False)

        self.table.setRowCount(0)
        self.raw_data_dict = {}
        self.liv_current_list = []
        self.liv_power_list = []
        self.liv_voltage_list = []
        self.liv_temp_list = []
        self._skip_next_beam_check_row = True

        self.auto_thread = AutoMeasurePBSFiberThread(
            self.psu,
            self.get_fresh_osa_metrics,
            self._model_timing(),
            max_current_a=max_a,
            measure_points=points,
        )
        self.auto_thread.update_current.connect(self.update_current_value)
        self.auto_thread.auto_row_data.connect(self.save_auto_row_data)
        self.auto_thread.finished.connect(self.auto_measure_done)
        self.auto_thread.start()

    def _on_auto_error(self, message: str):
        self.status.setText(str(message))
        QMessageBox.critical(self, "자동측정 오류", str(message))

    def update_current_value(self, value):
        self.current_value.setText(str(value))
        self.status.setText(f"자동 측정 중... 현재 {value}A 인가")

    def auto_measure_done(self):
        self.is_auto_mode = False
        self.btn_auto1.setEnabled(True)
        self.btn_auto2.setEnabled(True)
        self.btn_auto3.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.status.setText("자동 측정 완료")

        self.stop_osa_collection()

        if not self._is_stopping:
            self.current_value.setText("0")
        self._refresh_connection_ui()

    def export_to_excel(self):
        module_no = self._save_module_no()
        if not module_no:
            module_no, ok = QInputDialog.getText(self, "모듈번호", "저장할 모듈번호를 입력하세요. 예: E092")
            if not ok or not (module_no or "").strip():
                self.status.setText("엑셀 저장 취소: 모듈번호 없음")
                return
            module_no = re.sub(r"[^0-9A-Za-z]", "", str(module_no).strip().upper())
            self.ed_save_module.setText(module_no)

        self._refresh_save_groups()

        base_dir = self._save_default_dir()
        os.makedirs(base_dir, exist_ok=True)

        default_path = str(Path(base_dir) / f"{module_no}.xlsx")

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "엑셀 파일로 저장",
            default_path,
            "Excel Files (*.xlsx)"
        )
        if not file_path:
            self.status.setText("엑셀 저장 취소")
            return
        if not file_path.lower().endswith(".xlsx"):
            file_path += ".xlsx"

        # ✅ 실제 저장 위치 강제:
        # 사용자가 저장창에서 다른 폴더를 눌러도, 파일명만 가져오고
        # 실제 저장은 raw/측정단계/모델/월그룹 폴더에 저장한다.
        expected_dir = Path(base_dir).resolve()
        expected_dir.mkdir(parents=True, exist_ok=True)

        chosen_name = Path(file_path).name
        if not chosen_name.lower().endswith(".xlsx"):
            chosen_name += ".xlsx"

        actual_path = expected_dir / chosen_name
        if actual_path.name.strip() in ("", ".xlsx"):
            actual_path = expected_dir / f"{module_no}.xlsx"

        file_path = str(actual_path)

        nrow = self.table.rowCount()
        ncol = self.table.columnCount()
        columns = [self.table.horizontalHeaderItem(i).text() for i in range(ncol)]
        data = []
        for r in range(nrow):
            data.append([self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(ncol)])
        df = pd.DataFrame(data, columns=columns)

        cur_arr_all = np.array(self.liv_current_list, dtype=float)
        pow_arr_all = np.array(self.liv_power_list, dtype=float)
        mask_se = (~np.isnan(cur_arr_all)) & (~np.isnan(pow_arr_all)) & (cur_arr_all > 0) & (pow_arr_all > 0) & (pow_arr_all > -1000)
        if np.sum(mask_se) >= 2:
            coef = np.polyfit(cur_arr_all[mask_se], pow_arr_all[mask_se], 1)
            slope_eff = float(coef[0])
        else:
            slope_eff = float("nan")

        with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name="Summary", index=False)
            wb = writer.book
            ws = writer.sheets["Summary"]

            slope_row = nrow + 2
            slope_label = "Slope Eff (W/A)"
            slope_value = "NaN" if np.isnan(slope_eff) else f"{slope_eff:.4f}"

            ws.write(slope_row, 0, slope_label)
            ws.write(slope_row, 1, slope_value)

            center_fmt = wb.add_format({"align": "center", "valign": "vcenter"})
            for i, col in enumerate(df.columns):
                col_maxlen = max(df[col].astype(str).map(len).max(), len(col))
                if i == 0:
                    col_maxlen = max(col_maxlen, len(slope_label))
                elif i == 1:
                    col_maxlen = max(col_maxlen, len(slope_value))
                ws.set_column(i, i, col_maxlen + 2, center_fmt)
            ws.set_default_row(18)

            # 이하 원본 로직 유지
            center_fmt = wb.add_format({"align": "center", "valign": "vcenter"})
            for i, col in enumerate(df.columns):
                col_maxlen = max(df[col].astype(str).map(len).max(), len(col))
                if i == 0:
                    col_maxlen = max(col_maxlen, len(slope_label))
                elif i == 1:
                    col_maxlen = max(col_maxlen, len(slope_value))
                ws.set_column(i, i, col_maxlen + 2, center_fmt)

            ws.set_default_row(18)

            # LIV 축 범위는 현재 선택 모델의 Parameter 설정을 기준으로 고정한다.
            # 예: F976370W Max Current=20A이면 16A/20A 시트 모두 X축 끝은 20A.
            liv_x_axis_max = self._liv_chart_x_axis_max()
            liv_voltage_axis_max, liv_voltage_major_unit = self._liv_chart_voltage_axis()

            chart_amps = [16, 20, int(round(self._model_max_current()))]
            chart_amps = sorted(set([a for a in chart_amps if a > 0]))
            for amps in chart_amps:
                if amps not in self.raw_data_dict:
                    continue

                x, y, wl1, wl2 = self.raw_data_dict[amps]
                y_nw = dbm_to_nw(y)

                chart_df = pd.DataFrame({
                    "Wavelength (nm)": x,
                    "Power (dBm)": y,
                    "Power (nW)": y_nw
                })
                sheet_name = f"{int(amps)}A Chart"
                chart_df.to_excel(writer, sheet_name=sheet_name, index=False)
                ws_chart = writer.sheets[sheet_name]

                for i, col in enumerate(chart_df.columns):
                    maxlen = max(chart_df[col].astype(str).map(len).max(), len(col))
                    ws_chart.set_column(i, i, maxlen + 2, wb.add_format({"align": "center", "valign": "vcenter"}))
                ws_chart.set_default_row(15)

                peaks, _ = find_peaks(y, distance=10)
                if len(peaks) == 0:
                    idx_main = int(np.argmax(y))
                    idx_side = idx_main
                else:
                    idx_main_in_peaks = int(np.argmax(y[peaks]))
                    idx_main = int(peaks[idx_main_in_peaks])
                    wl_main = x[idx_main]
                    exclude_mask = (x[peaks] < wl_main - 1.2) | (x[peaks] > wl_main + 1.2)
                    candidate_peaks = peaks[exclude_mask]
                    if len(candidate_peaks) == 0:
                        idx_side = idx_main
                    else:
                        idx_side = int(candidate_peaks[np.argmax(y[candidate_peaks])])

                chart_log = wb.add_chart({"type": "scatter", "subtype": "straight"})
                chart_log.add_series({
                    "name": "Log(dBm)",
                    "categories": [sheet_name, 1, 0, len(x), 0],
                    "values":     [sheet_name, 1, 1, len(x), 1],
                    "marker": {"type": "none"},
                    "line": {"color": "blue"},
                })
                chart_log.add_series({
                    "name": "Peak1",
                    "categories": [sheet_name, idx_main + 1, 0, idx_main + 1, 0],
                    "values":     [sheet_name, idx_main + 1, 1, idx_main + 1, 1],
                    "marker": {"type": "circle", "size": 9, "border": {"color": "orange"}, "fill": {"color": "yellow"}},
                    "line": {"none": True},
                    "data_labels": {"value": False, "category": True},
                })
                chart_log.add_series({
                    "name": "Peak2",
                    "categories": [sheet_name, idx_side + 1, 0, idx_side + 1, 0],
                    "values":     [sheet_name, idx_side + 1, 1, idx_side + 1, 1],
                    "marker": {"type": "circle", "size": 9, "border": {"color": "green"}, "fill": {"color": "lime"}},
                    "line": {"none": True},
                    "data_labels": {"value": False, "category": True},
                })
                chart_log.set_title({"name": f"{int(amps)}A Log (dBm)"})
                chart_log.set_x_axis({"name": "Wavelength (nm)", "label_position": "low", "min": 966, "max": 986, "major_unit": 10})
                chart_log.set_y_axis({"name": "Power (dBm)", "min": -90, "max": -20, "major_unit": 10})
                ws_chart.insert_chart("F2", chart_log, {"x_scale": 1.3, "y_scale": 1.1})

                chart_lin = wb.add_chart({"type": "scatter", "subtype": "straight"})
                chart_lin.add_series({
                    "name": "Linear(nW)",
                    "categories": [sheet_name, 1, 0, len(x), 0],
                    "values":     [sheet_name, 1, 2, len(x), 2],
                    "marker": {"type": "none"},
                    "line": {"color": "blue"},
                })
                chart_lin.add_series({
                    "name": "Peak WL",
                    "categories": [sheet_name, idx_main + 1, 0, idx_main + 1, 0],
                    "values":     [sheet_name, idx_main + 1, 2, idx_main + 1, 2],
                    "marker": {"type": "circle", "size": 9, "border": {"color": "orange"}, "fill": {"color": "yellow"}},
                    "line": {"none": True},
                    "data_labels": {"value": False, "category": True},
                })
                try:
                    frac = calc_power_fraction(x, y_nw, 974.5, 977.5)
                    chart_lin.set_title({"name": f"{int(amps)}A Linear (nW)\n(974.5~977.5 nm 내 분포율: {frac:.2f}%)"})
                except Exception:
                    chart_lin.set_title({"name": f"{int(amps)}A Linear (nW)"})
                chart_lin.set_x_axis({"name": "Wavelength (nm)", "label_position": "low", "min": 966, "max": 986, "major_unit": 10})
                chart_lin.set_y_axis({"name": "Power (nW)", "major_unit": 100})
                ws_chart.insert_chart("F20", chart_lin, {"x_scale": 1.3, "y_scale": 1.1})

                # ==========================================================
                # ✅ LIV: 16A 시트는 0~16A, 20A 시트는 0~20A까지만
                # ✅ 그리고 0A, 1A Power는 0W로 고정
                # ==========================================================
                cur_arr = np.array(self.liv_current_list, dtype=float)
                pow_arr = np.array(self.liv_power_list, dtype=float)
                volt_arr = np.array(self.liv_voltage_list, dtype=float)

                cur_grid, pow_grid, volt_grid = _resample_liv_0toMax_step1(
                    cur_arr=cur_arr,
                    pow_arr=pow_arr,
                    volt_arr=volt_arr,
                    max_a=amps
                )

                df_liv = pd.DataFrame({
                    "Current (A)": cur_grid,
                    "Power (W)": pow_grid,
                    "Voltage (V)": volt_grid
                })

                startrow = len(x) + 3
                df_liv.to_excel(writer, sheet_name=sheet_name, index=False, startrow=startrow)

                # 16/20A 표시 박스(원본 유지)
                idx_16 = [i for i, c in enumerate(cur_arr) if int(round(c)) == 16]
                idx_20 = [i for i, c in enumerate(cur_arr) if int(round(c)) == 20]
                pow16 = pow_arr[idx_16[-1]] if idx_16 else None
                pow20 = pow_arr[idx_20[-1]] if idx_20 else None

                d40_row, d40_col = 39, 3
                e40_row, e40_col = 39, 4
                d41_row, d41_col = 40, 3
                e41_row, e41_col = 40, 4
                border_fmt = wb.add_format({"border": 2, "align": "center", "valign": "vcenter"})
                if amps == 16 and pow16 is not None:
                    ws_chart.write(d40_row, d40_col, "16A", border_fmt)
                    ws_chart.write(e40_row, e40_col, f"{pow16:.1f}W", border_fmt)
                elif amps == 20:
                    if pow16 is not None:
                        ws_chart.write(d40_row, d40_col, "16A", border_fmt)
                        ws_chart.write(e40_row, e40_col, f"{pow16:.1f}W", border_fmt)
                    if pow20 is not None:
                        ws_chart.write(d41_row, d41_col, "20A", border_fmt)
                        ws_chart.write(e41_row, e41_col, f"{pow20:.1f}W", border_fmt)

                # ✅ LIV 차트 축 범위
                # - X축: 현재 모델 Parameter의 Max Current
                # - 오른쪽 전압축: 9W/45W는 10V, 그 외 모델은 기존 40V
                chart_liv = wb.add_chart({"type": "scatter", "subtype": "straight"})
                chart_liv.add_series({
                    "name": "Power (W)",
                    "categories": [sheet_name, startrow + 1, 0, startrow + len(df_liv), 0],
                    "values":     [sheet_name, startrow + 1, 1, startrow + len(df_liv), 1],
                    "marker": {"type": "circle", "size": 5, "border": {"color": "blue"}, "fill": {"color": "cyan"}},
                    "line": {"color": "blue"},
                })
                chart_liv.add_series({
                    "name": "Voltage (V)",
                    "categories": [sheet_name, startrow + 1, 0, startrow + len(df_liv), 0],
                    "values":     [sheet_name, startrow + 1, 2, startrow + len(df_liv), 2],
                    "marker": {"type": "circle", "size": 5, "border": {"color": "red"}, "fill": {"color": "yellow"}},
                    "line": {"color": "red", "dash_type": "dash"},
                    "y2_axis": True,
                })
                chart_liv.set_title({"name": f"{int(amps)}A LIV Chart"})
                chart_liv.set_x_axis({
                    "name": "Current (A)",
                    "min": 0,
                    "max": float(liv_x_axis_max),
                })
                chart_liv.set_y_axis({"name": "Power (W)", "min": 0})
                chart_liv.set_y2_axis({
                    "name": "Voltage (V)",
                    "min": 0,
                    "max": float(liv_voltage_axis_max),
                    "major_unit": float(liv_voltage_major_unit),
                })
                ws_chart.insert_chart("F40", chart_liv, {"x_scale": 1.2, "y_scale": 1.0})

        # ✅ 저장 성공 후 모듈번호-모델 매핑도 보정
        try:
            if self.repo is not None and hasattr(self.repo, "upsert_module"):
                self.repo.upsert_module(module_no=module_no, model=self._selected_model())
        except Exception:
            pass

        self.status.setText(f"엑셀 저장 완료(실제 경로): {file_path}")

    def close(self):
        try:
            if self.worker is not None and self.worker.isRunning():
                self.worker.stop()
                self.worker.wait(6000)
        except Exception:
            pass
        try:
            if self.psu is not None:
                try:
                    self.psu.output_off()
                except Exception:
                    pass
                try:
                    self.psu.close()
                except Exception:
                    pass
        except Exception:
            pass
        super().close()

