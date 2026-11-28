from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QObject, Signal

from adofai_publisher.obs_ws import (
    ObsAuthError,
    ObsClient,
    build_auth_string,
    build_identify_message,
    build_request_message,
    connection_error_message,
    request_error_message,
)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class FakeSocket(QObject):
    connected = Signal()
    textMessageReceived = Signal(str)
    disconnected = Signal()
    errorOccurred = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []
        self.opened = ""
        self.closed = False
        self._error_text = ""
        self._close_code = 0

    def open(self, url) -> None:
        self.opened = url.toString()

    def sendTextMessage(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:
        pass

    def errorString(self) -> str:
        return self._error_text

    def closeCode(self) -> int:
        return self._close_code

    def receive(self, payload: dict) -> None:
        self.textMessageReceived.emit(json.dumps(payload))


def make_client() -> tuple[ObsClient, FakeSocket]:
    socket = FakeSocket()
    client = ObsClient(socket_factory=lambda: socket)
    return client, socket


def complete_handshake(client: ObsClient, socket: FakeSocket, password: str = "") -> None:
    socket.connected.emit()
    socket.receive(
        {
            "op": 0,
            "d": {
                "obsWebSocketVersion": "5.5.2",
                "rpcVersion": 1,
                "authentication": {"challenge": "challenge", "salt": "salt"},
            },
        }
    )
    identify = socket.sent[-1]
    assert identify["op"] == 1
    assert identify["d"]["rpcVersion"] == 1
    assert identify["d"]["authentication"]
    socket.receive({"op": 2, "d": {"negotiatedRpcVersion": 1}})


def respond(socket: FakeSocket, result: bool, data: dict | None = None) -> None:
    request = socket.sent[-1]
    assert request["op"] == 6
    socket.receive(
        {
            "op": 7,
            "d": {
                "requestType": request["d"]["requestType"],
                "requestId": request["d"]["requestId"],
                "requestStatus": {"result": result, "code": 100 if result else 500},
                "responseData": data or {},
            },
        }
    )


def test_auth_string_matches_obs_v5_algorithm():
    assert build_auth_string("password", "salt", "challenge") == (
        "zTM5ki6L2vVvBQiTG9ckH1Lh64AbnCf6XZ226UmnkIA="
    )


def test_build_identify_without_auth():
    message = build_identify_message({"rpcVersion": 1}, "")
    assert message == {
        "op": 1,
        "d": {"rpcVersion": 1, "eventSubscriptions": 0},
    }


def test_build_identify_requires_password_when_challenge_present():
    with pytest.raises(ObsAuthError, match="密码"):
        build_identify_message(
            {"rpcVersion": 1, "authentication": {"salt": "s", "challenge": "c"}},
            "",
        )


def test_build_request_message_shape():
    message = build_request_message("r1", "StartRecord")
    assert message["op"] == 6
    assert message["d"]["requestType"] == "StartRecord"
    assert message["d"]["requestId"] == "r1"
    assert message["d"]["requestData"] == {}


def test_request_error_messages_are_readable():
    assert "密码错误" in request_error_message(
        "StartRecord", {"code": 4009, "comment": "authentication failed"}
    )
    assert "已经在录制" in request_error_message(
        "StartRecord", {"code": 500, "comment": "already recording"}
    )
    assert "没有在录制" in request_error_message(
        "StopRecord", {"code": 500, "comment": "not recording"}
    )
    assert "录制输出" in request_error_message(
        "StartRecord", {"code": 500, "comment": "output not configured"}
    )


def test_connection_error_message_for_refused_port():
    from PySide6.QtNetwork import QAbstractSocket

    message = connection_error_message(
        QAbstractSocket.SocketError.ConnectionRefusedError
    )
    assert "启用服务器" in message
    assert "4455" in message


def test_client_handshake_sends_identify_and_emits_connected(qapp):
    client, socket = make_client()
    connected: list[bool] = []
    client.connected.connect(lambda: connected.append(True))

    client.test_connection("127.0.0.1", 4455, "secret")
    assert socket.opened == "ws://127.0.0.1:4455"

    complete_handshake(client, socket, "secret")
    assert connected == [True]
    assert client.is_connected
    client.close()


def test_client_identify_includes_hashed_password(qapp):
    client, socket = make_client()
    client.test_connection("127.0.0.1", 4455, "secret")
    socket.connected.emit()
    socket.receive(
        {
            "op": 0,
            "d": {
                "rpcVersion": 1,
                "authentication": {"challenge": "challenge", "salt": "salt"},
            },
        }
    )
    identify = socket.sent[-1]
    expected = build_auth_string("secret", "salt", "challenge")
    assert identify["d"]["authentication"] == expected
    client.close()


def test_client_missing_password_fails_readably(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)

    client.test_connection("127.0.0.1", 4455, "")
    socket.connected.emit()
    socket.receive(
        {
            "op": 0,
            "d": {
                "rpcVersion": 1,
                "authentication": {"challenge": "challenge", "salt": "salt"},
            },
        }
    )

    assert errors and "密码" in errors[0]
    assert not client.is_connected


def test_client_wrong_password_closes_with_4009(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.test_connection("127.0.0.1", 4455, "wrong")
    socket.connected.emit()
    socket.receive(
        {
            "op": 0,
            "d": {
                "rpcVersion": 1,
                "authentication": {"challenge": "c", "salt": "s"},
            },
        }
    )
    socket._close_code = 4009
    socket.disconnected.emit()

    assert errors and "密码错误" in errors[0]


def test_client_connection_failure_is_readable(qapp):
    from PySide6.QtNetwork import QAbstractSocket

    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.test_connection("127.0.0.1", 4455, "")
    socket.errorOccurred.emit(QAbstractSocket.SocketError.ConnectionRefusedError)

    assert errors and "启用服务器" in errors[0]


def test_start_record_success(qapp):
    client, socket = make_client()
    started: list[bool] = []
    client.record_started.connect(lambda: started.append(True))
    client.start_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")

    assert socket.sent[-1]["d"]["requestType"] == "StartRecord"
    respond(socket, True)
    assert started == [True]
    client.close()


def test_start_record_duplicate_is_reported(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.start_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")
    request = socket.sent[-1]
    socket.receive(
        {
            "op": 7,
            "d": {
                "requestType": "StartRecord",
                "requestId": request["d"]["requestId"],
                "requestStatus": {
                    "result": False,
                    "code": 500,
                    "comment": "already recording",
                },
            },
        }
    )

    assert errors and "已经在录制" in errors[0]


def test_stop_record_queries_status_for_output_path(qapp):
    client, socket = make_client()
    stopped: list[str] = []
    client.record_stopped.connect(stopped.append)
    client.stop_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")

    assert socket.sent[-1]["d"]["requestType"] == "StopRecord"
    respond(socket, True)
    assert socket.sent[-1]["d"]["requestType"] == "GetRecordStatus"
    respond(
        socket,
        True,
        {
            "outputActive": False,
            "outputPath": r"C:\Users\demo\Videos\recording.mp4",
        },
    )

    assert stopped == [r"C:\Users\demo\Videos\recording.mp4"]
    client.close()


def test_stop_record_falls_back_to_stop_response_path(qapp):
    client, socket = make_client()
    stopped: list[str] = []
    client.record_stopped.connect(stopped.append)
    client.stop_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")

    respond(socket, True, {"outputPath": r"C:\Videos\fallback.mp4"})
    respond(socket, True, {"outputActive": False})

    assert stopped == [r"C:\Videos\fallback.mp4"]
    client.close()


def test_stop_record_without_any_path_reports_empty(qapp):
    client, socket = make_client()
    stopped: list[str] = []
    client.record_stopped.connect(stopped.append)
    client.stop_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")

    respond(socket, True)
    respond(socket, True, {"outputActive": False})

    assert stopped == [""]
    client.close()


def test_start_record_rejects_output_not_configured(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.start_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")
    request = socket.sent[-1]
    socket.receive(
        {
            "op": 7,
            "d": {
                "requestType": "StartRecord",
                "requestId": request["d"]["requestId"],
                "requestStatus": {
                    "result": False,
                    "code": 500,
                    "comment": "output not configured",
                },
            },
        }
    )

    assert errors and "录制输出" in errors[0]


def test_missing_password_closes_with_4008(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.test_connection("127.0.0.1", 4455, "")
    socket.connected.emit()
    socket._close_code = 4008
    socket.disconnected.emit()

    assert errors and "要求 WebSocket 密码" in errors[0]


def test_get_record_status_emits_dict(qapp):
    client, socket = make_client()
    statuses: list[dict] = []
    client.record_status.connect(statuses.append)
    client.get_record_status("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")
    respond(
        socket,
        True,
        {"outputActive": True, "outputPath": "", "outputDuration": 1200},
    )

    assert statuses[-1]["outputActive"] is True
    client.close()


def test_request_failure_keeps_password_error_readable(qapp):
    client, socket = make_client()
    errors: list[str] = []
    client.failed.connect(errors.append)
    client.start_recording("127.0.0.1", 4455, "secret")
    complete_handshake(client, socket, "secret")
    request = socket.sent[-1]
    socket.receive(
        {
            "op": 7,
            "d": {
                "requestType": "StartRecord",
                "requestId": request["d"]["requestId"],
                "requestStatus": {
                    "result": False,
                    "code": 4009,
                    "comment": "authentication failed",
                },
            },
        }
    )

    assert errors and "密码错误" in errors[0]
