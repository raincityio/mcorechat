import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from mesh2irc.common import ChannelName, HTMLMessage, Message
from mesh2irc.matrix.common import (
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
)
from mesh2irc.matrix.htmlutils import strip_html

logger = logging.getLogger(__name__)


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

    async def get_room_members(self, room_id: RoomId, *, as_user_id: UserId | None = None):
        data = await self._get(["rooms", room_id, "members"], as_user_id=as_user_id)
        print(data)

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

    async def create_room(
        self, room_name: ChannelName, room_alias: RoomAlias, *, invite: list[UserId] | None = None
    ) -> RoomId:
        payload: dict[str, Any] = {
            "name": room_name,
            "room_alias_name": room_alias.name,
            "visibility": RoomVisibility.PUBLIC.value,  # "private" or "public"
            "invite": [] if invite is None else [e for e in invite],
            "preset": "public_chat",  # default preset
        }

        data = await self._post(["createRoom"], payload=payload)
        return RoomId(data["room_id"])

    async def create_direct_room(self, invite: list[UserId], *, as_user_id: UserId | None = None) -> RoomId:
        payload: dict[str, Any] = {
            "visibility": RoomVisibility.PRIVATE.value,
            "invite": [e for e in invite],
            "is_direct": True,
            "preset": "trusted_private_chat",
        }
        data = await self._post(["createRoom"], payload=payload, as_user_id=as_user_id)
        return RoomId(data["room_id"])

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

    async def invite_user(
        self,
        room_id: RoomId,
        user_id: UserId,
        *,
        as_user_id: UserId | None = None,
    ):
        payload = {"user_id": user_id}
        await self._post(["rooms", room_id, "invite"], payload=payload, as_user_id=as_user_id)

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
