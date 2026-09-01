from __future__ import annotations

import pytest

from codexrelay.codex.model_catalog import CodexModelCatalog, CodexModelOption


def catalog() -> CodexModelCatalog:
    return CodexModelCatalog(
        (
            CodexModelOption(
                model="gpt-fast",
                display_name="GPT Fast",
                description="Fast",
                default_reasoning_effort="medium",
                supported_reasoning_efforts=("low", "medium", "high"),
                is_default=True,
            ),
            CodexModelOption(
                model="gpt-deep",
                display_name="GPT Deep",
                description="Deep",
                default_reasoning_effort="high",
                supported_reasoning_efforts=("medium", "high", "xhigh"),
            ),
        )
    )


def test_catalog_resolves_stable_number_slug_and_display_name() -> None:
    models = catalog()

    assert models.resolve("2").model == "gpt-deep"
    assert models.resolve("GPT-FAST").model == "gpt-fast"
    assert models.resolve("gpt deep").model == "gpt-deep"


def test_catalog_effective_selection_falls_back_to_model_defaults() -> None:
    models = catalog()

    option, effort = models.effective("gpt-deep", "low")

    assert option.model == "gpt-deep"
    assert effort == "high"


def test_catalog_rejects_unknown_models() -> None:
    with pytest.raises(ValueError, match="unknown"):
        catalog().resolve("missing")
