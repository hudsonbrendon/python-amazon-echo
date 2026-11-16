"""Spoken phrases for the things Alexa only accepts as a voice request.

Alexa has no API to start a timer on an Echo: the timers API of the app starts
it on the app's own registration. A typed request works, but only with the
numbers spelled out ("timer de dez minutos" starts one, "timer de 10 minutos"
makes Alexa ask for how long), so the duration is written in words here.
"""

from __future__ import annotations

from datetime import timedelta

from .exceptions import AmazonEchoNotSupportedError

_PT_UNITS = [
    "zero",
    "um",
    "dois",
    "três",
    "quatro",
    "cinco",
    "seis",
    "sete",
    "oito",
    "nove",
    "dez",
    "onze",
    "doze",
    "treze",
    "quatorze",
    "quinze",
    "dezesseis",
    "dezessete",
    "dezoito",
    "dezenove",
]
_PT_TENS = {20: "vinte", 30: "trinta", 40: "quarenta", 50: "cinquenta"}
_EN_UNITS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_EN_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty"}


def _pt(number: int, *, feminine: bool = False) -> str:
    if number < 20:
        word = _PT_UNITS[number]
        return {"um": "uma", "dois": "duas"}.get(word, word) if feminine else word
    tens, rest = number - number % 10, number % 10
    return _PT_TENS[tens] + (f" e {_pt(rest, feminine=feminine)}" if rest else "")


def _en(number: int) -> str:
    if number < 20:
        return _EN_UNITS[number]
    tens, rest = number - number % 10, number % 10
    return _EN_TENS[tens] + (f"-{_EN_UNITS[rest]}" if rest else "")


def _parts(duration: timedelta) -> tuple[int, int, int]:
    seconds = int(duration.total_seconds())
    if not 0 < seconds < 24 * 3600:
        raise ValueError("The timer duration must be between 1 second and 24 hours")
    return seconds // 3600, seconds % 3600 // 60, seconds % 60


def timer_request(
    duration: timedelta, language: str, *, label: str | None = None
) -> str:
    """Return the typed request that starts a timer of ``duration``.

    Args:
        duration: More than zero and less than 24 hours.
        language: Locale of the device, e.g. ``pt-BR`` or ``en-US``.
        label: Optional name of the timer.

    Raises:
        AmazonEchoNotSupportedError: No phrase for that language.
        ValueError: The duration is out of range.

    """
    hours, minutes, seconds = _parts(duration)
    code = language[:2].lower()
    if code == "pt":
        spoken = " e ".join(
            f"{_pt(n, feminine=feminine)} {one if n == 1 else many}"
            for n, one, many, feminine in (
                (hours, "hora", "horas", True),
                (minutes, "minuto", "minutos", False),
                (seconds, "segundo", "segundos", False),
            )
            if n
        )
        return f"timer de {spoken}" + (f" chamado {label}" if label else "")
    if code == "en":
        spoken = " and ".join(
            f"{_en(n)} {one if n == 1 else many}"
            for n, one, many in (
                (hours, "hour", "hours"),
                (minutes, "minute", "minutes"),
                (seconds, "second", "seconds"),
            )
            if n
        )
        return (
            (f"set a {label} timer for {spoken}")
            if label
            else f"set a timer for {spoken}"
        )
    raise AmazonEchoNotSupportedError(f"No timer phrase for the language {language!r}")
