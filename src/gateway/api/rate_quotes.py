"""POST /v1/rate-quotes (FR-4). M4: the request model. M6 adds the route, idempotency and storage.

The model mirrors `RateQuoteRequest` in contracts/openapi.yaml and is the first line of defence:
nothing reaches the SOAP envelope unless it passed here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# [0-9], never \d: in Python regexes \d also matches other scripts' digits ("٣٠٣٠١"), which the
# contract's ECMA-262 pattern does not, and which Meridian's AS/400 would not understand.
ZIP = r"^[0-9]{5}$"


class RateQuoteRequest(BaseModel):
    # extra="forbid": the contract says additionalProperties: false.
    # strict=True: "1200" (a string) is not a number; the contract says type: number.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    origin_zip: str = Field(pattern=ZIP)
    dest_zip: str = Field(pattern=ZIP)
    weight_lb: float = Field(gt=0, le=45000, allow_inf_nan=False)
    service_level: Literal["LTL_STANDARD", "LTL_EXPEDITED", "FTL"]
