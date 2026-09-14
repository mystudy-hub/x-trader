"""Shared credential/terminal field classification for persistence and logging."""

import re

_PRIVATE_PARTS = (
    "password",
    "passwd",
    "authcode",
    "apikey",
    "secret",
    "accesstoken",
    "refreshtoken",
    "appid",
    "terminalpayload",
    "terminalinfo",
    "systeminfo",
    "macaddress",
    "ipaddress",
    "deviceid",
    "hardwareid",
    "investorid",
    "userid",
    "username",
    "authorization",
    "credential",
    "clientip",
    "localip",
)
_PRIVATE_NAMES = {"token", "mac", "ip", "authorization", "cookie", "credentials"}
_ASSIGNMENT = re.compile(
    r"""(?<![\w.-])(["']?)([^\W\d][\w.-]{0,127})\1(\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)"""
)


def is_private_field(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    chinese = ("密码", "密钥", "认证码", "令牌", "终端信息", "终端载荷", "设备标识", "硬件标识")
    return (
        normalized in _PRIVATE_NAMES
        or normalized.endswith("token")
        or any(part in normalized for part in _PRIVATE_PARTS)
        or any(part in name for part in chinese)
    )


def contains_private_assignment(text: str) -> bool:
    if ":" not in text and "=" not in text:
        return False
    return any(is_private_field(match.group(2)) for match in _ASSIGNMENT.finditer(text))


def redact_private_assignments(text: str, *, extra_private: frozenset[str] = frozenset()) -> str:
    def replace(match):
        name = match.group(2)
        normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
        if not is_private_field(name) and normalized not in extra_private and name.casefold() not in extra_private:
            return match.group(0)
        return match.group(1) + match.group(2) + match.group(1) + match.group(3) + "[REDACTED]"

    return _ASSIGNMENT.sub(replace, text) if ":" in text or "=" in text else text
