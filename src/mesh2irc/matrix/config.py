#!/usr/bin/env python3
import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class Config:
    domain: str = "example.com"
    homeserver: str = "http://localhost:8008"
    admin_user: str = "drew"
    admin_password: str = "usgp3140"

    @staticmethod
    def from_data(data: dict[str, Any]) -> "Config":
        kwargs = data.copy()
        return Config(**kwargs)
