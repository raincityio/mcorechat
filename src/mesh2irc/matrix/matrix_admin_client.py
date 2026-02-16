import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from mesh2irc.matrix.common import (
    HomeserverURL,
    SecretText,
    UserId,
    RoomId,
    matrix_jdump,
    MatrixAPIError,
)

logger = logging.getLogger(__name__)


class MatrixAdminClient:

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

    # GET /_synapse/admin/v2/users
    async def list_users(self) -> list[UserId]:
        params = {"limit": "10000"}
        return await self._get(["users"], v=2, params=params)

    # GET /_synapse/admin/v1/rooms
    async def list_rooms(self) -> list[RoomId]:
        return await self._get(["rooms"])

    async def delete_room(self, room_id: RoomId):
        payload = {
            "block": False,
            "purge": True,
        }
        return await self._delete(["rooms", room_id], payload=payload)

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
        v: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        params = {} if params is None else params
        if as_user_id is not None:
            params["user_id"] = str(as_user_id)
        payload = {} if payload is None else payload
        v = 1 if v is None else v
        full_path = ["_synapse", "admin", f"v{v}"] + path
        quoted_path = "/".join(quote(str(e)) for e in full_path)
        url = f"{self.homeserver}/{quoted_path}"
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
