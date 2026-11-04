"""Kiro CLI 3.x headless envelopes; ACP updates remain forward compatible."""

from typing import Any

import msgspec


class KiroEvent(msgspec.Struct, kw_only=True):
    type: str
    data: dict[str, Any]


_DECODER = msgspec.json.Decoder(KiroEvent)


def decode_event(line: bytes) -> KiroEvent:
    return _DECODER.decode(line)
