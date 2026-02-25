import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from mcorechat.common import HTMLMessage, Message
from mcorechat.matrix.common import (
    HomeserverURL,
    SecretText,
    UserId,
    RoomId,
    RoomAlias,
    matrix_jdump,
    MatrixAPIError,
    DisplayName,
    RoomVisibility,
    parse_room_alias,
    RoomName,
    RoomMember,
    MatrixEvent,
    parse_user_id,
    RoomMembership,
    DomainName,
)
from mcorechat.matrix.htmlutils import strip_html

logger = logging.getLogger(__name__)


def parse_member_state(user_id: UserId, state: dict[str, Any]) -> RoomMember:
    is_direct = state.get("is_direct", False)
    membership = RoomMembership(state["membership"])
    display_name_raw = state.get("displayname", None)
    display_name = None if (display_name_raw in (None, "")) else DisplayName(display_name_raw)
    return RoomMember(user_id, is_direct, membership, display_name)


def parse_member_event(event: MatrixEvent):
    user_id = parse_user_id(event["state_key"])
    return parse_member_state(user_id, event["content"])


class MatrixClient:

    def __init__(self, homeserver: HomeserverURL, token: SecretText):
        self.homeserver = homeserver
        self.token = token
        headers = {
            "Authorization": f"Bearer {self.token.value}",
            "Content-Type": "application/json",
        }
        self.session = aiohttp.ClientSession(headers=headers, json_serialize=matrix_jdump)

    async def close(self):
        await self.session.close()

    async def get_display_name(self, user_id: UserId) -> DisplayName | None:
        data = await self._get(["profile", user_id, "displayname"], as_user_id=user_id)
        raw_display_name = data.get("displayname", None)
        if (raw_display_name is None) or (raw_display_name == ""):
            return None
        return DisplayName(raw_display_name)

    async def set_display_name(self, user_id: UserId, display_name: DisplayName) -> None:
        payload = {
            "displayname": display_name,
        }
        await self._put(["profile", user_id, "displayname"], as_user_id=user_id, payload=payload)

    async def register_user(self, user_id: UserId):
        payload = {"username": user_id.name, "type": "m.login.application_service"}
        await self._post(["register"], payload=payload)

    async def get_public_rooms(self):
        data = await self._get(["publicRooms"])
        return [RoomId(e["room_id"]) for e in data["chunk"]]

    # GET /_matrix/client/v3/rooms/{roomId}/state/m.room.member/{userId}
    async def get_room_member(self, room_id: RoomId, user_id: UserId, *, as_user_id: UserId | None = None):
        try:
            data = await self._get(["rooms", room_id, "state", "m.room.member", user_id], as_user_id=as_user_id)
            return parse_member_state(user_id, data)
        except MatrixAPIError as e:
            if e.status == 403:
                if e.error.startswith(f"User {user_id} not in room {room_id}"):
                    return None
            elif e.status == 404:
                return None
            raise

    async def get_room_members(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        data = await self._get(["rooms", room_id, "members"], as_user_id=as_user_id)
        return [parse_member_event(e) for e in data.get("chunk", [])]

    async def set_room_alias(self, room_id: RoomId, alias: RoomAlias):
        payload = {
            "room_id": room_id,
        }
        await self._put(["directory", "room", alias], payload=payload)

    async def get_room_aliases(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        data = await self._get(["rooms", room_id, "aliases"], as_user_id=as_user_id)
        return [parse_room_alias(e) for e in data["aliases"]]

    async def set_room_visibility(self, room_id: RoomId, visibility: RoomVisibility):
        payload = {
            "visibility": visibility.value,
        }
        await self._put(["directory", "list", "room", room_id], payload=payload)

    async def joined_rooms(self, *, as_user_id: UserId | None = None):
        data = await self._get(["joined_rooms"], as_user_id=as_user_id)
        return [RoomId(e) for e in data["joined_rooms"]]

    # PUT /_matrix/client/v3/rooms/{space_id}/state/m.space.child/{child_room_id}
    async def state_attach_child(
        self, space_id: RoomId, child_room_id: RoomId, domain: DomainName, *, as_user_id: UserId | None = None
    ):
        payload = {"via": [domain]}
        await self._put(
            ["rooms", space_id, "state", "m.space.child", child_room_id], payload=payload, as_user_id=as_user_id
        )

    async def create_room(
        self, name: RoomName, alias: RoomAlias, *, is_space: bool | None = None, as_user_id: UserId | None = None
    ) -> RoomId:
        payload: dict[str, Any] = {
            "name": name,
            "room_alias_name": alias.name,
            "visibility": RoomVisibility.PUBLIC.value,  # "private" or "public"
            "preset": "public_chat",  # default preset
        }
        if is_space:
            payload["creation_content"] = {"type": "m.space"}
        data = await self._post(["createRoom"], payload=payload, as_user_id=as_user_id)
        return RoomId(data["room_id"])

    async def create_space(self, name: RoomName, alias: RoomAlias, *, as_user_id: UserId | None = None) -> RoomId:
        payload = {
            "name": name,
            "room_alias_name": alias.name,
            "visibility": RoomVisibility.PUBLIC.value,  # "private" or "public"
            "preset": "public_chat",  # default preset
            "creation_content": {"type": "m.space"},
        }
        data = await self._post(["createRoom"], payload=payload, as_user_id=as_user_id)
        return RoomId(data["room_id"])

    async def create_direct_room(self, name: RoomName, alias: RoomAlias, *, as_user_id: UserId | None = None) -> RoomId:
        payload: dict[str, Any] = {
            "name": name,
            "room_alias_name": alias.name,
            "visibility": RoomVisibility.PRIVATE.value,
            "preset": "trusted_private_chat",
            "is_direct": True,
        }
        data = await self._post(["createRoom"], payload=payload, as_user_id=as_user_id)
        return RoomId(data["room_id"])

    # PUT /_matrix/client/v3/rooms/{roomId}/state/m.room.power_levels
    async def get_room_power_levels(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        data = await self._get(["rooms", room_id, "state", "m.room.power_levels"], as_user_id=as_user_id)
        print(data)

    async def delete_room_alias(self, alias: RoomAlias) -> None:
        return await self._delete(["directory", "room", alias])

    async def get_room_id_by_alias(self, room_alias: RoomAlias):
        try:
            data = await self._get(["directory", "room", room_alias])
            return RoomId(data["room_id"])
        except MatrixAPIError as e:
            if e.status == 404:
                return None
            raise

    # PUT /_matrix/client/v3/rooms/{roomId}/state/m.room.tombstone
    async def tombstone_room(self, room_id: RoomId, replacement_room: RoomId, *, as_user_id: UserId | None = None):
        payload = {
            "replacement_room": replacement_room,
        }
        await self._put(["rooms", room_id, "state", "m.room.tombstone"], payload=payload, as_user_id=as_user_id)

    async def send_message(self, room_id: RoomId, body: Message | HTMLMessage, *, as_user_id: UserId | None = None):
        txn_id = str(time.time())
        # 'format': 'org.matrix.custom.html'
        # 'formatted_body': ''

        match body:
            case Message():
                payload = {
                    "msgtype": "m.text",
                    "body": body,
                }
            case HTMLMessage():
                payload = {
                    "format": "org.matrix.custom.html",
                    "formatted_body": body.value,
                    "msgtype": "m.text",
                    "body": strip_html(body.value),
                }

        await self._put(
            ["rooms", room_id, "send", "m.room.message", txn_id],
            as_user_id=as_user_id,
            payload=payload,
        )

    async def join_room(
        self,
        room_id: RoomId,
        *,
        as_user_id: UserId | None = None,
    ):
        await self._post(["join", room_id], as_user_id=as_user_id)

    async def leave_room(
        self,
        room_id: RoomId,
        *,
        as_user_id: UserId | None = None,
    ):
        await self._post(["rooms", room_id, "leave"], as_user_id=as_user_id)

    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
        *,
        as_user_id: UserId | None = None,
    ):
        payload = {"user_id": user_id}
        await self._post(["rooms", room_id, "invite"], payload=payload, as_user_id=as_user_id)

    # PUT /_matrix/client/v3/rooms/{roomId}/state/m.room.name
    async def set_room_name(self, room_id: RoomId, name: RoomName, *, as_user_id: UserId | None = None):
        payload = {"name": name}
        return await self._put(["rooms", room_id, "state", "m.room.name"], payload=payload, as_user_id=as_user_id)

    async def delete_room_name(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        await self._delete(["rooms", room_id, "state", "m.room.name"], as_user_id=as_user_id)

    async def get_room_name(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        data = await self._get(["rooms", room_id, "state", "m.room.name"], as_user_id=as_user_id)
        return RoomName(data["name"])

    async def get_room_state(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        return await self._get(["rooms", room_id, "state"], as_user_id=as_user_id)

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
        as_user_id: UserId | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if as_user_id is None:
            params = {}
        else:
            params = {
                "user_id": str(as_user_id),
            }
        payload = {} if payload is None else payload
        full_path = ["_matrix", "client", "v3"] + path
        quoted_path = "/".join(quote(str(e)) for e in full_path)
        url = f"{self.homeserver}/{quoted_path}"
        logger.debug(f"sending {verb} {url} {params} {payload}")
        verbs = {
            "get": self.session.get,
            "post": self.session.post,
            "put": self.session.put,
            "delete": self.session.delete,
        }
        method = verbs.get(verb)
        if method is None:
            raise Exception(f"Unknown verb: {verb}")
        async with method(
            url,
            json=payload,
            params=params,
        ) as resp:
            if resp.status != 200:
                data = await resp.json()
                raise MatrixAPIError(resp.status, data.get("errcode", ""), data.get("error", ""))
            data = await resp.json()
            return data
