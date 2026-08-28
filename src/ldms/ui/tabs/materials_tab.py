# -*- coding: utf-8 -*-
# src/ldms/ui/tabs/materials_tab.py
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QMessageBox,
    QHeaderView, QDialog, QFormLayout, QDialogButtonBox, QComboBox,
    QDoubleSpinBox
)


def _ro_item(v) -> QTableWidgetItem:
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


class MaterialEditDialog(QDialog):
    def __init__(self, parent=None, item: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("자재 등록/수정")
        self._item = item or None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.ed_code = QLineEdit()
        self.ed_name = QLineEdit()
        self.ed_spec = QLineEdit()
        self.cb_unit = QComboBox()
        self.cb_unit.addItems(["EA", "SET", "g", "mg", "ml", "m", "mm", "sheet", "pcs", "box"])
        self.ed_vendor = QLineEdit()
        self.cb_active = QComboBox()
        self.cb_active.addItems(["사용", "미사용"])

        form.addRow("자재코드*", self.ed_code)
        form.addRow("자재명*", self.ed_name)
        form.addRow("스펙/규격", self.ed_spec)
        form.addRow("단위", self.cb_unit)
        form.addRow("업체", self.ed_vendor)
        form.addRow("상태", self.cb_active)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        if self._item:
            self.ed_code.setText(str(self._item.get("code", "")).strip())
            self.ed_name.setText(str(self._item.get("name", "")).strip())
            self.ed_spec.setText(str(self._item.get("spec", "")).strip())
            unit = str(self._item.get("unit", "EA")).strip() or "EA"
            idx = self.cb_unit.findText(unit)
            if idx >= 0:
                self.cb_unit.setCurrentIndex(idx)
            self.ed_vendor.setText(str(self._item.get("vendor", "")).strip())
            self.cb_active.setCurrentIndex(0 if int(self._item.get("active", 1) or 0) == 1 else 1)

            # 수정시 코드 고정
            self.ed_code.setReadOnly(True)

    def get_payload(self) -> Optional[Dict[str, Any]]:
        code = self.ed_code.text().strip()
        name = self.ed_name.text().strip()
        if not code or not name:
            QMessageBox.warning(self, "입력 확인", "자재코드와 자재명은 필수입니다.")
            return None
        return {
            "code": code,
            "name": name,
            "spec": self.ed_spec.text().strip(),
            "unit": self.cb_unit.currentText().strip() or "EA",
            "vendor": self.ed_vendor.text().strip(),
            "active": (self.cb_active.currentIndex() == 0),
        }


class InventoryAdjustDialog(QDialog):
    def __init__(self, parent=None, material_code: str = ""):
        super().__init__(parent)
        self.setWindowTitle("재고 조정(입고/출고)")
        self.material_code = (material_code or "").strip()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.lb_code = QLabel(self.material_code)
        self.sp_delta = QDoubleSpinBox()
        self.sp_delta.setRange(-1e12, 1e12)
        self.sp_delta.setDecimals(6)
        self.sp_delta.setValue(0.0)

        self.ed_reason = QLineEdit()
        self.ed_reason.setPlaceholderText("사유(예: 입고, 샘플사용, 폐기 등)")

        form.addRow("자재코드", self.lb_code)
        form.addRow("수량 변화(qty_delta)", self.sp_delta)
        form.addRow("사유", self.ed_reason)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

    def get_payload(self) -> Dict[str, Any]:
        return {
            "qty_delta": float(self.sp_delta.value()),
            "reason": self.ed_reason.text().strip(),
        }


class MaterialsTab(QWidget):
    """
    ✅ 성능 개선 포인트
    - 기존: materials 목록 로딩 후 각 자재마다 get_inventory_qty(code) 호출(자재 300개면 300쿼리)
    - 개선: repo.list_inventory() (JOIN 한방)로 현재고까지 한 번에 가져옴
    """
    def __init__(self, repo):
        super().__init__()
        self.repo = repo

        root = QVBoxLayout(self)

        top = QHBoxLayout()
        root.addLayout(top)

        top.addWidget(QLabel("검색:"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("코드/자재명/스펙/업체 검색")
        top.addWidget(self.ed_search, 1)

        self.cb_show = QComboBox()
        self.cb_show.addItems(["사용만", "전체(미사용 포함)"])
        top.addWidget(self.cb_show)

        self.btn_refresh = QPushButton("새로고침")
        self.btn_add = QPushButton("등록")
        self.btn_edit = QPushButton("수정")
        self.btn_toggle_active = QPushButton("사용/미사용 전환")
        self.btn_adjust = QPushButton("재고조정")
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_add)
        top.addWidget(self.btn_edit)
        top.addWidget(self.btn_toggle_active)
        top.addWidget(self.btn_adjust)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "코드", "자재명", "스펙", "단위", "업체", "상태", "현재고", "재고갱신", "created_at"
        ])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.table, 1)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_add.clicked.connect(self.on_add)
        self.btn_edit.clicked.connect(self.on_edit)
        self.btn_toggle_active.clicked.connect(self.on_toggle_active)
        self.btn_adjust.clicked.connect(self.on_adjust_inventory)

        self.ed_search.textChanged.connect(self.refresh)
        self.cb_show.currentIndexChanged.connect(self.refresh)

        self.refresh()

    def _selected_code(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            raise ValueError("자재를 선택하세요.")
        code_it = self.table.item(row, 0)
        code = code_it.text().strip() if code_it else ""
        if not code:
            raise ValueError("선택된 자재 코드가 없습니다.")
        return code

    def _row_to_item(self, row: int) -> Dict[str, Any]:
        return {
            "code": self.table.item(row, 0).text().strip() if self.table.item(row, 0) else "",
            "name": self.table.item(row, 1).text().strip() if self.table.item(row, 1) else "",
            "spec": self.table.item(row, 2).text().strip() if self.table.item(row, 2) else "",
            "unit": self.table.item(row, 3).text().strip() if self.table.item(row, 3) else "EA",
            "vendor": self.table.item(row, 4).text().strip() if self.table.item(row, 4) else "",
            "active": 1 if (self.table.item(row, 5).text().strip() == "사용") else 0,
        }

    def refresh(self):
        try:
            include_inactive = (self.cb_show.currentIndex() == 1)
            keyword = (self.ed_search.text() or "").strip().lower()

            # ✅ JOIN 한방 조회 (repo.list_inventory)
            if hasattr(self.repo, "list_inventory"):
                rows = self.repo.list_inventory(include_inactive=include_inactive)
            else:
                # fallback: old repo (성능 느릴 수 있음)
                mats = self.repo.list_materials(include_inactive=include_inactive)
                rows = []
                for m in mats:
                    code = str(m.get("code", "")).strip()
                    qty = self.repo.get_inventory_qty(code) if hasattr(self.repo, "get_inventory_qty") else 0.0
                    rows.append({**m, "qty_on_hand": qty, "updated_at": ""})

            # 검색 필터
            if keyword:
                filtered = []
                for r in rows:
                    s = " ".join([
                        str(r.get("code", "")),
                        str(r.get("name", "")),
                        str(r.get("spec", "")),
                        str(r.get("vendor", "")),
                    ]).lower()
                    if keyword in s:
                        filtered.append(r)
                rows = filtered

            self.table.setRowCount(0)
            for r in rows:
                i = self.table.rowCount()
                self.table.insertRow(i)

                code = str(r.get("code", "")).strip()
                name = str(r.get("name", "")).strip()
                spec = str(r.get("spec", "")).strip()
                unit = str(r.get("unit", "EA")).strip() or "EA"
                vendor = str(r.get("vendor", "")).strip()
                active = int(r.get("active", 1) or 0)
                qty = r.get("qty_on_hand", 0)
                updated_at = str(r.get("updated_at", "") or "")
                created_at = str(r.get("created_at", "") or "")

                self.table.setItem(i, 0, _ro_item(code))
                self.table.setItem(i, 1, _ro_item(name))
                self.table.setItem(i, 2, _ro_item(spec))
                self.table.setItem(i, 3, _ro_item(unit))
                self.table.setItem(i, 4, _ro_item(vendor))
                self.table.setItem(i, 5, _ro_item("사용" if active == 1 else "미사용"))
                self.table.setItem(i, 6, _ro_item(qty))
                self.table.setItem(i, 7, _ro_item(updated_at))
                self.table.setItem(i, 8, _ro_item(created_at))
        except Exception as e:
            QMessageBox.critical(self, "자재 목록 오류", str(e))

    def on_add(self):
        try:
            dlg = MaterialEditDialog(self, None)
            if dlg.exec_():
                p = dlg.get_payload()
                if not p:
                    return
                self.repo.upsert_material(
                    code=p["code"],
                    name=p["name"],
                    spec=p.get("spec", ""),
                    unit=p.get("unit", "EA"),
                    vendor=p.get("vendor", ""),
                    active=bool(p.get("active", True)),
                )
                self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "등록 실패", str(e))

    def on_edit(self):
        try:
            row = self.table.currentRow()
            if row < 0:
                raise ValueError("수정할 자재를 선택하세요.")
            item = self._row_to_item(row)

            dlg = MaterialEditDialog(self, item)
            if dlg.exec_():
                p = dlg.get_payload()
                if not p:
                    return
                # code는 readOnly라 그대로
                self.repo.upsert_material(
                    code=item["code"],
                    name=p["name"],
                    spec=p.get("spec", ""),
                    unit=p.get("unit", "EA"),
                    vendor=p.get("vendor", ""),
                    active=bool(p.get("active", True)),
                )
                self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "수정 실패", str(e))

    def on_toggle_active(self):
        try:
            code = self._selected_code()
            row = self.table.currentRow()
            cur = self.table.item(row, 5).text().strip() if self.table.item(row, 5) else "사용"
            new_active = (cur != "사용")  # 현재가 미사용이면 True로 바꿈
            if hasattr(self.repo, "set_material_active"):
                self.repo.set_material_active(code, new_active)
            else:
                # fallback: upsert_material로 active만 갱신(다른 값은 유지 어려움 → 미지원)
                raise RuntimeError("repo.set_material_active()가 없습니다.")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "전환 실패", str(e))

    def on_adjust_inventory(self):
        try:
            code = self._selected_code()
            dlg = InventoryAdjustDialog(self, code)
            if dlg.exec_():
                p = dlg.get_payload()
                delta = float(p.get("qty_delta") or 0.0)
                reason = p.get("reason", "").strip() or "manual_adjust"

                # ✅ repo.adjust_inventory는 기본 음수재고 차단 (allow_negative=False)
                self.repo.adjust_inventory(
                    material_code=code,
                    qty_delta=delta,
                    reason=reason,
                    ref_type="manual",
                    ref_id=None,
                    note="MaterialsTab adjust",
                    allow_negative=False,
                )
                self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "재고 조정 실패", str(e))
