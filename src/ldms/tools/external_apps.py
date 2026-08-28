# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class LaunchResult:
    ok: bool
    message: str


class ExternalAppManager:
    """
    외부 장비 프로그램(EXE) 실행/종료 관리
    - 같은 key로 여러 번 실행 요청 시: 이미 실행 중이면 재실행하지 않음

    ✅ 주의:
    - 일부 장비 프로그램은 "런처 EXE"가 바로 종료되고, 실제 프로그램은 다른 프로세스로 뜰 수 있음
      -> 이 경우 Popen으로 잡은 pid만으로는 '실행중' 판단이 흔들릴 수 있음
      -> 그래서 launch 직후 즉시 종료되는 경우를 체크해 안내 메시지를 강화함
    """

    def __init__(self):
        self._procs: Dict[str, subprocess.Popen] = {}

    def is_running(self, key: str) -> bool:
        p = self._procs.get(key)
        return bool(p) and (p.poll() is None)

    def launch(self, key: str, exe_path: str, workdir: Optional[str] = None) -> LaunchResult:
        exe_path = (exe_path or "").strip().strip('"')

        if not exe_path:
            return LaunchResult(False, "EXE 경로가 비어있습니다. Parameter 탭에서 설정하세요.")

        # ✅ 폴더/파일 구분
        if not os.path.exists(exe_path):
            return LaunchResult(False, f"EXE 경로가 존재하지 않습니다:\n{exe_path}")
        if not os.path.isfile(exe_path):
            return LaunchResult(False, f"EXE 파일이 아닙니다(폴더일 수 있음):\n{exe_path}")

        if self.is_running(key):
            return LaunchResult(True, "이미 실행 중입니다.")

        try:
            cwd = workdir if workdir else (os.path.dirname(exe_path) or None)

            # ✅ UI 실행은 창이 떠야 하므로 NO_WINDOW 옵션 금지
            p = subprocess.Popen([exe_path], cwd=cwd)
            self._procs[key] = p

            # ✅ 런처가 즉시 종료되는 케이스(실패/권한/호환/런처 구조) 감지
            time.sleep(0.15)
            rc = p.poll()
            if rc is not None:
                # 즉시 종료된 상태 -> 성공일 수도 있지만(런처형) 실패일 가능성이 더 큼
                # 사용자에게 원인 추적 힌트 제공
                msg = (
                    "프로그램이 바로 종료되었습니다.\n\n"
                    "가능한 원인:\n"
                    "- 경로가 런처/업데이터 EXE라서 실제 프로그램이 별도 프로세스로 실행됨\n"
                    "- 관리자 권한 필요 / 보안 차단 / 호환 문제\n"
                    "- EXE 자체 오류\n\n"
                    f"종료 코드: {rc}\n"
                    f"경로: {exe_path}"
                )
                return LaunchResult(False, msg)

            return LaunchResult(True, "실행 완료")
        except Exception as e:
            return LaunchResult(False, f"실행 실패: {e}")

    def terminate(self, key: str, timeout_sec: float = 2.0) -> LaunchResult:
        """
        ✅ 종료는 terminate -> 대기 -> 필요시 kill 까지
        (장비프로그램이 종료 신호 무시하는 경우가 있어서 안전장치)
        """
        p = self._procs.get(key)
        if not p:
            return LaunchResult(True, "실행 기록이 없습니다.")
        if p.poll() is not None:
            return LaunchResult(True, "이미 종료된 상태입니다.")

        try:
            p.terminate()

            t0 = time.time()
            while time.time() - t0 < float(timeout_sec):
                if p.poll() is not None:
                    return LaunchResult(True, "종료(terminate) 완료")
                time.sleep(0.05)

            # 여기까지 왔으면 terminate로 안 죽음 -> kill 시도
            try:
                p.kill()
                return LaunchResult(True, "종료(kill) 완료")
            except Exception as e2:
                return LaunchResult(False, f"종료 실패(terminate 후 kill 실패): {e2}")

        except Exception as e:
            return LaunchResult(False, f"종료 실패: {e}")

    def terminate_all(self) -> None:
        for k in list(self._procs.keys()):
            try:
                self.terminate(k)
            except Exception:
                pass
