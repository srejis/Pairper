import argparse
import sys

from pairper.storage.library import Library


def main():
    parser = argparse.ArgumentParser(description="Pairper local paper library")
    parser.add_argument("--data-dir", help="Shared library folder (or PAIRPER_DATA_DIR)")
    parser.add_argument("--dev-ui", help="Explicit local Vite URL, e.g. http://127.0.0.1:5173/")
    parser.add_argument("--legacy-ui", action="store_true", help="Use the preserved Python UI")
    parser.add_argument("--import-pdf", nargs="+", metavar="PDF", help="Import without opening GUI")
    args = parser.parse_args()
    from pairper.tunnel.config import ConfigurationError, project_root, read_env

    root = project_root()
    try:
        local = read_env(root / ".env") if (root / ".env").is_file() else {}
    except ConfigurationError:
        local = {}
        print("터널 .env 설정을 확인하세요. 기본 라이브러리로 앱을 엽니다.", file=sys.stderr)
    library = Library(args.data_dir or local.get("PAIRPER_DATA_DIR"))
    if args.import_pdf:
        import json

        for path in args.import_pdf:
            print(json.dumps(library.register(path), ensure_ascii=False))
        return
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    if args.legacy_ui:
        from pairper.ui.legacy_window import MainWindow
    else:
        from pairper.ui.window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Pairper")
    app.setFont(QFont("Malgun Gothic" if sys.platform == "win32" else "sans-serif", 10))
    window = MainWindow(library) if args.legacy_ui else MainWindow(library, dev_url=args.dev_ui)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
