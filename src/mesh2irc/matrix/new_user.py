import asyncio

import aiohttp
from nio import AsyncClient

from mesh2irc.matrix.config import Config


# HOMESERVER = "https://example.com"
# ADMIN_TOKEN = "ADMIN_ACCESS_TOKEN"

# NEW_USERNAME = "botuser"
# NEW_PASSWORD = "supersecretpassword"
# USER_ID = f"@{NEW_USERNAME}:example.com"


async def create_user(config: Config, admin_token: str, new_user_name: str):
    user_id = f"@{new_user_name}:{config.domain}"
    url = f"{config.homeserver}/_synapse/admin/v2/users/{user_id}"
    payload = {"password": "password2", "admin": False, "deactivated": False}
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


async def login_user(config: Config, admin_user_name: str, admin_password: str) -> str:
    user_id = f"@{admin_user_name}:{config.domain}"
    client = AsyncClient(config.homeserver, user_id)
    resp = await client.login(admin_password)
    if resp.access_token:
        print(f"Logged in! Access token: {resp.access_token}")
    else:
        raise Exception(f"Login failed: {resp}")
    return resp.access_token


async def amain():
    config = Config()
    admin_token = await login_user(config, "alice", "password2")
    await create_user(config, admin_token, "alice")
    # await login_user()


def main():
    asyncio.run(amain())
