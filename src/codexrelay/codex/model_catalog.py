from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexModelOption:
    model: str
    display_name: str
    description: str
    default_reasoning_effort: str
    supported_reasoning_efforts: tuple[str, ...]
    is_default: bool = False

    def supports(self, reasoning_effort: str) -> bool:
        return reasoning_effort in self.supported_reasoning_efforts


class CodexModelCatalog:
    def __init__(self, models: tuple[CodexModelOption, ...]) -> None:
        if not models:
            raise ValueError("Codex did not report any available models")
        if len({option.model for option in models}) != len(models):
            raise ValueError("Codex reported duplicate model identifiers")
        self.models = models

    @property
    def default(self) -> CodexModelOption:
        return next((option for option in self.models if option.is_default), self.models[0])

    def get(self, model: str) -> CodexModelOption | None:
        return next((option for option in self.models if option.model == model), None)

    def resolve(self, selector: str) -> CodexModelOption:
        normalized = selector.strip()
        if normalized.isdigit():
            index = int(normalized)
            if 1 <= index <= len(self.models):
                return self.models[index - 1]
        matches = [
            option
            for option in self.models
            if normalized.casefold() in {option.model.casefold(), option.display_name.casefold()}
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"unknown Codex model: {selector}")
        raise ValueError(f"ambiguous Codex model: {selector}")

    def effective(
        self, model: str | None, reasoning_effort: str | None
    ) -> tuple[CodexModelOption, str]:
        option = self.get(model) if model is not None else None
        option = option or self.default
        effort = reasoning_effort or option.default_reasoning_effort
        if not option.supports(effort):
            effort = option.default_reasoning_effort
        return option, effort


REASONING_EFFORT_LABELS = {
    "none": "无",
    "minimal": "最少",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "超高",
    "max": "最大",
    "ultra": "极限",
}


def reasoning_effort_label(value: str) -> str:
    return REASONING_EFFORT_LABELS.get(value, value)
