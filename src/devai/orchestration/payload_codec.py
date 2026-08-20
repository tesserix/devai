"""Encrypted Temporal payload conversion for production workflow histories."""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

if TYPE_CHECKING:
    from devai.config import Settings

_ENCODING = b"binary/encrypted"
_AAD = b"devai-temporal-payload-v1"


class EncryptedPayloadCodec(PayloadCodec):
    """AES-256-GCM codec preserving the complete original Payload protobuf."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Temporal payload encryption key must decode to 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64(cls, value: str) -> EncryptedPayloadCodec:
        try:
            key = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("Temporal payload encryption key must be valid base64") from exc
        return cls(key)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        encoded: list[Payload] = []
        for payload in payloads:
            nonce = os.urandom(12)
            ciphertext = self._cipher.encrypt(nonce, payload.SerializeToString(), _AAD)
            encoded.append(
                Payload(
                    metadata={"encoding": _ENCODING},
                    data=nonce + ciphertext,
                )
            )
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") != _ENCODING:
                raise ValueError("Temporal history contains an unencrypted payload")
            if len(payload.data) < 29:
                raise ValueError("Temporal encrypted payload is truncated")
            plaintext = self._cipher.decrypt(payload.data[:12], payload.data[12:], _AAD)
            decoded.append(Payload.FromString(plaintext))
        return decoded


def temporal_data_converter(settings: Settings) -> DataConverter:
    key = str(getattr(settings, "temporal_payload_encryption_key", "") or "").strip()
    required = bool(getattr(settings, "temporal_payload_encryption_required", False))
    if not key:
        if required:
            raise ValueError("Temporal payload encryption key is required")
        return DataConverter.default
    return DataConverter(payload_codec=EncryptedPayloadCodec.from_base64(key))


__all__ = ["EncryptedPayloadCodec", "temporal_data_converter"]
