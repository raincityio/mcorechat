import asyncio

import aiohttp
from nio import AsyncClient

from mesh2irc.matrix.common import UserId
from mesh2irc.matrix.config import Config


async def create_user(config: Config, admin_token: str, user_id: UserId, display_name: str, user_password: str):
    # user_id = f"@{new_user_name}:{config.domain}"
    url = f"{config.homeserver}/_synapse/admin/v2/users/{user_id}"
    payload = {"password": user_password, "admin": False, "deactivated": False, "displayname": display_name}
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, json=payload) as resp:
            if resp.status == 200 or resp.status == 201:
                print(f"User created: {user_id}")
            # elif resp.status == 400:
            #     print(f"Bad request (maybe user exists?)")
            else:
                text = await resp.text()
                raise Exception(f"Failed ({resp.status}): {text}")


async def login_user(config: Config, user_id: UserId, user_password: str) -> AsyncClient:
    # user_id = f"@{admin_user_name}:{config.domain}"
    client = AsyncClient(config.homeserver, str(user_id))
    resp = await client.login(user_password)
    if resp.access_token:
        print(f"Logged in! Access token: {resp.access_token}")
    else:
        raise Exception(f"Login failed: {resp}")
    # return resp.access_token
    return client


async def amain():
    config = Config()
    admin_user_id = UserId.create(config.admin_user, config.domain)
    new_user_id = UserId.create("alice", config.domain)
    admin_token = (await login_user(config, admin_user_id, "password2")).access_token
    await create_user(config, admin_token, new_user_id)
    # await login_user()


def main():
    asyncio.run(amain())
