# -*- coding: utf-8 -*-
from __future__ import annotations

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QStackedWidget, QMessageBox,
)

from src.ldms.ui.tools.osa_automeasure_window import OSAAutoMeasureWindow
from src.ldms.ui.tools.burnin_window import BurnInWindow


class MeasurementTab(QWidget):
    """
    측정 탭
    - 탭 내부 페이지 전환(QStackedWidget)
    - 기본 페이지: 장비 자동측정(OSA/PSU)
    - 번인 테스트 버튼으로 번인 페이지 전환
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo

        self._auto_page = None
        self._burnin_page = None
        self._psu_prompted_once = False

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # ===== 상단 페이지 전환 메뉴 =====
        top = QHBoxLayout()

        self.btn_auto = QPushButton("장비 자동측정(OSA/PSU)")
        self.btn_burnin = QPushButton("번인 테스트")

        top.addWidget(self.btn_auto)
        top.addWidget(self.btn_burnin)
        top.addStretch(1)
        root.addLayout(top)

        # ===== 페이지 스택 =====
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self._auto_page = OSAAutoMeasureWindow(repo=self.repo)
        self.stack.addWidget(self._auto_page)

        self._burnin_page = BurnInWindow(repo=self.repo)
        self.stack.addWidget(self._burnin_page)

        self.stack.setCurrentWidget(self._auto_page)

        self.btn_auto.clicked.connect(self._go_auto_page)
        self.btn_burnin.clicked.connect(self._go_burnin_page)

    def _go_auto_page(self):
        # 번인 중 페이지를 바꾸면 OSA/PSU 상태가 꼬일 수 있으므로 차단한다.
        try:
            if self._burnin_page is not None and self._burnin_page.is_device_busy():
                QMessageBox.warning(
                    self,
                    "번인 진행 중",
                    "번인 진행 중에는 자동측정 화면으로 전환할 수 없습니다.\n"
                    "먼저 번인을 중지하거나 완료하세요.",
                )
                return
        except Exception:
            pass

        # 번인 화면이 가진 OSA/PSU 세션을 모두 닫고 자동측정으로 전환한다.
        # 같은 실제 장비에 두 화면의 세션이 동시에 붙는 것을 막는다.
        try:
            if (
                self._burnin_page is not None
                and not self._burnin_page.release_devices_for_page_switch()
            ):
                QMessageBox.warning(self, "장비 사용 중", "번인 장비 세션을 정리하지 못했습니다.")
                return
        except Exception as e:
            QMessageBox.warning(self, "장비 정리 실패", f"번인 장비 세션 정리 실패:\n{e}")
            return

        self.stack.setCurrentWidget(self._auto_page)
        self._ensure_psu_prompt_once()

    def _go_burnin_page(self):
        # 자동측정 진행 중 페이지를 바꾸면 PSU 출력과 OSA session이 꼬일 수 있다.
        try:
            if (
                self._auto_page is not None
                and self._auto_page.is_device_busy()
            ):
                QMessageBox.warning(
                    self,
                    "자동측정 진행 중",
                    "자동측정 진행 중에는 번인 화면으로 전환할 수 없습니다.\n"
                    "먼저 자동측정을 중지하거나 완료하세요.",
                )
                return
        except Exception:
            pass

        # 자동측정 화면이 가진 OSA/PSU 세션을 모두 닫고 번인으로 전환한다.
        # 실제 장비는 한 번에 한 화면만 소유한다.
        try:
            if (
                self._auto_page is not None
                and not self._auto_page.release_devices_for_page_switch()
            ):
                QMessageBox.warning(self, "장비 사용 중", "자동측정 장비 세션을 정리하지 못했습니다.")
                return
        except Exception as e:
            QMessageBox.warning(self, "장비 정리 실패", f"자동측정 장비 세션 정리 실패:\n{e}")
            return

        self.stack.setCurrentWidget(self._burnin_page)

    def _ensure_psu_prompt_once(self):
        if self._psu_prompted_once:
            return
        self._psu_prompted_once = True
        try:
            self._auto_page.ensure_psu_connected_lazy(prompt=True)
        except Exception:
            # 측정 탭 진입 자체가 실패하지 않도록 연결 안내 오류는 무시한다.
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_psu_prompt_once()
