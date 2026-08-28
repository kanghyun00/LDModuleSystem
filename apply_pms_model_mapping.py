from pathlib import Path
from datetime import datetime
import shutil

ROOT = Path(r"C:\Users\win10\Desktop\kanghyun\LDModuleSystemcode")
SRC = ROOT / "src" / "ldms"

FILES = {
    "repo": SRC / "db" / "repo.py",
    "schema": SRC / "db" / "schema.py",
    "pms": SRC / "services" / "pms_service.py",
    "process": SRC / "ui" / "tabs" / "process_tab.py",
}

backup_dir = ROOT / "backup_before_pms_model_mapping"
backup_dir.mkdir(parents=True, exist_ok=True)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

for name, path in FILES.items():
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    backup_path = backup_dir / f"{path.stem}_{stamp}.py"
    shutil.copy2(path, backup_path)
    print(f"[백업 완료] {backup_path}")

# =========================================================
# repo.py 수정
# =========================================================
repo_path = FILES["repo"]
repo = repo_path.read_text(encoding="utf-8")

old_get_module_model = '''    def get_module_model(self, module_no: str) -> str:
        module_no = normalize_module_no(module_no)
        if not module_no:
            return DEFAULT_MODEL_CODE
        with self.connect() as conn:
            row = conn.execute(
                "SELECT model FROM modules WHERE module_no = ? LIMIT 1",
                (module_no,),
            ).fetchone()
            if not row:
                return DEFAULT_MODEL_CODE
            return normalize_model_code(row["model"] or DEFAULT_MODEL_CODE)
'''

new_get_module_model = '''    def get_module_model(self, module_no: str) -> str:
        """
        모듈번호의 실제 모델을 조회합니다.

        - 등록된 모듈: 실제 모델 반환
        - 미등록 모듈: 빈 문자열 반환
        - 모델이 비어 있는 모듈: 빈 문자열 반환

        미등록 모듈을 기본 모델로 자동 귀속하지 않습니다.
        """
        module_no = normalize_module_no(module_no)

        if not module_no:
            return ""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT model
                FROM modules
                WHERE UPPER(module_no) = UPPER(?)
                LIMIT 1
                """,
                (module_no,),
            ).fetchone()

        if not row:
            return ""

        raw_model = str(row["model"] or "").strip()

        if not raw_model:
            return ""

        return normalize_model_code(raw_model)

    def get_module_models(self, module_nos: List[str]) -> Dict[str, str]:
        """
        여러 모듈번호의 모델을 한 번에 조회합니다.

        DB에 등록되지 않았거나 모델이 비어 있는 모듈은
        결과에 포함하지 않습니다.
        """
        normalized_modules: List[str] = []

        for raw in module_nos or []:
            module_no = normalize_module_no(raw)

            if module_no and module_no not in normalized_modules:
                normalized_modules.append(module_no)

        if not normalized_modules:
            return {}

        placeholders = ",".join("?" for _ in normalized_modules)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT module_no, model
                FROM modules
                WHERE UPPER(module_no) IN ({placeholders})
                """,
                [module_no.upper() for module_no in normalized_modules],
            ).fetchall()

        result: Dict[str, str] = {}

        for row in rows:
            module_no = normalize_module_no(row["module_no"])
            raw_model = str(row["model"] or "").strip()

            if not module_no or not raw_model:
                continue

            result[module_no] = normalize_model_code(raw_model)

        return result
'''

if old_get_module_model not in repo:
    raise RuntimeError("repo.py의 get_module_model 원본을 찾지 못했습니다.")

repo = repo.replace(old_get_module_model, new_get_module_model, 1)
repo_path.write_text(repo, encoding="utf-8")
print("[수정 완료] repo.py")

# =========================================================
# schema.py 수정
# =========================================================
schema_path = FILES["schema"]
schema = schema_path.read_text(encoding="utf-8")

schema = schema.replace(
    '_add_col_if_missing(conn, "process_logs", "model", "model TEXT DEFAULT \'F976370W\'")',
    '_add_col_if_missing(conn, "process_logs", "model", "model TEXT")',
)

schema = schema.replace(
    '_add_col_if_missing(conn, "modules", "model", "model TEXT DEFAULT \'F976370W\'")',
    '_add_col_if_missing(conn, "modules", "model", "model TEXT")',
)

old_update_process = '''    # ✅ 기존 데이터 보정:
    # 예전 버전에서 작성된 공정기록은 model 값이 NULL/공백일 수 있으므로 370W로 자동 귀속
    conn.execute("UPDATE process_logs SET model = 'F976370W' WHERE model IS NULL OR TRIM(model) = ''")
    conn.execute("UPDATE modules SET model = 'F976370W' WHERE model IS NULL OR TRIM(model) = ''")

'''

if old_update_process in schema:
    schema = schema.replace(old_update_process, "", 1)
else:
    print("[알림] schema.py의 기존 자동보정 주석 블록을 찾지 못했습니다.")

schema_path.write_text(schema, encoding="utf-8")
print("[수정 완료] schema.py")

# =========================================================
# pms_service.py 수정
# =========================================================
pms_path = FILES["pms"]
pms = pms_path.read_text(encoding="utf-8")

# 기존 5열 상수는 유지합니다.
# 기존 파일을 읽을 때 5열/6열을 모두 자동 인식하도록 함수를 교체합니다.

old_load_rows_start = "def load_process_rows_between(start_date: str, end_date: str) -> list[dict]:"
start_index = pms.find(old_load_rows_start)

if start_index < 0:
    raise RuntimeError("pms_service.py의 load_process_rows_between 함수를 찾지 못했습니다.")

end_marker = "\n\ndef summarize_week("
end_index = pms.find(end_marker, start_index)

if end_index < 0:
    raise RuntimeError("pms_service.py의 summarize_week 위치를 찾지 못했습니다.")

new_load_rows = '''def _header_index_map(ws) -> Dict[str, int]:
    """
    PMS Excel 헤더를 읽어 컬럼 위치를 반환합니다.

    지원 형식:
    구형: 날짜 | 공정명 | 개수 | 모듈번호 | 비고
    신형: 날짜 | 모델명 | 공정명 | 개수 | 모듈번호 | 비고
    """
    aliases = {
        "date": {"날짜", "date", "work_date"},
        "model": {"모델", "모델명", "model", "model_code", "product_model"},
        "process": {"공정명", "공정", "process", "process_name"},
        "qty": {"개수", "수량", "qty", "quantity"},
        "module": {"모듈번호", "모듈", "module", "module_no", "modules"},
        "remark": {"비고", "remark", "note"},
    }

    result: Dict[str, int] = {}

    for col_idx, cell in enumerate(ws[1], start=1):
        value = "" if cell.value is None else str(cell.value).strip()
        if not value:
            continue

        normalized = value.replace(" ", "").lower()

        for key, candidates in aliases.items():
            if normalized in {
                candidate.replace(" ", "").lower()
                for candidate in candidates
            }:
                result[key] = col_idx
                break

    # 헤더를 인식하지 못한 레거시 파일은 고정 5열로 처리합니다.
    if "process" not in result:
        result["process"] = 2
    if "qty" not in result:
        result["qty"] = 3
    if "module" not in result:
        result["module"] = 4
    if "remark" not in result:
        result["remark"] = 5

    return result


def _cell_text(ws, row_idx: int, col_idx: Optional[int]) -> str:
    if not col_idx or col_idx < 1:
        return ""

    value = ws.cell(row=row_idx, column=col_idx).value
    return "" if value is None else str(value).strip()


def _cell_int(ws, row_idx: int, col_idx: Optional[int]) -> int:
    value = _cell_text(ws, row_idx, col_idx)

    if not value:
        return 0

    try:
        return max(0, int(float(value)))
    except Exception:
        return 0


def load_process_rows_between(start_date: str, end_date: str) -> list[dict]:
    """
    PMS Excel 기간 자료를 읽습니다.

    구형 5열과 신형 6열을 모두 지원합니다.
    모델 정보가 없는 구형 행은 model을 빈 문자열로 반환하며,
    실제 모델 판정은 ProcessTab에서 DB 모듈 매핑으로 수행합니다.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    rows: list[dict] = []

    for d, path in _iter_pms_daily_files(start, end):
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            header_map = _header_index_map(ws)

            date_col = header_map.get("date")
            model_col = header_map.get("model")
            process_col = header_map.get("process")
            qty_col = header_map.get("qty")
            module_col = header_map.get("module")
            remark_col = header_map.get("remark")

            for row_idx in range(2, ws.max_row + 1):
                process = _cell_text(ws, row_idx, process_col)
                modules_csv = _cell_text(ws, row_idx, module_col)

                if not process and not modules_csv:
                    continue

                work_date = _cell_text(ws, row_idx, date_col) or d.strftime("%Y-%m-%d")
                raw_model = _cell_text(ws, row_idx, model_col)
                qty = _cell_int(ws, row_idx, qty_col)
                remark = _cell_text(ws, row_idx, remark_col)

                proc_norm = normalize_process_name(process)
                modules_csv_norm = ",".join(split_modules(modules_csv))

                rows.append({
                    "work_date": work_date,
                    "process": process,
                    "process_norm": proc_norm,
                    "qty": qty,
                    "modules_csv": modules_csv_norm,
                    "remark": remark,
                    "model": raw_model,
                    "model_code": raw_model,
                    "product_model": raw_model,
                })

            wb.close()

        except Exception:
            # 특정 파일이 손상되었거나 열려 있어도 다른 파일은 계속 읽습니다.
            continue

    return rows
'''

pms = pms[:start_index] + new_load_rows + pms[end_index:]
pms_path.write_text(pms, encoding="utf-8")
print("[수정 완료] pms_service.py")

# =========================================================
# process_tab.py 수정
# =========================================================
process_path = FILES["process"]
process = process_path.read_text(encoding="utf-8")

insert_anchor = "    def _load_pms_rows_for_daily_view(\n"

if insert_anchor not in process:
    raise RuntimeError("process_tab.py의 _load_pms_rows_for_daily_view 위치를 찾지 못했습니다.")

resolve_method = '''    def _resolve_pms_row_model(self, row: Dict[str, Any]) -> str:
        """
        PMS 한 행의 모델을 판정합니다.

        판정 순서:
        1. Excel 모델 열이 있으면 모델 열 사용
        2. 모델 열이 없으면 모듈번호로 DB 조회
        3. 하나라도 미등록이면 빈 문자열
        4. 여러 모델이 섞이면 빈 문자열
        5. 모든 모듈 모델이 같을 때만 해당 모델 반환
        """
        raw_model = (
            row.get("model")
            or row.get("model_code")
            or row.get("product_model")
            or ""
        )

        explicit_model = str(raw_model).strip()

        if explicit_model:
            return normalize_model_code(explicit_model)

        modules = _split_modules(
            row.get("modules_csv")
            or row.get("Module")
            or ""
        )

        if not modules:
            return ""

        if hasattr(self.repo, "get_module_models"):
            try:
                model_map = self.repo.get_module_models(modules)
            except Exception:
                model_map = {}
        else:
            model_map = {}

            for module_no in modules:
                if not hasattr(self.repo, "get_module_model"):
                    continue

                try:
                    module_model = self.repo.get_module_model(module_no)
                except Exception:
                    module_model = ""

                if module_model:
                    model_map[module_no] = module_model

        # 하나라도 DB에 등록되지 않은 모듈이 있으면 판정하지 않습니다.
        if len(model_map) != len(modules):
            return ""

        models = {
            normalize_model_code(model)
            for model in model_map.values()
            if str(model).strip()
        }

        # 한 행에 여러 모델이 섞인 경우 해당 행을 특정 모델로 표시하지 않습니다.
        if len(models) != 1:
            return ""

        return next(iter(models))

'''

process = process.replace(insert_anchor, resolve_method + insert_anchor, 1)

old_model_block = '''            # PMS 파일의 모델 컬럼을 우선 확인합니다.
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
'''

new_model_block = '''            # 신형 Excel은 모델 열을 사용하고,
            # 구형 Excel은 모듈번호로 DB 모델을 역조회합니다.
            row_model = self._resolve_pms_row_model(r)

            if not row_model:
                continue

            if row_model != model:
                continue
'''

if old_model_block not in process:
    raise RuntimeError("process_tab.py의 기존 PMS 모델 판정 블록을 찾지 못했습니다.")

process = process.replace(old_model_block, new_model_block, 1)

# pms_service.get_pms_dir() 호출 오류를 방지합니다.
process = process.replace(
    "pms_dir = pms_service.get_pms_dir()",
    "pms_dir = pms_service.ensure_pms_dir()",
)

process_path.write_text(process, encoding="utf-8")
print("[수정 완료] process_tab.py")

print()
print("==============================================")
print("PMS 모델 매핑 수정이 완료되었습니다.")
print("백업 폴더:", backup_dir)
print("==============================================")
