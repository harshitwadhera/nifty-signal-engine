from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime


class Confirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    quantity: int = Field(gt=0, le=1000000, strict=True)
    lots: int = Field(gt=0, le=10000, strict=True)
    actual_entry_premium: float = Field(gt=0, le=10000000)
    underlying_entry: float = Field(gt=0, le=10000000)
    opened_at: AwareDatetime


class Closure(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    actual_exit_premium: float | None = Field(default=None, ge=0, le=10000000)


class Acknowledgement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(min_length=1, max_length=100)
