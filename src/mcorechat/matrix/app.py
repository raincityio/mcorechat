import dataclasses
import json
import logging
import ssl
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web_request import Request

from mcorechat.chatter import (
    ChannelCallback,
    UnknownContactException,
    UnknownChannelException,
    DirectCallback,
)
from mcorechat.common import ContactName, Message, ChannelName, Contact, HTMLMessage, MessageId, DisplayName
from mcorechat.matrix.common import (
    MatrixAPIError,
    MatrixEvent,
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    parse_room_alias,
    RoomMembership,
    UserName,
    RoomName,
    SecretText,
    parse_user_id,
    sha256,
    RoomVisibility,
)
from mcorechat.matrix.config import Config
from mcorechat.matrix.matrix_client import MatrixClient, parse_member_event

type EventHandler = Callable[[MatrixEvent], Awaitable[None]]

logger = logging.getLogger(__name__)


def get_secret(secret: SecretText | None, secret_path: Path | None) -> SecretText:
    match (secret, secret_path):
        case (SecretText(), None):
            return secret
        case (None, Path()):
            value = json.loads(secret_path.read_text())
            return SecretText(value)
        case _:
            raise Exception("Exactly one of secret_path or secret must be provided")


@dataclasses.dataclass(frozen=True)
class ContactInfo:
    contact: Contact
    room_id: RoomId
    alias: RoomAlias
    user_id: UserId


class ContactManager:
    def __init__(self) -> None:
        self.contacts_by_user_id: dict[UserId, ContactInfo] = {}
        self.contacts_by_room_id: dict[RoomId, ContactInfo] = {}

    def add(self, contact_info: ContactInfo) -> None:
        assert contact_info.user_id not in self.contacts_by_user_id
        assert contact_info.room_id not in self.contacts_by_room_id
        self.contacts_by_user_id[contact_info.user_id] = contact_info
        self.contacts_by_room_id[contact_info.room_id] = contact_info

    def get(self, *, user_id: UserId | None = None, room_id: RoomId | None = None) -> ContactInfo:
        try:
            match (user_id, room_id):
                case (UserId(), None):
                    return self.contacts_by_user_id[user_id]
                case (None, RoomId()):
                    return self.contacts_by_room_id[room_id]
                case _:
                    raise Exception("Exactly one of user_id, room_id must be provided")
        except KeyError as e:
            raise UnknownContactException() from e

    def __contains__(self, handle: UserId | RoomId) -> bool:
        match handle:
            case UserId():
                return handle in self.contacts_by_user_id
            case RoomId():
                return handle in self.contacts_by_room_id


type RoomHandler = Callable[[RoomId, UserId, Message, MessageId], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class RoomInfo:
    id: RoomId
    alias: RoomAlias
    name: RoomName
    handler: RoomHandler
    admin_id: UserId


class UnknownRoomException(Exception):
    pass


class RoomManager:
    def __init__(self) -> None:
        self.rooms_by_room_id: dict[RoomId, RoomInfo] = {}
        self.rooms_by_alias: dict[RoomAlias, RoomInfo] = {}

    def add(self, room_info: RoomInfo) -> None:
        assert room_info.id not in self.rooms_by_room_id
        assert room_info.alias not in self.rooms_by_alias
        self.rooms_by_room_id[room_info.id] = room_info
        self.rooms_by_alias[room_info.alias] = room_info

    def get(self, *, room_id: RoomId | None = None, room_alias: RoomAlias | None = None) -> RoomInfo:
        try:
            match (room_id, room_alias):
                case (RoomId(), None):
                    return self.rooms_by_room_id[room_id]
                case (None, RoomAlias()):
                    return self.rooms_by_alias[room_alias]
                case _:
                    raise Exception("Exactly one of room_id, room_alias must be provided")
        except KeyError as e:
            raise UnknownRoomException() from e

    def __contains__(self, handle: RoomId | RoomAlias) -> bool:
        match handle:
            case RoomId():
                return handle in self.rooms_by_room_id
            case RoomAlias():
                return handle in self.rooms_by_alias


@dataclasses.dataclass(frozen=True)
class UserIdentity:
    id: UserId
    display_name: DisplayName


@dataclasses.dataclass(frozen=True)
class RoomIdentity:
    alias: RoomAlias
    name: RoomName


class Vanity:
    def __init__(self, config: Config) -> None:
        self.config = config

    def create_user_id(
        self,
        identity: Contact,
        *,
        display_name: DisplayName | None = None,
        contact: Contact | None = None,
    ) -> UserId:
        match (display_name, contact):
            case (DisplayName(), None):
                user_name = UserName(f"{self.config.app_namespace}.channel.{sha256(str(display_name))}")
                return UserId(user_name, self.config.domain)
            case (None, Contact()):
                user_name = UserName(f"{self.config.app_namespace}.contact.{identity.public_key}.{contact.public_key}")
                return UserId(user_name, self.config.domain)
            case _:
                raise Exception("Exactly one of display_name or contact must be provided")

    def create_display_name(self, contact: Contact):
        return DisplayName(f"{str(contact.name)}{self.config.contact_suffix}")

    def create_user_identity(
        self, identity: Contact, *, contact: Contact | None = None, display_name: DisplayName | None = None
    ):
        match (display_name, contact):
            case (DisplayName(), None):
                user_id = self.create_user_id(identity, display_name=display_name)
                return UserIdentity(user_id, display_name)
            case (None, Contact()):
                user_id = self.create_user_id(identity, contact=contact)
                display_name = self.create_display_name(contact)
                return UserIdentity(user_id, display_name)
            case _:
                raise Exception("Exactly one of display_name or contact must be provided")

    def create_direct_alias(self, identity: Contact, contact: Contact):
        return RoomAlias(
            ChannelName(f"{self.config.app_namespace}.direct.{identity.public_key}.{contact.public_key}"),
            self.config.domain,
        )

    def create_direct_identity(self, identity: Contact, contact: Contact):
        alias = self.create_direct_alias(identity, contact)
        name = RoomName(str(self.create_display_name(contact)))
        return RoomIdentity(alias, name)

    def create_channel_alias(self, identity: Contact, channel_name: ChannelName):
        return RoomAlias(
            ChannelName(f"{self.config.app_namespace}.channel.{identity.public_key}.{sha256(str(channel_name))}"),
            self.config.domain,
        )

    def create_channel_identity(self, identity: Contact, channel_name: ChannelName):
        name = RoomName(str(channel_name))
        alias = self.create_channel_alias(identity, channel_name)
        return RoomIdentity(alias, name)

    def create_space_identity(self, identity: Contact):
        alias = RoomAlias(
            ChannelName(f"{self.config.app_namespace}.space.{identity.public_key}"),
            self.config.domain,
        )
        name = RoomName(str(identity.name))
        return RoomIdentity(alias, name)

    def map_contact_name(self, contact_name: ContactName):
        if contact_name in self.config.contact_name_mappings:
            return self.config.contact_name_mappings[contact_name]
        else:
            return UserId(UserName(str(contact_name)), self.config.domain)


#### hmm
class MatrixChatter:

    def __init__(
        self,
        identity: Contact,
        space_info: RoomInfo,
        *,
        vanity: Vanity,
        config: Config,
        room_manager: RoomManager,
        channel_callback: ChannelCallback,
        direct_callback: DirectCallback,
        client: MatrixClient,
        contact_manager: ContactManager,
        app_user: UserId,
        mcm: "MatrixChatterManager",
    ):
        self.identity = identity
        self.space_info = space_info
        self.vanity = vanity
        self.config = config
        self.room_manager = room_manager
        self.channel_callback = channel_callback
        self.direct_callback = direct_callback
        self.client = client
        self.contact_manager = contact_manager
        self.app_user = app_user
        self.mcm = mcm

    # Interface
    async def add_contact(self, contact: Contact) -> None:
        async def handler(_room_id: RoomId, _source_user_id: UserId, _message: Message, _message_id: MessageId) -> None:
            _room_member = await self.mcm.get_room_member(_room_id, _source_user_id, as_user_id=user_identity.id)
            if _room_member is None:
                return
            if _room_member.display_name is None:
                _source = DisplayName(str(_source_user_id.name))
            else:
                _source = _room_member.display_name
            if _source == identity_display_name:
                await self.direct_callback(_source, contact.public_key, _message, _message_id)

        identity_display_name = DisplayName(str(self.identity.name))
        user_identity = self.vanity.create_user_identity(self.identity, contact=contact)
        room_member = await self.ensure_room_member(self.space_info.id, user_identity)
        if room_member.display_name != user_identity.display_name:
            await self.client.set_display_name(user_identity.id, user_identity.display_name)
        direct_identity = self.vanity.create_direct_identity(self.identity, contact)
        room_info = await self.ensure_room(direct_identity, handler, is_direct=True, as_user_id=user_identity.id)
        contact_info = ContactInfo(contact, room_info.id, room_info.alias, user_identity.id)
        self.contact_manager.add(contact_info)

    async def add_channel(self, channel_name: ChannelName, *, callback: ChannelCallback | None = None) -> None:
        callback = self.channel_callback if callback is None else callback

        async def handler(_room_id: RoomId, _source_user_id: UserId, _message: Message, _message_id: MessageId):
            _room_member = await self.mcm.get_room_member(_room_id, _source_user_id)
            if _room_member is None:
                return
            if _room_member.display_name is None:
                _source = DisplayName(str(_source_user_id.name))
            else:
                _source = _room_member.display_name
            if _source == identity_display_name:
                await callback(_source, channel_name, _message, _message_id)

        identity_display_name = DisplayName(str(self.identity.name))
        room_identity = self.vanity.create_channel_identity(self.identity, channel_name)
        room_info = await self.ensure_room(room_identity, handler)
        await self.send_room_invite(room_info.id, self.identity.name)

    async def send_direct(self, source: Contact, message: Message) -> None:
        source_user_id = self.vanity.create_user_id(self.identity, contact=source)
        room_alias = self.vanity.create_direct_alias(self.identity, source)
        try:
            room_info = self.room_manager.get(room_alias=room_alias)
        except UnknownRoomException as e:
            raise UnknownContactException() from e
        destination_user_id = self.vanity.map_contact_name(self.identity.name)
        destination_room_member = await self.mcm.get_room_member(
            room_info.id, destination_user_id, as_user_id=source_user_id
        )
        if destination_room_member is None:
            invite = True
        elif destination_room_member.membership not in (RoomMembership.JOIN, RoomMembership.INVITE):
            invite = True
        else:
            invite = False
        if invite:
            await self.mcm.invite_user(room_info.id, destination_user_id, as_user_id=source_user_id)
        await self.client.send_message(room_info.id, message, as_user_id=source_user_id)

    async def send_channel(self, source: Contact | DisplayName, channel_name: ChannelName, message: Message) -> None:
        room_alias = self.vanity.create_channel_alias(self.identity, channel_name)
        try:
            room_info = self.room_manager.get(room_alias=room_alias)
        except UnknownRoomException as e:
            raise UnknownChannelException() from e
        match source:
            case Contact():
                source_identity = self.vanity.create_user_identity(self.identity, contact=source)
            case DisplayName():
                source_identity = self.vanity.create_user_identity(self.identity, display_name=source)
        await self.ensure_room_member(room_info.id, source_identity)
        await self.client.send_message(room_info.id, message, as_user_id=source_identity.id)

    # Support
    async def ensure_room(
        self,
        identity: RoomIdentity,
        handler: RoomHandler,
        *,
        is_direct: bool | None = None,
        as_user_id: UserId | None = None,
    ) -> RoomInfo:
        # public_chat
        if identity.alias in self.room_manager:
            return self.room_manager.get(room_alias=identity.alias)
        room_id = await self.client.get_room_id_by_alias(identity.alias)
        if room_id is None:
            if is_direct is None:
                visibility = RoomVisibility.PRIVATE
                preset = "trusted_private_chat"
            else:
                visibility = RoomVisibility.PUBLIC
                preset = "public_chat"
            room_id = await self.client.create_room(
                identity.name,
                identity.alias,
                visibility=visibility,
                preset=preset,
                is_direct=is_direct,
                as_user_id=as_user_id,
            )
            await self.client.state_attach_child(self.space_info.id, room_id, self.config.domain)
        else:
            test_room_name = await self.client.get_room_name(room_id, as_user_id=as_user_id)
            if identity.name != test_room_name:
                await self.client.set_room_name(room_id, identity.name, as_user_id=as_user_id)
        admin_id = self.app_user if as_user_id is None else as_user_id
        room_info = RoomInfo(room_id, identity.alias, identity.name, handler, admin_id)
        self.room_manager.add(room_info)
        return room_info

    async def ensure_room_member(self, room_id: RoomId, identity: UserIdentity):
        room_member = await self.mcm.get_room_member(room_id, identity.id)
        if room_member is None:
            await self.mcm.invite_user(room_id, identity.id)
            try:
                await self.client.join_room(room_id, as_user_id=identity.id)
            except MatrixAPIError as e:
                if e.errcode != "M_FORBIDDEN":
                    raise
                try:
                    await self.client.register_user(identity.id)
                    await self.client.set_display_name(identity.id, identity.display_name)
                except MatrixAPIError as e:
                    if e.errcode != "M_USER_IN_USE":
                        raise
                await self.client.join_room(room_id, as_user_id=identity.id)
            room_member = await self.mcm.get_room_member(room_id, identity.id)
            assert room_member is not None
        else:
            if room_member.membership == RoomMembership.JOIN:
                pass
            elif room_member.membership == RoomMembership.INVITE:
                await self.client.join_room(room_id, as_user_id=identity.id)
            else:
                raise Exception(f"Unexpected membership: {room_member.membership}")
        return room_member

    async def send_room_invite(self, room_id: RoomId, contact_name: ContactName) -> None:
        user_id = self.vanity.map_contact_name(contact_name)
        room_member = await self.mcm.get_room_member(room_id, user_id)
        if room_member is None:
            await self.mcm.invite_user(room_id, user_id)


class MatrixChatterManager:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.vanity = Vanity(config)
        self.contact_manager = ContactManager()
        self.room_manager = RoomManager()
        # this is a cache, but is updated with transactions, so mostly correct
        self.room_members: dict[tuple[RoomId, UserId], RoomMember] = {}  # unbounded
        self.app_user = UserId(config.app_user, config.domain)
        self.client = MatrixClient(config.homeserver, get_secret(config.app_as_token, config.app_as_token_path))
        self.app_hs_token = get_secret(config.app_hs_token, config.app_hs_token_path)
        self._event_handlers: dict[str, EventHandler] = {
            "m.room.member": self.handle_room_member,
            "m.room.message": self.handle_room_message,
        }

    async def run(self):
        app = web.Application()
        app.router.add_put(
            "/_matrix/app/v1/transactions/{txn_id}",
            self.transactions,
        )
        app.router.add_get(
            "/_matrix/app/v1/users/{user_id}",
            self.users,
        )
        app.router.add_get(
            "/_matrix/app/v1/rooms/{alias}",
            self.rooms,
        )

        runner = web.AppRunner(app)
        await runner.setup()

        if (self.config.ssl is not None) and self.config.ssl.enabled:
            ssl_context = ssl.create_default_context()
            ssl_context.load_cert_chain(self.config.ssl.certfile, self.config.ssl.keyfile)
        else:
            ssl_context = None

        host, port = self.config.listen
        site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_context)
        await site.start()
        logger.info(f"Started server on {host}:{port}")

    # Support
    async def ensure_space(
        self,
        identity: RoomIdentity,
    ) -> RoomInfo:
        if identity.alias in self.room_manager:
            return self.room_manager.get(room_alias=identity.alias)
        room_id = await self.client.get_room_id_by_alias(identity.alias)
        if room_id is None:
            room_id = await self.client.create_room(
                identity.name, identity.alias, visibility=RoomVisibility.PUBLIC, preset="public_chat", is_space=True
            )
        else:
            test_room_name = await self.client.get_room_name(room_id)
            if identity.name != test_room_name:
                await self.client.set_room_name(room_id, identity.name)

        async def handler(*_args: Any) -> None:
            assert False

        room_info = RoomInfo(room_id, identity.alias, identity.name, handler, self.app_user)
        self.room_manager.add(room_info)
        return room_info

    async def get_room_member(
        self, room_id: RoomId, user_id: UserId, *, as_user_id: UserId | None = None
    ) -> RoomMember | None:
        key = (room_id, user_id)
        if key in self.room_members:
            return self.room_members[key]
        member = await self.client.get_room_member(room_id, user_id, as_user_id=as_user_id)
        if member is not None:
            self.room_members[key] = member
        return member

    async def get_room_members(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        members = await self.client.get_room_members(room_id, as_user_id=as_user_id)
        for member in members:
            key = (room_id, member.user_id)
            self.room_members[key] = member
        return members

    # invite will throw 403 (forbidden) if the user was already invited or is in the room
    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
        *,
        ignore_forbidden: bool | None = None,
        as_user_id: UserId | None = None,
    ):
        ignore_forbidden = True if ignore_forbidden is None else ignore_forbidden
        try:
            await self.client.invite_user(room_id, user_id, as_user_id=as_user_id)
        except MatrixAPIError as e:
            if not ((e.status == 403) and ignore_forbidden):
                raise

    # service app interface
    async def handle_room_member(self, event: MatrixEvent) -> None:
        room_id = RoomId(event["room_id"])
        member = parse_member_event(event)
        key = (room_id, member.user_id)
        self.room_members[key] = member
        if member.membership == RoomMembership.INVITE:
            if room_id in self.room_manager:
                room_alias = self.room_manager.get(room_id=room_id).alias
            else:
                room_alias = None
            if self.is_app_user_id(member.user_id):
                if self.is_channel_room_alias(room_alias):
                    await self.client.join_room(room_id, as_user_id=member.user_id)
                else:
                    # ok, not a channel, join if it's direct and the contact is direct, and add app user too for management
                    if member.is_direct and (member.user_id in self.contact_manager):  # self.is_direct_member(member):
                        await self.client.join_room(room_id, as_user_id=member.user_id)
                        contact_info = self.contact_manager.get(user_id=member.user_id)
                        sender_user_id = parse_user_id(event["sender"])
                        await self.invite_user(contact_info.room_id, sender_user_id, as_user_id=member.user_id)
                        await self.client.tombstone_room(room_id, contact_info.room_id, as_user_id=member.user_id)

    def is_channel_room_alias(self, alias: RoomAlias | None) -> bool:
        if alias is None:
            return False
        return str(alias.name).startswith(f"{self.config.app_namespace}.channel.")

    def is_app_user_id(self, user_id: UserId):
        return user_id.name.startswith(self.config.app_namespace)

    async def send_error(
        self, room_id: RoomId, msg: str, *, cause: str | None = None, as_user_id: UserId | None = None
    ) -> None:
        if cause is None:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}</b></i>"), as_user_id=as_user_id)
        else:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}:</b> {cause}</i>"), as_user_id=as_user_id)

    async def handle_room_message(self, event: MatrixEvent) -> None:
        source_user_id = parse_user_id(event["sender"])
        if self.is_app_user_id(source_user_id) or (source_user_id == self.app_user):
            logger.debug(f"Local user {source_user_id}")
            return
        room_id = RoomId(event["room_id"])
        if room_id not in self.room_manager:
            logger.debug(f"Room {room_id} not found")
            return
        room_info = self.room_manager.get(room_id=room_id)

        message = Message(event["content"]["body"])
        message_id = MessageId(event["event_id"])

        try:
            await room_info.handler(room_id, source_user_id, message, message_id)
        except Exception as e:
            await self.send_error(room_id, "Failed to send message", cause=str(e), as_user_id=room_info.admin_id)

    async def transactions(self, request: Request):
        try:
            await self.transactions_(request)
            return web.json_response({})
        except Exception as e:
            if self.config.dev_soft_fail:
                logger.exception(e)
                return web.json_response({})
            return web.json_response({}, status=500)

    async def transactions_(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        payload = await request.json()
        events = payload.get("events", [])
        for event in events:
            logger.debug(event)
            handler = self._event_handlers.get(event["type"])
            if handler is not None:
                await handler(event)

    async def users(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_user_id = request.match_info["user_id"]
        user_id = parse_user_id(raw_user_id)
        if user_id in self.contact_manager:
            return web.json_response({})
        return web.json_response({}, status=404)

    async def rooms(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_alias = request.match_info["alias"]
        alias = parse_room_alias(raw_alias)
        if alias in self.room_manager:
            return web.json_response({})
        return web.json_response({}, status=404)

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.app_hs_token.value}":
            raise web.HTTPUnauthorized()

    async def add_chatter(
        self,
        identity: Contact,
        *,
        channels: list[ChannelName] | None = None,
        channel_callback: ChannelCallback,
        contacts: list[Contact] | None = None,
        direct_callback: DirectCallback,
    ) -> MatrixChatter:
        channels = [] if channels is None else channels
        contacts = [] if contacts is None else contacts

        # set up space
        space_identity = self.vanity.create_space_identity(identity)
        space_info = await self.ensure_space(space_identity)

        chatter = MatrixChatter(
            identity,
            space_info,
            vanity=self.vanity,
            config=self.config,
            room_manager=self.room_manager,
            channel_callback=channel_callback,
            direct_callback=direct_callback,
            client=self.client,
            contact_manager=self.contact_manager,
            app_user=self.app_user,
            mcm=self,
        )

        # TODO remove old contacts
        assert len(contacts) == 0
        # current_contacts = await self.get_room_members(space_info.id)
        # current_contact_set = {e.user_id for e in current_contacts}
        # auth_contact_set = {self.vanity.create_user_id(identity, contact=e) for e in contacts}

        # TODO remove old channels
        assert len(channels) == 0

        return chatter
