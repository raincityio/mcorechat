#!/usr/bin/env python3
import dataclasses
from typing import Any

from mesh2irc.common import ContactName
from mesh2irc.matrix.common import DomainName, SecretText, HomeserverURL


@dataclasses.dataclass(frozen=True)
class Config:
    domain: DomainName
    admin_user: ContactName
    admin_password: SecretText
    homeserver: HomeserverURL = HomeserverURL("http://localhost:8008")
    user_password: SecretText = SecretText("password")

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "domain" in data:
            kwargs["domain"] = DomainName(data["domain"])
        if "admin_user" in data:
            kwargs["admin_user"] = ContactName(data["admin_user"])
        if "admin_password" in data:
            kwargs["admin_password"] = SecretText(data["admin_password"])
        if "homeserver" in data:
            kwargs["homeserver"] = HomeserverURL(data["homeserver"])
        if "user_password" in data:
            kwargs["user_password"] = SecretText(data["user_password"])
        return Config(**kwargs)


__all__ = ["Config"]
