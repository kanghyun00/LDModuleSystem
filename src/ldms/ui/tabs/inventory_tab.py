# -*- coding: utf-8 -*-
# src/ldms/ui/tabs/inventory_tab.py
from __future__ import annotations

from typing import Optional, List, Dict, Any
import csv
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QCheckBox,
    QDoubleSpinBox, QInputDialog, QGroupBox, QFileDialog, QComboBox
)

from src.ldms.process_flow import DEFAULT_MODEL_CODE, list_model_codes, normalize_model_code


def _ro_item(v) -> QTableWidgetItem:
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def _fmt_num(v: Any, decimals: int = 6) -> str:
    try:
        x = float(v)
        if abs(x - int(x)) < 1e-12:
            return str(int(x))
        return f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    except Exception:
        return "" if v is None else str(v)


def _num_item(v: Any, decimals: int = 6) -> QTableWidgetItem:
    it = _ro_item(_fmt_num(v, decimals))
    it.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
    return it


class InventoryTab(QWidget):
    """
    재고관리 탭
    - inventory(스냅샷) 조회
    - inventory_txn(원장) 기록 조회
    - adjust_inventory로 입고/출고/조정 처리

    ✅ 개선:
    - 선택 유지
    - 전체 원장 보기
    - limit 선택(200/500/1000)
    - 절대값으로 현재고 설정(=delta 계산)
    - CSV 내보내기(현재고/원장)
    - 음수 재고/출고 강확인
    - ✅ repo.adjust_inventory(... allow_negative=False)로 음수 원천 차단
    - ✅ 모델별 원장 필터: process_log에 연결된 BOM/수동 사용내역을 선택 모델 기준으로 필터링
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo

        self._last_selected_code: Optional[str] = None
        self._show_all_txn: bool = False

        self._build_ui()
        self.refresh_inventory()

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

    def _current_model_filter(self) -> str:
        """
        ""이면 전체 모델.
        """
        if not hasattr(self, "cmb_model"):
            return ""
        txt = (self.cmb_model.currentText() or "").strip()
        if not txt or txt == "(전체)":
            return ""
        return normalize_model_code(txt)

    def _txn_row_model(self, row: Dict[str, Any]) -> str:
        """
        inventory_txn이 process_log에 연결되어 있으면 process_log.model을 조회.
        연결이 없으면 공용/수기 재고 조정으로 보고 빈 값 반환.
        """
        try:
            ref_type = str(row.get("ref_type") or "").strip()
            ref_id = row.get("ref_id")
            if ref_type != "process_log" or ref_id in (None, ""):
                return ""

            if hasattr(self.repo, "get_process_log"):
                rec = self.repo.get_process_log(int(ref_id))
                if rec:
                    return normalize_model_code(rec.get("model") or DEFAULT_MODEL_CODE)
        except Exception:
            pass
        return ""

    def _filter_txn_by_model(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        model = self._current_model_filter()
        if not model:
            return rows

        out: List[Dict[str, Any]] = []
        for r in rows:
            row_model = self._txn_row_model(r)

            # 모델 필터가 켜진 경우:
            # - process_log에 연결된 사용/원복 내역은 해당 모델만 표시
            # - ref_type이 manual 등 공용 조정이면 모델을 알 수 없으므로 제외
            if row_model == model:
                rr = dict(r)
                rr["_model"] = row_model
                out.append(rr)

        return out

    def _model_filter_label(self) -> str:
        return self._current_model_filter() or "전체"

    def _build_ui(self):
        root = QVBoxLayout(self)

        # -------------------------
        # 상단: 검색/옵션/버튼
        # -------------------------
        top = QHBoxLayout()
        root.addLayout(top)

        top.addWidget(QLabel("검색:"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("코드/자재명/스펙/업체 검색")
        top.addWidget(self.ed_search, 1)

        top.addWidget(QLabel("모델:"))
        self.cmb_model = QComboBox()
        self.cmb_model.setMinimumWidth(150)
        self.cmb_model.addItem("(전체)")
        self.cmb_model.addItems(self._load_model_codes())
        idx_default = self.cmb_model.findText(DEFAULT_MODEL_CODE)
        if idx_default >= 0:
            self.cmb_model.setCurrentIndex(idx_default)
        top.addWidget(self.cmb_model)

        self.chk_include_inactive = QCheckBox("미사용 자재 포함")
        self.chk_include_inactive.setChecked(True)
        top.addWidget(self.chk_include_inactive)

        self.btn_export_inv = QPushButton("현재고 CSV")
        top.addWidget(self.btn_export_inv)

        self.btn_refresh = QPushButton("새로고침")
        top.addWidget(self.btn_refresh)

        # -------------------------
        # 현재고 테이블
        # -------------------------
        self.tbl_inv = QTableWidget(0, 9)
        self.tbl_inv.setHorizontalHeaderLabels([
            "코드", "자재명", "스펙", "단위", "업체", "상태", "현재고", "업데이트", "비고"
        ])
        hdr = self.tbl_inv.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.Stretch)

        self.tbl_inv.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_inv.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_inv.setSortingEnabled(True)
        root.addWidget(self.tbl_inv, 2)

        self.lb_model_note = QLabel("※ 현재고는 공용 재고입니다. 모델 선택은 아래 원장(BOM/수동 사용내역) 필터에 적용됩니다.")
        self.lb_model_note.setStyleSheet("color:#666;")
        root.addWidget(self.lb_model_note)

        # -------------------------
        # 입출고/조정 패널
        # -------------------------
        box = QGroupBox("입고/출고/조정")
        root.addWidget(box)
        form = QHBoxLayout(box)

        form.addWidget(QLabel("선택 자재:"))
        self.lb_selected = QLabel("-")
        self.lb_selected.setMinimumWidth(200)
        self.lb_selected.setStyleSheet("font-weight: 600;")
        form.addWidget(self.lb_selected)

        form.addWidget(QLabel("수량(Δ):"))
        self.sp_delta = QDoubleSpinBox()
        self.sp_delta.setRange(-1e12, 1e12)
        self.sp_delta.setDecimals(6)
        self.sp_delta.setValue(0.0)
        form.addWidget(self.sp_delta)

        self.btn_in = QPushButton("입고(+)")
        self.btn_out = QPushButton("출고(-)")
        self.btn_adjust = QPushButton("조정(Δ 적용)")
        self.btn_set_abs = QPushButton("현재고=값 설정")
        form.addWidget(self.btn_in)
        form.addWidget(self.btn_out)
        form.addWidget(self.btn_adjust)
        form.addWidget(self.btn_set_abs)

        form.addStretch(1)

        # -------------------------
        # 원장 상단
        # -------------------------
        txn_top = QHBoxLayout()
        root.addLayout(txn_top)

        self.lb_txn_title = QLabel("재고 원장(선택 자재, 최근 200건):")
        txn_top.addWidget(self.lb_txn_title, 1)

        txn_top.addWidget(QLabel("reason:"))
        self.cb_reason = QComboBox()
        self.cb_reason.addItems([
            "전체",
            "사용량(usage_apply/usage_revert)",
            "수동(manual)",
            "설정(set_abs_qty)",
            "기타",
        ])
        txn_top.addWidget(self.cb_reason)

        txn_top.addWidget(QLabel("limit:"))
        self.cb_limit = QComboBox()
        self.cb_limit.addItems(["200", "500", "1000"])
        self.cb_limit.setCurrentText("200")
        txn_top.addWidget(self.cb_limit)

        self.btn_txn_toggle = QPushButton("전체 원장 보기")
        txn_top.addWidget(self.btn_txn_toggle)

        self.btn_export_txn = QPushButton("원장 CSV")
        txn_top.addWidget(self.btn_export_txn)

        # -------------------------
        # 원장 테이블
        # -------------------------
        self.tbl_txn = QTableWidget(0, 9)
        self.tbl_txn.setHorizontalHeaderLabels([
            "시간", "모델", "자재코드", "Δ", "사유", "ref_type", "ref_id", "note", "id"
        ])
        self.tbl_txn.setColumnHidden(8, True)

        th = self.tbl_txn.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        th.setSectionResizeMode(7, QHeaderView.Stretch)

        self.tbl_txn.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_txn.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_txn.setSortingEnabled(False)
        root.addWidget(self.tbl_txn, 1)

        # signals
        self.btn_refresh.clicked.connect(self.refresh_inventory)
        self.ed_search.textChanged.connect(self.refresh_inventory)
        self.cmb_model.currentIndexChanged.connect(self._refresh_txn_by_mode)
        self.chk_include_inactive.stateChanged.connect(self.refresh_inventory)
        self.tbl_inv.itemSelectionChanged.connect(self._on_select_material)

        self.btn_in.clicked.connect(lambda: self._apply_delta(+abs(float(self.sp_delta.value()))))
        self.btn_out.clicked.connect(lambda: self._apply_delta(-abs(float(self.sp_delta.value()))))
        self.btn_adjust.clicked.connect(lambda: self._apply_delta(float(self.sp_delta.value())))
        self.btn_set_abs.clicked.connect(self._set_abs_qty)

        self.cb_limit.currentTextChanged.connect(self._refresh_txn_by_mode)
        self.cb_reason.currentTextChanged.connect(self._refresh_txn_by_mode)
        self.btn_txn_toggle.clicked.connect(self._toggle_txn_mode)

        self.btn_export_inv.clicked.connect(self._export_inventory_csv)
        self.btn_export_txn.clicked.connect(self._export_txn_csv)

        self._set_action_enabled(False)

    def _set_action_enabled(self, enabled: bool):
        self.btn_in.setEnabled(enabled)
        self.btn_out.setEnabled(enabled)
        self.btn_adjust.setEnabled(enabled)
        self.btn_set_abs.setEnabled(enabled)
        self.btn_export_txn.setEnabled(enabled or self._show_all_txn)

    def _selected_material_code(self) -> Optional[str]:
        r = self.tbl_inv.currentRow()
        if r < 0:
            return None
        item = self.tbl_inv.item(r, 0)
        return item.text().strip() if item else None

    def _find_row_by_code(self, code: str) -> int:
        code = (code or "").strip()
        if not code:
            return -1
        for r in range(self.tbl_inv.rowCount()):
            it = self.tbl_inv.item(r, 0)
            if it and it.text().strip() == code:
                return r
        return -1

    def _current_limit(self) -> int:
        try:
            return int(self.cb_limit.currentText())
        except Exception:
            return 200

    def _reason_filter(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mode = (self.cb_reason.currentText() or "전체").strip()
        if mode == "전체":
            return rows

        def _r(x: Dict[str, Any]) -> str:
            return str(x.get("reason") or "").strip()

        if mode == "사용량(usage_apply/usage_revert)":
            return [x for x in rows if _r(x) in ("usage_apply", "usage_revert")]
        if mode == "수동(manual)":
            return [x for x in rows if _r(x).startswith("manual")]
        if mode == "설정(set_abs_qty)":
            return [x for x in rows if _r(x) == "set_abs_qty"]
        if mode == "기타":
            return [x for x in rows if _r(x) not in ("usage_apply", "usage_revert") and not _r(x).startswith("manual") and _r(x) != "set_abs_qty"]
        return rows

    def refresh_inventory(self):
        keep_code = self._selected_material_code() or self._last_selected_code

        try:
            include_inactive = self.chk_include_inactive.isChecked()
            rows = self.repo.list_inventory(include_inactive=include_inactive)
        except Exception as e:
            QMessageBox.critical(self, "재고 조회 실패", str(e))
            return

        q = self.ed_search.text().strip().lower()

        view = []
        for r in rows:
            code = str(r.get("code", "")).strip()
            name = str(r.get("name", "")).strip()
            spec = str(r.get("spec", "")).strip()
            unit = str(r.get("unit", "")).strip()
            vendor = str(r.get("vendor", "")).strip()
            active = int(r.get("active") or 0)
            qty = float(r.get("qty_on_hand") or 0.0)
            updated_at = str(r.get("updated_at") or "")

            hay = " ".join([code, name, spec, unit, vendor]).lower()
            if q and (q not in hay):
                continue

            view.append((code, name, spec, unit, vendor, active, qty, updated_at))

        self.tbl_inv.setSortingEnabled(False)
        self.tbl_inv.setRowCount(0)

        for code, name, spec, unit, vendor, active, qty, updated_at in view:
            i = self.tbl_inv.rowCount()
            self.tbl_inv.insertRow(i)

            self.tbl_inv.setItem(i, 0, _ro_item(code))
            self.tbl_inv.setItem(i, 1, _ro_item(name))
            self.tbl_inv.setItem(i, 2, _ro_item(spec))
            self.tbl_inv.setItem(i, 3, _ro_item(unit))
            self.tbl_inv.setItem(i, 4, _ro_item(vendor))
            st = _ro_item("사용" if active == 1 else "미사용")
            st.setTextAlignment(Qt.AlignCenter)
            self.tbl_inv.setItem(i, 5, st)

            qty_item = _num_item(qty, 6)
            if qty < 0:
                qty_item.setForeground(Qt.red)
            self.tbl_inv.setItem(i, 6, qty_item)

            self.tbl_inv.setItem(i, 7, _ro_item(updated_at))
            self.tbl_inv.setItem(i, 8, _ro_item("⚠ 음수재고" if qty < 0 else ""))

        self.tbl_inv.setSortingEnabled(True)

        if hasattr(self, "lb_model_note"):
            self.lb_model_note.setText(
                f"※ 현재고는 공용 재고입니다. 모델 [{self._model_filter_label()}] 선택은 아래 원장(BOM/수동 사용내역) 필터에 적용됩니다."
            )

        if keep_code:
            row = self._find_row_by_code(keep_code)
            if row >= 0:
                self.tbl_inv.setCurrentCell(row, 0)

        self._on_select_material()

    def _on_select_material(self):
        code = self._selected_material_code()
        self._last_selected_code = code

        if not code:
            self.lb_selected.setText("-")
            self.tbl_txn.setRowCount(0)
            self._set_action_enabled(False)

            if self._show_all_txn:
                self.btn_export_txn.setEnabled(True)
                self._refresh_txn_by_mode()
            else:
                self._update_txn_title()
            return

        self.lb_selected.setText(code)
        self._set_action_enabled(True)
        self._refresh_txn_by_mode()

    def _update_txn_title(self):
        lim = self._current_limit()
        model = self._model_filter_label()
        if self._show_all_txn:
            self.lb_txn_title.setText(f"재고 원장(전체 자재, 모델={model}, 최근 {lim}건):")
        else:
            code = self._selected_material_code() or "-"
            self.lb_txn_title.setText(f"재고 원장(선택 자재: {code}, 모델={model}, 최근 {lim}건):")

    def _toggle_txn_mode(self):
        self._show_all_txn = not self._show_all_txn
        self.btn_txn_toggle.setText("선택 자재 원장 보기" if self._show_all_txn else "전체 원장 보기")
        self.btn_export_txn.setEnabled(self._show_all_txn or bool(self._selected_material_code()))
        self._refresh_txn_by_mode()

    def _refresh_txn_by_mode(self):
        self._update_txn_title()

        lim = self._current_limit()
        if self._show_all_txn:
            try:
                rows = self.repo.list_inventory_txn("", limit=lim)
            except Exception as e:
                QMessageBox.critical(self, "원장 조회 실패", str(e))
                return
            rows = self._reason_filter(rows)
            rows = self._filter_txn_by_model(rows)
            self._fill_txn_table(rows)
            return

        code = self._selected_material_code()
        if not code:
            self.tbl_txn.setRowCount(0)
            return
        self.refresh_txn(code)

    def refresh_txn(self, material_code: str):
        lim = self._current_limit()
        try:
            rows = self.repo.list_inventory_txn(material_code, limit=lim)
        except Exception as e:
            QMessageBox.critical(self, "원장 조회 실패", str(e))
            return
        rows = self._reason_filter(rows)
        rows = self._filter_txn_by_model(rows)
        self._fill_txn_table(rows)

    def _fill_txn_table(self, rows: List[Dict[str, Any]]):
        self.tbl_txn.setRowCount(0)
        for r in rows:
            i = self.tbl_txn.rowCount()
            self.tbl_txn.insertRow(i)

            ts = r.get("ts")
            code = r.get("material_code", "")
            delta = r.get("qty_delta")
            row_model = r.get("_model")
            if row_model is None:
                row_model = self._txn_row_model(r)
            row_model = row_model or ""

            self.tbl_txn.setItem(i, 0, _ro_item(ts))
            self.tbl_txn.setItem(i, 1, _ro_item(row_model))
            self.tbl_txn.setItem(i, 2, _ro_item(code))

            delta_item = _num_item(delta, 6)
            try:
                if float(delta or 0) < 0:
                    delta_item.setForeground(Qt.red)
            except Exception:
                pass
            self.tbl_txn.setItem(i, 3, delta_item)

            self.tbl_txn.setItem(i, 4, _ro_item(r.get("reason")))
            self.tbl_txn.setItem(i, 5, _ro_item(r.get("ref_type")))
            self.tbl_txn.setItem(i, 6, _ro_item(r.get("ref_id")))
            self.tbl_txn.setItem(i, 7, _ro_item(r.get("note")))
            self.tbl_txn.setItem(i, 8, _ro_item(r.get("id")))

    def _apply_delta(self, delta: float):
        code = self._selected_material_code()
        if not code:
            QMessageBox.information(self, "선택", "자재를 먼저 선택하세요.")
            return

        if abs(delta) < 1e-15:
            QMessageBox.information(self, "입력", "수량(Δ)이 0입니다.")
            return

        # 현재고 조회
        try:
            cur_qty = float(self.repo.get_inventory_qty(code))
        except Exception:
            cur_qty = None

        if delta < 0:
            ok = QMessageBox.question(
                self,
                "확인",
                f"[{code}] 출고/감소(Δ={_fmt_num(delta)}) 처리할까요?",
                QMessageBox.Yes | QMessageBox.No
            )
            if ok != QMessageBox.Yes:
                return

            if cur_qty is not None:
                after = cur_qty + delta
                if after < 0:
                    ok2 = QMessageBox.question(
                        self,
                        "⚠ 현재고 부족",
                        f"[{code}] 현재고 {_fmt_num(cur_qty)} 에서\n"
                        f"Δ={_fmt_num(delta)} 적용 시\n"
                        f"결과가 {_fmt_num(after)} (음수) 입니다.\n\n"
                        f"repo가 음수재고를 막으면 실패할 수 있습니다.\n"
                        f"그래도 진행할까요?",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    if ok2 != QMessageBox.Yes:
                        return

        reason, ok = QInputDialog.getText(self, "사유", "입출고/조정 사유를 입력하세요(선택):")
        if not ok:
            return

        try:
            self.repo.adjust_inventory(
                material_code=code,
                qty_delta=delta,
                reason="manual_adjust" if not reason else reason,
                ref_type="manual",
                ref_id=None,
                note="inventory tab",
                allow_negative=False,  # ✅ 기본 음수 방지
            )
            self.refresh_inventory()
            self._refresh_txn_by_mode()
            QMessageBox.information(self, "완료", f"{code} 재고 반영 완료 (Δ={_fmt_num(delta)})")
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def _set_abs_qty(self):
        code = self._selected_material_code()
        if not code:
            QMessageBox.information(self, "선택", "자재를 먼저 선택하세요.")
            return

        try:
            cur_qty = float(self.repo.get_inventory_qty(code))
        except Exception:
            cur_qty = 0.0

        val, ok = QInputDialog.getDouble(
            self,
            "현재고=값 설정",
            f"[{code}] 현재고를 어떤 값으로 맞출까요?\n(현재: {_fmt_num(cur_qty)})",
            value=cur_qty,
            min=-1e12,
            max=1e12,
            decimals=6,
        )
        if not ok:
            return

        target = float(val)
        delta = target - cur_qty
        if abs(delta) < 1e-15:
            QMessageBox.information(self, "안내", "변경사항이 없습니다.")
            return

        # target이 음수면 강하게 차단(운영 신뢰도)
        if target < 0:
            QMessageBox.warning(self, "차단", "현재고를 음수로 설정할 수 없습니다.")
            return

        ok2 = QMessageBox.question(
            self,
            "확인",
            f"[{code}] 현재고를 {_fmt_num(target)} 로 설정합니다.\n"
            f"적용 Δ = {_fmt_num(delta)}\n진행할까요?",
            QMessageBox.Yes | QMessageBox.No
        )
        if ok2 != QMessageBox.Yes:
            return

        reason, ok3 = QInputDialog.getText(self, "사유", "설정 사유를 입력하세요(선택):")
        if not ok3:
            return

        try:
            self.repo.adjust_inventory(
                material_code=code,
                qty_delta=delta,
                reason="set_abs_qty" if not reason else reason,
                ref_type="manual",
                ref_id=None,
                note="inventory tab set abs",
                allow_negative=False,  # ✅ 기본 음수 방지
            )
            self.refresh_inventory()
            self._refresh_txn_by_mode()
            QMessageBox.information(self, "완료", f"{code} 현재고 설정 완료 (현재고={_fmt_num(target)})")
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def _export_inventory_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "현재고 CSV 저장",
            f"inventory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return

        headers = ["코드", "자재명", "스펙", "단위", "업체", "상태", "현재고", "업데이트", "비고"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for r in range(self.tbl_inv.rowCount()):
                    row = [
                        self.tbl_inv.item(r, 0).text() if self.tbl_inv.item(r, 0) else "",
                        self.tbl_inv.item(r, 1).text() if self.tbl_inv.item(r, 1) else "",
                        self.tbl_inv.item(r, 2).text() if self.tbl_inv.item(r, 2) else "",
                        self.tbl_inv.item(r, 3).text() if self.tbl_inv.item(r, 3) else "",
                        self.tbl_inv.item(r, 4).text() if self.tbl_inv.item(r, 4) else "",
                        self.tbl_inv.item(r, 5).text() if self.tbl_inv.item(r, 5) else "",
                        self.tbl_inv.item(r, 6).text() if self.tbl_inv.item(r, 6) else "",
                        self.tbl_inv.item(r, 7).text() if self.tbl_inv.item(r, 7) else "",
                        self.tbl_inv.item(r, 8).text() if self.tbl_inv.item(r, 8) else "",
                    ]
                    w.writerow(row)
            QMessageBox.information(self, "저장", "현재고 CSV 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def _export_txn_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "원장 CSV 저장",
            f"inventory_txn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return

        headers = ["시간", "모델", "자재코드", "Δ", "사유", "ref_type", "ref_id", "note"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for r in range(self.tbl_txn.rowCount()):
                    row = [
                        self.tbl_txn.item(r, 0).text() if self.tbl_txn.item(r, 0) else "",
                        self.tbl_txn.item(r, 1).text() if self.tbl_txn.item(r, 1) else "",
                        self.tbl_txn.item(r, 2).text() if self.tbl_txn.item(r, 2) else "",
                        self.tbl_txn.item(r, 3).text() if self.tbl_txn.item(r, 3) else "",
                        self.tbl_txn.item(r, 4).text() if self.tbl_txn.item(r, 4) else "",
                        self.tbl_txn.item(r, 5).text() if self.tbl_txn.item(r, 5) else "",
                        self.tbl_txn.item(r, 6).text() if self.tbl_txn.item(r, 6) else "",
                        self.tbl_txn.item(r, 7).text() if self.tbl_txn.item(r, 7) else "",
                    ]
                    w.writerow(row)
            QMessageBox.information(self, "저장", "원장 CSV 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))
