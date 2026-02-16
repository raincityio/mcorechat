#!/usr/bin/env python3
import dataclasses
from typing import Any

from mesh2irc.common import ChannelName
from mesh2irc.matrix.common import DomainName, SecretText, HomeserverURL, UserName, AppNamespace


@dataclasses.dataclass(frozen=True)
class Config:
    domain: DomainName
    admin_user: UserName
    app_user: UserName
    app_as_token: SecretText
    app_hs_token: SecretText
    app_namespace: AppNamespace
    homeserver: HomeserverURL = HomeserverURL("http://localhost:8008")
    user_password: SecretText = SecretText("password")
    trusted_suffix: str | None = " [trusted]"
    enable_discovery_room: bool = True
    discovery_room_name: ChannelName = ChannelName("[discovery]")
    enable_advertisement_room: bool = True
    advertisement_room_name: ChannelName = ChannelName("[advertisements]")

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        field_types: dict[str, type] = {
            "domain": DomainName,
            "admin_user": UserName,
            "homeserver": HomeserverURL,
            "user_password": SecretText,
            "app_user": UserName,
            "app_as_token": SecretText,
            "app_hs_token": SecretText,
            "app_namespace": AppNamespace,
            "discovery_room_name": ChannelName,
            "advertisement_room_name": ChannelName,
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return Config(**kwargs)
