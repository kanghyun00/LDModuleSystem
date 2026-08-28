# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Dict, Any
import re
import unicodedata

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QComboBox, QLineEdit,
    QPlainTextEdit, QGroupBox, QFormLayout
)

from src.ldms.process_flow import DEFAULT_MODEL_CODE, list_model_codes, normalize_model_code


def _ro_item(v) -> QTableWidgetItem:
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def normalize_module_no(raw: str) -> str:
    if raw is None:
        return ""
    s = unicodedata.normalize("NFKC", str(raw)).strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9A-Za-z]", "", s)
    s = s.upper()

    m = re.match(r"^([A-Z]+)(\d+)$", s)
    if not m:
        return s

    prefix, num_str = m.group(1), m.group(2)
    width = max(3, len(num_str))
    try:
        num_val = int(num_str)
    except Exception:
        return s
    return f"{prefix}{num_val:0{width}d}"


def parse_module_numbers(text: str) -> List[str]:
    if not text:
        return []
    raw = text.replace(",", " ").replace(";", " ").split()
    out: List[str] = []
    seen = set()
    for t in raw:
        m = normalize_module_no(t)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


class ParameterModuleTab(QWidget):
    """
    모듈번호 ↔ 모델 관리 탭.

    목적:
    - 모듈번호별 모델을 명시적으로 관리
    - 검색/비교, 재고 원장, 공정관리, 측정데이터 조회에서 모델 필터 정확도 향상
    - 사용자가 공정관리 상단 모델을 잘못 선택했을 때도 나중에 모듈 기준으로 보정 가능
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._build_ui()
        self.refresh_table()

    # -------------------------
    # Helpers
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

    def _current_filter_model(self) -> str:
        txt = (self.cb_filter_model.currentText() or "").strip()
        if not txt or txt == "(전체)":
            return ""
        return normalize_model_code(txt)

    def _selected_save_model(self) -> str:
        return normalize_model_code(self.cb_model.currentText() or DEFAULT_MODEL_CODE)

    def _query_modules(self) -> List[Dict[str, Any]]:
        """
        modules 테이블 전체 조회.
        repo에 list_modules가 없어도 repo.connect()로 직접 조회한다.
        """
        rows: List[Dict[str, Any]] = []
        try:
            with self.repo.connect() as conn:
                # 혹시 기존 DB에 컬럼이 없더라도 안전하게 처리
                cur = conn.execute("PRAGMA table_info(modules)")
                cols = [str(r[1]) for r in cur.fetchall()]
                has_lot = "lot" in cols
                has_customer = "customer" in cols
                has_created = "created_at" in cols

                select_cols = ["module_no", "COALESCE(model, '') AS model"]
                if has_lot:
                    select_cols.append("COALESCE(lot, '') AS lot")
                else:
                    select_cols.append("'' AS lot")
                if has_customer:
                    select_cols.append("COALESCE(customer, '') AS customer")
                else:
                    select_cols.append("'' AS customer")
                if has_created:
                    select_cols.append("COALESCE(created_at, '') AS created_at")
                else:
                    select_cols.append("'' AS created_at")

                q = f"""
                SELECT {', '.join(select_cols)}
                FROM modules
                ORDER BY module_no ASC
                """
                fetched = conn.execute(q).fetchall()
                rows = [dict(r) for r in fetched]
        except Exception:
            rows = []
        return rows

    def _infer_missing_modules_from_process_logs(self) -> int:
        """
        DB process_log_modules에는 있는데 modules 테이블에 없는 모듈을 기본 370W로 등록.
        PMS 엑셀 전용 과거 데이터는 DB에 없는 경우가 있으므로 여기서는 DB 기준만 보정.
        """
        inserted = 0
        try:
            with self.repo.connect() as conn:
                rows = conn.execute("""
                    SELECT DISTINCT plm.module_no
                    FROM process_log_modules plm
                    LEFT JOIN modules m ON m.module_no = plm.module_no
                    WHERE m.module_no IS NULL
                    ORDER BY plm.module_no ASC
                """).fetchall()

            for r in rows:
                m = normalize_module_no(r["module_no"])
                if not m:
                    continue
                self.repo.upsert_module(m, model=DEFAULT_MODEL_CODE)
                inserted += 1
        except Exception:
            pass
        return inserted

    # -------------------------
    # UI
    # -------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("모듈번호 ↔ 모델 관리")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        root.addWidget(title)

        hint = QLabel(
            "※ 앞으로 여러 출력 모델을 같이 관리하려면 모듈번호별 모델을 등록해두는 것이 가장 안전합니다. "
            "검색/비교, 재고 원장, 진행현황의 모델 판별에 사용됩니다."
        )
        hint.setStyleSheet("color:#666;")
        root.addWidget(hint)

        # 단일 등록
        g1 = QGroupBox("단일 모듈 등록/수정")
        f1 = QFormLayout(g1)

        self.ed_module = QLineEdit()
        self.ed_module.setPlaceholderText("예: A001, C002, E092")
        f1.addRow("모듈번호", self.ed_module)

        self.cb_model = QComboBox()
        self.cb_model.addItems(self._load_model_codes())
        idx = self.cb_model.findText(DEFAULT_MODEL_CODE)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        f1.addRow("모델", self.cb_model)

        self.ed_lot = QLineEdit()
        self.ed_lot.setPlaceholderText("선택")
        f1.addRow("Lot", self.ed_lot)

        self.ed_customer = QLineEdit()
        self.ed_customer.setPlaceholderText("선택")
        f1.addRow("Customer", self.ed_customer)

        btn_single = QHBoxLayout()
        self.btn_save_one = QPushButton("단일 저장")
        self.btn_clear = QPushButton("입력 초기화")
        btn_single.addWidget(self.btn_save_one)
        btn_single.addWidget(self.btn_clear)
        btn_single.addStretch(1)
        f1.addRow(btn_single)

        root.addWidget(g1)

        # 일괄 등록
        g2 = QGroupBox("일괄 등록/수정")
        l2 = QVBoxLayout(g2)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("일괄 적용 모델"))
        self.cb_bulk_model = QComboBox()
        self.cb_bulk_model.addItems(self._load_model_codes())
        idx = self.cb_bulk_model.findText(DEFAULT_MODEL_CODE)
        if idx >= 0:
            self.cb_bulk_model.setCurrentIndex(idx)
        row2.addWidget(self.cb_bulk_model)
        row2.addStretch(1)
        l2.addLayout(row2)

        self.ed_bulk_modules = QPlainTextEdit()
        self.ed_bulk_modules.setPlaceholderText("모듈번호를 쉼표/공백/줄바꿈으로 입력\n예: A001 A002 A003\n또는\nC001, C002, C003")
        self.ed_bulk_modules.setFixedHeight(90)
        l2.addWidget(self.ed_bulk_modules)

        self.btn_bulk_save = QPushButton("일괄 저장")
        l2.addWidget(self.btn_bulk_save)

        root.addWidget(g2)

        # 목록
        top = QHBoxLayout()
        top.addWidget(QLabel("필터 모델"))
        self.cb_filter_model = QComboBox()
        self.cb_filter_model.addItem("(전체)")
        self.cb_filter_model.addItems(self._load_model_codes())
        top.addWidget(self.cb_filter_model)

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("모듈번호 검색")
        top.addWidget(self.ed_search, 1)

        self.btn_refresh = QPushButton("새로고침")
        self.btn_infer = QPushButton("DB 공정기록에서 누락 모듈 보정")
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_infer)
        root.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["모듈번호", "모델", "Lot", "Customer", "Created"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        root.addWidget(self.table, 1)

        self.lb_info = QLabel("")
        self.lb_info.setStyleSheet("color:#666;")
        root.addWidget(self.lb_info)

        # signals
        self.btn_save_one.clicked.connect(self.save_one)
        self.btn_clear.clicked.connect(self.clear_inputs)
        self.btn_bulk_save.clicked.connect(self.bulk_save)
        self.btn_refresh.clicked.connect(self.refresh_table)
        self.btn_infer.clicked.connect(self.infer_missing_modules)
        self.cb_filter_model.currentIndexChanged.connect(self.refresh_table)
        self.ed_search.textChanged.connect(self.refresh_table)
        self.table.itemSelectionChanged.connect(self.on_table_selected)

    # -------------------------
    # Actions
    # -------------------------
    def clear_inputs(self):
        self.ed_module.clear()
        self.ed_lot.clear()
        self.ed_customer.clear()
        idx = self.cb_model.findText(DEFAULT_MODEL_CODE)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)

    def save_one(self):
        try:
            module_no = normalize_module_no(self.ed_module.text())
            if not module_no:
                raise ValueError("모듈번호를 입력하세요.")

            model = self._selected_save_model()
            lot = (self.ed_lot.text() or "").strip()
            customer = (self.ed_customer.text() or "").strip()

            if hasattr(self.repo, "upsert_module"):
                self.repo.upsert_module(module_no, model=model, lot=lot, customer=customer)
            else:
                raise RuntimeError("repo.upsert_module()가 없습니다.")

            self.refresh_table()
            QMessageBox.information(self, "저장 완료", f"{module_no} → {model} 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))

    def bulk_save(self):
        try:
            mods = parse_module_numbers(self.ed_bulk_modules.toPlainText())
            if not mods:
                raise ValueError("일괄 저장할 모듈번호를 입력하세요.")

            model = normalize_model_code(self.cb_bulk_model.currentText() or DEFAULT_MODEL_CODE)

            ret = QMessageBox.question(
                self,
                "일괄 저장 확인",
                f"{len(mods)}개 모듈을 [{model}] 모델로 저장할까요?",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

            for m in mods:
                self.repo.upsert_module(m, model=model)

            self.refresh_table()
            QMessageBox.information(self, "일괄 저장 완료", f"{len(mods)}개 모듈 → {model} 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "일괄 저장 실패", str(e))

    def infer_missing_modules(self):
        try:
            n = self._infer_missing_modules_from_process_logs()
            self.refresh_table()
            QMessageBox.information(self, "보정 완료", f"누락 모듈 {n}개를 기본 모델 [{DEFAULT_MODEL_CODE}]로 보정했습니다.")
        except Exception as e:
            QMessageBox.critical(self, "보정 실패", str(e))

    def on_table_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return

        def cell(c):
            it = self.table.item(row, c)
            return it.text() if it else ""

        module_no = cell(0)
        model = cell(1)
        lot = cell(2)
        customer = cell(3)

        self.ed_module.setText(module_no)
        idx = self.cb_model.findText(model)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        self.ed_lot.setText(lot)
        self.ed_customer.setText(customer)

    def refresh_table(self):
        try:
            model_filter = self._current_filter_model()
            q = (self.ed_search.text() or "").strip().upper()

            rows = self._query_modules()
            filtered = []
            for r in rows:
                module_no = normalize_module_no(r.get("module_no") or "")
                model = normalize_model_code(r.get("model") or DEFAULT_MODEL_CODE)

                if model_filter and model != model_filter:
                    continue
                if q and q not in module_no:
                    continue

                rr = dict(r)
                rr["module_no"] = module_no
                rr["model"] = model
                filtered.append(rr)

            self.table.setRowCount(0)
            for r in filtered:
                i = self.table.rowCount()
                self.table.insertRow(i)
                self.table.setItem(i, 0, _ro_item(r.get("module_no")))
                self.table.setItem(i, 1, _ro_item(r.get("model")))
                self.table.setItem(i, 2, _ro_item(r.get("lot") or ""))
                self.table.setItem(i, 3, _ro_item(r.get("customer") or ""))
                self.table.setItem(i, 4, _ro_item(r.get("created_at") or ""))

            self.lb_info.setText(f"표시 {len(filtered)}개 / 전체 {len(rows)}개")
        except Exception as e:
            QMessageBox.critical(self, "조회 실패", str(e))
