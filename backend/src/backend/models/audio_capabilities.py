"""Validated application model for device audio routing and effects."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EffectStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested: bool
    available: bool
    enabled: bool

    @model_validator(mode="after")
    def enabled_effect_must_be_available_and_requested(self) -> "EffectStatus":
        if self.enabled and (not self.requested or not self.available):
            raise ValueError("an enabled effect must be requested and available")
        return self


class VoiceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["duplex_full", "duplex_isolated", "duplex_half"]
    input_route: Literal["built_in_mic", "bluetooth_hfp", "wired_mic", "usb", "unknown"]
    output_route: Literal[
        "speakerphone",
        "earpiece",
        "headphones",
        "bluetooth_hfp",
        "usb",
        "remote",
        "unknown",
    ]
    incremental_playback: bool = False
    native_sample_rate: int = Field(gt=0, le=384_000)
    aec: EffectStatus
    noise_suppression: EffectStatus
    fallback_reason: (
        Literal[
            "aec_unavailable",
            "aec_unhealthy",
            "route_not_isolated",
            "unsupported_route",
            "platform_unavailable",
        ]
        | None
    )

    @model_validator(mode="after")
    def route_and_effects_match_mode(self) -> "VoiceCapabilities":
        if self.mode == "duplex_full" and (
            self.output_route != "speakerphone" or not self.aec.enabled
        ):
            raise ValueError(
                "duplex_full requires speakerphone output with enabled AEC"
            )
        if self.mode == "duplex_isolated" and self.output_route not in {
            "headphones",
            "bluetooth_hfp",
            "usb",
        }:
            raise ValueError("duplex_isolated requires an isolated output route")
        if self.mode == "duplex_half" and self.fallback_reason is None:
            raise ValueError("duplex_half requires a fallback reason")
        if self.mode != "duplex_half" and self.fallback_reason is not None:
            raise ValueError("non-fallback modes cannot report a fallback reason")
        return self
