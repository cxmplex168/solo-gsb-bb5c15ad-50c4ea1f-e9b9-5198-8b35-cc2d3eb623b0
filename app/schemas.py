"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class ReconcileRequest(BaseModel):
    """恰好三段待核对文本。"""

    texts: list[str] = Field(..., description="恰好三段文本,每段不超过 120 个字素簇")

    @field_validator("texts")
    @classmethod
    def _exactly_three(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError("必须恰好提供三段文本(texts 长度须为 3)")
        return v


class Anchor(BaseModel):
    """一个锚点:三份文本中必须同列对齐的字素簇位置(0 起始)。"""

    positions: list[int] = Field(
        ..., description="恰好三个位置,依次为 texts[0]/texts[1]/texts[2] 中的字素簇下标"
    )

    @field_validator("positions")
    @classmethod
    def _exactly_three(cls, v: list[int]) -> list[int]:
        if len(v) != 3:
            raise ValueError("锚点必须恰好给出三个位置(positions 长度须为 3)")
        return v


class AnchoredReconcileRequest(ReconcileRequest):
    """三段文本加锚点列表。"""

    anchors: list[Anchor] = Field(
        default_factory=list,
        description="锚点列表;各来源位置须严格递增、不越界、不重复,否则返回 422",
    )


class SourceCell(BaseModel):
    """某列中一个来源的字素及其原位置;空位均为 null。"""

    grapheme: str | None
    position: int | None


class Column(BaseModel):
    index: int = Field(..., description="列序号(0 起始)")
    mask: int = Field(..., description="本列消费的来源位掩码,bit0/1/2 对应 texts[0/1/2]")
    cost: int = Field(..., description="本列成本")
    sources: list[SourceCell] = Field(..., description="三个来源的字素与原位置")
    consensus: str = Field(..., description="本列共识字素,未达成时为 �")


class UncertainColumn(BaseModel):
    column: int = Field(..., description="未达成共识的列序号")
    sources: list[SourceCell] = Field(..., description="三个来源的字素与原位置")


class ReconcileResponse(BaseModel):
    consensus: str = Field(..., description="共识文本")
    total_cost: int = Field(..., description="对齐总成本")
    columns: list[Column] = Field(..., description="对齐列")
    uncertain: list[UncertainColumn] = Field(..., description="写入 � 的列及各来源字素与原位置")


class AnchoredColumn(Column):
    """附带 anchored 标记的对齐列。"""

    anchored: bool = Field(..., description="本列是否为锚点强制对齐列")


class AnchoredReconcileResponse(ReconcileResponse):
    columns: list[AnchoredColumn] = Field(..., description="对齐列(含 anchored 标记)")


class Decision(BaseModel):
    """一项人工裁决:以三来源原始位置签名定位一个 uncertain 列并给出采用值。"""

    signature: list[int | None] = Field(
        ...,
        description=(
            "三来源原始位置签名,依次为该列在 texts[0/1/2] 中的字素簇下标;"
            "该列未消费的来源记为 null(与 uncertain 列 sources 中的 position 一致)"
        ),
    )
    source: int | None = Field(
        default=None, description="采用某个非空来源字素:来源下标 0/1/2;与 replacement 二选一"
    )
    replacement: str | None = Field(
        default=None, description="提供恰好一个 NFC 字素簇作为替换值;与 source 二选一"
    )

    @field_validator("signature")
    @classmethod
    def _signature_shape(cls, v: list) -> list:
        if len(v) != 3:
            raise ValueError("signature 必须恰好给出三个位置(长度须为 3,空位为 null)")
        for pos in v:
            if pos is not None and pos < 0:
                raise ValueError("signature 中的位置不得为负")
        return v

    @field_validator("source")
    @classmethod
    def _source_range(cls, v: int | None) -> int | None:
        if v is not None and v not in (0, 1, 2):
            raise ValueError("source 必须是 0、1 或 2")
        return v

    @field_validator("replacement")
    @classmethod
    def _replacement_non_empty(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            raise ValueError("replacement 不能为空;须提供恰好一个 NFC 字素簇")
        return v

    @model_validator(mode="after")
    def _exactly_one_choice(self) -> "Decision":
        if (self.source is None) == (self.replacement is None):
            raise ValueError("每项 decision 必须且只能提供 source 或 replacement 之一")
        return self


class AdjudicateRequest(AnchoredReconcileRequest):
    """三段文本、可选锚点与待校验的人工裁决列表。"""

    decisions: list[Decision] = Field(
        default_factory=list,
        description="人工裁决列表;全部有效才生效,任一项失效返回 409 且无部分结果",
    )


class Audit(BaseModel):
    """单项裁决的审计记录。"""

    signature: list[int | None] = Field(
        ..., description="被裁决列的三来源原始位置签名(空位为 null)"
    )
    original: list[str | None] = Field(
        ..., description="裁决前该列三个来源的原始字素(空位为 null)"
    )
    adopted: str = Field(..., description="裁决采用的字素簇")
    method: str = Field(
        ...,
        description="裁决方式:source:0/source:1/source:2 表示采用某来源字素,replacement 表示人工替换",
    )


class AdjudicateResponse(AnchoredReconcileResponse):
    uncertain: list[UncertainColumn] = Field(..., description="尚未裁决的 uncertain 列")
    audits: list[Audit] = Field(..., description="本次生效裁决的审计项,顺序与请求 decisions 一致")
