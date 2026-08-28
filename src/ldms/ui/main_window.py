# -*- coding: utf-8 -*-
# src/ldms/ui/main_window.py
from __future__ import annotations
from src.ldms.version import APP_VERSION, APP_NAME

import logging
import datetime

from PyQt5.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QLabel, QPushButton, QMessageBox
)
from PyQt5.QtGui import QIcon

from src.ldms.utils.resource_path import resource_path
from src.ldms.ui.tabs.process_tab import ProcessTab
from src.ldms.ui.tabs.measurement_tab import MeasurementTab
from src.ldms.ui.tabs.search_tab import SearchTab
from src.ldms.ui.tabs.materials_tab import MaterialsTab
from src.ldms.ui.tabs.parameter_tab import ParameterTab
from src.ldms.ui.tabs.bom_tab import BomTab
from src.ldms.ui.tabs.inventory_tab import InventoryTab

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, repo):
        super().__init__()
        self.repo = repo  # ✅ app.py에서 주입된 repo(배포 안전 경로)

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1200, 800)

        # ✅ (중요) 창 아이콘(좌상단/작업표시줄) 설정
        self.setWindowIcon(QIcon(resource_path("assets/company_logo.ico")))

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # 탭 인스턴스를 보관해서 탭 간 변경 신호를 연결한다.
        self.process_tab = ProcessTab(repo=self.repo)
        self.measurement_tab = MeasurementTab(repo=self.repo)
        self.search_tab = SearchTab(repo=self.repo)
        self.materials_tab = MaterialsTab(repo=self.repo)
        self.bom_tab = BomTab(repo=self.repo)
        self.inventory_tab = InventoryTab(repo=self.repo)
        self.parameter_tab = ParameterTab(repo=self.repo)

        self.tabs.addTab(self.process_tab, "공정관리")
        self.tabs.addTab(self.measurement_tab, "측정")
        self.tabs.addTab(self.search_tab, "검색/비교")
        self.tabs.addTab(self.materials_tab, "자재관리")
        self.tabs.addTab(self.bom_tab, "BOM")
        self.tabs.addTab(self.inventory_tab, "재고관리")
        self.tabs.addTab(self.parameter_tab, "Parameter")

        # Parameter에서 모델별 공정순서 저장 시,
        # 이미 입력된 PMS/DB 기록까지 현재 순서 기준으로 진행현황 즉시 재계산.
        self.parameter_tab.model_process_flow_changed.connect(
            self.process_tab.on_model_process_flow_changed
        )

        log.info("MainWindow initialized")

    def _placeholder(self, text: str) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel(text))
        layout.addStretch(1)
        return w

    def _pms_test_widget(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel("DB 연결/저장 테스트(신규 스키마 기준)"))

        btn = QPushButton("DB 테스트: 샘플 공정 1건 저장 → 모듈로 조회")
        layout.addWidget(btn)

        def on_click():
            today = datetime.date.today().strftime("%Y-%m-%d")

            module_nos = ["A000_TEST", "A001_TEST"]

            for m in module_nos:
                self.repo.upsert_module(m, model="LD", lot="LOT_TEST", customer="TEST")

            self.repo.insert_process_log(
                work_date=today,
                process_name="파이버정렬",
                process_name_raw="파이버 정렬",
                qty=2,
                remark="DB 테스트",
                operator="system",
                module_nos=module_nos,
            )

            rows = self.repo.get_process_logs_by_module("A000_TEST")
            QMessageBox.information(
                self,
                "DB 테스트 결과",
                f"저장/조회 성공!\n"
                f"- 저장 모듈 수: {len(module_nos)}\n"
                f"- A000_TEST 조회 건수: {len(rows)}"
            )

        btn.clicked.connect(on_click)

        layout.addStretch(1)
        return w
