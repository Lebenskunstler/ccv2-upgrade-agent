from dataclasses import dataclass
from typing import Any


@dataclass
class GatesOnlyResult:
    all_pass: bool
    raw: Any


@dataclass
class AbstractPipelineRunResult:
    succeeded: bool
    raw: Any


@dataclass
class ClassicPipelineRunResult:
    status: str
    raw: dict
