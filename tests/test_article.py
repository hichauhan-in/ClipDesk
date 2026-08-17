"""Writing the knowledge article, and filling the supplied Word template.

The template is a contract: its sections, wording and styling have to survive
intact, with only the content controls filled in.
"""

import json
import re
import zipfile
from xml.dom import minidom

import pytest

from clipdesk.actions.article import (
    Article,
    _clean_lines,
    _keyword_line,
    render_markdown,
    render_template_docx,
    write_article,
)
from clipdesk.actions.docxtemplate import (
    FIELD_TAGS,
    Block,
    TemplateError,
    read_markdown,
    render_docx,
    template_available,
    template_path,
)

pytestmark = pytest.mark.skipif(
    not template_available(), reason="the Word template is not present"
)


@pytest.fixture
def article():
    return Article(
        title="Outlook will not connect after the January update",
        symptoms=["The status bar reads **Disconnected**.", "Send/Receive returns 0x8004010F."],
        cause=["The profile points at an OST path the update moved."],
        resolution=["Close Outlook.", "Open **Control Panel > Mail**.", "Recreate the profile."],
        more_information=["Only profiles made before build 16.0.17126 are affected."],
        keywords=["outlook", "disconnected", "0x8004010f"],
    )


def document(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def paragraphs(xml):
    out = []
    for block in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        style = re.search(r'<w:pStyle w:val="([^"]+)"', block)
        number = re.search(r'<w:numId w:val="(\d+)"', block)
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", block, re.S))
        out.append((style.group(1) if style else "", number.group(1) if number else "", text))
    return out


# --- the template ------------------------------------------------------------
def test_the_result_is_a_readable_word_file(tmp_path, article):
    path = render_template_docx(tmp_path / "article.docx", article)

    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        minidom.parseString(archive.read("word/document.xml"))
        minidom.parseString(archive.read("docProps/core.xml"))


def test_every_part_of_the_template_is_carried_across(tmp_path, article):
    render_template_docx(tmp_path / "article.docx", article)

    with zipfile.ZipFile(template_path()) as original, zipfile.ZipFile(
        tmp_path / "article.docx"
    ) as written:
        assert set(written.namelist()) == set(original.namelist())


def test_the_templates_own_headings_survive(tmp_path, article):
    render_template_docx(tmp_path / "article.docx", article)
    headings = [
        text for style, _num, text in paragraphs(document(tmp_path / "article.docx"))
        if style == "Heading2"
    ]

    assert headings == [
        "Title(Note: 256 is the maximum length of title)",
        "Symptoms/Summary",
        "Cause (Applies only to Break/Fix help article)",
        "Resolution/Workaround (Applies only to Break/Fix help article)",
        "More Information",
        "Keyword(s) (Note: 256 is the maximum length of keywords)",
    ]


def test_the_instructions_at_the_top_are_left_alone(tmp_path, article):
    render_template_docx(tmp_path / "article.docx", article)

    assert "How to use this template" in document(tmp_path / "article.docx")
    assert "Please remove all PII information." in document(tmp_path / "article.docx")


def test_the_content_lands_in_the_right_fields(tmp_path, article):
    render_template_docx(tmp_path / "article.docx", article)
    texts = [text for _style, _num, text in paragraphs(document(tmp_path / "article.docx"))]

    assert article.title in texts
    assert "Send/Receive returns 0x8004010F." in texts
    assert "The profile points at an OST path the update moved." in texts
    assert "outlook, disconnected, 0x8004010f" in texts


def test_no_prompt_text_is_left_behind(tmp_path, article):
    xml = document(render_template_docx(tmp_path / "article.docx", article))

    # Grey prompt text that Word would wipe on the first click.
    assert "showingPlcHdr" not in xml
    assert "Click or tap here" not in xml


def test_steps_are_numbered_and_notes_are_bulleted(tmp_path, article):
    rows = paragraphs(document(render_template_docx(tmp_path / "article.docx", article)))
    numbers = {number for style, number, text in rows if text == "Close Outlook."}
    bullets = {number for style, number, text in rows if text.startswith("Send/Receive")}

    assert numbers == {"10"}  # the template's decimal list
    assert bullets == {"3"}  # the template's bullet list


def test_bold_survives_as_formatting_rather_than_asterisks(tmp_path, article):
    xml = document(render_template_docx(tmp_path / "article.docx", article))

    assert "**Disconnected**" not in xml
    assert "Disconnected" in xml


def test_a_single_paragraph_section_is_not_bulleted(tmp_path, article):
    rows = paragraphs(document(render_template_docx(tmp_path / "article.docx", article)))
    (row,) = [row for row in rows if row[2].startswith("The profile points")]

    assert row[1] == ""


def test_an_empty_section_keeps_its_blank_form_field(tmp_path):
    article = Article(title="A reference", symptoms=["How it works."], keywords=["ref"])
    rows = paragraphs(document(render_template_docx(tmp_path / "article.docx", article)))

    # Cause was never filled, so its prompt is still there for a human to use.
    assert any("enter the cause of this issue" in text for _s, _n, text in rows)


def test_extra_sections_go_under_more_information(tmp_path, article):
    article.extra = [("Escalation path", ["Attach the ETL to the ticket."])]
    rows = paragraphs(document(render_template_docx(tmp_path / "article.docx", article)))

    assert ("Heading3", "", "Escalation path") in rows
    assert any(text == "Attach the ETL to the ticket." for _s, _n, text in rows)


def test_document_properties_are_set_without_markup(tmp_path, article):
    render_template_docx(tmp_path / "article.docx", article)
    with zipfile.ZipFile(tmp_path / "article.docx") as archive:
        core = archive.read("docProps/core.xml").decode("utf-8")

    assert f"<dc:title>{article.title}</dc:title>" in core
    assert "**" not in core
    assert "<cp:keywords>outlook, disconnected, 0x8004010f</cp:keywords>" in core


def test_a_binding_cannot_overwrite_what_was_written(tmp_path, article):
    # A bound control is refilled from its property when Word opens the file,
    # which would flatten the section back to one line.
    xml = document(render_template_docx(tmp_path / "article.docx", article))

    assert "dataBinding" not in xml


def test_markup_in_the_content_cannot_break_the_document(tmp_path):
    article = Article(
        title="A <script> & an \"issue\"",
        symptoms=["Log shows <error code='5' /> & stops."],
        keywords=["<x>"],
    )
    path = render_template_docx(tmp_path / "article.docx", article)

    with zipfile.ZipFile(path) as archive:
        minidom.parseString(archive.read("word/document.xml"))
    assert "<script>" not in document(path)


def test_an_unknown_field_is_refused(tmp_path):
    with pytest.raises(TemplateError, match="Unknown template field"):
        render_docx(tmp_path / "x.docx", {"CI_Template_Nonsense": [Block("hi")]})


def test_the_known_fields_are_the_ones_in_the_template():
    with zipfile.ZipFile(template_path()) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")

    for tag in FIELD_TAGS:
        assert f'<w:tag w:val="{tag}"/>' in xml


# --- markdown ----------------------------------------------------------------
class FakeReport:
    title = "CCNA Day 30"


def test_markdown_uses_the_same_headings(article):
    text = render_markdown(article, FakeReport())

    assert text.startswith("# Outlook will not connect after the January update")
    assert "## Symptoms/Summary" in text
    assert "## Resolution/Workaround" in text
    assert "## Keywords" in text


def test_markdown_numbers_the_resolution(article):
    text = render_markdown(article, FakeReport())

    assert "1. Close Outlook." in text
    assert "3. Recreate the profile." in text


def test_markdown_leaves_out_a_section_with_nothing_in_it():
    text = render_markdown(Article(title="T", symptoms=["S"]), FakeReport())

    assert "## Cause" not in text
    assert "## Symptoms/Summary" in text


# --- reading it back ---------------------------------------------------------
def test_a_word_article_can_be_read_back_as_markdown(tmp_path, article):
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "## Symptoms/Summary" in text
    assert "## Resolution/Workaround" in text
    assert article.title in text


def test_the_guidance_inside_the_headings_is_left_out(tmp_path, article):
    # "Title(Note: 256 is the maximum length of title)" is advice to the author,
    # not part of the article.
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "## Title" in text
    assert "maximum length" not in text
    assert "Applies only to" not in text


def test_reading_back_keeps_the_lists(tmp_path, article):
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "1. Close Outlook." in text
    assert "3. Recreate the profile." in text
    assert "- Send/Receive returns 0x8004010F." in text


def test_reading_back_restores_bold_as_one_span(tmp_path, article):
    # Word splits a sentence across runs where it was edited, and marking each
    # run separately would produce "**Control** **Panel**".
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "**Control Panel > Mail**" in text
    assert "****" not in text


def test_headings_are_read_back_without_emphasis(tmp_path, article):
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "## **" not in text


def test_the_forms_instructions_are_left_out_of_the_reading(tmp_path, article):
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "How to use this template" not in text
    assert "Please remove all PII information" not in text


def test_an_unfilled_section_is_read_back_as_a_prompt(tmp_path):
    article = Article(title="A reference", symptoms=["How it works."], keywords=["ref"])
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "*Click or tap here to enter the resolution or workaround of this issue.*" in text


def test_escaped_markup_survives_the_round_trip(tmp_path):
    article = Article(title="Fixing <config> & friends", symptoms=["Log shows <err/>."])
    text = read_markdown(render_template_docx(tmp_path / "article.docx", article))

    assert "Fixing <config> & friends" in text
    assert "&amp;" not in text


# --- enrichment and diagrams -------------------------------------------------
class FakeLLM:
    """Captures the prompt and returns whatever payload the test wants."""

    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def complete(self, _system, user, **_kwargs):
        self.prompt = user
        return json.dumps(self.payload)


class FakeSegment:
    def __init__(self, text):
        self.start = 0.0
        self.text = text


class FakeTranscript:
    segments = [FakeSegment("TCP opens with a three-way handshake.")]


class FakeMedia:
    filename = "talk.mp4"


class FakeAnalysis:
    title = "TCP explained"
    abstract = "How TCP sets up a connection."
    summary = ""
    transcript = FakeTranscript()
    media = FakeMedia()


BASE_PAYLOAD = {
    "title": "How TCP connects",
    "symptoms": ["It uses a handshake."],
    "cause": [],
    "resolution": [],
    "more_information": [],
    "keywords": ["tcp"],
}


def test_no_enrichment_asks_the_model_to_stay_inside_the_recording():
    llm = FakeLLM(BASE_PAYLOAD)

    write_article(FakeAnalysis(), llm, enrichment=0)

    assert "How much you may add" not in llm.prompt


def test_enrichment_asks_for_more_and_requires_it_to_be_marked():
    llm = FakeLLM(BASE_PAYLOAD)

    write_article(FakeAnalysis(), llm, enrichment=4)

    assert "How much you may add" in llm.prompt
    assert "**Added context —**" in llm.prompt


def test_specifics_are_never_invented_however_high_the_level():
    llm = FakeLLM(BASE_PAYLOAD)
    write_article(FakeAnalysis(), llm, enrichment=6)

    from clipdesk.analysis.prompts import ARTICLE_SYSTEM

    assert "Never invent product versions" in ARTICLE_SYSTEM


def test_added_context_survives_into_the_markdown():
    llm = FakeLLM(
        {**BASE_PAYLOAD, "more_information": ["**Added context —** TCP dates from 1974."]}
    )
    article = write_article(FakeAnalysis(), llm, enrichment=2)

    assert "**Added context —** TCP dates from 1974." in render_markdown(article, FakeAnalysis())


def test_added_context_survives_into_the_word_document(tmp_path):
    llm = FakeLLM(
        {**BASE_PAYLOAD, "more_information": ["**Added context —** TCP dates from 1974."]}
    )
    article = write_article(FakeAnalysis(), llm, enrichment=2)
    rows = paragraphs(document(render_template_docx(tmp_path / "article.docx", article)))

    assert any("Added context" in text for _s, _n, text in rows)


def test_a_diagram_is_only_asked_for_when_it_can_be_shown():
    llm = FakeLLM(BASE_PAYLOAD)

    write_article(FakeAnalysis(), llm, want_diagram=False)
    assert '"diagram"' not in llm.prompt

    write_article(FakeAnalysis(), llm, want_diagram=True)
    assert '"diagram"' in llm.prompt


def test_a_diagram_is_fenced_into_the_markdown():
    llm = FakeLLM({**BASE_PAYLOAD, "diagram": "flowchart LR\n    A[SYN] --> B[SYN-ACK]"})
    article = write_article(FakeAnalysis(), llm, want_diagram=True)

    text = render_markdown(article, FakeAnalysis())

    assert "```mermaid" in text
    assert "A[SYN] --> B[SYN-ACK]" in text


def test_a_broken_diagram_is_dropped_rather_than_shipped():
    llm = FakeLLM({**BASE_PAYLOAD, "diagram": "Here is a picture of a handshake."})
    article = write_article(FakeAnalysis(), llm, want_diagram=True)

    assert article.diagram == ""
    assert "```mermaid" not in render_markdown(article, FakeAnalysis())


def test_a_diagram_label_is_repaired_on_the_way_in():
    llm = FakeLLM({**BASE_PAYLOAD, "diagram": "flowchart LR\n    A[Open (SYN)] --> B[Ack]"})
    article = write_article(FakeAnalysis(), llm, want_diagram=True)

    assert 'A["Open (SYN)"]' in article.diagram


# --- field handling ----------------------------------------------------------
def test_a_model_that_numbers_its_own_steps_is_tidied_up():
    assert _clean_lines(["1. Open it", "2) Close it", "- Done"], strip_markers=True) == [
        "Open it",
        "Close it",
        "Done",
    ]


def test_a_string_of_lines_is_accepted_as_a_list():
    assert _clean_lines("one\n\ntwo\n") == ["one", "two"]


def test_keywords_are_trimmed_to_the_templates_limit():
    line = _keyword_line([f"keyword-{index:03d}" for index in range(60)])

    assert len(line) <= 256
    assert not line.endswith(",")
