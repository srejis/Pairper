import json
import re

from pairper.app.autostart import autostart_status, set_autostart
from pairper.document.comparison import comparison_image
from pairper.document.presentation import display_document
from pairper.translation.service import TranslationService
from pairper.tunnel.config import project_root
from pairper.tunnel.runtime import public_status
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QMessageBox


class TaskSignals(QObject):
    finished = Signal(str, object)


class Task(QRunnable):
    def __init__(self, request_id, function):
        super().__init__()
        self.request_id, self.function = request_id, function
        self.signals = TaskSignals()

    def run(self):
        try:
            result = {"ok": True, "data": self.function()}
        except Exception:
            result = {
                "ok": False,
                "error": "데이터를 처리하지 못했습니다. 원본과 저장소 상태를 확인하세요.",
            }
        self.signals.finished.emit(self.request_id, result)


class TaskReceiver(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    @Slot(str, object)
    def receive(self, request_id, result):
        self.owner.complete(request_id, result)


class FrontendBridge(QObject):
    reply = Signal(str, str)
    libraryChanged = Signal()
    connectionChanged = Signal()

    def __init__(self, library, window, root=None):
        super().__init__(window)
        self.library, self.window = library, window
        self.root = root or project_root()
        self.tasks = {}
        self.receiver = TaskReceiver(self)
        self.signature = None
        self.polling = False
        self.importing = False
        self.supervisor = None
        self.translation_service = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1500)

    def dispatch(self, request_id, function):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", request_id) or request_id in self.tasks:
            return
        task = Task(request_id, function)
        self.tasks[request_id] = task
        task.signals.finished.connect(self.receiver.receive)
        QThreadPool.globalInstance().start(task)

    def complete(self, request_id, result):
        self.tasks.pop(request_id, None)
        if request_id == "_poll":
            self.polling = False
            if result["ok"]:
                signature = json.dumps(result["data"], sort_keys=True)
                if signature != self.signature:
                    self.signature = signature
                    self.libraryChanged.emit()
            return
        if request_id.startswith("import_"):
            self.importing = False
            self.libraryChanged.emit()
        self.reply.emit(request_id, json.dumps(result, ensure_ascii=False))

    def poll(self):
        if not self.polling:
            self.polling = True
            self.dispatch("_poll", self.paper_list)

    def paper_list(self):
        papers = [
            {
                key: p[key]
                for key in (
                    "id",
                    "title",
                    "filename",
                    "page_count",
                    "status",
                    "has_view",
                    "current_view_id",
                    "created_at",
                )
            }
            for p in self.library.list_papers()
        ]
        with self.library.connect() as con:
            for paper in papers:
                original = self.library.paper(paper["id"])
                metadata = json.loads(original["metadata"])
                paper["title"] = metadata.get("title") or paper["filename"]
                paper["original_available"] = self.library.managed(
                    original["original_path"]
                ).is_file()
                job = con.execute(
                    "SELECT error FROM jobs WHERE paper_id=? ORDER BY updated_at DESC LIMIT 1",
                    (paper["id"],),
                ).fetchone()
                paper["error"] = job["error"][:300] if job and job["error"] else None
        return papers

    @Slot(str, str, str, str, str)
    def sourceRegion(self, request_id, paper_id, view_id, block_id, region_id):
        self.dispatch(
            request_id,
            lambda: comparison_image(self.library, paper_id, view_id, block_id, region_id),
        )

    @Slot(str)
    def listPapers(self, request_id):
        self.dispatch(request_id, self.paper_list)

    @Slot(str, str)
    def copyUpdateRequest(self, request_id, paper_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", request_id):
            return
        try:
            if not re.fullmatch(r"[a-f0-9]{32}", paper_id):
                raise ValueError("Invalid paper ID")
            paper = self.library.paper(paper_id)
            title = json.loads(paper["metadata"]).get("title") or paper["filename"]
            QGuiApplication.clipboard().setText(
                f"Pairper 논문 업데이트해 줘. 대상 논문: {title} (paper_id: {paper_id})"
            )
            result = {"ok": True, "data": {"copied": True}}
        except Exception:
            result = {"ok": False, "error": "요청 문구를 복사하지 못했습니다."}
        self.reply.emit(request_id, json.dumps(result, ensure_ascii=False))

    @Slot(str, str)
    def getDocument(self, request_id, paper_id):
        def load():
            if not re.fullmatch(r"[a-f0-9]{32}", paper_id):
                raise ValueError("Invalid paper ID")
            return display_document(self.library, paper_id)

        self.dispatch(request_id, load)

    def translations(self):
        if self.translation_service is None:
            self.translation_service = TranslationService(self.library)
        return self.translation_service

    @Slot(str, str)
    def listTranslations(self, request_id, paper_id):
        def load():
            if not re.fullmatch(r'[a-f0-9]{32}', paper_id):
                raise ValueError('Invalid paper ID')
            return self.translations().sessions(paper_id)
        self.dispatch(request_id, load)

    @Slot(str, str)
    def getTranslationDocument(self, request_id, session_id):
        def load():
            if not re.fullmatch(r'[a-f0-9]{64}', session_id):
                raise ValueError('Invalid translation session')
            return self.translations().display(session_id)
        self.dispatch(request_id, load)

    @Slot(str)
    def autostartStatus(self, request_id):
        self.dispatch(request_id, lambda: autostart_status(self.root))

    @Slot(str, bool)
    def setAutostart(self, request_id, enabled):
        self.dispatch(request_id, lambda: set_autostart(enabled, self.root))

    @Slot(str)
    def connectionStatus(self, request_id):
        self.dispatch(request_id, self.connection_snapshot)

    def attach_supervisor(self, supervisor):
        self.supervisor = supervisor

    def connection_snapshot(self):
        return self.supervisor.snapshot() if self.supervisor else public_status(self.root)

    @Slot(str)
    def reconnectTunnel(self, request_id):
        if not self.supervisor:
            self.reply.emit(
                request_id,
                json.dumps(
                    {"ok": False, "error": "앱 연결 관리자가 실행되지 않았습니다."},
                    ensure_ascii=False,
                ),
            )
            return
        self.supervisor.reconnect()
        self.dispatch(request_id, self.connection_snapshot)

    @Slot(str)
    def importPdfs(self, request_id):
        if self.importing or not request_id.startswith("import_"):
            self.reply.emit(
                request_id, json.dumps({"ok": False, "error": "PDF 등록 작업이 진행 중입니다."})
            )
            return
        paths, _ = QFileDialog.getOpenFileNames(self.window, "PDF 추가", "", "PDF (*.pdf)")
        self.importing = True

        def register():
            imported, failed = 0, 0
            for path in paths:
                try:
                    self.library.register(path)
                    imported += 1
                except Exception:
                    failed += 1
            return {"imported": imported, "failed": failed}

        self.dispatch(request_id, register)

    @Slot(str)
    def openDiagnostics(self, request_id):
        self.dispatch(request_id, self.connection_snapshot)
        # Fixed native operation; the frontend cannot supply paths, URLs or commands.
        status = self.connection_snapshot()
        if status.get("admin_url") and status.get("healthy"):
            QDesktopServices.openUrl(QUrl(status["admin_url"]))
        else:
            QMessageBox.information(
                self.window,
                "Pairper 터널 진단",
                "프로젝트 폴더에서 test-pairper-tunnel.ps1을 실행하세요.\n"
                "진단 로그: .pairper/doctor.log\n"
                "자동 시작은 앱 설정에서 켜거나 끌 수 있습니다.",
            )
