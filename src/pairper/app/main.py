import argparse
import sys

from pairper.storage.library import Library


def main():
    parser = argparse.ArgumentParser(description="Pairper local paper library")
    parser.add_argument("--data-dir", help="Shared library folder (or PAIRPER_DATA_DIR)")
    parser.add_argument("--import-pdf", nargs="+", metavar="PDF", help="Import without opening GUI")
    args = parser.parse_args()
    library = Library(args.data_dir)
    if args.import_pdf:
        import json

        for path in args.import_pdf:
            print(json.dumps(library.register(path), ensure_ascii=False))
        return
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from pairper.ui.window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Pairper")
    app.setFont(QFont("Malgun Gothic" if sys.platform == "win32" else "sans-serif", 10))
    window = MainWindow(library)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
