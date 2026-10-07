"""[测试辅助] 仅绑定回环地址的原帧回放服务器（S1-12，A03）。

调用方显式传入预录或人工构造的请求/响应；服务端不引用被测协议解析逻辑。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplayStep:
    request: bytes
    response: bytes | None


class FakeTdxServer:
    """严格比对请求，按固定小块发送响应，None 响应用于模拟服务端断连。"""

    def __init__(self, steps: Sequence[ReplayStep], *, fragment_size: int = 3) -> None:
        self.steps = steps
        self.fragment_size = fragment_size
        self.requests: list[bytes] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(3)
        self.endpoint = self._listener.getsockname()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> FakeTdxServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._thread.join(timeout=4)
        self._listener.close()
        if exc_type is None:
            if self._thread.is_alive():
                raise AssertionError("fake TDX server did not terminate")
            if self.error is not None:
                raise AssertionError("fake TDX replay failed") from self.error

    def _run(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                connection.settimeout(3)
                for step in self.steps:
                    request = bytearray()
                    while len(request) < len(step.request):
                        part = connection.recv(len(step.request) - len(request))
                        if not part:
                            raise AssertionError("client disconnected before the expected request")
                        request.extend(part)
                    self.requests.append(bytes(request))
                    if bytes(request) != step.request:
                        raise AssertionError(f"request mismatch: {request.hex()} != {step.request.hex()}")
                    if step.response is None:
                        return
                    for offset in range(0, len(step.response), self.fragment_size):
                        connection.sendall(step.response[offset : offset + self.fragment_size])
        except BaseException as exc:
            self.error = exc
        finally:
            self._listener.close()
