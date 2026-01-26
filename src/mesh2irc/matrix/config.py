#!/usr/bin/env python3
import dataclasses
from typing import Any

from mesh2irc.chatter import UserName
from mesh2irc.matrix.common import DomainName, SecretText


@dataclasses.dataclass(frozen=True)
class Config:
    domain: DomainName
    admin_user: UserName
    admin_password: SecretText
    homeserver: str = "http://localhost:8008"

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "domain" in data:
            kwargs["domain"] = DomainName(data["domain"])
        if "admin_user" in data:
            kwargs["admin_user"] = UserName(data["admin_user"])
        if "admin_password" in data:
            kwargs["admin_password"] = SecretText(data["admin_password"])
        return Config(**kwargs)
