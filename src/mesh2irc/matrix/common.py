#!/usr/bin/env python3

import dataclasses
import hashlib
from typing import NewType

from mesh2irc.common import ContactName, Contact

UserName = NewType("UserName", str)
DomainName = NewType("DomainName", str)
HomeserverURL = NewType("HomeserverURL", str)


@dataclasses.dataclass(frozen=True)
class SecretText:
    raw: str

    def __str__(self):
        return "********"

    def __repr__(self) -> str:
        return repr(str(self))


def sha256(text: str) -> str:
    utf8_bytes = text.encode("utf-8")
    sha256_hash = hashlib.sha256(utf8_bytes)
    return sha256_hash.hexdigest()


@dataclasses.dataclass(frozen=True)
class UserId:
    name: UserName
    domain: DomainName

    def __str__(self):
        return f"@{self.name}:{self.domain}"

    @staticmethod
    def create_from_contact(contact: Contact, domain: DomainName):
        user_name = UserName(f"t_{str(contact.public_key).lower()}")
        return UserId(user_name, DomainName(domain))

    @staticmethod
    def create_from_contact_name(contact_name: ContactName, domain: DomainName):
        user_name = UserName(f"u_{sha256(contact_name.raw)}")
        return UserId(user_name, DomainName(domain))

    @staticmethod
    def parse_user_id(raw: str):
        assert raw.startswith("@")
        user_raw, domain_raw = raw[1:].split(":", 1)
        return UserId(UserName(user_raw), DomainName(domain_raw))


__all__ = ["DomainName", "HomeserverURL", "SecretText", "sha256", "UserId"]
