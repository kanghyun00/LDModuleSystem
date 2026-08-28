import sys
import logging

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon

from src.ldms.logging_conf import setup_logging
from src.ldms.ui.main_window import MainWindow
from src.ldms.config import APP_TITLE, APP_VERSION, get_db_path
from src.ldms.db.repo import DBRepo
from src.ldms.utils.resource_path import resource_path


def main():
    setup_logging()
    log = logging.getLogger("app")
    log.info(f"Starting {APP_TITLE} v{APP_VERSION}")

    db_path = get_db_path()

    # ✅ 배포/개발 모두 안전한 DB 경로로 repo 1개 생성
    repo = DBRepo(db_path)
    log.info(f"DB path: {db_path}")

    app = QApplication(sys.argv)

    # ✅ (중요) 실행 중 아이콘(작업표시줄/Alt+Tab) 설정
    icon_path = resource_path("assets/company_logo.ico")
    app.setWindowIcon(QIcon(icon_path))
    log.info(f"App icon path: {icon_path}")

    w = MainWindow(repo=repo)   # ✅ repo를 창에 주입
    w.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
