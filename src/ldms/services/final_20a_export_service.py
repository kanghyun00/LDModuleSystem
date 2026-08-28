# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side

from src.ldms.app_config import load_path_settings, normalize_and_fill_defaults
from src.ldms.process_flow import DEFAULT_MODEL_CODE, normalize_model_code


SETTINGS_KEY = "final_export_rules"
ALLOWED_EXTS = {".xlsx", ".xlsm", ".xls", ".csv"}


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _project_measure_raw_root() -> Path:
    return PROJECT_ROOT / "data" / "measurements" / "raw"


def _project_grading_out_root() -> Path:
    return PROJECT_ROOT / "data" / "measurements" / "grading"


# -------------------------
# 규칙 로드
# -------------------------
@dataclass
class FinalExportRules:
    judge_current: float = 20.0
    current_tolerance: float = 0.2

    a_min_power: float = 390.0
    b_min_power: float = 380.0
    c_min_power: float = 370.0

    smsr_yellow_max: float = 30.0
    smsr_red_max: float = 20.0

    pkg_yellow_min: float = 36.0
    pkg_red_min: float = 40.0

    ok_keywords_csv: str = "양품확정,양품 확정"


def _model_settings_key(model: str) -> str:
    model = normalize_model_code(model or DEFAULT_MODEL_CODE)
    return f"{SETTINGS_KEY}::{model}"


def _load_rules(repo, model: str = "") -> FinalExportRules:
    s = FinalExportRules()
    model = normalize_model_code(model or DEFAULT_MODEL_CODE)

    # 모델별 설정 우선, 없으면 구버전 공통 설정 fallback
    raw = ""
    try:
        raw = repo.get_setting(_model_settings_key(model), "")
    except Exception:
        raw = ""

    if not raw:
        try:
            raw = repo.get_setting(SETTINGS_KEY, "{}")
        except Exception:
            raw = "{}"

    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            for k, v in data.items():
                if hasattr(s, k):
                    try:
                        if isinstance(getattr(s, k), float):
                            setattr(s, k, float(v))
                        else:
                            setattr(s, k, "" if v is None else str(v))
                    except Exception:
                        pass
    except Exception:
        pass
    return s


# -------------------------
# 파일 스캔 유틸
# -------------------------
def _is_junk_file(p: Path) -> bool:
    name = p.name
    if name.startswith("~$") or name.startswith("."):
        return True
    return False


def _iter_files(root: Path) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    out: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_junk_file(p):
            continue
        if p.suffix.lower() not in ALLOWED_EXTS:
            continue
        out.append(p)
    out.sort(key=lambda x: x.name.lower())
    return out


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, str) and v.strip() == "":
            return None
        return float(v)
    except Exception:
        return None


def _norm(s: str) -> str:
    t = str(s).strip().lower()
    t = t.replace("\n", " ").replace("\r", " ")
    t = " ".join(t.split())
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("㎚", "nm")
    return t


def _canon_col(name: str) -> Optional[str]:
    n = _norm(name).replace(" ", "")

    # 기본
    if n in ("current", "current(a)", "i", "i(a)", "current(a)"):
        return "Current"
    if "peak1" in n:
        return "Peak1"
    if "peak2" in n:
        return "Peak2"
    if "fwhm" in n or "반치폭" in n:
        return "FWHM(nm)"
    if "smsr" in n:
        return "SMSR"
    if n.startswith("power") or n in ("p", "power(w)", "power(watt)"):
        return "Power"
    if n.startswith("voltage") or n in ("v", "voltage(v)"):
        return "Voltage"

    # 온도
    if n in ("lid(ch5)", "lidch5", "lid_temp(ch5)", "ch5lid", "base(ch5)", "basech5", "온도ch5"):
        return "Base(CH5)"  # (시트 표기가 Base(CH5)라서 유지)
    if n in ("base(ch3)", "basech3", "pkg(ch3)", "pkgch3", "pkg", "온도ch3", "temperature", "temp", "온도"):
        return "PKG(CH3)"  # (시트 표기가 PKG(CH3)라서 유지)
    if n in ("fiber(ch2)", "fiberch2", "ch2"):
        return "Fiber(CH2)"

    # 효율
    if n in ("eff", "efficiency", "효율"):
        return "효율"

    # SlopeEff
    if "slopeeff" in n or "slope_eff" in n or n == "slope":
        return "SlopeEff(W/A)"

    return None


def _read_summary_points(file_path: Path) -> List[Dict]:
    """
    Summary 표 형태를 최대한 유연하게 읽고,
    표 형태(열=헤더, 행=데이터)로 반환.
    """
    def _to_points(df: pd.DataFrame) -> List[Dict]:
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

        out: List[Dict] = []
        for _, r in sdf.iterrows():
            cur = r.get("Current", None)
            if pd.isna(cur):
                continue
            row = {}
            for k in [
                "Current", "Peak1", "Peak2", "FWHM(nm)", "SMSR", "Power", "Voltage",
                "Base(CH5)", "PKG(CH3)", "Fiber(CH2)", "효율", "SlopeEff(W/A)"
            ]:
                if k in sdf.columns:
                    row[k] = r.get(k, "")
            out.append(row)
        return out

    def _try_raw_matrix(raw: pd.DataFrame) -> List[Dict]:
        if raw is None or raw.empty:
            return []
        max_scan = min(len(raw), 300)
        for i in range(max_scan):
            row = raw.iloc[i].tolist()
            normed = [_norm(x) for x in row]
            if any(x in ("current", "current(a)", "i", "i(a)") for x in normed):
                header = raw.iloc[i].tolist()
                data = raw.iloc[i + 1:].copy()
                data.columns = header
                data = data.loc[:, [c for c in data.columns if str(c).strip() != ""]]
                return _to_points(data)
        return []

    suf = file_path.suffix.lower()
    if suf == ".csv":
        df = pd.read_csv(file_path, engine="python")
        return _to_points(df)

    xls = pd.ExcelFile(file_path)
    for sh in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sh, header=0)
            pts = _to_points(df)
            if pts:
                return pts
        except Exception:
            pass

        try:
            raw = pd.read_excel(xls, sheet_name=sh, header=None)
            pts = _try_raw_matrix(raw)
            if pts:
                return pts
        except Exception:
            pass

    return []


def _pick_row_at_current(points: List[Dict], target: float, tol: float) -> Optional[Dict]:
    if not points:
        return None
    best = None
    best_diff = None
    for p in points:
        cur = _num(p.get("Current"))
        if cur is None:
            continue
        diff = abs(cur - float(target))
        if best is None or diff < best_diff:
            best, best_diff = p, diff
    if best is None:
        return None
    if best_diff is not None and best_diff > float(tol):
        return None
    return best




def _extract_module_no_from_name(name: str) -> str:
    """
    파일명에서 모듈번호를 최대한 추출.
    예: E092_VBG오븐후.xlsx -> E092
    """
    stem = Path(str(name)).stem.strip()
    m = re.search(r"([A-Za-z]{1,4}\d{1,4})", stem)
    return m.group(1).upper() if m else ""


def _name_compact(p: Path) -> str:
    return re.sub(r"\s+", "", str(p).lower())


def _is_vbg_oven_after_file(p: Path) -> bool:
    """
    VBG 오븐 후 출력 기준 파일 후보 판정.

    허용 예:
    - VBG 오븐 후
    - VBG오븐후
    - VBG oven after
    - VBG after oven
    - VBG post oven
    """
    s = _name_compact(p)
    if "vbg" not in s:
        return False

    korean_after = ("오븐후" in s) or ("oven후" in s)
    english_after = (
        ("ovenafter" in s)
        or ("afteroven" in s)
        or ("postoven" in s)
        or ("ovenpost" in s)
    )
    return bool(korean_after or english_after)


def _is_fiber_align_oven_after_file(p: Path) -> bool:
    """
    Fiber 정렬 후 오븐 후 출력 파일 후보 판정.

    허용 예:
    - Fiber 오븐 후
    - Fiber 정렬 후 오븐 후
    - Fiber오븐후
    - Fiber oven after
    - Fiber after oven
    - Fiber post oven

    주의:
    - VBG가 함께 들어간 파일은 VBG 기준 파일로 볼 수 있으므로 제외
    """
    s = _name_compact(p)
    has_fiber = ("fiber" in s) or ("파이버" in s)
    if not has_fiber:
        return False
    if "vbg" in s:
        return False

    korean_after = ("오븐후" in s) or ("oven후" in s)
    english_after = (
        ("ovenafter" in s)
        or ("afteroven" in s)
        or ("postoven" in s)
        or ("ovenpost" in s)
    )
    return bool(korean_after or english_after)


def _pick_last_current_power(f: Path) -> Optional[Dict[str, object]]:
    """
    Summary에서 가장 높은 Current 행의 Power를 반환.
    사용자가 정의한 커플링 기준은 '맨 마지막 출력값 = 가장 높은 전류 인가 시 출력'이므로
    judge_current 설정값이 아니라 Summary 내 최대 Current 행을 사용한다.
    """
    try:
        pts = _read_summary_points(f)
    except Exception:
        return None

    best = None
    best_cur = None
    for p in pts:
        cur = _num(p.get("Current"))
        power = _num(p.get("Power"))
        if cur is None or power is None:
            continue
        if best is None or cur > best_cur:
            best = p
            best_cur = cur

    if best is None:
        return None

    return {
        "current": _num(best.get("Current")),
        "power": _num(best.get("Power")),
        "file": f.name,
    }


def _build_power_map_by_stage(files: List[Path], predicate) -> Dict[str, Dict[str, object]]:
    """
    모듈번호별 기준 Power 맵 생성.
    같은 모듈 후보가 여러 개면 파일 수정시간이 최신인 파일을 사용.
    """
    out: Dict[str, Dict[str, object]] = {}

    for f in files:
        if not predicate(f):
            continue

        mod = _extract_module_no_from_name(f.name)
        if not mod:
            continue

        picked = _pick_last_current_power(f)
        if not picked or _num(picked.get("power")) is None:
            continue

        try:
            mtime = f.stat().st_mtime
        except Exception:
            mtime = 0

        old = out.get(mod)
        if old is None or float(mtime) >= float(old.get("_mtime") or 0):
            picked["module_no"] = mod
            picked["_mtime"] = mtime
            out[mod] = picked

    return out


def _build_vbg_oven_after_power_map(files: List[Path], rules: FinalExportRules) -> Dict[str, Dict[str, object]]:
    # rules 인자는 기존 호출부 호환용으로 유지
    return _build_power_map_by_stage(files, _is_vbg_oven_after_file)


def _build_fiber_oven_after_power_map(files: List[Path], rules: FinalExportRules) -> Dict[str, Dict[str, object]]:
    # rules 인자는 기존 호출부 호환용으로 유지
    return _build_power_map_by_stage(files, _is_fiber_align_oven_after_file)




def _grade_from_power(power: Optional[float], rules: FinalExportRules) -> str:
    if power is None:
        return "Fail"
    if power >= rules.a_min_power:
        return "A급"
    if power >= rules.b_min_power:
        return "B급"
    if power >= rules.c_min_power:
        return "C급"
    return "Fail"


# -------------------------
# 엑셀 스타일
# -------------------------
FILL_HEADER_GRAY = PatternFill("solid", fgColor="D9D9D9")   # 헤더
FILL_ROW_OK_GRAY = PatternFill("solid", fgColor="D9D9D9")   # ✅ 양품확정 회색(행 전체)

FILL_A = PatternFill("solid", fgColor="BDD7EE")            # A급(파랑)
FILL_B = PatternFill("solid", fgColor="A9D18E")            # B급(초록)
FILL_C = PatternFill("solid", fgColor="FFD966")            # C급(노랑)
FILL_FAIL = PatternFill("solid", fgColor="FF0000")         # Fail(빨강)

FILL_WARN_YELLOW = PatternFill("solid", fgColor="FFD966")  # 경고(노랑)
FILL_WARN_RED = PatternFill("solid", fgColor="FF0000")     # 경고(빨강)

BORDER_THIN = Border(
    left=Side(style="thin", color="A0A0A0"),
    right=Side(style="thin", color="A0A0A0"),
    top=Side(style="thin", color="A0A0A0"),
    bottom=Side(style="thin", color="A0A0A0"),
)


def _autosize_columns(ws, min_w=8, max_w=40):
    for col_cells in ws.columns:
        max_len = 0
        col_letter = col_cells[0].column_letter
        for cell in col_cells:
            v = cell.value
            if v is None:
                continue
            s = str(v)
            if len(s) > max_len:
                max_len = len(s)
        ws.column_dimensions[col_letter].width = max(min_w, min(max_w, max_len + 2))


def _resolve_raw_root(repo) -> Path:
    """
    등급정리 입력 RAW 루트.
    1순위: Parameter > 검색/비교 RAW 폴더(search_compare_raw_root)
    2순위: Parameter > 측정 저장 폴더(export_dir_measure)
    3순위: 현재 프로젝트/data/measurements/raw
    """
    try:
        ps = normalize_and_fill_defaults(load_path_settings(repo))

        p1 = str(getattr(ps, "search_compare_raw_root", "") or "").strip()
        if p1:
            return Path(p1)

        p2 = str(getattr(ps, "export_dir_measure", "") or "").strip()
        if p2:
            return Path(p2)
    except Exception:
        pass

    return _project_measure_raw_root()


def _resolve_20a_grading_out_dir(repo, stage: str, group: str) -> Path:
    """
    등급정리 엑셀 저장 경로.
    1순위: Parameter > 20A 등급분류 저장 폴더(export_dir_20a_grading)
    2순위: 현재 프로젝트/data/measurements/grading
    """
    stage = str(stage).strip()
    group = str(group).strip() if group is not None else "(전체)"

    try:
        ps = normalize_and_fill_defaults(load_path_settings(repo))
        out_base = str(getattr(ps, "export_dir_20a_grading", "") or "").strip()
        out_dir = Path(out_base) if out_base else _project_grading_out_root()
    except Exception:
        out_dir = _project_grading_out_root()

    if stage:
        out_dir = out_dir / stage
    if group and group != "(전체)":
        out_dir = out_dir / group

    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def export_finaldata_20a_excel(repo, stage: str, group: str = "(전체)", model: str = "") -> str:
    """
    SearchTab에서 호출하는 엔트리포인트(이름 유지)
    - 실제 전류는 Parameter(final_export_rules)의 judge_current 사용
    - ✅ 저장 경로는 Parameter(path_settings)의 export_dir_20a_grading 사용(추가)
    """
    model = normalize_model_code(model or DEFAULT_MODEL_CODE)
    rules = _load_rules(repo, model=model)

    stage = str(stage).strip()
    group = str(group).strip() if group is not None else "(전체)"

    # RAW 위치:
    # 새 구조: raw/stage/model/group
    # 하위호환: raw/stage/group
    raw_root = _resolve_raw_root(repo)
    base_new = raw_root / stage / model
    if group and group != "(전체)":
        base_new = base_new / group

    base_old = raw_root / stage
    if group and group != "(전체)":
        base_old = base_old / group

    if base_new.exists():
        base = base_new
    elif base_old.exists():
        base = base_old
    else:
        raise FileNotFoundError(f"RAW 폴더가 없습니다:\n새 구조: {base_new}\n기존 구조: {base_old}")

    # ✅ 결과 폴더: 새 경로가 있으면 그쪽, 없으면 기존 base/등급정리
    out_dir = _resolve_20a_grading_out_dir(repo, stage=stage, group=group)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_stage = stage.replace(os.sep, "_")
    safe_group = (group if group else "(전체)").replace(os.sep, "_")
    suffix_group = "" if (not group or group == "(전체)") else f"_{safe_group}"

    out_path = out_dir / f"{safe_stage}{suffix_group}_{model}_data_{rules.judge_current:g}A_등급정리_{ts}.xlsx"

    # 모으기
    rows: List[Dict] = []
    ok_keywords = [k.strip() for k in (rules.ok_keywords_csv or "").split(",") if k.strip()]

    all_files = _iter_files(base)
    vbg_after_map = _build_vbg_oven_after_power_map(all_files, rules)
    fiber_after_map = _build_fiber_oven_after_power_map(all_files, rules)

    for f in all_files:
        pts = _read_summary_points(f)
        picked = _pick_row_at_current(pts, rules.judge_current, rules.current_tolerance)
        if not picked:
            continue

        power = _num(picked.get("Power"))
        smsr = _num(picked.get("SMSR"))
        base_ch5 = _num(picked.get("Base(CH5)"))
        pkg_ch3 = _num(picked.get("PKG(CH3)"))

        grade = _grade_from_power(power, rules)

        # slopeeff: 파일에 없으면 Power/Current로 근사
        slope = _num(picked.get("SlopeEff(W/A)"))
        cur = _num(picked.get("Current"))
        if slope is None and power is not None and cur not in (None, 0):
            slope = power / cur

        # ✅ "양품확정" 키워드 포함 여부는 저장만 해두고(조건표 안내용), 색칠 판단은 아래에서 A급+무경고로만 함
        is_ok_keyword = any(k in f.stem for k in ok_keywords) if ok_keywords else False

        # ✅ Coupling 효율 계산 기준:
        #   Fiber 정렬 후 오븐 후의 최고전류 Power / VBG 오븐 후의 최고전류 Power × 100
        #   일반적으로 Fiber 오븐 후 Power가 VBG 오븐 후 Power보다 낮으므로 100% 이하로 계산됨.
        module_no = _extract_module_no_from_name(f.name)
        vbg_ref = vbg_after_map.get(module_no, {}) if module_no else {}
        fiber_ref = fiber_after_map.get(module_no, {}) if module_no else {}

        vbg_after_power = _num(vbg_ref.get("power"))
        fiber_after_power = _num(fiber_ref.get("power"))
        vbg_after_current = _num(vbg_ref.get("current"))
        fiber_after_current = _num(fiber_ref.get("current"))

        coupling_eff = None
        try:
            if fiber_after_power is not None and vbg_after_power not in (None, 0):
                coupling_eff = float(fiber_after_power) / float(vbg_after_power) * 100.0
        except Exception:
            coupling_eff = None

        rows.append({
            "Model": model,
            "Name": f.stem,
            "Current": cur,
            "Peak1 (nm)": _num(picked.get("Peak1")),
            "Peak2 (nm)": _num(picked.get("Peak2")),
            "FWHM (nm)": _num(picked.get("FWHM(nm)")),
            "SMSR (dB)": smsr,
            "Power (W)": power,
            "VBG 오븐 후 최고전류 (A)": vbg_after_current,
            "VBG 오븐 후 Power (W)": vbg_after_power,
            "Fiber 오븐 후 최고전류 (A)": fiber_after_current,
            "Fiber 오븐 후 Power (W)": fiber_after_power,
            "Coupling Eff (%)": coupling_eff,
            "VBG 오븐 후 파일": vbg_ref.get("file", ""),
            "Fiber 오븐 후 파일": fiber_ref.get("file", ""),
            "Voltage (V)": _num(picked.get("Voltage")),
            "Base(CH5) (°C)": base_ch5,
            "PKG(CH3) (°C)": pkg_ch3,
            "Fiber(CH2) (°C)": _num(picked.get("Fiber(CH2)")),
            "효율 (%)": _num(picked.get("효율")),
            "SlopeEff (W/A)": slope,
            "_grade": grade,
            "_ok_keyword": "Y" if is_ok_keyword else "",
        })

    # 등급 정렬: A -> B -> C -> Fail
    order = {"A급": 0, "B급": 1, "C급": 2, "Fail": 3}
    rows.sort(key=lambda r: (order.get(r.get("_grade", ""), 99), str(r.get("Name", ""))))

    # 엑셀 생성
    wb = Workbook()
    ws = wb.active
    ws.title = f"{model}_{rules.judge_current:g}A"[:31]

    headers = [
        "Model", "Name", "Current", "Peak1 (nm)", "Peak2 (nm)", "FWHM (nm)", "SMSR (dB)", "Power (W)",
        "VBG 오븐 후 최고전류 (A)", "VBG 오븐 후 Power (W)",
        "Fiber 오븐 후 최고전류 (A)", "Fiber 오븐 후 Power (W)",
        "Coupling Eff (%)", "VBG 오븐 후 파일", "Fiber 오븐 후 파일",
        "Voltage (V)", "Base(CH5) (°C)", "PKG(CH3) (°C)", "Fiber(CH2) (°C)", "효율 (%)", "SlopeEff (W/A)"
    ]

    # 헤더
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = FILL_HEADER_GRAY
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER_THIN

    # 데이터
    for r in rows:
        ws.append([r.get(h, "") for h in headers])

    # 테두리/정렬
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=len(headers)):
        for cell in row:
            cell.border = BORDER_THIN
            cell.alignment = Alignment(horizontal="center", vertical="center")

    # 컬럼 인덱스
    col_smsr = headers.index("SMSR (dB)") + 1
    col_power = headers.index("Power (W)") + 1
    col_pkg = headers.index("PKG(CH3) (°C)") + 1
    col_base = headers.index("Base(CH5) (°C)") + 1

    # -------------------------
    # ✅ 색칠 규칙 적용
    # - 등급 색: Power 셀에만 (A=파랑, B=초록, C=노랑, Fail=빨강)
    # - SMSR / PKG / Base 색: 조건에 걸리면 해당 셀만 노/빨
    # - ✅ 양품(회색): "Power가 A급 조건" AND "같은 행에 경고색이 하나도 없을 때" -> 행 전체 회색 덮기
    # -------------------------
    for rr in range(2, ws.max_row + 1):
        power_v = _num(ws.cell(rr, col_power).value)
        smsr_v = _num(ws.cell(rr, col_smsr).value)
        pkg_v = _num(ws.cell(rr, col_pkg).value)
        base_v = _num(ws.cell(rr, col_base).value)

        # 1) 등급 색(POWER 셀)
        if power_v is None:
            ws.cell(rr, col_power).fill = FILL_FAIL
        elif power_v >= rules.a_min_power:
            ws.cell(rr, col_power).fill = FILL_A
        elif power_v >= rules.b_min_power:
            ws.cell(rr, col_power).fill = FILL_B
        elif power_v >= rules.c_min_power:
            ws.cell(rr, col_power).fill = FILL_C
        else:
            ws.cell(rr, col_power).fill = FILL_FAIL

        # 2) SMSR 색(조건)
        warn_any = False
        if smsr_v is not None:
            if smsr_v <= rules.smsr_red_max:
                ws.cell(rr, col_smsr).fill = FILL_WARN_RED
                warn_any = True
            elif smsr_v <= rules.smsr_yellow_max:
                ws.cell(rr, col_smsr).fill = FILL_WARN_YELLOW
                warn_any = True

        # 3) PKG 색(조건)
        if pkg_v is not None:
            if pkg_v >= rules.pkg_red_min:
                ws.cell(rr, col_pkg).fill = FILL_WARN_RED
                warn_any = True
            elif pkg_v >= rules.pkg_yellow_min:
                ws.cell(rr, col_pkg).fill = FILL_WARN_YELLOW
                warn_any = True

        # 4) Base(CH5)도 같은 기준으로 색칠(이미지처럼)
        if base_v is not None:
            if base_v >= rules.pkg_red_min:
                ws.cell(rr, col_base).fill = FILL_WARN_RED
                warn_any = True
            elif base_v >= rules.pkg_yellow_min:
                ws.cell(rr, col_base).fill = FILL_WARN_YELLOW
                warn_any = True

        # ✅ 5) 양품(회색) = "Power가 A급 조건" AND "경고색 하나도 없음"
        if (power_v is not None) and (power_v >= rules.a_min_power) and (not warn_any):
            for cc in range(1, len(headers) + 1):
                ws.cell(rr, cc).fill = FILL_ROW_OK_GRAY

    # -------------------------
    # ✅ 오른쪽 조건표(안내) 유지
    # -------------------------
    legend_col = len(headers) + 3
    r0 = 2

    def put(r, c, text, fill=None, bold=False):
        cell = ws.cell(r, c, value=text)
        cell.border = BORDER_THIN
        cell.alignment = Alignment(horizontal="left", vertical="center")
        if fill:
            cell.fill = fill
        if bold:
            cell.font = Font(bold=True)
        return cell

    put(r0, legend_col, f"등급/색 조건 - {model}", fill=FILL_HEADER_GRAY, bold=True); r0 += 1
    put(r0, legend_col, f"Power ≥ {rules.a_min_power:g}  →  A급", fill=FILL_A); r0 += 1
    put(r0, legend_col, f"Power ≥ {rules.b_min_power:g}  →  B급", fill=FILL_B); r0 += 1
    put(r0, legend_col, f"Power ≥ {rules.c_min_power:g}  →  C급", fill=FILL_C); r0 += 1
    put(r0, legend_col, f"Power < {rules.c_min_power:g}  →  Fail", fill=FILL_FAIL); r0 += 2

    put(r0, legend_col, "SMSR 색", fill=FILL_HEADER_GRAY, bold=True); r0 += 1
    put(r0, legend_col, f"노랑: SMSR ≤ {rules.smsr_yellow_max:g}", fill=FILL_WARN_YELLOW); r0 += 1
    put(r0, legend_col, f"빨강: SMSR ≤ {rules.smsr_red_max:g}", fill=FILL_WARN_RED); r0 += 2

    put(r0, legend_col, "온도(Base/PKG) 색", fill=FILL_HEADER_GRAY, bold=True); r0 += 1
    put(r0, legend_col, f"노랑: ≥ {rules.pkg_yellow_min:g}", fill=FILL_WARN_YELLOW); r0 += 1
    put(r0, legend_col, f"빨강: ≥ {rules.pkg_red_min:g}", fill=FILL_WARN_RED); r0 += 2

    put(r0, legend_col, "양품확정", fill=FILL_HEADER_GRAY, bold=True); r0 += 1
    put(r0, legend_col, "A급 + (SMSR/온도 조건 색 없음) → 행 전체 회색", fill=FILL_ROW_OK_GRAY); r0 += 2

    put(r0, legend_col, "Coupling Eff", fill=FILL_HEADER_GRAY, bold=True); r0 += 1
    put(r0, legend_col, "Fiber 오븐 후 최고전류 Power / VBG 오븐 후 최고전류 Power × 100", fill=None); r0 += 1
    put(r0, legend_col, "VBG 파일명 예: VBG오븐후, VBG 오븐 후, VBG oven after", fill=None); r0 += 1
    put(r0, legend_col, "Fiber 파일명 예: Fiber오븐후, Fiber 정렬 후 오븐 후, Fiber oven after", fill=None); r0 += 1

    _autosize_columns(ws)
    wb.save(str(out_path))
    return str(out_path)
