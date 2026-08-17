"""Keeping generated diagrams renderable.

A broken Mermaid block is not a missing picture — GitHub and VS Code draw a red
parse error in its place, which is worse than no diagram at all.
"""

from clipdesk.analysis.mermaid import repair, repair_markdown


def fenced(body):
    return f"Before.\n\n```mermaid\n{body}\n```\n\nAfter.\n"


# --- what makes a block usable ----------------------------------------------
def test_a_valid_diagram_is_left_alone():
    body = "flowchart TD\n    A[Start] --> B[Finish]"

    assert repair(body) == body


def test_a_block_that_is_not_a_diagram_is_dropped():
    assert repair("Here is how it works:\n    A --> B") is None


def test_a_diagram_with_nothing_in_it_is_dropped():
    assert repair("flowchart TD") is None
    assert repair("   ") is None


def test_every_diagram_type_the_model_might_choose_is_kept():
    for header in ("sequenceDiagram", "stateDiagram-v2", "erDiagram", "graph LR", "mindmap"):
        assert repair(f"{header}\n    A --> B") is not None


# --- the failure that actually happens ---------------------------------------
def test_brackets_inside_a_label_are_quoted():
    # "A[Retry (max 3)]" ends the label at the first ")" and fails to parse.
    out = repair("flowchart TD\n    A[Retry (max 3)] --> B[Done]")

    assert 'A["Retry (max 3)"]' in out
    assert "B[Done]" in out


def test_a_decision_label_is_quoted_too():
    out = repair("flowchart TD\n    A{Retry, or fail?} --> B[Done]")

    assert 'A{"Retry, or fail?"}' in out


def test_a_label_that_is_already_quoted_is_not_quoted_twice():
    body = 'flowchart TD\n    A["Retry (max 3)"] --> B[Done]'

    assert repair(body) == body


def test_a_quote_inside_a_label_becomes_an_entity():
    # Mermaid has no escape for a quote inside a quoted label.
    out = repair('flowchart TD\n    A[Says "hello", loudly] --> B[Done]')

    assert '#quot;hello#quot;' in out
    assert out.count('"') == 2


def test_markdown_in_a_label_is_removed():
    out = repair("flowchart TD\n    A[**Bold** step] --> B[Done]")

    assert "A[Bold step]" in out
    assert "**" not in out


def test_end_cannot_be_used_as_a_node_name():
    out = repair("flowchart TD\n    start[Begin] --> end[Stop]")

    assert "endStep[Stop]" in out


def test_a_subgraph_terminator_is_left_alone():
    body = "flowchart TD\n    subgraph One\n    A[Go] --> B[Stop]\n    end"

    assert repair(body).endswith("end")


def test_only_a_flowchart_has_its_labels_quoted():
    # A class body uses braces, and a sequence note uses brackets, for something
    # other than a node label. Quoting either would break the diagram.
    class_body = "classDiagram\n    class Animal {\n      +int age\n    }"
    sequence = "sequenceDiagram\n    PC->>Server: SYN [seq 10]"

    assert repair(class_body) == class_body
    assert repair(sequence) == sequence


# --- inside a document -------------------------------------------------------
def test_a_document_without_a_diagram_is_untouched():
    text = "# Notes\n\nNothing to draw here.\n"

    assert repair_markdown(text) == text


def test_a_diagram_in_a_document_is_repaired_in_place():
    out = repair_markdown(fenced("flowchart TD\n    A[Retry (max 3)] --> B[Done]"))

    assert 'A["Retry (max 3)"]' in out
    assert out.startswith("Before.")
    assert out.rstrip().endswith("After.")


def test_a_hopeless_diagram_is_removed_from_the_document():
    out = repair_markdown(fenced("Just some prose the model fenced by mistake."))

    assert "```mermaid" not in out
    assert "Before." in out and "After." in out


def test_other_code_blocks_are_not_touched():
    text = "```python\nA[Retry (max 3)]\n```\n"

    assert repair_markdown(text) == text


def test_several_diagrams_are_all_repaired():
    text = fenced("flowchart TD\n    A[One (1)] --> B[x]") + fenced(
        "flowchart LR\n    C[Two (2)] --> D[y]"
    )
    out = repair_markdown(text)

    assert 'A["One (1)"]' in out
    assert 'C["Two (2)"]' in out


def test_an_unclosed_block_does_not_lose_the_document():
    out = repair_markdown("Intro.\n\n```mermaid\nflowchart TD\n    A[Go] --> B[Stop]\n")

    assert "Intro." in out
    assert "A[Go]" in out
