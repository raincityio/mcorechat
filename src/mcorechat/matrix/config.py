#!/usr/bin/env python3
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcorechat.common import ChannelName, ContactName
from mcorechat.matrix.common import DomainName, SecretText, HomeserverURL, UserId, UserName, AppNamespace, parse_user_id

default_listen = ("127.0.0.1", 9000)
default_homeserver = HomeserverURL("http://127.0.0.1:8008")


@dataclasses.dataclass(frozen=True)
class SSLConfig:
    certfile: Path
    keyfile: Path
    enabled: bool = True

    @staticmethod
    def from_data(data: dict[str, Any]) -> "SSLConfig":
        field_types: dict[str, Callable[[Any], Any]] = {
            "certfile": Path,
            "keyfile": Path,
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return SSLConfig(**kwargs)


@dataclasses.dataclass(frozen=True)
class Config:
    domain: DomainName
    app_user: UserName
    app_namespace: AppNamespace
    app_as_token: SecretText | None = None
    app_as_token_path: Path | None = None
    app_hs_token: SecretText | None = None
    app_hs_token_path: Path | None = None
    listen: tuple[str, int] = default_listen
    homeserver: HomeserverURL = default_homeserver
    contact_suffix: str | None = " [contact]"
    advertisement_channel_name: ChannelName = ChannelName("[advertisements]")
    command_channel_name: ChannelName = ChannelName("[command]")
    ssl: SSLConfig | None = None
    dev_soft_fail: bool = False
    contact_name_mappings: dict[ContactName, UserId] = dataclasses.field(default_factory=dict[ContactName, UserId])

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        field_types: dict[str, Callable[[Any], Any]] = {
            "domain": DomainName,
            "homeserver": HomeserverURL,
            "app_user": UserName,
            "app_as_token": SecretText,
            "app_as_token_path": Path,
            "app_hs_token": SecretText,
            "app_hs_token_path": Path,
            "app_namespace": AppNamespace,
            "listen": tuple,
            "advertisement_room_name": ChannelName,
            "ssl": SSLConfig.from_data,
            "contact_name_mappings": lambda x: {ContactName(k): parse_user_id(v) for k, v in x.items()},
        }
        kwargs = data.copy()
        for key, cls in field_types.items():
            if key in data:
                kwargs[key] = cls(data[key])
        return Config(**kwargs)
