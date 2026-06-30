"""RoomHub 房间级常驻广播的单元测试。"""

import asyncio

from app.services.room_hub import RoomHub


def test_broadcast_reaches_all_subscribers():
    hub = RoomHub()
    a = hub.subscribe("r1")
    b = hub.subscribe("r1")
    hub.broadcast("r1", "x")
    assert a.get_nowait() == "x"
    assert b.get_nowait() == "x"
    assert hub.member_count("r1") == 2


def test_broadcast_isolated_per_room():
    hub = RoomHub()
    a = hub.subscribe("r1")
    hub.subscribe("r2")
    hub.broadcast("r2", "y")
    assert a.empty()  # r1 订阅者收不到 r2 的广播


def test_inflight_replay_for_midstream_join():
    hub = RoomHub()
    hub.begin_generation("r1")
    hub.broadcast("r1", "chunk1")
    hub.broadcast("r1", "chunk2")
    # 生成中途接入：应立即重放已广播的 chunk
    late = hub.subscribe("r1")
    assert late.get_nowait() == "chunk1"
    assert late.get_nowait() == "chunk2"
    # 结束生成后清空 buffer，新订阅者不再重放
    hub.end_generation("r1")
    later = hub.subscribe("r1")
    assert later.empty()


def test_generation_prelude_is_buffered():
    """start 的 prelude（玩家事件 + generating）在 begin_generation 之后广播，进入 buffer，
    断线重连可重放——避免「点了发送但自己消息没显示、只剩思考中」的吞消息问题。"""
    from app.services.generation_manager import GenerationManager
    from app.services.room_hub import room_hub

    async def _noop():
        await asyncio.sleep(0.01)

    async def run():
        gm = GenerationManager()
        task = gm.start("r_prelude", _noop(), prelude=["player-evt", "data: gen"])
        late = room_hub.subscribe("r_prelude")  # 中途接入：应重放 prelude
        got = [late.get_nowait(), late.get_nowait()]
        await task
        return got

    assert asyncio.run(run()) == ["player-evt", "data: gen"]


def test_unsubscribe_removes():
    hub = RoomHub()
    a = hub.subscribe("r1")
    hub.unsubscribe("r1", a)
    assert hub.member_count("r1") == 0
    hub.broadcast("r1", "z")  # 不应抛错
    assert a.empty()


def test_stream_room_yields_until_none():
    from app.services.room_hub import stream_room

    hub = RoomHub()
    q = hub.subscribe("r1")
    q.put_nowait("a")
    q.put_nowait("b")
    q.put_nowait(None)

    # stream_room 使用模块级单例 room_hub 退订，这里只验证产出序列
    async def collect():
        out = []
        async for c in stream_room("r1", q):
            out.append(c)
        return out

    assert asyncio.run(collect()) == ["a", "b"]
