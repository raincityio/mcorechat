import hashlib
import hmac

import requests


async def create_user():
    # === CONFIG ===
    HOMESERVER = "http://localhost:8008"
    SHARED_SECRET = "DEVSECRET123"  # must match homeserver.yaml
    USERNAME = "bob"
    PASSWORD = "password"
    ADMIN = False  # True to make admin

    # === Compute MAC for registration ===
    # Synapse shared secret registration requires HMAC(username + admin flag) using the shared secret
    msg = f"{USERNAME}\n{str(ADMIN).lower()}".encode("utf-8")
    mac = hmac.new(SHARED_SECRET.encode("utf-8"), msg, hashlib.sha1).hexdigest()

    # === Send registration request ===
    url = f"{HOMESERVER}/_synapse/admin/v1/register"
    payload = {"username": USERNAME, "password": PASSWORD, "admin": ADMIN, "mac": mac}

    resp = requests.post(url, json=payload)
    if resp.status_code == 200:
        print(f"User @{USERNAME} created successfully!")
    elif resp.status_code == 400 and "already in use" in resp.text:
        print(f"User @{USERNAME} already exists.")
    else:
        print(f"Failed to create user: {resp.status_code}")
        print(resp.text)
