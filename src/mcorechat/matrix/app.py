import asyncio
import json
import logging
import ssl
from collections.abc import Callable, Awaitable
from pathlib import Path

from aiohttp import web
from aiohttp.web_request import Request
from meshcore.events import Event

from mcorechat.chatter import DirectCallback, ChannelCallback, CommandCallback
from mcorechat.common import ContactName, Message, ChannelName, Contact, PublicKey, HTMLMessage, MessageId, DisplayName
from mcorechat.matrix.common import (
    MatrixAPIError,
    MatrixEvent,
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    Room,
    parse_room_alias,
    RoomMembership,
    UserName,
    RoomName,
    SecretText,
    parse_user_id,
    sha256,
)
from mcorechat.matrix.config import Config
from mcorechat.matrix.matrix_client import MatrixClient

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


class MatrixASChatter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.direct_callbacks = dict[DisplayName, tuple[PublicKey, DirectCallback]]()
        self.channel_callbacks = dict[RoomAlias, tuple[PublicKey, ChannelName, ChannelCallback]]()
        self.room_cache: dict[RoomId, Room] = {}
        self.room_cache_lock = asyncio.Lock()
        self.user_cache: dict[UserId, DisplayName] = {}
        self.contacts = dict[UserId, Contact]()
        self.app_user = UserId(config.app_user, config.domain)
        self.client = MatrixClient(config.homeserver, get_secret(config.app_as_token, config.app_as_token_path))
        self.app_hs_token = get_secret(config.app_hs_token, config.app_hs_token_path)
        self.discovery_room_id: RoomId | None = None
        self.advertisement_room_id: RoomId | None = None
        self._event_handlers: dict[str, EventHandler] = {
            "m.room.create": self.handle_room_create,
            "m.room.member": self.handle_room_member,
            "m.room.canonical_alias": self.handle_room_canonical_alias,
            "m.room.name": self.handle_room_name,
            "m.room.message": self.handle_room_message,
        }

    async def add_command_callback(
        self, contact: Contact, cb: CommandCallback, *, invitees: list[ContactName] | None = None
    ):
        room_name = self.create_room_name(contact, self.config.command_channel_name)
        room_alias = self.create_room_alias(contact.public_key, self.config.command_channel_name)
        room = await self.ensure_room(room_name, room_alias)

        async def _cb(
            _identity: PublicKey,
            _source: DisplayName,
            _destination: ChannelName,
            _message: Message,
            _message_id: MessageId,
        ):
            try:
                for _line in await cb(_identity, _source, _message):
                    await self.client.send_message(room.id, Message(_line))
            except Exception as e:
                await self.send_error(room.id, str(e))

        await self.add_channel_callback(contact, self.config.command_channel_name, _cb, invitees=invitees)

    ## Resource Helpers
    def create_user_id(
        self,
        *,
        display_name: DisplayName | None = None,
        contact: Contact | None = None,
    ) -> UserId:
        match (display_name, contact):
            case (DisplayName(), None):
                user_name = UserName(f"{self.config.app_namespace}.u_{sha256(str(display_name))}")
                return UserId(user_name, self.config.domain)
            case (None, Contact()):
                user_name = UserName(f"{self.config.app_namespace}.t_{contact.public_key}")
                return UserId(user_name, self.config.domain)
            case _:
                raise Exception("Exactly one of display_name or contact must be provided")

    def create_room_alias(self, identity: PublicKey, room_name: ChannelName):
        return RoomAlias(ChannelName(f"{self.config.app_namespace}.{identity}.{room_name}"), self.config.domain)

    def create_room_name(self, contact: Contact, channel_name: ChannelName):
        return RoomName(f"{channel_name}")

    def create_display_name(self, contact_name: ContactName):
        return DisplayName(f"{str(contact_name)}{self.config.contact_suffix}")

    ## Lifecycle
    async def init(self, contact: Contact) -> None:
        # FIXME this doesn't work
        # synapse throws an exception in its log
        # await self.client.set_display_name(self.app_user, DisplayName("MeshBot"))
        if self.config.enable_discovery_room:
            discovery_channel_name = self.config.discovery_channel_name
            discovery_room_name = self.create_room_name(contact, discovery_channel_name)
            discovery_room_alias = self.create_room_alias(contact.public_key, discovery_channel_name)
            discovery_room = await self.ensure_room(discovery_room_name, discovery_room_alias)
            self.discovery_room_id = discovery_room.id
            for member in discovery_room.members.values():
                display_name = (
                    DisplayName(str(member.user_id.name)) if member.display_name is None else member.display_name
                )
                self.user_cache[member.user_id] = display_name
            await self.send_channel_invite(contact, discovery_channel_name, [contact.name])
        if self.config.enable_advertisement_room:
            advertisement_channel_name = self.config.advertisement_channel_name
            advertisement_room_name = self.create_room_name(contact, advertisement_channel_name)
            advertisement_room_alias = self.create_room_alias(contact.public_key, advertisement_channel_name)
            advertisement_room = await self.ensure_room(advertisement_room_name, advertisement_room_alias)
            self.advertisement_room_id = advertisement_room.id
            await self.send_channel_invite(contact, advertisement_channel_name, [contact.name])

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
        logger.debug(f"Started server on {host}:{port}")

    ## Interface
    async def advertise(self, identity: PublicKey, public_key: PublicKey, *, contact: Contact | None = None) -> None:
        if self.config.enable_advertisement_room:
            assert self.advertisement_room_id is not None
            message = Message(str(public_key))
            if contact is None:
                await self.client.send_message(self.advertisement_room_id, message)
            else:
                user_id = self.create_user_id(contact=contact)
                display_name = self.create_display_name(contact.name)
                await self.ensure_user(user_id, display_name)
                room = await self.get_room(self.advertisement_room_id)
                await self.ensure_room_join_membership(room, user_id)
                await self.client.send_message(room.id, message, as_user_id=user_id)

    async def update_contact(self, contact: Contact) -> None:
        user_id = self.create_user_id(contact=contact)
        self.contacts[user_id] = contact
        await self.ensure_user(user_id, self.create_display_name(contact.name))
        if self.discovery_room_id is not None:
            await self.ensure_room_join_membership(self.room_cache[self.discovery_room_id], user_id)

    # async def update_channel(self, contact: Contact, channel_name: ChannelName) -> None:
    #     room_alias = self.create_room_alias(room_name=channel_name)
    #     await self.ensure_room(channel_name, room_alias)

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None:
        source_user_id = self.create_user_id(contact=source)
        destination_user_id = UserId(UserName(str(destination)), self.config.domain)
        await self.ensure_user(source_user_id, self.create_display_name(source.name))
        room = await self._find_dm_room(source_user_id, destination_user_id)
        if room is None:
            room_id = await self.client.create_direct_room(
                invite=[destination_user_id, self.app_user],
                as_user_id=source_user_id,
            )
            room = await self.get_room(room_id)
        await self.client.send_message(room.id, message, as_user_id=source_user_id)

    async def _find_dm_room(self, source_user_id: UserId, destination_user_id: UserId) -> Room | None:
        def check_room(_room: Room):
            return _room.is_present(source_user_id) and _room.is_present(destination_user_id)

        for room in self.room_cache.values():
            if room.name is not None:
                continue
            if check_room(room):
                return room
        for room_id in await self.client.joined_rooms(as_user_id=source_user_id):
            room = await self.get_room(room_id, as_user_id=source_user_id)
            if room.name is not None:
                continue
            if check_room(room):
                return room
        return None

    async def ensure_room(self, room_name: RoomName, room_alias: RoomAlias):
        room = next(filter(lambda x: room_alias == x.alias, self.room_cache.values()), None)
        if room is not None:
            if room.name != room_name:
                await self.client.set_room_name(room.id, room_name)
            return room
        room_id = await self.client.get_room_id_by_alias(room_alias)
        if room_id is None:
            room_id = await self.client.create_room(room_name, room_alias)
        return await self.get_room(room_id)

    async def send_channel_invite(
        self, contact: Contact, channel_name: ChannelName, invitees: list[ContactName]
    ) -> None:
        for invitee in invitees:
            room_alias = self.create_room_alias(contact.public_key, channel_name)
            room_name = self.create_room_name(contact, channel_name)
            room = await self.ensure_room(room_name, room_alias)
            user_id = UserId(UserName(str(invitee)), self.config.domain)
            if user_id not in room.members:
                try:
                    await self.client.invite_user(room.id, user_id)
                except MatrixAPIError as e:
                    if e.status != 403:
                        raise

    async def ensure_user(self, user_id: UserId, display_name: DisplayName) -> None:
        test_display_name = self.user_cache.get(user_id, None)
        if test_display_name == display_name:
            return
        if test_display_name is None:
            try:
                await self.client.register_user(user_id)
            except MatrixAPIError as e:
                if e.errcode == "M_USER_IN_USE":
                    logger.debug(f"User {user_id} already registered")
                else:
                    raise
        await self.client.set_display_name(user_id, display_name)
        self.user_cache[user_id] = display_name

    async def ensure_room_join_membership(self, room: Room, user_id: UserId):
        room_member = room.members.get(user_id)
        if room_member is None:
            try:
                await self.client.invite_user(room.id, user_id)
            except MatrixAPIError as e:
                if e.status != 403:
                    raise
            await self.client.join_room(room.id, as_user_id=user_id)
        else:
            match room_member.membership:
                case RoomMembership.JOIN:
                    pass
                case RoomMembership.INVITE:
                    await self.client.join_room(room.id, as_user_id=user_id)
                case _:
                    raise Exception(f"Unknown membership: {room_member.membership}")

    async def send_channel(
        self, contact: Contact, source: DisplayName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        room_alias = self.create_room_alias(contact.public_key, channel_name)
        room_name = self.create_room_name(contact, channel_name)
        room = await self.ensure_room(room_name, room_alias)
        source_user_id = self.create_user_id(display_name=source)
        if source_user_id not in room.members:
            await self.ensure_user(source_user_id, source)
            await self.ensure_room_join_membership(room, source_user_id)
        await self.client.send_message(room.id, message, as_user_id=source_user_id)

    async def add_direct_callback(self, identity: PublicKey, source: DisplayName, cb: DirectCallback) -> None:
        assert source not in self.direct_callbacks
        self.direct_callbacks[source] = (identity, cb)

    async def add_channel_callback(
        self,
        contact: Contact,
        channel_name: ChannelName,
        cb: ChannelCallback,
        *,
        invitees: list[ContactName] | None = None,
    ) -> None:
        invitees = [] if invitees is None else invitees
        room_alias = self.create_room_alias(contact.public_key, channel_name)
        assert room_alias not in self.channel_callbacks
        room_name = self.create_room_name(contact, channel_name)
        await self.ensure_room(room_name, room_alias)
        await self.send_channel_invite(contact, channel_name, invitees)
        self.channel_callbacks[room_alias] = (contact.public_key, channel_name, cb)

    def _parse_event_content(self, event: MatrixEvent, key: str) -> str | None:
        value = event.get("content", {}).get(key, None)
        return None if value == "" else value

    async def handle_room_create(self, event: MatrixEvent) -> None:
        # m.room.create
        room_id = RoomId(event["room_id"])
        async with self.room_cache_lock:
            if room_id not in self.room_cache:
                self.room_cache[room_id] = Room(room_id)

    def parse_room_name(self, event: MatrixEvent) -> RoomName | None:
        raw_name = self._parse_event_content(event, "name")
        return None if raw_name is None else RoomName(raw_name)

    async def handle_room_name(self, event: MatrixEvent) -> None:
        # m.room.name
        room_id = RoomId(event["room_id"])
        async with self.room_cache_lock:
            room = self.room_cache.get(room_id)
            if room is not None:
                room.name = self.parse_room_name(event)

    def parse_room_canonical_alias(self, event: MatrixEvent) -> RoomAlias | None:
        raw_alias = self._parse_event_content(event, "alias")
        return None if raw_alias is None else parse_room_alias(raw_alias)

    async def handle_room_canonical_alias(self, event: MatrixEvent) -> None:
        # m.room.canonical_alias
        room_id = RoomId(event["room_id"])
        async with self.room_cache_lock:
            room = self.room_cache.get(room_id)
            if room is not None:
                room.alias = self.parse_room_canonical_alias(event)

    def parse_member(self, event: MatrixEvent):
        user_id = parse_user_id(event["state_key"])
        is_direct = event["content"].get("is_direct", False)
        membership = RoomMembership(event["content"]["membership"])
        display_name_raw = self._parse_event_content(event, "displayname")
        display_name = None if display_name_raw is None else DisplayName(display_name_raw)
        return RoomMember(user_id, is_direct, membership, display_name)

    # only valid if it's marked direct *and* a contact was used
    def is_direct_member(self, member: RoomMember) -> bool:
        contact = self.contacts.get(member.user_id)
        return member.is_direct and (contact is not None)

    async def handle_room_member(self, event: MatrixEvent) -> None:
        member = self.parse_member(event)
        room_id = RoomId(event["room_id"])
        async with self.room_cache_lock:
            room = self.room_cache.get(room_id)
            if room is not None:
                room.members[member.user_id] = member
        # only take action if we own the user
        if not self.is_app_user_id(member.user_id):
            return
        if member.membership == RoomMembership.INVITE:
            invite_room = self._build_room_from_state(room_id, event.get("invite_room_state", []))
            # If this is an app room alias then its an unambiguous channel that we created
            if self.is_app_room_alias(invite_room.alias):
                if self.is_direct_member(member):
                    # NOTE we don't support direct members in our channel rooms
                    # TODO feedback?
                    logger.error(f"Invalid channel invite: {invite_room} {member}")
                else:
                    await self.client.join_room(room_id, as_user_id=member.user_id)
            else:
                # ok, not a channel, join if it's direct and the contact is direct, and add app user too for management
                if self.is_direct_member(member):
                    await self.client.join_room(room_id, as_user_id=member.user_id)
                    try:
                        await self.client.invite_user(room_id, self.app_user, as_user_id=member.user_id)
                    except MatrixAPIError as e:
                        # the user was already invited, this error code seems like a bug
                        if e.status != 403:
                            raise
                    await self.client.join_room(room_id)
                else:
                    # NOTE we don't support non-direct members in non-channels
                    # TODO feedback?
                    logger.error(f"Invalid direct invite: {invite_room} {member}")

    def is_app_room_alias(self, alias: RoomAlias | None) -> bool:
        if alias is None:
            return False
        return alias.startswith(self.config.app_namespace)

    def is_app_user_id(self, user_id: UserId) -> bool:
        if user_id.name.startswith(self.config.app_namespace):
            return True
        if user_id == self.app_user:
            return True
        return False

    async def send_error(self, room_id: RoomId, msg: str, *, cause: str | None = None) -> None:
        if cause is None:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}</b></i>"))
        else:
            await self.client.send_message(room_id, HTMLMessage(f"<i><b>{msg}:</b> {cause}</i>"))

    async def handle_room_message(self, event: MatrixEvent) -> None:
        event_id = MessageId(event["event_id"])
        source_user_id = parse_user_id(event["sender"])
        # TODO is this correct
        if self.is_app_user_id(source_user_id):
            return
        room_id = RoomId(event["room_id"])
        room = await self.get_room(room_id)
        source_display_name = room.members[source_user_id].display_name
        if source_display_name is None:
            source = DisplayName(str(source_user_id.name))
        else:
            source = source_display_name
        message = Message(event["content"]["body"])
        if self.is_app_room_alias(room.alias):
            assert room.name is not None
            assert room.alias is not None
            cb = self.channel_callbacks.get(room.alias, None)
            if cb is None:
                await self.send_error(room_id, "Failed to send message", cause=f"Unknown channel: {room.name}")
            else:
                try:
                    await cb[2](cb[0], source, cb[1], message, event_id)
                except Exception as e:
                    await self.send_error(room_id, "Failed to send message", cause=str(e))
        else:
            for member in room.members.values():
                if self.is_app_user_id(member.user_id) and self.is_direct_member(member):
                    contact = self.contacts[member.user_id]
                    cb = self.direct_callbacks.get(source, None)
                    if cb is None:
                        await self.send_error(room_id, "Failed to send message", cause=f"Unknown source: {source}")
                    else:
                        try:
                            await cb[1](cb[0], source, contact.public_key, message, event_id)
                        except Exception as e:
                            await self.send_error(room_id, "Failed to send message", cause=str(e))

    def _build_room_from_state(self, room_id: RoomId, state: list[MatrixEvent]) -> Room:
        room = Room(room_id)
        for event in state:
            match event["type"]:
                case "m.room.member":
                    member = self.parse_member(event)
                    room.members[member.user_id] = member
                case "m.room.name":
                    room.name = self.parse_room_name(event)
                case "m.room.canonical_alias":
                    room.alias = self.parse_room_canonical_alias(event)
                case _:
                    pass
        return room

    async def get_room(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        if room_id in self.room_cache:
            return self.room_cache[room_id]
        async with self.room_cache_lock:
            room = self.room_cache.get(room_id)
            if room is None:
                state = await self.client.get_room_state(room_id, as_user_id=as_user_id)
                room = self._build_room_from_state(room_id, state)
                self.room_cache[room_id] = room
        return room

    async def transactions(self, request: Request):
        try:
            return await self.transactions_(request)
        except Exception as e:
            logger.exception(e)
            return web.json_response({})

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
        return web.json_response({})

    async def users(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_user_id = request.match_info["user_id"]
        user_id = parse_user_id(raw_user_id)
        if user_id in self.user_cache:
            return web.json_response({})
        if not str(user_id.name).startswith(str(self.config.app_namespace)):
            return web.json_response({}, status=404)
        try:
            await self.client.register_user(user_id)
            self.user_cache[user_id] = DisplayName(str(user_id.name))
        except MatrixAPIError as e:
            if e.errcode != "M_USER_IN_USE":
                return web.json_response({}, status=404)
        return web.json_response({})

    async def rooms(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        raw_alias = request.match_info["alias"]
        alias = parse_room_alias(raw_alias)
        if alias.domain != self.config.domain:
            return web.json_response({}, status=404)
        found = next(filter(lambda x: x.alias == alias, self.room_cache.values()), None)
        if found is None:
            return web.json_response({}, status=404)
        return web.json_response({}, status=200)

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.app_hs_token.value}":
            raise web.HTTPUnauthorized()
