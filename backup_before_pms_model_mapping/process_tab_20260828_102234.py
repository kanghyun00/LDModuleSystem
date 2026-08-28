# -*- coding: utf-8 -*-
# src/ldms/ui/tabs/process_tab.py
from __future__ import annotations

from typing import List, Dict, Any, Optional
from collections import defaultdict
import re
import unicodedata
from datetime import datetime

from PyQt5.QtCore import QDate, Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QDateEdit,
    QSpinBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QMessageBox, QHeaderView, QStackedWidget, QInputDialog, QDialog,
    QDialogButtonBox, QFormLayout, QComboBox, QTextEdit, QDoubleSpinBox,
    QCheckBox
)

from src.ldms.services import pms_service
from src.ldms.services.process_progress_service import compute_progress_from_daily_records
from src.ldms.process_flow import (
    PROCESS_FLOW, DEFAULT_MODEL_CODE, get_process_flow,
    list_model_codes, normalize_model_code,
)
# ✅ 중복 import 제거 + flow_norm 제거 (공정 정규화 기준을 pms_service로 통일)
# from src.ldms.process_flow import PROCESS_FLOW, normalize_process_name as flow_norm


# =========================================================
# Utils
# =========================================================
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


def normalize_process_name(name: str) -> str:
    return pms_service.normalize_process_name(name)


def parse_module_numbers(text: str) -> List[str]:
    if not text:
        return []
    raw = text.replace(",", " ").split()
    out: List[str] = []
    seen = set()
    for t in raw:
        m = normalize_module_no(t)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _norm_modules_csv(s: str) -> str:
    if s is None:
        return ""
    txt = str(s).replace("\n", ",")
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    mods = [normalize_module_no(p) for p in parts]
    mods = [m for m in mods if m]
    return ",".join(mods)


def _split_modules(module_csv: str) -> List[str]:
    if module_csv is None:
        return []
    txt = str(module_csv).replace("\n", ",")
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    out = []
    seen = set()
    for p in parts:
        m = normalize_module_no(p)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ✅ 추가: 중복판정용 "정렬 키"
def _norm_modules_key(s: str) -> str:
    mods = _split_modules(_norm_modules_csv(s))
    mods = sorted(set(mods))
    return ",".join(mods)


def _format_top3_modules(proc_to_modules: Dict[str, set]) -> str:
    items = sorted(proc_to_modules.items(), key=lambda x: len(x[1]), reverse=True)
    top = []
    for proc, s in items[:3]:
        top.append(f"{proc} {len(s)}개")
    return " / ".join(top) if top else "데이터 없음"


def _week_range_monday(qd: QDate) -> tuple[QDate, QDate]:
    monday = qd.addDays(-(qd.dayOfWeek() - 1))
    sunday = monday.addDays(6)
    return monday, sunday


def _month_range(qd: QDate) -> tuple[QDate, QDate]:
    start = QDate(qd.year(), qd.month(), 1)
    end = start.addMonths(1).addDays(-1)
    return start, end


def _ro_item(v):
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


# =========================================================
# Dialog: Module status
# =========================================================
class _ModuleStatusDialog(QDialog):
    def __init__(self, parent, repo):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("불량/폐기 모듈 관리")
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_module = QLineEdit()
        self.cb_status = QComboBox()
        self.cb_status.addItems(["OK", "NG", "SCRAP"])
        self.ed_reason = QTextEdit()
        self.ed_reason.setFixedHeight(80)
        form.addRow("모듈번호", self.ed_module)
        form.addRow("상태", self.cb_status)
        form.addRow("사유(선택)", self.ed_reason)
        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_save(self):
        m = normalize_module_no(self.ed_module.text())
        if not m:
            QMessageBox.warning(self, "입력 오류", "모듈번호를 입력하세요.")
            return
        status = self.cb_status.currentText().strip().upper()
        reason = (self.ed_reason.toPlainText() or "").strip()
        self.repo.set_module_status(m, status=status, reason=reason)
        QMessageBox.information(self, "저장 완료", f"{m} 상태 저장: {status}")
        self.accept()


# =========================================================
# Dialog: Manual usage / return
# =========================================================
class _ManualUsageDialog(QDialog):
    def __init__(self, parent, repo, process_log_id: int):
        super().__init__(parent)
        self.repo = repo
        self.process_log_id = int(process_log_id)
        self.setWindowTitle("수동 자재 처리(추가사용/불량/재작업/반납)")
        self._build()
        self._load_materials()

    def _build(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.cb_material = QComboBox()
        self.cb_material.setEditable(True)
        self.cb_material.setInsertPolicy(QComboBox.NoInsert)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["추가 사용/불량/재작업(재고감소)", "반납/회수(재고증가)"])

        self.sp_qty = QDoubleSpinBox()
        self.sp_qty.setRange(0, 1e12)
        self.sp_qty.setDecimals(6)
        self.sp_qty.setValue(0.0)

        self.cb_reason = QComboBox()
        self.cb_reason.addItems(["EXTRA", "SCRAP", "REWORK", "ETC", "RETURN"])

        self.ed_note = QTextEdit()
        self.ed_note.setFixedHeight(70)

        self.ed_unit = QLineEdit("EA")
        self.ed_unit.setMaximumWidth(120)

        self.ed_operator = QLineEdit()

        form.addRow("자재(코드/검색)", self.cb_material)
        form.addRow("모드", self.cb_mode)
        form.addRow("수량", self.sp_qty)
        form.addRow("사유(reason)", self.cb_reason)

        unit_row = QHBoxLayout()
        unit_row.addWidget(self.ed_unit)
        unit_row.addStretch(1)
        w_unit = QWidget()
        w_unit.setLayout(unit_row)
        form.addRow("단위(unit)", w_unit)

        form.addRow("작업자(선택)", self.ed_operator)
        form.addRow("메모(note)", self.ed_note)

        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _load_materials(self):
        self.cb_material.blockSignals(True)
        self.cb_material.clear()
        try:
            mats = self.repo.list_materials(include_inactive=True)
        except Exception:
            mats = []

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

        self.cb_material.blockSignals(False)

    def _selected_material_code(self) -> str:
        code = self.cb_material.currentData()
        if code:
            return str(code).strip()
        txt = (self.cb_material.currentText() or "").strip()
        if "|" in txt:
            return txt.split("|", 1)[0].strip()
        return txt.strip()

    def _on_save(self):
        try:
            code = self._selected_material_code()
            if not code:
                raise ValueError("자재 코드를 입력/선택하세요.")
            qty = float(self.sp_qty.value())
            if qty <= 0:
                raise ValueError("수량은 0보다 커야 합니다.")
            reason = (self.cb_reason.currentText() or "EXTRA").strip()
            note = (self.ed_note.toPlainText() or "").strip()
            unit = (self.ed_unit.text() or "EA").strip() or "EA"
            operator = (self.ed_operator.text() or "").strip()

            mode = self.cb_mode.currentIndex()  # 0=consume, 1=return
            if mode == 0:
                if not hasattr(self.repo, "add_manual_usage"):
                    raise RuntimeError("repo.add_manual_usage()가 없습니다.")
                self.repo.add_manual_usage(
                    process_log_id=self.process_log_id,
                    material_code=code,
                    qty_used=qty,
                    reason=reason if reason != "RETURN" else "EXTRA",
                    note=note,
                    unit=unit,
                    operator=operator,
                )
            else:
                if not hasattr(self.repo, "add_manual_return"):
                    raise RuntimeError("repo.add_manual_return()가 없습니다.")
                self.repo.add_manual_return(
                    process_log_id=self.process_log_id,
                    material_code=code,
                    qty_return=qty,
                    reason=reason if reason else "RETURN",
                    note=note,
                    unit=unit,
                    operator=operator,
                )
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))


class _ManualUsageEditDialog(QDialog):
    def __init__(self, parent, repo, usage_row: Dict[str, Any]):
        super().__init__(parent)
        self.repo = repo
        self.usage = dict(usage_row)
        self.setWindowTitle("수동 사용/반납 수정")
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.lb_id = QLabel(str(self.usage.get("id", "")))
        self.lb_code = QLabel(str(self.usage.get("material_code", "")))
        self.lb_name = QLabel(str(self.usage.get("material_name", "")))

        self.sp_qty = QDoubleSpinBox()
        self.sp_qty.setRange(-1e12, 1e12)
        self.sp_qty.setDecimals(6)
        try:
            self.sp_qty.setValue(float(self.usage.get("qty_used") or 0.0))
        except Exception:
            self.sp_qty.setValue(0.0)

        self.ed_reason = QLineEdit(str(self.usage.get("reason", "")))
        self.ed_note = QLineEdit(str(self.usage.get("note", "")))

        form.addRow("usage_id", self.lb_id)
        form.addRow("자재코드", self.lb_code)
        form.addRow("자재명", self.lb_name)
        form.addRow("qty_used(+)사용 / (-)반납", self.sp_qty)
        form.addRow("reason", self.ed_reason)
        form.addRow("note", self.ed_note)

        lay.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def payload(self) -> Dict[str, Any]:
        return {
            "usage_id": int(self.usage.get("id")),
            "qty_used": float(self.sp_qty.value()),
            "reason": (self.ed_reason.text() or "").strip(),
            "note": (self.ed_note.text() or "").strip(),
        }


# =========================================================
# Main: ProcessTab
# =========================================================
class ProcessTab(QWidget):
    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        # 모델이 없는 레거시 기록을 특정 모델로 자동 귀속하지 않습니다.
        # 실제 모델이 확인된 경우에만 별도 마이그레이션으로 수정해야 합니다.
        self._build_ui()
        self._go_page("daily")

    def _load_model_codes(self) -> List[str]:
        """DB model_master 우선, 실패 시 process_flow 기본 모델 사용."""
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

    def _process_flow_for_model(self, model: str = "") -> List[str]:
        model = normalize_model_code(model or self._current_model())
        try:
            if hasattr(self.repo, "get_model_process_flow"):
                rows = self.repo.get_model_process_flow(model, include_inactive=False)
                flow = [str(r.get("process_name") or "").strip() for r in rows if str(r.get("process_name") or "").strip()]
                if flow:
                    return flow
        except Exception:
            pass
        try:
            return get_process_flow(model)
        except Exception:
            return list(PROCESS_FLOW or [])

    def _process_items_for_model(self, model: str = "") -> List[str]:
        """
        현재 선택 모델의 공정 후보.
        DB(model_process_flow)가 있으면 DB 우선, 실패 시 process_flow.py 기본값 사용.
        """
        model = normalize_model_code(model or self._current_model())
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
            items = self._process_flow_for_model(model)

        return items

    def _refresh_process_candidates(self) -> None:
        """
        모델 변경 시 공정 입력 후보를 해당 모델 공정순서로 다시 채움.
        """
        if not hasattr(self, "proc_edit"):
            return
        try:
            cur_txt = ""
            if isinstance(self.proc_edit, QComboBox):
                cur_txt = self.proc_edit.currentText().strip()
                self.proc_edit.blockSignals(True)
                self.proc_edit.clear()
                self.proc_edit.addItems(self._process_items_for_model(self._current_model()))
                if cur_txt:
                    self.proc_edit.setCurrentText(cur_txt)
                self.proc_edit.blockSignals(False)
        except Exception:
            pass

    def _canonical_legacy_process(self, proc_norm: str) -> str:
        """
        기존 PMS 엑셀에서 쓰던 공정명과 현재 공정명을 최대한 통일.
        예: 미러정렬 -> MIRROR1정렬
        """
        p = normalize_process_name(proc_norm)

        alias = {
            "미러정렬": "MIRROR1정렬",
            "MIRROR정렬": "MIRROR1정렬",
            "MIRROR1": "MIRROR1정렬",
            "MIRROR1정렬": "MIRROR1정렬",
            "MIRROR 1정렬": "MIRROR1정렬",
            "MIRROR1ALIGN": "MIRROR1정렬",
            "리듀서정렬": "REDUCER정렬",
            "REDUCER정렬": "REDUCER정렬",
            "REDUCERALIGN": "REDUCER정렬",
            "파이버정렬": "파이버정렬",
            "FIBER정렬": "파이버정렬",
            "FIBERALIGN": "파이버정렬",
            "LID부착": "LID부착",
            "LID착": "LID부착",
            "리플로우": "리플로우",
            "REFLOW": "리플로우",
            "침본딩": "칩본딩",
            "칩본딩": "칩본딩",
            "전극바부착": "전극바부착",
            "패키지마킹": "패키지마킹",
            "FAC정렬": "FAC정렬",
            "SAC정렬": "SAC정렬",
            "PBS정렬": "PBS정렬",
            "VBG정렬": "VBG정렬",
        }
        return alias.get(p, p)

    def _ensure_process_in_model_flow(self, proc_norm: str, model: str = "") -> str:
        """
        PMS 전체 불러오기용 안전장치.
        기존 PMS에 등록된 공정명이 현재 모델 공정순서에 없으면,
        import를 막지 않고 해당 모델 공정순서 맨 뒤에 자동 추가한다.
        """
        model = normalize_model_code(model or self._current_model())
        proc_norm = self._canonical_legacy_process(proc_norm)
        flow = [normalize_process_name(p) for p in self._process_items_for_model(model)]

        if proc_norm in flow:
            return proc_norm

        try:
            if hasattr(self.repo, "get_model_process_flow") and hasattr(self.repo, "set_model_process_flow"):
                rows = self.repo.get_model_process_flow(model, include_inactive=True)
                existing = []
                for r in rows:
                    p = normalize_process_name(r.get("process_name") or "")
                    if p and p not in existing:
                        existing.append(p)
                if proc_norm not in existing:
                    existing.append(proc_norm)
                    self.repo.set_model_process_flow(model, existing)
                    self._refresh_process_candidates()
                    return proc_norm
        except Exception:
            pass

        # repo에 set_model_process_flow가 없으면 화면 공정 후보에라도 추가해서 이번 import는 통과
        return proc_norm

    def _validate_process_for_model(self, proc_norm: str, model: str = "") -> None:
        """
        선택 모델에 등록된 공정만 저장/수정 허용.
        단, PMS 전체 불러오기에서는 _ensure_process_in_model_flow()를 먼저 호출해
        기존 PMS 공정을 모델 공정순서에 자동 편입할 수 있다.
        """
        model = normalize_model_code(model or self._current_model())
        proc_norm = self._canonical_legacy_process(proc_norm)
        flow = [normalize_process_name(p) for p in self._process_items_for_model(model)]
        if flow and proc_norm not in flow:
            raise ValueError(
                f"현재 모델 [{model}]에 등록되지 않은 공정입니다.\n\n"
                f"- 입력 공정: {proc_norm}\n"
                f"- 해결: Parameter > 모델 설정에서 [{model}] 공정순서에 공정을 추가하거나, "
                f"상단 모델 선택을 확인하세요."
            )

    def on_model_process_flow_changed(self, model_code: str) -> None:
        """
        Parameter > 모델 설정에서 공정순서가 저장됐을 때 호출된다.

        기존 공정기록(PMS + DB)은 건드리지 않고,
        현재 저장된 모델별 공정순서를 다시 읽어서 아래 항목을 즉시 재계산한다.
        - 현재공정
        - 다음공정
        - 진행률
        - 완료수
        - 공정별 대기목록 / 대기수
        - 완성품 여부
        """
        changed_model = normalize_model_code(model_code or DEFAULT_MODEL_CODE)

        # 현재 선택 중인 모델이 변경 대상일 때 즉시 화면과 후보를 갱신
        if changed_model == self._current_model():
            self._refresh_process_candidates()

            # 진행현황 화면이 아니어도 계산은 안전하게 다시 해 둔다.
            # 사용자가 진행현황 탭으로 이동하면 현재 DB 공정순서를 다시 읽어 표시한다.
            try:
                self.refresh_progress()
            except Exception:
                pass

            # 현재 보고 있는 화면도 필요한 경우 갱신
            try:
                cur = self.stack.currentWidget() if hasattr(self, "stack") else None
                if cur == getattr(self, "page_daily", None):
                    self.refresh_daily()
                elif cur == getattr(self, "page_summary", None):
                    self._update_summary_label_only()
            except Exception:
                pass

    def _on_model_changed(self):
        try:
            if hasattr(self, "lb_model_hint"):
                flow_cnt = len(self._process_items_for_model(self._current_model()))
                self.lb_model_hint.setText(f"선택 모델 공정 {flow_cnt}개만 표시/관리")
        except Exception:
            pass
        self._refresh_process_candidates()
        cur = self.stack.currentWidget() if hasattr(self, "stack") else None
        if cur == getattr(self, "page_daily", None):
            self.refresh_daily()
        elif cur == getattr(self, "page_summary", None):
            self._update_summary_label_only()
        elif cur == getattr(self, "page_progress", None):
            self.refresh_progress()
        elif cur == getattr(self, "page_import", None) and hasattr(self, "import_log"):
            self.import_log.appendPlainText(f"현재 선택 모델: {self._current_model()}")

    def _repo_insert_process_log(self, work_date, process_name, process_name_raw, qty, remark, operator, module_nos, model: str = DEFAULT_MODEL_CODE) -> int:
        if hasattr(self.repo, "insert_process_log_with_materials"):
            try:
                return self.repo.insert_process_log_with_materials(
                    work_date=work_date,
                    process_name=process_name,
                    process_name_raw=process_name_raw,
                    qty=qty,
                    remark=remark,
                    operator=operator,
                    module_nos=module_nos,
                    model=model,
                )
            except TypeError:
                return self.repo.insert_process_log_with_materials(
                    work_date=work_date,
                    process_name=process_name,
                    process_name_raw=process_name_raw,
                    qty=qty,
                    remark=remark,
                    operator=operator,
                    module_nos=module_nos,
                )

        try:
            return self.repo.insert_process_log(
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
                model=model,
            )
        except TypeError:
            return self.repo.insert_process_log(
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
            )


    def _repo_insert_process_log_no_materials(
        self,
        work_date,
        process_name,
        process_name_raw,
        qty,
        remark,
        operator,
        module_nos,
        model: str = DEFAULT_MODEL_CODE,
    ) -> int:
        """
        ✅ PMS 과거 데이터 import 전용.
        - 과거 PMS 기록을 DB로 옮길 때는 이미 지나간 작업이므로 재고/BOM을 다시 차감하면 안 됨.
        - 그래서 insert_process_log_with_materials가 아니라 기본 process_log만 저장.
        """
        if not hasattr(self.repo, "insert_process_log"):
            # 최후 fallback. 원칙적으로 repo에는 insert_process_log가 있어야 함.
            return self._repo_insert_process_log(
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
                model=model,
            )

        try:
            return self.repo.insert_process_log(
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
                model=model,
            )
        except TypeError:
            # 구버전 repo 호환
            return self.repo.insert_process_log(
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
            )


    def _repo_update_process_log(self, process_log_id, work_date, process_name, process_name_raw, qty, remark, operator, module_nos, model: str = DEFAULT_MODEL_CODE) -> None:
        if hasattr(self.repo, "update_process_log_with_materials"):
            try:
                self.repo.update_process_log_with_materials(
                    process_log_id=process_log_id,
                    work_date=work_date,
                    process_name=process_name,
                    process_name_raw=process_name_raw,
                    qty=qty,
                    remark=remark,
                    operator=operator,
                    module_nos=module_nos,
                    model=model,
                )
            except TypeError:
                self.repo.update_process_log_with_materials(
                    process_log_id=process_log_id,
                    work_date=work_date,
                    process_name=process_name,
                    process_name_raw=process_name_raw,
                    qty=qty,
                    remark=remark,
                    operator=operator,
                    module_nos=module_nos,
                )
            return

        try:
            self.repo.update_process_log(
                process_log_id=process_log_id,
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
                model=model,
            )
        except TypeError:
            self.repo.update_process_log(
                process_log_id=process_log_id,
                work_date=work_date,
                process_name=process_name,
                process_name_raw=process_name_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
            )

    def _repo_delete_process_log(self, process_log_id: int) -> None:
        if hasattr(self.repo, "delete_process_log_with_materials"):
            self.repo.delete_process_log_with_materials(process_log_id)
            return
        self.repo.delete_process_log(process_log_id)

    def _format_usage_popup(self, title: str, usage_rows: List[Dict[str, object]]) -> str:
        if not usage_rows:
            return f"{title}\n\n- 사용 내역 없음 (BOM이 비어있거나 qty=0, 또는 수동사용 없음)"
        lines = [title, ""]
        for u in usage_rows:
            utype = str(u.get("usage_type") or "BOM").upper()
            reason = str(u.get("reason") or "").strip()
            note = str(u.get("note") or "").strip()

            code = str(u.get("material_code") or "")
            name = str(u.get("material_name") or "") or code
            spec = str(u.get("material_spec") or "")
            qty = u.get("qty_used")
            unit = str(u.get("unit") or "EA")

            tag = "BOM" if utype == "BOM" else f"MANUAL/{reason or 'ETC'}"
            label = f"- [{tag}] {name} [{code}]"
            if spec:
                label += f" ({spec})"
            label += f" : {qty} {unit}"
            if note and utype == "MANUAL":
                label += f"  / note: {note}"
            lines.append(label)
        return "\n".join(lines)

    def _safe_list_usage(self, process_log_id: int) -> List[Dict[str, Any]]:
        if hasattr(self.repo, "list_material_usage"):
            return self.repo.list_material_usage(process_log_id)
        if hasattr(self.repo, "list_material_usage_by_process_log"):
            return self.repo.list_material_usage_by_process_log(process_log_id)
        return []

    def _guard_qty_vs_modules(self, qty: int, module_nos: List[str], ctx: str) -> Optional[int]:
        if not module_nos:
            return qty

        mcnt = len(module_nos)
        if qty == mcnt:
            return qty

        ret = QMessageBox.question(
            self,
            "⚠ 수량/모듈수 불일치",
            f"[{ctx}] 수량(qty)={qty} 인데 모듈이 {mcnt}개 입니다.\n\n"
            f"이대로 저장하면 BOM 차감도 qty={qty} 기준으로 나갑니다.\n\n"
            f"YES: qty를 모듈수({mcnt})로 자동 맞춤\n"
            f"NO: 경고 무시하고 그대로 저장\n"
            f"CANCEL: 취소",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
        )
        if ret == QMessageBox.Cancel:
            return None
        if ret == QMessageBox.Yes:
            return mcnt
        return qty

    def _precheck_bom_shortage(self, process_name_norm: str, qty: int) -> None:
        if not hasattr(self.repo, "preview_bom_shortage"):
            return
        try:
            bad = self.repo.preview_bom_shortage(process_name_norm, qty, model=self._current_model())
        except TypeError:
            bad = self.repo.preview_bom_shortage(process_name_norm, qty)
        if not bad:
            return

        lines = ["[BOM 차감 시 음수재고 발생] 아래 자재가 부족합니다.\n"]
        for b in bad[:50]:
            lines.append(
                f"- {b['material_code']}: 현재고={b['on_hand']} / 필요={b['required']} / 적용후={b['after']}"
            )
        if len(bad) > 50:
            lines.append(f"... 외 {len(bad)-50}개")

        QMessageBox.critical(self, "저장 차단(음수재고)", "\n".join(lines))
        raise ValueError("BOM 차감 시 음수재고 발생 → 저장 차단")

    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("날짜"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate.currentDate())
        top.addWidget(self.date_edit)

        top.addWidget(QLabel("모델"))
        self.cb_model = QComboBox()
        self.cb_model.setMinimumWidth(150)
        self.cb_model.addItems(self._load_model_codes())
        idx_default = self.cb_model.findText(DEFAULT_MODEL_CODE)
        if idx_default >= 0:
            self.cb_model.setCurrentIndex(idx_default)
        top.addWidget(self.cb_model)

        self.lb_model_hint = QLabel("")
        self.lb_model_hint.setStyleSheet("color:#666;")
        top.addWidget(self.lb_model_hint)

        self.btn_input = QPushButton("입력/저장")
        self.btn_daily = QPushButton("일일 목록")
        self.btn_summary = QPushButton("주간/월간")
        self.btn_import = QPushButton("PMS 불러오기")
        self.btn_progress = QPushButton("진행현황")
        top.addSpacing(16)
        top.addWidget(self.btn_input)
        top.addWidget(self.btn_daily)
        top.addWidget(self.btn_summary)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_progress)
        top.addStretch(1)
        root.addLayout(top)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.page_input = QWidget()
        self.page_daily = QWidget()
        self.page_summary = QWidget()
        self.page_import = QWidget()
        self.page_progress = QWidget()

        self._build_page_input(self.page_input)
        self._build_page_daily(self.page_daily)
        self._build_page_summary(self.page_summary)
        self._build_page_import(self.page_import)
        self._build_page_progress(self.page_progress)

        self.stack.addWidget(self.page_input)
        self.stack.addWidget(self.page_daily)
        self.stack.addWidget(self.page_summary)
        self.stack.addWidget(self.page_import)
        self.stack.addWidget(self.page_progress)

        self.btn_input.clicked.connect(lambda: self._go_page("input"))
        self.btn_daily.clicked.connect(lambda: self._go_page("daily"))
        self.btn_summary.clicked.connect(lambda: self._go_page("summary"))
        self.btn_import.clicked.connect(lambda: self._go_page("import"))
        self.btn_progress.clicked.connect(lambda: self._go_page("progress"))
        self.date_edit.dateChanged.connect(self._on_date_changed)
        self.cb_model.currentIndexChanged.connect(self._on_model_changed)
        self._refresh_process_candidates()
        self._on_model_changed()

    def _go_page(self, name: str):
        if name == "input":
            self.stack.setCurrentWidget(self.page_input)
            self._refresh_process_candidates()
        elif name == "daily":
            self.stack.setCurrentWidget(self.page_daily)
            self.refresh_daily()
        elif name == "summary":
            self.stack.setCurrentWidget(self.page_summary)
            self._update_summary_label_only()
        elif name == "import":
            self.stack.setCurrentWidget(self.page_import)
        elif name == "progress":
            self.stack.setCurrentWidget(self.page_progress)
            self.refresh_progress()

    def _on_date_changed(self, _qd: QDate):
        cur = self.stack.currentWidget()
        if cur == self.page_daily:
            self.refresh_daily()
        elif cur == self.page_summary:
            self._update_summary_label_only()
        elif cur == self.page_progress:
            self.refresh_progress()

    # -------------------------
    # Page: Input
    # -------------------------
    def _build_page_input(self, page: QWidget):
        layout = QVBoxLayout(page)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("공정명"))
        self.proc_edit = QComboBox()
        self.proc_edit.setEditable(True)
        self.proc_edit.setInsertPolicy(QComboBox.NoInsert)
        self.proc_edit.setMinimumWidth(240)
        self.proc_edit.addItems(self._process_items_for_model(self._current_model()))
        row1.addWidget(self.proc_edit, 3)
        row1.addWidget(QLabel("수량"))
        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(0, 999999)
        row1.addWidget(self.qty_spin)
        row1.addWidget(QLabel("작업자"))
        self.op_edit = QLineEdit()
        row1.addWidget(self.op_edit, 1)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("모듈번호 (쉼표/공백/줄바꿈 가능, 중복 자동 제거)"))
        self.modules_edit = QPlainTextEdit()
        left.addWidget(self.modules_edit)
        right = QVBoxLayout()
        right.addWidget(QLabel("비고"))
        self.remark_edit = QPlainTextEdit()
        right.addWidget(self.remark_edit)
        row2.addLayout(left, 2)
        row2.addLayout(right, 1)
        layout.addLayout(row2)

        btns = QHBoxLayout()
        self.btn_save = QPushButton("저장")
        self.btn_go_daily = QPushButton("저장 후 일일 목록 보기")
        btns.addWidget(self.btn_save)
        btns.addWidget(self.btn_go_daily)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.btn_save.clicked.connect(self.on_save)
        self.btn_go_daily.clicked.connect(lambda: self._go_page("daily"))

    def on_save(self):
        try:
            work_date = self.date_edit.date().toString("yyyy-MM-dd")
            model = self._current_model()
            proc_raw = (self.proc_edit.currentText() if isinstance(self.proc_edit, QComboBox) else self.proc_edit.text() or "").strip()
            if not proc_raw:
                raise ValueError("공정명을 입력하세요.")
            proc_norm = self._canonical_legacy_process(proc_raw)
            self._validate_process_for_model(proc_norm, model)
            qty = int(self.qty_spin.value())
            operator = (self.op_edit.text() or "").strip()
            remark = (self.remark_edit.toPlainText() or "").strip()
            module_nos = parse_module_numbers(self.modules_edit.toPlainText())

            new_qty = self._guard_qty_vs_modules(qty, module_nos, ctx="저장")
            if new_qty is None:
                return
            qty = int(new_qty)

            if qty > 0:
                self._precheck_bom_shortage(proc_norm, qty)

            module_csv = ", ".join(module_nos)

            log_id = self._repo_insert_process_log(
                work_date=work_date,
                process_name=proc_norm,
                process_name_raw=proc_raw,
                qty=qty,
                remark=remark,
                operator=operator,
                module_nos=module_nos,
                model=model,
            )

            usage = self._safe_list_usage(log_id)
            msg = self._format_usage_popup(
                f"저장 완료: [{model}] {proc_norm} (qty={qty})\n[자재 내역(BOM + 수동)]",
                usage
            )

            try:
                pms_service.append_daily_record_xlsx(work_date, proc_norm, qty, module_csv, remark)
            except Exception as e:
                msg += f"\n\n⚠ PMS 엑셀 반영 실패(파일 열림/권한 등):\n{e}\n(DB가 기준입니다)"

            if isinstance(self.proc_edit, QComboBox):
                self.proc_edit.setCurrentIndex(0 if self.proc_edit.count() > 0 else -1)
            else:
                self.proc_edit.clear()
            self.qty_spin.setValue(0)
            self.modules_edit.clear()
            self.remark_edit.clear()

            QMessageBox.information(self, "저장 완료", msg)
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))

    # -------------------------
    # Page: Daily list
    # -------------------------
    def _build_page_daily(self, page: QWidget):
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("새로고침")
        self.chk_daily_all_dates = QCheckBox("전체 날짜")
        self.chk_daily_all_dates.setToolTip("체크하면 선택한 모델의 모든 날짜 공정기록을 표시합니다.")
        self.btn_edit = QPushButton("선택 수정")
        self.btn_delete = QPushButton("선택 삭제")
        self.btn_manual_usage = QPushButton("수동 자재(추가/불량/재작업/반납)")
        self.btn_view_usage = QPushButton("선택 자재내역 보기")
        self.btn_edit_manual = QPushButton("수동내역 수정")
        self.btn_delete_manual = QPushButton("수동내역 삭제")
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.chk_daily_all_dates)
        bar.addWidget(self.btn_edit)
        bar.addWidget(self.btn_delete)
        bar.addSpacing(12)
        bar.addWidget(self.btn_manual_usage)
        bar.addWidget(self.btn_view_usage)
        bar.addWidget(self.btn_edit_manual)
        bar.addWidget(self.btn_delete_manual)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.btn_refresh.clicked.connect(self.refresh_daily)
        self.chk_daily_all_dates.stateChanged.connect(lambda _: self.refresh_daily())
        self.btn_edit.clicked.connect(self.on_edit_selected)
        self.btn_delete.clicked.connect(self.on_delete_selected)
        self.btn_manual_usage.clicked.connect(self.on_manual_usage_selected)
        self.btn_view_usage.clicked.connect(self.on_view_usage_selected)
        self.btn_edit_manual.clicked.connect(self.on_edit_manual_usage)
        self.btn_delete_manual.clicked.connect(self.on_delete_manual_usage)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "ID", "날짜", "모델", "공정명(정규화)", "공정명(원문)", "수량", "모듈번호", "작업자", "비고"
        ])
        self.table.setColumnHidden(0, True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        layout.addWidget(self.table)

    def refresh_daily(self):
        work_date = self.date_edit.date().toString("yyyy-MM-dd")
        model = self._current_model()

        show_all_dates = bool(getattr(self, "chk_daily_all_dates", None) and self.chk_daily_all_dates.isChecked())
        if show_all_dates:
            d1, d2 = self._pms_date_range_from_files()
            date_from = d1 or work_date
            date_to = d2 or work_date
        else:
            date_from = work_date
            date_to = work_date

        try:
            db_rows = self.repo.list_process_logs(date_from=date_from, date_to=date_to, model=model)
        except TypeError:
            db_rows = self.repo.list_process_logs(date_from=date_from, date_to=date_to)
            db_rows = [
                r for r in db_rows
                if normalize_model_code(r.get("model") or "UNKNOWN") == model
            ]

        pms_rows = self._load_pms_rows_for_daily_view(date_from, date_to, model)
        rows = self._merge_db_operator_into_pms_rows(pms_rows, db_rows)

        # 최종 방어: 현재 선택 모델과 다른 기록은 화면에 절대 표시하지 않습니다.
        rows = [
            r for r in rows
            if normalize_model_code(
                r.get("model") or "UNKNOWN"
            ) == model
        ]

        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, _ro_item(r.get("id")))
            self.table.setItem(i, 1, _ro_item(r.get("work_date")))
            self.table.setItem(i, 2, _ro_item(r.get("model") or "UNKNOWN"))
            self.table.setItem(i, 3, _ro_item(r.get("process_name")))
            self.table.setItem(i, 4, _ro_item(r.get("process_name_raw")))
            self.table.setItem(i, 5, _ro_item(r.get("qty")))
            self.table.setItem(i, 6, _ro_item(r.get("modules_csv") or ""))
            self.table.setItem(i, 7, _ro_item(r.get("operator") or ""))
            self.table.setItem(i, 8, _ro_item(r.get("remark") or ""))

    def _selected_process_log_id(self) -> int:
        row = self.table.currentRow()
        if row < 0:
            raise ValueError("행을 선택하세요.")
        pid_item = self.table.item(row, 0)
        if not pid_item or not pid_item.text().strip():
            raise ValueError("선택한 행의 ID를 찾지 못했습니다.")
        return int(pid_item.text().strip())

    def on_view_usage_selected(self):
        try:
            pid = self._selected_process_log_id()
            rec = self.repo.get_process_log(pid)
            if not rec:
                raise ValueError("기록을 DB에서 찾지 못했습니다.")
            usage = self._safe_list_usage(pid)
            title = f"공정: {rec.get('process_name')} / qty={rec.get('qty')}\n[자재 내역(BOM + 수동)]"
            QMessageBox.information(self, "자재 내역", self._format_usage_popup(title, usage))
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def on_manual_usage_selected(self):
        try:
            pid = self._selected_process_log_id()
            rec = self.repo.get_process_log(pid)
            if not rec:
                raise ValueError("기록을 DB에서 찾지 못했습니다.")

            dlg = _ManualUsageDialog(self, self.repo, pid)
            if dlg.exec_():
                usage = self._safe_list_usage(pid)
                QMessageBox.information(
                    self,
                    "반영 완료",
                    self._format_usage_popup("수동 자재 반영 완료\n[현재 자재 내역]", usage),
                )
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def _pick_manual_usage_row(self, pid: int) -> Optional[Dict[str, Any]]:
        usage = self._safe_list_usage(pid)
        manual = [u for u in usage if str(u.get("usage_type") or "").upper() == "MANUAL"]
        if not manual:
            QMessageBox.information(self, "안내", "선택 공정에 수동(MANUAL) 내역이 없습니다.")
            return None

        items = []
        for u in manual:
            items.append(
                f"#{u.get('id')} | {u.get('material_code')} | qty_used={u.get('qty_used')} | {u.get('reason')} | {u.get('note')}"
            )
        sel, ok = QInputDialog.getItem(self, "수동 내역 선택", "수정/삭제할 수동내역:", items, 0, False)
        if not ok or not sel:
            return None
        try:
            uid = int(str(sel).split("|", 1)[0].strip().lstrip("#"))
        except Exception:
            return None
        for u in manual:
            if int(u.get("id") or 0) == uid:
                return u
        return None

    def on_edit_manual_usage(self):
        try:
            pid = self._selected_process_log_id()
            u = self._pick_manual_usage_row(pid)
            if not u:
                return
            dlg = _ManualUsageEditDialog(self, self.repo, u)
            if dlg.exec_():
                p = dlg.payload()
                self.repo.update_manual_usage(
                    usage_id=int(p["usage_id"]),
                    new_qty_used=float(p["qty_used"]),
                    new_reason=p.get("reason", ""),
                    new_note=p.get("note", ""),
                )
                usage = self._safe_list_usage(pid)
                QMessageBox.information(self, "수정 완료", self._format_usage_popup("수동 내역 수정 후\n[현재 자재 내역]", usage))
        except Exception as e:
            QMessageBox.critical(self, "수정 실패", str(e))

    def on_delete_manual_usage(self):
        try:
            pid = self._selected_process_log_id()
            u = self._pick_manual_usage_row(pid)
            if not u:
                return
            uid = int(u.get("id") or 0)
            ret = QMessageBox.question(
                self,
                "삭제 확인",
                f"수동 내역 #{uid} 를 삭제할까요?\n삭제 시 재고가 원복됩니다.",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return
            self.repo.delete_manual_usage(uid)
            usage = self._safe_list_usage(pid)
            QMessageBox.information(self, "삭제 완료", self._format_usage_popup("수동 내역 삭제 후\n[현재 자재 내역]", usage))
        except Exception as e:
            QMessageBox.critical(self, "삭제 실패", str(e))

    def on_edit_selected(self):
        try:
            process_log_id = self._selected_process_log_id()
            rec = self.repo.get_process_log(process_log_id)
            if not rec:
                raise ValueError("수정할 기록을 DB에서 찾지 못했습니다.")

            work_date = rec.get("work_date") or ""
            old_model = normalize_model_code(rec.get("model") or self._current_model())
            old_proc_norm = rec.get("process_name") or ""
            old_proc_raw = rec.get("process_name_raw") or ""
            old_qty = int(rec.get("qty") or 0)
            old_modules_csv = rec.get("modules_csv") or ""
            old_remark = rec.get("remark") or ""
            old_operator = rec.get("operator") or ""

            old_usage = self._safe_list_usage(process_log_id)

            new_model = self._current_model()
            if old_model != new_model:
                raise ValueError(
                    f"현재 선택 모델[{new_model}]과 선택 기록 모델[{old_model}]이 다릅니다.\n"
                    f"상단 모델을 [{old_model}]로 변경한 뒤 수정하세요."
                )

            proc_items = self._process_items_for_model(new_model)
            old_proc_display = normalize_process_name(old_proc_raw or old_proc_norm)
            if proc_items:
                idx_proc = proc_items.index(old_proc_display) if old_proc_display in proc_items else 0
                new_proc_raw, ok = QInputDialog.getItem(self, "수정", "공정명", proc_items, idx_proc, True)
            else:
                new_proc_raw, ok = QInputDialog.getText(self, "수정", "공정명(원문)", text=str(old_proc_raw))
            if not ok:
                return
            new_proc_raw = (new_proc_raw or "").strip()
            if not new_proc_raw:
                raise ValueError("공정명은 비워둘 수 없습니다.")
            new_proc_norm = self._canonical_legacy_process(new_proc_raw)
            self._validate_process_for_model(new_proc_norm, new_model)

            new_qty_str, ok = QInputDialog.getText(self, "수정", "수량", text=str(old_qty))
            if not ok:
                return
            new_qty = int(str(new_qty_str).strip() or "0")

            new_modules_text, ok = QInputDialog.getMultiLineText(
                self, "수정", "모듈번호(쉼표/공백/줄바꿈 가능)", text=str(old_modules_csv)
            )
            if not ok:
                return
            new_module_nos = parse_module_numbers(new_modules_text)

            guarded = self._guard_qty_vs_modules(new_qty, new_module_nos, ctx="수정")
            if guarded is None:
                return
            new_qty = int(guarded)

            new_modules_csv = ", ".join(new_module_nos)

            new_operator, ok = QInputDialog.getText(self, "수정", "작업자", text=str(old_operator))
            if not ok:
                return
            new_operator = (new_operator or "").strip()

            new_remark, ok = QInputDialog.getMultiLineText(self, "수정", "비고", text=str(old_remark))
            if not ok:
                return
            new_remark = (new_remark or "").strip()

            if new_qty > 0:
                self._precheck_bom_shortage(new_proc_norm, new_qty)

            ret = QMessageBox.question(
                self, "수정 확인",
                "선택한 기록을 수정할까요?\n(DB 기준으로 진행됩니다)\n\n"
                "※ 수동 출고(MANUAL)는 유지되고, BOM만 재계산됩니다。",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

            self._repo_update_process_log(
                process_log_id=process_log_id,
                work_date=work_date,
                process_name=new_proc_norm,
                process_name_raw=new_proc_raw,
                qty=new_qty,
                remark=new_remark,
                operator=new_operator,
                module_nos=new_module_nos,
                model=new_model,
            )

            new_usage = self._safe_list_usage(process_log_id)

            try:
                pms_service.update_daily_record_xlsx(
                    work_date=work_date,
                    old_process=old_proc_norm,
                    old_qty=old_qty,
                    old_module_csv=old_modules_csv,
                    old_remark=old_remark,
                    new_process=new_proc_norm,
                    new_qty=new_qty,
                    new_module_csv=new_modules_csv,
                    new_remark=new_remark,
                )
            except Exception:
                pass

            self.refresh_daily()

            msg_parts = []
            msg_parts.append(self._format_usage_popup("수정 전 자재 내역(참고)", old_usage))
            msg_parts.append("")
            msg_parts.append(self._format_usage_popup("수정 후 자재 내역(BOM 재계산 + MANUAL 유지)", new_usage))
            QMessageBox.information(self, "수정 완료", "\n".join(msg_parts))
        except Exception as e:
            QMessageBox.critical(self, "수정 실패", str(e))

    def on_delete_selected(self):
        try:
            process_log_id = self._selected_process_log_id()

            rec = self.repo.get_process_log(process_log_id)
            if not rec:
                raise ValueError("삭제할 기록을 DB에서 찾지 못했습니다.")

            proc_norm = rec.get("process_name") or ""
            qty = int(rec.get("qty") or 0)

            old_usage = self._safe_list_usage(process_log_id)

            ret = QMessageBox.question(
                self,
                "삭제 확인",
                f"선택한 기록을 삭제할까요?\n\n{proc_norm} (qty={qty})\n"
                f"삭제 시 BOM+수동(반납 포함) 내역이 있으면 재고가 원복됩니다.",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return

            self._repo_delete_process_log(process_log_id)

            try:
                pms_service.delete_daily_record_xlsx(
                    rec.get("work_date") or "",
                    proc_norm,
                    qty,
                    rec.get("modules_csv") or "",
                    rec.get("remark") or "",
                )
            except Exception:
                pass

            self.refresh_daily()

            msg = self._format_usage_popup(
                f"삭제 완료: {proc_norm}\n[원복된 자재 내역(BOM+수동)]",
                old_usage
            )
            QMessageBox.information(self, "삭제 완료", msg)
        except Exception as e:
            QMessageBox.critical(self, "삭제 실패", str(e))

    # -------------------------
    # Page: Summary (✅ DB 기준)
    # -------------------------
    def _build_page_summary(self, page: QWidget):
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.btn_week_summary = QPushButton("선택일 주간 현황(DB)")
        self.btn_month_summary = QPushButton("선택일 월간 현황(DB)")
        bar.addWidget(self.btn_week_summary)
        bar.addWidget(self.btn_month_summary)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.summary_label = QLabel("현황: (주간/월간 버튼을 누르면 표시됩니다)")
        layout.addWidget(self.summary_label)

        self.summary_table = QTableWidget(0, 3)
        self.summary_table.setHorizontalHeaderLabels(["공정명", "총 진행 모듈 수(중복제외)", "진행된 모듈 번호"])
        hdr = self.summary_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.summary_table)

        self.btn_week_summary.clicked.connect(self.on_week_summary)
        self.btn_month_summary.clicked.connect(self.on_month_summary)

    def _collect_proc_modules_between_db(self, d1: str, d2: str) -> Dict[str, set]:
        """
        주간/월간 집계.
        - F976370W 과거 자료는 data/pms 엑셀을 직접 읽음
        - DB에만 있는 수기 기록도 추가 반영
        """
        model = self._current_model()

        try:
            db_rows = self.repo.list_process_logs(date_from=d1, date_to=d2, model=model)
        except TypeError:
            db_rows = self.repo.list_process_logs(date_from=d1, date_to=d2)
            db_rows = [
                r for r in db_rows
                if normalize_model_code(r.get("model") or "UNKNOWN") == model
            ]

        pms_rows = self._load_pms_rows_for_daily_view(d1, d2, model)
        rows = self._merge_db_operator_into_pms_rows(pms_rows, db_rows)

        proc_to_modules: Dict[str, set] = defaultdict(set)
        for r in rows:
            row_model = normalize_model_code(r.get("model") or "UNKNOWN")
            if row_model != model:
                continue
            proc_norm = self._canonical_legacy_process(r.get("process_name") or r.get("process_name_raw") or "")
            for m in _split_modules(r.get("modules_csv") or ""):
                proc_to_modules[proc_norm].add(m)
        return proc_to_modules

    def _fill_summary_table(self, proc_to_modules: Dict[str, set]):
        items = sorted(proc_to_modules.items(), key=lambda x: len(x[1]), reverse=True)
        self.summary_table.setRowCount(0)
        for proc, modset in items:
            mods = sorted(list(modset))
            mods_text = ", ".join(mods)
            i = self.summary_table.rowCount()
            self.summary_table.insertRow(i)
            self.summary_table.setItem(i, 0, _ro_item(proc))
            self.summary_table.setItem(i, 1, _ro_item(len(modset)))
            self.summary_table.setItem(i, 2, _ro_item(mods_text))

    def _update_summary_label_only(self):
        base = self.date_edit.date()
        ws, we = _week_range_monday(base)
        ms, me = _month_range(base)
        self.summary_label.setText(
            f"모델 [{self._current_model()}] 기준: "
            f"주간({ws.toString('yyyy-MM-dd')}~{we.toString('yyyy-MM-dd')}), "
            f"월간({ms.toString('yyyy-MM-dd')}~{me.toString('yyyy-MM-dd')})"
        )

    def on_week_summary(self):
        try:
            base = self.date_edit.date()
            start, end = _week_range_monday(base)
            d1 = start.toString("yyyy-MM-dd")
            d2 = end.toString("yyyy-MM-dd")
            proc_to_modules = self._collect_proc_modules_between_db(d1, d2)
            top3 = _format_top3_modules(proc_to_modules)
            self.summary_label.setText(f"주간(DB 기준) 모델[{self._current_model()}] ({d1}~{d2}) 총 진행 모듈 수 TOP3: {top3}")
            self._fill_summary_table(proc_to_modules)
        except Exception as e:
            QMessageBox.critical(self, "주간 현황 오류", str(e))

    def on_month_summary(self):
        try:
            base = self.date_edit.date()
            start, end = _month_range(base)
            d1 = start.toString("yyyy-MM-dd")
            d2 = end.toString("yyyy-MM-dd")
            proc_to_modules = self._collect_proc_modules_between_db(d1, d2)
            top3 = _format_top3_modules(proc_to_modules)
            self.summary_label.setText(f"월간(DB 기준) 모델[{self._current_model()}] ({d1}~{d2}) 총 진행 모듈 수 TOP3: {top3}")
            self._fill_summary_table(proc_to_modules)
        except Exception as e:
            QMessageBox.critical(self, "월간 현황 오류", str(e))

    # -------------------------
    # Page: Import PMS
    # -------------------------
    def _build_page_import(self, page: QWidget):
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.btn_import_pms = QPushButton("선택일 PMS 엑셀 새로고침")
        bar.addWidget(self.btn_import_pms)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.import_log = QPlainTextEdit()
        self.import_log.setReadOnly(True)
        layout.addWidget(self.import_log)
        self.btn_import_pms.clicked.connect(self.on_import_pms_for_date)

    def on_import_pms_for_date(self):
        """
        ✅ PMS 파일이 원본이므로 DB로 import하지 않는다.
        선택일 PMS 파일을 다시 읽도록 일일목록만 갱신한다.
        """
        try:
            self.refresh_daily()
            model = self._current_model()
            work_date = self.date_edit.date().toString("yyyy-MM-dd")
            msg = f"{work_date} PMS 파일 기준으로 일일목록을 새로고침했습니다. / 모델[{model}]"
            self.import_log.appendPlainText(msg)
            QMessageBox.information(self, "PMS 새로고침", msg)
        except Exception as e:
            QMessageBox.critical(self, "PMS 새로고침 실패", str(e))

    def _pms_date_range_from_files(self):
        try:
            pms_dir = pms_service.get_pms_dir()
        except Exception:
            return None, None

        dates = []
        for fp in pms_dir.glob("*.xlsx"):
            try:
                d = datetime.strptime(fp.stem, "%Y-%m-%d").date()
                dates.append(d)
            except Exception:
                continue
        if not dates:
            return None, None
        dates.sort()
        return dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")

    def _load_pms_rows_for_daily_view(
        self,
        date_from: str,
        date_to: str,
        model: str,
    ) -> List[Dict[str, Any]]:
        """모델 정보가 확인된 PMS 행만 화면에 표시합니다.

        현재 구형 PMS 엑셀은 모델 컬럼이 없으므로 45W/370W를 구분할 수
        없습니다. 따라서 모델 없는 PMS 행을 F976370W로 임의 표시하지 않습니다.
        pms_service가 향후 model/model_code를 반환하면 해당 모델만 표시합니다.
        """
        model = normalize_model_code(model)

        try:
            rows = pms_service.load_process_rows_between(date_from, date_to)
        except Exception:
            return []

        out: List[Dict[str, Any]] = []

        for r in rows:
            # PMS 파일의 모델 컬럼을 우선 확인합니다.
            row_model_raw = (
                r.get("model")
                or r.get("model_code")
                or r.get("product_model")
                or ""
            )
            row_model = normalize_model_code(str(row_model_raw).strip())

            # 모델 정보가 없는 PMS 기록은 특정 모델 화면에 표시하지 않습니다.
            if not row_model or row_model == DEFAULT_MODEL_CODE and not str(row_model_raw).strip():
                continue

            if row_model != model:
                continue

            work_date = str(r.get("work_date") or "").strip()
            proc_raw = str(r.get("process") or "").strip()
            proc_norm = self._canonical_legacy_process(
                r.get("process_norm") or proc_raw
            )
            qty = int(r.get("qty") or 0)
            modules_csv = _norm_modules_csv(r.get("modules_csv") or "")
            remark = str(r.get("remark") or "").strip()

            if not work_date or not proc_norm:
                continue

            out.append({
                "id": "",
                "work_date": work_date,
                "model": row_model,
                "process_name": proc_norm,
                "process_name_raw": proc_raw or proc_norm,
                "qty": qty,
                "modules_csv": modules_csv,
                "operator": "",
                "remark": remark,
                "_source": "PMS",
            })

        return out

    def _merge_db_operator_into_pms_rows(self, pms_rows: List[Dict[str, Any]], db_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        PMS row와 같은 DB row가 있으면 operator/id를 DB값으로 보강.
        단, 오늘 잘못 import된 operator=system 행은 무시.
        """
        db_map: Dict[tuple, Dict[str, Any]] = {}
        for d in db_rows:
            op = str(d.get("operator") or "").strip()
            if op.lower() == "system":
                continue
            key = (
                str(d.get("work_date") or ""),
                normalize_model_code(d.get("model") or DEFAULT_MODEL_CODE),
                str(d.get("process_name") or ""),
                int(d.get("qty") or 0),
                _norm_modules_csv(d.get("modules_csv") or ""),
                str(d.get("remark") or ""),
            )
            db_map[key] = d

        merged = []
        seen = set()
        for p in pms_rows:
            key = (
                str(p.get("work_date") or ""),
                normalize_model_code(p.get("model") or DEFAULT_MODEL_CODE),
                str(p.get("process_name") or ""),
                int(p.get("qty") or 0),
                _norm_modules_csv(p.get("modules_csv") or ""),
                str(p.get("remark") or ""),
            )
            d = db_map.get(key)
            if d:
                p = dict(p)
                p["id"] = d.get("id") or ""
                p["operator"] = d.get("operator") or ""
                p["_source"] = "PMS+DB"
            merged.append(p)
            seen.add(key)

        # PMS에는 없고 DB에만 있는 수기 기록은 추가 표시
        for d in db_rows:
            op = str(d.get("operator") or "").strip()
            if op.lower() == "system":
                continue
            key = (
                str(d.get("work_date") or ""),
                normalize_model_code(d.get("model") or DEFAULT_MODEL_CODE),
                str(d.get("process_name") or ""),
                int(d.get("qty") or 0),
                _norm_modules_csv(d.get("modules_csv") or ""),
                str(d.get("remark") or ""),
            )
            if key in seen:
                continue
            dd = dict(d)
            dd["_source"] = "DB"
            merged.append(dd)

        merged.sort(key=lambda r: (str(r.get("work_date") or ""), str(r.get("process_name") or ""), str(r.get("modules_csv") or "")), reverse=True)
        return merged


    # -------------------------
    # Page: Progress (✅ FIX: modules_csv를 1개 모듈당 1행으로 풀어서 compute에 전달)
    # -------------------------
    def _build_page_progress(self, page: QWidget):
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.btn_progress_refresh = QPushButton("새로고침")
        self.btn_status_manage = QPushButton("불량/폐기 관리")
        bar.addWidget(self.btn_progress_refresh)
        bar.addWidget(self.btn_status_manage)
        bar.addSpacing(12)

        self.btn_view_inprogress = QPushButton("진행중")
        self.btn_view_finished = QPushButton("완성품 목록")
        self.btn_view_ngscrap = QPushButton("불량/폐기 목록")
        self.btn_view_process = QPushButton("공정별 대기목록")
        bar.addWidget(self.btn_view_inprogress)
        bar.addWidget(self.btn_view_finished)
        bar.addWidget(self.btn_view_ngscrap)
        bar.addWidget(self.btn_view_process)

        bar.addStretch(1)
        layout.addLayout(bar)

        self.btn_progress_refresh.clicked.connect(self.refresh_progress)
        self.btn_status_manage.clicked.connect(self.on_open_status_dialog)

        self.progress_info = QLabel("진행현황: ✅ DB 누적 기록 기준(전체 날짜)")
        layout.addWidget(self.progress_info)

        self.progress_stack = QStackedWidget()
        layout.addWidget(self.progress_stack)

        w0 = QWidget()
        l0 = QVBoxLayout(w0)
        self.tbl_inprogress = QTableWidget(0, 9)
        self.tbl_inprogress.setHorizontalHeaderLabels(["모듈번호", "모델", "상태", "사유", "현재공정(완료)", "다음공정", "진행률", "완료수", "최근일자"])
        hdr0 = self.tbl_inprogress.horizontalHeader()
        hdr0.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr0.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        hdr0.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        self.tbl_inprogress.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_inprogress.setSelectionMode(QTableWidget.SingleSelection)
        l0.addWidget(self.tbl_inprogress)

        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        self.tbl_finished = QTableWidget(0, 6)
        self.tbl_finished.setHorizontalHeaderLabels(["모듈번호", "모델", "상태", "사유", "완료공정", "최근일자"])
        hdr1 = self.tbl_finished.horizontalHeader()
        hdr1.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr1.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr1.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr1.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_finished.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_finished.setSelectionMode(QTableWidget.SingleSelection)
        l1.addWidget(self.tbl_finished)

        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        self.tbl_ngscrap = QTableWidget(0, 6)
        self.tbl_ngscrap.setHorizontalHeaderLabels(["모듈번호", "모델", "상태", "사유", "현재공정(완료)", "최근일자"])
        hdr2 = self.tbl_ngscrap.horizontalHeader()
        hdr2.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr2.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr2.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr2.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_ngscrap.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_ngscrap.setSelectionMode(QTableWidget.SingleSelection)
        l2.addWidget(self.tbl_ngscrap)

        w3 = QWidget()
        l3 = QVBoxLayout(w3)
        self.tbl_process_waiting = QTableWidget(0, 3)
        self.tbl_process_waiting.setHorizontalHeaderLabels(["공정명", "대기수", "진행 예정 모듈(불량/폐기/완성품 제외)"])
        hdr3 = self.tbl_process_waiting.horizontalHeader()
        hdr3.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr3.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr3.setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_process_waiting.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_process_waiting.setSelectionMode(QTableWidget.SingleSelection)
        l3.addWidget(self.tbl_process_waiting)

        self.progress_stack.addWidget(w0)
        self.progress_stack.addWidget(w1)
        self.progress_stack.addWidget(w2)
        self.progress_stack.addWidget(w3)

        self.btn_view_inprogress.clicked.connect(lambda: self.progress_stack.setCurrentIndex(0))
        self.btn_view_finished.clicked.connect(lambda: self.progress_stack.setCurrentIndex(1))
        self.btn_view_ngscrap.clicked.connect(lambda: self.progress_stack.setCurrentIndex(2))
        self.btn_view_process.clicked.connect(lambda: self.progress_stack.setCurrentIndex(3))
        self.progress_stack.setCurrentIndex(0)

    def on_open_status_dialog(self):
        dlg = _ModuleStatusDialog(self, self.repo)
        if dlg.exec_():
            self.refresh_progress()

    def _collect_rows_for_progress(self, model: str) -> List[Dict[str, Any]]:
        """
        진행현황용 누적 row 수집.
        - F976370W 과거 자료는 PMS 엑셀 직접 조회
        - DB에만 있는 수기 기록은 추가 반영
        """
        d1, d2 = self._pms_date_range_from_files()
        if not d1 or not d2:
            d1 = "1900-01-01"
            d2 = "2999-12-31"

        try:
            db_rows = self.repo.list_process_logs(model=model)
        except TypeError:
            db_rows = self.repo.list_process_logs()
            db_rows = [
                r for r in db_rows
                if normalize_model_code(r.get("model") or "UNKNOWN") == model
            ]

        pms_rows = self._load_pms_rows_for_daily_view(d1, d2, model)
        return self._merge_db_operator_into_pms_rows(pms_rows, db_rows)

    def refresh_progress(self):
        try:
            model = self._current_model()
            flow = [self._canonical_legacy_process(p) for p in self._process_flow_for_model(model)]
            flow = [p for p in flow if p]

            rows = self._collect_rows_for_progress(model)

            records: List[Dict[str, str]] = []
            for r in rows:
                row_model = normalize_model_code(r.get("model") or "UNKNOWN")
                if row_model != model:
                    continue
                work_date = str(r.get("work_date") or r.get("date") or "")
                proc = self._canonical_legacy_process(r.get("process_name") or r.get("process_name_raw") or "")
                modules_csv = str(r.get("modules_csv") or "")
                for m in _split_modules(modules_csv):
                    records.append({
                        "date": work_date,
                        "process": proc,
                        "module_no": m,
                        "model": row_model,
                    })

            try:
                module_map, waiting_by_process = compute_progress_from_daily_records(
                    records,
                    model=model,
                    process_flow=flow,
                )
            except TypeError:
                module_map, waiting_by_process = compute_progress_from_daily_records(records, model=model)

            status_map = self.repo.get_module_status_map()

            last_flow = flow[-1] if flow else ""
            finished_set = set()
            for m, prog in module_map.items():
                if (prog.next_process or "").upper() == "DONE":
                    finished_set.add(m)
                elif prog.last_process == last_flow:
                    finished_set.add(m)

            inprogress_items = []
            finished_items = []
            ngscrap_items = []

            for prog in module_map.values():
                m = prog.module_no
                st = (status_map.get(m, {}).get("status") or "OK").upper()
                rs = status_map.get(m, {}).get("reason") or ""

                if st in ("NG", "SCRAP"):
                    ngscrap_items.append((prog, st, rs))
                elif m in finished_set:
                    finished_items.append((prog, st, rs))
                else:
                    inprogress_items.append((prog, st, rs))

            # 진행중은 뒤 공정일수록 위에 보이게 정렬
            inprogress_items.sort(key=lambda x: (-(x[0].progress_index if x[0].progress_index >= 0 else -1), x[0].module_no))
            finished_items.sort(key=lambda x: x[0].module_no)
            ngscrap_items.sort(key=lambda x: x[0].module_no)

            def _prog_model(prog):
                return getattr(prog, "model", model) or model

            def _done_count_text(prog):
                done = getattr(prog, "done_count", "")
                total = getattr(prog, "total_count", len(flow))
                if done == "":
                    return ""
                return f"{done}/{total}"

            def _last_date(prog):
                return getattr(prog, "last_date", "") or ""

            self.tbl_inprogress.setRowCount(0)
            for prog, st, rs in inprogress_items:
                i = self.tbl_inprogress.rowCount()
                self.tbl_inprogress.insertRow(i)
                self.tbl_inprogress.setItem(i, 0, _ro_item(prog.module_no))
                self.tbl_inprogress.setItem(i, 1, _ro_item(_prog_model(prog)))
                self.tbl_inprogress.setItem(i, 2, _ro_item(st))
                self.tbl_inprogress.setItem(i, 3, _ro_item(rs))
                self.tbl_inprogress.setItem(i, 4, _ro_item(prog.last_process))
                self.tbl_inprogress.setItem(i, 5, _ro_item(prog.next_process))
                self.tbl_inprogress.setItem(i, 6, _ro_item(f"{int(prog.progress_ratio * 100)}%"))
                self.tbl_inprogress.setItem(i, 7, _ro_item(_done_count_text(prog)))
                self.tbl_inprogress.setItem(i, 8, _ro_item(_last_date(prog)))

            self.tbl_finished.setRowCount(0)
            for prog, st, rs in finished_items:
                i = self.tbl_finished.rowCount()
                self.tbl_finished.insertRow(i)
                self.tbl_finished.setItem(i, 0, _ro_item(prog.module_no))
                self.tbl_finished.setItem(i, 1, _ro_item(_prog_model(prog)))
                self.tbl_finished.setItem(i, 2, _ro_item(st))
                self.tbl_finished.setItem(i, 3, _ro_item(rs))
                self.tbl_finished.setItem(i, 4, _ro_item(prog.last_process))
                self.tbl_finished.setItem(i, 5, _ro_item(_last_date(prog)))

            self.tbl_ngscrap.setRowCount(0)
            for prog, st, rs in ngscrap_items:
                i = self.tbl_ngscrap.rowCount()
                self.tbl_ngscrap.insertRow(i)
                self.tbl_ngscrap.setItem(i, 0, _ro_item(prog.module_no))
                self.tbl_ngscrap.setItem(i, 1, _ro_item(_prog_model(prog)))
                self.tbl_ngscrap.setItem(i, 2, _ro_item(st))
                self.tbl_ngscrap.setItem(i, 3, _ro_item(rs))
                self.tbl_ngscrap.setItem(i, 4, _ro_item(prog.last_process))
                self.tbl_ngscrap.setItem(i, 5, _ro_item(_last_date(prog)))

            excluded_ngscrap = {
                m for m, v in status_map.items()
                if (v.get("status") or "OK").upper() in ("NG", "SCRAP")
            }
            excluded_all = set(excluded_ngscrap) | set(finished_set)

            self.tbl_process_waiting.setRowCount(0)
            for proc in flow:
                mods = waiting_by_process.get(proc, []) or []
                mods = [m for m in mods if m not in excluded_all]
                mods_text = ", ".join(mods)
                i = self.tbl_process_waiting.rowCount()
                self.tbl_process_waiting.insertRow(i)
                self.tbl_process_waiting.setItem(i, 0, _ro_item(proc))
                self.tbl_process_waiting.setItem(i, 1, _ro_item(len(mods)))
                self.tbl_process_waiting.setItem(i, 2, _ro_item(mods_text))

            self.progress_info.setText(
                f"진행현황: 모델 [{model}] / PMS+DB 누적 기준 | "
                f"총 {len(module_map)} / 진행중 {len(inprogress_items)} / "
                f"완성품 {len(finished_items)} / 불량·폐기 {len(ngscrap_items)}"
            )

        except Exception as e:
            QMessageBox.critical(self, "진행현황 갱신 실패", str(e))

