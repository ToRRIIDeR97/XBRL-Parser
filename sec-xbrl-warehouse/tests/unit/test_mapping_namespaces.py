from pathlib import Path

import pytest
import yaml

from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper
from sec_xbrl_warehouse.mapping.namespaces import namespace_family

CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.mark.parametrize(
    ("namespace", "family"),
    [
        ("http://fasb.org/us-gaap/2009-01-31", "us-gaap"),
        ("http://fasb.org/us-gaap/2025", "us-gaap"),
        ("http://fasb.org/srt/2024", "srt"),
        ("http://xbrl.sec.gov/dei/2014-01-31", "dei"),
        ("https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full", "ifrs-full"),
        ("http://fasb.org/us-gaap-like/2025", None),
        ("http://example.com/us-gaap-extension/2025", None),
        ("http://www.apple.com/20250927", None),
        ("http://fasb.org/us-gaap/2025/extra", None),
    ],
)
def test_namespace_family(namespace: str, family: str | None) -> None:
    assert namespace_family(namespace) == family


class _NoRepository:
    async def approved_mappings_for_concept(self, *args, **kwargs):
        return ()

    async def create_reviews(self, filing, reviews):
        return {}


def test_repository_seed_priorities_are_unique_per_metric_and_family() -> None:
    mapper = CanonicalMapper.load_rules(
        CONFIG / "canonical_metrics.yml", CONFIG / "concept_seed.yml", _NoRepository()
    )
    seen: dict[tuple[str, str, int], str] = {}
    for (family, concept), metrics in mapper.candidate_rules.items():
        priority = mapper.candidate_priorities[(family, concept)]
        key = (metrics[0], family, priority)
        assert key not in seen, (key, seen[key], concept)
        seen[key] = concept


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        ({"namespace_hint": "extension", "concept": "X", "canonical_metric": "revenue"}, "hint"),
        ({"namespace_hint": "us-gaap", "concept": "X", "canonical_metric": "fcf"}, "Formula"),
    ],
)
def test_seed_loader_rejects_invalid_candidates(tmp_path: Path, seed, message: str) -> None:
    metrics = tmp_path / "metrics.yml"
    metrics.write_text(
        yaml.safe_dump(
            {
                "metrics": [
                    {
                        "code": "revenue",
                        "name": "Revenue",
                        "statement": "income_statement",
                        "period_type": "duration",
                        "unit_family": "monetary",
                    },
                    {
                        "code": "fcf",
                        "name": "FCF",
                        "statement": "derived",
                        "period_type": "duration",
                        "unit_family": "monetary",
                        "formula": "revenue - revenue",
                    },
                ]
            }
        )
    )
    seeds = tmp_path / "seeds.yml"
    seeds.write_text(yaml.safe_dump({"mappings": [seed]}))
    with pytest.raises(ValueError, match=message):
        CanonicalMapper.load_rules(metrics, seeds, _NoRepository())
