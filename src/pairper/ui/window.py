"""Minimal Qt shell; React/TypeScript owns application and document UI."""

from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from pairper.document.renderer import WEB
from pairper.ui.bridge import FrontendBridge

FRONTEND = WEB / "frontend"


class LocalOnly(QWebEngineUrlRequestInterceptor):
    def __init__(self, library, parent=None, dev_url=None):
        super().__init__(parent)
        self.roots = [FRONTEND.resolve(), (library.root / "assets").resolve()]
        self.dev_url = dev_url
        self.blocked_count = 0

    def allowed(self, url):
        if url.toString() in {"about:blank", "qrc:///qtwebchannel/qwebchannel.js"}:
            return True
        if self.dev_url and url.scheme() in {"http", "ws"}:
            return url.host() == "127.0.0.1" and url.port() == QUrl(self.dev_url).port()
        if not url.isLocalFile():
            return False
        path = Path(url.toLocalFile()).resolve()
        return any(path.is_relative_to(root) for root in self.roots) and path.suffix.lower() in {
            ".html",
            ".css",
            ".js",
            ".png",
            ".woff",
            ".woff2",
            ".ttf",
        }

    def interceptRequest(self, info):
        if not self.allowed(info.requestUrl()):
            self.blocked_count += 1
            info.block(True)


class ReaderPage(QWebEnginePage):
    def __init__(self, profile, parent, entry):
        super().__init__(profile, parent)
        self.entry = entry

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        target = QUrl(url)
        target.setFragment(None)
        return url.toString() == "about:blank" or (is_main_frame and target == self.entry)

    def createWindow(self, window_type):
        return None


class MainWindow(QMainWindow):
    def __init__(self, library, dev_url=None, project=None):
        super().__init__()
        self.library = library
        if dev_url:
            parsed = urlparse(dev_url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
                raise ValueError("개발 UI 주소는 명시적인 127.0.0.1 HTTP 포트여야 합니다.")
        elif not (FRONTEND / "index.html").is_file():
            raise RuntimeError(
                "프론트엔드 빌드가 없습니다. frontend에서 npm ci 및 npm run build를 실행하세요."
            )
        entry = QUrl(dev_url) if dev_url else QUrl.fromLocalFile(str(FRONTEND / "index.html"))
        self.setWindowTitle("Pairper")
        self.resize(1280, 850)
        self.reader = QWebEngineView()
        self.profile = QWebEngineProfile(self.reader)
        self.interceptor = LocalOnly(library, self.profile, dev_url)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.reader.setPage(ReaderPage(self.profile, self.reader, entry))
        settings = self.reader.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, bool(dev_url)
        )
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, False)
        self.channel = QWebChannel(self.reader.page())
        self.bridge = FrontendBridge(library, self, project)
        self.channel.registerObject("pairper", self.bridge)
        self.reader.page().setWebChannel(self.channel)
        self.reader.loadFinished.connect(self.loaded)
        self.setCentralWidget(self.reader)
        self.reader.load(entry)

    def loaded(self, success):
        if not success:
            self.statusBar().showMessage(
                "UI를 열 수 없습니다. 프론트엔드 빌드와 로컬 리소스를 확인하세요."
            )

    def closeEvent(self, event):
        handler = getattr(self, "close_handler", None)
        if handler and handler(event):
            return
        self.bridge.timer.stop()
        self.reader.page().setWebChannel(None)
        # Replace the owned page before its private profile is destroyed by Qt.
        self.reader.stop()
        self.reader.setPage(QWebEnginePage(self.reader))
        super().closeEvent(event)
