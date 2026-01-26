#!/usr/bin/env python3

import dataclasses
import hashlib


def string_to_sha(input_str: str) -> str:
    """
    Converts any Unicode string to a SHA-256 hex string.
    """
    # Encode as UTF-8 bytes
    utf8_bytes = input_str.encode("utf-8")

    # Compute SHA-256
    sha256_hash = hashlib.sha256(utf8_bytes)

    # Return hex digest
    return sha256_hash.hexdigest()


@dataclasses.dataclass(frozen=True)
class UserId:
    name: str
    domain: str

    def __str__(self):
        return f"@{self.name}:{self.domain}"

    @staticmethod
    def create(name: str, domain: str):
        return UserId(string_to_sha(name), domain)
