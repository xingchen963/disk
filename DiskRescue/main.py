import sys
from PySide6.QtWidgets import QApplication
from diskrescue.workbench import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DiskRescue")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
