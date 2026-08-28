from __future__ import annotations

import ctypes
import json
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


DLL_PATH = Path(r"C:\Program Files (x86)\Graphtec\GL-Connection\gtcusbr.dll")
COMMAND = b":MEAS:OUTP:ONEJSON?\r\n"


def output(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {
        "BURN OUT",
        "BURNOUT",
        "OFF",
        "OVER",
        "NAN",
        "---",
    }:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class GraphtecUsb:
    def __init__(self, dll_path: Path) -> None:
        self.dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        self.handle = None
        self._configure()

    def _configure(self) -> None:
        self.dll.GtcUSBr_OpenDevice.argtypes = []
        self.dll.GtcUSBr_OpenDevice.restype = wintypes.HANDLE

        self.dll.GtcUSBr_CloseDevice.argtypes = [wintypes.HANDLE]
        self.dll.GtcUSBr_CloseDevice.restype = wintypes.BOOL

        self.dll.GtcUSBr_WriteDevice.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        ]
        self.dll.GtcUSBr_WriteDevice.restype = wintypes.BOOL

        self.dll.GtcUSBr_ReadDevice.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        ]
        self.dll.GtcUSBr_ReadDevice.restype = wintypes.BOOL

        self.dll.GtcUSBr_GetLastError.argtypes = []
        self.dll.GtcUSBr_GetLastError.restype = wintypes.DWORD

    def last_error(self) -> int:
        return int(self.dll.GtcUSBr_GetLastError())

    def open(self) -> None:
        self.handle = self.dll.GtcUSBr_OpenDevice()
        value = ctypes.cast(self.handle, ctypes.c_void_p).value if self.handle else 0
        if value in (0, ctypes.c_void_p(-1).value):
            raise RuntimeError(f"OpenDevice 실패 | LastError={self.last_error()}")

    def close(self) -> None:
        if self.handle:
            self.dll.GtcUSBr_CloseDevice(self.handle)
            self.handle = None

    def query(self, payload: bytes) -> bytes:
        if not self.handle:
            raise RuntimeError("GL840 장치가 열려 있지 않습니다.")

        write_buf = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD(0)

        ok = bool(
            self.dll.GtcUSBr_WriteDevice(
                self.handle,
                ctypes.cast(write_buf, wintypes.LPVOID),
                len(payload),
                ctypes.byref(written),
                3000,
            )
        )
        if not ok:
            raise RuntimeError(f"WriteDevice 실패 | LastError={self.last_error()}")
        if written.value != len(payload):
            raise RuntimeError(f"전송 길이 불일치 {written.value}/{len(payload)}")

        chunks: list[bytes] = []
        deadline = time.monotonic() + 5.0

        while time.monotonic() < deadline:
            read_buf = ctypes.create_string_buffer(131072)
            count = wintypes.DWORD(0)

            ok = bool(
                self.dll.GtcUSBr_ReadDevice(
                    self.handle,
                    ctypes.cast(read_buf, wintypes.LPVOID),
                    131072,
                    ctypes.byref(count),
                    1200,
                )
            )

            if not ok and count.value == 0:
                if chunks:
                    break
                raise RuntimeError(
                    f"ReadDevice 실패 | LastError={self.last_error()}"
                )

            chunk = bytes(read_buf.raw[: count.value])
            if not chunk:
                break

            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"\n" in joined or b"\r" in joined or b"\x00" in joined:
                break

        return b"".join(chunks).rstrip(b"\x00\r\n ")


def parse_gl840_json(text: str) -> dict[str, Any]:
    root = json.loads(text)

    measured_at: str | None = None
    channels: dict[str, dict[str, Any]] = {}

    for entry in root.get("datas", []):
        if not isinstance(entry, dict):
            continue

        name = str(entry.get("name", "")).strip().lower()
        items = entry.get("items", {})
        if not isinstance(items, dict):
            continue

        if name == "date":
            measured_at = str(items.get("value", "")).strip() or None
            continue

        if name != "ch":
            continue

        channel_no = str(items.get("ch", "")).strip()
        if not channel_no:
            continue

        channels[f"CH{channel_no}"] = {
            "value": to_float(items.get("value")),
            "raw_value": items.get("value"),
            "unit": items.get("unit"),
            "annotation": items.get("annot"),
            "alarm": items.get("alarm"),
        }

    def channel_value(name: str) -> float | None:
        info = channels.get(name, {})
        value = info.get("value")
        return float(value) if isinstance(value, (int, float)) else None

    return {
        "ok": True,
        "device_id": root.get("id"),
        "measured_at": measured_at,
        "ch2": channel_value("CH2"),
        "ch3": channel_value("CH3"),
        "ch5": channel_value("CH5"),
        "channels": {
            "CH2": channels.get("CH2"),
            "CH3": channels.get("CH3"),
            "CH5": channels.get("CH5"),
        },
    }


def main() -> int:
    if struct.calcsize("P") * 8 != 32:
        return output(
            {
                "ok": False,
                "error": "32비트 Python으로 실행해야 합니다.",
                "python": sys.executable,
                "bitness": struct.calcsize("P") * 8,
            },
            2,
        )

    if not DLL_PATH.exists():
        return output(
            {
                "ok": False,
                "error": "gtcusbr.dll을 찾지 못했습니다.",
                "dll": str(DLL_PATH),
            },
            3,
        )

    device = GraphtecUsb(DLL_PATH)

    try:
        device.open()
        raw = device.query(COMMAND)
        text = raw.decode("utf-8", errors="replace").strip()

        result = parse_gl840_json(text)
        result["raw_text"] = text
        return output(result, 0)

    except Exception as exc:
        return output(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
            1,
        )

    finally:
        try:
            device.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
