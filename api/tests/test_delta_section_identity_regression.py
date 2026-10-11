"""A renamed heading is not a new section identity for preservation checks."""
from evolving_profile_api.engine.reflect.delta_ops import AppendBlockOp, RenameSectionOp, apply_operations
from evolving_profile_api.engine.reflect.structured_doc import render_document, render_section, split_markdown


def test_rename_then_append_uses_persisted_section_identity_for_byte_stability():
    original = split_markdown("## Procedure\n\nCall recall.\n\n## Constraints\n\nKeep this exact text.\n")
    renamed = apply_operations(original, [RenameSectionOp(section_id="procedure", new_heading="Recall Procedure")]).document
    prior = {section.id:render_section(section) for section in renamed.sections}
    applied = apply_operations(renamed, [AppendBlockOp(section_id="procedure", text="Run the rerank stage.")])
    current = {section.id:render_section(section) for section in applied.document.sections}
    touched = {operation["section_id"] for operation in applied.applied}
    assert touched == {"procedure"}
    assert current["constraints"] == prior["constraints"]
    assert "Run the rerank stage." in current["procedure"]
    # Re-parsing the render produces a display slug, not the stored identity.
    assert split_markdown(render_document(applied.document)).sections[0].id == "recall-procedure"
    assert applied.document.sections[0].id == "procedure"
