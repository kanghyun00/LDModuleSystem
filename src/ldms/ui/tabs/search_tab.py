# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import pandas as pd

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QComboBox,
    QStackedWidget, QAbstractItemView
)

from src.ldms.app_config import load_path_settings, normalize_and_fill_defaults
from src.ldms.services.final_20a_export_service import export_finaldata_20a_excel
from src.ldms.process_flow import DEFAULT_MODEL_CODE, list_model_codes, normalize_model_code


PROJECT_ROOT = Path(__file__).resolve().parents[4]
RAW_ROOT = PROJECT_ROOT / "data" / "measurements" / "raw"
ALLOWED_EXTS = {".xlsx", ".xlsm", ".xls", ".csv"}

MOD_PAT = re.compile(r"([A-Za-z]{1,4}\d{1,4})")

SUMMARY_COLS = [
    "Current", "Peak1", "Peak2", "FWHM(nm)", "SMSR", "Power", "Voltage",
    "Lid(CH5)", "Base(CH3)", "Fiber(CH2)", "효율"
]


def _item(text) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    it.setFlags(it.flags() ^ Qt.ItemIsEditable)
    return it


def _display_file_name(stage: str, file_name: str) -> str:
    stage = (stage or "").strip()
    file_name = (file_name or "").strip()
    prefix = f"{stage}__"
    if stage and file_name.startswith(prefix):
        return file_name[len(prefix):]
    return file_name


def _is_junk_file(p: Path) -> bool:
    name = p.name
    if name.startswith("~$"):
        return True
    if name.startswith("."):
        return True
    return False


def _extract_module_no_from_filename(filename: str) -> Optional[str]:
    if not filename:
        return None
    stem = Path(filename).stem
    m = MOD_PAT.search(stem)
    if not m:
        return None
    return m.group(1).upper()


def _pretty_title_from_filename(filename: str) -> str:
    if not filename:
        return ""
    stem = Path(filename).stem
    m = MOD_PAT.search(stem)
    if not m:
        return stem

    left = stem[:m.start()]
    right = stem[m.end():]
    txt = (left + " " + right).strip()
    txt = re.sub(r"[_\-]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt if txt else stem


def _list_subdirs(p: Path) -> List[Path]:
    if not p.exists() or not p.is_dir():
        return []
    out = [x for x in p.iterdir() if x.is_dir()]
    out.sort(key=lambda x: x.name.lower())
    return out


def _iter_measure_files(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    files: List[Path] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if _is_junk_file(p):
            continue
        if p.suffix.lower() not in ALLOWED_EXTS:
            continue
        files.append(p)
    files.sort(key=lambda x: x.name.lower())
    return files


def _build_module_index(folder: Path) -> Dict[str, List[Path]]:
    idx: Dict[str, List[Path]] = defaultdict(list)
    for f in _iter_measure_files(folder):
        mod = _extract_module_no_from_filename(f.name)
        if not mod:
            continue
        idx[mod].append(f)
    for k in list(idx.keys()):
        idx[k].sort(key=lambda x: x.name.lower())
    return dict(idx)



def _looks_like_model_name(name: str) -> bool:
    return re.fullmatch(r"F\d{3,4}\d{1,4}W", (name or "").strip(), flags=re.IGNORECASE) is not None


def _merge_module_index(dst: Dict[str, List[Path]], src: Dict[str, List[Path]]) -> Dict[str, List[Path]]:
    for mod, files in (src or {}).items():
        dst.setdefault(mod, [])
        dst[mod].extend(files)

    for k in list(dst.keys()):
        unique = {}
        for f in dst[k]:
            try:
                unique[str(f.resolve())] = f
            except Exception:
                unique[str(f)] = f
        dst[k] = sorted(unique.values(), key=lambda p: str(p).lower())

    return dst


def _build_legacy_stage_index(stage_dir: Path, group: str = "(전체)") -> Dict[str, List[Path]]:
    """
    기존 구조 호환:
    raw/stage/group/file.xlsx 또는 raw/stage/file.xlsx

    새 구조 raw/stage/F976370W/... 같은 모델 폴더는 legacy 스캔에서 제외한다.
    """
    idx: Dict[str, List[Path]] = defaultdict(list)

    if not stage_dir.exists() or not stage_dir.is_dir():
        return {}

    def add_file(f: Path):
        if _is_junk_file(f):
            return
        if f.suffix.lower() not in ALLOWED_EXTS:
            return
        mod = _extract_module_no_from_filename(f.name)
        if mod:
            idx[mod].append(f)

    if group and group != "(전체)":
        gp = stage_dir / group
        if gp.exists() and gp.is_dir() and not _looks_like_model_name(gp.name):
            return _build_module_index(gp)
        return {}

    for child in stage_dir.iterdir():
        if child.is_file():
            add_file(child)
        elif child.is_dir():
            if _looks_like_model_name(child.name):
                continue
            _merge_module_index(idx, _build_module_index(child))

    return dict(_merge_module_index(idx, {}))


def _module_sort_key(mod: str) -> Tuple[str, int, str]:
    mod = (mod or "").strip().upper()
    m = re.match(r"^([A-Z]+)(\d+)$", mod)
    if not m:
        return ("", 0, mod)
    alpha = m.group(1)
    num = int(m.group(2))
    return (alpha, num, mod)


def _norm(s: str) -> str:
    t = str(s).strip().lower()
    t = t.replace("\n", " ").replace("\r", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("㎚", "nm")
    return t


def _canon_col(name: str) -> Optional[str]:
    n = _norm(name)

    if n in ("current", "current(a)", "i", "i(a)", "current (a)"):
        return "Current"

    if "peak1" in n or "peak 1" in n:
        return "Peak1"
    if "peak2" in n or "peak 2" in n:
        return "Peak2"

    if "fwhm" in n or "반치폭" in n:
        return "FWHM(nm)"

    if "smsr" in n or "dbm diff" in n or "dbm_diff" in n or "dbmdiff" in n:
        return "SMSR"

    if n.startswith("power") or n in ("p", "power(w)", "power (w)", "power(watt)"):
        return "Power"

    if n.startswith("voltage") or n in ("v", "voltage(v)", "voltage (v)"):
        return "Voltage"

    nn = n.replace(" ", "")
    if nn in ("lid(ch5)", "lidch5", "ch5lid", "lid_temp(ch5)"):
        return "Lid(CH5)"
    if nn in ("base(ch5)", "basech5", "ch5", "온도ch5"):
        return "Lid(CH5)"

    if nn in ("base(ch3)", "basech3", "ch3", "base_temp(ch3)"):
        return "Base(CH3)"
    if nn in ("pkg(ch3)", "pkgch3", "pkg", "온도ch3"):
        return "Base(CH3)"

    if nn in ("온도", "온도(°c)", "온도(℃)", "temperature", "temp", "temp(°c)", "temp(℃)"):
        return "Base(CH3)"

    if nn in ("fiber(ch2)", "fiberch2", "ch2"):
        return "Fiber(CH2)"

    if nn in ("eff", "efficiency", "효율"):
        return "효율"

    return None


def _to_points_from_df(df: pd.DataFrame) -> List[dict]:
    if df is None or df.empty:
        return []

    rename_map: Dict[str, str] = {}
    for c in df.columns:
        cc = _canon_col(c)
        if cc:
            rename_map[c] = cc

    if not rename_map:
        return []

    sdf = df.rename(columns=rename_map).copy()
    if "Current" not in sdf.columns:
        return []

    sdf = sdf.dropna(how="all")

    pts: List[dict] = []
    for _, r in sdf.iterrows():
        cur = r.get("Current", None)
        if pd.isna(cur):
            continue

        row = {k: "" for k in SUMMARY_COLS}
        for k in SUMMARY_COLS:
            if k in sdf.columns:
                row[k] = r.get(k, "")
        pts.append(row)

    return pts


def _try_parse_from_raw_matrix(raw: pd.DataFrame) -> List[dict]:
    if raw is None or raw.empty:
        return []

    max_scan = min(len(raw), 300)
    for i in range(max_scan):
        row = raw.iloc[i].tolist()
        normed = [_norm(x) for x in row]
        if any(x in ("current", "current(a)", "i", "i(a)", "current (a)") for x in normed):
            header = raw.iloc[i].tolist()
            data = raw.iloc[i + 1:].copy()
            data.columns = header
            data = data.loc[:, [c for c in data.columns if str(c).strip() != ""]]
            return _to_points_from_df(data)
    return []


def read_summary_from_file(file_path: Path) -> List[dict]:
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    suf = file_path.suffix.lower()

    if suf == ".csv":
        df = pd.read_csv(file_path, engine="python")
        pts = _to_points_from_df(df)
        if pts:
            return pts
        raise ValueError("CSV에서 Summary 표를 찾지 못했습니다.")

    xls = pd.ExcelFile(file_path)
    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet, header=0)
            pts = _to_points_from_df(df)
            if pts:
                return pts
        except Exception:
            pass

        try:
            raw = pd.read_excel(xls, sheet_name=sheet, header=None)
            pts = _try_parse_from_raw_matrix(raw)
            if pts:
                return pts
        except Exception:
            pass

    raise ValueError("Summary 표를 찾지 못했습니다. (Current/Peak/FWHM/SMSR/온도 등의 헤더가 있는 표 필요)")


class SearchTab(QWidget):
    VIEW_STAGE = 0
    VIEW_MODULE = 1

    def __init__(self, repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._building = False

        self.raw_root = self._resolve_raw_root()
        self._folder_index: Dict[str, List[Path]] = {}
        self._current_folder: Optional[Path] = None

        self._raw_override_file: Optional[Path] = None
        self._raw_override_module: Optional[str] = None
        self._raw_override_points: Optional[List[dict]] = None
        self._suppress_db_summary_once = False

        self._build_ui()
        self.reload_stage_group()

    def _resolve_raw_root(self) -> Path:
        """
        검색/비교 RAW 루트.
        1순위: Parameter > 검색/비교 RAW 폴더(search_compare_raw_root)
        2순위: Parameter > 측정 저장 폴더(export_dir_measure)
        3순위: 현재 프로젝트/data/measurements/raw
        """
        try:
            ps = normalize_and_fill_defaults(load_path_settings(self.repo))

            p1 = (getattr(ps, "search_compare_raw_root", "") or "").strip()
            if p1:
                return Path(p1)

            p2 = (getattr(ps, "export_dir_measure", "") or "").strip()
            if p2:
                return Path(p2)
        except Exception:
            pass

        return Path(RAW_ROOT)

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

    def _infer_model_from_files(self, files: List[Path]) -> str:
        """
        파일명/경로에서 F976370W 같은 모델명이 보이면 우선 사용.
        """
        joined = " ".join(str(f) for f in (files or []))
        m = re.search(r"(F\d{3,4}\d{1,4}W)", joined, flags=re.IGNORECASE)
        if m:
            return normalize_model_code(m.group(1))
        return ""

    def _module_model(self, module_no: str, files: Optional[List[Path]] = None) -> str:
        """
        모듈의 모델 판정.
        1) 파일명/경로에 모델명이 있으면 우선
        2) DB modules.model 조회
        3) 없으면 기존 데이터 호환상 F976370W
        """
        inferred = self._infer_model_from_files(files or [])
        if inferred:
            return inferred

        try:
            if hasattr(self.repo, "get_module_model"):
                m = normalize_model_code(self.repo.get_module_model(module_no))
                if m:
                    return m
        except Exception:
            pass

        return DEFAULT_MODEL_CODE

    def _module_matches_model(self, module_no: str, files: Optional[List[Path]] = None) -> bool:
        mf = self._current_model_filter()
        if not mf:
            return True
        return self._module_model(module_no, files or []) == mf

    def _on_model_filter_changed(self):
        try:
            self.on_stage_changed()
            self.apply_sort_only()
            if self.stack.currentIndex() == self.VIEW_MODULE and self.module_edit.text().strip():
                self.on_search()
        except Exception:
            pass

    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.btn_view_stage = QPushButton("Stage/Group 목록")
        self.btn_view_module = QPushButton("모듈 조회")
        top.addWidget(self.btn_view_stage)
        top.addWidget(self.btn_view_module)
        top.addSpacing(16)
        top.addWidget(QLabel("모델"))
        self.cmb_model = QComboBox()
        self.cmb_model.setMinimumWidth(150)
        self.cmb_model.addItem("(전체)")
        self.cmb_model.addItems(self._load_model_codes())
        idx_default = self.cmb_model.findText(DEFAULT_MODEL_CODE)
        if idx_default >= 0:
            self.cmb_model.setCurrentIndex(idx_default)
        top.addWidget(self.cmb_model)
        top.addStretch(1)
        root.addLayout(top)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.btn_view_stage.clicked.connect(lambda: self.stack.setCurrentIndex(self.VIEW_STAGE))
        self.btn_view_module.clicked.connect(lambda: self.stack.setCurrentIndex(self.VIEW_MODULE))
        self.cmb_model.currentIndexChanged.connect(self._on_model_filter_changed)

        # ------------------ Stage/Group page ------------------
        page_stage = QWidget()
        lay = QVBoxLayout(page_stage)

        row = QHBoxLayout()
        row.addWidget(QLabel("Stage"))
        self.cmb_stage = QComboBox()
        self.cmb_stage.setMinimumWidth(280)
        row.addWidget(self.cmb_stage)

        row.addWidget(QLabel("Group"))
        self.cmb_group = QComboBox()
        self.cmb_group.setMinimumWidth(260)
        row.addWidget(self.cmb_group)

        row.addWidget(QLabel("정렬"))
        self.cmb_sort = QComboBox()
        self.cmb_sort.addItems(["모듈번호 오름차순", "모듈번호 내림차순"])
        row.addWidget(self.cmb_sort)

        self.btn_apply_sort = QPushButton("정렬 적용")
        row.addWidget(self.btn_apply_sort)

        self.btn_reload_sg = QPushButton("새로고침")
        self.btn_scan_folder = QPushButton("폴더 스캔(모듈목록)")
        self.btn_export_final20a = QPushButton("모델별 엑셀 저장(등급/색)")

        row.addWidget(self.btn_reload_sg)
        row.addWidget(self.btn_scan_folder)
        row.addWidget(self.btn_export_final20a)
        row.addStretch(1)
        lay.addLayout(row)

        self.lbl_sg_info = QLabel("")
        lay.addWidget(self.lbl_sg_info)

        split = QHBoxLayout()
        lay.addLayout(split)

        self.module_list_table = QTableWidget(0, 3)
        self.module_list_table.setHorizontalHeaderLabels(["모듈번호", "모델", "파일들(요약)"])
        self.module_list_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.module_list_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.module_list_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.module_list_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.module_list_table.setSelectionMode(QTableWidget.SingleSelection)
        self.module_list_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        split.addWidget(self.module_list_table, 3)

        right = QVBoxLayout()
        split.addLayout(right, 2)

        right.addWidget(QLabel("선택 모듈의 파일 목록 (더블클릭: 모듈조회 + Summary도 해당 RAW 파일 기준)"))
        self.file_list_table = QTableWidget(0, 3)
        self.file_list_table.setHorizontalHeaderLabels(["파일명", "제목", "상대경로"])
        self.file_list_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.file_list_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_list_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.file_list_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.file_list_table.setSelectionMode(QTableWidget.SingleSelection)
        self.file_list_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        right.addWidget(self.file_list_table)

        self.lbl_preview = QLabel("선택 파일 Summary(미리보기)")
        right.addWidget(self.lbl_preview)

        self.preview_table = QTableWidget(0, len(SUMMARY_COLS))
        self.preview_table.setHorizontalHeaderLabels(SUMMARY_COLS)
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        right.addWidget(self.preview_table)

        self.btn_open_selected = QPushButton("선택 파일의 모듈 조회로 이동")
        right.addWidget(self.btn_open_selected)

        self.btn_reload_sg.clicked.connect(self.reload_stage_group)
        self.cmb_stage.currentIndexChanged.connect(self.on_stage_changed)
        self.btn_scan_folder.clicked.connect(self.scan_selected_stage_group)
        self.btn_apply_sort.clicked.connect(self.apply_sort_only)
        self.btn_export_final20a.clicked.connect(self.export_final_20a_excel)

        self.module_list_table.itemSelectionChanged.connect(self.on_select_module_in_folder)
        self.file_list_table.itemSelectionChanged.connect(self.on_select_file_for_preview)
        self.file_list_table.itemDoubleClicked.connect(self.on_doubleclick_file)
        self.btn_open_selected.clicked.connect(self.open_selected_file_module)

        self.stack.addWidget(page_stage)

        # ------------------ Module page ------------------
        page_module = QWidget()
        laym = QVBoxLayout(page_module)

        rowm = QHBoxLayout()
        rowm.addWidget(QLabel("모듈번호"))
        self.module_edit = QLineEdit()
        rowm.addWidget(self.module_edit, 2)
        self.lbl_module_model = QLabel("모델: -")
        self.lbl_module_model.setStyleSheet("color:#666;")
        rowm.addWidget(self.lbl_module_model)
        self.btn_search = QPushButton("조회")
        rowm.addWidget(self.btn_search)
        rowm.addStretch(1)
        laym.addLayout(rowm)

        self.btn_search.clicked.connect(self.on_search)
        self.module_edit.returnPressed.connect(self.on_search)

        laym.addWidget(QLabel("공정 이력"))
        self.proc_table = QTableWidget(0, 7)
        self.proc_table.setHorizontalHeaderLabels(["날짜", "공정(정규)", "공정(원문)", "수량", "작업자", "비고", "ID"])
        self.proc_table.setColumnHidden(6, True)
        self.proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.proc_table.horizontalHeader().setStretchLastSection(True)
        laym.addWidget(self.proc_table)

        laym.addWidget(QLabel("측정 엑셀 이력(Import)"))
        self.meas_file_table = QTableWidget(0, 6)
        self.meas_file_table.setHorizontalHeaderLabels(["import일시", "단계", "파일명", "모듈번호", "파일경로", "ID"])
        self.meas_file_table.setColumnHidden(4, True)
        self.meas_file_table.setColumnHidden(5, True)

        hdr = self.meas_file_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(True)

        self.meas_file_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.meas_file_table.setSelectionMode(QTableWidget.SingleSelection)
        laym.addWidget(self.meas_file_table)

        self.lbl_summary_detail = QLabel("Summary 상세(선택된 1개)")
        laym.addWidget(self.lbl_summary_detail)

        self.summary_table = QTableWidget(0, len(SUMMARY_COLS))
        self.summary_table.setHorizontalHeaderLabels(SUMMARY_COLS)
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.summary_table.horizontalHeader().setStretchLastSection(True)
        laym.addWidget(self.summary_table)

        self.meas_file_table.itemSelectionChanged.connect(self.on_select_measurement_file)
        self.stack.addWidget(page_module)

        self.stack.setCurrentIndex(self.VIEW_STAGE)

    def reload_stage_group(self):
        try:
            self._building = True
            self.cmb_stage.blockSignals(True)
            self.cmb_group.blockSignals(True)

            self.raw_root = self._resolve_raw_root()

            self.cmb_stage.clear()
            self.cmb_group.clear()
            self.module_list_table.setRowCount(0)
            self.file_list_table.setRowCount(0)
            self.preview_table.setRowCount(0)
            self._folder_index = {}
            self._current_folder = None

            if not self.raw_root.exists() or not self.raw_root.is_dir():
                self.lbl_sg_info.setText(
                    f"RAW_ROOT 폴더가 없습니다: {self.raw_root}\n"
                    f"→ Parameter 탭에서 '검색/비교 RAW 폴더(Stage/Group 스캔)' 또는 '측정 저장 폴더'를 설정하세요."
                )
                return

            stages = _list_subdirs(self.raw_root)
            if not stages:
                self.lbl_sg_info.setText("RAW_ROOT 아래 Stage 폴더가 없습니다.")
                return

            self.cmb_stage.addItems([p.name for p in stages])
            self.lbl_sg_info.setText(f"RAW Stage {len(stages)}개 로드됨. (root={self.raw_root})")
        except Exception as e:
            QMessageBox.critical(self, "Stage 로드 실패", str(e))
        finally:
            self.cmb_stage.blockSignals(False)
            self.cmb_group.blockSignals(False)
            self._building = False
            if self.cmb_stage.count() > 0:
                self.on_stage_changed()

    def on_stage_changed(self):
        if self._building:
            return
        try:
            stage = (self.cmb_stage.currentText() or "").strip()
            if not stage:
                return

            self._building = True
            self.cmb_group.blockSignals(True)
            self.cmb_group.clear()

            stage_dir = self.raw_root / stage
            model = self._current_model_filter()

            self.cmb_group.addItem("(전체)")

            if model:
                # 새 구조: raw/stage/model/group
                # 기존 370W 자료: raw/stage/group 도 함께 표시
                names = set()

                group_root = stage_dir / model
                for g in _list_subdirs(group_root):
                    names.add(g.name)

                if model == DEFAULT_MODEL_CODE:
                    for g in _list_subdirs(stage_dir):
                        if not _looks_like_model_name(g.name):
                            names.add(g.name)

                for name in sorted(names):
                    self.cmb_group.addItem(name)

                self.lbl_sg_info.setText(
                    f"Stage: {stage} / Model: {model} / Group {self.cmb_group.count()}개(전체 포함)"
                )
            else:
                # 전체 모델: raw/stage 아래 모델 폴더 group + 기존 legacy group 이름을 합쳐서 표시
                names = set()

                for child in _list_subdirs(stage_dir):
                    if _looks_like_model_name(child.name):
                        for g in _list_subdirs(child):
                            names.add(g.name)
                    else:
                        names.add(child.name)

                for name in sorted(names):
                    self.cmb_group.addItem(name)

                self.lbl_sg_info.setText(
                    f"Stage: {stage} / Model: 전체 / Group {self.cmb_group.count()}개(전체 포함)"
                )
        except Exception as e:
            QMessageBox.critical(self, "Group 로드 실패", str(e))
        finally:
            self.cmb_group.blockSignals(False)
            self._building = False

    def _selected_folder(self) -> Path:
        stage = (self.cmb_stage.currentText() or "").strip()
        group = (self.cmb_group.currentText() or "").strip()
        model = self._current_model_filter()

        p = self.raw_root / stage if stage else self.raw_root

        # 새 구조: raw/stage/model/group
        if model:
            p = p / model
            if group and group != "(전체)":
                p = p / group
            return p

        # 전체 모델: group이 전체면 stage 전체, 특정 group이면 scan_selected_stage_group에서 별도 처리
        if group and group != "(전체)":
            # 여기서는 stage를 반환하고 scan에서 group 필터링
            return p
        return p

    def _current_sort_desc(self) -> bool:
        return self.cmb_sort.currentText().endswith("내림차순")

    def scan_selected_stage_group(self):
        folder = self._selected_folder()
        if not folder.exists() or not folder.is_dir():
            QMessageBox.information(self, "안내", f"선택 폴더가 존재하지 않습니다.\n{folder}")
            return

        model = self._current_model_filter()
        group = (self.cmb_group.currentText() or "").strip()
        stage = (self.cmb_stage.currentText() or "").strip()
        stage_dir = self.raw_root / stage if stage else self.raw_root

        idx: Dict[str, List[Path]] = {}

        if model:
            # 새 구조 우선: raw/stage/model[/group]
            new_base = stage_dir / model
            if group and group != "(전체)":
                new_base = new_base / group
            if new_base.exists() and new_base.is_dir():
                _merge_module_index(idx, _build_module_index(new_base))

            # 기존 370W 구조 호환: raw/stage[/group]
            if model == DEFAULT_MODEL_CODE:
                _merge_module_index(idx, _build_legacy_stage_index(stage_dir, group or "(전체)"))

            self._folder_index = idx
            self._current_folder = stage_dir

        else:
            if group and group != "(전체)":
                # 전체 모델 + 특정 group:
                # 1) raw/stage/*model/group
                # 2) raw/stage/group legacy
                for md in _list_subdirs(stage_dir):
                    if not _looks_like_model_name(md.name):
                        continue
                    gp = md / group
                    if gp.exists() and gp.is_dir():
                        _merge_module_index(idx, _build_module_index(gp))

                _merge_module_index(idx, _build_legacy_stage_index(stage_dir, group))
                self._folder_index = idx
                self._current_folder = stage_dir
            else:
                # 전체 모델 + 전체 group: stage 전체 스캔
                idx = _build_module_index(stage_dir)
                self._folder_index = idx
                self._current_folder = stage_dir

        self.apply_sort_only(scan_message=True)

    def apply_sort_only(self, scan_message: bool = False):
        self.module_list_table.setRowCount(0)
        self.file_list_table.setRowCount(0)
        self.preview_table.setRowCount(0)

        if not self._folder_index:
            if scan_message:
                self.lbl_sg_info.setText(f"선택 경로: {self._selected_folder()} / 모듈번호 추출된 파일 없음")
            return

        desc = self._current_sort_desc()
        all_mods = sorted(self._folder_index.keys(), key=_module_sort_key, reverse=desc)
        mods = [
            m for m in all_mods
            if self._module_matches_model(m, self._folder_index.get(m, []))
        ]

        for mod in mods:
            files = self._folder_index.get(mod, [])
            model = self._module_model(mod, files)
            titles = [_pretty_title_from_filename(f.name) for f in files]
            summary = ", ".join(titles[:4])
            if len(titles) > 4:
                summary += f" ...(+{len(titles) - 4})"

            r = self.module_list_table.rowCount()
            self.module_list_table.insertRow(r)
            self.module_list_table.setItem(r, 0, _item(mod))
            self.module_list_table.setItem(r, 1, _item(model))
            self.module_list_table.setItem(r, 2, _item(summary))

        folder = self._current_folder or self._selected_folder()
        mf = self._current_model_filter() or "전체"
        self.lbl_sg_info.setText(
            f"스캔 완료: {folder} / 모델 {mf} / 모듈 {len(mods)}개"
            f" (전체 {len(all_mods)}개) / 정렬: {'내림차순' if desc else '오름차순'}"
        )

        if self.module_list_table.rowCount() > 0:
            self.module_list_table.selectRow(0)

    def export_final_20a_excel(self):
        stage = (self.cmb_stage.currentText() or "").strip()
        group = (self.cmb_group.currentText() or "").strip()
        model = self._current_model_filter() or DEFAULT_MODEL_CODE

        if not stage:
            QMessageBox.information(self, "엑셀 저장", "Stage를 먼저 선택하세요.")
            return

        try:
            out_path = export_finaldata_20a_excel(self.repo, stage=stage, group=group, model=model)
            QMessageBox.information(self, "엑셀 저장 완료", f"✅ 저장 완료!\n모델: {model}\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "엑셀 저장 실패", str(e))

    def on_select_module_in_folder(self):
        row = self.module_list_table.currentRow()
        if row < 0:
            return
        it = self.module_list_table.item(row, 0)
        if not it:
            return
        mod = (it.text() or "").strip().upper()
        if not mod:
            return

        files = self._folder_index.get(mod, [])
        self.file_list_table.setRowCount(0)
        self.preview_table.setRowCount(0)

        if not files:
            return

        base = self._current_folder or self._selected_folder()
        for f in files:
            r = self.file_list_table.rowCount()
            self.file_list_table.insertRow(r)

            try:
                rel = str(f.relative_to(base))
            except Exception:
                rel = str(f)

            self.file_list_table.setItem(r, 0, _item(f.name))
            self.file_list_table.setItem(r, 1, _item(_pretty_title_from_filename(f.name)))
            self.file_list_table.setItem(r, 2, _item(rel))

        if self.file_list_table.rowCount() > 0:
            self.file_list_table.selectRow(0)

    def _get_selected_file_path(self) -> Optional[Path]:
        row = self.file_list_table.currentRow()
        if row < 0:
            return None
        base = self._current_folder or self._selected_folder()
        rel_item = self.file_list_table.item(row, 2)
        name_item = self.file_list_table.item(row, 0)
        if not name_item:
            return None
        rel = rel_item.text() if rel_item else name_item.text()
        return (base / rel).resolve()

    def _fill_summary_table(self, table: QTableWidget, pts: List[dict]):
        table.setRowCount(0)
        if not pts:
            return
        for p in pts:
            i = table.rowCount()
            table.insertRow(i)
            for c, k in enumerate(SUMMARY_COLS):
                table.setItem(i, c, _item(p.get(k, "")))

    def on_select_file_for_preview(self):
        p = self._get_selected_file_path()
        self.preview_table.setRowCount(0)
        if not p:
            return
        try:
            pts = read_summary_from_file(p)
            self._fill_summary_table(self.preview_table, pts)
            self.lbl_preview.setText(f"선택 파일 Summary(미리보기) - {p.name}")
        except Exception as e:
            self.lbl_preview.setText(f"선택 파일 Summary(미리보기) - {p.name} (실패)")
            QMessageBox.information(self, "Summary 미리보기 실패", f"{p.name}\n\n{e}")

    def on_doubleclick_file(self):
        self.open_selected_file_module()

    def open_selected_file_module(self):
        p = self._get_selected_file_path()
        if not p:
            QMessageBox.information(self, "안내", "파일을 선택하세요.")
            return

        mod = _extract_module_no_from_filename(p.name)
        if not mod:
            QMessageBox.information(self, "안내", "선택 파일명에서 모듈번호를 찾지 못했습니다.")
            return

        try:
            pts = read_summary_from_file(p)
        except Exception as e:
            QMessageBox.information(self, "안내", f"선택 파일 Summary 읽기 실패:\n{p.name}\n\n{e}")
            return

        self._raw_override_file = p
        self._raw_override_module = mod
        self._raw_override_points = pts

        self.stack.setCurrentIndex(self.VIEW_MODULE)
        self.module_edit.setText(mod)
        self.on_search()

    def on_search(self):
        module_no = (self.module_edit.text() or "").strip().upper()
        if not module_no:
            return

        module_model = self._module_model(module_no)
        if hasattr(self, "lbl_module_model"):
            self.lbl_module_model.setText(f"모델: {module_model}")

        mf = self._current_model_filter()
        if mf and module_model != mf:
            QMessageBox.information(
                self,
                "모델 확인",
                f"현재 모델 필터는 [{mf}]인데, 조회 모듈 [{module_no}]의 모델은 [{module_model}]입니다.\n"
                f"조회는 계속 진행합니다."
            )

        procs = self.repo.get_process_logs_by_module(module_no)
        self.proc_table.setRowCount(0)
        for r in procs:
            i = self.proc_table.rowCount()
            self.proc_table.insertRow(i)
            self.proc_table.setItem(i, 0, _item(r.get("work_date", "")))
            self.proc_table.setItem(i, 1, _item(r.get("process_name", "")))
            self.proc_table.setItem(i, 2, _item(r.get("process_name_raw", "")))
            self.proc_table.setItem(i, 3, _item(r.get("qty", "")))
            self.proc_table.setItem(i, 4, _item(r.get("operator", "")))
            self.proc_table.setItem(i, 5, _item(r.get("remark", "")))
            self.proc_table.setItem(i, 6, _item(r.get("id", "")))

        # ✅ 여기서 터졌던 부분: repo에 list_measurement_files가 없었음
        files = self.repo.list_measurement_files(module_no)

        self.meas_file_table.setRowCount(0)
        for r in files:
            i = self.meas_file_table.rowCount()
            self.meas_file_table.insertRow(i)

            stage = r.get("stage", "") or ""
            fname = _display_file_name(stage, r.get("file_name", "") or "")

            self.meas_file_table.setItem(i, 0, _item(r.get("imported_at", "")))
            self.meas_file_table.setItem(i, 1, _item(stage))
            self.meas_file_table.setItem(i, 2, _item(fname))
            self.meas_file_table.setItem(i, 3, _item(r.get("module_no", "")))
            self.meas_file_table.setItem(i, 4, _item(r.get("file_path", "")))
            self.meas_file_table.setItem(i, 5, _item(r.get("id", "")))

        self.summary_table.setRowCount(0)

        if self._raw_override_points and self._raw_override_module == module_no:
            self._suppress_db_summary_once = True
            if self.meas_file_table.rowCount() > 0:
                self.meas_file_table.blockSignals(True)
                self.meas_file_table.selectRow(0)
                self.meas_file_table.blockSignals(False)

            self._fill_summary_table(self.summary_table, self._raw_override_points)
            self.lbl_summary_detail.setText(f"Summary 상세(선택 RAW 파일) - {self._raw_override_file.name}")
            return

        self.lbl_summary_detail.setText("Summary 상세(선택된 1개)")
        if self.meas_file_table.rowCount() > 0:
            self.meas_file_table.selectRow(0)

    def _get_selected_measurement_id_for_summary(self):
        row = self.meas_file_table.currentRow()
        if row < 0:
            return None
        id_item = self.meas_file_table.item(row, 5)
        if not id_item or not id_item.text().strip():
            return None
        return int(id_item.text().strip())

    def on_select_measurement_file(self):
        if self._suppress_db_summary_once:
            self._suppress_db_summary_once = False
            return

        if self._raw_override_points is not None:
            self._raw_override_points = None
            self._raw_override_file = None
            self._raw_override_module = None
            self.lbl_summary_detail.setText("Summary 상세(선택된 1개)")

        mid = self._get_selected_measurement_id_for_summary()
        if mid is None:
            return

        pts = self.repo.get_measurement_summary_points(mid)
        self.summary_table.setRowCount(0)

        for p in pts:
            i = self.summary_table.rowCount()
            self.summary_table.insertRow(i)

            self.summary_table.setItem(i, 0, _item(p.get("current", "")))
            self.summary_table.setItem(i, 1, _item(p.get("peak1", "")))
            self.summary_table.setItem(i, 2, _item(p.get("peak2", "")))
            self.summary_table.setItem(i, 3, _item(p.get("fwhm_nm", "")))
            self.summary_table.setItem(i, 4, _item(p.get("smsr", "")))
            self.summary_table.setItem(i, 5, _item(p.get("power", "")))
            self.summary_table.setItem(i, 6, _item(p.get("voltage", "")))

            self.summary_table.setItem(i, 7, _item(p.get("base_ch5", "")))   # -> Lid(CH5)
            self.summary_table.setItem(i, 8, _item(p.get("pkg_ch3", "")))    # -> Base(CH3)
            self.summary_table.setItem(i, 9, _item(p.get("fiber_ch2", "")))
            self.summary_table.setItem(i, 10, _item(p.get("eff", "")))