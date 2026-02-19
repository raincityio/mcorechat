#!/usr/bin/env python3

import dataclasses
import enum
import hashlib
import json
from typing import Any

from mcorechat.common import ContactName, Contact, PublicKey, ChannelName
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
class DisplayName:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class RoomName:
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


def sha256(text: str) -> str:
    utf8_bytes = text.encode("utf-8")
    sha256_hash = hashlib.sha256(utf8_bytes)
    return sha256_hash.hexdigest()


@dataclasses.dataclass(frozen=True)
class RoomAlias:
    name: ChannelName
    domain: DomainName

    def __str__(self):
        return f"#{self.name}:{self.domain}"

    @staticmethod
    def from_name(app_namespace: AppNamespace, identity: PublicKey, name: ChannelName, domain: DomainName):
        return RoomAlias(ChannelName(f"{app_namespace}.{identity}.{name}"), domain)

    def startswith(self, app_namespace: AppNamespace):
        return str(self.name).startswith(str(app_namespace))


def parse_room_alias(raw: str):
    assert raw.startswith("#")
    raw_name, raw_domain = raw[1:].split(":", 1)
    return RoomAlias(ChannelName(raw_name), DomainName(raw_domain))


@dataclasses.dataclass(frozen=True)
class UserId:
    name: UserName
    domain: DomainName
    public_key: PublicKey | None = None

    def __str__(self):
        return f"@{self.name}:{self.domain}"

    @staticmethod
    def create_from_contact(app_namespace: AppNamespace, contact: Contact, domain: DomainName):
        user_name = UserName(f"{app_namespace}.t_{contact.public_key}")
        return UserId(user_name, domain, contact.public_key)

    @staticmethod
    def create_from_contact_name(app_namespace: AppNamespace, contact_name: ContactName, domain: DomainName):
        user_name = UserName(f"{app_namespace}.u_{sha256(str(contact_name))}")
        return UserId(user_name, domain)


def parse_user_id(app_namespace: AppNamespace, raw: str):
    if raw.startswith(f"@{app_namespace}."):
        namespaced_raw_user, raw_domain = raw[1:].split(":", 1)
        raw_user = namespaced_raw_user[len(app_namespace) + 1 :]
        if raw_user.startswith("t_"):
            public_key = PublicKey(raw_user[2:])
            return UserId(UserName(namespaced_raw_user), DomainName(raw_domain), public_key)
        elif raw_user.startswith("u_"):
            return UserId(UserName(namespaced_raw_user), DomainName(raw_domain), None)
        else:
            raise Exception(f"Invalid mesh user ID: {raw_user}")
    else:
        raw_user, raw_domain = raw[1:].split(":", 1)
        return UserId(UserName(raw_user), DomainName(raw_domain))


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


@dataclasses.dataclass
class Room:
    id: RoomId
    name: RoomName | None = None
    members: dict[UserId, RoomMember] = dataclasses.field(default_factory=dict[UserId, RoomMember])
    alias: RoomAlias | None = None

    def is_present(self, user_id: UserId) -> bool:
        return (user_id in self.members) and (
            self.members[user_id].membership in (RoomMembership.JOIN, RoomMembership.INVITE)
        )


class MatrixAPIError(Exception):
    def __init__(self, status: int, errcode: str, error: str):
        self.status = status
        self.errcode = errcode
        self.error = error
        super().__init__(f"{status} {errcode}: {error}")


class MatrixJSONEncoder(CommonJSONEncoder):
    def default(self, o: Any) -> Any:
        if type(o) in (RoomAlias, UserId, DisplayName, UserName, RoomId, RoomName):
            return str(o)
        return super().default(o)


def matrix_jdump(obj: Any) -> Any:
    return json.dumps(obj, cls=MatrixJSONEncoder)
