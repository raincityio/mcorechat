import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from aiohttp import web
from aiohttp.web_exceptions import HTTPNotFound, HTTPBadRequest, HTTPUnauthorized
from aiohttp.web_request import Request
from meshcore.events import Event

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
        self.room_cache_lock = asyncio.Lock()
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
        # await self.cleanup()
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

    async def cleanup(self):
        for room_id in await self.client.get_public_rooms():
            for room_alias in await self.client.get_room_aliases(room_id):
                await self.client.delete_room_alias(room_alias)
            await self.client.set_room_visibility(room_id, "private")
        raise Exception("DONE")

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
            try:
                await self.client.invite_user(room.room_id, source_user_id)
            except HTTPUnauthorized:
                logger.warning(f"Duplciate invite")
                pass
        # TODO only try join if we aren't already
        await self.client.join_room(room.room_id, as_user_id=source_user_id)
        await self.client.send_message(room.room_id, message, as_user_id=source_user_id)

    async def ensure_room(self, room_name: ChannelName, room_alias: RoomAlias):
        room = next(filter(lambda x: room_alias == x.alias, self.room_cache.values()), None)
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

    @contextlib.asynccontextmanager
    async def update_room(self, room_id: RoomId):

        class Updater:
            def __init__(self, _room: Optional[ChannelRoom]):
                self.room = _room

            def __call__(self, **_kwargs: Any):
                assert self.room is not None
                self.room = self.room.copy(**_kwargs)

            def set(self, _room: ChannelRoom):
                assert self.room is None
                self.room = _room

        async with self.room_cache_lock:
            room = self.room_cache.get(room_id, None)
            updater = Updater(room)
            yield updater
            if updater.room is not None:
                self.room_cache[room_id] = updater.room

    async def handle_room_create(self, event: dict[str, Any]) -> None:
        # m.room.create
        room_id = RoomId(event["room_id"])
        async with self.update_room(room_id) as updater:
            if updater.room is None:
                updater.set(ChannelRoom(room_id))

    async def handle_room_name(self, event: dict[str, Any]) -> None:
        # m.room.name
        room_id = RoomId(event["room_id"])
        raw_name = event.get("content", {}).get("name", None)
        raw_name = None if raw_name == "" else raw_name
        name = None if raw_name is None else ChannelName(raw_name)
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                updater(name=name)

    async def handle_room_canonical_alias(self, event: dict[str, Any]) -> None:
        # m.room.canonical_alias
        room_id = RoomId(event["room_id"])
        raw_alias = event.get("content", {}).get("alias", None)
        raw_alias = None if raw_alias == "" else raw_alias
        alias = None if raw_alias is None else parse_room_alias(raw_alias)
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                updater(alias=alias)

    async def handle_room_member(self, event: dict[str, Any]) -> None:
        # m.room.member
        room_id = RoomId(event["room_id"])
        membership = event.get("content", {}).get("membership")
        member = self.parse_member(event)
        async with self.update_room(room_id) as updater:
            if updater.room is not None:
                if membership == "join":
                    new_members = updater.room.members.copy()
                    new_members[member.user_id] = member
                    updater(members=new_members)
                elif membership == "leave":
                    new_members = updater.room.members.copy()
                    new_members.pop(member.user_id, None)
                    updater(members=new_members)
        if membership == "invite":
            if member.user_id != self.admin_user:
                await self.client.join_room(room_id, as_user_id=member.user_id)

    async def handle_room_message(self, event: dict[str, Any]) -> None:
        # m.room.message
        event_id = event["event_id"]
        user_id = self.parse_user_id(event["sender"])
        if user_id != self.admin_user:
            return
        room_id = RoomId(event["room_id"])
        room = await self.get_room(room_id)
        message = Message(event["content"]["body"])
        if room.name is None:
            raise Exception()
        else:
            for cb in self.channel_callbacks:
                await cb(room.name, message, event_id)

    async def get_room(self, room_id: RoomId, *, as_user_id: Optional[UserId] = None):
        if room_id in self.room_cache:
            return self.room_cache[room_id]
        async with self.update_room(room_id) as updater:
            if updater.room is None:
                state = await self.client.get_room_state(room_id, as_user_id=as_user_id)
                room_name: Optional[ChannelName] = None
                members: dict[UserId, RoomMember] = {}
                alias: Optional[RoomAlias] = None
                # TODO hmm this should probably go through the same flow as the transaction
                for event in state:
                    if event["type"] == "m.room.member":
                        member = self.parse_member(event)
                        members[member.user_id] = member
                    elif event["type"] == "m.room.name":
                        room_name = ChannelName(event["content"]["name"])
                    elif event["type"] == "m.room.canonical_alias":
                        alias = parse_room_alias(event["content"]["alias"])
                room = ChannelRoom(room_id, room_name, members, alias)
                updater.set(room)
            else:
                room = updater.room
        return room

    async def transactions(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        try:
            payload = await request.json()
            events = payload.get("events", [])
            for event in events:
                logger.debug(event)
                if event["type"] == "m.room.create":
                    await self.handle_room_create(event)
                elif event["type"] == "m.room.member":
                    await self.handle_room_member(event)
                elif event["type"] == "m.room.canonical_alias":
                    await self.handle_room_canonical_alias(event)
                elif event["type"] == "m.room.name":
                    await self.handle_room_name(event)
                elif event["type"] in "m.room.message":
                    await self.handle_room_message(event)
        except Exception as e:
            logger.exception(e)
        return web.json_response({})

    async def users(self, request: Request):
        logger.debug(request)
        self.verify_as_token(request)
        return web.json_response({})

    # TODO consider returning 404 for all here
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
        return [RoomId(e["room_id"]) for e in data["chunk"]]

    async def get_room_members(self, room_id: RoomId, *, as_user_id: Optional[UserId] = None):
        data = await self._get(["rooms", str(room_id), "members"], as_user_id=as_user_id)
        print(data)

    async def get_room_aliases(self, room_id: RoomId, *, as_user_id: Optional[UserId] = None):
        data = await self._get(["rooms", str(room_id), "aliases"], as_user_id=as_user_id)
        return [parse_room_alias(e) for e in data["aliases"]]

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
                if resp.status == 403:
                    raise HTTPUnauthorized()
                if resp.status == 404:
                    raise HTTPNotFound()
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"{resp.status} {text}")
                data = await resp.json()
                return data
