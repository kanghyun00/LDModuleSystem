# -*- coding: utf-8 -*-
# src/ldms/db/repo.py
from __future__ import annotations

import sqlite3
import re
import unicodedata
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .schema import init_db
from src.ldms.process_flow import DEFAULT_MODEL_CODE, normalize_model_code


# =========================
# ✅ Normalization
# =========================
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
    # pms_service.normalize_process_name와 동일 정책(공백 제거)
    if not name:
        return ""
    return "".join(str(name).strip().split())


class DBRepo:
    """
    ✅ 음수재고 기본 차단 정책
    - allow_negative=False (기본)
    - InventoryTab에서 강확인으로 음수 허용이 필요하면, allow_negative=True로만 통과시키는 API를 따로 쓰게끔(추후 확장 가능)
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._initialized = False

    def _ensure_init(self, conn: sqlite3.Connection) -> None:
        if not self._initialized:
            init_db(conn)
            self._initialized = True

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            self._ensure_init(conn)
            yield conn
        finally:
            conn.close()

    # =========================
    # internal helpers (inventory)
    # =========================
    def _get_onhand_conn(self, conn: sqlite3.Connection, code: str) -> float:
        row = conn.execute(
            "SELECT qty_on_hand FROM inventory WHERE material_code = ? LIMIT 1",
            (code,),
        ).fetchone()
        return float(row["qty_on_hand"]) if row and row["qty_on_hand"] is not None else 0.0

    def _ensure_material_inventory_conn(self, conn: sqlite3.Connection, code: str) -> None:
        conn.execute("INSERT OR IGNORE INTO materials(code, name) VALUES(?, ?)", (code, code))
        conn.execute("INSERT OR IGNORE INTO inventory(material_code, qty_on_hand) VALUES(?, 0)", (code,))

    def _apply_inventory_delta_conn(
        self,
        conn: sqlite3.Connection,
        code: str,
        delta: float,
        reason: str,
        ref_type: str,
        ref_id: Optional[int],
        note: str,
        allow_negative: bool = False,
    ) -> None:
        """
        ✅ 재고 증감 + 원장(inventory_txn) 기록
        ✅ (추가) qty_before / qty_after 기록
        """
        self._ensure_material_inventory_conn(conn, code)

        before = self._get_onhand_conn(conn, code)
        after = before + float(delta)

        if (not allow_negative) and after < 0:
            raise ValueError(
                f"[음수재고 차단] {code} 현재고={before} 에서 Δ={delta} 적용 시 {after} (음수)\n"
                f"- 원인: BOM 과다/qty 오입력/재고 미입고 등\n"
                f"- 해결: 입고 반영 또는 BOM/qty 확인 후 재시도"
            )

        # 1) 스냅샷(inventory) 반영
        conn.execute(
            """
            UPDATE inventory
               SET qty_on_hand = COALESCE(qty_on_hand,0) + ?,
                   updated_at = datetime('now','localtime')
             WHERE material_code = ?
            """,
            (float(delta), code),
        )

        # 2) 원장(inventory_txn) 기록 (✅ before/after 포함)
        #    - schema.py에서 qty_before/qty_after 컬럼 추가되어 있어야 함
        conn.execute(
            """
            INSERT INTO inventory_txn(
                material_code, qty_delta, qty_before, qty_after,
                reason, ref_type, ref_id, note, ts
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            """,
            (
                code,
                float(delta),
                float(before),
                float(after),
                reason or "",
                ref_type or "",
                int(ref_id) if ref_id is not None else None,
                note or "",
            ),
        )

    # =========================
    # modules
    # =========================
    def upsert_module(self, module_no: str, model: str = "", lot: str = "", customer: str = "") -> None:
        module_no = normalize_module_no(module_no)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)
        if not module_no:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO modules(module_no, model, lot, customer)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(module_no) DO UPDATE SET
                  model=excluded.model,
                  lot=excluded.lot,
                  customer=excluded.customer
                """,
                (module_no, model, lot, customer),
            )
            conn.commit()

    def get_module_model(self, module_no: str) -> str:
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

    def set_module_model(self, module_no: str, model: str) -> None:
        self.upsert_module(module_no=module_no, model=model or DEFAULT_MODEL_CODE)

    # =========================
    # module_status
    # =========================
    def set_module_status(self, module_no: str, status: str = "OK", reason: str = "") -> None:
        module_no = normalize_module_no(module_no)
        status = (status or "OK").strip().upper()
        if status not in ("OK", "NG", "SCRAP"):
            status = "OK"
        reason = "" if reason is None else str(reason).strip()

        if not module_no:
            return

        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO modules(module_no) VALUES (?)", (module_no,))
            conn.execute(
                """
                INSERT INTO module_status(module_no, status, reason, updated_at)
                VALUES(?, ?, ?, datetime('now','localtime'))
                ON CONFLICT(module_no) DO UPDATE SET
                  status=excluded.status,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at
                """,
                (module_no, status, reason),
            )
            conn.commit()

    def get_module_status(self, module_no: str) -> Tuple[str, str]:
        module_no = normalize_module_no(module_no)
        if not module_no:
            return ("OK", "")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, reason FROM module_status WHERE module_no = ? LIMIT 1",
                (module_no,),
            ).fetchone()
            if not row:
                return ("OK", "")
            return (str(row["status"] or "OK"), str(row["reason"] or ""))

    def get_module_status_map(self) -> Dict[str, Dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT module_no, status, reason FROM module_status").fetchall()
            out: Dict[str, Dict[str, str]] = {}
            for r in rows:
                m = normalize_module_no(r["module_no"])
                out[m] = {"status": str(r["status"] or "OK"), "reason": str(r["reason"] or "")}
            return out

    # =========================
    # process_logs (기본)
    # =========================
    def insert_process_log(
        self,
        work_date: str,
        process_name: str,
        process_name_raw: str = "",
        qty: int = 0,
        remark: str = "",
        operator: str = "",
        module_nos: Optional[List[str]] = None,
        model: str = DEFAULT_MODEL_CODE,
    ) -> int:
        module_nos = module_nos or []
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO process_logs(work_date, process_name, process_name_raw, model, qty, remark, operator)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (work_date, process_name, process_name_raw, model, int(qty), remark, operator),
            )
            log_id = int(cur.lastrowid)

            for m in module_nos:
                m = normalize_module_no(m)
                if not m:
                    continue
                conn.execute(
                    """
                    INSERT INTO modules(module_no, model)
                    VALUES (?, ?)
                    ON CONFLICT(module_no) DO UPDATE SET
                      model = CASE
                        WHEN modules.model IS NULL OR TRIM(modules.model) = '' THEN excluded.model
                        ELSE modules.model
                      END
                    """,
                    (m, model),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO process_log_modules(process_log_id, module_no)
                    VALUES (?, ?)
                    """,
                    (log_id, m),
                )

            conn.commit()
            return log_id

    def update_process_log(
        self,
        process_log_id: int,
        work_date: str,
        process_name: str,
        process_name_raw: str = "",
        qty: int = 0,
        remark: str = "",
        operator: str = "",
        module_nos: Optional[List[str]] = None,
        model: str = DEFAULT_MODEL_CODE,
    ) -> None:
        module_nos = module_nos or []
        pid = int(process_log_id)
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE process_logs
                   SET work_date = ?,
                       process_name = ?,
                       process_name_raw = ?,
                       model = ?,
                       qty = ?,
                       remark = ?,
                       operator = ?
                 WHERE id = ?
                """,
                (work_date, process_name, process_name_raw, model, int(qty), remark, operator, pid),
            )

            conn.execute("DELETE FROM process_log_modules WHERE process_log_id = ?", (pid,))
            for m in module_nos:
                m = normalize_module_no(m)
                if not m:
                    continue
                conn.execute(
                    """
                    INSERT INTO modules(module_no, model)
                    VALUES (?, ?)
                    ON CONFLICT(module_no) DO UPDATE SET
                      model = CASE
                        WHEN modules.model IS NULL OR TRIM(modules.model) = '' THEN excluded.model
                        ELSE modules.model
                      END
                    """,
                    (m, model),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO process_log_modules(process_log_id, module_no)
                    VALUES (?, ?)
                    """,
                    (pid, m),
                )

            conn.commit()

    def get_process_logs_by_module(
        self,
        module_no: str,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """모듈번호별 공정 이력을 조회합니다.

        model이 전달되면 현재 선택 모델의 공정기록만 반환합니다.
        모델이 비어 있는 레거시 데이터는 UNKNOWN으로 반환하며,
        특정 모델로 임의 귀속하지 않습니다.
        """
        module_no = normalize_module_no(module_no)
        if not module_no:
            return []

        q = """
            SELECT
              pl.id,
              pl.work_date,
              pl.process_name,
              pl.process_name_raw,
              COALESCE(NULLIF(TRIM(pl.model), ''), 'UNKNOWN') AS model,
              pl.qty,
              pl.remark,
              pl.operator,
              pl.created_at
            FROM process_logs pl
            JOIN process_log_modules plm
              ON plm.process_log_id = pl.id
            WHERE plm.module_no = ?
        """
        args: List[Any] = [module_no]

        if model:
            model = normalize_model_code(model)
            q += """
                AND UPPER(
                    COALESCE(NULLIF(TRIM(pl.model), ''), 'UNKNOWN')
                ) = UPPER(?)
            """
            args.append(model)

        q += " ORDER BY pl.work_date ASC, pl.id ASC"

        with self.connect() as conn:
            rows = conn.execute(q, args).fetchall()
            return [dict(r) for r in rows]

    # =========================================================
    # ✅ [ADD] SearchTab(검색/비교) 모듈조회에서 쓰는 측정파일 목록
    # =========================================================
    def list_measurement_files(self, module_no: str) -> List[Dict[str, Any]]:
        """
        SearchTab에서 호출: self.repo.list_measurement_files(module_no)

        measurement_files 테이블 기준으로,
        해당 모듈의 import 이력 목록을 반환한다.

        반환 예:
          [{
            "id": 1,
            "module_no": "A001",
            "file_name": "...",
            "file_path": "...",
            "imported_at": "...",
            "stage": "...",
            "group_name": "..."
          }, ...]
        """
        module_no = normalize_module_no(module_no)
        if not module_no:
            return []

        with self.connect() as conn:
            # 컬럼 존재/호환을 위해 stage/group_name은 없을 수도 있으니 SELECT * 로 받고 dict로 처리
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM measurement_files
                    WHERE UPPER(module_no) = ?
                    ORDER BY imported_at DESC, id DESC
                    """,
                    (module_no.upper(),),
                ).fetchall()
            except Exception:
                # 테이블이 없거나 스키마가 다른 경우 안전하게 빈 리스트
                return []

            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                out.append({
                    "id": d.get("id"),
                    "module_no": d.get("module_no", module_no),
                    "file_name": d.get("file_name", ""),
                    "file_path": d.get("file_path", ""),
                    "imported_at": d.get("imported_at", ""),
                    "stage": d.get("stage", "") or "",
                    "group_name": d.get("group_name", "") or "",
                })
            return out
    # =========================================================
    # ✅ [ADD] SearchTab(검색/비교) Summary 포인트 조회
    # =========================================================
    def get_measurement_summary_points(self, measurement_file_id: int) -> List[Dict[str, Any]]:
        """
        SearchTab에서 호출:
          pts = self.repo.get_measurement_summary_points(mid)

        measurement_summary_points 테이블에서 summary 포인트 목록을 반환.
        (DB에 테이블이 없거나 컬럼이 다르면 빈 리스트 반환해서 크래시 방지)
        """
        mid = int(measurement_file_id)

        with self.connect() as conn:
            # 1) 최신 스키마(권장) 컬럼 기준
            try:
                rows = conn.execute(
                    """
                    SELECT
                      COALESCE(current,'')  AS current,
                      COALESCE(peak1,'')    AS peak1,
                      COALESCE(peak2,'')    AS peak2,
                      COALESCE(fwhm_nm,'')  AS fwhm_nm,
                      COALESCE(smsr,'')     AS smsr,
                      COALESCE(power,'')    AS power,
                      COALESCE(voltage,'')  AS voltage,
                      COALESCE(base_ch5,'') AS base_ch5,
                      COALESCE(pkg_ch3,'')  AS pkg_ch3,
                      COALESCE(fiber_ch2,'') AS fiber_ch2,
                      COALESCE(eff,'')      AS eff
                    FROM measurement_summary_points
                    WHERE measurement_file_id = ?
                    ORDER BY id ASC
                    """,
                    (mid,),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                # 테이블 없거나 컬럼명이 다르면 안전하게 빈 리스트
                return []
            except Exception:
                return []
    def list_process_logs(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """날짜와 모델 기준으로 공정기록을 조회합니다.

        model이 전달되면 반드시 해당 모델의 기록만 조회합니다.
        모델 정보가 없는 레거시 기록은 UNKNOWN으로 유지합니다.
        """
        q = """
            SELECT
              pl.id,
              pl.work_date,
              pl.process_name,
              pl.process_name_raw,
              COALESCE(NULLIF(TRIM(pl.model), ''), 'UNKNOWN') AS model,
              pl.qty,
              pl.remark,
              pl.operator,
              pl.created_at,
              (
                SELECT group_concat(module_no, ', ')
                FROM (
                  SELECT plm.module_no AS module_no
                  FROM process_log_modules plm
                  WHERE plm.process_log_id = pl.id
                  ORDER BY plm.module_no ASC
                )
              ) AS modules_csv
            FROM process_logs pl
            WHERE 1 = 1
        """
        args: List[Any] = []

        if date_from:
            q += " AND pl.work_date >= ?"
            args.append(date_from)

        if date_to:
            q += " AND pl.work_date <= ?"
            args.append(date_to)

        if model:
            model = normalize_model_code(model)
            q += """
                AND UPPER(
                    COALESCE(NULLIF(TRIM(pl.model), ''), 'UNKNOWN')
                ) = UPPER(?)
            """
            args.append(model)

        q += " ORDER BY pl.work_date DESC, pl.id DESC"

        with self.connect() as conn:
            rows = conn.execute(q, args).fetchall()
            return [dict(row) for row in rows]

    def delete_process_log(self, process_log_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM process_log_modules WHERE process_log_id = ?", (int(process_log_id),))
            conn.execute("DELETE FROM process_logs WHERE id = ?", (int(process_log_id),))
            conn.commit()

    def get_process_log(self, process_log_id: int):
        q = """
        SELECT
        pl.id, pl.work_date, pl.process_name, pl.process_name_raw,
        COALESCE(NULLIF(TRIM(pl.model), ''), 'UNKNOWN') AS model,
        pl.qty, pl.remark, pl.operator, pl.created_at,
        -- ✅ modules_csv: 항상 module_no 정렬된 상태로 반환
        (SELECT group_concat(module_no, ', ')
           FROM (
             SELECT plm.module_no AS module_no
             FROM process_log_modules plm
             WHERE plm.process_log_id = pl.id
             ORDER BY plm.module_no ASC
           )
        ) AS modules_csv
        FROM process_logs pl
        WHERE pl.id = ?
        """
        with self.connect() as conn:
            r = conn.execute(q, (int(process_log_id),)).fetchone()
            return dict(r) if r else None

    # =========================================================
    # ✅ Materials / BOM / Inventory / Usage
    # =========================================================
    # ---------- materials ----------
    def upsert_material(
        self,
        code: str,
        name: str,
        spec: str = "",
        unit: str = "EA",
        vendor: str = "",
        active: bool = True,
    ) -> None:
        code = (code or "").strip()
        name = (name or "").strip()
        if not code or not name:
            raise ValueError("material code/name required")
        spec = (spec or "").strip()
        unit = (unit or "EA").strip()
        vendor = (vendor or "").strip()
        active_i = 1 if bool(active) else 0

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO materials(code, name, spec, unit, vendor, active, created_at)
                VALUES(?,?,?,?,?,?, datetime('now','localtime'))
                ON CONFLICT(code) DO UPDATE SET
                  name=excluded.name,
                  spec=excluded.spec,
                  unit=excluded.unit,
                  vendor=excluded.vendor,
                  active=excluded.active
                """,
                (code, name, spec, unit, vendor, active_i),
            )
            conn.execute("INSERT OR IGNORE INTO inventory(material_code, qty_on_hand) VALUES(?, 0)", (code,))
            conn.commit()

    def list_materials(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            q = "SELECT code, name, spec, unit, vendor, active, created_at FROM materials"
            if not include_inactive:
                q += " WHERE active = 1"
            q += " ORDER BY code ASC"
            rows = conn.execute(q).fetchall()
            out = [dict(r) for r in rows]

        if out:
            return out

        # fallback (구버전)
        raw = ""
        try:
            raw = self.get_setting("materials_master_v1", "")
        except Exception:
            raw = ""

        if not raw:
            return []

        try:
            data = json.loads(raw)
            if isinstance(data, list):
                tmp = []
                for d in data:
                    if not isinstance(d, dict):
                        continue
                    if (not include_inactive) and (str(d.get("active", True)).lower() in ("0", "false", "no", "n")):
                        continue
                    tmp.append({
                        "code": str(d.get("code", "")).strip(),
                        "name": str(d.get("name", "")).strip(),
                        "spec": str(d.get("spec", "")).strip(),
                        "unit": str(d.get("unit", "EA")).strip() or "EA",
                        "vendor": str(d.get("vendor", "")).strip(),
                        "active": 1 if str(d.get("active", True)).lower() not in ("0", "false", "no", "n") else 0,
                        "created_at": "",
                    })
                tmp.sort(key=lambda x: x.get("code", ""))
                return tmp
        except Exception:
            return []

        return []

    def set_material_active(self, code: str, active: bool) -> None:
        code = (code or "").strip()
        if not code:
            return
        with self.connect() as conn:
            conn.execute("UPDATE materials SET active = ? WHERE code = ?", (1 if active else 0, code))
            conn.commit()

    # ---------- inventory ----------
    def get_inventory_qty(self, material_code: str) -> float:
        code = (material_code or "").strip()
        if not code:
            return 0.0
        with self.connect() as conn:
            row = conn.execute(
                "SELECT qty_on_hand FROM inventory WHERE material_code = ? LIMIT 1",
                (code,),
            ).fetchone()
            return float(row["qty_on_hand"]) if row and row["qty_on_hand"] is not None else 0.0

    def adjust_inventory(
        self,
        material_code: str,
        qty_delta: float,
        reason: str = "",
        ref_type: str = "",
        ref_id: Optional[int] = None,
        note: str = "",
        allow_negative: bool = False,   # ✅ 기본 False: 음수재고 차단
    ) -> None:
        code = (material_code or "").strip()
        if not code:
            raise ValueError("material_code required")

        delta = float(qty_delta)
        reason = (reason or "").strip()
        ref_type = (ref_type or "").strip()
        note = (note or "").strip()

        with self.connect() as conn:
            conn.execute("BEGIN")
            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=delta,
                reason=reason or "manual_adjust",
                ref_type=ref_type or "manual",
                ref_id=ref_id,
                note=note,
                allow_negative=bool(allow_negative),
            )
            conn.commit()

    def list_inventory(self, include_inactive: bool = True) -> List[Dict[str, Any]]:
        q = """
        SELECT
          m.code,
          m.name,
          m.spec,
          m.unit,
          m.vendor,
          m.active,
          COALESCE(i.qty_on_hand, 0) AS qty_on_hand,
          COALESCE(i.updated_at, '') AS updated_at
        FROM materials m
        LEFT JOIN inventory i ON i.material_code = m.code
        WHERE 1=1
        """
        if not include_inactive:
            q += " AND m.active = 1"
        q += " ORDER BY m.code ASC"

        with self.connect() as conn:
            rows = conn.execute(q).fetchall()
            return [dict(r) for r in rows]

    def list_inventory_txn(self, material_code: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        code = (material_code or "").strip()
        lim = int(limit) if limit else 200

        with self.connect() as conn:
            if code:
                rows = conn.execute(
                    """
                    SELECT id, ts, material_code, qty_delta,
                           COALESCE(qty_before, NULL) AS qty_before,
                           COALESCE(qty_after, NULL) AS qty_after,
                           reason, ref_type, ref_id, note
                    FROM inventory_txn
                    WHERE material_code = ?
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (code, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, ts, material_code, qty_delta,
                           COALESCE(qty_before, NULL) AS qty_before,
                           COALESCE(qty_after, NULL) AS qty_after,
                           reason, ref_type, ref_id, note
                    FROM inventory_txn
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
            return [dict(r) for r in rows]

    # ---------- process_bom ----------
    def upsert_process_bom(
        self,
        process_name: str,
        material_code: str,
        qty_per_unit: float,
        unit: str = "EA",
        active: bool = True,
        model: str = DEFAULT_MODEL_CODE,
    ) -> None:
        """
        ✅ 모델별 BOM 등록/수정.
        - 새 구조: model_process_bom 사용
        - 하위호환: F976370W는 기존 process_bom에도 같이 반영
        """
        process_name = normalize_process_name(process_name)
        material_code = (material_code or "").strip()
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        if not process_name or not material_code:
            raise ValueError("process_name/material_code required")

        unit = (unit or "EA").strip()
        active_i = 1 if bool(active) else 0
        qty_per_unit = float(qty_per_unit)

        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO materials(code, name) VALUES(?, ?)", (material_code, material_code))
            conn.execute("INSERT OR IGNORE INTO inventory(material_code, qty_on_hand) VALUES(?, 0)", (material_code,))

            conn.execute("""
                INSERT OR IGNORE INTO model_master(model_code, display_name)
                VALUES(?, ?)
            """, (model, model))

            conn.execute(
                """
                INSERT INTO model_process_bom(model_code, process_name, material_code, qty_per_unit, unit, active)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(model_code, process_name, material_code) DO UPDATE SET
                  qty_per_unit=excluded.qty_per_unit,
                  unit=excluded.unit,
                  active=excluded.active
                """,
                (model, process_name, material_code, qty_per_unit, unit, active_i),
            )

            # 하위호환: 기본 모델은 기존 공통 BOM에도 저장
            if model == DEFAULT_MODEL_CODE:
                conn.execute(
                    """
                    INSERT INTO process_bom(process_name, material_code, qty_per_unit, unit, active)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(process_name, material_code) DO UPDATE SET
                      qty_per_unit=excluded.qty_per_unit,
                      unit=excluded.unit,
                      active=excluded.active
                    """,
                    (process_name, material_code, qty_per_unit, unit, active_i),
                )

            conn.commit()

    def list_process_bom(
        self,
        process_name: str,
        include_inactive: bool = False,
        model: str = DEFAULT_MODEL_CODE,
    ) -> List[Dict[str, Any]]:
        """
        ✅ 모델별 BOM 조회.
        - model_process_bom 우선
        - 해당 모델 BOM이 비어있고 기본 모델이면 기존 process_bom fallback
        """
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)
        if not process_name:
            return []

        q = """
        SELECT
          pb.id, pb.model_code AS model, pb.process_name, pb.material_code,
          pb.qty_per_unit, pb.unit, pb.active,
          m.name AS material_name, m.spec AS material_spec, m.vendor AS material_vendor
        FROM model_process_bom pb
        LEFT JOIN materials m ON m.code = pb.material_code
        WHERE pb.model_code = ?
          AND pb.process_name = ?
        """
        args: List[Any] = [model, process_name]
        if not include_inactive:
            q += " AND pb.active = 1"
        q += " ORDER BY pb.material_code ASC"

        with self.connect() as conn:
            rows = conn.execute(q, args).fetchall()
            out = [dict(r) for r in rows]

            if out or model != DEFAULT_MODEL_CODE:
                return out

            # 기본 모델 fallback: 기존 process_bom
            q2 = """
            SELECT
              pb.id, ? AS model, pb.process_name, pb.material_code,
              pb.qty_per_unit, pb.unit, pb.active,
              m.name AS material_name, m.spec AS material_spec, m.vendor AS material_vendor
            FROM process_bom pb
            LEFT JOIN materials m ON m.code = pb.material_code
            WHERE pb.process_name = ?
            """
            args2: List[Any] = [model, process_name]
            if not include_inactive:
                q2 += " AND pb.active = 1"
            q2 += " ORDER BY pb.material_code ASC"
            rows2 = conn.execute(q2, args2).fetchall()
            return [dict(r) for r in rows2]

    def set_process_bom_active(
        self,
        process_name: str,
        material_code: str,
        active: bool,
        model: str = DEFAULT_MODEL_CODE,
    ) -> None:
        process_name = normalize_process_name(process_name)
        material_code = (material_code or "").strip()
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)
        if not process_name or not material_code:
            return

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE model_process_bom
                   SET active = ?
                 WHERE model_code = ?
                   AND process_name = ?
                   AND material_code = ?
                """,
                (1 if active else 0, model, process_name, material_code),
            )
            if model == DEFAULT_MODEL_CODE:
                conn.execute(
                    "UPDATE process_bom SET active = ? WHERE process_name = ? AND material_code = ?",
                    (1 if active else 0, process_name, material_code),
                )
            conn.commit()

    # ---------- material_usage ----------
    def list_material_usage_by_process_log(self, process_log_id: int) -> List[Dict[str, Any]]:
        pid = int(process_log_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT mu.id, mu.work_date, mu.process_name, mu.material_code,
                       mu.qty_used, mu.unit, mu.operator, mu.created_at,
                       COALESCE(mu.usage_type,'BOM') AS usage_type,
                       COALESCE(mu.reason,'') AS reason,
                       COALESCE(mu.note,'') AS note,
                       m.name AS material_name, m.spec AS material_spec, m.vendor AS material_vendor
                FROM material_usage mu
                LEFT JOIN materials m ON m.code = mu.material_code
                WHERE mu.process_log_id = ?
                ORDER BY mu.id ASC
                """,
                (pid,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_material_usage(self, process_log_id: int) -> List[Dict[str, Any]]:
        return self.list_material_usage_by_process_log(process_log_id)

    def _get_usage_rows(self, conn: sqlite3.Connection, process_log_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, material_code, qty_used, unit,
                   COALESCE(usage_type,'BOM') AS usage_type,
                   COALESCE(reason,'') AS reason,
                   COALESCE(note,'') AS note,
                   COALESCE(operator,'') AS operator
            FROM material_usage
            WHERE process_log_id = ?
            ORDER BY id ASC
            """,
            (int(process_log_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def _revert_usage(self, conn: sqlite3.Connection, process_log_id: int, note_prefix: str = "") -> None:
        rows = self._get_usage_rows(conn, process_log_id)
        for r in rows:
            code = str(r.get("material_code") or "").strip()
            qty_used = float(r.get("qty_used") or 0.0)
            if not code or abs(qty_used) < 1e-15:
                continue

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=+qty_used,
                reason="usage_revert",
                ref_type="process_log",
                ref_id=int(process_log_id),
                note=(note_prefix + " revert usage").strip(),
                allow_negative=False,
            )

        conn.execute("DELETE FROM material_usage WHERE process_log_id = ?", (int(process_log_id),))

    def _compute_bom_required(
        self,
        conn: sqlite3.Connection,
        process_name: str,
        qty: int,
        model: str = DEFAULT_MODEL_CODE,
    ) -> List[Tuple[str, float, str]]:
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        bom_rows = conn.execute(
            """
            SELECT material_code, qty_per_unit, unit
            FROM model_process_bom
            WHERE model_code = ?
              AND process_name = ?
              AND active = 1
            ORDER BY material_code ASC
            """,
            (model, process_name),
        ).fetchall()

        # 기본 모델만 기존 process_bom fallback
        if not bom_rows and model == DEFAULT_MODEL_CODE:
            bom_rows = conn.execute(
                """
                SELECT material_code, qty_per_unit, unit
                FROM process_bom
                WHERE process_name = ?
                  AND active = 1
                ORDER BY material_code ASC
                """,
                (process_name,),
            ).fetchall()

        out: List[Tuple[str, float, str]] = []
        for b in bom_rows:
            code = str(b["material_code"] or "").strip()
            per = float(b["qty_per_unit"] or 0.0)
            unit = str(b["unit"] or "EA").strip() or "EA"
            if not code or abs(per) < 1e-15:
                continue
            required = float(per) * float(int(qty))
            if abs(required) < 1e-15:
                continue
            out.append((code, required, unit))
        return out

    def preview_bom_shortage(
        self,
        process_name: str,
        qty: int,
        model: str = DEFAULT_MODEL_CODE,
    ) -> List[Dict[str, Any]]:
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)
        qty_i = int(qty)
        if not process_name or qty_i <= 0:
            return []

        with self.connect() as conn:
            reqs = self._compute_bom_required(conn, process_name, qty_i, model=model)
            bad = []
            for code, required, _unit in reqs:
                self._ensure_material_inventory_conn(conn, code)
                on = self._get_onhand_conn(conn, code)
                after = on - float(required)
                if after < 0:
                    bad.append({
                        "material_code": code,
                        "required": float(required),
                        "on_hand": float(on),
                        "after": float(after),
                    })
            return bad

    def _apply_bom_usage(
        self,
        conn: sqlite3.Connection,
        process_log_id: int,
        work_date: str,
        process_name: str,
        qty: int,
        operator: str = "",
        allow_negative: bool = False,
        model: str = DEFAULT_MODEL_CODE,
    ) -> None:
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)
        qty_i = int(qty)

        reqs = self._compute_bom_required(conn, process_name, qty_i, model=model)

        for code, required, _unit in reqs:
            self._ensure_material_inventory_conn(conn, code)
            on = self._get_onhand_conn(conn, code)
            after = on - float(required)
            if (not allow_negative) and after < 0:
                raise ValueError(
                    f"[BOM 음수재고 차단] 모델={model}, 공정={process_name}, qty={qty_i}\n"
                    f"- 자재 {code}: 현재고={on} / 필요={required} / 적용후={after}\n"
                    f"→ 입고 반영 또는 BOM/qty 확인 필요"
                )

        for code, required, unit in reqs:
            conn.execute(
                """
                INSERT INTO material_usage(
                    process_log_id, work_date, process_name,
                    material_code, qty_used, unit, operator,
                    usage_type, reason, note,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
                """,
                (
                    int(process_log_id),
                    work_date,
                    process_name,
                    code,
                    float(required),
                    unit,
                    operator or "",
                    "BOM",
                    "BOM",
                    "",
                ),
            )

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=-float(required),
                reason="usage_apply",
                ref_type="process_log",
                ref_id=int(process_log_id),
                note=f"{process_name} x{qty_i}",
                allow_negative=bool(allow_negative),
            )

    def _apply_manual_usage_rows(
        self,
        conn: sqlite3.Connection,
        process_log_id: int,
        work_date: str,
        process_name: str,
        rows: List[Dict[str, Any]],
        allow_negative: bool = False,
    ) -> None:
        process_name = normalize_process_name(process_name)

        for r in rows:
            code = str(r.get("material_code") or "").strip()
            used = float(r.get("qty_used") or 0.0)
            if not code or abs(used) < 1e-15:
                continue

            delta = -used
            self._ensure_material_inventory_conn(conn, code)
            on = self._get_onhand_conn(conn, code)
            after = on + float(delta)
            if (not allow_negative) and after < 0:
                raise ValueError(
                    f"[MANUAL 음수재고 차단] {code} 현재고={on}, 적용Δ={delta} → {after}\n"
                    f"(수동 사용량/입고/원복 확인 필요)"
                )

        for r in rows:
            code = str(r.get("material_code") or "").strip()
            used = float(r.get("qty_used") or 0.0)
            unit = str(r.get("unit") or "EA").strip() or "EA"
            operator = str(r.get("operator") or "").strip()
            reason = str(r.get("reason") or "").strip()
            note = str(r.get("note") or "").strip()

            if not code or abs(used) < 1e-15:
                continue

            conn.execute(
                """
                INSERT INTO material_usage(
                    process_log_id, work_date, process_name,
                    material_code, qty_used, unit, operator,
                    usage_type, reason, note,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
                """,
                (
                    int(process_log_id),
                    work_date,
                    process_name,
                    code,
                    used,
                    unit,
                    operator,
                    "MANUAL",
                    reason,
                    note,
                ),
            )

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=-used,
                reason="manual_usage_reapply",
                ref_type="process_log",
                ref_id=int(process_log_id),
                note=note or "reapply manual usage",
                allow_negative=bool(allow_negative),
            )

    def add_manual_usage(
        self,
        process_log_id: int,
        material_code: str,
        qty_used: float,
        reason: str = "EXTRA",
        note: str = "",
        unit: str = "EA",
        operator: str = "",
        allow_negative: bool = False,
    ) -> None:
        pid = int(process_log_id)
        code = (material_code or "").strip()
        used = float(qty_used)
        reason = (reason or "").strip()
        note = (note or "").strip()
        unit = (unit or "EA").strip() or "EA"
        operator = (operator or "").strip()

        if not code:
            raise ValueError("material_code required")
        if used <= 0:
            raise ValueError("qty_used must be > 0 (추가 사용/불량/재작업은 양수로 입력)")

        with self.connect() as conn:
            conn.execute("BEGIN")

            pl = conn.execute(
                "SELECT work_date, process_name, operator FROM process_logs WHERE id = ? LIMIT 1",
                (pid,),
            ).fetchone()
            if not pl:
                raise ValueError("process_log not found")

            work_date = str(pl["work_date"] or "")
            process_name = normalize_process_name(str(pl["process_name"] or ""))
            op = operator or str(pl["operator"] or "")

            self._ensure_material_inventory_conn(conn, code)

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=-used,
                reason="manual_usage",
                ref_type="process_log",
                ref_id=pid,
                note=f"{process_name} | {reason} | {note}".strip(),
                allow_negative=bool(allow_negative),
            )

            conn.execute(
                """
                INSERT INTO material_usage(
                    process_log_id, work_date, process_name,
                    material_code, qty_used, unit, operator,
                    usage_type, reason, note,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
                """,
                (pid, work_date, process_name, code, used, unit, op, "MANUAL", reason, note),
            )

            conn.commit()

    def add_manual_return(
        self,
        process_log_id: int,
        material_code: str,
        qty_return: float,
        reason: str = "RETURN",
        note: str = "",
        unit: str = "EA",
        operator: str = "",
        allow_negative: bool = False,
    ) -> None:
        pid = int(process_log_id)
        code = (material_code or "").strip()
        qty = float(qty_return)
        reason = (reason or "RETURN").strip()
        note = (note or "").strip()
        unit = (unit or "EA").strip() or "EA"
        operator = (operator or "").strip()

        if not code:
            raise ValueError("material_code required")
        if qty <= 0:
            raise ValueError("qty_return must be > 0")

        with self.connect() as conn:
            conn.execute("BEGIN")

            pl = conn.execute(
                "SELECT work_date, process_name, operator FROM process_logs WHERE id = ? LIMIT 1",
                (pid,),
            ).fetchone()
            if not pl:
                raise ValueError("process_log not found")

            work_date = str(pl["work_date"] or "")
            process_name = normalize_process_name(str(pl["process_name"] or ""))
            op = operator or str(pl["operator"] or "")

            self._ensure_material_inventory_conn(conn, code)

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=+qty,
                reason="manual_return",
                ref_type="process_log",
                ref_id=pid,
                note=f"{process_name} | {reason} | {note}".strip(),
                allow_negative=bool(allow_negative),
            )

            conn.execute(
                """
                INSERT INTO material_usage(
                    process_log_id, work_date, process_name,
                    material_code, qty_used, unit, operator,
                    usage_type, reason, note,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
                """,
                (pid, work_date, process_name, code, -qty, unit, op, "MANUAL", reason, note),
            )

            conn.commit()

    def update_manual_usage(
        self,
        usage_id: int,
        new_qty_used: float,
        new_reason: str = "",
        new_note: str = "",
        allow_negative: bool = False,
    ) -> None:
        uid = int(usage_id)
        new_used = float(new_qty_used)
        if abs(new_used) < 1e-15:
            raise ValueError("new_qty_used cannot be 0")

        with self.connect() as conn:
            conn.execute("BEGIN")

            row = conn.execute(
                """
                SELECT id, process_log_id, work_date, process_name, material_code, qty_used, unit, operator, usage_type
                FROM material_usage
                WHERE id = ?
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
            if not row:
                raise ValueError("usage not found")

            if str(row["usage_type"] or "").upper() != "MANUAL":
                raise ValueError("BOM usage는 수정 대상이 아닙니다(MANUAL만 수정 가능)")

            pid = int(row["process_log_id"])
            code = str(row["material_code"] or "").strip()
            old_used = float(row["qty_used"] or 0.0)

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=+old_used,
                reason="manual_usage_edit_revert",
                ref_type="process_log",
                ref_id=pid,
                note="edit manual usage revert",
                allow_negative=False,
            )

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=-new_used,
                reason="manual_usage_edit_apply",
                ref_type="process_log",
                ref_id=pid,
                note="edit manual usage apply",
                allow_negative=bool(allow_negative),
            )

            conn.execute(
                """
                UPDATE material_usage
                   SET qty_used = ?,
                       reason = ?,
                       note = ?,
                       created_at = datetime('now','localtime')
                 WHERE id = ?
                """,
                (new_used, (new_reason or "").strip(), (new_note or "").strip(), uid),
            )

            conn.commit()

    def delete_manual_usage(self, usage_id: int) -> None:
        uid = int(usage_id)
        with self.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                """
                SELECT id, process_log_id, material_code, qty_used, usage_type
                FROM material_usage
                WHERE id = ?
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
            if not row:
                raise ValueError("usage not found")

            if str(row["usage_type"] or "").upper() != "MANUAL":
                raise ValueError("BOM usage는 삭제 대상이 아닙니다(MANUAL만 삭제 가능)")

            pid = int(row["process_log_id"])
            code = str(row["material_code"] or "").strip()
            used = float(row["qty_used"] or 0.0)

            self._apply_inventory_delta_conn(
                conn,
                code=code,
                delta=+used,
                reason="manual_usage_delete",
                ref_type="process_log",
                ref_id=pid,
                note="delete manual usage",
                allow_negative=False,
            )

            conn.execute("DELETE FROM material_usage WHERE id = ?", (uid,))
            conn.commit()

    def insert_process_log_with_materials(
        self,
        work_date: str,
        process_name: str,
        process_name_raw: str = "",
        qty: int = 0,
        remark: str = "",
        operator: str = "",
        module_nos: Optional[List[str]] = None,
        allow_negative: bool = False,
        model: str = DEFAULT_MODEL_CODE,
    ) -> int:
        module_nos = module_nos or []
        qty_i = int(qty)
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        with self.connect() as conn:
            conn.execute("BEGIN")

            cur = conn.execute(
                """
                INSERT INTO process_logs(work_date, process_name, process_name_raw, model, qty, remark, operator)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (work_date, process_name, process_name_raw, model, qty_i, remark, operator),
            )
            log_id = int(cur.lastrowid)

            for m in module_nos:
                m = normalize_module_no(m)
                if not m:
                    continue
                conn.execute(
                    """
                    INSERT INTO modules(module_no, model)
                    VALUES (?, ?)
                    ON CONFLICT(module_no) DO UPDATE SET
                      model = CASE
                        WHEN modules.model IS NULL OR TRIM(modules.model) = '' THEN excluded.model
                        ELSE modules.model
                      END
                    """,
                    (m, model),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO process_log_modules(process_log_id, module_no)
                    VALUES (?, ?)
                    """,
                    (log_id, m),
                )

            self._apply_bom_usage(conn, log_id, work_date, process_name, qty_i, operator=operator, allow_negative=allow_negative, model=model)

            conn.commit()
            return log_id

    def update_process_log_with_materials(
        self,
        process_log_id: int,
        work_date: str,
        process_name: str,
        process_name_raw: str = "",
        qty: int = 0,
        remark: str = "",
        operator: str = "",
        module_nos: Optional[List[str]] = None,
        allow_negative: bool = False,
        model: str = DEFAULT_MODEL_CODE,
    ) -> None:
        module_nos = module_nos or []
        pid = int(process_log_id)
        qty_i = int(qty)
        process_name = normalize_process_name(process_name)
        model = normalize_model_code(model or DEFAULT_MODEL_CODE)

        with self.connect() as conn:
            conn.execute("BEGIN")

            prev_usage = self._get_usage_rows(conn, pid)
            manual_rows = [u for u in prev_usage if str(u.get("usage_type") or "BOM").upper() == "MANUAL"]

            self._revert_usage(conn, pid, note_prefix="update")

            conn.execute(
                """
                UPDATE process_logs
                   SET work_date = ?,
                       process_name = ?,
                       process_name_raw = ?,
                       model = ?,
                       qty = ?,
                       remark = ?,
                       operator = ?
                 WHERE id = ?
                """,
                (work_date, process_name, process_name_raw, model, qty_i, remark, operator, pid),
            )

            conn.execute("DELETE FROM process_log_modules WHERE process_log_id = ?", (pid,))
            for m in module_nos:
                m = normalize_module_no(m)
                if not m:
                    continue
                conn.execute(
                    """
                    INSERT INTO modules(module_no, model)
                    VALUES (?, ?)
                    ON CONFLICT(module_no) DO UPDATE SET
                      model = CASE
                        WHEN modules.model IS NULL OR TRIM(modules.model) = '' THEN excluded.model
                        ELSE modules.model
                      END
                    """,
                    (m, model),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO process_log_modules(process_log_id, module_no)
                    VALUES (?, ?)
                    """,
                    (pid, m),
                )

            self._apply_bom_usage(conn, pid, work_date, process_name, qty_i, operator=operator, allow_negative=allow_negative, model=model)

            if manual_rows:
                self._apply_manual_usage_rows(conn, pid, work_date, process_name, manual_rows, allow_negative=allow_negative)

            conn.commit()

    def delete_process_log_with_materials(self, process_log_id: int) -> None:
        pid = int(process_log_id)
        with self.connect() as conn:
            conn.execute("BEGIN")

            self._revert_usage(conn, pid, note_prefix="delete")

            conn.execute("DELETE FROM process_log_modules WHERE process_log_id = ?", (pid,))
            conn.execute("DELETE FROM process_logs WHERE id = ?", (pid,))

            conn.commit()


    def backfill_legacy_model_data(
        self,
        default_model: str = DEFAULT_MODEL_CODE,
    ) -> int:
        """레거시 모델 정보를 임의로 변경하지 않습니다.

        모델이 없는 기록을 기본 모델로 변경하면 45W 기록이 370W로
        오인될 수 있으므로 자동 보정은 수행하지 않습니다.
        실제 모델을 확인한 뒤 별도의 명시적 마이그레이션으로 처리해야 합니다.
        """
        return 0


    # =========================
    # model master / recipe
    # =========================
    def list_models(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        q = """
        SELECT model_code, display_name, max_current_a, pbs_step_a,
               pbs_points_csv, vbg_points_csv, burnin_current_a,
               COALESCE(beam_check_current_a, 0.8) AS beam_check_current_a,
               COALESCE(measure_step_wait_s, 10) AS measure_step_wait_s,
               COALESCE(max_hold_wait_s, 15) AS max_hold_wait_s,
               active, note, created_at, updated_at
        FROM model_master
        WHERE 1=1
        """
        if not include_inactive:
            q += " AND active = 1"
        q += " ORDER BY model_code ASC"
        with self.connect() as conn:
            rows = conn.execute(q).fetchall()
            return [dict(r) for r in rows]

    def get_model(self, model_code: str) -> Dict[str, Any]:
        model_code = normalize_model_code(model_code or DEFAULT_MODEL_CODE)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT model_code, display_name, max_current_a, pbs_step_a,
                       pbs_points_csv, vbg_points_csv, burnin_current_a,
                       COALESCE(beam_check_current_a, 0.8) AS beam_check_current_a,
                       COALESCE(measure_step_wait_s, 10) AS measure_step_wait_s,
                       COALESCE(max_hold_wait_s, 15) AS max_hold_wait_s,
                       active, note, created_at, updated_at
                FROM model_master
                WHERE model_code = ?
                LIMIT 1
                """,
                (model_code,),
            ).fetchone()
            if row:
                return dict(row)

            # DB에 없으면 기본 370W로 fallback
            row = conn.execute(
                "SELECT * FROM model_master WHERE model_code = ? LIMIT 1",
                (DEFAULT_MODEL_CODE,),
            ).fetchone()
            return dict(row) if row else {
                "model_code": DEFAULT_MODEL_CODE,
                "display_name": "F976370W / 370W",
                "max_current_a": 20.0,
                "pbs_step_a": 1.0,
                "pbs_points_csv": ",".join(str(x) for x in range(1, 21)),
                "vbg_points_csv": "2,5,10,15,16,17,20",
                "burnin_current_a": 20.0,
                "beam_check_current_a": 0.8,
                "measure_step_wait_s": 10.0,
                "max_hold_wait_s": 15.0,
                "active": 1,
                "note": "",
            }

    def upsert_model(
        self,
        model_code: str,
        display_name: str = "",
        max_current_a: float = 20.0,
        pbs_step_a: float = 1.0,
        pbs_points_csv: str = "",
        vbg_points_csv: str = "",
        burnin_current_a: Optional[float] = None,
        beam_check_current_a: Optional[float] = None,
        measure_step_wait_s: float = 10.0,
        max_hold_wait_s: float = 15.0,
        active: bool = True,
        note: str = "",
    ) -> None:
        """
        모델 설정 저장.

        beam_check_current_a:
        - 값이 전달되면 해당 모델의 초기 빔 확인 전류를 저장한다.
        - None이면 신규 모델 생성 시 0.8A를 사용하고,
          기존 모델 업데이트 시에는 현재 DB 값을 유지한다.
        """
        model_code = normalize_model_code(model_code)
        if not model_code:
            raise ValueError("model_code required")
        if burnin_current_a is None:
            burnin_current_a = float(max_current_a)

        max_i = max(1, int(round(float(max_current_a))))
        step_i = max(1, int(round(float(pbs_step_a or 1.0))))
        if not pbs_points_csv:
            pbs_points = list(range(1, max_i + 1, step_i))
            if max_i not in pbs_points:
                pbs_points.append(max_i)
            pbs_points_csv = ",".join(str(x) for x in pbs_points)
        if not vbg_points_csv:
            vbg_points_csv = str(max_i)

        beam_value = 0.8 if beam_check_current_a is None else float(beam_check_current_a)
        beam_value_provided = 0 if beam_check_current_a is None else 1

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO model_master(
                    model_code, display_name, max_current_a, pbs_step_a,
                    pbs_points_csv, vbg_points_csv, burnin_current_a, beam_check_current_a,
                    measure_step_wait_s, max_hold_wait_s,
                    active, note, created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'), datetime('now','localtime'))
                ON CONFLICT(model_code) DO UPDATE SET
                  display_name=excluded.display_name,
                  max_current_a=excluded.max_current_a,
                  pbs_step_a=excluded.pbs_step_a,
                  pbs_points_csv=excluded.pbs_points_csv,
                  vbg_points_csv=excluded.vbg_points_csv,
                  burnin_current_a=excluded.burnin_current_a,
                  beam_check_current_a=CASE
                    WHEN ? = 1 THEN excluded.beam_check_current_a
                    ELSE model_master.beam_check_current_a
                  END,
                  measure_step_wait_s=excluded.measure_step_wait_s,
                  max_hold_wait_s=excluded.max_hold_wait_s,
                  active=excluded.active,
                  note=excluded.note,
                  updated_at=datetime('now','localtime')
                """,
                (
                    model_code,
                    display_name or model_code,
                    float(max_current_a),
                    float(pbs_step_a),
                    pbs_points_csv,
                    vbg_points_csv,
                    float(burnin_current_a),
                    beam_value,
                    float(measure_step_wait_s if measure_step_wait_s is not None else 10.0),
                    float(max_hold_wait_s if max_hold_wait_s is not None else 15.0),
                    1 if active else 0,
                    note or "",
                    beam_value_provided,
                ),
            )
            conn.commit()

    def get_model_process_flow(
        self,
        model_code: str,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        모델별 공정순서를 현재 DB 기준으로 반환한다.

        include_inactive=False:
            실제 진행현황/입력 후보에 사용하는 활성 공정만 반환.
        include_inactive=True:
            관리/보정 용도로 비활성 공정까지 반환.

        기존 호출 방식 get_model_process_flow(model_code)와도 완전히 호환된다.
        """
        model_code = normalize_model_code(model_code or DEFAULT_MODEL_CODE)
        q = """
            SELECT id, model_code, seq, process_name, process_name_raw, active
            FROM model_process_flow
            WHERE model_code = ?
        """
        args: List[Any] = [model_code]
        if not include_inactive:
            q += " AND active = 1"
        q += " ORDER BY seq ASC"

        with self.connect() as conn:
            rows = conn.execute(q, args).fetchall()
            return [dict(r) for r in rows]

    def replace_model_process_flow(self, model_code: str, process_names: List[str]) -> None:
        model_code = normalize_model_code(model_code or DEFAULT_MODEL_CODE)
        names = [normalize_process_name(x) for x in (process_names or []) if normalize_process_name(x)]
        if not names:
            raise ValueError("process_names is empty")

        with self.connect() as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM model_process_flow WHERE model_code = ?", (model_code,))
            conn.execute(
                """
                INSERT OR IGNORE INTO model_master(model_code, display_name)
                VALUES(?, ?)
                """,
                (model_code, model_code),
            )
            for idx, p in enumerate(names, start=1):
                conn.execute(
                    """
                    INSERT INTO model_process_flow(model_code, seq, process_name, process_name_raw, active)
                    VALUES(?,?,?,?,1)
                    """,
                    (model_code, idx, p, p),
                )
            conn.commit()

    def parse_model_pbs_points(self, model_code: str) -> List[float]:
        m = self.get_model(model_code)
        raw = str(m.get("pbs_points_csv") or "").replace(";", ",")
        out: List[float] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except Exception:
                pass

        max_a = float(m.get("max_current_a") or 20.0)
        if not out:
            step = max(1, int(round(float(m.get("pbs_step_a") or 1.0))))
            out = [float(x) for x in range(1, int(round(max_a)) + 1, step)]
        out = [x for x in out if 0 < x <= max_a]
        if max_a not in out:
            out.append(max_a)
        return sorted(set(out))

    def parse_model_vbg_points(self, model_code: str) -> List[float]:
        m = self.get_model(model_code)
        raw = str(m.get("vbg_points_csv") or "").replace(";", ",")
        out: List[float] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except Exception:
                pass
        max_a = float(m.get("max_current_a") or 20.0)
        out = [x for x in out if 0 < x <= max_a]
        if max_a not in out:
            out.append(max_a)
        return sorted(set(out))


    # =========================
    # settings
    # =========================
    def get_setting(self, key: str, default: str = "") -> str:
        key = (key or "").strip()
        if not key:
            return default
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ? LIMIT 1", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        key = (key or "").strip()
        if not key:
            return
        value = "" if value is None else str(value)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES(?, ?, datetime('now','localtime'))
                ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
                """,
                (key, value),
            )
            conn.commit()