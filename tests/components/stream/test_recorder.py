"""The tests for hls streams."""
from datetime import timedelta
from io import BytesIO
import os
from unittest.mock import patch

import av
import pytest

from homeassistant.components.stream import create_stream
from homeassistant.components.stream.const import HLS_PROVIDER, RECORDER_PROVIDER
from homeassistant.components.stream.core import Part
from homeassistant.components.stream.fmp4utils import find_box
from homeassistant.components.stream.recorder import recorder_save_worker
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util

from tests.common import async_fire_time_changed
from tests.components.stream.common import (
    DefaultSegment as Segment,
    generate_h264_video,
    remux_with_audio,
)

MAX_ABORT_SEGMENTS = 20  # Abort test to avoid looping forever


async def test_record_stream(hass, hass_client, record_worker_sync):
    """
    Test record stream.

    Tests full integration with the stream component, and captures the
    stream worker and save worker to allow for clean shutdown of background
    threads.  The actual save logic is tested in test_recorder_save below.
    """
    await async_setup_component(hass, "stream", {"stream": {}})

    # Setup demo track
    source = generate_h264_video()
    stream = create_stream(hass, source, {})
    with patch.object(hass.config, "is_allowed_path", return_value=True):
        await stream.async_record("/example/path")

    # After stream decoding finishes, the record worker thread starts
    segments = await record_worker_sync.get_segments()
    assert len(segments) >= 1

    # Verify that the save worker was invoked, then block until its
    # thread completes and is shutdown completely to avoid thread leaks.
    await record_worker_sync.join()

    stream.stop()


async def test_record_lookback(
    hass, hass_client, stream_worker_sync, record_worker_sync
):
    """Exercise record with loopback."""
    await async_setup_component(hass, "stream", {"stream": {}})

    source = generate_h264_video()
    stream = create_stream(hass, source, {})

    # Start an HLS feed to enable lookback
    stream.add_provider(HLS_PROVIDER)
    stream.start()

    with patch.object(hass.config, "is_allowed_path", return_value=True):
        await stream.async_record("/example/path", lookback=4)

    # This test does not need recorder cleanup since it is not fully exercised

    stream.stop()


async def test_recorder_timeout(hass, hass_client, stream_worker_sync):
    """
    Test recorder timeout.

    Mocks out the cleanup to assert that it is invoked after a timeout.
    This test does not start the recorder save thread.
    """
    await async_setup_component(hass, "stream", {"stream": {}})

    stream_worker_sync.pause()

    with patch("homeassistant.components.stream.IdleTimer.fire") as mock_timeout:
        # Setup demo track
        source = generate_h264_video()

        stream = create_stream(hass, source, {})
        with patch.object(hass.config, "is_allowed_path", return_value=True):
            await stream.async_record("/example/path")
        recorder = stream.add_provider(RECORDER_PROVIDER)

        await recorder.recv()

        # Wait a minute
        future = dt_util.utcnow() + timedelta(minutes=1)
        async_fire_time_changed(hass, future)
        await hass.async_block_till_done()

        assert mock_timeout.called

        stream_worker_sync.resume()
        stream.stop()
        await hass.async_block_till_done()
        await hass.async_block_till_done()


async def test_record_path_not_allowed(hass, hass_client):
    """Test where the output path is not allowed by home assistant configuration."""
    await async_setup_component(hass, "stream", {"stream": {}})

    # Setup demo track
    source = generate_h264_video()
    stream = create_stream(hass, source, {})
    with patch.object(
        hass.config, "is_allowed_path", return_value=False
    ), pytest.raises(HomeAssistantError):
        await stream.async_record("/example/path")


def add_parts_to_segment(segment, source):
    """Add relevant part data to segment for testing recorder."""
    moof_locs = list(find_box(source.getbuffer(), b"moof")) + [len(source.getbuffer())]
    segment.init = source.getbuffer()[: moof_locs[0]].tobytes()
    segment.parts_by_byterange = {
        moof_locs[i]: Part(
            duration=None,
            has_keyframe=None,
            data=source.getbuffer()[moof_locs[i] : moof_locs[i + 1]],
        )
        for i in range(len(moof_locs) - 1)
    }


async def test_recorder_save(tmpdir):
    """Test recorder save."""
    # Setup
    source = generate_h264_video()
    filename = f"{tmpdir}/test.mp4"

    # Run
    segment = Segment(sequence=1)
    add_parts_to_segment(segment, source)
    segment.duration = 4
    recorder_save_worker(filename, [segment])

    # Assert
    assert os.path.exists(filename)


async def test_recorder_discontinuity(tmpdir):
    """Test recorder save across a discontinuity."""
    # Setup
    source = generate_h264_video()
    filename = f"{tmpdir}/test.mp4"

    # Run
    segment_1 = Segment(sequence=1, stream_id=0)
    add_parts_to_segment(segment_1, source)
    segment_1.duration = 4
    segment_2 = Segment(sequence=2, stream_id=1)
    add_parts_to_segment(segment_2, source)
    segment_2.duration = 4
    recorder_save_worker(filename, [segment_1, segment_2])
    # Assert
    assert os.path.exists(filename)


async def test_recorder_no_segments(tmpdir):
    """Test recorder behavior with a stream failure which causes no segments."""
    # Setup
    filename = f"{tmpdir}/test.mp4"

    # Run
    recorder_save_worker("unused-file", [])

    # Assert
    assert not os.path.exists(filename)


async def test_record_stream_audio(
    hass, hass_client, stream_worker_sync, record_worker_sync
):
    """
    Test treatment of different audio inputs.

    Record stream output should have an audio channel when input has
    a valid codec and audio packets and no audio channel otherwise.
    """
    await async_setup_component(hass, "stream", {"stream": {}})

    # Generate source video with no audio
    source = generate_h264_video(container_format="mov")

    for a_codec, expected_audio_streams in (
        ("aac", 1),  # aac is a valid mp4 codec
        ("pcm_mulaw", 0),  # G.711 is not a valid mp4 codec
        ("empty", 0),  # audio stream with no packets
        (None, 0),  # no audio stream
    ):

        # Remux source video with new audio
        source = remux_with_audio(source, "mov", a_codec)  # mov can store PCM

        record_worker_sync.reset()
        stream_worker_sync.pause()

        stream = create_stream(hass, source, {})
        with patch.object(hass.config, "is_allowed_path", return_value=True):
            await stream.async_record("/example/path")
        recorder = stream.add_provider(RECORDER_PROVIDER)

        while True:
            await recorder.recv()
            if not (segment := recorder.last_segment):
                break
            last_segment = segment
            stream_worker_sync.resume()

        result = av.open(
            BytesIO(last_segment.init + last_segment.get_data()),
            "r",
            format="mp4",
        )

        assert len(result.streams.audio) == expected_audio_streams
        result.close()
        stream.stop()
        await hass.async_block_till_done()

        # Verify that the save worker was invoked, then block until its
        # thread completes and is shutdown completely to avoid thread leaks.
        await record_worker_sync.join()


async def test_recorder_log(hass, caplog):
    """Test starting a stream to record logs the url without username and password."""
    await async_setup_component(hass, "stream", {"stream": {}})
    stream = create_stream(hass, "https://abcd:efgh@foo.bar", {})
    with patch.object(hass.config, "is_allowed_path", return_value=True):
        await stream.async_record("/example/path")
    assert "https://abcd:efgh@foo.bar" not in caplog.text
    assert "https://****:****@foo.bar" in caplog.text
