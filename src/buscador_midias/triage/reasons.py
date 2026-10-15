"""Motivos de descarte compartilhados pelas camadas da triagem."""

from __future__ import annotations

from enum import StrEnum


class FilterReason(StrEnum):
    """Por que uma URL foi descartada na triagem."""

    HOME = "home"
    LISTING = "listing"
    SECTION = "section"
    ASSET = "asset"
    OFF_TOPIC = "off_topic"
    LOW_SIGNAL = "l1_low_signal"
