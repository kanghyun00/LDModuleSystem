# -*- coding: utf-8 -*-
# src/ldms/db/schema.py
from __future__ import annotations

import sqlite3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 1) 모듈 마스터(완성품/기본정보)
CREATE TABLE IF NOT EXISTS modules (
  module_no TEXT PRIMARY KEY,
  model TEXT,
  lot TEXT,
  customer TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ✅ 모듈 상태(정상/불량/폐기)
CREATE TABLE IF NOT EXISTS module_status (
  module_no TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'OK',   -- OK / NG / SCRAP
  reason TEXT DEFAULT '',
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (module_no) REFERENCES modules(module_no)
);

-- 2) 공정 기록(헤더)
CREATE TABLE IF NOT EXISTS process_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_date TEXT NOT NULL,                -- YYYY-MM-DD
  process_name TEXT NOT NULL,             -- 정규화된 공정명(공백 제거)
  process_name_raw TEXT,                  -- 원문(표시용)
  model TEXT DEFAULT 'F976370W',            -- ✅ 모델명/출력모델
  qty INTEGER NOT NULL DEFAULT 0,
  remark TEXT,
  operator TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 3) 공정 기록 <-> 모듈번호 연결(N개)
CREATE TABLE IF NOT EXISTS process_log_modules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  process_log_id INTEGER NOT NULL,
  module_no TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (process_log_id) REFERENCES process_logs(id) ON DELETE CASCADE,
  FOREIGN KEY (module_no) REFERENCES modules(module_no),
  UNIQUE(process_log_id, module_no)
);

-- 4) 측정(모듈 기준)
CREATE TABLE IF NOT EXISTS measurements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  module_no TEXT NOT NULL,
  optic_step INTEGER DEFAULT 1,
  recipe TEXT,
  started_at TEXT,
  finished_at TEXT,
  judge TEXT,
  note TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (module_no) REFERENCES modules(module_no)
);

-- 5) 측정 상세 값 (Key-Value)
CREATE TABLE IF NOT EXISTS measurement_values (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  measurement_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  value REAL,
  unit TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY (measurement_id) REFERENCES measurements(id) ON DELETE CASCADE
);

-- =========================================================
-- ✅ 모델 마스터 / 모델별 공정순서 / 측정 Recipe
-- =========================================================
CREATE TABLE IF NOT EXISTS model_master (
  model_code TEXT PRIMARY KEY,
  display_name TEXT DEFAULT '',
  max_current_a REAL NOT NULL DEFAULT 20,
  pbs_step_a REAL NOT NULL DEFAULT 1,
  pbs_points_csv TEXT DEFAULT '',
  vbg_points_csv TEXT DEFAULT '2,5,10,15,16,17,20',
  burnin_current_a REAL NOT NULL DEFAULT 20,
  beam_check_current_a REAL NOT NULL DEFAULT 0.8,
  measure_step_wait_s REAL NOT NULL DEFAULT 10,
  max_hold_wait_s REAL NOT NULL DEFAULT 15,
  active INTEGER NOT NULL DEFAULT 1,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model_process_flow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_code TEXT NOT NULL,
  seq INTEGER NOT NULL,
  process_name TEXT NOT NULL,
  process_name_raw TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(model_code, seq),
  UNIQUE(model_code, process_name),
  FOREIGN KEY(model_code) REFERENCES model_master(model_code)
);


-- Index
CREATE INDEX IF NOT EXISTS idx_process_logs_date ON process_logs(work_date);
CREATE INDEX IF NOT EXISTS idx_process_logs_proc ON process_logs(process_name);
CREATE INDEX IF NOT EXISTS idx_plm_module ON process_log_modules(module_no);

CREATE INDEX IF NOT EXISTS idx_measurements_module ON measurements(module_no);
CREATE INDEX IF NOT EXISTS idx_measure_values_mid ON measurement_values(measurement_id);
CREATE INDEX IF NOT EXISTS idx_model_flow_model_seq ON model_process_flow(model_code, seq);

-- ✅ module_status index
CREATE INDEX IF NOT EXISTS idx_module_status_status ON module_status(status);

-- =========================================================
-- ✅ 자재/재고/공정 BOM/사용이력/재고트랜잭션
-- =========================================================

-- 자재 마스터
CREATE TABLE IF NOT EXISTS materials (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  spec TEXT DEFAULT '',
  unit TEXT DEFAULT 'EA',
  vendor TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 현재고(스냅샷)
CREATE TABLE IF NOT EXISTS inventory (
  material_code TEXT PRIMARY KEY,
  qty_on_hand REAL NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(material_code) REFERENCES materials(code)
);

-- 공정별 BOM(공정명 정규화 기준)
CREATE TABLE IF NOT EXISTS process_bom (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  process_name TEXT NOT NULL,
  material_code TEXT NOT NULL,
  qty_per_unit REAL NOT NULL DEFAULT 0,     -- qty 1당 소요량
  unit TEXT DEFAULT 'EA',
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(process_name, material_code),
  FOREIGN KEY(material_code) REFERENCES materials(code)
);

-- 사용이력(공정 로그와 연결)
CREATE TABLE IF NOT EXISTS material_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  process_log_id INTEGER NOT NULL,
  work_date TEXT NOT NULL,
  process_name TEXT NOT NULL,
  material_code TEXT NOT NULL,
  qty_used REAL NOT NULL,
  unit TEXT DEFAULT 'EA',
  operator TEXT DEFAULT '',
  usage_type TEXT NOT NULL DEFAULT 'BOM',   -- BOM / MANUAL
  reason TEXT DEFAULT '',                  -- EXTRA / SCRAP / REWORK / RETURN / ...
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  FOREIGN KEY(process_log_id) REFERENCES process_logs(id) ON DELETE CASCADE,
  FOREIGN KEY(material_code) REFERENCES materials(code)
);

-- 재고 변동(원장)
CREATE TABLE IF NOT EXISTS inventory_txn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  material_code TEXT NOT NULL,
  qty_delta REAL NOT NULL,                 -- 출고는 음수
  qty_before REAL,                         -- ✅ 추가(선택): 적용 전 재고
  qty_after REAL,                          -- ✅ 추가(선택): 적용 후 재고
  reason TEXT DEFAULT '',
  ref_type TEXT DEFAULT '',
  ref_id INTEGER,
  note TEXT DEFAULT '',
  FOREIGN KEY(material_code) REFERENCES materials(code)
);

-- Index
CREATE INDEX IF NOT EXISTS idx_bom_proc ON process_bom(process_name);
CREATE INDEX IF NOT EXISTS idx_usage_plid ON material_usage(process_log_id);
CREATE INDEX IF NOT EXISTS idx_txn_mat_ts ON inventory_txn(material_code, ts);

-- ✅ 선택 인덱스: 자재별 사용량 조회/집계가 많으면 도움
CREATE INDEX IF NOT EXISTS idx_usage_mat_date ON material_usage(material_code, work_date);
"""


def _table_cols(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _add_col_if_missing(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    cols = _table_cols(conn, table)
    if col in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def ensure_measurement_tables(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS measurement_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        module_no TEXT NOT NULL,
        stage TEXT DEFAULT '',
        group_name TEXT DEFAULT '',
        file_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        FOREIGN KEY (module_no) REFERENCES modules(module_no),
        UNIQUE(module_no, stage, group_name, file_name)
    )
    """)

    cols = [r[1] for r in cur.execute("PRAGMA table_info(measurement_files)").fetchall()]
    if "stage" not in cols:
        cur.execute("ALTER TABLE measurement_files ADD COLUMN stage TEXT DEFAULT ''")
    if "group_name" not in cols:
        cur.execute("ALTER TABLE measurement_files ADD COLUMN group_name TEXT DEFAULT ''")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS measurement_summary_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        measurement_id INTEGER NOT NULL,
        current REAL,
        peak1 REAL,
        peak2 REAL,
        fwhm_nm REAL,
        smsr REAL,
        power REAL,
        voltage REAL,
        base_ch5 REAL,
        pkg_ch3 REAL,
        fiber_ch2 REAL,
        eff REAL,
        FOREIGN KEY(measurement_id) REFERENCES measurement_files(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS measurement_spectra_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        measurement_id INTEGER NOT NULL,
        sheet_name TEXT NOT NULL,
        wavelength_nm REAL,
        power_dbm REAL,
        power_nw REAL,
        FOREIGN KEY(measurement_id) REFERENCES measurement_files(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_mfiles_module ON measurement_files(module_no);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mfiles_stage ON measurement_files(stage);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mfiles_group ON measurement_files(group_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msum_mid ON measurement_summary_points(measurement_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mspec_mid_sheet ON measurement_spectra_points(measurement_id, sheet_name);")

    conn.commit()


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """)
    conn.execute("""
    INSERT OR IGNORE INTO settings(key, value)
    VALUES ('grading_rules', '{}')
    """)


def _ensure_module_status_compat(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS module_status (
      module_no TEXT PRIMARY KEY,
      status TEXT NOT NULL DEFAULT 'OK',
      reason TEXT DEFAULT '',
      updated_at TEXT DEFAULT '',
      FOREIGN KEY (module_no) REFERENCES modules(module_no)
    )
    """)
    _add_col_if_missing(conn, "module_status", "reason", "reason TEXT DEFAULT ''")
    _add_col_if_missing(conn, "module_status", "updated_at", "updated_at TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_module_status_status ON module_status(status);")


def _ensure_material_tables_compat(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS materials (
      code TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      spec TEXT DEFAULT '',
      unit TEXT DEFAULT 'EA',
      vendor TEXT DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
      material_code TEXT PRIMARY KEY,
      qty_on_hand REAL NOT NULL DEFAULT 0,
      updated_at TEXT DEFAULT (datetime('now','localtime')),
      FOREIGN KEY(material_code) REFERENCES materials(code)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS process_bom (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      process_name TEXT NOT NULL,
      material_code TEXT NOT NULL,
      qty_per_unit REAL NOT NULL DEFAULT 0,
      unit TEXT DEFAULT 'EA',
      active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(process_name, material_code),
      FOREIGN KEY(material_code) REFERENCES materials(code)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS material_usage (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      process_log_id INTEGER NOT NULL,
      work_date TEXT NOT NULL,
      process_name TEXT NOT NULL,
      material_code TEXT NOT NULL,
      qty_used REAL NOT NULL,
      unit TEXT DEFAULT 'EA',
      operator TEXT DEFAULT '',
      usage_type TEXT NOT NULL DEFAULT 'BOM',
      reason TEXT DEFAULT '',
      note TEXT DEFAULT '',
      created_at TEXT DEFAULT (datetime('now','localtime')),
      FOREIGN KEY(process_log_id) REFERENCES process_logs(id) ON DELETE CASCADE,
      FOREIGN KEY(material_code) REFERENCES materials(code)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory_txn (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT DEFAULT (datetime('now','localtime')),
      material_code TEXT NOT NULL,
      qty_delta REAL NOT NULL,
      qty_before REAL,
      qty_after REAL,
      reason TEXT DEFAULT '',
      ref_type TEXT DEFAULT '',
      ref_id INTEGER,
      note TEXT DEFAULT '',
      FOREIGN KEY(material_code) REFERENCES materials(code)
    )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bom_proc ON process_bom(process_name);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_plid ON material_usage(process_log_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_mat_ts ON inventory_txn(material_code, ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_mat_date ON material_usage(material_code, work_date);")

    _add_col_if_missing(conn, "material_usage", "usage_type", "usage_type TEXT NOT NULL DEFAULT 'BOM'")
    _add_col_if_missing(conn, "material_usage", "reason", "reason TEXT DEFAULT ''")
    _add_col_if_missing(conn, "material_usage", "note", "note TEXT DEFAULT ''")

    _add_col_if_missing(conn, "inventory_txn", "qty_before", "qty_before REAL")
    _add_col_if_missing(conn, "inventory_txn", "qty_after", "qty_after REAL")



def _ensure_model_tables_compat(conn: sqlite3.Connection) -> None:
    """
    ✅ 모델별 공정/측정 조건 테이블 보장
    - 기존 DB에도 안전하게 컬럼/테이블 추가
    """
    _add_col_if_missing(conn, "process_logs", "model", "model TEXT")
    _add_col_if_missing(conn, "modules", "model", "model TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_process_logs_model ON process_logs(model);")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS model_master (
      model_code TEXT PRIMARY KEY,
      display_name TEXT DEFAULT '',
      max_current_a REAL NOT NULL DEFAULT 20,
      pbs_step_a REAL NOT NULL DEFAULT 1,
      pbs_points_csv TEXT DEFAULT '',
      vbg_points_csv TEXT DEFAULT '2,5,10,15,16,17,20',
      burnin_current_a REAL NOT NULL DEFAULT 20,
      beam_check_current_a REAL NOT NULL DEFAULT 0.8,
      active INTEGER NOT NULL DEFAULT 1,
      note TEXT DEFAULT '',
      created_at TEXT DEFAULT (datetime('now','localtime')),
      updated_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """)

    _add_col_if_missing(conn, "model_master", "pbs_points_csv", "pbs_points_csv TEXT DEFAULT ''")
    _add_col_if_missing(conn, "model_master", "beam_check_current_a", "beam_check_current_a REAL NOT NULL DEFAULT 0.8")
    _add_col_if_missing(conn, "model_master", "measure_step_wait_s", "measure_step_wait_s REAL NOT NULL DEFAULT 10")
    _add_col_if_missing(conn, "model_master", "max_hold_wait_s", "max_hold_wait_s REAL NOT NULL DEFAULT 15")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS model_process_flow (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      model_code TEXT NOT NULL,
      seq INTEGER NOT NULL,
      process_name TEXT NOT NULL,
      process_name_raw TEXT DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT DEFAULT (datetime('now','localtime')),
      UNIQUE(model_code, seq),
      UNIQUE(model_code, process_name),
      FOREIGN KEY(model_code) REFERENCES model_master(model_code)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS model_process_bom (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      model_code TEXT NOT NULL DEFAULT 'F976370W',
      process_name TEXT NOT NULL,
      material_code TEXT NOT NULL,
      qty_per_unit REAL NOT NULL DEFAULT 0,
      unit TEXT DEFAULT 'EA',
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT DEFAULT (datetime('now','localtime')),
      UNIQUE(model_code, process_name, material_code),
      FOREIGN KEY(model_code) REFERENCES model_master(model_code),
      FOREIGN KEY(material_code) REFERENCES materials(code)
    )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_process_logs_model ON process_logs(model);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_flow_model_seq ON model_process_flow(model_code, seq);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_bom_model_proc ON model_process_bom(model_code, process_name);")

    # 기존 공통 process_bom 데이터가 있으면 기본 모델(F976370W) BOM으로 복사
    try:
        conn.execute("""
        INSERT OR IGNORE INTO model_process_bom(model_code, process_name, material_code, qty_per_unit, unit, active)
        SELECT 'F976370W', process_name, material_code, qty_per_unit, unit, active
        FROM process_bom
        """)
    except Exception:
        pass

    # 기존 DB의 PBS Step 설정을 PBS 측정 포인트 CSV로 1회 변환한다.
    # 기존 사용자 설정은 삭제하지 않고, 새 컬럼이 비어 있는 행만 채운다.
    try:
        rows = conn.execute(
            "SELECT model_code, max_current_a, pbs_step_a, pbs_points_csv FROM model_master"
        ).fetchall()
        for row in rows:
            getter = row.__getitem__
            raw_csv = str(getter("pbs_points_csv") if hasattr(row, "keys") else row[3] or "").strip()
            if raw_csv:
                continue
            max_a = float(getter("max_current_a") if hasattr(row, "keys") else row[1] or 20.0)
            step = max(1, int(round(float(getter("pbs_step_a") if hasattr(row, "keys") else row[2] or 1.0))))
            max_i = max(1, int(round(max_a)))
            points = list(range(1, max_i + 1, step))
            if max_i not in points:
                points.append(max_i)
            conn.execute(
                "UPDATE model_master SET pbs_points_csv = ? WHERE model_code = ?",
                (",".join(str(x) for x in points), getter("model_code") if hasattr(row, "keys") else row[0]),
            )
    except Exception:
        pass

    # 기본 모델 seed
    defaults = [
        ("F9769W",   "F9769W / 9W",   13.0, 1.0, ",".join(str(x) for x in range(1, 14)), "1,2,3,5,8,10,13", 13.0, 0.8, 10.0, 15.0, 1, ""),
        ("F97645W",  "F97645W / 45W", 13.0, 1.0, ",".join(str(x) for x in range(1, 14)), "1,2,3,5,8,10,13", 13.0, 0.8, 10.0, 15.0, 1, ""),
        ("F976370W", "F976370W / 370W", 20.0, 1.0, ",".join(str(x) for x in range(1, 21)), "2,5,10,15,16,17,20", 20.0, 0.8, 10.0, 15.0, 1, ""),
        ("F976550W", "F976550W / 550W", 25.0, 1.0, ",".join(str(x) for x in range(1, 26)), "2,5,10,15,20,25", 25.0, 0.8, 10.0, 15.0, 1, ""),
        ("F976700W", "F976700W / 700W", 30.0, 1.0, ",".join(str(x) for x in range(1, 31)), "2,5,10,15,20,25,30", 30.0, 0.8, 10.0, 15.0, 1, ""),
    ]
    for row in defaults:
        conn.execute("""
        INSERT OR IGNORE INTO model_master(
          model_code, display_name, max_current_a, pbs_step_a,
          pbs_points_csv, vbg_points_csv, burnin_current_a, beam_check_current_a,
          measure_step_wait_s, max_hold_wait_s, active, note
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, row)

    default_flow = [
        "패키지마킹", "리플로우", "전극바부착", "칩본딩",
        "FAC정렬", "SAC정렬", "PBS정렬", "MIRROR1정렬",
        "VBG정렬", "REDUCER정렬", "파이버정렬", "LID부착"
    ]

    for model_code, *_ in defaults:
        existing = conn.execute(
            "SELECT COUNT(*) AS cnt FROM model_process_flow WHERE model_code = ?",
            (model_code,),
        ).fetchone()
        if int(existing["cnt"] if hasattr(existing, "keys") else existing[0]) > 0:
            continue
        for idx, proc in enumerate(default_flow, start=1):
            conn.execute("""
            INSERT OR IGNORE INTO model_process_flow(model_code, seq, process_name, process_name_raw, active)
            VALUES(?,?,?,?,1)
            """, (model_code, idx, proc, proc))


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    _ensure_module_status_compat(conn)
    _ensure_material_tables_compat(conn)
    _ensure_model_tables_compat(conn)
    conn.commit()

    ensure_measurement_tables(conn)
    _ensure_settings_table(conn)
    conn.commit()
