"""Load Curie owned YAML without silently replacing repeated keys."""

from collections.abc import Hashable
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.error import Mark
from yaml.nodes import MappingNode, Node

_MERGE_TAG = "tag:yaml.org,2002:merge"
_VALUE_TAG = "tag:yaml.org,2002:value"
_MERGE_KEY = object()


class DuplicateKeyError(ConstructorError):
    """A mapping repeats a key authored in the same mapping."""

    def __init__(self, key: object, context_mark: Mark, problem_mark: Mark) -> None:
        self.key = key
        super().__init__(
            "while constructing a mapping",
            context_mark,
            f"found duplicate key {key!r}",
            problem_mark,
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._checked_mapping_nodes: set[int] = set()

    def _check_authored_keys(self, node: Node) -> None:
        if not isinstance(node, MappingNode):
            return

        node_id = id(node)
        if node_id in self._checked_mapping_nodes:
            return
        self._checked_mapping_nodes.add(node_id)

        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                key: object = "<<"
                comparable: Hashable = _MERGE_KEY
            elif key_node.tag == _VALUE_TAG:
                key = key_node.value
                comparable = key
            else:
                key = self.construct_object(key_node, deep=False)
                if not isinstance(key, Hashable):
                    raise ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found unhashable key",
                        key_node.start_mark,
                    )
                comparable = key
            if comparable in seen:
                raise DuplicateKeyError(key, node.start_mark, key_node.start_mark)
            seen.add(comparable)

    def flatten_mapping(self, node: MappingNode) -> None:
        self._check_authored_keys(node)
        super().flatten_mapping(node)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Hashable, Any]:
        self._check_authored_keys(node)
        return super().construct_mapping(node, deep=deep)


def safe_load_unique(text: str) -> Any:
    """Safely load YAML and reject repeated authored mapping keys."""

    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]
