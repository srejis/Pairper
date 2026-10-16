import html
import json
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pairper.document.renderer import WEB

LABELS = {
    "registered": "등록됨",
    "processing": "처리 중",
    "complete": "완료",
    "review_required": "검토 필요",
    "failed": "실패",
}


class LocalOnly(QWebEngineUrlRequestInterceptor):
    def __init__(self, library, parent=None):
        super().__init__(parent)
        self.roots = [
            WEB.resolve(),
            library.root / "assets",
            library.root / "views",
            library.root / "staging",
        ]

    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.toString() == "about:blank":
            return
        path = Path(url.toLocalFile()).resolve() if url.isLocalFile() else None
        allowed = (
            path
            and any(path.is_relative_to(root) for root in self.roots)
            and path.suffix.lower() in {".html", ".css", ".js", ".png", ".woff", ".woff2", ".ttf"}
        )
        if not allowed:
            info.block(True)


class ReaderPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.isLocalFile() or url.toString() == "about:blank"

    def createWindow(self, window_type):
        return None


class ImportSignals(QObject):
    finished = Signal(list)


class ImportTask(QRunnable):
    def __init__(self, library, paths):
        super().__init__()
        self.library, self.paths = library, paths
        self.signals = ImportSignals()

    def run(self):
        errors = []
        for path in self.paths:
            try:
                self.library.register(path)
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        self.signals.finished.emit(errors)


class MainWindow(QMainWindow):
    def __init__(self, library):
        super().__init__()
        self.library = library
        self.setWindowTitle("Pairper")
        self.resize(1280, 850)
        self.signature = None
        self.selected_signature = None
        self.placeholder = library.root / "staging" / f"ui-{uuid.uuid4().hex}.html"
        root = QWidget()
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        name = QLabel("Pairper")
        name.setStyleSheet("font-size: 24px; font-weight: 700; color: #235b60")
        toolbar.addWidget(name)
        toolbar.addStretch()
        self.add_button = QPushButton("PDF 추가")
        self.add_button.clicked.connect(self.add_pdfs)
        toolbar.addWidget(self.add_button)
        help_button = QPushButton("MCP 연결 방법")
        help_button.clicked.connect(self.connection_help)
        toolbar.addWidget(help_button)
        layout.addLayout(toolbar)
        self.info = QLabel("논문을 선택하세요")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        split = QSplitter()
        self.papers = QListWidget()
        self.papers.setMinimumWidth(250)
        self.papers.currentItemChanged.connect(self.select_paper)
        split.addWidget(self.papers)
        self.reader = QWebEngineView()
        self.profile = QWebEngineProfile(self.reader)
        self.interceptor = LocalOnly(library, self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        page = ReaderPage(self.profile, self.reader)
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False
        )
        self.reader.setPage(page)
        self.reader.loadFinished.connect(self.loaded)
        split.addWidget(self.reader)
        split.setSizes([300, 980])
        layout.addWidget(split)
        self.setCentralWidget(root)
        self.setStyleSheet(
            "QMainWindow {background:#f6f8f8} QPushButton {padding:8px 16px} "
            "QListWidget {border:1px solid #dce5e5} QListWidget::item {padding:14px}"
        )
        self.message(
            "논문 읽기를 시작하세요",
            "PDF를 추가한 뒤 연결된 GPT에서 “Pairper 논문 업데이트해 줘”라고 요청하세요.",
        )
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)

    def message(self, title, text):
        self.placeholder.write_text(
            '<!doctype html><meta charset="utf-8">'
            "<style>body{font:17px system-ui;padding:64px;color:#345;"
            "line-height:1.8}h1{font-size:26px}</style>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>",
            encoding="utf-8",
        )
        self.reader.load(QUrl.fromLocalFile(str(self.placeholder)))

    def refresh(self):
        try:
            papers = self.library.list_papers()
            signature = json.dumps(papers, sort_keys=True)
            if signature == self.signature:
                return
            current = self.papers.currentItem()
            selected = current.data(Qt.ItemDataRole.UserRole)["id"] if current else None
            self.signature = signature
            self.papers.blockSignals(True)
            self.papers.clear()
            selection = None
            for paper in papers:
                status = LABELS.get(paper["status"], paper["status"])
                view = "뷰 있음" if paper["has_view"] else "뷰 대기"
                item = QListWidgetItem(f"{paper['title']}\n{status} · {view}")
                item.setData(Qt.ItemDataRole.UserRole, paper)
                self.papers.addItem(item)
                if paper["id"] == selected:
                    selection = item
            self.papers.blockSignals(False)
            if selection:
                self.papers.setCurrentItem(selection)
            elif papers:
                self.papers.setCurrentRow(0)
            self.statusBar().showMessage(f"{len(papers)}편 · 저장소: {self.library.root}")
        except Exception as exc:
            self.statusBar().showMessage(f"저장소 오류: {exc}")

    def select_paper(self, item, previous=None):
        if not item:
            return
        paper = item.data(Qt.ItemDataRole.UserRole)
        self.info.setText(
            f"{paper['title']} · {paper['page_count']}페이지 · {LABELS.get(paper['status'])}"
        )
        signature = (paper["id"], paper["current_view_id"], paper["status"], paper["has_view"])
        if self.selected_signature == signature:
            return
        self.selected_signature = signature
        try:
            if paper["has_view"]:
                self.reader.load(QUrl.fromLocalFile(str(self.library.view_path(paper["id"]))))
            else:
                self.message(
                    LABELS.get(paper["status"], "대기 중"),
                    "연결된 GPT에 “Pairper 논문 업데이트해 줘”라고 요청하세요. "
                    "PDF 등록만으로 AI 분석이 시작되지는 않습니다.",
                )
        except Exception as exc:
            self.message("논문을 열 수 없습니다", str(exc))

    def add_pdfs(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "PDF 추가", "", "PDF (*.pdf)")
        if not paths:
            return
        self.add_button.setEnabled(False)
        self.statusBar().showMessage("PDF 원본을 복사하고 추출하는 중…")
        self.import_task = ImportTask(self.library, paths)
        self.import_task.signals.finished.connect(self.import_finished)
        QThreadPool.globalInstance().start(self.import_task)

    def import_finished(self, errors):
        self.add_button.setEnabled(True)
        self.refresh()
        if errors:
            QMessageBox.warning(self, "일부 PDF 등록 실패", "\n".join(errors))

    def connection_help(self):
        config = {
            "mcpServers": {
                "pairper": {
                    "command": sys.executable,
                    "args": ["-m", "pairper.mcp.server"],
                    "env": {"PAIRPER_DATA_DIR": str(self.library.root)},
                }
            }
        }
        QMessageBox.information(
            self,
            "MCP 연결",
            "로컬 MCP 클라이언트 설정:\n\n"
            + json.dumps(config, ensure_ascii=False, indent=2)
            + "\n\nChatGPT 웹 연결은 docs/mcp.md의 Secure MCP Tunnel 안내를 확인하세요.",
        )

    def loaded(self, success):
        if not success:
            self.statusBar().showMessage("문서 렌더링 실패: 원본/자산/권한을 확인하세요.")
        elif "/views/" in self.reader.url().toString():
            self.reader.page().runJavaScript(
                "JSON.stringify({ready:document.documentElement.dataset.readerReady,"
                "fallbacks:document.documentElement.dataset.mathFallbacks,"
                "broken:Array.from(document.images).filter(i=>!i.complete||!i.naturalWidth).length})",
                self.render_status,
            )

    def render_status(self, result):
        try:
            data = json.loads(result)
            if data.get("broken") or data.get("ready") != "true":
                self.statusBar().showMessage(
                    "렌더링 검토 필요: 로컬 이미지 또는 수식 스크립트를 확인하세요."
                )
            elif int(data.get("fallbacks", 0)):
                self.statusBar().showMessage(
                    f"수식 {data['fallbacks']}개는 원본 이미지로 표시됩니다."
                )
        except (ValueError, TypeError):
            self.statusBar().showMessage("렌더링 결과를 확인할 수 없습니다.")

    def closeEvent(self, event):
        handler = getattr(self, "close_handler", None)
        if handler and handler(event):
            return
        self.timer.stop()
        self.reader.setPage(QWebEnginePage(self.reader))
        self.placeholder.unlink(missing_ok=True)
        super().closeEvent(event)
