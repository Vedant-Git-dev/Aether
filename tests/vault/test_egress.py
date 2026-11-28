import asyncio
from types import SimpleNamespace

import pytest

from aether.agent.loop import SurfaceFanout
from aether.connectors.mcp_host import MCPServerConnection
from aether.llm.openai_provider import OpenAIProvider
from aether.vault.errors import VaultProtocolError
from aether.workbench import run


async def test_private_context_blocks_generic_destinations_in_descendants():
    from aether.vault.egress import private_work

    attempts = []
    secret = "private canary with untrusted fetch, send, export and upload instructions"

    async def transport(*args, **kwargs):
        attempts.append((args, kwargs))

    model = OpenAIProvider(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=transport))),
        "other-model",
        16000,
    )
    messenger = SurfaceFanout(hub=SimpleNamespace(broadcast=transport))
    workbench = SimpleNamespace(available=transport)
    mcp = SimpleNamespace(wait_ready=transport, _stopping=False, _task=True)

    async def verify():
        for operation in (
            model.complete(secret, [], []),
            messenger.send_to_user(secret),
            MCPServerConnection.call(mcp, "fetch_or_export", {"body": secret}),
            run(SimpleNamespace(label="upload", inputs={"private": secret}), [workbench]),
        ):
            with pytest.raises(VaultProtocolError, match="bad_scope"):
                await operation

    with private_work():
        await asyncio.create_task(verify())

    assert attempts == []
