"""数据集注册表：由 external.py 机械构建，供双出口与契约测试共用。"""
from app.datasets.external import EXTERNAL_DATASETS
from app.datasets.spec import DatasetSpec

DATASET_REGISTRY: dict[str, DatasetSpec] = {d.id: d for d in EXTERNAL_DATASETS}

# 源链：dataset id → provider 名列表（左→右为降级方向）。
# Phase 1 external 数据集均单源（provider 内部已含多级兜底）。
SOURCE_CHAIN: dict[str, tuple[str, ...]] = {
    d.id: (d.source,) for d in EXTERNAL_DATASETS if d.source
}


def all_datasets() -> list[DatasetSpec]:
    return list(EXTERNAL_DATASETS)


def get_spec(dataset_id: str) -> DatasetSpec | None:
    return DATASET_REGISTRY.get(dataset_id)


def source_chain(dataset_id: str) -> tuple[str, ...]:
    return SOURCE_CHAIN.get(dataset_id, ())
