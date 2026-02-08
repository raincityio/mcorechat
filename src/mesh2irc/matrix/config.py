#!/usr/bin/env python3
import dataclasses
from typing import Any, Optional

from mesh2irc.matrix.common import DomainName, SecretText, HomeserverURL, UserName


@dataclasses.dataclass(frozen=True)
class Config:
    domain: DomainName
    admin_user: UserName
    admin_password: SecretText
    app_user: UserName
    app_as_token: SecretText
    app_hs_token: SecretText
    app_prefix: str
    homeserver: HomeserverURL = HomeserverURL("http://localhost:8008")
    user_password: SecretText = SecretText("password")
    trusted_suffix: Optional[str] = "[trusted]"

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        if "domain" in data:
            kwargs["domain"] = DomainName(data["domain"])
        if "admin_user" in data:
            kwargs["admin_user"] = UserName(data["admin_user"])
        if "admin_password" in data:
            kwargs["admin_password"] = SecretText(data["admin_password"])
        if "homeserver" in data:
            kwargs["homeserver"] = HomeserverURL(data["homeserver"])
        if "user_password" in data:
            kwargs["user_password"] = SecretText(data["user_password"])
        if "app_user" in data:
            kwargs["app_user"] = UserName(data["app_user"])
        if "app_as_token" in data:
            kwargs["app_as_token"] = SecretText(data["app_as_token"])
        if "app_hs_token" in data:
            kwargs["app_hs_token"] = SecretText(data["app_hs_token"])
        return Config(**kwargs)


__all__ = ["Config"]
