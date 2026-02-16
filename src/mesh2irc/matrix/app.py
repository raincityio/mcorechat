import asyncio
import logging
from collections.abc import Callable, Awaitable

from aiohttp import web
from aiohttp.web_request import Request
from meshcore.events import Event

from mesh2irc.chatter import DirectCallback, ChannelCallback
from mesh2irc.common import ContactName, Message, ChannelName, Contact, PublicKey, HTMLMessage, MessageId
from mesh2irc.matrix import common
from mesh2irc.matrix.common import (
    DisplayName,
    MatrixAPIError,
    MatrixEvent,
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    ChannelRoom,
    parse_room_alias,
    RoomMembership,
    UserName,
)
from mesh2irc.matrix.config import Config
from mesh2irc.matrix.matrix_client import MatrixClient

type EventHandler = Callable[[MatrixEvent], Awaitable[None]]

logger = logging.getLogger(__name__)


class MatrixASChatter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.direct_callbacks = list[DirectCallback]()
        self.channel_callbacks = list[ChannelCallback]()
        self.room_cache: dict[RoomId, ChannelRoom] = {}
        self.room_cache_lock = asyncio.Lock()
        self.user_cache: dict[UserId, DisplayName] = {}
        self.app_user = UserId(config.app_user, config.domain)
        self.client = MatrixClient(config.homeserver, config.app_as_token)
        self.discovery_room_id: RoomId | None = None
        self.advertisement_room_id: RoomId | None = None
        self._event_handlers: dict[str, EventHandler] = {
            "m.room.create": self.handle_room_create,
            "m.room.member": self.handle_room_member,
            "m.room.canonical_alias": self.handle_room_canonical_alias,
            "m.room.name": self.handle_room_name,
            "m.room.message": self.handle_room_message,
        }

    ## Resource Helpers
    def create_user_id(self, *, contact_name: ContactName | None = None, contact: Contact | None = None) -> UserId:
        match (contact_name, contact):
            case (ContactName() as cn, None):
                return UserId.create_from_contact_name(cn, self.config.domain, prefix=self.config.app_prefix)
            case (None, Contact() as c):
                return UserId.create_from_contact(c, self.config.domain, prefix=self.config.app_prefix)
            case _:
                raise Exception("Exactly one of contact_name or contact must be provided")

    def parse_user_id(self, raw: str):
        return common.parse_user_id(self.config.app_prefix, raw)

    def create_room_alias(self, room_name: ChannelName):
        return RoomAlias.from_name(room_name, self.config.domain, prefix=self.config.app_prefix)

    def create_display_name(self, *, contact_name: ContactName | None = None, contact: Contact | None = None):
        match (contact_name, contact):
            case (ContactName() as cn, None):
                return DisplayName(str(cn))
            case (None, Contact() as c):
                return DisplayName(f"{str(c.name)}{self.config.trusted_suffix}")
            case _:
                raise Exception("Exactly one of contact_name or contact must be provided")

    ## Lifecycle
    async def init(self):
        # FIXME this doesn't work
        # synapse throws an exception in its log
        # await self.client.set_display_name(self.app_user, DisplayName("MeshBot"))
        if self.config.enable_discovery_room:
            discovery_room_name = self.config.discovery_room_name
            discovery_room_alias = self.create_room_alias(discovery_room_name)
            discovery_room = await self.ensure_room(discovery_room_name, discovery_room_alias)
            self.discovery_room_id = discovery_room.room_id
            for member in discovery_room.members.values():
                display_name = (
                    DisplayName(str(member.user_id.name)) if member.display_name is None else member.display_name
                )
                self.user_cache[member.user_id] = display_name
        if self.config.enable_advertisement_room:
            advertisement_room_name = self.config.advertisement_room_name
            advertisement_room_alias = self.create_room_alias(advertisement_room_name)
            advertisement_room = await self.ensure_room(advertisement_room_name, advertisement_room_alias)
            self.advertisement_room_id = advertisement_room.room_id

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

        site = web.TCPSite(runner, host="0.0.0.0", port=9000)
        await site.start()
        logger.debug(f"Started server on {9000}")

    ## Interface
    async def advertise(self, public_key: PublicKey, *, contact: Contact | None = None) -> None:
        if self.config.enable_advertisement_room:
            assert self.advertisement_room_id is not None
            message = Message(str(public_key))
            if contact is None:
                await self.client.send_message(self.advertisement_room_id, message)
            else:
                user_id = self.create_user_id(contact=contact)
                display_name = self.create_display_name(contact=contact)
                await self.ensure_user(user_id, display_name)
                room = await self.get_room(self.advertisement_room_id)
                await self.ensure_room_join_membership(room, user_id)
                await self.client.send_message(self.advertisement_room_id, message, as_user_id=user_id)

    async def update_contact(self, contact: Contact) -> None:
        user_id = self.create_user_id(contact=contact)
        await self.ensure_user(user_id, self.create_display_name(contact=contact))
        if self.discovery_room_id is not None:
            await self.ensure_room_join_membership(self.room_cache[self.discovery_room_id], user_id)

    async def update_channel(self, channel_name: ChannelName) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        await self.ensure_room(channel_name, room_alias)

    async def send_direct(self, source: Contact, destination: ContactName, message: Message, event: Event) -> None:
        source_user_id = self.create_user_id(contact=source)
        destination_user_id = UserId(UserName(str(destination)), self.config.domain)
        await self.ensure_user(source_user_id, self.create_display_name(contact=source))
        room = await self._find_dm_room(source_user_id, destination_user_id)
        if room is None:
            room_id = await self.client.create_direct_room(
                invite=[destination_user_id, self.app_user],
                as_user_id=source_user_id,
            )
            room = await self.get_room(room_id)
        await self.client.send_message(room.room_id, message, as_user_id=source_user_id)

    async def _find_dm_room(self, source_user_id: UserId, destination_user_id: UserId) -> ChannelRoom | None:
        def check_room(_room: ChannelRoom):
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

    async def ensure_room(self, room_name: ChannelName, room_alias: RoomAlias):
        room = next(filter(lambda x: room_alias == x.alias, self.room_cache.values()), None)
        if room is not None:
            return room
        room_id = await self.client.get_room_id_by_alias(room_alias)
        if room_id is None:
            room_id = await self.client.create_room(room_name, room_alias)
        return await self.get_room(room_id)

    async def send_channel_invite(self, contact_name: ContactName, channel_name: ChannelName) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        room = await self.ensure_room(channel_name, room_alias)
        user_id = UserId(UserName(str(contact_name)), self.config.domain)
        if user_id not in room.members:
            try:
                await self.client.invite_user(room.room_id, user_id)
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

    async def ensure_room_join_membership(self, room: ChannelRoom, user_id: UserId):
        room_member = room.members.get(user_id)
        if room_member is None:
            try:
                await self.client.invite_user(room.room_id, user_id)
            except MatrixAPIError as e:
                if e.status != 403:
                    raise
            await self.client.join_room(room.room_id, as_user_id=user_id)
        else:
            match room_member.membership:
                case RoomMembership.JOIN:
                    pass
                case RoomMembership.INVITE:
                    await self.client.join_room(room.room_id, as_user_id=user_id)
                case _:
                    raise Exception(f"Unknown membership: {room_member.membership}")

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        room_alias = self.create_room_alias(channel_name)
        room = await self.ensure_room(channel_name, room_alias)
        source_user_id = self.create_user_id(contact_name=source)
        if source_user_id not in room.members:
            await self.ensure_user(source_user_id, self.create_display_name(contact_name=source))
            await self.ensure_room_join_membership(room, source_user_id)
        await self.client.send_message(room.room_id, message, as_user_id=source_user_id)

    async def add_direct_callback(self, cb: DirectCallback) -> None:
        self.direct_callbacks.append(cb)

    async def add_channel_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.append(cb)

    def _parse_event_content(self, event: MatrixEvent, key: str) -> str | None:
        value = event.get("content", {}).get(key, None)
        return None if value == "" else value

    async def handle_room_create(self, event: MatrixEvent) -> None:
        # m.room.create
        room_id = RoomId(event["room_id"])
        async with self.room_cache_lock:
            if room_id not in self.room_cache:
                self.room_cache[room_id] = ChannelRoom(room_id)

    def parse_room_name(self, event: MatrixEvent) -> ChannelName | None:
        raw_name = self._parse_event_content(event, "name")
        return None if raw_name is None else ChannelName(raw_name)

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
        user_id = self.parse_user_id(event["state_key"])
        is_direct = event["content"].get("is_direct", False)
        membership = RoomMembership(event["content"]["membership"])
        display_name_raw = self._parse_event_content(event, "displayname")
        display_name = None if display_name_raw is None else DisplayName(display_name_raw)
        return RoomMember(user_id, is_direct, membership, display_name)

    # only valid if it's marked direct *and* a contact was used
    def is_direct_member(self, member: RoomMember) -> bool:
        return member.is_direct and (member.user_id.public_key is not None)

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
        return alias.startswith(self.config.app_prefix)

    def is_app_user_id(self, user_id: UserId) -> bool:
        if user_id.name.startswith(self.config.app_prefix):
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
        source_user_id = self.parse_user_id(event["sender"])
        # TODO is this correct
        if self.is_app_user_id(source_user_id):
            return
        room_id = RoomId(event["room_id"])
        room = await self.get_room(room_id)
        source_display_name = room.members[source_user_id].display_name
        if source_display_name is None:
            source = ContactName(str(source_user_id.name))
        else:
            source = ContactName(str(source_display_name))
        message = Message(event["content"]["body"])
        if self.is_app_room_alias(room.alias):
            assert room.name is not None
            for cb in self.channel_callbacks:
                try:
                    await cb(source, room.name, message, event_id)
                except Exception as e:
                    await self.send_error(room_id, "Failed to send message", cause=str(e))
        else:
            for member in room.members.values():
                if self.is_app_user_id(member.user_id) and self.is_direct_member(member):
                    assert member.user_id.public_key is not None
                    for cb in self.direct_callbacks:
                        try:
                            await cb(source, member.user_id.public_key, message, event_id)
                        except Exception as e:
                            await self.send_error(room_id, "Failed to send message", cause=str(e))

    def _build_room_from_state(self, room_id: RoomId, state: list[MatrixEvent]) -> ChannelRoom:
        room = ChannelRoom(room_id)
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
        user_id = self.parse_user_id(raw_user_id)
        if user_id in self.user_cache:
            return web.json_response({})
        if not str(user_id.name).startswith(str(self.config.app_prefix)):
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
        found = next(filter(lambda x: x.name == alias.name, self.room_cache.values()), None)
        if found is None:
            return web.json_response({}, status=404)
        return web.json_response({}, status=200)

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.config.app_hs_token.value}":
            raise web.HTTPUnauthorized()
