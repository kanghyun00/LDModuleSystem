# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox,
    QGroupBox, QFormLayout, QLineEdit, QFileDialog, QFrame, QCheckBox
)

from src.ldms.app_config import (
    PathSettings,
    load_path_settings,
    save_path_settings,
    normalize_and_fill_defaults,
)

from src.ldms.process_flow import (
    DEFAULT_MODEL_CODE,
    list_model_codes,
    normalize_model_code,
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


def _hr() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _year_group_names(year: int) -> list[str]:
    # 기존 운영 방식: A~L을 월별 모듈 그룹으로 사용
    return [f"{year}-{chr(ord('A') + i)}" for i in range(12)]


class ParameterPathsTab(QWidget):
    """측정/검색/리포트에 사용하는 저장 경로 설정 탭."""

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._build_ui()
        self.load_settings()

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("경로 설정")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        root.addWidget(title)
        root.addWidget(
            QLabel(
                "※ 여기서 저장한 경로는 측정/번인/공정 엑셀 저장, "
                "검색·비교 RAW 스캔 및 로그 저장에 사용됩니다."
            )
        )
        root.addWidget(_hr())

        # ---------- 데이터/저장 폴더 ----------
        g1 = QGroupBox("데이터/저장 폴더 경로")
        f1 = QFormLayout(g1)

        self.ed_export_measure, w = self._folder_path_row()
        f1.addRow("측정 저장 폴더", w)

        self.ed_export_burnin, w = self._folder_path_row()
        f1.addRow("번인 저장 폴더", w)

        self.ed_search_raw_root, w = self._folder_path_row()
        f1.addRow("검색/비교 RAW ROOT(Stage/Group 스캔)", w)

        self.ed_export_20a_grading, w = self._folder_path_row()
        f1.addRow("20A 등급분류 엑셀 저장 폴더", w)

        self.ed_export_process, w = self._folder_path_row()
        f1.addRow("공정(주/월간 등) 엑셀 저장 폴더", w)

        self.ed_reports_dir, w = self._folder_path_row()
        f1.addRow("리포트 폴더(비우면 공정 저장 폴더 사용)", w)

        self.ed_logs_dir, w = self._folder_path_row()
        f1.addRow("로그 폴더", w)

        self.ed_import_default, w = self._folder_path_row()
        f1.addRow("파일선택 기본 폴더", w)

        root.addWidget(g1)

        # ---------- 폴더 구조 생성 ----------
        g2 = QGroupBox("측정/번인 폴더 구조 생성")
        l2 = QVBoxLayout(g2)
        l2.addWidget(
            QLabel(
                "※ 저장 경로 아래에 단계/모델/월그룹 폴더를 미리 생성합니다. "
                "기존 파일은 이동하거나 삭제하지 않습니다."
            )
        )

        row2 = QHBoxLayout()
        self.chk_make_month_groups = QCheckBox("현재년도 A~L 월그룹까지 생성")
        self.chk_make_month_groups.setChecked(True)
        row2.addWidget(self.chk_make_month_groups)

        self.btn_make_measure_folders = QPushButton("측정 폴더 구조 생성")
        self.btn_make_burnin_folders = QPushButton("번인 폴더 구조 생성")
        row2.addWidget(self.btn_make_measure_folders)
        row2.addWidget(self.btn_make_burnin_folders)
        row2.addStretch(1)
        l2.addLayout(row2)

        root.addWidget(g2)
        root.addStretch(1)

        # ---------- 버튼 ----------
        btns = QHBoxLayout()
        self.btn_reload = QPushButton("불러오기")
        self.btn_save = QPushButton("저장")
        btns.addWidget(self.btn_reload)
        btns.addWidget(self.btn_save)
        btns.addStretch(1)
        root.addLayout(btns)

        self.btn_reload.clicked.connect(self.load_settings)
        self.btn_save.clicked.connect(self.save_settings)
        self.btn_make_measure_folders.clicked.connect(self.create_measure_folder_structure)
        self.btn_make_burnin_folders.clicked.connect(self.create_burnin_folder_structure)

    def _folder_path_row(self):
        ed = QLineEdit()
        ed.setPlaceholderText("경로를 입력하거나 오른쪽 버튼으로 선택")
        btn = QPushButton("찾기")
        btn.setFixedWidth(70)

        def _browse():
            start = ed.text().strip() or os.getcwd()
            path = QFileDialog.getExistingDirectory(self, "폴더 선택", start)
            if path:
                ed.setText(path)

        btn.clicked.connect(_browse)

        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(ed, 1)
        layout.addWidget(btn)
        return ed, widget

    def _load_model_codes(self) -> list[str]:
        codes = []
        try:
            if hasattr(self.repo, "list_models"):
                rows = self.repo.list_models(include_inactive=False)
                for row in rows:
                    code = str(row.get("model_code") or "").strip()
                    if code and code not in codes:
                        codes.append(code)
        except Exception:
            codes = []

        if not codes:
            try:
                codes = list_model_codes()
            except Exception:
                codes = [DEFAULT_MODEL_CODE]

        if DEFAULT_MODEL_CODE not in codes:
            codes.insert(0, DEFAULT_MODEL_CODE)

        return [normalize_model_code(code) for code in codes if code]

    def _selected_measure_root(self) -> Path:
        path = (self.ed_export_measure.text() or "").strip()
        if not path:
            settings = normalize_and_fill_defaults(load_path_settings(self.repo))
            path = settings.export_dir_measure
        return Path(path)

    def _selected_burnin_root(self) -> Path:
        path = (self.ed_export_burnin.text() or "").strip()
        if not path:
            settings = normalize_and_fill_defaults(load_path_settings(self.repo))
            path = settings.export_dir_burnin
        return Path(path)

    def create_measure_folder_structure(self):
        """측정 저장 구조: export_dir_measure / stage / model / group"""
        try:
            root = self._selected_measure_root()
            models = self._load_model_codes()
            year = datetime.now().year
            groups = _year_group_names(year) if self.chk_make_month_groups.isChecked() else [""]

            made = 0
            for stage in MEASURE_STAGE_FOLDERS:
                for model in models:
                    base = root / stage / model
                    if groups == [""]:
                        existed = base.exists()
                        base.mkdir(parents=True, exist_ok=True)
                        if not existed:
                            made += 1
                    else:
                        for group in groups:
                            path = base / group
                            existed = path.exists()
                            path.mkdir(parents=True, exist_ok=True)
                            if not existed:
                                made += 1

            QMessageBox.information(
                self,
                "측정 폴더 구조 생성 완료",
                f"측정 폴더 구조 생성 완료\n\n"
                f"루트:\n{root}\n\n"
                f"모델: {', '.join(models)}\n"
                f"신규 생성 폴더 수: {made}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "측정 폴더 구조 생성 실패", str(exc))

    def create_burnin_folder_structure(self):
        """번인 저장 구조: export_dir_burnin / model / group"""
        try:
            root = self._selected_burnin_root()
            models = self._load_model_codes()
            year = datetime.now().year
            groups = _year_group_names(year) if self.chk_make_month_groups.isChecked() else [""]

            made = 0
            for model in models:
                base = root / model
                if groups == [""]:
                    existed = base.exists()
                    base.mkdir(parents=True, exist_ok=True)
                    if not existed:
                        made += 1
                else:
                    for group in groups:
                        path = base / group
                        existed = path.exists()
                        path.mkdir(parents=True, exist_ok=True)
                        if not existed:
                            made += 1

            QMessageBox.information(
                self,
                "번인 폴더 구조 생성 완료",
                f"번인 폴더 구조 생성 완료\n\n"
                f"루트:\n{root}\n\n"
                f"모델: {', '.join(models)}\n"
                f"신규 생성 폴더 수: {made}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "번인 폴더 구조 생성 실패", str(exc))

    def load_settings(self):
        try:
            settings = normalize_and_fill_defaults(load_path_settings(self.repo))

            self.ed_export_measure.setText(settings.export_dir_measure or "")
            self.ed_export_burnin.setText(settings.export_dir_burnin or "")
            self.ed_search_raw_root.setText(
                getattr(settings, "search_compare_raw_root", "") or ""
            )
            self.ed_export_20a_grading.setText(
                getattr(settings, "export_dir_20a_grading", "") or ""
            )
            self.ed_export_process.setText(settings.export_dir_process or "")
            self.ed_reports_dir.setText(settings.reports_dir or "")
            self.ed_logs_dir.setText(settings.logs_dir or "")
            self.ed_import_default.setText(settings.import_dir_default or "")
        except Exception as exc:
            QMessageBox.critical(self, "불러오기 실패", str(exc))

    def save_settings(self):
        try:
            settings = PathSettings(
                export_dir_measure=self.ed_export_measure.text().strip(),
                export_dir_burnin=self.ed_export_burnin.text().strip(),
                export_dir_process=self.ed_export_process.text().strip(),
                reports_dir=self.ed_reports_dir.text().strip(),
                logs_dir=self.ed_logs_dir.text().strip(),
                import_dir_default=self.ed_import_default.text().strip(),
                export_dir_20a_grading=self.ed_export_20a_grading.text().strip(),
                search_compare_raw_root=self.ed_search_raw_root.text().strip(),
            )
            settings = normalize_and_fill_defaults(settings)
            save_path_settings(self.repo, settings)

            QMessageBox.information(self, "저장", "경로 설정 저장 완료")
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
