# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import List

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QGroupBox, QFormLayout, QLineEdit, QDoubleSpinBox, QFrame,
    QComboBox
)

from src.ldms.process_flow import (
    DEFAULT_MODEL_CODE,
    list_model_codes,
    normalize_model_code,
    get_model_recipe,
)

SETTINGS_KEY = "final_export_rules"


def _model_settings_key(model: str) -> str:
    model = normalize_model_code(model or DEFAULT_MODEL_CODE)
    return f"{SETTINGS_KEY}::{model}"


def _hr() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


@dataclass
class FinalExportRules:
    # ===== 전류 (엑셀에서 이 전류 행을 뽑음) =====
    judge_current: float = 20.0
    current_tolerance: float = 0.2

    # ===== 등급 기준 (Power 기준) =====
    a_min_power: float = 390.0
    b_min_power: float = 380.0
    c_min_power: float = 370.0

    # ===== 색칠 조건 =====
    smsr_yellow_max: float = 30.0
    smsr_red_max: float = 20.0

    pkg_yellow_min: float = 36.0
    pkg_red_min: float = 40.0

    # ===== 기타 =====
    ok_keywords_csv: str = "양품확정,양품 확정"


def _default_rules_for_model(model: str) -> FinalExportRules:
    """
    모델별 기본 판정값.
    Parameter 탭에서 저장하면 저장값이 우선 적용됨.
    """
    model = normalize_model_code(model or DEFAULT_MODEL_CODE)
    try:
        recipe = get_model_recipe(model)
        max_a = float(recipe.max_current_a)
    except Exception:
        max_a = 20.0

    # 출력값은 모델명에서 W 앞 숫자를 최대한 추출
    target_w = 370.0
    try:
        import re
        m = re.search(r"F\d{3,4}(\d{1,4})W", model.upper())
        if m:
            target_w = float(m.group(1))
    except Exception:
        pass

    # 기본 등급 기준: A=목표+약5%, B=목표+약2.5%, C=목표
    # 370W 기존 기본값은 기존 프로그램 기준 유지
    if model == DEFAULT_MODEL_CODE:
        return FinalExportRules(
            judge_current=20.0,
            current_tolerance=0.2,
            a_min_power=390.0,
            b_min_power=380.0,
            c_min_power=370.0,
        )

    return FinalExportRules(
        judge_current=max_a,
        current_tolerance=0.2,
        a_min_power=round(target_w * 1.05, 2),
        b_min_power=round(target_w * 1.025, 2),
        c_min_power=round(target_w, 2),
    )


class ParameterGradingTab(QWidget):
    """
    모델별 최종data 엑셀 저장(등급/색) 규칙 설정 탭.
    Search/비교 탭의 엑셀 저장이 현재 선택 모델 규칙을 사용한다.
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._build_ui()
        self.load_settings()

    def _load_model_codes(self) -> List[str]:
        codes: List[str] = []
        try:
            if hasattr(self.repo, "list_models"):
                rows = self.repo.list_models(include_inactive=False)
                for r in rows:
                    c = str(r.get("model_code") or "").strip()
                    if c and c not in codes:
                        codes.append(c)
        except Exception:
            codes = []

        if not codes:
            try:
                codes = list_model_codes()
            except Exception:
                codes = [DEFAULT_MODEL_CODE]

        if DEFAULT_MODEL_CODE not in codes:
            codes.insert(0, DEFAULT_MODEL_CODE)
        return codes

    def _current_model(self) -> str:
        return normalize_model_code(self.cb_model.currentText() or DEFAULT_MODEL_CODE)

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("모델별 최종data 엑셀 저장(등급/색) 설정")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        root.addWidget(title)

        root.addWidget(QLabel("※ Search/비교 탭의 엑셀 저장 기능이 여기 모델별 설정을 사용합니다."))
        root.addWidget(_hr())

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("모델"))
        self.cb_model = QComboBox()
        self.cb_model.setMinimumWidth(180)
        self.cb_model.addItems(self._load_model_codes())
        idx = self.cb_model.findText(DEFAULT_MODEL_CODE)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        model_row.addWidget(self.cb_model)

        self.btn_apply_default = QPushButton("선택 모델 기본값 적용")
        model_row.addWidget(self.btn_apply_default)
        model_row.addStretch(1)
        root.addLayout(model_row)

        # ---------------- 전류 설정 ----------------
        g0 = QGroupBox("전류(행 선택 기준)")
        f0 = QFormLayout(g0)

        self.sp_current = QDoubleSpinBox()
        self.sp_current.setRange(0.0, 200.0)
        self.sp_current.setDecimals(2)
        self.sp_current.setSingleStep(0.5)
        f0.addRow("기준 전류 (A)", self.sp_current)

        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.0, 5.0)
        self.sp_tol.setDecimals(2)
        self.sp_tol.setSingleStep(0.05)
        f0.addRow("허용 오차 (±A)", self.sp_tol)

        root.addWidget(g0)

        # ---------------- 등급 기준 ----------------
        g1 = QGroupBox("등급 기준 (Power 기준)")
        f1 = QFormLayout(g1)

        self.sp_a = QDoubleSpinBox()
        self.sp_a.setRange(0.0, 9999.0)
        self.sp_a.setDecimals(2)
        self.sp_a.setSingleStep(1.0)
        f1.addRow("A급: Power ≥", self.sp_a)

        self.sp_b = QDoubleSpinBox()
        self.sp_b.setRange(0.0, 9999.0)
        self.sp_b.setDecimals(2)
        self.sp_b.setSingleStep(1.0)
        f1.addRow("B급: Power ≥", self.sp_b)

        self.sp_c = QDoubleSpinBox()
        self.sp_c.setRange(0.0, 9999.0)
        self.sp_c.setDecimals(2)
        self.sp_c.setSingleStep(1.0)
        f1.addRow("C급: Power ≥", self.sp_c)

        root.addWidget(g1)

        # ---------------- 색칠 기준 ----------------
        g2 = QGroupBox("색칠 기준")
        f2 = QFormLayout(g2)

        self.sp_smsr_y = QDoubleSpinBox()
        self.sp_smsr_y.setRange(-9999.0, 9999.0)
        self.sp_smsr_y.setDecimals(2)
        self.sp_smsr_y.setSingleStep(0.5)
        f2.addRow("SMSR 노랑: SMSR ≤", self.sp_smsr_y)

        self.sp_smsr_r = QDoubleSpinBox()
        self.sp_smsr_r.setRange(-9999.0, 9999.0)
        self.sp_smsr_r.setDecimals(2)
        self.sp_smsr_r.setSingleStep(0.5)
        f2.addRow("SMSR 빨강: SMSR ≤", self.sp_smsr_r)

        self.sp_pkg_y = QDoubleSpinBox()
        self.sp_pkg_y.setRange(-9999.0, 9999.0)
        self.sp_pkg_y.setDecimals(2)
        self.sp_pkg_y.setSingleStep(0.5)
        f2.addRow("PKG 노랑: PKG ≥", self.sp_pkg_y)

        self.sp_pkg_r = QDoubleSpinBox()
        self.sp_pkg_r.setRange(-9999.0, 9999.0)
        self.sp_pkg_r.setDecimals(2)
        self.sp_pkg_r.setSingleStep(0.5)
        f2.addRow("PKG 빨강: PKG ≥", self.sp_pkg_r)

        root.addWidget(g2)

        # ---------------- 기타 ----------------
        g3 = QGroupBox("기타")
        f3 = QFormLayout(g3)

        self.ed_ok_kw = QLineEdit()
        self.ed_ok_kw.setPlaceholderText("예: 양품확정,양품 확정")
        f3.addRow("양품확정 키워드(콤마)", self.ed_ok_kw)

        root.addWidget(g3)
        root.addStretch(1)

        btns = QHBoxLayout()
        self.btn_reload = QPushButton("불러오기")
        self.btn_save = QPushButton("저장")
        btns.addWidget(self.btn_reload)
        btns.addWidget(self.btn_save)
        btns.addStretch(1)
        root.addLayout(btns)

        self.cb_model.currentIndexChanged.connect(self.load_settings)
        self.btn_apply_default.clicked.connect(self.apply_model_default)
        self.btn_reload.clicked.connect(self.load_settings)
        self.btn_save.clicked.connect(self.save_settings)

    def _get_from_ui(self) -> FinalExportRules:
        return FinalExportRules(
            judge_current=float(self.sp_current.value()),
            current_tolerance=float(self.sp_tol.value()),
            a_min_power=float(self.sp_a.value()),
            b_min_power=float(self.sp_b.value()),
            c_min_power=float(self.sp_c.value()),
            smsr_yellow_max=float(self.sp_smsr_y.value()),
            smsr_red_max=float(self.sp_smsr_r.value()),
            pkg_yellow_min=float(self.sp_pkg_y.value()),
            pkg_red_min=float(self.sp_pkg_r.value()),
            ok_keywords_csv=(self.ed_ok_kw.text() or "").strip(),
        )

    def _set_ui(self, s: FinalExportRules):
        self.sp_current.setValue(float(s.judge_current))
        self.sp_tol.setValue(float(s.current_tolerance))
        self.sp_a.setValue(float(s.a_min_power))
        self.sp_b.setValue(float(s.b_min_power))
        self.sp_c.setValue(float(s.c_min_power))
        self.sp_smsr_y.setValue(float(s.smsr_yellow_max))
        self.sp_smsr_r.setValue(float(s.smsr_red_max))
        self.sp_pkg_y.setValue(float(s.pkg_yellow_min))
        self.sp_pkg_r.setValue(float(s.pkg_red_min))
        self.ed_ok_kw.setText(s.ok_keywords_csv or "")

    def _load_rules_dict(self, model: str) -> dict:
        # 모델별 설정 우선, 없으면 구버전 공통 설정, 그것도 없으면 기본값
        key_model = _model_settings_key(model)
        raw = self.repo.get_setting(key_model, "")
        if not raw:
            raw = self.repo.get_setting(SETTINGS_KEY, "")

        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def load_settings(self):
        try:
            model = self._current_model()
            s = _default_rules_for_model(model)
            data = self._load_rules_dict(model)

            for k, v in data.items():
                if hasattr(s, k):
                    try:
                        if isinstance(getattr(s, k), float):
                            setattr(s, k, float(v))
                        else:
                            setattr(s, k, "" if v is None else str(v))
                    except Exception:
                        pass

            self._set_ui(s)
        except Exception as e:
            QMessageBox.critical(self, "불러오기 실패", str(e))

    def apply_model_default(self):
        model = self._current_model()
        self._set_ui(_default_rules_for_model(model))
        QMessageBox.information(self, "기본값 적용", f"[{model}] 기본 판정값을 화면에 적용했습니다. 저장을 눌러야 반영됩니다.")

    def save_settings(self):
        try:
            model = self._current_model()
            s = self._get_from_ui()
            payload = json.dumps(asdict(s), ensure_ascii=False)

            # 모델별 저장
            self.repo.set_setting(_model_settings_key(model), payload)

            # 370W는 구버전 호환을 위해 공통 키에도 저장
            if model == DEFAULT_MODEL_CODE:
                self.repo.set_setting(SETTINGS_KEY, payload)

            QMessageBox.information(self, "저장", f"✅ [{model}] 최종data 엑셀 규칙 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))
