"""LinkedIn: member identity and text posts.

Personal posting only. LinkedIn's personal access tokens last about 60 days
and most apps cannot refresh them, so the panel states the reconnect
expectation. Organization posting, feed reads, messages, and connections are
not part of this provider.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .types import AppDefinition, ProviderDefinition, ToolDefinition

LINKEDIN_API = "https://api.linkedin.com"
LINKEDIN_OPENID = "openid"
LINKEDIN_PROFILE = "profile"
LINKEDIN_EMAIL = "email"
LINKEDIN_POST = "w_member_social"

# LinkedIn versions the Posts API monthly. A version is supported for at
# least a year; 202610 is the current contract at the time of this build.
LINKEDIN_API_VERSION = "202610"

_POST_HEADERS = {
    "Linkedin-Version": LINKEDIN_API_VERSION,
    "X-Restli-Protocol-Version": "2.0.0",
}

_LINKEDIN_SETUP_STEPS = (
    "Open LinkedIn Developers and create an app.",
    "Associate it with a LinkedIn Page you administer.",
    "Request Share on LinkedIn (w_member_social) and Sign In with LinkedIn using OpenID Connect.",
    "Add the redirect URI shown here. LinkedIn requires HTTPS; for local "
    "testing, expose Aether through an HTTPS tunnel and set AETHER_PUBLIC_URL.",
    "Copy the client ID and client secret into Aether when asked.",
)


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"successful": True, "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"successful": False, "error": message}


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(data.get("sub", "")),
        "name": str(data.get("name", "")),
        "given_name": str(data.get("given_name", "")),
        "family_name": str(data.get("family_name", "")),
        "email": str(data.get("email", "")),
        "email_verified": bool(data.get("email_verified")),
        "picture": str(data.get("picture", "")),
        "locale": str(data.get("locale", "")),
    }


async def get_profile(ctx: Any, _params: dict[str, Any]) -> Any:
    data = await ctx.json("GET", "/v2/userinfo")
    return _ok({"profile": _profile(data)})


async def create_post(ctx: Any, params: dict[str, Any]) -> Any:
    text = str(params.get("text") or params.get("commentary") or "").strip()
    if not text:
        return _err("some post text is required")
    visibility = str(params.get("visibility") or "PUBLIC").strip().upper()
    if visibility != "PUBLIC":
        return _err("LinkedIn member posts are public")
    account_id = str(getattr(ctx.connection, "account_id", "") or "").strip()
    if not account_id:
        return _err("LinkedIn needs to be reconnected before posting")
    payload = {
        "author": f"urn:li:person:{account_id}",
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    response = await ctx.request("POST", "/rest/posts", json=payload, headers=_POST_HEADERS)
    post_id = str(response.headers.get("x-restli-id", "") or "")
    return _ok(
        {
            "published": True,
            "id": post_id,
            "url": (
                f"https://www.linkedin.com/feed/update/{quote(post_id, safe=':')}/"
                if post_id
                else ""
            ),
            "visibility": visibility,
        }
    )


_TOOL_PROFILE = ToolDefinition(
    key="get_profile",
    description="Read the connected LinkedIn member's name, email, and picture.",
    effect="read",
    required_scopes=(LINKEDIN_OPENID,),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=get_profile,
)

_TOOL_POST = ToolDefinition(
    key="create_post",
    description=(
        "Publish a text-only LinkedIn post as the connected member. "
        "LinkedIn member posts are public."
    ),
    effect="write",
    required_scopes=(LINKEDIN_POST,),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The post commentary. LinkedIn applies its own length limit.",
            },
            "visibility": {
                "type": "string",
                "description": "Only PUBLIC is supported for member posts.",
            },
        },
        "required": ["text"],
    },
    handler=create_post,
)

LINKEDIN_APP = AppDefinition(
    key="linkedin",
    name="LinkedIn",
    provider="linkedin",
    scopes=(
        LINKEDIN_OPENID,
        LINKEDIN_PROFILE,
        LINKEDIN_EMAIL,
        LINKEDIN_POST,
    ),
    tools=(_TOOL_PROFILE, _TOOL_POST),
    note=(
        "LinkedIn personal sign-ins last about 60 days. Aether will ask you "
        "to reconnect when this one expires."
    ),
)

LINKEDIN_PROVIDER = ProviderDefinition(
    key="linkedin",
    name="LinkedIn",
    auth_modes=("oauth",),
    default_auth_mode="oauth",
    authorization_endpoint="https://www.linkedin.com/oauth/v2/authorization",
    token_endpoint="https://www.linkedin.com/oauth/v2/accessToken",
    userinfo_endpoint="https://api.linkedin.com/v2/userinfo",
    api_base=LINKEDIN_API,
    client_id_name="LINKEDIN_CLIENT_ID",
    client_secret_name="LINKEDIN_CLIENT_SECRET",
    pkce=False,
    prompt_consent_param=False,
    setup_url="https://www.linkedin.com/developers/apps",
    setup_steps=_LINKEDIN_SETUP_STEPS,
    forbidden_plain=(
        "LinkedIn blocked that action — the app may need the Share on LinkedIn "
        "product, or the sign-in may need reconnecting"
    ),
    not_found_plain="that LinkedIn item isn't there, or this sign-in can't see it",
    apps=(LINKEDIN_APP,),
)

PROVIDERS = [LINKEDIN_PROVIDER]
