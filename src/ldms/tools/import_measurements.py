# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import pandas as pd

from src.ldms.config import get_data_dir, get_db_path
from src.ldms.db.repo import DBRepo
from src.ldms.process_flow import DEFAULT_MODEL_CODE, normalize_model_code


@dataclass
class ExcelTarget:
    path: Path
    stage: str
    group_name: str
    module_no: str
    file_name: str
    model: str = DEFAULT_MODEL_CODE


# ✅ 모듈번호: 알파벳(1~4) + 숫자(1~4)
MOD_PAT = re.compile(r"([A-Za-z]{1,4}\d{1,4})")
MODEL_PAT = re.compile(r"(F\d{3,4}\d{1,4}W)", re.IGNORECASE)


def extract_model_code_from_path(path: Path, default: str = DEFAULT_MODEL_CODE) -> str:
    """
    파일명/상위 폴더명에서 F976370W, F976550W 같은 모델명을 추출.
    없으면 기본 F976370W.
    """
    try:
        m = MODEL_PAT.search(str(path))
        if m:
            return normalize_model_code(m.group(1))
    except Exception:
        pass
    return normalize_model_code(default or DEFAULT_MODEL_CODE)


def extract_module_no_from_filename(name: str) -> Optional[str]:
    stem = Path(name).stem
    m = MOD_PAT.search(stem)
    if not m:
        return None
    return m.group(1).upper()


def find_excels(raw_root: Path) -> List[ExcelTarget]:
    out: List[ExcelTarget] = []

    if not raw_root.exists():
        return out

    # raw 아래 1단계 폴더 = stage 로 취급
    for stage_dir in sorted([p for p in raw_root.iterdir() if p.is_dir()]):
        stage = stage_dir.name.strip()

        # stage_dir 아래는 (바로 파일이 있을 수도 / group 폴더가 있을 수도)
        for p in stage_dir.rglob("*.xlsx"):
            if p.name.startswith("~$"):
                continue

            # 새 구조: raw/stage/model/group/file.xlsx
            # 기존 구조: raw/stage/group/file.xlsx
            rel = p.relative_to(stage_dir)
            parts = rel.parts
            group_name = ""
            model = extract_model_code_from_path(p)

            if len(parts) >= 2:
                if re.fullmatch(r"F\d{3,4}\d{1,4}W", parts[0], flags=re.IGNORECASE):
                    model = normalize_model_code(parts[0])
                    if len(parts) >= 3:
                        group_name = parts[1]
                else:
                    group_name = parts[0]

            module_no = extract_module_no_from_filename(p.name)
            if not module_no:
                continue

            out.append(
                ExcelTarget(
                    path=p,
                    stage=stage,
                    group_name=group_name,
                    module_no=module_no,
                    file_name=p.name,
                    model=model,
                )
            )
    return out


def safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, str) and x.strip() == "":
            return None
        return float(x)
    except Exception:
        try:
            return float(str(x).strip())
        except Exception:
            return None


def _norm(s: str) -> str:
    t = str(s).strip().lower()
    t = t.replace("\n", " ").replace("\r", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("㎚", "nm")
    return t


def _canon_col(name: str) -> Optional[str]:
    """
    Summary 헤더 유도리 매핑 (구/신/최신 모두)
    표준 의미:
      Current, Peak1, Peak2, FWHM(nm), SMSR, Power, Voltage,
      Lid(CH5), Base(CH3), Fiber(CH2), 효율
    """
    n = _norm(name)
    nn = n.replace(" ", "")

    # Current
    if n in ("current", "current(a)", "i", "i(a)", "current (a)"):
        return "Current"

    # Peak
    if "peak1" in n or "peak 1" in n:
        return "Peak1"
    if "peak2" in n or "peak 2" in n:
        return "Peak2"

    # FWHM
    if "fwhm" in n or "반치폭" in n:
        return "FWHM(nm)"

    # SMSR
    if "smsr" in n or "dbm diff" in n or "dbm_diff" in n or "dbmdiff" in n:
        return "SMSR"

    # Power / Voltage
    if n.startswith("power") or n in ("p", "power(w)", "power (w)"):
        return "Power"
    if n.startswith("voltage") or n in ("v", "voltage(v)", "voltage (v)"):
        return "Voltage"

    # Temp Channels
    # 최신: Lid(CH5), Base(CH3), Fiber(CH2)
    # 구버전: Base(CH5) == Lid(CH5)
    # 구버전: PKG(CH3) == Base(CH3)
    # "온도" 단독은 CH3로 간주 -> Base(CH3)
    if nn in ("lid(ch5)", "lidch5", "ch5lid"):
        return "Lid(CH5)"
    if nn in ("base(ch5)", "basech5", "ch5"):
        return "Lid(CH5)"

    if nn in ("base(ch3)", "basech3", "ch3"):
        return "Base(CH3)"
    if nn in ("pkg(ch3)", "pkgch3", "pkg"):
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
        std = _canon_col(c)
        if std:
            rename_map[c] = std

    if not rename_map:
        return []

    sdf = df.rename(columns=rename_map).copy()
    if "Current" not in sdf.columns:
        return []

    sdf = sdf.dropna(how="all")

    out: List[dict] = []
    for _, r in sdf.iterrows():
        cur = safe_float(r.get("Current"))
        if cur is None:
            continue

        std = {
            "Current": cur,
            "Peak1": safe_float(r.get("Peak1")) if "Peak1" in sdf.columns else None,
            "Peak2": safe_float(r.get("Peak2")) if "Peak2" in sdf.columns else None,
            "FWHM(nm)": safe_float(r.get("FWHM(nm)")) if "FWHM(nm)" in sdf.columns else None,
            "SMSR": safe_float(r.get("SMSR")) if "SMSR" in sdf.columns else None,
            "Power": safe_float(r.get("Power")) if "Power" in sdf.columns else None,
            "Voltage": safe_float(r.get("Voltage")) if "Voltage" in sdf.columns else None,
            "Lid(CH5)": safe_float(r.get("Lid(CH5)")) if "Lid(CH5)" in sdf.columns else None,
            "Base(CH3)": safe_float(r.get("Base(CH3)")) if "Base(CH3)" in sdf.columns else None,
            "Fiber(CH2)": safe_float(r.get("Fiber(CH2)")) if "Fiber(CH2)" in sdf.columns else None,
            "효율": safe_float(r.get("효율")) if "효율" in sdf.columns else None,
        }
        out.append(std)

    return out


def _std_to_db_point(std: dict) -> dict:
    return dict(
        current=std.get("Current"),
        peak1=std.get("Peak1"),
        peak2=std.get("Peak2"),
        fwhm_nm=std.get("FWHM(nm)"),
        smsr=std.get("SMSR"),
        power=std.get("Power"),
        voltage=std.get("Voltage"),
        base_ch5=std.get("Lid(CH5)"),      # 최신 lid -> DB base_ch5
        pkg_ch3=std.get("Base(CH3)"),      # 최신 base(ch3) -> DB pkg_ch3
        fiber_ch2=std.get("Fiber(CH2)"),
        eff=std.get("효율"),
    )


def parse_summary_sheet(xls: pd.ExcelFile) -> List[dict]:
    if "Summary" not in xls.sheet_names:
        return []

    df = pd.read_excel(xls, sheet_name="Summary", header=0)
    std_pts = _to_points_from_df(df)
    return [_std_to_db_point(s) for s in std_pts]


def parse_spectrum_sheet(
    xls: pd.ExcelFile,
    sheet_name: str
) -> List[Tuple[str, Optional[float], Optional[float], Optional[float]]]:
    if sheet_name not in xls.sheet_names:
        return []
    df = pd.read_excel(xls, sheet_name=sheet_name)

    wl_col = None
    dbm_col = None
    nw_col = None
    for c in df.columns:
        s = str(c).strip().lower()
        if "wavelength" in s or "파장" in s:
            wl_col = c
        elif "dbm" in s:
            dbm_col = c
        elif "nw" in s:
            nw_col = c

    if wl_col is None:
        return []

    out = []
    for _, r in df.iterrows():
        wl = safe_float(r.get(wl_col))
        if wl is None:
            continue
        out.append((
            sheet_name,
            wl,
            safe_float(r.get(dbm_col)) if dbm_col else None,
            safe_float(r.get(nw_col)) if nw_col else None
        ))
    return out


def import_one_excel(repo: DBRepo, t: ExcelTarget) -> bool:
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    model = normalize_model_code(getattr(t, "model", "") or extract_model_code_from_path(t.path))

    # ✅ 측정파일 import 시 모듈번호-모델 매핑도 함께 보정
    try:
        if t.module_no and hasattr(repo, "upsert_module"):
            repo.upsert_module(module_no=t.module_no, model=model)
    except Exception:
        pass

    mid = repo.upsert_measurement_file(
        module_no=t.module_no,
        stage=t.stage,
        group_name=t.group_name,
        file_name=t.file_name,
        file_path=str(t.path),
        imported_at=imported_at,
    )

    # 재-import 대비 삭제 후 다시 넣기
    repo.delete_measurement_points(mid)

    try:
        xls = pd.ExcelFile(t.path)
    except Exception:
        return False

    # Summary
    try:
        summary_points = parse_summary_sheet(xls)
        for p in summary_points:
            repo.insert_measurement_summary_point(mid, p)
    except Exception:
        # Summary 실패해도 Spectrum은 시도
        pass

    # Spectrum
    try:
        spec_rows: List[Tuple[str, Optional[float], Optional[float], Optional[float]]] = []
        spec_rows += parse_spectrum_sheet(xls, "16A Chart")
        spec_rows += parse_spectrum_sheet(xls, "20A Chart")
        repo.insert_measurement_spectra_points_bulk(mid, spec_rows)
    except Exception:
        pass

    return True


def main():
    data_dir = get_data_dir()
    raw_root = data_dir / "measurements" / "raw"
    db_path = get_db_path()
    repo = DBRepo(db_path)

    targets = find_excels(raw_root)
    total = len(targets)
    ok = 0
    fail = 0

    for t in targets:
        if import_one_excel(repo, t):
            ok += 1
        else:
            fail += 1

    print(f"✅ 측정 엑셀 Import 완료: {ok}개 / 실패: {fail}개 / 대상: {total}개")
    print(f"📁 폴더: {raw_root}")
    print(f"🗄️ DB: {db_path}")


if __name__ == "__main__":
    main()
