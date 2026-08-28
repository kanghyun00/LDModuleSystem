# -*- coding: utf-8 -*-
# src/ldms/ui/tabs/bom_tab.py
from __future__ import annotations

from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QComboBox,
    QDoubleSpinBox, QCheckBox, QLineEdit
)

from src.ldms.services import pms_service
from src.ldms.process_flow import PROCESS_FLOW, DEFAULT_MODEL_CODE, list_model_codes, normalize_model_code, get_process_flow


def _ro_item(v) -> QTableWidgetItem:
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def _parse_material_code_from_text(txt: str) -> str:
    """
    콤보가 editable이라서 사용자가 직접 입력할 수 있음.
    표시 포맷: "CODE | NAME (SPEC) [VENDOR]"
    직접 입력해도 앞의 CODE를 최대한 추출.
    """
    s = (txt or "").strip()
    if not s:
        return ""
    if "|" in s:
        return s.split("|", 1)[0].strip()
    return s.strip()


class BomTab(QWidget):
    """
    모델별 공정 BOM 관리
    - model_process_bom: (model_code, process_name, material_code, qty_per_unit, unit, active)
    - materials: 자재 마스터에서 코드/이름 가져와 선택
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo

        self._build_ui()
        self._load_process_candidates()
        self._load_material_candidates()
        self._refresh_bom_table()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # -------------------------
        # 상단: 공정 선택
        # -------------------------
        top = QHBoxLayout()
        root.addLayout(top)

        top.addWidget(QLabel("모델:"))
        self.cb_model = QComboBox()
        self.cb_model.setMinimumWidth(150)
        self.cb_model.addItems(self._load_model_codes())
        idx_default = self.cb_model.findText(DEFAULT_MODEL_CODE)
        if idx_default >= 0:
            self.cb_model.setCurrentIndex(idx_default)
        top.addWidget(self.cb_model)

        top.addWidget(QLabel("공정명:"))

        self.cb_process = QComboBox()
        self.cb_process.setEditable(True)
        self.cb_process.setMinimumWidth(260)
        self.cb_process.setInsertPolicy(QComboBox.NoInsert)
        top.addWidget(self.cb_process)

        self.chk_show_inactive = QCheckBox("비활성(BOM) 포함")
        top.addWidget(self.chk_show_inactive)

        self.btn_refresh = QPushButton("새로고침")
        top.addWidget(self.btn_refresh)

        top.addStretch(1)

        # -------------------------
        # 등록/수정 영역
        # -------------------------
        form = QHBoxLayout()
        root.addLayout(form)

        form.addWidget(QLabel("자재:"))
        self.cb_material = QComboBox()
        self.cb_material.setEditable(True)
        self.cb_material.setInsertPolicy(QComboBox.NoInsert)
        self.cb_material.setMinimumWidth(420)

        le: QLineEdit = self.cb_material.lineEdit()
        if le:
            le.setPlaceholderText("코드 또는 'CODE | NAME'를 입력/검색")
        form.addWidget(self.cb_material, 2)

        form.addWidget(QLabel("소요량(qty/1):"))
        self.sp_qty = QDoubleSpinBox()
        self.sp_qty.setRange(0, 1e12)
        self.sp_qty.setDecimals(6)
        self.sp_qty.setValue(0.0)
        form.addWidget(self.sp_qty)

        form.addWidget(QLabel("단위:"))
        self.cb_unit = QComboBox()
        self.cb_unit.addItems(["EA", "SET", "g", "mg", "ml", "m", "mm", "sheet", "pcs", "box"])
        form.addWidget(self.cb_unit)

        self.chk_active = QCheckBox("사용")
        self.chk_active.setChecked(True)
        form.addWidget(self.chk_active)

        self.btn_save = QPushButton("등록/수정")
        self.btn_disable = QPushButton("미사용 처리")
        form.addWidget(self.btn_save)
        form.addWidget(self.btn_disable)

        # -------------------------
        # 테이블
        # -------------------------
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "ID", "모델", "자재코드", "자재명", "소요량(qty/1)", "단위", "상태", "스펙"
        ])
        self.table.setColumnHidden(0, True)

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.Stretch)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.table, 1)

        hint = QLabel("※ 공정기록 저장 시 repo.insert_process_log_with_materials(...)를 쓰면, 여기 BOM 기준으로 재고가 자동 차감됩니다.")
        hint.setStyleSheet("color:#666;")
        root.addWidget(hint)

        # signals
        self.btn_refresh.clicked.connect(self._on_refresh_all)
        self.cb_model.currentIndexChanged.connect(self._on_model_changed)
        self.cb_process.currentTextChanged.connect(lambda _: self._refresh_bom_table())
        self.chk_show_inactive.stateChanged.connect(lambda _: self._refresh_bom_table())
        self.btn_save.clicked.connect(self._on_save_bom)
        self.btn_disable.clicked.connect(self._on_disable_bom)
        self.table.itemSelectionChanged.connect(self._on_table_selected)

    # -------------------------
    # load combos
    # -------------------------
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
        try:
            return normalize_model_code(self.cb_model.currentText() or DEFAULT_MODEL_CODE)
        except Exception:
            return DEFAULT_MODEL_CODE

    def _on_model_changed(self):
        self._load_process_candidates()
        self._refresh_bom_table()

    def _load_process_candidates(self):
        """
        ✅ 선택 모델의 공정순서만 공정 후보로 표시.
        1) DB model_process_flow 우선
        2) 실패 시 process_flow.py의 모델별 flow 사용
        """
        self.cb_process.blockSignals(True)
        cur = (self.cb_process.currentText() or "").strip()
        model = self._current_model()

        items: List[str] = []
        try:
            if hasattr(self.repo, "get_model_process_flow"):
                rows = self.repo.get_model_process_flow(model, include_inactive=False)
                for r in rows:
                    p = str(r.get("process_name") or "").strip()
                    if p and p not in items:
                        items.append(p)
        except Exception:
            items = []

        if not items:
            try:
                for p in get_process_flow(model):
                    pn = pms_service.normalize_process_name(p)
                    if pn and pn not in items:
                        items.append(pn)
            except Exception:
                for p in (PROCESS_FLOW or []):
                    pn = pms_service.normalize_process_name(p)
                    if pn and pn not in items:
                        items.append(pn)

        self.cb_process.clear()
        self.cb_process.addItems(items)

        if cur and cur in items:
            self.cb_process.setCurrentText(cur)
        elif items:
            self.cb_process.setCurrentIndex(0)

        self.cb_process.blockSignals(False)

    def _load_material_candidates(self):
        self.cb_material.blockSignals(True)
        cur_text = (self.cb_material.currentText() or "").strip()
        cur_code = _parse_material_code_from_text(cur_text)

        self.cb_material.clear()
        try:
            mats = self.repo.list_materials(include_inactive=True)
        except Exception as e:
            self.cb_material.blockSignals(False)
            QMessageBox.critical(self, "자재 로드 실패", str(e))
            return

        for m in mats:
            code = str(m.get("code", "")).strip()
            name = str(m.get("name", "")).strip()
            spec = str(m.get("spec", "")).strip()
            vendor = str(m.get("vendor", "")).strip()

            if not code:
                continue

            label = f"{code} | {name}"
            if spec:
                label += f" ({spec})"
            if vendor:
                label += f" [{vendor}]"

            self.cb_material.addItem(label, code)

        # 기존 선택 유지(가능하면 코드로)
        if cur_code:
            idx = -1
            for i in range(self.cb_material.count()):
                if (self.cb_material.itemData(i) or "") == cur_code:
                    idx = i
                    break
            if idx >= 0:
                self.cb_material.setCurrentIndex(idx)
            else:
                self.cb_material.setCurrentText(cur_text)

        self.cb_material.blockSignals(False)

    # -------------------------
    # refresh table
    # -------------------------
    def _refresh_bom_table(self):
        proc = (self.cb_process.currentText() or "").strip()
        if not proc:
            self.table.setRowCount(0)
            return

        proc_norm = pms_service.normalize_process_name(proc)
        include_inactive = self.chk_show_inactive.isChecked()

        try:
            rows = self.repo.list_process_bom(proc_norm, include_inactive=include_inactive, model=self._current_model())
        except Exception as e:
            QMessageBox.critical(self, "BOM 조회 실패", str(e))
            return

        rows = list(rows)
        rows.sort(key=lambda r: (0 if int(r.get("active") or 0) == 1 else 1, str(r.get("material_code") or "")))

        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, _ro_item(r.get("id")))
            self.table.setItem(i, 1, _ro_item(r.get("model") or self._current_model()))
            self.table.setItem(i, 2, _ro_item(r.get("material_code")))
            self.table.setItem(i, 3, _ro_item(r.get("material_name") or ""))
            self.table.setItem(i, 4, _ro_item(r.get("qty_per_unit")))
            self.table.setItem(i, 5, _ro_item(r.get("unit") or "EA"))
            active_txt = "사용" if int(r.get("active") or 0) == 1 else "미사용"
            self.table.setItem(i, 6, _ro_item(active_txt))
            self.table.setItem(i, 7, _ro_item(r.get("material_spec") or ""))

            self.table.item(i, 6).setTextAlignment(Qt.AlignCenter)

    def _on_refresh_all(self):
        self._load_process_candidates()
        self._load_material_candidates()
        self._refresh_bom_table()

    # -------------------------
    # selection -> fill form
    # -------------------------
    def _on_table_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return

        code = (self.table.item(row, 2).text() if self.table.item(row, 2) else "").strip()
        qty_txt = self.table.item(row, 4).text() if self.table.item(row, 4) else "0"
        unit = (self.table.item(row, 5).text() if self.table.item(row, 5) else "EA").strip()
        st = (self.table.item(row, 6).text() if self.table.item(row, 6) else "사용").strip()

        idx = -1
        for i in range(self.cb_material.count()):
            if (self.cb_material.itemData(i) or "") == code:
                idx = i
                break
        if idx >= 0:
            self.cb_material.setCurrentIndex(idx)
        else:
            self.cb_material.setCurrentText(code)

        try:
            self.sp_qty.setValue(float(qty_txt))
        except Exception:
            self.sp_qty.setValue(0.0)

        uidx = self.cb_unit.findText(unit)
        if uidx >= 0:
            self.cb_unit.setCurrentIndex(uidx)
        else:
            self.cb_unit.setCurrentText(unit)

        self.chk_active.setChecked(st == "사용")

    # -------------------------
    # save / disable
    # -------------------------
    def _get_selected_material_code(self) -> str:
        code = self.cb_material.currentData()
        code = (code or "").strip()
        if code:
            return code
        txt = (self.cb_material.currentText() or "").strip()
        return _parse_material_code_from_text(txt)

    def _on_save_bom(self):
        proc = (self.cb_process.currentText() or "").strip()
        if not proc:
            QMessageBox.warning(self, "입력", "공정명을 입력/선택하세요.")
            return
        proc_norm = pms_service.normalize_process_name(proc)

        mat_code = self._get_selected_material_code().strip()
        if not mat_code:
            QMessageBox.warning(self, "입력", "자재를 선택하거나 코드로 입력하세요.")
            return

        qty_per = float(self.sp_qty.value())
        if qty_per == 0.0:
            ret = QMessageBox.question(
                self,
                "확인",
                "소요량(qty/1)이 0입니다.\n실수일 가능성이 큽니다.\n그래도 저장할까요?",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

        unit = (self.cb_unit.currentText() or "EA").strip()
        active = self.chk_active.isChecked()

        try:
            self.repo.upsert_process_bom(
                process_name=proc_norm,
                material_code=mat_code,
                qty_per_unit=qty_per,
                unit=unit,
                active=active,
                model=self._current_model(),
            )
            self._refresh_bom_table()
            QMessageBox.information(
                self, "저장",
                f"BOM 저장 완료:\n- 공정: {proc_norm}\n- 자재: {mat_code}\n- 소요량: {qty_per} {unit}\n- 상태: {'사용' if active else '미사용'}"
            )
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))

    def _on_disable_bom(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "선택", "미사용 처리할 BOM 항목을 선택하세요.")
            return

        proc = (self.cb_process.currentText() or "").strip()
        if not proc:
            return
        proc_norm = pms_service.normalize_process_name(proc)

        code = (self.table.item(row, 2).text() if self.table.item(row, 2) else "").strip()
        if not code:
            return

        ret = QMessageBox.question(
            self,
            "미사용 처리",
            f"[{self._current_model()}] {proc_norm} 공정의 [{code}] BOM을 미사용으로 바꿀까요?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ret != QMessageBox.Yes:
            return

        try:
            qty_txt = self.table.item(row, 4).text() if self.table.item(row, 4) else "0"
            unit = (self.table.item(row, 5).text() if self.table.item(row, 5) else "EA").strip()
            try:
                qty_per = float(qty_txt)
            except Exception:
                qty_per = float(self.sp_qty.value())

            self.repo.upsert_process_bom(
                process_name=proc_norm,
                material_code=code,
                qty_per_unit=qty_per,
                unit=unit,
                active=False,
                model=self._current_model(),
            )
            self._refresh_bom_table()
        except Exception as e:
            QMessageBox.critical(self, "처리 실패", str(e))