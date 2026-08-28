# -*- coding: utf-8 -*-
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Dict


# 32-bit Graphtec reader location.
# Keep graphtec_reader32.py beside the main EXE or in src/ldms/ui/tools.
GRAPHTEC_READER_NAME = "graphtec_reader32.py"

_NOVA_LOCK = threading.RLock()
_NOVA_BY_THREAD: dict[int, "NovaDirectReader"] = {}


def _candidate_graphtec_reader_paths() -> list[Path]:
    candidates: list[Path] = []

    # Source run: this module and helper are in the same folder.
    candidates.append(Path(__file__).resolve().with_name(GRAPHTEC_READER_NAME))

    # PyInstaller one-dir / executable folder.
    try:
        candidates.append(Path(sys.executable).resolve().parent / GRAPHTEC_READER_NAME)
    except Exception:
        pass

    # PyInstaller extraction folder.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / GRAPHTEC_READER_NAME)
        candidates.append(Path(meipass) / "src" / "ldms" / "ui" / "tools" / GRAPHTEC_READER_NAME)

    # Project root fallback.
    try:
        candidates.append(Path(__file__).resolve().parents[4] / GRAPHTEC_READER_NAME)
    except Exception:
        pass

    # De-duplicate.
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _find_graphtec_reader() -> Path:
    for path in _candidate_graphtec_reader_paths():
        if path.exists():
            return path
    raise FileNotFoundError(
        "graphtec_reader32.py를 찾지 못했습니다. "
        "이 파일을 LDModuleSystem.exe 옆 또는 "
        "src\\ldms\\ui\\tools 폴더에 넣어주세요."
    )


def _candidate_python32_paths() -> list[Path]:
    home = Path.home()
    candidates = [
        home / "AppData" / "Local" / "Programs" / "Python" / "Python311-32" / "python.exe",
        Path(r"C:\Python311-32\python.exe"),
    ]

    env_path = os.environ.get("GRAPHETC_PYTHON32", "").strip()
    if env_path:
        candidates.insert(0, Path(env_path))

    return candidates


def _find_python32() -> Path:
    for path in _candidate_python32_paths():
        if path.exists():
            return path
    raise FileNotFoundError(
        "Python 3.11 32비트 실행파일을 찾지 못했습니다. "
        r"예상 경로: %LOCALAPPDATA%\Programs\Python\Python311-32\python.exe"
    )


def read_graphtec_temperatures_direct(timeout_seconds: float = 12.0) -> Dict[str, float]:
    """
    64-bit main process -> 32-bit Python helper -> gtcusbr.dll -> GL840 USB.

    Returns:
      {"CH2": float, "CH3": float, "CH5": float}
    """
    python32 = _find_python32()
    reader = _find_graphtec_reader()

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    completed = subprocess.run(
        [str(python32), str(reader)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        creationflags=flags,
    )

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if completed.returncode != 0:
        raise RuntimeError(
            f"Graphtec Reader 실패(returncode={completed.returncode}): "
            f"{stderr or stdout or '응답 없음'}"
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Graphtec Reader JSON 파싱 실패: {stdout[:500]}") from exc

    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "Graphtec Reader 실패"))

    result = {
        "CH2": float(payload["ch2"]),
        "CH3": float(payload["ch3"]),
        "CH5": float(payload["ch5"]),
    }

    print(
        f"[GL840 DIRECT] at={payload.get('measured_at')} | "
        f"CH2={result['CH2']:.3f} | CH3={result['CH3']:.3f} | CH5={result['CH5']:.3f}"
    )
    return result


class NovaDirectReader:
    """Ophir Nova II direct USB reader. Must be used only by its owner thread."""

    def __init__(self) -> None:
        self.pythoncom = None
        self.com = None
        self.handle = None
        self.latest_valid: Optional[float] = None
        self.serial: Optional[str] = None

    @staticmethod
    def _as_list(value) -> list:
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            return [value]
        try:
            return list(value)
        except TypeError:
            return [value]

    @staticmethod
    def _normalize_serials(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        try:
            return [str(x) for x in value if str(x).strip()]
        except TypeError:
            return [str(value)]

    def open(self) -> None:
        if self.handle is not None:
            return

        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Nova II 직접 통신에 pywin32가 필요합니다. "
                "pip install pywin32를 실행하세요."
            ) from exc

        pythoncom.CoInitialize()
        self.pythoncom = pythoncom

        try:
            self.com = win32com.client.Dispatch(
                "OphirLMMeasurement.CoLMMeasurement"
            )
            serials = self._normalize_serials(self.com.ScanUSB())
            if not serials:
                raise RuntimeError("USB Ophir 장비를 찾지 못했습니다.")

            self.serial = serials[0]
            self.handle = self.com.OpenUSBDevice(self.serial)
            self.com.StartStream(self.handle, 0)
            time.sleep(0.20)
            print(f"[NOVA SERVICE] 연결 성공 | serial={self.serial}")
        except Exception:
            self.abandon()
            raise

    def read_fresh_power(self, timeout_seconds: float = 2.0) -> float:
        """
        호출 시점 이후의 새 정상 샘플만 반환한다.
        Nova COM 객체는 전용 service thread 안에서만 사용된다.
        """
        self.open()
        self.latest_valid = None

        # 이전 측정 구간 버퍼 제거.
        try:
            self.com.GetData(self.handle, 0)
        except Exception as exc:
            raise RuntimeError(f"Nova II 버퍼 정리 실패: {exc}") from exc

        deadline = time.monotonic() + max(0.3, float(timeout_seconds))
        while time.monotonic() < deadline:
            values, timestamps, statuses = self.com.GetData(self.handle, 0)

            values_l = self._as_list(values)
            timestamps_l = self._as_list(timestamps)
            statuses_l = self._as_list(statuses)

            n = min(len(values_l), len(timestamps_l), len(statuses_l))
            for i in range(n):
                try:
                    value = float(values_l[i])
                    status = int(statuses_l[i])
                except (TypeError, ValueError):
                    continue

                if status == 0:
                    self.latest_valid = value
                elif status == 1:
                    raise RuntimeError("Nova II Overrange(status=1)")
                elif status == 2:
                    raise RuntimeError("Nova II Saturated(status=2)")

            if self.latest_valid is not None:
                print(f"[NOVA SERVICE] Power={self.latest_valid:.6g} W")
                return float(self.latest_valid)

            time.sleep(0.04)

        raise TimeoutError("Nova II 새 정상 Power 데이터를 받지 못했습니다.")

    def abandon(self) -> None:
        """
        COM 종료 함수가 장비/드라이버 상태에 따라 멈추는 사례가 있어,
        측정 완료 경로에서는 StopStream/Close를 호출하지 않는다.

        전용 daemon thread가 프로세스 수명 동안 세션을 유지하고,
        앱 종료 시 OS가 프로세스 자원과 USB handle을 정리한다.
        """
        self.handle = None
        self.com = None
        self.latest_valid = None
        self.serial = None

        if self.pythoncom is not None:
            try:
                self.pythoncom.CoUninitialize()
            except Exception:
                pass
            self.pythoncom = None


class _NovaRequest:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.done = threading.Event()
        self.value: Optional[float] = None
        self.error: Optional[BaseException] = None


class NovaPowerService:
    """
    Nova COM 객체를 하나의 전용 daemon thread에서만 소유한다.

    기존 문제:
    - 자동측정 QThread 종료 중 StopStream/Close가 멈춰 finished가 안 나옴
    - UI의 측정중지가 auto_thread.wait()에서 무한 대기해 응답없음
    - QThread ID 재사용으로 이전 COM handle이 재사용됨

    해결:
    - 자동측정 thread는 COM 객체를 직접 만들거나 닫지 않음
    - 모든 Power 요청을 전용 service thread로 전달
    - 측정 종료 때 Nova 종료 작업을 하지 않으므로 즉시 finished 처리
    """
    def __init__(self) -> None:
        import queue

        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="NovaPowerService",
            daemon=True,
        )
        self._start_lock = threading.Lock()
        self._started = False

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="NovaPowerService",
                daemon=True,
            )
            self._started = True
            self._thread.start()

    def read_power(self, timeout_seconds: float = 2.0) -> float:
        self._ensure_started()

        request = _NovaRequest(timeout_seconds)
        self._queue.put(request)

        # COM read timeout + 연결/재시도 여유.
        wait_timeout = max(3.0, float(timeout_seconds) + 4.0)
        if not request.done.wait(wait_timeout):
            raise TimeoutError("Nova Power service 응답 시간 초과")

        if request.error is not None:
            raise RuntimeError(
                f"Nova II Power 읽기 실패: "
                f"{type(request.error).__name__}: {request.error}"
            )
        if request.value is None:
            raise RuntimeError("Nova II Power 값이 비어 있습니다.")
        return float(request.value)

    def _run(self) -> None:
        reader: Optional[NovaDirectReader] = None

        while True:
            request = self._queue.get()
            if request is None:
                return

            try:
                if reader is None:
                    reader = NovaDirectReader()

                try:
                    request.value = reader.read_fresh_power(
                        timeout_seconds=request.timeout_seconds
                    )
                except Exception as first_error:
                    print(
                        f"[NOVA SERVICE] 첫 읽기 실패, 새 세션으로 재시도: "
                        f"{type(first_error).__name__}: {first_error}"
                    )

                    # StopStream/Close를 호출하면 hang 가능성이 있으므로
                    # 기존 객체 참조만 버리고 전용 thread에서 새 COM 객체 생성.
                    try:
                        reader.abandon()
                    except Exception:
                        pass
                    reader = NovaDirectReader()

                    request.value = reader.read_fresh_power(
                        timeout_seconds=max(2.0, request.timeout_seconds)
                    )

            except BaseException as exc:
                request.error = exc
            finally:
                request.done.set()


_NOVA_SERVICE = NovaPowerService()


def read_nova_power_direct(timeout_seconds: float = 2.0) -> float:
    return _NOVA_SERVICE.read_power(timeout_seconds=timeout_seconds)


def release_nova_for_current_thread() -> None:
    """
    하위호환용 no-op.

    Nova는 이제 자동측정 QThread가 아니라 전용 daemon service thread가
    프로그램 실행 중 계속 소유하므로 측정 종료 시 닫지 않는다.
    """
    return


def close_direct_measurement_devices() -> None:
    """
    앱 종료 시에도 blocking StopStream/Close를 수행하지 않는다.
    daemon service thread와 USB/COM 자원은 프로세스 종료 시 OS가 정리한다.
    """
    return


atexit.register(close_direct_measurement_devices)
