"""Shared credential/terminal field classification for persistence and observability boundaries."""

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
)
_PRIVATE_NAMES = {"token", "mac", "ip", "investorid", "userid", "authorization", "cookie", "credentials"}
_ASSIGNMENT = re.compile(r"""(?<![A-Za-z0-9_.-])["']?([A-Za-z_][A-Za-z0-9_.-]{0,127})["']?\s*[:=]\s*["']?\S+""")


def is_private_field(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return normalized in _PRIVATE_NAMES or any(part in normalized for part in _PRIVATE_PARTS)


def contains_private_assignment(text: str) -> bool:
    if ":" not in text and "=" not in text:
        return False
    return any(is_private_field(match.group(1)) for match in _ASSIGNMENT.finditer(text))
