"""Unit table for ``plugin_format.grantable_routes`` (#558).

The operator opt-in ``grantableViaPolicy`` marks a gate whose policy-gate
approval MAY mint a one-shot grant for the tool the gate names (its ``gate``
field, MANIFEST-supplied, never model-supplied). ``grantable_routes`` derives the
``{route: tool}`` map the runner and worker consume, plus the set of routes that
are AMBIGUOUS (one route claimed by more than one distinct grantable tool) and so
excluded from the map. Comparison is case-sensitive and both fields are stripped,
mirroring ``load_approval_policy``'s normalization so a config that validates
green at deploy resolves identically at runtime (#453).
"""

from plugin_format import ApprovalGate, grantable_routes


def _gate(gate: str, route: str, grantable: bool = False) -> ApprovalGate:
    return ApprovalGate.model_validate(
        {"gate": gate, "route": route, "grantableViaPolicy": grantable}
    )


def test_empty_input_yields_empty_map_and_no_ambiguity() -> None:
    assert grantable_routes([]) == ({}, set())


def test_single_grantable_gate_maps_route_to_tool() -> None:
    routes, ambiguous = grantable_routes([_gate("close_issue", "deal-desk", grantable=True)])
    assert routes == {"deal-desk": "close_issue"}
    assert ambiguous == set()


def test_non_grantable_gates_are_ignored() -> None:
    # Absent / false grantableViaPolicy contributes nothing.
    routes, ambiguous = grantable_routes(
        [
            _gate("close_issue", "deal-desk", grantable=False),
            _gate("escalate", "managers"),
        ]
    )
    assert routes == {}
    assert ambiguous == set()


def test_route_with_two_distinct_tools_is_ambiguous_and_excluded() -> None:
    routes, ambiguous = grantable_routes(
        [
            _gate("close_issue", "deal-desk", grantable=True),
            _gate("escalate", "deal-desk", grantable=True),
        ]
    )
    # Excluded from the resolved map AND surfaced in the ambiguous set.
    assert routes == {}
    assert ambiguous == {"deal-desk"}


def test_duplicate_same_tool_same_route_is_not_ambiguous() -> None:
    # One route, one DISTINCT tool (declared twice) is a duplicate, not a
    # conflict: a single entry, nothing ambiguous.
    routes, ambiguous = grantable_routes(
        [
            _gate("close_issue", "deal-desk", grantable=True),
            _gate("close_issue", "deal-desk", grantable=True),
        ]
    )
    assert routes == {"deal-desk": "close_issue"}
    assert ambiguous == set()


def test_gate_and_route_are_stripped() -> None:
    routes, ambiguous = grantable_routes(
        [_gate("  close_issue  ", "  deal-desk  ", grantable=True)]
    )
    assert routes == {"deal-desk": "close_issue"}
    assert ambiguous == set()


def test_blank_gate_or_route_is_ignored() -> None:
    # A grantable gate whose gate or route is empty once stripped keys nothing.
    routes, ambiguous = grantable_routes(
        [
            _gate("   ", "deal-desk", grantable=True),
            _gate("close_issue", "   ", grantable=True),
        ]
    )
    assert routes == {}
    assert ambiguous == set()


def test_route_matching_is_case_sensitive() -> None:
    # Deal-Desk and deal-desk are distinct routes, so two grantable gates on
    # them do not collide: both resolve, nothing is ambiguous.
    routes, ambiguous = grantable_routes(
        [
            _gate("close_issue", "Deal-Desk", grantable=True),
            _gate("escalate", "deal-desk", grantable=True),
        ]
    )
    assert routes == {"Deal-Desk": "close_issue", "deal-desk": "escalate"}
    assert ambiguous == set()
