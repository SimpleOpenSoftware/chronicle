import asyncio
import io
import wave
from contextlib import aclosing

import pytest

from backend.services.interaction_modes.voice.engine import ModularVoiceEngine
from backend.services.interaction_modes.voice.engine_types import (
    VoiceAudio,
    VoiceCompleted,
    VoiceEngineError,
    VoiceEngineSettings,
    VoiceText,
    VoiceToolCall,
)

pytestmark = pytest.mark.unit


def _wav(samples=600):
    body = io.BytesIO()
    with wave.open(body, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24_000)
        writer.writeframes(b"\x01\x00" * samples)
    return body.getvalue()


async def _tts(_text):
    return _wav()


async def test_audio_starts_before_model_completion_and_preserves_sample_timeline():
    proceed = asyncio.Event()
    closed = asyncio.Event()

    async def llm(**_kwargs):
        try:
            yield {"type": "content", "text": "Hello there. "}
            await proceed.wait()
            yield {"type": "content", "text": "How are you?"}
            yield {"type": "done", "finish_reason": "stop"}
        finally:
            closed.set()

    engine = ModularVoiceEngine(llm_stream=llm, synthesize=_tts)
    events = []
    async with aclosing(engine.generate(text="Hello")) as response:
        events.append(await anext(response))
        first_audio = await asyncio.wait_for(anext(response), timeout=1)
        events.append(first_audio)
        assert isinstance(first_audio, VoiceAudio)
        assert not proceed.is_set()
        assert not closed.is_set()
        proceed.set()
        events.extend([event async for event in response])
    audio = [event for event in events if isinstance(event, VoiceAudio)]
    assert [event.start_sample for event in audio] == [0, 480, 600, 1080]
    assert [len(event.pcm) for event in audio] == [960, 240, 960, 240]
    assert [event.phrase_final for event in audio] == [False, True, False, True]
    assert events[-1] == VoiceCompleted("Hello there. How are you?", "stop", 1200)
    assert closed.is_set()


async def test_reasoning_never_reaches_synthesis_and_llm_has_no_fallback():
    calls = []

    async def llm(**kwargs):
        assert kwargs["allow_fallback"] is False
        assert kwargs["operation"] == "voice_conversation"
        assert kwargs["messages"][-1] == {"role": "user", "content": "Hi"}
        yield {"type": "reasoning", "text": "private chain"}
        yield {"type": "content", "text": "Hello."}
        yield {
            "type": "done",
            "reasoning_content": "also private",
            "finish_reason": "stop",
        }

    async def tts(text):
        calls.append(text)
        return _wav()

    events = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Hi"
        )
    ]
    assert calls == ["Hello."]
    assert events[-1].text == "Hello."


async def test_tool_continuation_does_not_duplicate_user_and_never_executes_tools():
    history = [
        {"role": "user", "content": "Find it"},
        {"role": "assistant", "tool_calls": [{"id": "previous"}]},
        {"role": "tool", "tool_call_id": "previous", "content": "No result"},
    ]
    schemas = [{"type": "function", "function": {"name": "delegate_to_hermes"}}]

    async def llm(**kwargs):
        assert kwargs["messages"][1:] == history
        assert kwargs["tools"] == schemas
        yield {
            "type": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "delegate_to_hermes",
                        "arguments": '{"request": "Find the answer"}',
                    },
                }
            ],
        }

    async def forbidden_tts(_text):
        pytest.fail("tool-only output must not synthesize")

    events = [
        event
        async for event in ModularVoiceEngine(
            llm_stream=llm, synthesize=forbidden_tts
        ).generate(text=None, history=history, tool_schemas=schemas)
    ]
    assert events == [
        VoiceToolCall("call-1", "delegate_to_hermes", {"request": "Find the answer"}),
        VoiceCompleted("", "tool_calls", 0),
    ]


@pytest.mark.parametrize("cancel_by_event", [True, False])
async def test_cancellation_cleans_stalled_synthesis_and_model(cancel_by_event):
    tts_started = asyncio.Event()
    tts_closed = asyncio.Event()
    llm_closed = asyncio.Event()
    cancellation = asyncio.Event()

    async def llm(**_kwargs):
        try:
            yield {"type": "content", "text": "Hello. "}
            await asyncio.Event().wait()
        finally:
            llm_closed.set()

    async def tts(_text):
        tts_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tts_closed.set()

    async with aclosing(
        ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Hi", cancellation=cancellation
        )
    ) as response:
        assert isinstance(await anext(response), VoiceText)
        pending = asyncio.create_task(anext(response))
        await asyncio.wait_for(tts_started.wait(), 1)
        if cancel_by_event:
            cancellation.set()
        else:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 1)
    assert tts_closed.is_set()
    assert llm_closed.is_set()


async def test_cancel_while_waiting_for_first_model_token_closes_provider():
    started = asyncio.Event()
    closed = asyncio.Event()
    cancellation = asyncio.Event()

    async def llm(**_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover
        finally:
            closed.set()

    response = ModularVoiceEngine(llm_stream=llm, synthesize=_tts).generate(
        text="Hi", cancellation=cancellation
    )
    pending = asyncio.create_task(anext(response))
    await started.wait()
    cancellation.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    assert closed.is_set()


async def test_backpressure_bounds_model_lookahead_and_closing_output_cleans_it():
    produced = 0
    closed = asyncio.Event()

    async def llm(**_kwargs):
        nonlocal produced
        try:
            for _ in range(100):
                produced += 1
                yield {"type": "content", "text": "A phrase. "}
            yield {"type": "done", "finish_reason": "stop"}
        finally:
            closed.set()

    async with aclosing(
        ModularVoiceEngine(llm_stream=llm, synthesize=_tts).generate(text="Hi")
    ) as response:
        assert isinstance(await anext(response), VoiceText)
        assert isinstance(await anext(response), VoiceAudio)
        # Current audio, one prepared/in-flight next phrase, one queued text
        # phrase, and one producer blocked on insertion. No unbounded read-ahead.
        await asyncio.sleep(0.02)
        assert produced <= 4
    assert closed.is_set()


async def test_synthesis_prefetch_has_exactly_one_slot_while_delivery_is_paused():
    started = []
    second_ready = asyncio.Event()

    async def llm(**kwargs):
        yield {"type": "content", "text": "First. Second. Third. Fourth."}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        started.append(text)
        if text == "Second.":
            second_ready.set()
        return _wav(36000)

    async with aclosing(
        ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(text="Hi")
    ) as response:
        assert isinstance(await anext(response), VoiceText)
        assert isinstance(await anext(response), VoiceAudio)
        await asyncio.wait_for(second_ready.wait(), 0.5)
        # A completed second WAV still occupies the one prefetch slot. A queue
        # alone would incorrectly allow a third synthesis to start in flight.
        await asyncio.sleep(0.02)
        assert started == ["First.", "Second."]


@pytest.mark.parametrize("cancel_by_event", [True, False])
async def test_cancellation_closes_inflight_prefetch_while_audio_is_paused(
    cancel_by_event,
):
    second_started = asyncio.Event()
    second_closed = asyncio.Event()
    model_closed = asyncio.Event()
    cancellation = asyncio.Event()

    async def llm(**kwargs):
        try:
            yield {"type": "content", "text": "First. Second. Third."}
            await asyncio.Event().wait()
        finally:
            model_closed.set()

    async def tts(text):
        if text == "First.":
            return _wav(36000)
        second_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            second_closed.set()

    async with aclosing(
        ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Hi", cancellation=cancellation
        )
    ) as response:
        assert isinstance(await anext(response), VoiceText)
        assert isinstance(await anext(response), VoiceAudio)
        await asyncio.wait_for(second_started.wait(), 0.5)
        if cancel_by_event:
            cancellation.set()
            with pytest.raises(asyncio.CancelledError):
                await anext(response)
    assert second_closed.is_set()
    assert model_closed.is_set()


async def test_prefetch_preserves_audio_then_tool_intent_order():
    async def llm(**kwargs):
        yield {"type": "content", "text": "Checking. "}
        yield {
            "type": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"food"}',
                    },
                }
            ],
        }

    result = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=_tts).generate(
            text="Hi", tool_schemas=[{"function": {"name": "search"}}]
        )
    ]
    assert isinstance(result[-2], VoiceToolCall)
    assert isinstance(result[-1], VoiceCompleted)
    assert all(isinstance(event, VoiceAudio) for event in result[1:-2])


@pytest.mark.parametrize(
    "wav, message",
    [(None, "unavailable"), (_wav(91_681), "audio bound"), (b"broken", "valid WAV")],
)
async def test_synthesis_errors_fail_generation_without_completion(wav, message):
    async def llm(**_kwargs):
        yield {"type": "content", "text": "Hello."}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(_text):
        return wav

    events = []
    with pytest.raises((VoiceEngineError, ValueError), match=message):
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Hi"
        ):
            events.append(event)
    assert not any(isinstance(event, VoiceCompleted) for event in events)


@pytest.mark.parametrize("finish", [None, "length"])
async def test_missing_or_incomplete_model_completion_is_failure(finish):
    async def llm(**_kwargs):
        yield {"type": "content", "text": "Hello."}
        if finish:
            yield {"type": "done", "finish_reason": finish}

    with pytest.raises(VoiceEngineError):
        _ = [
            event
            async for event in ModularVoiceEngine(
                llm_stream=llm, synthesize=_tts
            ).generate(text="Hi")
        ]


async def test_invalid_tool_set_is_rejected_before_exposing_any_tool():
    async def llm(**_kwargs):
        yield {
            "type": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": "valid", "function": {"name": "allowed", "arguments": "{}"}},
                {"id": "invalid", "function": {"name": "blocked", "arguments": "{}"}},
            ],
        }

    with pytest.raises(VoiceEngineError, match="unadvertised"):
        _ = [
            event
            async for event in ModularVoiceEngine(
                llm_stream=llm, synthesize=_tts
            ).generate(
                text="Hi",
                tool_schemas=[{"type": "function", "function": {"name": "allowed"}}],
            )
        ]


async def test_long_response_is_broken_into_bounded_phrases_without_losing_words():
    full = "This is a deliberately long sentence that ends in a final short phrase."
    phrases = []

    async def llm(**_kwargs):
        for offset in range(0, len(full), 5):
            yield {"type": "content", "text": full[offset : offset + 5]}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        phrases.append(text)
        return _wav()

    events = [
        event
        async for event in ModularVoiceEngine(
            llm_stream=llm,
            synthesize=tts,
            settings=VoiceEngineSettings(max_phrase_chars=24),
        ).generate(text="Hi")
    ]
    assert all(len(phrase) <= 24 for phrase in phrases)
    assert " ".join(phrases) == full
    assert events[-1].text == full


async def test_normal_kokoro_phrase_duration_fits_reserved_audio_budget():
    # A real local Kokoro synthesis of this32-character phrase was2.775s.
    # The old2-second ceiling rejected ordinary short conversational output.
    async def llm(**kwargs):
        yield {"type": "content", "text": "Hello, how can I help you today?"}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        return _wav(66_600)

    events = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Hello"
        )
    ]
    assert events[-1].total_samples == 66_600
    assert (
        sum(len(event.pcm) // 2 for event in events if isinstance(event, VoiceAudio))
        == 66_600
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1,234.560", "one thousand, two hundred and thirty-four point five six zero"),
        ("-0.05", "minus zero point zero five"),
        ("0012", "zero zero one two"),
        ("12.5%", "twelve point five percent"),
        (
            "$1,234.56.",
            "one thousand, two hundred and thirty-four dollars, fifty-six cents.",
        ),
        ("10:30", "ten thirty"),
        ("21st", "twenty-first"),
    ],
)
def test_numeric_pronunciation_preserves_value_and_decimal_precision(raw, expected):
    from backend.services.interaction_modes.voice.speech_text import (
        normalize_numeric_token,
    )

    assert normalize_numeric_token(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1e3", "one ee three"),
        ("1/2", "one slash two"),
        ("1/2.", "one slash two."),
        ("$1.234", "dollar sign one point two three four"),
        ("1,23,456", "one comma two three comma four five six"),
        ("25°C", "two five degree sign capital see"),
        ("B2B", "capital bee two capital bee"),
        ("version2.1.3", "vee ee ar ess eye oh en two point one point three"),
    ],
)
def test_unrecognized_numeric_notation_is_spelled_literally(raw, expected):
    from backend.services.interaction_modes.voice.speech_text import (
        normalize_numeric_token,
    )

    assert normalize_numeric_token(raw) == expected


def test_literal_pronunciation_retains_all_digits_and_bounds_expansion():
    from backend.services.interaction_modes.voice.speech_text import (
        normalize_numeric_token,
    )

    assert normalize_numeric_token("1" * 33) == " ".join(["one"] * 33)
    with pytest.raises(VoiceEngineError, match="literal voice token"):
        normalize_numeric_token("1" * 129)


async def test_units_versions_and_fractions_do_not_fail_conversation():
    text = "It is 25°C. B2B uses version2.1.3 and 1/2."
    phrases = []

    async def llm(**kwargs):
        # Include character-level deltas to exercise unknown-token boundaries.
        for character in text:
            yield {"type": "content", "text": character}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(phrase):
        assert not any(character.isdigit() for character in phrase)
        assert len(phrase) <= 32
        phrases.append(phrase)
        return _wav()

    events = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Explain it"
        )
    ]
    assert events[-1].text == text
    spoken = " ".join(phrases)
    assert "two five degree sign capital see" in spoken
    assert "capital bee two capital bee" in spoken
    assert "two point one point three" in spoken
    assert "one slash two" in spoken


async def test_number_delta_boundaries_never_split_value_or_change_original_text():
    calls = []

    async def llm(**kwargs):
        for delta in ["The total price is $1,", "234.", "56.", " "]:
            yield {"type": "content", "text": delta}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(text):
        calls.append(text)
        assert not any(char.isdigit() for char in text)
        assert len(text) <= 32
        return _wav()

    events = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="How much?"
        )
    ]
    assert events[-1].text == "The total price is $1,234.56. "
    assert (
        " ".join(calls)
        == "The total price is one thousand, two hundred and thirty-four dollars, fifty-six cents."
    )


async def test_numeric_expansion_has_a_separate_spoken_text_bound():
    async def llm(**kwargs):
        yield {"type": "content", "text": "12345678901234567890123456789012"}
        yield {"type": "done", "finish_reason": "stop"}

    with pytest.raises(VoiceEngineError, match="normalized voice response"):
        _ = [
            event
            async for event in ModularVoiceEngine(
                llm_stream=llm,
                synthesize=_tts,
                settings=VoiceEngineSettings(max_response_chars=40),
            ).generate(text="Say it")
        ]


@pytest.mark.parametrize(
    "text, measured_phrase_samples",
    [
        (
            "The total price is $1,234.56.",
            {
                "The total price is one thousand,": 72600,
                "two hundred and thirty-four": 61200,
                "dollars, fifty-six cents.": 63600,
            },
        ),
        (
            "The reference number is 1234567890.",
            {
                "The reference number is one": 66000,
                "billion, two hundred and": 58800,
                "thirty-four million, five": 63600,
                "hundred and sixty-seven": 63000,
                "thousand, eight hundred and": 63000,
                "ninety.": 42000,
            },
        ),
    ],
)
async def test_real_kokoro_numeric_phrase_durations_fit_engine_budget(
    text, measured_phrase_samples
):
    # Durations from the local synthetic Kokoro audit; fixture waveforms retain
    # those lengths without checking private/machine-local media into the repo.
    async def llm(**kwargs):
        yield {"type": "content", "text": text}
        yield {"type": "done", "finish_reason": "stop"}

    async def tts(phrase):
        return _wav(measured_phrase_samples[phrase])

    events = [
        event
        async for event in ModularVoiceEngine(llm_stream=llm, synthesize=tts).generate(
            text="Please say it"
        )
    ]
    assert events[-1].text == text
    assert events[-1].total_samples == sum(measured_phrase_samples.values())


def test_currency_conversion_does_not_round_large_values_through_float_or_decimal_context():
    from num2words import num2words

    from backend.services.interaction_modes.voice.speech_text import (
        normalize_numeric_token,
    )

    amount = "123456789012345678901234567890"
    expected = num2words(int(amount), lang="en") + " dollars, twelve cents"
    assert normalize_numeric_token("$" + amount + ".12") == expected


async def test_committed_utterance_uses_tts_without_model_or_rewording():
    from unittest.mock import AsyncMock

    model = AsyncMock(
        side_effect=AssertionError("committed text must not call a model")
    )
    engine = ModularVoiceEngine(llm_stream=model, synthesize=_tts)
    events = [event async for event in engine.speak("कौन सा कमरा?")]
    assert events[-1].text == "कौन सा कमरा?"
    assert any(isinstance(event, VoiceAudio) for event in events)
    model.assert_not_called()
