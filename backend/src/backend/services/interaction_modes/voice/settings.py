"""Validated operation settings, separate from archival model configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import backend.config as config


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    vault_retrieval_enabled: bool = False
    default_engine: Literal["modular", "realtime"] = "modular"
    idle_timeout_seconds: int = Field(default=60, ge=5, le=1200)
    max_duration_seconds: int = Field(default=1200, ge=60, le=3000)
    vault_timeout_seconds: float = Field(default=15, gt=0, le=60)
    hermes_timeout_seconds: float = Field(default=600, gt=0, le=3600)

    @classmethod
    def load(cls):

        return cls.model_validate(config.get_config().get("voice", {}))
