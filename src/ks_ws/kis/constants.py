"""KIS OpenAPI base URLs and well-known endpoint paths.

Reference: https://apiportal.koreainvestment.com/apiservice
"""

from typing import Literal

Env = Literal["mock", "live"]

REST_BASE_URL: dict[Env, str] = {
    "live": "https://openapi.koreainvestment.com:9443",
    "mock": "https://openapivts.koreainvestment.com:29443",
}

OAUTH_TOKEN_PATH = "/oauth2/tokenP"
OAUTH_REVOKE_PATH = "/oauth2/revokeP"
OAUTH_HASHKEY_PATH = "/uapi/hashkey"


def rest_url(env: Env, path: str) -> str:
    return f"{REST_BASE_URL[env]}{path}"
