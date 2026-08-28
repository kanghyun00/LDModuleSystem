# -*- coding: utf-8 -*-
from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTabWidget

from src.ldms.ui.tabs.parameter_model_tab import ParameterModelTab
from src.ldms.ui.tabs.parameter_module_tab import ParameterModuleTab
from src.ldms.ui.tabs.parameter_grading_tab import ParameterGradingTab
from src.ldms.ui.tabs.parameter_paths_tab import ParameterPathsTab


class ParameterTab(QWidget):
    # 하위 모델설정 탭의 공정순서 변경 이벤트를 MainWindow로 전달
    model_process_flow_changed = pyqtSignal(str)

    """
    Parameter 탭 (카테고리 분리 컨테이너)
    - 탭1: 모델 설정
    - 탭2: 모듈-모델 관리
    - 탭3: 분류/판정 설정
    - 탭4: 경로 설정
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()

        self.model_tab = ParameterModelTab(repo=self.repo)
        self.module_tab = ParameterModuleTab(repo=self.repo)
        self.grading_tab = ParameterGradingTab(repo=self.repo)
        self.paths_tab = ParameterPathsTab(repo=self.repo)

        self.tabs.addTab(self.model_tab, "모델 설정")
        self.tabs.addTab(self.module_tab, "모듈-모델 관리")
        self.tabs.addTab(self.grading_tab, "분류/판정")
        self.tabs.addTab(self.paths_tab, "경로 설정")

        # 모델 공정순서 저장 → MainWindow로 전달
        self.model_tab.model_process_flow_changed.connect(
            self.model_process_flow_changed.emit
        )

        root.addWidget(self.tabs)
