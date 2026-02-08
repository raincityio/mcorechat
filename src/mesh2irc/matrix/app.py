import json
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiohttp.web_exceptions import HTTPNotFound, HTTPBadRequest
from aiohttp.web_request import Request
from meshcore.events import Event
from nio import AsyncClient, LoginError

from mesh2irc.chatter import DirectCallback, ChannelCallback
from mesh2irc.common import ContactName, Message, ChannelName, Contact
from mesh2irc.matrix import common
from mesh2irc.matrix.common import (
    RoomAlias,
    RoomId,
    UserId,
    RoomMember,
    ChannelRoom,
    SecretText,
    UserName,
    parse_room_alias,
)
from mesh2irc.matrix.config import Config

logger = logging.getLogger(__name__)


class MatrixASChatter:

    def __init__(self, config: Config) -> None:
        self.config = config
        self.channel_callbacks = list[ChannelCallback]()
        self.room_cache: dict[RoomId, ChannelRoom] = {}
        self.user_cache: dict[UserId, ContactName] = {}
        self.admin_user = UserId(config.admin_user, config.domain)
        self.app_user = UserId(UserName(config.app_user), config.domain)
        self.client = SynapseClient(config.homeserver, config.app_as_token)

    ## Resource Helpers
    def create_user_id(
        self, *, contact_name: Optional[ContactName] = None, contact: Optional[Contact] = None
    ) -> UserId:
        if contact_name is not None:
            return UserId.create_from_contact_name(contact_name, self.config.domain, prefix=self.config.app_prefix)
        elif contact is not None:
            return UserId.create_from_contact(contact, self.config.domain, prefix=self.config.app_prefix)
        else:
            raise Exception(f"No contact name or contact provided")

    def parse_user_id(self, raw: str):
        return common.parse_user_id(self.config.app_prefix, raw)

    def create_room_alias(self, room_name: ChannelName):
        return RoomAlias.from_name(room_name, self.config.domain, prefix=self.config.app_prefix)

    ## Lifecycle
    async def init(self):
        # FIXME this doesn't work
        # await self.client.set_display_name(self.app_user, ContactName("Admin"))
        pass

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

        channel_name = ChannelName("Public")
        # room_alias = self.create_room_alias(room_name=channal_name)
        source = ContactName("tesla")
        message = Message("meow")
        print("Send A")
        await self.send_channel(source, message, None, channel_name)
        print("Send B")
        await self.send_channel(source, message, None, channel_name)
        print("Send C")
        # room_alias = self.create_room_alias(RoomName("barf"))
        # await self.delete_room_alias(room_alias)
        # room_id = await self.create_room(room_alias.name, invite=[self.admin_user])
        # room_id = await self.get_room_id_by_alias(room_alias)
        # await self.set_visi(room_id)
        # await self.cleanup()

    async def cleanup(self):
        for room_id in await self.client.get_public_rooms():
            await self.client.set_room_visibility(room_id, "private")

    ## Interface
    async def update_contact(self, contact: Contact) -> None:
        user_id = self.create_user_id(contact=contact)
        await self.ensure_user(user_id, contact.name)

    async def update_channel(self, channel_name: ChannelName) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        await self.ensure_room(channel_name, room_alias)

    async def send_direct(self, source: Contact, message: Message, event: Event) -> None:
        raise Exception()

    async def send_channel(
        self, source: ContactName, message: Message, event: Event, channel_name: ChannelName
    ) -> None:
        room_alias = self.create_room_alias(room_name=channel_name)
        room = await self.ensure_room(channel_name, room_alias)
        source_user_id = self.create_user_id(contact_name=source)
        await self.ensure_user(source_user_id, source)
        if source_user_id not in room.members:
            await self.client.invite_user(room.room_id, source_user_id)
        # TODO only try join if we aren't already
        await self.client.join_room(room.room_id, as_user_id=source_user_id)
        await self.client.send_message(room.room_id, message, as_user_id=source_user_id)

    async def ensure_room(self, room_name: ChannelName, room_alias: RoomAlias):
        room = next(filter(lambda x: room_alias in x.aliases, self.room_cache.values()), None)
        if room is not None:
            return room
        room_id = await self.client.get_room_id_by_alias(room_alias)
        if room_id is not None:
            return await self.get_room(room_id)
        room_id = await self.client.create_room(room_name, room_alias, invite=[self.admin_user])
        return await self.get_room(room_id)

    async def ensure_user(self, user_id: UserId, display_name: ContactName) -> None:
        test_display_name = self.user_cache.get(user_id, None)
        if test_display_name == display_name:
            return
        try:
            await self.client.register_user(user_id)
        except HTTPBadRequest:
            pass
        await self.client.set_display_name(user_id, display_name)
        self.user_cache[user_id] = display_name

    async def add_direct_callback(self, cb: DirectCallback) -> None:
        pass

    async def add_channel_callback(self, cb: ChannelCallback) -> None:
        self.channel_callbacks.append(cb)

    def parse_member(self, event: dict[str, Any]):
        user_id = self.parse_user_id(event["state_key"])
        is_direct = event["content"].get("is_direct", False)
        return RoomMember(user_id, is_direct)

    async def handle_room_create(self, event: dict[str, Any]) -> None:
        room_id = RoomId(event["room_id"])
        self.room_cache[room_id] = ChannelRoom(room_id)

    async def handle_room_name(self, event: dict[str, Any]) -> None:
        room_id = RoomId(event["room_id"])
        room = self.room_cache.get(room_id, None)
        if room is not None:
            name = ChannelName(event["content"]["name"])
            self.room_cache[room_id] = room.copy(name=name)

    async def handle_room_canonical_alias(self, event: dict[str, Any]) -> None:
        # m.room.canonical_alias
        room_id = RoomId(event["room_id"])
        alias = parse_room_alias(event["content"]["alias"])
        room = self.room_cache.get(room_id, None)
        if room is not None:
            new_aliases = room.aliases.copy()
            new_aliases.add(alias)
            self.room_cache[room_id] = room.copy(aliases=new_aliases)

    async def handle_room_member(self, event: dict[str, Any]) -> None:
        room_id = RoomId(event["room_id"])
        membership = event.get("content", {}).get("membership")
        member = self.parse_member(event)
        room = self.room_cache.get(room_id)
        if room is not None:
            if membership == "join":
                new_members = room.members.copy()
                new_members[member.user_id] = member
                new_room = room.copy(members=new_members)
                self.room_cache[room_id] = new_room
            elif membership == "leave":
                new_members = room.members.copy()
                new_members.pop(member.user_id, None)
                new_room = room.copy(members=new_members)
                self.room_cache[room_id] = new_room
        if membership == "invite":
            if member.user_id != self.admin_user:
                await self.client.join_room(room_id, as_user_id=member.user_id)

    async def handle_room_message(self, event: dict[str, Any]) -> None:
        event_id = event["event_id"]
        user_id = self.parse_user_id(event["sender"])
        if user_id != self.admin_user:
            return
        room_id = RoomId(event["room_id"])
        # TODO we shouldnt need as_user_id
        room = await self.get_room(room_id, as_user_id=user_id)
        message = Message(event["content"]["body"])
        if room.name is None:
            assert len(room.members) == 2
            for member in room.members:
                if member == self.admin_user:
                    continue
                logger.debug(member)
        else:
            logger.debug(room.name)

    async def get_room(
        self, room_id: RoomId, *, as_user_id: Optional[UserId] = None, force_update: Optional[bool] = None
    ):
        force_update = force_update or False
        if (not force_update) and (room_id in self.room_cache):
            return self.room_cache[room_id]
        state = await self.client.get_room_state(room_id, as_user_id=as_user_id)
        room_name: Optional[ChannelName] = None
        members: dict[UserId, RoomMember] = {}
        aliases: set[RoomAlias] = set()
        for event in state:
            if event["type"] == "m.room.member":
                member = self.parse_member(event)
                members[member.user_id] = member
            elif event["type"] == "m.room.name":
                room_name = ChannelName(event["content"]["name"])
            elif event["type"] == "m.room.canonical_alias":
                aliases.add(parse_room_alias(event["content"]["alias"]))
        room = ChannelRoom(room_id, room_name, members, aliases)
        self.room_cache[room_id] = room
        return room

    async def transactions(self, request: Request):
        self.verify_as_token(request)
        try:
            payload = await request.json()
            events = payload.get("events", [])
            for event in events:
                logging.debug(event)
                if event["type"] == "m.room.create":
                    await self.handle_room_create(event)
                elif event["type"] == "m.room.member":
                    await self.handle_room_member(event)
                elif event["type"] in ("m.room.encrypted", "m.room.message"):
                    await self.handle_room_message(event)
                elif event["type"] == "m.room.canonical_alias":
                    await self.handle_room_canonical_alias(event)
        except Exception as e:
            logging.exception(e)
        return web.json_response({})

    async def users(self, request: Request):
        self.verify_as_token(request)
        print(request)
        return web.json_response({})

    async def rooms(self, request: Request):
        self.verify_as_token(request)
        raw_alias = request.match_info["alias"]
        alias = parse_room_alias(raw_alias)
        if alias.domain != self.config.domain:
            return web.json_response({}, status=404)
        found = next(filter(lambda x: x.name == alias.name, self.room_cache.values()), None)
        if found is None:
            return web.json_response({}, status=404)
        return web.json_response({}, status=200)

    async def login_user(self, user_id: UserId, user_password: SecretText) -> AsyncClient:
        logger.debug(f"Login user: {user_id} @ {self.config.homeserver}")
        client = AsyncClient(self.config.homeserver, str(user_id))
        resp = await client.login(user_password.raw)
        if isinstance(resp, LoginError):
            await client.close()
            raise Exception(f"Login failed: {resp} {user_id}")
        return client

    def verify_as_token(self, request: Request):
        token = request.headers.get("Authorization")
        if token != f"Bearer {self.config.app_hs_token.raw}":
            raise web.HTTPUnauthorized()


class JSONEncoder(json.JSONEncoder):
    def default(self, o: Any):
        return super().default(o)


class SynapseClient:

    def __init__(self, homeserver: str, token: SecretText):
        self.homeserver = homeserver
        self.token = token

    async def set_display_name(self, user_id: UserId, display_name: ContactName) -> None:
        payload = {
            "displayname": str(display_name),
        }
        await self._put(["profile", str(user_id), "displayname"], as_user_id=user_id, payload=payload)

    async def register_user(self, user_id: UserId):
        payload = {"username": user_id.name, "type": "m.login.application_service"}
        await self._post(["register"], payload=payload)

    async def get_public_rooms(self):
        data = await self._get(["publicRooms"])
        return [RoomId(e) for e in data["chunk"]]

    async def set_room_visibility(self, room_id: RoomId, visibility: str):
        payload = {
            "visibility": visibility,
        }
        await self._put(["directory", "list", "room", str(room_id)], payload=payload)

    async def joined_rooms(self, *, as_user_id: Optional[UserId] = None):
        data = await self._get(["joined_rooms"], as_user_id=as_user_id)
        return [RoomId(e) for e in data["joined_rooms"]]

    async def create_room(self, room_name: ChannelName, room_alias: RoomAlias, invite: list[UserId]):
        payload: dict[str, Any] = {
            "name": str(room_name),
            "room_alias_name": str(room_alias.name),
            "visibility": "public",  # "private" or "public"
            "invite": [str(e) for e in invite],
            "preset": "public_chat",  # default preset
        }

        data = await self._post(["createRoom"], payload=payload)
        return RoomId(data["room_id"])

    async def delete_room_alias(self, alias: RoomAlias) -> None:
        return await self._delete(["directory", "room", str(alias)])

    async def get_room_id_by_alias(self, room_alias: RoomAlias):
        try:
            data = await self._get(["directory", "room", str(room_alias)])
            return RoomId(data["room_id"])
        except HTTPNotFound:
            return None

    async def send_message(self, room_id: RoomId, body: str, *, as_user_id: Optional[UserId] = None):
        txn_id = str(time.time())
        payload = {
            "msgtype": "m.text",
            "body": body,
        }

        await self._put(
            ["rooms", str(room_id), "send", "m.room.message", str(txn_id)],
            as_user_id=as_user_id,
            payload=payload,
        )

    async def join_room(
        self,
        room_id: RoomId,
        *,
        as_user_id: Optional[UserId] = None,
    ):
        await self._post(["join", room_id], as_user_id=as_user_id)

    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
    ):
        payload = {"user_id": str(user_id)}
        await self._post(["rooms", str(room_id), "invite"], payload=payload)

    async def get_room_state(self, room_id: RoomId, *, as_user_id: Optional[UserId] = None):
        return await self._get(["rooms", str(room_id), "state"], as_user_id=as_user_id)

    async def _put(self, *args: Any, **kwargs: Any) -> Any:
        return await self._http_client_verb("put", *args, **kwargs)

    async def _post(self, *args: Any, **kwargs: Any) -> Any:
        return await self._http_client_verb("post", *args, **kwargs)

    async def _delete(self, *args: Any, **kwargs: Any) -> Any:
        return await self._http_client_verb("delete", *args, **kwargs)

    async def _get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._http_client_verb("get", *args, **kwargs)

    async def _http_client_verb(
        self,
        verb: str,
        path: list[str],
        *,
        as_user_id: Optional[UserId] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> Any:
        if as_user_id is None:
            params = {}
        else:
            params = {
                "user_id": str(as_user_id),
            }
        payload = {} if payload is None else payload
        full_path = ["_matrix", "client", "v3"] + path
        quoted_path = "/".join([quote(e) for e in full_path])
        url = f"{self.homeserver}/{quoted_path}"
        headers = {
            "Authorization": f"Bearer {self.token.raw}",
            "Content-Type": "application/json",
        }
        logger.debug(f"sending {verb} {url} {params} {payload}")
        async with aiohttp.ClientSession(headers=headers) as session:
            if verb == "get":
                method = session.get
            elif verb == "post":
                method = session.post
            elif verb == "put":
                method = session.put
            elif verb == "delete":
                method = session.delete
            else:
                raise Exception(f"Unknown verb: {verb}")
            async with method(
                url,
                json=payload,
                params=params,
            ) as resp:
                if resp.status == 400:
                    raise HTTPBadRequest()
                if resp.status == 404:
                    raise HTTPNotFound()
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"{resp.status} {text}")
                data = await resp.json()
                return data
