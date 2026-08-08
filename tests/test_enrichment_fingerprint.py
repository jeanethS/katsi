from katsi_core.workspace.enrichment import EnrichmentFingerprint


def test_enrichment_fingerprint_is_deterministic_and_changes_with_compatibility_inputs() -> None:
    base = EnrichmentFingerprint(
        content_hash="a" * 64,
        extraction_contract_version="1",
        model_identity="local",
        prompt_version="1",
        chunking_version="1",
        semantic_settings_version="1",
    )
    same = EnrichmentFingerprint(
        content_hash="a" * 64,
        extraction_contract_version="1",
        model_identity="local",
        prompt_version="1",
        chunking_version="1",
        semantic_settings_version="1",
    )
    changed = base.model_copy(update={"prompt_version": "2"})

    assert base.digest() == same.digest()
    assert base.digest() != changed.digest()
