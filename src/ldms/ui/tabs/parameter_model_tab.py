# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Any, List

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QGroupBox, QFormLayout, QLineEdit, QDoubleSpinBox, QCheckBox,
    QPlainTextEdit, QSplitter
)

from src.ldms.process_flow import (
    DEFAULT_MODEL_CODE,
    MODEL_RECIPES,
    get_process_flow,
    normalize_model_code,
    normalize_process_name,
)


def _ro_item(v) -> QTableWidgetItem:
    it = QTableWidgetItem("" if v is None else str(v))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def _num_text(v) -> str:
    try:
        x = float(v)
        if abs(x - int(x)) < 1e-12:
            return str(int(x))
        return f"{x:.4f}".rstrip("0").rstrip(".")
    except Exception:
        return "" if v is None else str(v)


class ParameterModelTab(QWidget):
    # 모델별 공정순서가 저장되면 상위 탭/MainWindow에 알림
    model_process_flow_changed = pyqtSignal(str)

    """
    모델별 측정/공정 설정
    - model_master: Max A, PBS 측정 포인트, VBG/Fiber 측정 포인트, Burn-In current, 초기 빔 확인 전류
    - model_process_flow: 모델별 공정순서
    """

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._loading = False
        self._build_ui()
        self._ensure_defaults_silent()
        self.refresh_models()

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("모델별 설정")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        root.addWidget(title)
        root.addWidget(QLabel("※ 모델별 Max A, PBS 측정 포인트, VBG/Fiber 측정 포인트, 번인 전류, 초기 빔 확인 전류, 공정순서를 관리합니다."))

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)

        btn_row = QHBoxLayout()
        self.btn_refresh = QPushButton("새로고침")
        self.btn_add_defaults = QPushButton("기본 모델 생성/복구")
        btn_row.addWidget(self.btn_refresh)
        btn_row.addWidget(self.btn_add_defaults)
        btn_row.addStretch(1)
        left_lay.addLayout(btn_row)

        self.tbl_models = QTableWidget(0, 11)
        self.tbl_models.setHorizontalHeaderLabels([
            "모델코드", "표시명", "Max A", "PBS Points", "VBG/Fiber Points", "Burn-In A",
            "초기 빔 A", "측정대기(s)", "Max대기(s)", "사용", "비고"
        ])
        hdr = self.tbl_models.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(10, QHeaderView.Stretch)
        self.tbl_models.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_models.setSelectionMode(QTableWidget.SingleSelection)
        left_lay.addWidget(self.tbl_models, 1)

        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)

        g1 = QGroupBox("모델 정보")
        f1 = QFormLayout(g1)
        self.ed_model_code = QLineEdit()
        self.ed_display_name = QLineEdit()
        self.sp_max_a = QDoubleSpinBox()
        self.sp_max_a.setRange(0.1, 500.0)
        self.sp_max_a.setDecimals(2)
        self.sp_max_a.setSingleStep(1.0)
        self.ed_pbs_points = QLineEdit()
        self.ed_pbs_points.setPlaceholderText("예: 1,2,3,4,5,6,7,8,9,10")
        self.ed_vbg_points = QLineEdit()
        self.ed_vbg_points.setPlaceholderText("예: 2,5,10,15,16,17,20")
        self.sp_burnin_a = QDoubleSpinBox()
        self.sp_burnin_a.setRange(0.1, 500.0)
        self.sp_burnin_a.setDecimals(2)
        self.sp_burnin_a.setSingleStep(1.0)

        self.sp_beam_check_a = QDoubleSpinBox()
        self.sp_beam_check_a.setRange(0.1, 500.0)
        self.sp_beam_check_a.setDecimals(2)
        self.sp_beam_check_a.setSingleStep(0.1)
        self.sp_beam_check_a.setValue(0.8)

        self.sp_measure_wait_s = QDoubleSpinBox()
        self.sp_measure_wait_s.setRange(0.0, 3600.0)
        self.sp_measure_wait_s.setDecimals(1)
        self.sp_measure_wait_s.setSingleStep(1.0)
        self.sp_measure_wait_s.setValue(10.0)

        self.sp_max_wait_s = QDoubleSpinBox()
        self.sp_max_wait_s.setRange(0.0, 3600.0)
        self.sp_max_wait_s.setDecimals(1)
        self.sp_max_wait_s.setSingleStep(1.0)
        self.sp_max_wait_s.setValue(15.0)

        self.chk_active = QCheckBox("사용")
        self.chk_active.setChecked(True)
        self.ed_note = QLineEdit()

        f1.addRow("모델코드", self.ed_model_code)
        f1.addRow("표시명", self.ed_display_name)
        f1.addRow("Max Current (A)", self.sp_max_a)
        f1.addRow("PBS 측정 포인트(A)", self.ed_pbs_points)
        f1.addRow("VBG/Fiber 측정 포인트(A)", self.ed_vbg_points)
        f1.addRow("Burn-In Current (A)", self.sp_burnin_a)
        f1.addRow("초기 빔 확인 전류(A)", self.sp_beam_check_a)
        f1.addRow("측정 지점 대기시간(s)", self.sp_measure_wait_s)
        f1.addRow("Max A 대기시간(s)", self.sp_max_wait_s)
        f1.addRow("상태", self.chk_active)
        f1.addRow("비고", self.ed_note)
        right_lay.addWidget(g1)

        g2 = QGroupBox("공정순서")
        g2_lay = QVBoxLayout(g2)
        self.ed_flow = QPlainTextEdit()
        self.ed_flow.setPlaceholderText("공정명을 한 줄에 하나씩 입력하세요. 위에서 아래 순서가 진행현황 기준입니다.")
        g2_lay.addWidget(self.ed_flow, 1)
        right_lay.addWidget(g2, 1)

        save_row = QHBoxLayout()
        self.btn_new = QPushButton("신규 입력")
        self.btn_save = QPushButton("저장")
        self.btn_load_default_flow = QPushButton("기본 공정순서 넣기")
        save_row.addWidget(self.btn_new)
        save_row.addWidget(self.btn_save)
        save_row.addWidget(self.btn_load_default_flow)
        save_row.addStretch(1)
        right_lay.addLayout(save_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.btn_refresh.clicked.connect(self.refresh_models)
        self.btn_add_defaults.clicked.connect(self._on_restore_defaults)
        self.btn_new.clicked.connect(self._clear_form)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_load_default_flow.clicked.connect(self._load_default_flow_to_editor)
        self.tbl_models.itemSelectionChanged.connect(self._on_model_selected)

    def _ensure_defaults_silent(self, overwrite_existing: bool = False):
        """
        기본 모델을 보장한다.

        overwrite_existing=False:
            프로그램 시작 시 사용. DB에 없는 모델만 생성해서
            사용자가 저장한 모델별 값을 절대 덮어쓰지 않는다.

        overwrite_existing=True:
            사용자가 '기본 모델 생성/복구' 버튼을 직접 눌렀을 때만
            코드의 기본 Recipe 값으로 복구한다.
        """
        if not hasattr(self.repo, "upsert_model"):
            return
        try:
            existing_codes = set()
            if hasattr(self.repo, "list_models"):
                try:
                    existing_codes = {
                        normalize_model_code(x.get("model_code"))
                        for x in self.repo.list_models(include_inactive=True)
                        if x.get("model_code")
                    }
                except Exception:
                    existing_codes = set()

            for code, r in MODEL_RECIPES.items():
                code_n = normalize_model_code(code)

                if overwrite_existing or code_n not in existing_codes:
                    self.repo.upsert_model(
                        model_code=code_n,
                        display_name=r.display_name,
                        max_current_a=r.max_current_a,
                        pbs_step_a=r.pbs_step_a,
                        pbs_points_csv=",".join(_num_text(x) for x in r.pbs_points),
                        vbg_points_csv=",".join(_num_text(x) for x in r.vbg_points),
                        burnin_current_a=r.burnin_current_a,
                        beam_check_current_a=getattr(r, "beam_check_current_a", 0.8),
                        measure_step_wait_s=getattr(r, "measure_step_wait_s", 10.0),
                        max_hold_wait_s=getattr(r, "max_hold_wait_s", 15.0),
                        active=True,
                        note="default",
                    )

                if hasattr(self.repo, "get_model_process_flow") and hasattr(self.repo, "replace_model_process_flow"):
                    flow = self.repo.get_model_process_flow(code_n)
                    if not flow:
                        self.repo.replace_model_process_flow(code_n, r.process_flow)
        except Exception:
            pass

    def _on_restore_defaults(self):
        try:
            self._ensure_defaults_silent(overwrite_existing=True)
            self.refresh_models()
            QMessageBox.information(self, "완료", "기본 모델/공정순서를 생성 또는 복구했습니다.")
        except Exception as e:
            QMessageBox.critical(self, "실패", str(e))

    def refresh_models(self):
        self._loading = True
        try:
            if not hasattr(self.repo, "list_models"):
                QMessageBox.warning(self, "DB 함수 없음", "repo.list_models()가 없습니다. repo.py 수정본이 적용됐는지 확인하세요.")
                return
            rows = self.repo.list_models(include_inactive=True)
            self.tbl_models.setRowCount(0)
            for r in rows:
                i = self.tbl_models.rowCount()
                self.tbl_models.insertRow(i)
                self.tbl_models.setItem(i, 0, _ro_item(r.get("model_code")))
                self.tbl_models.setItem(i, 1, _ro_item(r.get("display_name")))
                self.tbl_models.setItem(i, 2, _ro_item(_num_text(r.get("max_current_a"))))
                self.tbl_models.setItem(i, 3, _ro_item(r.get("pbs_points_csv") or ""))
                self.tbl_models.setItem(i, 4, _ro_item(r.get("vbg_points_csv")))
                self.tbl_models.setItem(i, 5, _ro_item(_num_text(r.get("burnin_current_a"))))
                self.tbl_models.setItem(i, 6, _ro_item(_num_text(r.get("beam_check_current_a") if r.get("beam_check_current_a") is not None else 0.8)))
                self.tbl_models.setItem(i, 7, _ro_item(_num_text(r.get("measure_step_wait_s") if r.get("measure_step_wait_s") is not None else 10)))
                self.tbl_models.setItem(i, 8, _ro_item(_num_text(r.get("max_hold_wait_s") if r.get("max_hold_wait_s") is not None else 15)))
                self.tbl_models.setItem(i, 9, _ro_item("사용" if int(r.get("active") or 0) == 1 else "미사용"))
                self.tbl_models.setItem(i, 10, _ro_item(r.get("note") or ""))
            if self.tbl_models.rowCount() > 0 and self.tbl_models.currentRow() < 0:
                self.tbl_models.setCurrentCell(0, 0)
        except Exception as e:
            QMessageBox.critical(self, "모델 로드 실패", str(e))
        finally:
            self._loading = False

    def _row_model_code(self, row: int) -> str:
        if row < 0:
            return ""
        it = self.tbl_models.item(row, 0)
        return it.text().strip() if it else ""

    def _on_model_selected(self):
        if self._loading:
            return
        code = self._row_model_code(self.tbl_models.currentRow())
        if not code:
            return
        try:
            r = self.repo.get_model(code)
            self.ed_model_code.setText(str(r.get("model_code") or code))
            self.ed_display_name.setText(str(r.get("display_name") or code))
            self.sp_max_a.setValue(float(r.get("max_current_a") or 20.0))
            self.ed_pbs_points.setText(str(r.get("pbs_points_csv") or ""))
            self.ed_vbg_points.setText(str(r.get("vbg_points_csv") or ""))
            self.sp_burnin_a.setValue(float(r.get("burnin_current_a") or r.get("max_current_a") or 20.0))
            self.sp_beam_check_a.setValue(float(r.get("beam_check_current_a") if r.get("beam_check_current_a") is not None else 0.8))
            self.sp_measure_wait_s.setValue(float(r.get("measure_step_wait_s") if r.get("measure_step_wait_s") is not None else 10.0))
            self.sp_max_wait_s.setValue(float(r.get("max_hold_wait_s") if r.get("max_hold_wait_s") is not None else 15.0))
            self.chk_active.setChecked(int(r.get("active") or 0) == 1)
            self.ed_note.setText(str(r.get("note") or ""))

            flow_rows = []
            if hasattr(self.repo, "get_model_process_flow"):
                flow_rows = self.repo.get_model_process_flow(code)
            if flow_rows:
                lines = [str(x.get("process_name_raw") or x.get("process_name") or "").strip() for x in flow_rows]
            else:
                lines = get_process_flow(code)
            self.ed_flow.setPlainText("\n".join([x for x in lines if x]))
        except Exception as e:
            QMessageBox.critical(self, "모델 선택 실패", str(e))

    def _clear_form(self):
        self.ed_model_code.clear()
        self.ed_display_name.clear()
        self.sp_max_a.setValue(20.0)
        self.ed_pbs_points.setText(",".join(str(x) for x in range(1, 21)))
        self.ed_vbg_points.setText("2,5,10,15,16,17,20")
        self.sp_burnin_a.setValue(20.0)
        self.sp_beam_check_a.setValue(0.8)
        self.sp_measure_wait_s.setValue(10.0)
        self.sp_max_wait_s.setValue(15.0)
        self.chk_active.setChecked(True)
        self.ed_note.clear()
        self._load_default_flow_to_editor()

    def _load_default_flow_to_editor(self):
        self.ed_flow.setPlainText("\n".join(get_process_flow(DEFAULT_MODEL_CODE)))

    def _parse_flow_lines(self) -> List[str]:
        text = self.ed_flow.toPlainText() or ""
        out: List[str] = []
        seen = set()
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            n = normalize_process_name(s)
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def _on_save(self):
        try:
            if not hasattr(self.repo, "upsert_model"):
                raise RuntimeError("repo.upsert_model()가 없습니다. repo.py 수정본 적용이 필요합니다.")

            code = normalize_model_code(self.ed_model_code.text())
            if not code:
                raise ValueError("모델코드를 입력하세요.")
            max_a = float(self.sp_max_a.value())
            burnin_a = float(self.sp_burnin_a.value())
            pbs_csv = (self.ed_pbs_points.text() or "").strip()
            vbg_csv = (self.ed_vbg_points.text() or "").strip()
            if not pbs_csv:
                pbs_csv = ",".join(str(x) for x in range(1, int(round(max_a)) + 1))
            if not vbg_csv:
                vbg_csv = _num_text(max_a)

            self.repo.upsert_model(
                model_code=code,
                display_name=(self.ed_display_name.text() or code).strip(),
                max_current_a=max_a,
                pbs_step_a=1.0,  # 완성품 측정은 기존 1A 간격 유지
                pbs_points_csv=pbs_csv,
                vbg_points_csv=vbg_csv,
                burnin_current_a=burnin_a,
                beam_check_current_a=float(self.sp_beam_check_a.value()),
                measure_step_wait_s=float(self.sp_measure_wait_s.value()),
                max_hold_wait_s=float(self.sp_max_wait_s.value()),
                active=self.chk_active.isChecked(),
                note=(self.ed_note.text() or "").strip(),
            )

            flow = self._parse_flow_lines()
            if flow and hasattr(self.repo, "replace_model_process_flow"):
                self.repo.replace_model_process_flow(code, flow)

            self.refresh_models()
            # 저장한 모델 다시 선택
            for r in range(self.tbl_models.rowCount()):
                if self._row_model_code(r) == code:
                    self.tbl_models.setCurrentCell(r, 0)
                    break
            # 저장 직후 공정관리 탭이 기존 공정기록을 현재 공정순서 기준으로 재계산하도록 알림
            self.model_process_flow_changed.emit(code)

            QMessageBox.information(
                self,
                "저장 완료",
                f"{code} 모델 설정을 저장했습니다.\n"
                f"기존 공정기록의 진행현황/다음공정/대기목록도 현재 공정순서 기준으로 재계산됩니다."
            )
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", str(e))
