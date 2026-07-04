import asyncio
from unittest.mock import MagicMock

from livekit.agents.voice import io
from livekit.agents.voice.agent import Agent
from livekit.agents.voice.agent_activity import AgentActivity
from livekit.agents.voice.agent_session import AgentSession


class MockVideoInput(io.VideoInput):
    def __init__(self, label: str):
        super().__init__(label=label)

    async def __anext__(self):
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise StopAsyncIteration from None


class MockAudioInput(io.AudioInput):
    def __init__(self, label: str):
        super().__init__(label=label)

    async def __anext__(self):
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise StopAsyncIteration from None


async def async_noop(*args, **kwargs):
    return None


async def run_test():
    # Setup mock agent
    mock_agent = MagicMock(spec=Agent)
    mock_agent.id = "mock_agent"
    mock_agent.label = "mock_agent"

    print("--- CASE 1: Session starts but no activity is initialized ---")
    session1 = AgentSession()

    # Mock _update_activity to do nothing so we don't start real engines
    session1._update_activity = async_noop

    # Create mock stream wrappers
    video_stream = MockVideoInput(label="camera")
    audio_stream = MockAudioInput(label="microphone")

    session1.input.video = video_stream
    session1.input.audio = audio_stream

    # Start the session using the real start() method
    await session1.start(agent=mock_agent)
    await asyncio.sleep(0.1)  # Let tasks start

    video_task1 = session1._forward_video_atask
    print(f"Video Task started & active: {video_task1 is not None and not video_task1.done()}")

    print("Calling real session.aclose()...")
    await session1.aclose()

    print(f"Video Task is done after close: {video_task1.done()}")
    if not video_task1.done():
        print("🚨 CASE 1 LEAK VERIFIED: The video task is STILL RUNNING!")
        video_task1.cancel()
    else:
        print("✅ Case 1 terminated cleanly.")

    print("\n--- CASE 2: Session has an active activity ---")
    session2 = AgentSession()
    session2._update_activity = async_noop

    session2.input.video = video_stream
    session2.input.audio = audio_stream

    await session2.start(agent=mock_agent)
    await asyncio.sleep(0.1)  # Let tasks start

    video_task2 = session2._forward_video_atask
    print(f"Video Task started & active: {video_task2 is not None and not video_task2.done()}")

    # Mock an active AgentActivity
    mock_activity = MagicMock(spec=AgentActivity)
    mock_activity.agent = mock_agent
    mock_activity.current_speech = None
    mock_activity.interrupt = async_noop
    mock_activity.drain = async_noop
    mock_activity.aclose = async_noop
    mock_activity._audio_recognition = None  # Avoid AttributeError

    session2._activity = mock_activity

    print("Calling real session.aclose()...")
    await session2.aclose()

    # Wait a bit to let any event callbacks execute
    await asyncio.sleep(0.1)

    print(f"Video Task is done after close: {video_task2.done()}")
    if not video_task2.done():
        print("🚨 CASE 2 LEAK VERIFIED: The video task is STILL RUNNING!")
        video_task2.cancel()
    else:
        print("✅ Case 2 terminated without leaking.")


if __name__ == "__main__":
    asyncio.run(run_test())
