"""Guard rails for services that dial, record or control the Bria app."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.exceptions import ServiceValidationError

from .const import (
    CONF_ALLOW_APP_CONTROL,
    CONF_ALLOW_OUTGOING_CALLS,
    CONF_ALLOW_RECORDING,
    CONF_DIAL_ALLOW_PATTERN,
    DOMAIN,
    MAX_NUMBER_LENGTH,
    SAFETY_DEFAULTS,
)


def safety_option(options: Mapping[str, Any], key: str) -> Any:
    return options.get(key, SAFETY_DEFAULTS[key])


def _blocked(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def check_dial(options: Mapping[str, Any], number: str) -> None:
    """Raise unless the options allow dialling ``number`` (place, transfer, attended transfer)."""
    if not safety_option(options, CONF_ALLOW_OUTGOING_CALLS):
        raise _blocked("outgoing_calls_disabled")
    if len(number) > MAX_NUMBER_LENGTH:
        raise _blocked("number_too_long", max=str(MAX_NUMBER_LENGTH))
    pattern = safety_option(options, CONF_DIAL_ALLOW_PATTERN)
    if pattern and re.fullmatch(pattern, number.strip()) is None:
        raise _blocked("number_not_allowed", number=number)


def check_recording(options: Mapping[str, Any], filename: str) -> None:
    if not safety_option(options, CONF_ALLOW_RECORDING):
        raise _blocked("recording_disabled")
    if ".." in filename or any(ord(c) < 32 for c in filename):
        raise _blocked("recording_filename_invalid")


def check_app_control(options: Mapping[str, Any]) -> None:
    """Sign in, sign out and exit."""
    if not safety_option(options, CONF_ALLOW_APP_CONTROL):
        raise _blocked("app_control_disabled")
