import sys
from app.app_config import AppConfig
from app.logging_config import configure_logging
from storage.database.repository import Repository

def main():
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from app.main_window import MainWindow
    except ImportError:
        print("请先运行 python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("DeepRecover")
    app.setOrganizationName("DeepRecover")
    try:
        config = AppConfig.default()
        config.initialize()
        logger = configure_logging(config.data_dir / "Logs")
        repository = Repository(config.database)
        repository.recover_interrupted()
        window = MainWindow(config, repository)
    except Exception as error:
        QMessageBox.critical(None, "DeepRecover 启动失败", f"无法初始化工作目录或数据库（{type(error).__name__}）。请检查目录权限及版本。")
        return 1
    logger.info("application_started version=0.1.0")
    window.show()
    return app.exec()
