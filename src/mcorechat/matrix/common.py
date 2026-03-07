#!/usr/bin/env python3

import dataclasses
import enum
import hashlib
import json
from typing import Any

from mcorechat.common import JSONEncoder as CommonJSONEncoder

type MatrixEvent = dict[str, Any]


@dataclasses.dataclass(frozen=True)
class HomeserverURL:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class AppNamespace:
    value: str

    def __str__(self):
        return self.value

    def __len__(self):
        return len(self.value)


@dataclasses.dataclass(frozen=True)
class RoomName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class DisplayName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class RoomId:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class UserName:
    value: str

    def startswith(self, namespace: AppNamespace):
        return self.value.startswith(str(namespace))

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class DomainName:
    value: str

    def __str__(self):
        return self.value


class RoomVisibility(enum.Enum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclasses.dataclass(frozen=True)
class SecretText:
    value: str

    def __str__(self):
        return "********"

    def __repr__(self) -> str:
        return repr(str(self))


def sha256(*args: str) -> str:
    sha256_hash = hashlib.sha256()
    for part in args:
        sha256_hash.update(part.encode("utf-8"))
    return sha256_hash.hexdigest()


@dataclasses.dataclass(frozen=True)
class AliasName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class RoomAlias:
    name: AliasName
    domain: DomainName

    def __str__(self):
        return f"#{self.name}:{self.domain}"

    def startswith(self, app_namespace: AppNamespace):
        return str(self.name).startswith(str(app_namespace))


def parse_room_alias(raw: str):
    assert raw.startswith("#")
    raw_name, raw_domain = raw[1:].split(":", 1)
    return RoomAlias(AliasName(raw_name), DomainName(raw_domain))


@dataclasses.dataclass(frozen=True)
class UserId:
    name: UserName
    domain: DomainName

    def __str__(self):
        return f"@{self.name}:{self.domain}"


def parse_user_id(raw: str):
    assert raw.startswith("@")
    raw_name, raw_domain = raw[1:].split(":", 1)
    return UserId(UserName(raw_name), DomainName(raw_domain))


class RoomMembership(enum.Enum):
    INVITE = "invite"
    JOIN = "join"
    LEAVE = "leave"


@dataclasses.dataclass(frozen=True)
class RoomMember:
    user_id: UserId
    is_direct: bool
    membership: RoomMembership
    display_name: DisplayName | None


class MatrixAPIError(Exception):
    def __init__(self, status: int, errcode: str, error: str):
        self.status = status
        self.errcode = errcode
        self.error = error
        super().__init__(f"{status} {errcode}: {error}")


class MatrixJSONEncoder(CommonJSONEncoder):
    def default(self, o: Any) -> Any:
        if type(o) in (RoomAlias, UserId, DisplayName, UserName, RoomId, RoomName, DomainName, AliasName):
            return str(o)
        return super().default(o)


def matrix_jdump(obj: Any) -> Any:
    return json.dumps(obj, cls=MatrixJSONEncoder)
