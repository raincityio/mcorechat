import logging
import time
from typing import Optional, Any
from urllib.parse import quote

import aiohttp
from aiohttp.web_exceptions import HTTPBadRequest, HTTPUnauthorized, HTTPNotFound

from mesh2irc.common import ContactName, ChannelName
from mesh2irc.matrix.common import SecretText, UserId, RoomId, RoomAlias, parse_room_alias

logger = logging.getLogger(__name__)


class MatrixClient:

    def __init__(self, homeserver: str, token: SecretText):
        self.homeserver = homeserver
        self.token = token
        headers = {
            "Authorization": f"Bearer {self.token.raw}",
            "Content-Type": "application/json",
        }
        self.session = aiohttp.ClientSession(headers=headers)

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
        logger.debug(f"sending {verb} {url} {params} {payload}")
        if verb == "get":
            method = self.session.get
        elif verb == "post":
            method = self.session.post
        elif verb == "put":
            method = self.session.put
        elif verb == "delete":
            method = self.session.delete
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


__all__ = ["MatrixClient"]
