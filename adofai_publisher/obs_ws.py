from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

logger = logging.getLogger(__name__)

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

DEFAULT_OBS_HOST = "127.0.0.1"
DEFAULT_OBS_PORT = 4455
DEFAULT_RPC_VERSION = 1


class ObsError(Exception):
    """OBS WebSocket 操作的基类异常。"""


class ObsAuthError(ObsError):
    """OBS 认证失败或缺少密码。"""


def build_auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


def build_identify_message(hello: dict[str, Any], password: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "rpcVersion": int(hello.get("rpcVersion") or DEFAULT_RPC_VERSION),
        "eventSubscriptions": 0,
    }
    authentication = hello.get("authentication")
    if isinstance(authentication, dict):
        salt = str(authentication.get("salt") or "")
        challenge = str(authentication.get("challenge") or "")
        if not password:
            raise ObsAuthError(
                "OBS 要求输入 WebSocket 密码。请在「06 设置 → 录制（OBS）」里填写密码。"
            )
        data["authentication"] = build_auth_string(password, salt, challenge)
    return {"op": OP_IDENTIFY, "d": data}


def build_request_message(
    request_id: str,
    request_type: str,
    request_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "op": OP_REQUEST,
        "d": {
            "requestType": request_type,
            "requestId": request_id,
            "requestData": request_data or {},
        },
    }


def request_error_message(request_type: str, status: dict[str, Any]) -> str:
    comment = str(status.get("comment") or "").strip()
    lower = comment.casefold()
    try:
        code = int(status.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if (
        code in {4008, 4009}
        or "authentication" in lower
        or "password" in lower
    ):
        return "OBS 密码错误，请核对 OBS「工具 → WebSocket 服务器设置」里的密码。"
    if (
        "no output" in lower
        or "output not" in lower
        or "output is not" in lower
        or "no recording" in lower
        or "not configured" in lower
    ):
        return "OBS 未配置可用的录制输出，请先检查 OBS 的录制路径与输出模式。"
    if request_type == "StopRecord" and (
        "not recording" in lower or "not active" in lower
    ):
        return "OBS 当前没有在录制。"
    if request_type == "StartRecord" and (
        "already" in lower or "recording" in lower or "active" in lower
    ):
        return "OBS 已经在录制中，请先停止当前录制。"
    if "output" in lower and (
        "not" in lower or "invalid" in lower or "configure" in lower
    ):
        return "OBS 未配置可用的录制输出，请先检查 OBS 的录制路径与输出模式。"
    if "not recording" in lower:
        return "OBS 当前没有在录制。"
    return f"OBS 拒绝了请求（{request_type}）：{comment or '未知错误'}"


def connection_error_message(error: Any, error_text: str = "") -> str:
    if error == QAbstractSocket.SocketError.ConnectionRefusedError:
        return (
            "无法连接 OBS。请确认 OBS 已启动，并在「工具 → WebSocket 服务器设置」"
            "里启用服务器（默认端口 4455）；如果已经启用，请点「测试连接」重试。"
        )
    if error == QAbstractSocket.SocketError.HostNotFoundError:
        return "找不到 OBS 地址，请检查「06 设置 → 录制（OBS）」里的地址是否正确。"
    if error == QAbstractSocket.SocketError.RemoteHostClosedError:
        return "OBS 关闭了连接，请确认 OBS 仍在运行且 WebSocket 服务器没有关闭。"
    if error == QAbstractSocket.SocketError.TimeoutError:
        return (
            "连接 OBS 超时。请确认 OBS 已启动且 WebSocket 服务器已开启；"
            "如果刚启动 OBS，请稍后再点「测试连接」。"
        )
    detail = error_text.strip()
    return f"连接 OBS 失败：{detail}" if detail else "连接 OBS 失败，请检查地址、端口和密码。"


class ObsClient(QObject):
    connected = Signal()
    disconnected = Signal()
    failed = Signal(str)
    test_succeeded = Signal()
    record_started = Signal()
    record_stopped = Signal(str)
    record_status = Signal(dict)

    def __init__(
        self,
        parent: QObject | None = None,
        socket_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._socket_factory = socket_factory or QWebSocket
        self._socket: Any = None
        self._state = "idle"
        self._host = DEFAULT_OBS_HOST
        self._port = DEFAULT_OBS_PORT
        self._password = ""
        self._request_seq = 0
        self._pending: dict[str, tuple[str, Callable[[dict[str, Any]], None], QTimer]] = {}
        self._connect_callbacks: list[Callable[[], None]] = []
        self._close_requested = False
        self._failure_sent = False
        self._pending_socket_error: Any = None
        self._stop_output_path = ""
        self._stop_pending = False
        self._stop_poll_count = 0
        self._last_status: dict[str, Any] = {}
        self._socket_error_fallback = QTimer(self)
        self._socket_error_fallback.setSingleShot(True)
        self._socket_error_fallback.setInterval(350)
        self._socket_error_fallback.timeout.connect(self._flush_socket_error)
        self._connect_timeout = QTimer(self)
        self._connect_timeout.setSingleShot(True)
        self._connect_timeout.setInterval(6000)
        self._connect_timeout.timeout.connect(self._on_connect_timeout)

    @property
    def busy(self) -> bool:
        return self._state in {"connecting", "identifying"} or bool(self._pending)

    @property
    def is_connected(self) -> bool:
        return self._state == "connected"

    @property
    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)

    def test_connection(self, host: str, port: int, password: str) -> None:
        self.connect_to_obs(
            host,
            port,
            password,
            lambda: self._send_request(
                "GetRecordStatus", {}, self._on_test_status_response
            ),
        )

    def start_recording(self, host: str, port: int, password: str) -> None:
        self.connect_to_obs(
            host,
            port,
            password,
            lambda: self._send_request("StartRecord", {}, self._on_start_response),
        )

    def stop_recording(self, host: str, port: int, password: str) -> None:
        self.connect_to_obs(
            host,
            port,
            password,
            lambda: self._send_request("StopRecord", {}, self._on_stop_response),
        )

    def get_record_status(self, host: str, port: int, password: str) -> None:
        self.connect_to_obs(
            host,
            port,
            password,
            lambda: self._send_request(
                "GetRecordStatus", {}, self._on_record_status_response
            ),
        )

    def connect_to_obs(
        self,
        host: str,
        port: int,
        password: str,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        host = (host or DEFAULT_OBS_HOST).strip() or DEFAULT_OBS_HOST
        try:
            port = int(port)
        except (TypeError, ValueError):
            self._fail("OBS 端口无效，请填写 1–65535 之间的数字。")
            return
        if not 1 <= port <= 65535:
            self._fail("OBS 端口无效，请填写 1–65535 之间的数字。")
            return
        if (
            self._state == "connected"
            and host == self._host
            and port == self._port
            and password == self._password
        ):
            if on_ready is not None:
                on_ready()
            return
        if self._state in {"connecting", "identifying"}:
            self._fail("正在连接 OBS，请稍候再试。")
            return

        self._close_requested = True
        self._drop_socket()
        self._clear_pending()
        self._host = host
        self._port = port
        self._password = password
        self._state = "connecting"
        self._failure_sent = False
        self._close_requested = False
        self._connect_callbacks = [on_ready] if on_ready is not None else []

        socket = self._socket_factory()
        self._socket = socket
        socket.connected.connect(self._on_socket_connected)
        socket.textMessageReceived.connect(self._on_text_message)
        socket.disconnected.connect(self._on_socket_disconnected)
        socket.errorOccurred.connect(self._on_socket_error)
        self._connect_timeout.start()

        url = QUrl()
        url.setScheme("ws")
        url.setHost(host)
        url.setPort(port)
        socket.open(url)

    def close(self) -> None:
        self._close_requested = True
        self._connect_timeout.stop()
        self._socket_error_fallback.stop()
        self._pending_socket_error = None
        self._stop_pending = False
        self._stop_output_path = ""
        self._stop_poll_count = 0
        self._clear_pending()
        self._connect_callbacks.clear()
        self._state = "idle"
        self._drop_socket()

    def _drop_socket(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is None:
            return
        try:
            socket.close()
        except Exception:
            logger.debug("关闭 OBS WebSocket 失败", exc_info=True)
        try:
            socket.deleteLater()
        except Exception:
            pass

    def _clear_pending(self) -> None:
        for _request_type, _callback, timer in self._pending.values():
            timer.stop()
        self._pending.clear()

    def _fail(self, message: str) -> None:
        if self._failure_sent:
            return
        self._failure_sent = True
        self._connect_timeout.stop()
        self._socket_error_fallback.stop()
        self._pending_socket_error = None
        self._clear_pending()
        self._connect_callbacks.clear()
        self._state = "idle"
        self._close_requested = True
        self._stop_pending = False
        self._stop_output_path = ""
        self._stop_poll_count = 0
        self._drop_socket()
        self.failed.emit(message)

    def _send_json(self, payload: dict[str, Any]) -> bool:
        if self._socket is None:
            return False
        try:
            self._socket.sendTextMessage(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            logger.warning("向 OBS 发送消息失败", exc_info=True)
            self._fail(f"向 OBS 发送请求失败：{exc}")
            return False

    def _send_request(
        self,
        request_type: str,
        request_data: dict[str, Any],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        if self._state != "connected" or self._socket is None:
            self._fail("OBS 尚未连接，请先点「测试连接」。")
            return
        self._request_seq += 1
        request_id = f"adofai-go-{self._request_seq}"
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(8000)
        timer.timeout.connect(lambda rid=request_id: self._on_request_timeout(rid))
        self._pending[request_id] = (request_type, callback, timer)
        timer.start()
        if not self._send_json(build_request_message(request_id, request_type, request_data)):
            timer.stop()
            self._pending.pop(request_id, None)

    def _on_socket_connected(self) -> None:
        if self.sender() is not self._socket:
            return
        self._state = "identifying"

    def _on_text_message(self, raw: str) -> None:
        if self.sender() is not self._socket:
            return
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("OBS 返回了无法解析的消息：%s", raw[:200])
            return
        if not isinstance(message, dict):
            return
        op = message.get("op")
        data = message.get("d")
        if op == OP_HELLO and isinstance(data, dict):
            self._state = "identifying"
            try:
                identify = build_identify_message(data, self._password)
            except ObsAuthError as exc:
                self._fail(str(exc))
                return
            self._send_json(identify)
            return
        if op == OP_IDENTIFIED:
            self._connect_timeout.stop()
            self._state = "connected"
            self.connected.emit()
            callbacks, self._connect_callbacks = self._connect_callbacks, []
            for callback in callbacks:
                callback()
            return
        if op == OP_REQUEST_RESPONSE and isinstance(data, dict):
            self._handle_response(data)
            return
        if op == OP_EVENT:
            return

    def _handle_response(self, data: dict[str, Any]) -> None:
        request_id = str(data.get("requestId") or "")
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        request_type, callback, timer = pending
        timer.stop()
        status = data.get("requestStatus") or {}
        if not isinstance(status, dict) or not status.get("result"):
            if request_type == "GetRecordStatus" and self._resolve_stop_fallback():
                return
            self._fail(
                request_error_message(
                    request_type, status if isinstance(status, dict) else {}
                )
            )
            return
        response_data = data.get("responseData")
        callback(response_data if isinstance(response_data, dict) else {})

    def _on_start_response(self, _data: dict[str, Any]) -> None:
        self.record_started.emit()

    def _on_stop_response(self, data: dict[str, Any]) -> None:
        self._stop_output_path = str(data.get("outputPath") or "")
        self._stop_pending = True
        self._stop_poll_count = 0
        self._send_request(
            "GetRecordStatus", {}, self._on_record_status_response
        )

    def _on_test_status_response(self, data: dict[str, Any]) -> None:
        self._last_status = dict(data)
        self.record_status.emit(data)
        self.test_succeeded.emit()

    def _on_record_status_response(self, data: dict[str, Any]) -> None:
        self._last_status = dict(data)
        if self._stop_pending and data.get("outputActive") is True:
            if self._stop_poll_count < 10:
                self._stop_poll_count += 1
                QTimer.singleShot(
                    300,
                    lambda: self._send_request(
                        "GetRecordStatus", {}, self._on_record_status_response
                    ),
                )
                return
            self._stop_pending = False
            self._stop_output_path = ""
            self._stop_poll_count = 0
            self._fail(
                "OBS 仍在录制中，停止请求可能未生效。请稍后再试或检查 OBS 的录制输出。"
            )
            return
        self.record_status.emit(data)
        if not self._stop_pending:
            return
        path = str(data.get("outputPath") or self._stop_output_path or "")
        self._stop_pending = False
        self._stop_output_path = ""
        self.record_stopped.emit(path)

    def _resolve_stop_fallback(self) -> bool:
        if not self._stop_pending:
            return False
        path = self._stop_output_path
        self._stop_pending = False
        self._stop_output_path = ""
        if not path:
            return False
        self.record_stopped.emit(path)
        return True

    def _on_request_timeout(self, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        request_type = pending[0]
        if request_type == "GetRecordStatus" and self._resolve_stop_fallback():
            return
        self._fail("OBS 响应超时，请确认 OBS 正在运行且 WebSocket 服务器已开启。")

    def _on_connect_timeout(self) -> None:
        if self._state in {"connecting", "identifying"}:
            self._fail(
                "连接 OBS 超时：请确认 OBS 已启动，并在「工具 → WebSocket 服务器设置」"
                "里启用服务器（默认端口 4455）；如果刚启动 OBS，请稍后再点「测试连接」。"
            )

    def _on_socket_error(self, error: Any) -> None:
        if self.sender() is not self._socket:
            return
        if self._close_requested or self._failure_sent:
            return
        if self._state == "identifying":
            self._pending_socket_error = error
            self._socket_error_fallback.start()
            return
        error_text = ""
        if self._socket is not None:
            try:
                error_text = self._socket.errorString()
            except Exception:
                error_text = ""
        self._fail(connection_error_message(error, error_text))

    def _flush_socket_error(self) -> None:
        error = self._pending_socket_error
        self._pending_socket_error = None
        if error is None or self._state != "identifying":
            return
        self._fail(connection_error_message(error))

    def _on_socket_disconnected(self) -> None:
        if self.sender() is not self._socket:
            return
        if self._close_requested or self._failure_sent:
            return
        self._socket_error_fallback.stop()
        self._pending_socket_error = None
        code = 0
        if self._socket is not None:
            try:
                code = int(self._socket.closeCode())
            except Exception:
                code = 0
        if code == 4009:
            self._fail(
                "OBS 密码错误，请核对 OBS「工具 → WebSocket 服务器设置」里的密码。"
            )
            return
        if code == 4008:
            self._fail(
                "OBS 要求 WebSocket 密码，请在「06 设置 → 录制（OBS）」里填写密码。"
            )
            return
        if self._state in {"connecting", "identifying"}:
            self._fail("OBS 在握手阶段断开了连接，请检查密码和 WebSocket 服务器设置。")
            return
        self._state = "idle"
        self._drop_socket()
        self.disconnected.emit()
