import uuid
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

GRID_COLUMNS = 12
MAX_TILE_ROWS = 8
MAX_ITEMS = 20
DEFAULT_LAYOUT: dict[str, object] = {"columns": GRID_COLUMNS}


class RelativeRange(BaseModel):
    """ "The last `days` days, ending today" -- today being the *project's*
    calendar date, resolved by the client. Counts today, so days=7 is today and
    the six days before it."""

    type: Literal["relative"] = "relative"
    days: int = Field(ge=1, le=366)


class AbsoluteRange(BaseModel):
    type: Literal["absolute"] = "absolute"
    from_: date = Field(alias="from")
    to: date

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _ordered(self) -> "AbsoluteRange":
        if self.from_ > self.to:
            raise ValueError("range.from must not be after range.to")
        return self


DashboardRange = Annotated[RelativeRange | AbsoluteRange, Field(discriminator="type")]

DEFAULT_RANGE = RelativeRange(days=30)


class Position(BaseModel):
    """A tile's rectangle on a 12-column grid: `x`/`w` in columns, `y`/`h` in
    rows. Origin top-left."""

    x: int = Field(ge=0, lt=GRID_COLUMNS)
    y: int = Field(ge=0)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=MAX_TILE_ROWS)

    @model_validator(mode="after")
    def _fits_grid(self) -> "Position":
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError(
                f"a tile must fit within {GRID_COLUMNS} columns (x + w <= {GRID_COLUMNS})"
            )
        return self


class ItemInput(BaseModel):
    insight_id: uuid.UUID
    position: Position


def overlaps(a: Position, b: Position) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def validate_layout(items: list[ItemInput]) -> None:
    """Whole-layout rules a single tile can't check on its own. Raises
    ValueError with a message safe to show the caller."""
    if len(items) > MAX_ITEMS:
        raise ValueError(f"a dashboard can hold at most {MAX_ITEMS} insights")
    ids = [item.insight_id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("the same insight can only appear once on a dashboard")
    for i, first in enumerate(items):
        for second in items[i + 1 :]:
            if overlaps(first.position, second.position):
                raise ValueError("tiles must not overlap")
