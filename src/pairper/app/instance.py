"""Per-user/project desktop lock and a local show-window IPC message."""

import hashlib
import os

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    activated = Signal()

    def __init__(self, root, parent=None):
        super().__init__(parent)
        runtime = root / ".pairper"
        runtime.mkdir(exist_ok=True)
        identity = os.path.normcase(str(root.resolve()))
        self.name = "pairper-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.lock = QLockFile(str(runtime / "desktop.lock"))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept)
        self.connections = set()

    def acquire(self):
        if not self.lock.tryLock(0):
            return False
        # Only the lock owner can remove a stale socket left by an earlier crashed instance.
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.lock.unlock()
            raise RuntimeError("Pairper 로컬 활성화 채널을 열 수 없습니다.")
        return True

    def activate_existing(self, timeout=2000):
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(timeout):
            return False
        socket.write(b"show\n")
        socket.flush()
        if not socket.bytesAvailable() and not socket.waitForReadyRead(timeout):
            return False
        acknowledged = bytes(socket.readAll()) == b"ok\n"
        socket.disconnectFromServer()
        return acknowledged

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.connections.add(socket)
            socket.readyRead.connect(lambda s=socket: self.read(s))
            socket.disconnected.connect(lambda s=socket: self.release(s))
            if socket.bytesAvailable():
                self.read(socket)

    def read(self, socket):
        if socket.bytesAvailable() > 32:
            socket.abort()
            return
        if socket.canReadLine():
            if bytes(socket.readLine(32)) == b"show\n":
                self.activated.emit()
                socket.write(b"ok\n")
                socket.flush()
            else:
                socket.abort()

    def release(self, socket):
        self.connections.discard(socket)
        socket.deleteLater()

    def close(self):
        for socket in list(self.connections):
            socket.abort()
        self.server.close()
        self.lock.unlock()
