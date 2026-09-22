"""Bounded English numeric pronunciation before TTS phrase segmentation.

This is deliberately a small notation contract, not a general document parser:
plain/grouped integers, ordinals, exact decimals, percent, symbol-prefixed USD/GBP/EUR/INR,
and HH:MM clock times. Other digit-bearing tokens are spelled literally with
named characters rather than asking TTS to infer a value. Original model text remains
untouched in VoiceCompleted; only the spoken projection is normalized.
"""

from __future__ import annotations

import re
import string
import unicodedata
from decimal import Decimal, DecimalException, localcontext

from num2words import num2words

from .engine_types import VoiceEngineError

_INTEGER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
_NUMBER = re.compile(
    rf"(?P<sign>[+-]?)(?P<integer>{_INTEGER})(?:\.(?P<fraction>[0-9]+))?"
)
_CLOCK = re.compile(r"([0-9]{1,2}):([0-9]{2})")
_ORDINAL = re.compile(r"([0-9]+)(st|nd|rd|th)")
_CURRENCIES = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
_PREFIX = re.compile(r"^[\(\[\{\"']*")
_SUFFIX = re.compile(r"[\)\]\}\"'.,!?;:]*$")
_DIGIT_WORDS = tuple(num2words(i, lang="en") for i in range(10))


def _number_words(value: str) -> str:
    match = _NUMBER.fullmatch(value)
    if match is None:
        raise VoiceEngineError("unsupported numeric notation in spoken response")
    sign, integer, fraction = match.group("sign", "integer", "fraction")
    integer = integer.replace(",", "")
    if len(integer) + len(fraction or "") > 32:
        raise VoiceEngineError("spoken number exceeds digit limit")
    # Leading zeros often identify codes. Keep every digit rather than turning
    # e.g. 0012 into twelve. Decimal tails similarly retain their exact zeros.
    words = (
        " ".join(_DIGIT_WORDS[int(digit)] for digit in integer)
        if len(integer) > 1 and integer.startswith("0")
        else num2words(int(integer), lang="en")
    )
    if fraction is not None:
        words += " point " + " ".join(_DIGIT_WORDS[int(digit)] for digit in fraction)
    return ("minus " if sign == "-" else "plus " if sign == "+" else "") + words


def _token_parts(token: str) -> tuple[str, str, str]:
    prefix = _PREFIX.match(token).group()
    suffix = _SUFFIX.search(token).group()
    core = token[len(prefix) : len(token) - len(suffix) if suffix else None]
    return prefix, core, suffix


def _normalize_recognized_token(token: str) -> str:
    prefix, core, suffix = _token_parts(token)
    clock = _CLOCK.fullmatch(core)
    ordinal = _ORDINAL.fullmatch(core)
    if ordinal is not None:
        value = ordinal.group(1)
        _number_words(value)
        if num2words(int(value), lang="en", to="ordinal_num") != core:
            raise VoiceEngineError("invalid ordinal in spoken response")
        spoken = num2words(int(value), lang="en", to="ordinal")
    elif clock is not None:
        hour, minute = map(int, clock.groups())
        if hour > 23 or minute > 59:
            raise VoiceEngineError("invalid clock time in spoken response")
        spoken = num2words(hour, lang="en") + (
            " o'clock"
            if minute == 0
            else (
                " oh " + num2words(minute, lang="en")
                if minute < 10
                else " " + num2words(minute, lang="en")
            )
        )
    elif core and core[0] in _CURRENCIES:
        value = core[1:]
        match = _NUMBER.fullmatch(value)
        if match is None or len(match.group("fraction") or "") > 2:
            raise VoiceEngineError("unsupported currency precision in spoken response")
        _number_words(value)  # Validate grouping and bound before conversion.
        # The library quantizes currency to cents. Its default Decimal context
        # has only 28 digits, below our explicit 32-digit notation limit.
        try:
            with localcontext() as context:
                context.prec = 40
                spoken = num2words(
                    Decimal(value.replace(",", "")),
                    lang="en",
                    to="currency",
                    currency=_CURRENCIES[core[0]],
                )
        except (DecimalException, ValueError, OverflowError) as exc:
            raise VoiceEngineError(
                "unsupported currency value in spoken response"
            ) from exc
    else:
        percent = core.endswith("%")
        spoken = _number_words(core[:-1] if percent else core)
        if percent:
            spoken += " percent"
    return prefix + spoken + suffix


_LETTER_NAMES = dict(
    zip(
        string.ascii_lowercase,
        (
            "ay",
            "bee",
            "see",
            "dee",
            "ee",
            "eff",
            "gee",
            "aitch",
            "eye",
            "jay",
            "kay",
            "ell",
            "em",
            "en",
            "oh",
            "pee",
            "cue",
            "ar",
            "ess",
            "tee",
            "you",
            "vee",
            "double you",
            "ex",
            "why",
            "zee",
        ),
    )
)
_CHARACTER_NAMES = {
    "/": "slash",
    "\\": "backslash",
    "-": "dash",
    ".": "point",
    ",": "comma",
    ":": "colon",
    ";": "semicolon",
    "_": "underscore",
    "+": "plus",
    "=": "equals",
    "%": "percent sign",
    "°": "degree sign",
    "$": "dollar sign",
    "£": "pound sign",
    "€": "euro sign",
    "₹": "rupee sign",
    "@": "at sign",
    "#": "number sign",
    "&": "ampersand",
    "*": "asterisk",
    "(": "open parenthesis",
    ")": "close parenthesis",
    "[": "open bracket",
    "]": "close bracket",
    "{": "open brace",
    "}": "close brace",
    "?": "question mark",
    "!": "exclamation mark",
    "'": "apostrophe",
    '"': "quote",
}


def _literal_token(token: str) -> str:
    """Spell an unfamiliar code, version, fraction, or unit without interpreting it."""
    if len(token) > 128:
        raise VoiceEngineError("literal voice token exceeds character bound")
    words = []
    size = 0
    for character in token:
        if character in string.digits:
            word = _DIGIT_WORDS[int(character)]
        elif character.lower() in _LETTER_NAMES:
            word = ("capital " if character.isupper() else "") + _LETTER_NAMES[
                character.lower()
            ]
        elif character in _CHARACTER_NAMES:
            word = _CHARACTER_NAMES[character]
        else:
            # Unicode's standard name retains unfamiliar symbols without silent
            # omission. If unnamed, speak the exact scalar value digit by digit.
            word = unicodedata.name(character, "").lower() or (
                "unicode code point "
                + " ".join(_DIGIT_WORDS[int(d)] for d in str(ord(character)))
            )
            word = re.sub(
                r"[0-9]",
                lambda match: " " + _DIGIT_WORDS[int(match.group())] + " ",
                word,
            )
        size += len(word) + bool(words)
        if size > 2048:
            raise VoiceEngineError("literal voice token exceeds pronunciation bound")
        words.append(word)
    return " ".join(words)


def normalize_numeric_token(token: str) -> str:
    if not any(character.isdigit() for character in token):
        return token
    try:
        return _normalize_recognized_token(token)
    except VoiceEngineError:
        # This is a deterministic pronunciation policy, not another provider.
        # Unsupported syntax must not turn a normal conversation into a failure.
        prefix, core, suffix = _token_parts(token)
        return prefix + _literal_token(core) + suffix


class SpeechTextNormalizer:
    """Hold an unfinished lexical token across model deltas, never split a number."""

    def __init__(self, *, limit: int, language: str = "en"):
        if language not in {"en", "hi"}:
            raise ValueError("Speech normalization language must be en or hi")
        self.language = language
        self.pending = ""
        self.produced = 0
        self.limit = limit

    def feed(self, delta: str, *, final: bool = False) -> str:
        if any("\u0900" <= c <= "\u097f" for c in delta):
            self.language = "hi"
        self.pending += delta
        if len(self.pending) > self.limit:
            raise VoiceEngineError("voice text token exceeds bound")
        cut = (
            len(self.pending)
            if final
            else max(
                (
                    index + 1
                    for index, char in enumerate(self.pending)
                    if char.isspace()
                ),
                default=0,
            )
        )
        complete, self.pending = self.pending[:cut], self.pending[cut:]
        normalized = (
            complete
            if self.language == "hi"
            else re.sub(
                r"\S+", lambda match: normalize_numeric_token(match.group()), complete
            )
        )
        self.produced += len(normalized)
        if self.produced > self.limit:
            raise VoiceEngineError("normalized voice response exceeds text bound")
        return normalized
