# -*- coding: utf-8 -*-
import os
import re
import time
import threading
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict, Any

import numpy as np
import pandas as pd

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGridLayout, QFrame, QFileDialog, QInputDialog, QMessageBox, QComboBox, QLineEdit
)

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas

from scipy.signal import find_peaks

# ✅ Parameter 탭에서 저장한 기본경로 적용
from src.ldms.app_config import load_path_settings, normalize_and_fill_defaults
from src.ldms.process_flow import DEFAULT_MODEL_CODE, get_model_recipe, list_model_codes, normalize_model_code
from src.ldms.utils.visa_backend import create_resource_manager
from src.ldms.ui.tools.direct_measurement_devices import (
    read_nova_power_direct,
    read_graphtec_temperatures_direct,
)



def _normalize_module_no(raw: str) -> str:
    s = "" if raw is None else str(raw).strip().upper()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9A-Za-z]", "", s)
    return s


def _default_group_from_module(module_no: str) -> str:
    """
    기존 측정 폴더 운영 방식과 맞춤:
    모듈 앞 알파벳을 월/그룹으로 사용.
    예: E092 -> 2026-E
    """
    m = re.match(r"^([A-Z]+)", (module_no or "").strip().upper())
    prefix = m.group(1) if m else ""
    year = datetime.now().year
    return f"{year}-{prefix}" if prefix else f"{year}"


# -------------------- (선택) Windows 알람 --------------------
def _beep_alarm():
    try:
        import winsound
        winsound.Beep(1500, 500)
        winsound.Beep(1200, 500)
        winsound.Beep(1800, 700)
    except Exception:
        pass


# -------------------- 공용 유틸 --------------------
# Power/온도는 direct_measurement_devices를 통해 직접 수집한다.

# -------------------- PSU 컨트롤 --------------------
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
        """PSU write를 직렬화하고 일시적 timeout이면 짧게 재시도한다."""
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
    return x_arr[order], y_arr[order]


_NOVA_READ_LOCK = threading.RLock()
_GRAPHTEC_READ_LOCK = threading.RLock()


def _moving_average(values: np.ndarray, points: int) -> np.ndarray:
    points = max(1, int(points))
    if points <= 1:
        return np.array(values, dtype=float, copy=True)
    if points % 2 == 0:
        points += 1
    points = min(points, max(1, values.size // 5 * 2 + 1))
    if points <= 1:
        return np.array(values, dtype=float, copy=True)
    kernel = np.ones(points, dtype=float) / float(points)
    return np.convolve(values, kernel, mode="same")


def _analyze_osa_trace(
    x,
    y,
    current_a: Optional[float] = None,
) -> Dict[str, Any]:
    """
    OSA Trace의 실제 레이저 신호 유효성을 검사한 뒤 피크를 계산한다.

    0A 또는 PSU 미연결 상태에서는 OSA 노이즈 최고점을 파장으로 표시하지 않는다.
    전류가 인가된 상태에서도 주 피크의 SNR/prominence가 부족하면
    '레이저 신호 없음'으로 처리한다.
    """
    result: Dict[str, Any] = {
        "valid": False,
        "peak1": float("nan"),
        "peak2": float("nan"),
        "diff": float("nan"),
        "main_dbm": float("nan"),
        "noise_floor_dbm": float("nan"),
        "snr_db": float("nan"),
        "prominence_db": float("nan"),
        "reason": "OSA Trace 없음",
    }

    x_arr, y_arr = _prepare_osa_trace(x, y)
    if x_arr.size == 0:
        return result

    if current_a is not None:
        try:
            current_value = float(current_a)
        except Exception:
            current_value = float("nan")
        if np.isfinite(current_value) and current_value < 0.5:
            result["reason"] = f"레이저 전류 미인가({current_value:.2f}A)"
            return result

    try:
        dx = abs(float(np.median(np.diff(x_arr)))) if x_arr.size > 1 else 0.01
        smooth_points = max(3, int(round(0.02 / max(dx, 1e-9))))
        smooth_points = min(21, smooth_points)
        y_smooth = _moving_average(y_arr, smooth_points)

        # convolution edge가 피크 후보가 되지 않도록 가장자리 제외
        edge = max(2, smooth_points)
        usable = np.arange(edge, max(edge, y_smooth.size - edge), dtype=int)
        if usable.size < 10:
            usable = np.arange(y_smooth.size, dtype=int)

        distance = max(3, int(round(0.03 / max(dx, 1e-9))))
        peaks, props = find_peaks(
            y_smooth,
            distance=distance,
            prominence=1.0,
        )
        peaks = peaks[(peaks >= usable[0]) & (peaks <= usable[-1])] if peaks.size else peaks
        if peaks.size == 0:
            result["reason"] = "뚜렷한 레이저 피크 없음"
            return result

        idx_main_pos = int(np.argmax(y_smooth[peaks]))
        idx_main = int(peaks[idx_main_pos])
        main_dbm = float(y_smooth[idx_main])

        # 전체 바닥보다 약간 높은 percentile을 사용해 랜덤 단발 spike 영향을 줄인다.
        noise_floor = float(np.percentile(y_smooth[usable], 60.0))
        snr_db = float(main_dbm - noise_floor)

        # find_peaks의 prominence 배열은 필터 전 peaks와 같은 순서이므로
        # 현재 idx_main의 prominence를 직접 다시 계산하는 대신 주변 바닥으로 보수 추정한다.
        half_window = max(5, int(round(0.20 / max(dx, 1e-9))))
        left = max(0, idx_main - half_window)
        right = min(y_smooth.size, idx_main + half_window + 1)
        local = y_smooth[left:right]
        local_floor = float(np.percentile(local, 20.0)) if local.size else noise_floor
        prominence_db = float(main_dbm - max(noise_floor, local_floor))

        result.update({
            "main_dbm": main_dbm,
            "noise_floor_dbm": noise_floor,
            "snr_db": snr_db,
            "prominence_db": prominence_db,
        })

        # 실제 레이저 피크 판정 기준. 너무 낮게 잡으면 0A 노이즈가 파장으로 표시된다.
        if snr_db < 6.0 or prominence_db < 3.0:
            result["reason"] = (
                f"레이저 신호 약함(SNR {snr_db:.1f}dB, "
                f"Prominence {prominence_db:.1f}dB)"
            )
            return result

        wl_main = float(x_arr[idx_main])

        side_candidates = peaks[np.abs(x_arr[peaks] - wl_main) >= 1.2]
        idx_side = None
        if side_candidates.size:
            idx_side = int(side_candidates[np.argmax(y_smooth[side_candidates])])

        wl_side = float("nan")
        dbm_diff = float("nan")
        if idx_side is not None:
            wl_side = float(x_arr[idx_side])
            dbm_diff = max(0.0, float(main_dbm - float(y_smooth[idx_side])))

        result.update({
            "valid": True,
            "peak1": wl_main,
            "peak2": wl_side,
            "diff": dbm_diff,
            "reason": "정상",
        })
        return result

    except Exception as e:
        result["reason"] = f"OSA 분석 오류: {type(e).__name__}: {e}"
        return result


def _calc_osa_peaks(
    x,
    y,
    current_a: Optional[float] = None,
) -> Tuple[float, float, float]:
    analysis = _analyze_osa_trace(x, y, current_a=current_a)
    return (
        float(analysis["peak1"]),
        float(analysis["peak2"]),
        float(analysis["diff"]),
    )


class OSAWorker(QThread):
    """
    번인 전용 OSA 수집 Worker.

    핵심:
    - OSA Trace를 Worker 내부에도 thread-safe하게 보관한다.
    - BurnInThread가 UI signal 처리 시점에 의존하지 않고 최신 Trace를 직접 조회한다.
    - 일시적인 VISA timeout은 재시도하고, 연속 실패 시 세션을 다시 연다.
    """
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

        self._trace_condition = threading.Condition()
        self._latest_x = np.array([], dtype=float)
        self._latest_y = np.array([], dtype=float)
        self._latest_seq = 0
        self._latest_received_monotonic = 0.0

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
        self.osa.timeout = 10000
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

        vals = self.osa.query_ascii_values(
            ":TRACe:DATA:Y:DCA?",
            separator=",",
        )
        if len(vals) < 2:
            raise ValueError(f"OSA DCA 응답값 부족: {vals}")

        start_wavelength = float(vals[0])
        end_wavelength = float(vals[1])

        if not np.isfinite(start_wavelength) or not np.isfinite(end_wavelength):
            raise ValueError(f"OSA 파장 범위 비정상: {vals[:3]}")
        if end_wavelength <= start_wavelength:
            raise ValueError(
                f"OSA 파장 범위 역전: {start_wavelength}~{end_wavelength}"
            )

        y_values = self.osa.query_ascii_values(
            ":TRACe:DATA:Y? TRA",
            separator=",",
        )
        y = np.asarray(y_values, dtype=float).reshape(-1)

        if y.size < 10 or np.count_nonzero(np.isfinite(y)) < 10:
            raise ValueError(f"OSA Trace 데이터 부족: {y.size} points")

        x = np.linspace(
            start_wavelength,
            end_wavelength,
            y.size,
            dtype=float,
        )
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
                print(
                    f"번인 OSA Trace 읽기 {attempt}/{self.QUERY_RETRIES} 실패: "
                    f"{type(e).__name__}: {e}"
                )

                try:
                    if self.osa is not None:
                        self.osa.clear()
                except Exception:
                    pass

                if attempt < self.QUERY_RETRIES:
                    self.msleep(300)

        if last_error is not None:
            raise last_error
        raise RuntimeError("OSA Trace 읽기 실패")

    def _store_trace(self, x: np.ndarray, y: np.ndarray) -> int:
        with self._trace_condition:
            self._latest_x = np.array(x, dtype=float, copy=True)
            self._latest_y = np.array(y, dtype=float, copy=True)
            self._latest_seq += 1
            self._latest_received_monotonic = time.monotonic()
            seq = int(self._latest_seq)
            self._trace_condition.notify_all()
            return seq

    def get_latest_metrics(
        self,
        after_seq: int = 0,
        timeout_seconds: float = 0.0,
        max_age_seconds: float = 10.0,
        current_a: Optional[float] = None,
    ) -> Tuple[float, float, float, int, float]:
        """
        Worker가 보관한 최신 Trace에서 Peak1/Peak2/dBm Diff를 반환한다.

        after_seq보다 새로운 Trace를 timeout_seconds 동안 기다린다.
        새 Trace가 시간 안에 안 와도, 최신 Trace가 max_age_seconds 이내면
        그 값을 사용한다. 일시적인 signal 지연 때문에 0으로 저장되는 것을 막는다.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))

        with self._trace_condition:
            while (
                self.running
                and self._latest_seq <= int(after_seq)
                and time.monotonic() < deadline
            ):
                remaining = deadline - time.monotonic()
                self._trace_condition.wait(min(0.25, max(0.0, remaining)))

            x = self._latest_x.copy()
            y = self._latest_y.copy()
            seq = int(self._latest_seq)
            received = float(self._latest_received_monotonic)

        age = (
            time.monotonic() - received
            if received > 0
            else float("inf")
        )

        if x.size < 10 or y.size < 10 or age > float(max_age_seconds):
            return 0.0, 0.0, 0.0, seq, age

        peak1, peak2, diff = _calc_osa_peaks(x, y, current_a=current_a)
        return float(peak1), float(peak2), float(diff), seq, age

    def has_valid_trace(self, max_age_seconds: float = 10.0) -> bool:
        peak1, _peak2, _diff, _seq, age = self.get_latest_metrics(
            after_seq=-1,
            timeout_seconds=0.0,
            max_age_seconds=max_age_seconds,
        )
        return bool(np.isfinite(peak1) and peak1 > 0.0 and age <= float(max_age_seconds))

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
                    seq = self._store_trace(x, y)

                    if seq == 1:
                        analysis = _analyze_osa_trace(x, y)
                        if analysis["valid"]:
                            p1 = analysis["peak1"]
                            p2 = analysis["peak2"]
                            diff = analysis["diff"]
                            p2_text = f"{p2:.3f} nm" if np.isfinite(p2) else "없음"
                            diff_text = f"{diff:.2f} dB" if np.isfinite(diff) else "-"
                            self.status_changed.emit(
                                f"OSA Trace 수신 정상: Peak1={p1:.3f} nm / "
                                f"Peak2={p2_text} / Diff={diff_text}"
                            )
                        else:
                            self.status_changed.emit(
                                f"OSA Trace 수신됨 / {analysis['reason']}"
                            )

                    self.data_collected.emit(x, y)

                except Exception as e:
                    if not self.running:
                        break

                    consecutive_failures += 1
                    msg = (
                        f"OSA 통신 오류({consecutive_failures}회): "
                        f"{type(e).__name__}: {e}"
                    )
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
            with self._trace_condition:
                self._trace_condition.notify_all()
            self._close_session()
            self.status_changed.emit("OSA 수집 종료")

    def stop(self):
        self.running = False
        with self._trace_condition:
            self._trace_condition.notify_all()


# -------------------- 전류 내림(소프트다운) 스레드 --------------------
class SoftDownThread(QThread):
    update_current = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self, psu: PowerSupplyController):
        super().__init__()
        self.psu = psu
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            if self.psu is None or (not self.psu.is_open()):
                return

            cur = self.psu.measure_current()
            if cur is None or np.isnan(cur):
                cur = 0.0

            cur_int = int(round(float(cur)))
            for a in range(cur_int, 0, -1):
                if (not self._running) or (self.psu is None) or (not self.psu.is_open()):
                    return
                try:
                    self.psu.set_current(a - 1)
                except Exception:
                    return
                self.update_current.emit(float(a - 1))
                time.sleep(1)

            try:
                if self.psu is not None and self.psu.is_open():
                    self.psu.output_off()
            except Exception:
                pass

        finally:
            self.finished.emit()


# -------------------- 번인 실시간 장비 모니터 --------------------
class RealtimeMonitorThread(QThread):
    telemetry = pyqtSignal(dict)

    def __init__(self, get_psu_func):
        super().__init__()
        self.get_psu_func = get_psu_func
        self._running = True

    def stop(self):
        self._running = False

    def _sleep_interruptible(self, milliseconds: int):
        remaining = max(0, int(milliseconds))
        while self._running and remaining > 0:
            chunk = min(100, remaining)
            self.msleep(chunk)
            remaining -= chunk

    def run(self):
        last_power = float("nan")
        last_ch2 = float("nan")
        last_ch3 = float("nan")
        last_ch5 = float("nan")
        next_power = 0.0
        next_temp = 0.0

        while self._running:
            now = time.monotonic()
            current = float("nan")
            voltage = float("nan")

            try:
                psu = self.get_psu_func()
            except Exception:
                psu = None

            try:
                if psu is not None and psu.is_open():
                    current = float(psu.measure_current())
                    voltage = float(psu.measure_voltage())
            except Exception as e:
                print(f"[BURNIN REALTIME PSU ERROR] {type(e).__name__}: {e}")

            if now >= next_power:
                try:
                    with _NOVA_READ_LOCK:
                        last_power = float(read_nova_power_direct(timeout_seconds=1.5))
                except Exception as e:
                    print(f"[BURNIN REALTIME NOVA ERROR] {type(e).__name__}: {e}")
                    last_power = float("nan")
                next_power = now + 2.0

            if now >= next_temp:
                try:
                    with _GRAPHTEC_READ_LOCK:
                        values = read_graphtec_temperatures_direct(timeout_seconds=6.0)
                    last_ch2 = float(values.get("CH2", float("nan")))
                    last_ch3 = float(values.get("CH3", float("nan")))
                    last_ch5 = float(values.get("CH5", float("nan")))
                except Exception as e:
                    print(f"[BURNIN REALTIME GL840 ERROR] {type(e).__name__}: {e}")
                    last_ch2 = float("nan")
                    last_ch3 = float("nan")
                    last_ch5 = float("nan")
                next_temp = now + 5.0

            efficiency = float("nan")
            if (
                np.isfinite(last_power)
                and np.isfinite(current)
                and np.isfinite(voltage)
                and current > 0
                and voltage > 0
            ):
                efficiency = last_power / (current * voltage) * 100.0

            self.telemetry.emit({
                "Current": current,
                "Voltage": voltage,
                "Power": last_power,
                "FiberTemp_CH2": last_ch2,
                "PKGTemp_CH3": last_ch3,
                "BaseTemp_CH5": last_ch5,
                "Efficiency": efficiency,
            })
            self._sleep_interruptible(1000)


# -------------------- 번인 스레드 --------------------
class BurnInThread(QThread):
    update_current = pyqtSignal(float)
    log_row = pyqtSignal(dict)
    finished = pyqtSignal()
    finished_ok = pyqtSignal()

    def __init__(
        self,
        psu: PowerSupplyController,
        get_peaks_func,
        hold_minutes: float,
        target_current_a: float = 20.0,
    ):
        super().__init__()
        self.psu = psu
        self.get_peaks_func = get_peaks_func
        self._running = True
        self.hold_seconds = max(1, int(float(hold_minutes) * 60))
        self.target_current_a = float(target_current_a or 20.0)

    def stop(self):
        self._running = False

    def _emit_log(self, current: float):
        """
        기록 시점에서 최신 완전 데이터들을 즉시 한 번 스냅샷한다.
        새 파일 샘플을 추가로 기다리지 않아 기록 시점이 밀리지 않는다.
        """
        started = time.monotonic()
        captured_at = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        wl1, wl2, dbm_diff = self.get_peaks_func(float(current))

        try:
            with _NOVA_READ_LOCK:
                power = read_nova_power_direct(timeout_seconds=1.5)
        except Exception as e:
            print(f"[NOVA DIRECT ERROR] {type(e).__name__}: {e}")
            power = float("nan")

        try:
            with _GRAPHTEC_READ_LOCK:
                graphtec_values = read_graphtec_temperatures_direct(timeout_seconds=12.0)
        except Exception as e:
            print(f"[GL840 DIRECT ERROR] {type(e).__name__}: {e}")
            graphtec_values = {}
        temp_fiber = graphtec_values.get("CH2") if graphtec_values else None
        temp_pkg = graphtec_values.get("CH3") if graphtec_values else None
        temp_base = graphtec_values.get("CH5") if graphtec_values else None

        try:
            voltage = self.psu.measure_voltage()
        except Exception:
            voltage = float("nan")

        elapsed = time.monotonic() - started
        print(
            f"[BURNIN SNAPSHOT] at={captured_at} | elapsed={elapsed:.3f}s | "
            f"Current={float(current):.6g}A | "
            f"V={float(voltage):.6g}V | Power={float(power):.6g}W | "
            f"CH5={temp_base} | CH3={temp_pkg} | CH2={temp_fiber}"
        )

        rec = {
            "Current": float(current),
            "Peak1": float(wl1),
            "Peak2": float(wl2),
            "dBm Diff": float(dbm_diff),
            "Power": float(power),
            "Voltage": float(voltage),
            "FiberTemp_CH2": float(temp_fiber) if temp_fiber is not None else float("nan"),
            "PKGTemp_CH3": float(temp_pkg) if temp_pkg is not None else float("nan"),
            "BaseTemp_CH5": float(temp_base) if temp_base is not None else float("nan"),
        }
        self.log_row.emit(rec)

    def run(self):
        try:
            ps = self.psu
            if ps is None or (not ps.is_open()):
                return

            try:
                ps.set_voltage(40)
                time.sleep(0.2)
                if not self._running or (not ps.is_open()):
                    return

                ps.output_on()
                time.sleep(0.5)
                if not self._running or (not ps.is_open()):
                    return

                ps.set_current(0)
                self.update_current.emit(0)

                for i in range(1, int(round(self.target_current_a)) + 1):
                    if (not self._running) or (not ps.is_open()):
                        return
                    ps.set_current(float(i))
                    self.update_current.emit(float(i))
                    time.sleep(1)

                if (not self._running) or (not ps.is_open()):
                    return

                self._emit_log(float(self.target_current_a))

                log_interval = 5
                last_log = time.monotonic()
                elapsed = 0
                while self._running and elapsed < self.hold_seconds and ps.is_open():
                    time.sleep(1)
                    elapsed += 1
                    now = time.monotonic()
                    if now - last_log >= log_interval - 0.1:
                        self._emit_log(float(self.target_current_a))
                        last_log = now

                if (not self._running) or (not ps.is_open()):
                    return

                for i in range(int(round(self.target_current_a)) - 1, -1, -1):
                    if (not self._running) or (not ps.is_open()):
                        return
                    ps.set_current(i)
                    self.update_current.emit(i)
                    time.sleep(1)

                try:
                    if ps.is_open():
                        ps.output_off()
                except Exception:
                    pass

                self.finished_ok.emit()

            except Exception as e:
                print(f"BurnInThread 전체 오류: {e}")

        finally:
            self.finished.emit()


# -------------------- 번인 Excel 저장 --------------------
_BURNIN_EXPORT_COLUMNS = [
    "Model", "ModuleNo",
    "Time (sec)", "Time (min)",
    "Current", "Peak1", "Peak2", "dBm Diff",
    "Power", "Voltage",
    "FiberTemp_CH2", "PKGTemp_CH3", "BaseTemp_CH5",
    "효율",
]


def _excel_safe_value(value):
    """numpy 값과 NaN/Inf를 Excel에서 안전한 Python 값으로 변환한다."""
    if value is None:
        return None
    try:
        if isinstance(value, np.generic):
            value = value.item()
    except Exception:
        pass
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def _write_burnin_excel_file(
    file_path: str,
    records: List[Dict[str, Any]],
    model_code: str,
    module_no: str,
) -> None:
    """
    UI 스레드 밖에서 번인 데이터를 xlsx로 저장한다.

    - openpyxl 우선, 없으면 xlsxwriter로 자동 fallback
    - 같은 폴더의 임시 파일에 먼저 저장한 뒤 os.replace로 완성본 교체
    - NaN/Inf는 빈 셀로 저장
    """
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    rows: List[List[Any]] = []
    for rec in list(records or []):
        row_map = {
            "Model": model_code,
            "ModuleNo": module_no,
            **dict(rec or {}),
        }
        rows.append([_excel_safe_value(row_map.get(c)) for c in _BURNIN_EXPORT_COLUMNS])

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.stem}_",
        suffix=".xlsx",
        dir=str(target.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        saved = False
        openpyxl_error = None

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font
            from openpyxl.utils import get_column_letter

            wb = Workbook()
            ws = wb.active
            ws.title = "Summary"
            ws.append(_BURNIN_EXPORT_COLUMNS)
            for row in rows:
                ws.append(row)

            header_font = Font(bold=True)
            center = Alignment(horizontal="center", vertical="center")
            for cell in ws[1]:
                cell.font = header_font
                cell.alignment = center

            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = center

            for col_idx, col_name in enumerate(_BURNIN_EXPORT_COLUMNS, start=1):
                max_len = len(str(col_name))
                for row_idx in range(2, ws.max_row + 1):
                    value = ws.cell(row=row_idx, column=col_idx).value
                    if value is not None:
                        max_len = max(max_len, len(str(value)))
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 28)

            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            wb.save(str(temp_path))
            wb.close()
            saved = True
        except Exception as exc:
            openpyxl_error = exc

        if not saved:
            try:
                import xlsxwriter

                workbook = xlsxwriter.Workbook(str(temp_path), {"constant_memory": True})
                worksheet = workbook.add_worksheet("Summary")
                header_fmt = workbook.add_format({
                    "bold": True,
                    "align": "center",
                    "valign": "vcenter",
                })
                center_fmt = workbook.add_format({
                    "align": "center",
                    "valign": "vcenter",
                })

                for col_idx, name in enumerate(_BURNIN_EXPORT_COLUMNS):
                    worksheet.write(0, col_idx, name, header_fmt)

                widths = [len(str(c)) for c in _BURNIN_EXPORT_COLUMNS]
                for row_idx, row in enumerate(rows, start=1):
                    for col_idx, value in enumerate(row):
                        if value is None:
                            worksheet.write_blank(row_idx, col_idx, None, center_fmt)
                        else:
                            worksheet.write(row_idx, col_idx, value, center_fmt)
                            widths[col_idx] = max(widths[col_idx], len(str(value)))

                for col_idx, width in enumerate(widths):
                    worksheet.set_column(col_idx, col_idx, min(width + 2, 28))
                worksheet.freeze_panes(1, 0)
                worksheet.autofilter(0, 0, max(0, len(rows)), len(_BURNIN_EXPORT_COLUMNS) - 1)
                workbook.close()
                saved = True
            except Exception as xlsxwriter_error:
                raise RuntimeError(
                    "Excel 저장 엔진을 사용할 수 없습니다. "
                    f"openpyxl 오류: {openpyxl_error} / "
                    f"xlsxwriter 오류: {xlsxwriter_error}"
                ) from xlsxwriter_error

        if not saved or not temp_path.exists() or temp_path.stat().st_size <= 0:
            raise RuntimeError("임시 Excel 파일이 정상적으로 생성되지 않았습니다.")

        try:
            os.replace(str(temp_path), str(target))
        except PermissionError as exc:
            raise PermissionError(
                f"저장 대상 파일이 Excel에서 열려 있거나 쓰기 권한이 없습니다:\n{target}"
            ) from exc
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass


class BurninExcelExportThread(QThread):
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        file_path: str,
        records: List[Dict[str, Any]],
        model_code: str,
        module_no: str,
        parent=None,
    ):
        super().__init__(parent)
        self.file_path = str(file_path)
        self.records = [dict(r) for r in list(records or [])]
        self.model_code = str(model_code)
        self.module_no = str(module_no)

    def run(self):
        try:
            _write_burnin_excel_file(
                self.file_path,
                self.records,
                self.model_code,
                self.module_no,
            )
            self.succeeded.emit(self.file_path)
        except Exception as exc:
            print("[BURNIN EXCEL EXPORT ERROR]")
            print(traceback.format_exc())
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# -------------------- UI: 번인 윈도우 --------------------
class BurnInWindow(QWidget):
    def __init__(
        self,
        repo=None,
        osa_addr: str = "TCPIP0::192.168.0.10::inst0::INSTR",
        parent=None,
    ):
        super().__init__(parent)
        self.repo = repo
        self.selected_model_code = DEFAULT_MODEL_CODE
        self.osa_addr = osa_addr

        self.setWindowTitle("번인 테스트 (OSA/PSU)")
        self.resize(1200, 800)

        self.x = np.array([])
        self.y = np.array([])
        self._trace_condition = threading.Condition()
        self._trace_seq = 0
        self._last_metric_trace_seq = 0
        self._trace_received_monotonic = 0.0
        self.log_records: List[Dict[str, Any]] = []
        self.time_list: List[float] = []
        self.burnin_start_time: Optional[float] = None
        self.burnin_hold_start_time: Optional[float] = None
        self.burnin_hold_minutes: Optional[float] = None

        # ✅ 상태
        self.psu_addr: Optional[str] = None
        self.psu: Optional[PowerSupplyController] = None

        # OSA QThread는 수집 시작할 때마다 새 객체로 만든다.
        # 이전 Thread가 통신 오류로 종료된 뒤 같은 객체를 재사용하지 않는다.
        self.osa_worker: Optional[OSAWorker] = None
        self.burnin_thread: Optional[BurnInThread] = None
        self.softdown_thread: Optional[SoftDownThread] = None
        self.realtime_thread: Optional[RealtimeMonitorThread] = None
        self.export_thread: Optional[BurninExcelExportThread] = None
        self._export_row_count = 0
        self._latest_realtime: Dict[str, float] = {}

        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_remaining_time)
        self.timer.start(1000)

        self._refresh_conn_ui()

    # ------------------ helpers: running state ------------------
    def _is_busy(self) -> bool:
        if self.burnin_thread is not None and self.burnin_thread.isRunning():
            return True
        if self.softdown_thread is not None and self.softdown_thread.isRunning():
            return True
        return False

    def is_device_busy(self) -> bool:
        return self._is_busy()

    # ------------------ PSU connect helper (자동측정과 동일 스타일) ------------------
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

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.lbl_status = QLabel("Idle")
        layout.addWidget(self.lbl_status)

        model_line = QHBoxLayout()
        model_line.addWidget(QLabel("모델:"))
        self.cb_model = QComboBox()
        self.cb_model.setMinimumWidth(220)
        self._load_model_combo()
        model_line.addWidget(self.cb_model)
        self.lbl_model_info = QLabel("")
        model_line.addWidget(self.lbl_model_info)
        model_line.addStretch(1)
        layout.addLayout(model_line)

        save_line = QHBoxLayout()
        save_line.addWidget(QLabel("저장 모듈번호:"))
        self.ed_save_module = QLineEdit()
        self.ed_save_module.setPlaceholderText("예: E092")
        self.ed_save_module.setMinimumWidth(100)
        save_line.addWidget(self.ed_save_module)

        save_line.addWidget(QLabel("월/그룹:"))
        self.cb_save_group = QComboBox()
        self.cb_save_group.setEditable(True)
        self.cb_save_group.setMinimumWidth(160)
        save_line.addWidget(self.cb_save_group)

        self.btn_refresh_save_groups = QPushButton("그룹 새로고침")
        save_line.addWidget(self.btn_refresh_save_groups)
        save_line.addStretch(1)
        layout.addLayout(save_line)

        # ✅ 연결 상태 라벨
        self.lbl_conn = QLabel("PSU: DISCONNECTED")
        layout.addWidget(self.lbl_conn)

        row_time = QHBoxLayout()
        self.lbl_start = QLabel("번인 시작 시각: -")
        self.lbl_remain = QLabel("남은 번인 시간: -")
        row_time.addWidget(self.lbl_start)
        row_time.addSpacing(20)
        row_time.addWidget(self.lbl_remain)
        row_time.addStretch(1)
        layout.addLayout(row_time)

        row_btn = QHBoxLayout()
        self.lbl_current = QLabel("현재 전류: 0A")
        row_btn.addWidget(self.lbl_current)

        row_btn.addSpacing(20)

        # ✅ PSU 연결/해제 분리
        self.btn_psu_connect = QPushButton("PSU 연결")
        self.btn_psu_disconnect = QPushButton("PSU 해제")

        self.btn_start_osa = QPushButton("OSA 수집 시작")
        self.btn_burnin = QPushButton("번인 시작")
        self.btn_stop = QPushButton("중지")
        self.btn_softdown = QPushButton("전류내림(0A)")
        self.btn_export = QPushButton("엑셀 저장")

        row_btn.addWidget(self.btn_psu_connect)
        row_btn.addWidget(self.btn_psu_disconnect)

        row_btn.addSpacing(10)
        row_btn.addWidget(self.btn_start_osa)
        row_btn.addWidget(self.btn_burnin)
        row_btn.addWidget(self.btn_stop)
        row_btn.addWidget(self.btn_softdown)
        row_btn.addWidget(self.btn_export)
        row_btn.addStretch(1)
        layout.addLayout(row_btn)

        # ✅ wiring
        self.btn_psu_connect.clicked.connect(self._on_psu_connect)
        self.btn_psu_disconnect.clicked.connect(self._on_psu_disconnect)
        self.btn_start_osa.clicked.connect(self._on_start_osa)
        self.btn_burnin.clicked.connect(self._on_start_burnin)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_softdown.clicked.connect(self._on_softdown)
        self.btn_export.clicked.connect(self._on_export)
        self.cb_model.currentIndexChanged.connect(lambda _: (self._update_model_info(), self._refresh_save_groups()))
        self.ed_save_module.textChanged.connect(lambda _: self._refresh_save_groups())
        self.btn_refresh_save_groups.clicked.connect(self._refresh_save_groups)
        self._update_model_info()
        self._refresh_save_groups()

        layout.addWidget(QLabel("Output Chart (Output vs Time)"))
        self.out_canvas = FigureCanvas(plt.Figure())
        self.out_ax = self.out_canvas.figure.subplots()
        layout.addWidget(self.out_canvas)

        self.data_labels: Dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        fields = [
            ("Current (A)", "current"),
            ("Peak1 WL (nm)", "peak1"),
            ("Peak2 WL (nm)", "peak2"),
            ("dBm Diff", "dbm_diff"),
            ("Power (W)", "power"),
            ("Voltage (V)", "voltage"),
            ("Fiber Temp CH2 (°C)", "temp_fiber_ch2"),
            ("PKG Temp CH3 (°C)", "temp_pkg_ch3"),
            ("Base Temp CH5 (°C)", "temp_base_ch5"),
            ("Efficiency (%)", "eff"),
        ]

        cols = 4
        for idx, (text, key) in enumerate(fields):
            r = idx // cols
            c = idx % cols
            frame = QFrame()
            frame.setFrameShape(QFrame.Box)
            frame.setFrameShadow(QFrame.Sunken)
            frame.setLineWidth(1)

            box = QVBoxLayout()
            box.setContentsMargins(6, 4, 6, 4)

            name = QLabel(text)
            name.setAlignment(Qt.AlignCenter)

            val = QLabel("-")
            val.setAlignment(Qt.AlignCenter)
            f = val.font()
            f.setPointSize(f.pointSize() + 2)
            f.setBold(True)
            val.setFont(f)

            box.addWidget(name)
            box.addWidget(val)
            frame.setLayout(box)

            grid.addWidget(frame, r, c)
            self.data_labels[key] = val

        layout.addWidget(QLabel("Realtime Data"))
        layout.addLayout(grid)

        self._update_chart()

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
                try:
                    cur = float(r.get("burnin_current_a") or r.get("max_current_a") or 20.0)
                    label = f"{code} / Burn-in {cur:g}A"
                except Exception:
                    label = f"{code} / {name}"
                if code:
                    self.cb_model.addItem(label, code)
        else:
            for code in list_model_codes():
                recipe = get_model_recipe(code)
                self.cb_model.addItem(f"{recipe.model_code} / Burn-in {recipe.burnin_current_a:g}A", recipe.model_code)

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
            "burnin_current_a": recipe.burnin_current_a,
        }

    def _burnin_target_current(self) -> float:
        m = self._selected_model_settings()
        try:
            return float(m.get("burnin_current_a") or m.get("max_current_a") or 20.0)
        except Exception:
            return 20.0

    def _update_model_info(self):
        try:
            self.lbl_model_info.setText(f"Target {self._burnin_target_current():g}A")
        except Exception:
            self.lbl_model_info.setText("")




    def _burnin_base_root(self) -> str:
        """
        번인 Excel 저장 루트를 반환한다.

        저장 경로 우선순위:
        1. Parameter 탭에 저장된 export_dir_burnin
        2. 현재 실행 환경에 맞는 Documents/LDModuleSystem/data/burnin/raw

        사용자 계정명, PC 이름, 구버전 프로젝트 폴더를 하드코딩하지 않는다.
        개발 PC와 PyInstaller 배포 PC 모두에서 동작하도록 구성한다.
        """
        try:
            # DB에 저장된 Parameter 경로를 먼저 사용한다.
            settings = load_path_settings(self.repo)
            settings = normalize_and_fill_defaults(settings)
            configured_root = str(
                getattr(settings, "export_dir_burnin", "") or ""
            ).strip()

            if configured_root:
                root = Path(os.path.expandvars(os.path.expanduser(configured_root)))
                root.mkdir(parents=True, exist_ok=True)
                return str(root.resolve())
        except Exception as exc:
            # 설정값이 없거나 잘못되어도 안전한 기본 경로로 계속 진행한다.
            print(f"[BURNIN PATH SETTINGS WARNING] {type(exc).__name__}: {exc}")

        # normalize_and_fill_defaults가 실패한 경우의 최종 fallback이다.
        # Path.home()은 실행 중인 PC의 실제 사용자 홈 경로를 자동으로 사용한다.
        fallback_root = (
            Path.home()
            / "Documents"
            / "LDModuleSystem"
            / "data"
            / "burnin"
            / "raw"
        )
        fallback_root.mkdir(parents=True, exist_ok=True)
        return str(fallback_root.resolve())

    def _save_module_no(self) -> str:
        try:
            return _normalize_module_no(self.ed_save_module.text())
        except Exception:
            return ""

    def _selected_save_group(self) -> str:
        try:
            g = (self.cb_save_group.currentText() or "").strip()
        except Exception:
            g = ""
        if g:
            return g
        return _default_group_from_module(self._save_module_no())

    def _burnin_save_dir(self) -> str:
        """
        번인 저장 구조:
        export_dir_burnin / 모델명 / 월그룹
        예:
        data/burnin/F976370W/2026-E/E092_burnin.xlsx
        """
        base = Path(self._burnin_base_root())
        model = normalize_model_code(self._selected_model())
        group = self._selected_save_group()
        return str(base / model / group)

    def _refresh_save_groups(self):
        try:
            model = normalize_model_code(self._selected_model())
            root = Path(self._burnin_base_root()) / model
            old = (self.cb_save_group.currentText() or "").strip()
            recommend = _default_group_from_module(self._save_module_no())

            self.cb_save_group.blockSignals(True)
            self.cb_save_group.clear()

            groups = []
            if root.exists():
                groups = [p.name for p in sorted(root.iterdir()) if p.is_dir()]

            if recommend and recommend not in groups:
                groups.insert(0, recommend)

            self.cb_save_group.addItems(groups)

            if old:
                self.cb_save_group.setCurrentText(old)
            elif recommend:
                self.cb_save_group.setCurrentText(recommend)

            self.cb_save_group.blockSignals(False)
        except Exception:
            pass

    def _refresh_conn_ui(self):
        psu_ok = self.psu is not None and self.psu.is_open()
        busy = self._is_busy()

        self.lbl_conn.setText(
            f"PSU: {'CONNECTED' if psu_ok else 'DISCONNECTED'}"
            + (f" ({self.psu_addr})" if psu_ok and self.psu_addr else "")
        )

        self.btn_psu_disconnect.setEnabled(psu_ok and (not busy))
        self.btn_psu_connect.setEnabled(not psu_ok)

    def _require_connected(self) -> bool:
        if self.psu is None or (not self.psu.is_open()):
            QMessageBox.warning(self, "필수", "먼저 PSU를 연결하세요.")
            self._refresh_conn_ui()
            return False
        return True

    # ------------------ PSU connect / disconnect ------------------
    def _on_psu_connect(self):
        if self.psu is not None and self.psu.is_open():
            self.lbl_status.setText("PSU는 이미 연결됨")
            self._refresh_conn_ui()
            return

        ps_addr = self._ask_psu_port_and_make_addr(parent=self)
        if not ps_addr:
            self.lbl_status.setText("PSU 연결 취소")
            return

        try:
            if self.psu is not None:
                self.psu.close()
        except Exception:
            pass
        self.psu = None
        self.psu_addr = None

        try:
            self.psu = PowerSupplyController(ps_addr)
            self.psu_addr = ps_addr
            self._ensure_realtime_monitor_running()

            i_text = PowerSupplyController._fmt_number(self.psu.last_verified_current)
            v_text = PowerSupplyController._fmt_number(self.psu.last_verified_voltage)
            self.lbl_status.setText(
                f"PSU 연결 완료: {ps_addr} | 실제응답 I={i_text}A, V={v_text}V"
            )
        except Exception as e:
            QMessageBox.critical(self, "PSU 연결 실패", str(e))
            self.psu = None
            self.psu_addr = None
            self.lbl_status.setText("PSU 연결 실패")

        self._refresh_conn_ui()

    def _on_psu_disconnect(self):
        if self._is_busy():
            QMessageBox.warning(self, "불가", "동작 중에는 PSU 해제할 수 없습니다.\n먼저 중지/전류내림 종료 후 해제하세요.")
            self._refresh_conn_ui()
            return

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
            # ✅ 예외가 나도 UI 상태는 무조건 끊김으로
            self.psu = None
            self.psu_addr = None
            self.lbl_status.setText("PSU 해제 완료")
            self._refresh_conn_ui()



    # ------------------ 실시간 장비 데이터 ------------------
    def _ensure_realtime_monitor_running(self):
        if self.realtime_thread is not None and self.realtime_thread.isRunning():
            return
        self.realtime_thread = RealtimeMonitorThread(lambda: self.psu)
        self.realtime_thread.telemetry.connect(self._on_realtime_telemetry)
        self.realtime_thread.finished.connect(self._on_realtime_finished)
        self.realtime_thread.start()

    def _stop_realtime_monitor(self, wait_ms: int = 1500):
        worker = self.realtime_thread
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.stop()
                worker.wait(max(0, int(wait_ms)))
        except Exception as e:
            print(f"번인 실시간 모니터 종료 경고: {e}")
        if not worker.isRunning():
            self.realtime_thread = None

    def _on_realtime_finished(self):
        if self.realtime_thread is not None and not self.realtime_thread.isRunning():
            self.realtime_thread = None

    def _on_realtime_telemetry(self, rec: dict):
        self._latest_realtime = dict(rec or {})

        current = float(rec.get("Current", float("nan")))
        voltage = float(rec.get("Voltage", float("nan")))
        power = float(rec.get("Power", float("nan")))
        ch2 = float(rec.get("FiberTemp_CH2", float("nan")))
        ch3 = float(rec.get("PKGTemp_CH3", float("nan")))
        ch5 = float(rec.get("BaseTemp_CH5", float("nan")))
        eff = float(rec.get("Efficiency", float("nan")))

        self._set_panel("current", current, "{:.3f}")
        self._set_panel("voltage", voltage, "{:.6g}")
        self._set_panel("power", power, "{:.6g}")
        self._set_panel("temp_fiber_ch2", ch2, "{:.3f}")
        self._set_panel("temp_pkg_ch3", ch3, "{:.3f}")
        self._set_panel("temp_base_ch5", ch5, "{:.3f}")
        self._set_panel("eff", eff, "{:.2f}")

        if np.isfinite(current):
            self.lbl_current.setText(f"현재 전류: {current:.3f}A")

    def release_devices_for_page_switch(self) -> bool:
        """진행 중이 아닐 때 번인 화면이 가진 OSA/PSU 세션을 모두 반환한다."""
        if self._is_busy():
            return False

        self.stop_osa_collection()
        self._stop_realtime_monitor(wait_ms=1500)

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
            self.psu_addr = None
            self._refresh_conn_ui()
        return True

    # ------------------ OSA ------------------
    def _ensure_osa_worker_running(self):
        if self.osa_worker is not None and self.osa_worker.isRunning():
            return

        self._last_metric_trace_seq = 0
        self.osa_worker = OSAWorker(self.osa_addr)
        self.osa_worker.data_collected.connect(self._on_osa_data)
        self.osa_worker.status_changed.connect(self._on_osa_status)
        self.osa_worker.communication_error.connect(self._on_osa_error)
        self.osa_worker.finished.connect(self._on_osa_worker_finished)
        self.osa_worker.start()

    def _on_osa_status(self, message: str):
        print(message)
        if self.burnin_thread is None or not self.burnin_thread.isRunning():
            self.lbl_status.setText(message)

    def _on_osa_error(self, message: str):
        print(message)
        if self.burnin_thread is None or not self.burnin_thread.isRunning():
            self.lbl_status.setText(message)

    def _on_osa_worker_finished(self):
        with self._trace_condition:
            self._trace_condition.notify_all()

    def _on_start_osa(self):
        if self.osa_worker is not None and self.osa_worker.isRunning():
            self.lbl_status.setText("OSA는 이미 수집 중")
            return
        self._ensure_realtime_monitor_running()
        self._ensure_osa_worker_running()
        self.lbl_status.setText("OSA/전류/전압/출력/온도 실시간 수집 시작...")

    def _on_osa_data(self, x: np.ndarray, y: np.ndarray):
        x_arr, y_arr = _prepare_osa_trace(x, y)
        if x_arr.size == 0:
            return

        with self._trace_condition:
            self.x = x_arr
            self.y = y_arr
            self._trace_seq += 1
            self._trace_received_monotonic = time.monotonic()
            self._trace_condition.notify_all()

        # PSU 미연결/0A 상태의 OSA 노이즈를 실제 파장으로 표시하지 않는다.
        if self.psu is None or not self.psu.is_open():
            current_for_validation = 0.0
        else:
            current_for_validation = float(
                self._latest_realtime.get("Current", float("nan"))
            )
            if not np.isfinite(current_for_validation):
                current_for_validation = 0.0

        analysis = _analyze_osa_trace(
            x_arr,
            y_arr,
            current_a=current_for_validation,
        )
        self._set_panel("peak1", analysis["peak1"], "{:.3f}")
        self._set_panel("peak2", analysis["peak2"], "{:.3f}")
        self._set_panel("dbm_diff", analysis["diff"], "{:.2f}")

        if self.burnin_thread is None or not self.burnin_thread.isRunning():
            if analysis["valid"]:
                p2_text = (
                    f"{analysis['peak2']:.3f} nm"
                    if np.isfinite(analysis["peak2"])
                    else "없음"
                )
                diff_text = (
                    f"{analysis['diff']:.2f} dB"
                    if np.isfinite(analysis["diff"])
                    else "-"
                )
                self.lbl_status.setText(
                    f"OSA 실제 신호: Peak1 {analysis['peak1']:.3f} nm / "
                    f"Peak2 {p2_text} / Diff {diff_text}"
                )
            else:
                self.lbl_status.setText(
                    f"OSA Trace 수신 중 / {analysis['reason']}"
                )

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
        return x, y, age

    def _get_peaks(self) -> Tuple[float, float, float]:
        x, y, _age = self._get_trace_snapshot(wait_for_new=False)
        return _calc_osa_peaks(x, y)

    def _get_fresh_peaks(self, current_a: float) -> Tuple[float, float, float]:
        """
        번인 Worker에서 호출한다.

        UI의 queued signal이 처리되는 시점에 의존하지 않고,
        OSAWorker가 내부에 직접 보관한 최신 Trace를 읽는다.
        """
        worker = self.osa_worker
        if worker is None or not worker.isRunning():
            print("번인 OSA Worker가 실행 중이 아닙니다.")
            return 0.0, 0.0, 0.0

        peak1, peak2, diff, seq, age = worker.get_latest_metrics(
            after_seq=self._last_metric_trace_seq,
            timeout_seconds=5.0,
            max_age_seconds=10.0,
            current_a=float(current_a),
        )

        if seq > 0:
            self._last_metric_trace_seq = max(
                self._last_metric_trace_seq,
                int(seq),
            )

        if not np.isfinite(peak1) or peak1 <= 0.0:
            print(
                "번인 OSA 유효 Trace 확보 실패: "
                f"seq={seq}, age={age:.2f}s"
            )
            return float("nan"), float("nan"), float("nan")

        return float(peak1), float(peak2), float(diff)

    # ------------------ burn-in ------------------
    def _on_start_burnin(self):
        if not self._require_connected():
            return

        minutes, ok = QInputDialog.getDouble(
            self, "번인 시간", f"{self._burnin_target_current():g}A 유지 시간을 입력 (분):",
            60.0, 1.0, 6000.0, 0
        )
        if not ok:
            return

        self._ensure_realtime_monitor_running()
        self._ensure_osa_worker_running()

        self.log_records = []
        self.time_list = []
        self.burnin_start_time = time.time()
        self.burnin_hold_start_time = None
        self.burnin_hold_minutes = float(minutes)

        self.lbl_start.setText("번인 시작 시각: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.burnin_start_time)))
        target_a = self._burnin_target_current()
        self.lbl_status.setText(f"번인 시작: 0→{target_a:g}A, {target_a:g}A {int(minutes)}분 유지(5초 기록), {target_a:g}→0A")

        self.btn_burnin.setEnabled(False)

        self.burnin_thread = BurnInThread(
            self.psu,
            self._get_fresh_peaks,
            hold_minutes=minutes,
            target_current_a=target_a,
        )
        print(
            f"[번인 시작] PSU={self.psu_addr} | "
            "Power=Nova II 직접통신 | Temperature=GL840 USB 직접통신"
        )
        self.burnin_thread.update_current.connect(self._on_update_current)
        self.burnin_thread.log_row.connect(self._on_log_row)
        self.burnin_thread.finished_ok.connect(self._on_burnin_ok)
        self.burnin_thread.finished.connect(self._on_burnin_done)
        self.burnin_thread.start()

        self._refresh_conn_ui()

    def _on_update_current(self, value: float):
        self.lbl_current.setText(f"현재 전류: {value:.0f}A")

    def _on_log_row(self, rec: dict):
        if (
            self.burnin_hold_start_time is None
            and float(rec.get("Current", 0)) >= (self._burnin_target_current() - 0.1)
        ):
            self.burnin_hold_start_time = time.time()

        now = time.time()
        t = 0.0 if self.burnin_start_time is None else (now - self.burnin_start_time)

        power = float(rec.get("Power", float("nan")))
        set_current = float(rec.get("Current", float("nan")))
        psu_voltage = float(rec.get("Voltage", float("nan")))

        eff = float("nan")
        try:
            if (
                not np.isnan(power)
                and not np.isnan(set_current)
                and not np.isnan(psu_voltage)
                and set_current > 0
                and psu_voltage > 0
            ):
                eff = power / (psu_voltage * set_current) * 100.0
        except Exception:
            eff = float("nan")

        # Worker가 같은 기록 시점에 잡은 온도 스냅샷을 그대로 사용한다.
        temp_fiber = rec.get("FiberTemp_CH2", float("nan"))
        temp_pkg = rec.get("PKGTemp_CH3", float("nan"))
        temp_base = rec.get("BaseTemp_CH5", float("nan"))

        row = {
            "Time (sec)": t,
            "Time (min)": t / 60.0,
            "Current": set_current,
            "Peak1": float(rec.get("Peak1", 0.0)),
            "Peak2": float(rec.get("Peak2", 0.0)),
            "dBm Diff": float(rec.get("dBm Diff", 0.0)),
            "Power": power,
            "Voltage": psu_voltage,
            "FiberTemp_CH2": float(temp_fiber) if temp_fiber is not None else float("nan"),
            "PKGTemp_CH3": float(temp_pkg) if temp_pkg is not None else float("nan"),
            "BaseTemp_CH5": float(temp_base) if temp_base is not None else float("nan"),
            "효율": float(eff),
        }
        self.log_records.append(row)
        self.time_list.append(t)

        self._set_panel("current", row["Current"], "{:.0f}")
        self._set_panel("peak1", row["Peak1"], "{:.3f}")
        self._set_panel("peak2", row["Peak2"], "{:.3f}")
        self._set_panel("dbm_diff", row["dBm Diff"], "{:.2f}")
        self._set_panel("power", row["Power"], "{:.6g}")
        self._set_panel("voltage", row["Voltage"], "{:.6g}")
        self._set_panel("temp_fiber_ch2", row["FiberTemp_CH2"], "{:.3f}")
        self._set_panel("temp_pkg_ch3", row["PKGTemp_CH3"], "{:.3f}")
        self._set_panel("temp_base_ch5", row["BaseTemp_CH5"], "{:.3f}")
        self._set_panel("eff", row["효율"], "{:.2f}")

        self._update_chart()
        self._refresh_conn_ui()

    def _on_burnin_ok(self):
        _beep_alarm()

    def _on_burnin_done(self):
        self.btn_burnin.setEnabled(True)
        self.lbl_status.setText("번인 종료")
        self._refresh_conn_ui()

    def _update_remaining_time(self):
        if self.burnin_hold_start_time is None or self.burnin_hold_minutes is None:
            self.lbl_remain.setText("남은 번인 시간: -")
            return
        total = self.burnin_hold_minutes * 60.0
        elapsed = max(0.0, time.time() - self.burnin_hold_start_time)
        remain = max(0.0, total - elapsed)
        m = int(remain // 60)
        s = int(remain % 60)
        self.lbl_remain.setText(f"남은 번인 시간: {m:02d}:{s:02d}")

    def _on_stop(self):
        try:
            if self.burnin_thread is not None and self.burnin_thread.isRunning():
                self.burnin_thread.stop()
                self.burnin_thread.wait(2000)
        except Exception:
            pass
        self.btn_burnin.setEnabled(True)

        self.stop_osa_collection()

        try:
            if self.psu is not None and self.psu.is_open():
                try:
                    self.psu.output_off()
                except Exception:
                    pass
        except Exception:
            pass

        self.lbl_status.setText("중지됨")
        self._refresh_conn_ui()

    def stop_osa_collection(self):
        """페이지 전환 시 OSA 세션만 안전하게 종료한다."""
        worker = self.osa_worker
        if worker is None:
            return

        try:
            if worker.isRunning():
                worker.stop()
                worker.wait(6000)
        except Exception as e:
            print(f"번인 OSA 종료 경고: {e}")

        if not worker.isRunning():
            self.osa_worker = None

        with self._trace_condition:
            self._trace_condition.notify_all()

    def _on_softdown(self):
        if self.psu is None or (not self.psu.is_open()):
            QMessageBox.warning(self, "필수", "먼저 PSU를 연결해줘.")
            self._refresh_conn_ui()
            return

        try:
            if self.burnin_thread is not None and self.burnin_thread.isRunning():
                self.burnin_thread.stop()
                self.burnin_thread.wait(500)
        except Exception:
            pass

        if self.softdown_thread is not None and self.softdown_thread.isRunning():
            self.lbl_status.setText("전류내림 진행 중...")
            return

        self.lbl_status.setText("전류내림(0A) 실행 중...")
        self.btn_softdown.setEnabled(False)

        self.softdown_thread = SoftDownThread(self.psu)
        self.softdown_thread.update_current.connect(self._on_update_current)
        self.softdown_thread.finished.connect(self._on_softdown_done)
        self.softdown_thread.start()

        self._refresh_conn_ui()

    def _on_softdown_done(self):
        self.btn_softdown.setEnabled(True)
        self.lbl_status.setText("전류내림 완료(OUTP OFF)")
        self.lbl_current.setText("현재 전류: 0A")
        self._refresh_conn_ui()

    def _on_export(self):
        if self.export_thread is not None and self.export_thread.isRunning():
            QMessageBox.information(self, "저장 중", "번인 Excel 파일을 저장하고 있습니다.")
            return

        if not self.log_records:
            QMessageBox.warning(self, "없음", "저장할 데이터가 없어.")
            return

        module_no = self._save_module_no()
        if not module_no:
            module_no, ok = QInputDialog.getText(
                self,
                "모듈번호",
                "저장할 모듈번호를 입력하세요. 예: E092",
            )
            if not ok or not (module_no or "").strip():
                self.lbl_status.setText("엑셀 저장 취소: 모듈번호 없음")
                return
            module_no = _normalize_module_no(module_no)
            self.ed_save_module.setText(module_no)

        self._refresh_save_groups()

        try:
            base_dir = self._burnin_save_dir()
            os.makedirs(base_dir, exist_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "저장 경로 오류", str(exc))
            return

        default_path = str(Path(base_dir) / f"{module_no}_burnin.xlsx")
        chosen_path, _ = QFileDialog.getSaveFileName(
            self,
            "엑셀 저장",
            default_path,
            "Excel Files (*.xlsx)",
        )
        if not chosen_path:
            return

        # 기존 운영 규칙 유지: 사용자가 고른 파일명만 사용하고 실제 저장 폴더는
        # Parameter의 번인 저장 폴더/모델/월그룹으로 고정한다.
        # 대화상자에서 선택한 경로 전체를 사용하지 않고 파일명만 취한다.
        # 따라서 다른 폴더를 선택해도 실제 저장 루트 밖으로 이탈하지 않는다.
        chosen_name = Path(chosen_path).name.strip()
        if not chosen_name:
            QMessageBox.warning(self, "저장 취소", "파일명이 비어 있습니다.")
            return
        if not chosen_name.lower().endswith(".xlsx"):
            chosen_name += ".xlsx"

        # Windows 금지 문자와 예약 장치명을 제거해 다른 PC에서도 안전하게 저장한다.
        chosen_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", chosen_name)
        chosen_name = chosen_name.rstrip(" .")
        if not chosen_name or chosen_name.lower() in {".xlsx", "con.xlsx", "prn.xlsx", "aux.xlsx", "nul.xlsx"}:
            chosen_name = f"{module_no}_burnin.xlsx"

        safe_base_dir = Path(base_dir).resolve()
        safe_base_dir.mkdir(parents=True, exist_ok=True)
        file_path = str((safe_base_dir / chosen_name).resolve())

        # 번인 진행 중에도 안전하게 저장할 수 있도록 현재 기록을 복사해 고정한다.
        records_snapshot = [dict(r) for r in list(self.log_records)]
        model_code = normalize_model_code(self._selected_model())

        self._export_row_count = len(records_snapshot)
        self.btn_export.setEnabled(False)
        self.lbl_status.setText(
            f"엑셀 저장 중... {self._export_row_count}개 데이터 / {file_path}"
        )

        self.export_thread = BurninExcelExportThread(
            file_path=file_path,
            records=records_snapshot,
            model_code=model_code,
            module_no=module_no,
            parent=self,
        )
        self.export_thread.succeeded.connect(self._on_export_success)
        self.export_thread.failed.connect(self._on_export_failed)
        self.export_thread.finished.connect(self._on_export_finished)
        self.export_thread.start()

    def _on_export_success(self, file_path: str):
        try:
            module_no = self._save_module_no()
            if self.repo is not None and hasattr(self.repo, "upsert_module"):
                self.repo.upsert_module(
                    module_no=module_no,
                    model=self._selected_model(),
                )
        except Exception as exc:
            print(f"번인 저장 후 모듈 DB 등록 경고: {exc}")

        self.lbl_status.setText(f"엑셀 저장 완료: {file_path}")
        QMessageBox.information(
            self,
            "저장 완료",
            f"번인 데이터 {self._export_row_count}개를 저장했습니다.\n\n{file_path}",
        )

    def _on_export_failed(self, message: str):
        self.lbl_status.setText("엑셀 저장 실패")
        QMessageBox.critical(
            self,
            "엑셀 저장 실패",
            "프로그램은 종료되지 않았습니다.\n\n"
            f"원인:\n{message}\n\n"
            "같은 파일이 Excel에서 열려 있으면 닫은 뒤 다시 저장하세요.",
        )

    def _on_export_finished(self):
        self.btn_export.setEnabled(True)
        worker = self.export_thread
        self.export_thread = None
        if worker is not None:
            worker.deleteLater()

    def _update_chart(self):
        self.out_ax.cla()
        if self.log_records:
            t = np.array([r["Time (sec)"] for r in self.log_records], dtype=float) / 60.0
            p = np.array([r["Power"] for r in self.log_records], dtype=float)
            mask = ~np.isnan(t) & ~np.isnan(p)
            if np.any(mask):
                self.out_ax.plot(t[mask], p[mask], marker="o", linestyle="-")
        self.out_ax.set_xlabel("Time (min)")
        self.out_ax.set_ylabel("Output (W)")
        self.out_ax.grid(True)
        self.out_canvas.draw()

    def _set_panel(self, key: str, val: float, fmt: str):
        if key not in self.data_labels:
            return
        try:
            numeric = float(val)
        except Exception:
            self.data_labels[key].setText("-")
            return
        if not np.isfinite(numeric):
            self.data_labels[key].setText("-")
        else:
            self.data_labels[key].setText(fmt.format(numeric))

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_realtime_monitor_running()

    def hideEvent(self, event):
        # MeasurementTab은 동작 중 페이지 전환을 차단한다.
        # 따라서 숨겨질 때 idle이면 실시간 장비 polling을 중지한다.
        if not self._is_busy():
            self._stop_realtime_monitor(wait_ms=500)
        super().hideEvent(event)

    def closeEvent(self, event):
        try:
            self._stop_realtime_monitor(wait_ms=1500)
        except Exception:
            pass
        try:
            self._on_stop()
        except Exception:
            pass
        try:
            if self.softdown_thread is not None and self.softdown_thread.isRunning():
                self.softdown_thread.stop()
                self.softdown_thread.wait(1000)
        except Exception:
            pass
        try:
            if self.psu is not None:
                self.psu.close()
        except Exception:
            pass
        try:
            if self.export_thread is not None and self.export_thread.isRunning():
                self.export_thread.wait(15000)
        except Exception:
            pass
        super().closeEvent(event)
