"""Prompts.

They live in one file on purpose. Prompt wording is the main tuning surface of
this app, and having it all in one place makes it obvious what the model is
being asked for and what shape the answer must take.
"""

from __future__ import annotations

ANALYST_SYSTEM = """\
You are a meticulous video analyst working on an internal corporate recording \
(a meeting, training session, design review, demo or briefing).

You will be given part of a timestamped transcript. Each line looks like:
    [42] 12:30 Speaker: what was said

Your job is to classify what is happening and identify what is worth keeping.

Classification of each transcript line, using the `kind` field:
  on_topic   - substantive content: explanation, decision, demo narration, analysis
  qa         - a question and its answer
  off_topic  - unrelated chatter, side conversations, personal asides
  admin      - logistics: "can you hear me", waiting for people, scheduling, recording notices
  filler     - hesitation, repetition, restarts, thinking aloud with no content
  intro      - opening pleasantries and agenda-reading before the real content
  outro      - closing remarks, thanks, sign-off
  transition - moving between topics with no content of its own

Rules you must follow:
- Only refer to segment ids that appear in the transcript you were given.
- Cover the full id range: every id must appear in exactly one entry of `segments`.
- Use contiguous id ranges. Do not emit one entry per line when a run shares a verdict.
- `importance` is 0.0 to 1.0: how much a viewer would lose if this were cut.
- Chapter and clip boundaries must fall on natural sentence starts and ends.
- A `clip` must make sense on its own to someone who did not watch the rest.
- Be honest. If a stretch is genuinely low value, say so; do not inflate scores.
"""

WINDOW_USER_TEMPLATE = """\
Video title: {title}
This is window {window_index} of {window_count}, covering {start} to {end}.
{context}
Transcript:
{transcript}

Return this JSON object:
{{
  "segments": [
    {{
      "from_id": <int>, "to_id": <int>,
      "kind": "on_topic|qa|off_topic|admin|filler|intro|outro|transition",
      "topic": "<short label>",
      "importance": <0.0-1.0>,
      "reason": "<max 12 words>"
    }}
  ],
  "chapters": [
    {{
      "title": "<specific, 3-8 words>",
      "from_id": <int>, "to_id": <int>,
      "summary": "<1-2 sentences>",
      "key_points": ["<point>", "..."],
      "keywords": ["<term>", "..."]
    }}
  ],
  "clips": [
    {{
      "title": "<what this clip covers>",
      "from_id": <int>, "to_id": <int>,
      "score": <0.0-1.0>,
      "hook": "<one line that would make someone watch>",
      "reason": "<why it stands alone>"
    }}
  ],
  "action_items": [{{"text": "<action>", "owner": "<name or null>", "id": <int>}}],
  "decisions": [{{"text": "<decision made>", "id": <int>}}]
}}

Chapters must not overlap. Include 0-4 clips — only genuinely standalone ones. \
Leave `action_items` and `decisions` as empty lists when there are none.
"""

OVERVIEW_SYSTEM = """\
You are summarising an internal corporate video for colleagues who did not attend. \
Be concrete and specific. Name the things that were actually discussed rather than \
describing the video in the abstract. Never invent detail that is not in the input.
"""

OVERVIEW_USER_TEMPLATE = """\
Video title: {title}
Duration: {duration}

Chapters found in the recording:
{chapters}

Return this JSON object:
{{
  "title": "<a better title for this video, 3-10 words>",
  "abstract": "<one sentence, max 30 words, that says what this video is>",
  "summary": "<3-6 sentences covering what was discussed and concluded>",
  "keywords": ["<term>", "... 5-12 items"],
  "audience": "<who should watch this>",
  "topics": ["<major topic>", "... 3-8 items"]
}}
"""

NOTES_SYSTEM = """\
You are writing study notes from a corporate recording, for a colleague who has \
15 minutes and will not watch the video.

Write in GitHub-flavoured Markdown. Rules:
- Lead with what matters. No throat-clearing, no "In this video...".
- Prefer short paragraphs, tables and bullet lists over long prose.
- Keep technical terms, product names, numbers and commands exactly as spoken.
- Never wrap the whole answer in a code fence.
"""

NOTES_USER_TEMPLATE = """\
Video title: {title}
Section: {section_title} ({start} - {end})

Transcript for this section:
{transcript}

Write the notes for this section as Markdown starting at heading level 2 (`## `).

Include, when the content supports it:
- A short lead paragraph saying what this section covers.
- `### Key points` as a bullet list.
- `### Details` with the substance, using sub-headings where there is more than one thread.
- A Markdown table when the content compares options, lists parameters, or maps names to meanings.
{mermaid_hint}
- `### Follow-ups` for anything left open, only if there is something real.

{timestamp_hint}
{enrichment_hint}
{length_hint}
Output Markdown only.
"""

# Not every provider accepts a cap on output length — the VS Code Language Model
# API has no such setting — so the only lever that works everywhere is asking.
LENGTH_HINT = """\
Keep this section to about {words} words. Cover the substance and stop; do not \
pad it out to look thorough.\
"""

MERMAID_HINT = """\
- A ```mermaid fenced diagram when the section describes a process, a sequence of \
steps, a decision, an architecture, or how components relate. Prefer one where \
the relationship is hard to hold in your head from prose alone.

  Choose the form that fits: `flowchart TD` for a process or decision, \
`flowchart LR` for a pipeline or data flow, `sequenceDiagram` for an exchange \
between parties, `stateDiagram-v2` for lifecycles, `erDiagram` for data models.

  The diagram must show what the recording actually described — the same steps, \
the same order, the same names — not a generic textbook version of the topic. \
Every node must be something the speaker referred to.

  Syntax that survives rendering: wrap any label containing brackets, commas, \
quotes or colons in double quotes, as in `A["Retry (max 3)"]`. Keep labels under \
six words. Never use `end` as a node name. No Markdown inside labels.

  Only include a diagram when it genuinely helps; never force one.\
"""

TIMESTAMP_HINT = """\
Reference times as `[mm:ss]` at the end of the bullet or sentence they belong to, \
so a reader can jump back to the recording.\
"""

# --- notes enrichment --------------------------------------------------------
# Anything the model adds beyond the recording must be visibly separated from it.
# These notes get shared internally, and a reader who cannot tell "what was said"
# from "what the model filled in" will eventually attribute an invention to a
# colleague. The marker below is the mechanism that prevents that.
ENRICHMENT_MARKER = (
    "Anything you add that was NOT said in the recording must go inside a blockquote "
    'that starts with `> **Added context —** `. Never blend added material into a '
    "sentence describing what the speaker said. If you are unsure whether something "
    "was in the recording, treat it as added."
)

ENRICHMENT_LEVELS: dict[int, str] = {
    0: "Use only what is in the transcript. Do not add facts, definitions or "
    "background that the speaker did not provide.",
    1: f"""\
You may clarify terms the speaker used but did not define: expand acronyms, give \
a one-line definition of jargon, and name the product or standard being referred \
to obliquely. Do not introduce topics the speaker never mentioned.

{ENRICHMENT_MARKER}\
""",
    2: f"""\
Add brief background so a colleague new to this area can follow the section: what \
a mentioned technology is for, why an approach is normally chosen, and any \
prerequisite concept the speaker assumed. Keep additions to a short paragraph or \
a few bullets per section, and stay tightly relevant to what was discussed.

{ENRICHMENT_MARKER}\
""",
    3: f"""\
  Connect the concepts into a coherent technical explanation. Add how the mentioned \
  components relate, the normal data or control flow, why the approach works, and the \
  most important trade-offs. Include a compact architecture or sequence explanation \
  when relevant. Stay on the technologies and problem actually discussed.

  {ENRICHMENT_MARKER}\
  """,
    4: f"""\
  Turn the section into a practical implementation guide as well as a record of what \
  was said. Add prerequisites, a concrete step-by-step approach, representative \
  configuration or commands, ways to verify the result, and common troubleshooting \
  checks. Explain what each example demonstrates rather than dumping unexplained code. \
  Use dedicated Added context subsections where that makes the source boundary clearer.

  Do not invent organisation-specific values, credentials, endpoints or commands. Use \
  clearly generic placeholders where the recording did not provide them.

  {ENRICHMENT_MARKER}\
  """,
    5: f"""\
  Write a technical deep dive for an experienced practitioner. In addition to the \
  recording, cover internal mechanics, architecture boundaries, important defaults, \
  alternatives and decision criteria, security and reliability implications, \
  performance or scaling considerations, observable failure modes, and a systematic \
  troubleshooting path. Include concrete examples, commands, tables or diagrams when \
  they materially improve understanding. Make the added material substantial rather \
  than a single short paragraph, while staying tightly focused on the section topic.

  Never fabricate uncertain version numbers, benchmarks, quotations, APIs or internal \
  system details. State uncertainty or omit the detail when it cannot be supported.

  {ENRICHMENT_MARKER}\
  """,
    6: f"""\
  Produce a comprehensive expert reference and study guide on the exact technology or \
  problem discussed. Preserve what the recording said, then thoroughly supply the \
  missing conceptual model and practical knowledge a reader would need to design, \
  implement, operate and troubleshoot it independently.

  When relevant, organize Added context into several of these substantive subsections:
  - Architecture and component responsibilities.
  - End-to-end data/control flow and underlying mechanics.
  - Implementation procedure with representative configuration, commands or examples.
  - Alternatives, trade-offs and decision guidance.
  - Security, reliability, performance and scaling concerns.
  - Failure modes, diagnostics, verification and troubleshooting.
  - A concise checklist or mental model for future use.

  Depth matters more than brevity at this level. Explain why, not only what, and connect \
  details into a usable model. Stay within the precise topic of the recording and do \
  not pad the notes with generic adjacent material.

  Never fabricate uncertain version numbers, benchmarks, quotations, APIs or internal \
  system details. Use generic placeholders, state uncertainty, or omit unsupported \
  details. Added context is background to verify, not a claim about what the speaker said.

  {ENRICHMENT_MARKER}\
  """,
}

ENRICHMENT_LABELS: dict[int, str] = {
    0: "Transcript only",
    1: "Clarify terms",
    2: "Add background",
    3: "Connect concepts",
    4: "Practical guide",
    5: "Technical deep dive",
    6: "Expert reference",
}

# --- knowledge article -------------------------------------------------------
ARTICLE_SYSTEM = """\
You are writing a support knowledge-base article from a recording, for an engineer \
who will read it while a customer waits.

Rules:
- Never invent product versions, error codes, registry paths, URLs, commands or \
figures that were not said. Where the recording did not give a value, use an \
obvious placeholder instead of a plausible-looking invention. This holds however \
much you are allowed to add.
- Unless a section below says otherwise, every statement must come from the \
transcript.
- Write in plain, direct English. No marketing tone, no "In this video".
- Keep product names, numbers, commands and error text exactly as spoken.
- Do not include names of people, email addresses, machine names, ticket numbers \
or any other personal or customer-identifying detail.
- Return JSON only, with no code fence and no commentary.
"""

ARTICLE_USER_TEMPLATE = """\
Recording title: {title}
Article type: {shape_hint}
{audience_hint}
What the recording covers:
{summary}

Transcript:
{transcript}

Return a single JSON object with exactly these keys:

{{
  "title": "one line, under 256 characters, states the problem or the task",
  "symptoms": ["paragraph or bullet strings describing what the reader observes"],
  "cause": ["why it happens; empty list if the recording never says"],
  "resolution": ["ordered steps that fix it; each string is one step"],
  "more_information": ["context, caveats, links mentioned aloud; may be empty"],
  "keywords": ["8-14 short search terms, lower case"]
}}
{diagram_hint}{extra_hint}{enrichment_hint}
Guidance for this article type: {shape_guidance}

Write a step as a single instruction. Do not number the steps yourself; they are \
numbered when the document is built. Use **bold** only to mark a literal UI label \
or command. Return JSON only.
"""

ARTICLE_DIAGRAM_HINT = """\
Also add a "diagram" key: one Mermaid diagram of what the article describes, as a \
single string with no code fence, or an empty string when a picture would not \
help. {mermaid}
"""

# The article's fields are JSON strings, so the blockquote convention the notes
# use is not available. A literal prefix carries the same warning and survives
# into both Markdown and Word.
ARTICLE_ENRICHMENT_MARKER = (
    "Anything you add that was NOT said in the recording must be its own list item "
    'beginning exactly `**Added context —** `. Never blend added material into an '
    "item describing what the recording said. If you are unsure whether something "
    "was in the recording, treat it as added."
)

ARTICLE_ENRICHMENT_HINT = """\
How much you may add beyond the recording: {level}

{marker}
"""

ARTICLE_EXTRA_HINT = """\
Also add these keys, one per requested section, each a list of strings drawn from \
the recording. Leave a list empty rather than inventing material for it:
{keys}
"""

ARTICLE_SHAPE_GUIDANCE: dict[str, str] = {
    "break-fix": "Something is broken. Fill symptoms, cause and resolution. The "
    "resolution must be steps the reader can follow to the end.",
    "how-to": "Nothing is broken. Put what the task achieves in symptoms, leave "
    "cause empty, and put the procedure in resolution.",
    "reference": "Explain how something works. Put the overview in symptoms, the "
    "mechanism in cause, leave resolution empty unless the recording gives a "
    "procedure, and put the detail in more_information.",
}

# --- clip discovery ----------------------------------------------------------
CLIP_FIND_SYSTEM = """\
You are choosing which parts of a recording are worth cutting into standalone clips.

A good clip starts at the beginning of a thought and ends after it is resolved. It \
must make sense to someone who did not watch the rest of the recording. Never start \
or end mid-sentence.

You will be given a timestamped transcript where each line looks like:
    [42] 12:30 Speaker: what was said

Refer to positions only by the segment ids shown. Be honest — if only two parts of \
the recording are genuinely worth clipping, return two.
"""

CLIP_DURATION_USER = """\
Video: {title}
{request_line}
Preferred length: about {target_seconds:.0f} seconds. This is a soft target — going \
{tolerance_seconds:.0f} seconds over or under is fine if it means the clip starts and \
ends on a clean thought. A coherent clip matters more than an exact runtime.

Transcript:
{transcript}

Return this JSON object:
{{
  "candidates": [
    {{
      "title": "<specific, 4-9 words>",
      "summary": "<exactly two sentences describing what this clip covers>",
      "from_id": <int>, "to_id": <int>,
      "score": <0.0-1.0>,
      "reason": "<max 12 words on why this is worth clipping>"
    }}
  ]
}}

Return up to {max_candidates} candidates, best first. They must not overlap.
"""

CLIP_TOPIC_USER = """\
Video: {title}
The user is looking for: {request}

Find every part of the transcript where that topic is actually discussed. It may \
appear in one continuous stretch or in several separate places — return each one.
Ignore passing mentions; include a section only when the topic is genuinely being \
explained or worked through.

Transcript:
{transcript}

Return this JSON object:
{{
  "matches": [
    {{
      "title": "<what this section covers, 4-9 words>",
      "summary": "<exactly two sentences describing what is said here>",
      "from_id": <int>, "to_id": <int>,
      "relevance": <0.0-1.0>,
      "reason": "<max 12 words on how this matches the request>"
    }}
  ],
  "found": <true or false>,
  "note": "<if found is false, one sentence on what the recording covers instead>"
}}

Order the matches by where they appear in the recording, earliest first. If the \
topic is genuinely not discussed, set "found" to false and return an empty list \
rather than offering something loosely related.
"""

HIGHLIGHT_FIND_USER = """\
Video: {title}
Duration: {duration}
{request_line}
Find the moments most worth sharing on their own — the parts someone would send to \
a colleague who missed the session. Each must be self-contained and no longer than \
{max_seconds:.0f} seconds.

Transcript:
{transcript}

Return this JSON object:
{{
  "candidates": [
    {{
      "title": "<specific, 4-9 words>",
      "summary": "<exactly two sentences describing what this covers>",
      "from_id": <int>, "to_id": <int>,
      "score": <0.0-1.0>,
      "hook": "<one line that would make someone watch>",
      "reason": "<max 12 words>"
    }}
  ]
}}

Return at most {max_candidates}, best first, without overlaps. Return fewer if the \
recording genuinely does not contain that many standalone moments — do not pad the \
list with weak entries.
"""

