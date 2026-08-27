"""Every ``build:``-declaring connector in an example bundle has a publish job.

The failure this exists to stop, from the install it actually happened on:

A connector that declares ``build:`` and nothing else has no published image. At
the skill and local tiers that is fine -- ``curie build`` records a local image id
and the local daemon has it. At the **cluster** tier it is refused, because a
cluster cannot pull an image that exists only in one machine's Docker daemon. So
an unpublished connector is a connector the live bot cannot have, and if that
connector carries the bundle's write verb, it is a *write verb* the live bot
cannot have.

``examples/sre-bot``'s ``k8s-scale`` reached `next` merged and reviewed in exactly
that state: the source, the Dockerfile, the tests and the gate all landed, no
image was ever published, and the deployed bot answered `restart_deployment`
alone. Nothing reported it -- a bundle validates, a connector is healthy, and the
absent second verb looks like a design choice rather than a missing pipeline.

The check is deliberately shallow and cheap: it reads the declaration and the
release workflow, not a registry. It cannot tell you whether a published image is
*current*; it tells you whether anything will ever publish one at all, which is
the failure that stayed invisible.

Naming: connector ``tempo`` in bundle ``sre-bot`` publishes as ``sre-bot-tempo``,
the convention ``release.yaml``'s matrix already uses.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yaml"


def _published_image_names() -> set[str]:
    """The image names ``release.yaml`` builds and pushes.

    Read off the matrix's ``include`` entries rather than the ``name:`` list,
    because an entry in the list with no ``include`` has no build context and so
    publishes nothing -- the list alone would pass a half-wired addition.
    """

    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    names: set[str] = set()
    for job in (workflow.get("jobs") or {}).values():
        include = (((job.get("strategy") or {}).get("matrix") or {}).get("include")) or []
        for entry in include:
            if isinstance(entry, dict) and entry.get("dockerfile") and entry.get("name"):
                names.add(str(entry["name"]))
    return names


def _build_connectors() -> list[tuple[str, str]]:
    """``(bundle, connector)`` for every connector declaring ``build:``."""

    found: list[tuple[str, str]] = []
    for declaration in sorted(EXAMPLES.glob("*/connectors.yaml")):
        bundle = declaration.parent.name
        parsed = yaml.safe_load(declaration.read_text(encoding="utf-8")) or {}
        for name, spec in (parsed.get("connectors") or {}).items():
            if isinstance(spec, dict) and "build" in spec:
                found.append((bundle, str(name)))
    return found


def test_the_declaration_and_the_workflow_are_both_readable() -> None:
    # A guard that silently finds nothing passes vacuously, which is the failure
    # mode of every check that reads files by glob.
    assert _build_connectors(), "no build-declaring connectors found: check the glob"
    assert _published_image_names(), "no publishable images found in release.yaml"


@pytest.mark.parametrize("bundle,connector", _build_connectors())
def test_a_build_declaring_connector_has_a_publish_job(bundle: str, connector: str) -> None:
    expected = f"{bundle}-{connector}"
    published = _published_image_names()
    assert expected in published, (
        f"{bundle}'s '{connector}' connector declares build: but nothing publishes "
        f"'{expected}'. Without a published image it cannot be deployed at the "
        f"cluster tier, so it is a capability the live bot cannot have. Add it to "
        f"release.yaml's image matrix (both the name list and an include entry "
        f"naming its context and dockerfile), or drop the connector."
    )
