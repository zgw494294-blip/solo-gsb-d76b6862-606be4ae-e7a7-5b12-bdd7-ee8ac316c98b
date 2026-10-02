from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Request models reject unknown fields so typos cannot be silently ignored."""

    model_config = ConfigDict(extra="forbid")


class SourceColumnIn(StrictModel):
    name: str = Field(min_length=1)
    sensitive: bool = False


class SourceNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["source"]
    columns: list[SourceColumnIn] = Field(min_length=1)


class ProjectionColumnIn(StrictModel):
    """An output column either copies an upstream column or is replaced by a constant.

    ``constant`` may legitistically be JSON ``null``, so presence is checked with
    ``model_fields_set`` rather than comparing against the default value.
    The field is aliased to ``copy`` to avoid shadowing ``BaseModel.copy``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    copy_: str | None = Field(default=None, alias="copy", min_length=1)
    constant: object | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ProjectionColumnIn":
        has_copy = "copy_" in self.model_fields_set
        has_constant = "constant" in self.model_fields_set
        if has_copy == has_constant:
            raise ValueError(
                "each projection column must specify exactly one of 'copy' or 'constant'"
            )
        return self

    @property
    def copy_name(self) -> str | None:
        return self.copy_

    @property
    def is_constant(self) -> bool:
        return "constant" in self.model_fields_set


class ProjectionNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["projection"]
    input: str = Field(min_length=1)
    columns: list[ProjectionColumnIn] = Field(min_length=1)


class FilterNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["filter"]
    input: str = Field(min_length=1)
    references: list[str] = Field(min_length=1)


class JoinOutputIn(StrictModel):
    alias: str = Field(min_length=1)
    side: Literal["left", "right"]
    column: str = Field(min_length=1)


class JoinNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["join"]
    left_input: str = Field(min_length=1)
    right_input: str = Field(min_length=1)
    left_key: str = Field(min_length=1)
    right_key: str = Field(min_length=1)
    outputs: list[JoinOutputIn] = Field(min_length=1)


NodeIn = Annotated[
    Union[SourceNodeIn, ProjectionNodeIn, FilterNodeIn, JoinNodeIn],
    Field(discriminator="type"),
]


class ReviewRequest(StrictModel):
    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)


class ReviseRequest(StrictModel):
    """A review request plus the export columns the caller allows nulling.

    The whole graph is still validated by the same rules as ``ReviewRequest``;
    the replacement list may be empty (distinctness is then vacuous) and every
    listed name must be non-empty. Existence of each name on the export node is
    checked in the engine together with the rest of the graph.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    allowed_replacements: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _replacement_names_distinct(self) -> "ReviseRequest":
        seen: set[str] = set()
        for name in self.allowed_replacements:
            if name in seen:
                raise ValueError(
                    f"replacement column '{name}' is listed more than once"
                )
            seen.add(name)
        return self


class PruneRequest(StrictModel):
    """A review request plus the export columns the caller wants to keep.

    ``columns`` selects a non-empty subset of the export node's columns. The
    names must be non-empty and distinct (their order never matters: the result
    always follows the export node's original column order). The whole graph is
    still validated by the same rules as ``ReviewRequest``; existence of every
    selected name on the export node is checked in the engine.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    columns: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def _column_names_distinct(self) -> "PruneRequest":
        seen: set[str] = set()
        for name in self.columns:
            if name in seen:
                raise ValueError(
                    f"selected column '{name}' is listed more than once"
                )
            seen.add(name)
        return self
