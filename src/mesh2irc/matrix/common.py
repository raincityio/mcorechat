#!/usr/bin/env python3

import dataclasses
import enum
import hashlib
import json
from typing import Any, Optional

from mesh2irc.common import ContactName, Contact, PublicKey, ChannelName
from mesh2irc.common import JSONEncoder as CommonJSONEncoder

type MatrixEvent = dict[str, Any]


@dataclasses.dataclass(frozen=True)
class HomeserverURL:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class AppPrefix:
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
class RoomId:
    value: str

    def __str__(self):
        return self.value


@dataclasses.dataclass(frozen=True)
class UserName:
    value: str

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


def shallow_copy(obj: Any) -> Any:
    return {field.name: getattr(obj, field.name) for field in dataclasses.fields(obj)}


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
    def from_name(name: ChannelName, domain: DomainName, *, prefix: Optional[AppPrefix] = None):
        if prefix is None:
            return RoomAlias(name, domain)
        name = ChannelName(f"{prefix}{name}")
        return RoomAlias(name, domain)


def parse_room_alias(raw: str):
    assert raw.startswith("#")
    raw_name, raw_domain = raw[1:].split(":", 1)
    return RoomAlias(ChannelName(raw_name), DomainName(raw_domain))


@dataclasses.dataclass(frozen=True)
class UserId:
    name: UserName
    domain: DomainName
    public_key: Optional[PublicKey] = None

    def __str__(self):
        return f"@{self.name}:{self.domain}"

    @staticmethod
    def create_from_contact(contact: Contact, domain: DomainName, *, prefix: Optional[AppPrefix] = None):
        if prefix is None:
            user_name = UserName(f"t_{str(contact.public_key)}")
        else:
            user_name = UserName(f"{prefix}t_{str(contact.public_key)}")
        return UserId(user_name, domain, contact.public_key)

    @staticmethod
    def create_from_contact_name(contact_name: ContactName, domain: DomainName, *, prefix: Optional[AppPrefix] = None):
        if prefix is None:
            user_name = UserName(f"u_{sha256(str(contact_name))}")
        else:
            user_name = UserName(f"{prefix}u_{sha256(str(contact_name))}")
        return UserId(user_name, domain)


def parse_user_id(app_prefix: AppPrefix, raw: str):
    mesh_user_id_start = f"@{app_prefix}"
    if raw.startswith(mesh_user_id_start):
        prefixed_raw_user, raw_domain = raw[1:].split(":", 1)
        raw_user = prefixed_raw_user[len(app_prefix) :]
        if raw_user.startswith("t_"):
            public_key = PublicKey(raw_user[2:])
            return UserId(UserName(prefixed_raw_user), DomainName(raw_domain), public_key)
        elif raw_user.startswith("u_"):
            return UserId(UserName(prefixed_raw_user), DomainName(raw_domain), None)
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
    display_name: Optional[DisplayName]


@dataclasses.dataclass(frozen=True)
class ChannelRoom:
    room_id: RoomId
    name: Optional[ChannelName] = None
    members: dict[UserId, RoomMember] = dataclasses.field(default_factory=dict[UserId, RoomMember])
    alias: Optional[RoomAlias] = None

    def to_data(self):
        return shallow_copy(self)

    def copy(self, **kwargs: Any):
        return ChannelRoom(**(self.to_data() | kwargs))


class MatrixJSONEncoder(CommonJSONEncoder):
    def default(self, o: Any) -> Any:
        if type(o) in (RoomAlias, UserId, DisplayName, UserName):
            return str(o)
        return super().default(o)


def matrix_jdump(obj: Any) -> Any:
    return json.dumps(obj, cls=MatrixJSONEncoder)
