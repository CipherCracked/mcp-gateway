import asyncio, os, json, sys

os.environ.setdefault("GATEWAY_CONFIG_PATH", "/workspace/testdata/config/gateway.yaml")
os.environ.setdefault("GATEWAY_DB_PATH", "/workspace/testdata/data/executions.db")

import httpx
from asgi_lifespan import LifespanManager
from reference.mcp_gateway.main import app

A = {"X-API-Key": "key-a"}
B = {"X-API-Key": "key-b"}
ADMIN = {"X-API-Key": "key-admin"}


async def main():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            r = await c.get("/tools/list", headers=A)
            print("tools/list A:", r.status_code, r.json())
            r = await c.get("/tools/list")
            print("tools/list noauth:", r.status_code, r.json())
            r = await c.get("/tools/list", headers={"X-API-Key": "nope"})
            print("tools/list badauth:", r.status_code, r.json())

            r = await c.post("/tools/call", headers=A, json={"tool": "documents.search_documents", "arguments": {"query": "gateway", "limit": 2}})
            print("call:", r.status_code, r.json())
            eid = r.json()["execution_id"]
            for _ in range(50):
                r = await c.get(f"/executions/{eid}/status", headers=A)
                s = r.json()
                if s["state"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.1)
            print("status:", r.status_code, s)
            r = await c.get(f"/executions/{eid}/result", headers=A)
            print("result:", r.status_code, r.json())

            # forbidden tool
            r = await c.post("/tools/call", headers=A, json={"tool": "jobs.execute_job", "arguments": {"payload": {}}})
            print("forbidden tool:", r.status_code, r.json())
            # not found
            r = await c.post("/tools/call", headers=A, json={"tool": "documents.nope", "arguments": {}})
            print("not found:", r.status_code, r.json())
            # missing required
            r = await c.post("/tools/call", headers=A, json={"tool": "documents.search_documents", "arguments": {}})
            print("missing:", r.status_code, r.json())
            # type mismatch
            r = await c.post("/tools/call", headers=A, json={"tool": "documents.search_documents", "arguments": {"query": 5}})
            print("type:", r.status_code, r.json())
            # unexpected field
            r = await c.post("/tools/call", headers=A, json={"tool": "documents.search_documents", "arguments": {"query": "x", "bogus": 1}})
            print("unexpected:", r.status_code, r.json())
            # enum mismatch
            r = await c.post("/tools/call", headers=B, json={"tool": "jobs.execute_job", "arguments": {"payload": {}, "priority": "urgent"}})
            print("enum:", r.status_code, r.json())
            # ownership
            r = await c.get(f"/executions/{eid}/result", headers=B)
            print("wrong owner:", r.status_code, r.json())
            # unknown execution
            r = await c.get("/executions/does-not-exist/status", headers=A)
            print("unknown exec:", r.status_code, r.json())
            # jobs call
            r = await c.post("/tools/call", headers=B, json={"tool": "jobs.execute_job", "arguments": {"payload": {"a": 1}, "priority": "high"}})
            print("jobs call:", r.status_code, r.json())
            jid = r.json()["execution_id"]
            await asyncio.sleep(0.5)
            r = await c.get(f"/executions/{jid}/result", headers=B)
            print("jobs result:", r.status_code, r.json())

asyncio.run(main())
