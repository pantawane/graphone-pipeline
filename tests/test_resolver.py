import pytest

from src.resolver import EntityResolver, load_seeds


@pytest.fixture()
def resolver():
    return EntityResolver(load_seeds())


def test_seed_file_has_50_unique_entries():
    seeds = load_seeds()
    assert len(seeds) == 50
    assert len({s["canonical"] for s in seeds}) == 50


@pytest.mark.parametrize("raw", ["OpenAI", "OpenAI, Inc.", "Open AI", "OPEN AI", "openai", "Open AI, Inc"])
def test_openai_variants(resolver, raw):
    r = resolver.resolve(raw)
    assert r.canonical == "OpenAI" and not r.is_new


def test_hugging_face_variants(resolver):
    assert resolver.resolve("HuggingFace Inc.").canonical == "Hugging Face"


def test_alias_and_ampersand(resolver):
    assert resolver.resolve("W&B").canonical == "Weights & Biases"
    assert resolver.resolve("Weights and Biases").canonical == "Weights & Biases"
    assert resolver.resolve("Mistral").canonical == "Mistral AI"
    assert resolver.resolve("Mistral AI SAS").canonical == "Mistral AI"


def test_domain_match(resolver):
    r = resolver.resolve("Unknown Co", domain="https://careers.openai.com/jobs/1")
    assert r.canonical == "OpenAI" and r.method == "domain"


def test_core_name(resolver):
    r = resolver.resolve("Cohere AI")
    assert r.canonical == "Cohere" and r.method == "core_name"


def test_typo_is_fuzzy_matched(resolver):
    r = resolver.resolve("Anthropik")
    assert r.canonical == "Anthropic" and r.method == "fuzzy_edit_distance"


def test_short_and_near_names_are_not_confused(resolver):
    assert resolver.resolve("Grok").is_new            # xAI's chatbot, not the chip company Groq
    assert resolver.resolve("Coherent").is_new        # not Cohere


def test_new_entity_is_consistent(resolver):
    a = resolver.resolve("Foo Labs Inc")
    b = resolver.resolve("Foo Labs")
    assert a.is_new and a.canonical == "Foo Labs" and b.canonical == "Foo Labs" and not b.is_new


def test_seed_conflict_is_rejected():
    seeds = [{"canonical": "A Corp", "aliases": ["Shared"]}, {"canonical": "B Corp", "aliases": ["Shared"]}]
    with pytest.raises(ValueError):
        EntityResolver(seeds)