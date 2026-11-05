import hashlib
import importlib.util
import sys

import pytest

WT = "/Users/martin/Projects/kiro-crew-for-pi-worktrees/sync-main-20261105"
spec = importlib.util.spec_from_file_location(
    "ct", f"{WT}/test/test_dashboard_server_composition_contract_reach.py"
)
ct = importlib.util.module_from_spec(spec)
sys.modules["ct"] = ct
spec.loader.exec_module(ct)


OUT = "/Users/martin/.kiro/crew/scratch/dashboard-member-kirocrew-maintainer.mem-a0216664/boot_dump.txt"


def test_dump_routes():
    from aiohttp import web

    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    rows = [" ".join(r) for r in ct._routes(app)]
    with open(OUT, "w") as f:
        f.write(f"ROWS>>> {len(rows)}\n")
        f.write(f"DIGEST>>> {hashlib.sha256(chr(10).join(rows).encode()).hexdigest()}\n")


@pytest.mark.asyncio
async def test_dump_dashboard(tmp_path, monkeypatch):
    runner, state, boot, teardown = await ct._traced_dashboard(tmp_path, monkeypatch)
    with open(OUT, "a") as f:
        f.write("DASH>>>\n")
        for x in ct._own(boot):
            f.write(f"D: {x}\n")
    if teardown:
        await teardown()


@pytest.mark.asyncio
async def test_dump_api(tmp_path, monkeypatch):
    runner, state, boot, teardown = await ct._traced_api(tmp_path, monkeypatch)
    with open(OUT, "a") as f:
        f.write("API>>>\n")
        for x in ct._own(boot):
            f.write(f"A: {x}\n")
    if teardown:
        await teardown()
