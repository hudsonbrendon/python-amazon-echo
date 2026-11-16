"""Tests for the LAN probe."""

from __future__ import annotations

import asyncio

import aiohttp

from amazon_echo import LocalInfo, is_reachable, probe_local

from .conftest import FakeResponse

INFO = {
    "status": 101,
    "deviceID": "0123456789abcdef0123456789abcdef0123",
    "remoteName": "",
    "brandDisplayName": "Amazon",
    "modelDisplayName": "Echo_Plus",
    "activeUser": "",
    "groupStatus": "NONE",
    "libraryVersion": "3.209.367-g12ca7f99",
    "aliases": [
        {"name": "Office Echo", "id": "1", "isGroup": "false"},
        {"name": "Everywhere", "id": "2", "isGroup": "true"},
        "garbage",
    ],
}


async def test_probe_local(session):
    session.add("GET", "http://192.0.2.10:4070/spotifyConnect", INFO)
    info = await probe_local("192.0.2.10", session=session)
    assert info == LocalInfo(
        host="192.0.2.10",
        name="Office Echo",
        model="Echo_Plus",
        brand="Amazon",
        device_id="0123456789abcdef0123456789abcdef0123",
        spotify_user=None,
        groups=("Everywhere",),
        library_version="3.209.367-g12ca7f99",
    )
    assert session.calls[0]["params"] == {"action": "getInfo"}
    assert not session.closed


async def test_probe_local_prefers_remote_name_and_user(session):
    session.add(
        "GET",
        "/spotifyConnect",
        {
            **INFO,
            "remoteName": "Renamed",
            "activeUser": "spotify-user",
            "aliases": None,
        },
    )
    info = await probe_local("192.0.2.10", session=session)
    assert (info.name, info.spotify_user, info.groups) == (
        "Renamed",
        "spotify-user",
        (),
    )


async def test_probe_local_rejects_other_brands(session):
    session.add("GET", "/spotifyConnect", {**INFO, "brandDisplayName": "LG"})
    assert await probe_local("192.0.2.11", session=session) is None


async def test_probe_local_failures(session):
    session.add(
        "GET",
        "/spotifyConnect",
        FakeResponse(404),
        TimeoutError(),
        aiohttp.ClientConnectionError("refused"),
        FakeResponse(200, text="<html>"),
        FakeResponse(200, ["list"]),
    )
    for _ in range(5):
        assert await probe_local("192.0.2.12", session=session) is None


async def test_probe_local_own_session_is_closed(monkeypatch, session):
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    session.add("GET", "/spotifyConnect", INFO)
    assert (await probe_local("192.0.2.10")).model == "Echo_Plus"
    assert session.closed


async def test_is_reachable():
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await is_reachable("127.0.0.1", port=port)
    finally:
        server.close()
        await server.wait_closed()
    assert not await is_reachable("127.0.0.1", port=port, timeout=0.5)
