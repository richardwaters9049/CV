#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    name: str
    url: str
    description_paragraphs: list[str]
    labeled_lists: list[tuple[str, list[str]]]
    labeled_lines: list[tuple[str, str]]
    trailing_paragraphs: list[str]


@dataclass(frozen=True)
class ExperienceEntry:
    title: str
    company: str
    location: str
    dates: str
    bullets: list[str]


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def strip_md_line_breaks(text: str) -> str:
    return re.sub(r"\s{2,}$", "", text)


def strip_inline_markdown(text: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"\1", text)


def escape_typst_markup(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("#", "\\#")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def typst_str(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f"\"{escaped}\""


def parse_markdown(md: str) -> dict:
    lines = [strip_md_line_breaks(line.rstrip("\n")) for line in md.splitlines()]

    def find_line_index(pattern: str) -> int:
        for index, line in enumerate(lines):
            if re.fullmatch(pattern, line):
                return index
        return -1

    name_index = find_line_index(r"#\s+.+")
    if name_index == -1:
        die("missing top-level '# Name' heading")
    full_name = lines[name_index].removeprefix("#").strip()

    def next_non_empty(start: int) -> tuple[int, str]:
        for index in range(start, len(lines)):
            if lines[index].strip():
                return index, lines[index]
        die("unexpected end of file")

    tagline_index, tagline = next_non_empty(name_index + 1)

    contact: dict[str, str] = {}
    i = tagline_index + 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i].strip()
        if line.startswith("Email:"):
            contact["email"] = line.split(":", 1)[1].strip()
        elif line.startswith("GitHub:"):
            contact["github"] = line.split(":", 1)[1].strip()
        elif line:
            contact["location"] = line
        i += 1
    if "location" not in contact or "email" not in contact or "github" not in contact:
        die("contact block must include Location, Email:, and GitHub:")

    def section_block(*titles: str, required: bool = True) -> list[str]:
        start = -1
        matched_title = None
        for title in titles:
            start = find_line_index(rf"##\s+{re.escape(title)}")
            if start != -1:
                matched_title = title
                break
        if start == -1:
            if required:
                die(f"missing section '## {titles[0]}'")
            return []
        start += 1
        block: list[str] = []
        index = start
        while index < len(lines):
            line = lines[index]
            if re.match(r"##\s+", line):
                break
            if line.strip() == "---":
                index += 1
                continue
            block.append(line)
            index += 1
        return block

    about_lines = section_block("About Me")

    def parse_paragraphs(block: list[str]) -> list[str]:
        paragraphs: list[str] = []
        buffer: list[str] = []
        for raw in block + [""]:
            line = raw.strip()
            if not line:
                if buffer:
                    paragraphs.append(" ".join(buffer).strip())
                    buffer = []
                continue
            buffer.append(line)
        return [p for p in paragraphs if p]

    def parse_about(block: list[str]) -> dict:
        first_list_idx = next((i for i, l in enumerate(block) if l.strip().startswith("- ")), -1)
        if first_list_idx == -1:
            return {
                "pre_paragraphs": parse_paragraphs(block),
                "list_heading": None,
                "list_items": [],
                "post_paragraphs": [],
            }

        pre = block[:first_list_idx]
        list_heading = None
        for i in range(len(pre) - 1, -1, -1):
            candidate = pre[i].strip()
            if not candidate:
                continue
            if candidate.endswith(":"):
                list_heading = candidate
                pre = pre[:i] + pre[i + 1 :]
            break

        list_end = first_list_idx
        while list_end < len(block) and (block[list_end].strip().startswith("- ") or not block[list_end].strip()):
            list_end += 1

        list_items = [l.strip()[2:].strip() for l in block[first_list_idx:list_end] if l.strip().startswith("- ")]
        post = block[list_end:]

        return {
            "pre_paragraphs": parse_paragraphs(pre),
            "list_heading": list_heading,
            "list_items": [li for li in list_items if li],
            "post_paragraphs": parse_paragraphs(post),
        }

    about = parse_about(about_lines)

    project_lines = section_block("Key Projects")

    def split_projects(block: list[str]) -> list[list[str]]:
        projects: list[list[str]] = []
        current: list[str] = []
        for line in block:
            if re.match(r"###\s+", line):
                if current:
                    projects.append(current)
                current = [line]
                continue
            if current:
                current.append(line)
        if current:
            projects.append(current)
        return projects

    raw_projects = split_projects(project_lines)
    if not raw_projects:
        die("no projects found under '## Key Projects'")

    projects: list[Project] = []
    for raw in raw_projects:
        header = raw[0]
        project_name = header.removeprefix("###").strip()
        # Skip empty lines, then URL, then the rest.
        cursor = 1
        while cursor < len(raw) and not raw[cursor].strip():
            cursor += 1
        if cursor >= len(raw):
            die(f"project '{project_name}' missing URL line")
        url = raw[cursor].strip()
        cursor += 1

        description_paragraphs: list[str] = []
        labeled_lists: list[tuple[str, list[str]]] = []
        labeled_lines: list[tuple[str, str]] = []
        trailing_paragraphs: list[str] = []

        current_paragraph: list[str] = []
        current_label: str | None = None
        current_list: list[str] | None = None

        def flush_paragraph(into_trailing: bool = False) -> None:
            nonlocal current_paragraph
            if current_paragraph:
                text = " ".join(x.strip() for x in current_paragraph if x.strip())
                if text:
                    (trailing_paragraphs if into_trailing else description_paragraphs).append(
                        text
                    )
                current_paragraph = []

        def flush_label() -> None:
            nonlocal current_label, current_list
            if current_label and current_list:
                labeled_lists.append((current_label, [x for x in current_list if x]))
            current_label = None
            current_list = None

        while cursor < len(raw):
            line = raw[cursor].rstrip()
            cursor += 1
            if line.strip() == "---":
                continue
            if not line.strip():
                if current_label and current_list is None:
                    current_label = None
                flush_paragraph(into_trailing=bool(labeled_lists or labeled_lines))
                # Allow a blank line between a label and its value/list.
                if current_label and current_list == []:
                    continue
                flush_label()
                continue

            if re.fullmatch(r".+:\s*", line.strip()):
                flush_paragraph(into_trailing=bool(labeled_lists or labeled_lines))
                flush_label()
                current_label = line.strip().removesuffix(":").strip()
                current_list = []
                continue

            if line.lstrip().startswith("- ") and current_label and current_list is not None:
                current_list.append(line.strip()[2:].strip())
                continue

            if current_label and current_list is not None and not line.lstrip().startswith("- "):
                # Either a label's single-line value, or text after a completed list.
                if current_list == []:
                    labeled_lines.append((current_label, line.strip()))
                    current_label = None
                    current_list = None
                    continue
                flush_label()
                current_paragraph.append(line.strip())
                continue

            current_paragraph.append(line.strip())

        flush_paragraph(into_trailing=bool(labeled_lists or labeled_lines))
        flush_label()

        projects.append(
            Project(
                name=project_name,
                url=url,
                description_paragraphs=[p for p in description_paragraphs if p],
                labeled_lists=labeled_lists,
                labeled_lines=labeled_lines,
                trailing_paragraphs=[p for p in trailing_paragraphs if p],
            )
        )

    skills: list[tuple[str, list[str]]] = []
    tech_lines = section_block("Technical Skills", required=False)
    table_rows = [line for line in tech_lines if line.strip().startswith("|")]
    if table_rows:
        data_rows = table_rows[2:]
        for row in data_rows:
            cols = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cols) < 2:
                continue
            area, items = strip_inline_markdown(cols[0]), strip_inline_markdown(cols[1])
            if area and items:
                skills.append((area, [item.strip() for item in items.split(";") if item.strip()]))
    elif tech_lines:
        current_area: str | None = None
        current_items: list[str] = []

        def flush_skill() -> None:
            nonlocal current_area, current_items
            if current_area and current_items:
                skills.append((current_area, current_items[:]))
            current_area = None
            current_items = []

        for raw in tech_lines:
            line = raw.strip()
            if not line:
                continue
            bold_heading = re.fullmatch(r"\*\*(.+?)\*\*", line)
            if bold_heading:
                flush_skill()
                current_area = strip_inline_markdown(bold_heading.group(1).strip())
                continue
            if line.startswith("* "):
                current_items.append(strip_inline_markdown(line[2:].strip()))
                continue
        flush_skill()

    exp_lines = section_block("Professional Experience", "Work Experience")
    experience: list[ExperienceEntry] = []
    if any(re.match(r"###\s+", line) for line in exp_lines):
        current: list[str] = []
        raw_entries: list[list[str]] = []
        for line in exp_lines:
            if re.match(r"###\s+", line):
                if current:
                    raw_entries.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            raw_entries.append(current)

        for raw in raw_entries:
            header = raw[0].removeprefix("###").strip()
            parts = [part.strip() for part in header.split("|")]
            if len(parts) < 4:
                die(f"experience entry header must have 4 parts: {header}")
            title, company, location, dates = parts[:4]
            bullets = [line.strip()[2:].strip() for line in raw[1:] if line.strip().startswith("- ")]
            experience.append(
                ExperienceEntry(
                    title=title,
                    company=company,
                    location=location,
                    dates=dates,
                    bullets=bullets,
                )
            )
    else:
        exp_compact = [l for l in exp_lines if l.strip()]
        idx = 0
        while idx + 1 < len(exp_compact):
            experience.append(
                ExperienceEntry(
                    title=exp_compact[idx].strip(),
                    company=exp_compact[idx + 1].strip(),
                    location="",
                    dates="",
                    bullets=[],
                )
            )
            idx += 2
    if not experience:
        die("no experience entries parsed")

    edu_lines = section_block("Qualifications", "Education", "Education & Certifications")
    edu_compact = [l for l in edu_lines if l.strip()]
    if len(edu_compact) < 3:
        die("Qualifications section must include degree line, honours line, and institution line")
    qualifications = {
        "degree": edu_compact[0],
        "honours": edu_compact[1],
        "institution": edu_compact[2],
    }

    return {
        "name": full_name,
        "tagline": tagline.strip(),
        "contact": contact,
        "about": about,
        "projects": projects,
        "skills": skills,
        "experience": experience,
        "qualifications": qualifications,
    }


def render_typst_cards(doc: dict) -> str:
    def para(text: str) -> str:
        return escape_typst_markup(text)

    out: list[str] = []
    out.append(
        """
#set page(
  paper: "a4",
  margin: (top: 12mm, bottom: 12mm, left: 16mm, right: 16mm),
  fill: rgb("#0f141a"),
)

#let accent = rgb("#6cb6ff")
#let muted = rgb("#cdd3da")
#let rule-col = rgb("#27303a")
#let font-body = ("Avenir Next", "Helvetica Neue", "Helvetica")
#let font-display = ("Avenir Next", "Helvetica Neue", "Helvetica")

#set text(font: font-body, size: 10.9pt, fill: rgb("#eef1f4"))
#set par(justify: false, leading: 1.36em)
#set list(indent: 14pt, body-indent: 6pt, spacing: 2pt)

#let hr() = rect(height: 1pt, fill: rule-col)

#let section(title) = [
  #v(6pt)
  #block(
    fill: rgb("#171d25"),
    inset: (x: 10pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 13.8pt, weight: "bold")[#title]
  ]
  #v(6pt)
]

#let section-compact(title) = [
  #block(
    fill: rgb("#171d25"),
    inset: (x: 7pt, y: 2pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 10.8pt, weight: "bold")[#title]
  ]
  #v(0pt)
]

#let label(name) = text(fill: muted, weight: "bold")[#name]

#let project(name, url, body) = block(breakable: false)[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 8pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 12.7pt, weight: "bold")[#name]
    #v(1.5pt)
    #link(url)[#text(size: 9.6pt, fill: accent)[#url]]
    #v(3.5pt)
    #body
  ]
  #v(3pt)
]

#let experience-card(title, company, meta, body) = block[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 8pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 11.5pt, weight: "bold")[#title]
    #h(7pt)
    #text(size: 9.4pt, fill: accent, weight: "bold")[#company]
    #v(1.5pt)
    #text(size: 8.8pt, fill: muted)[#meta]
    #v(4.5pt)
    #set text(size: 9.2pt)
    #body
  ]
  #v(5pt)
]

#let experience-bullet(content) = grid(
  columns: (9pt, 1fr),
  gutter: 5pt,
  [#text(size: 8.8pt, fill: muted)[•]],
  [#block(width: 100%)[
    #set par(leading: 0.67em)
    #text(size: 9.2pt)[#content]
  ]],
)

#let project-summary(content) = block(width: 100%)[
  #set par(leading: 0.72em)
  #text(size: 10.1pt)[#content]
]

#let project-row(name, content) = block(width: 100%)[
  #set par(leading: 0.72em)
  #text(size: 9.8pt, fill: muted, weight: "bold")[#name]
  #h(3pt)
  #text(size: 9.8pt)[#content]
]

#let skill-card(title, body) = block(breakable: false)[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 7pt, y: 5pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 10.2pt, weight: "bold")[#title]
    #v(3pt)
    #body
  ]
  #v(4pt)
]

#let skill-bullet(content) = grid(
  columns: (7pt, 1fr),
  gutter: 3.5pt,
  [#text(size: 8pt, fill: muted)[•]],
  [#block(width: 100%)[
    #set par(leading: 0.62em)
    #text(size: 8.7pt)[#content]
  ]],
)
""".strip(
            "\n"
        )
    )

    out.append(f'#text(font: font-display, size: 30pt, weight: "bold")[{para(doc["name"])}]')
    out.append("#v(3pt)")
    out.append(f'#text(size: 11.7pt, fill: muted)[{para(doc["tagline"])}]')
    out.append("#v(7pt)")
    contact = doc["contact"]
    out.append(
        "#text(size: 10pt, fill: muted)["
        + f'{para(contact["location"])}  •  Email: '
        + f'#link({typst_str("mailto:" + contact["email"])})[#text({typst_str(contact["email"])})]'
        + "  •  GitHub: "
        + f'#link({typst_str(contact["github"])})[#text({typst_str(contact["github"].removeprefix("https://").removeprefix("http://"))})]'
        + "]"
    )
    out.append("#v(8pt)")
    out.append("#hr()")
    out.append("#v(7pt)")

    out.append('#section("About Me")')
    about = doc["about"]
    for paragraph in about["pre_paragraphs"]:
        out.append(para(paragraph))
        out.append("")

    if about["list_items"]:
        if about["list_heading"]:
            out.append(para(about["list_heading"]))
        else:
            out.append("My work focuses on:")
        out.append("#v(3pt)")
        out.append("#set list(spacing: 8pt)")
        for item in about["list_items"]:
            out.append(f"- {para(item)}")
        out.append("#set list(spacing: 2pt)")
        out.append("")

    for paragraph in about["post_paragraphs"]:
        out.append(para(paragraph))
        out.append("")

    experience_entries = list(doc["experience"])
    if experience_entries:
        first_entry = experience_entries[0]
        body: list[str] = []
        if first_entry.bullets:
            body.append("#stack(spacing: 3pt)[")
            for bullet in first_entry.bullets:
                body.append(f"  #experience-bullet[{para(bullet)}]")
            body.append("]")
        else:
            body.append("")

        meta_parts = [part for part in [first_entry.location, first_entry.dates] if part]
        meta = " | ".join(meta_parts) if meta_parts else ""
        out.append('#section("Professional Experience")')
        out.append(
            f"#experience-card({typst_str(first_entry.title)}, {typst_str(first_entry.company)}, {typst_str(meta)})[\n  "
            + "\n  ".join(body)
            + "\n]"
        )

    if len(experience_entries) > 1:
        out.append("#pagebreak()")

    for entry in experience_entries[1:]:
        body = []
        if entry.bullets:
            body.append("#stack(spacing: 3pt)[")
            for bullet in entry.bullets:
                body.append(f"  #experience-bullet[{para(bullet)}]")
            body.append("]")
        else:
            body.append("")

        meta_parts = [part for part in [entry.location, entry.dates] if part]
        meta = " | ".join(meta_parts) if meta_parts else ""
        out.append(
            f"#experience-card({typst_str(entry.title)}, {typst_str(entry.company)}, {typst_str(meta)})[\n  "
            + "\n  ".join(body)
            + "\n]"
        )

    if doc["skills"]:
        out.append("#pagebreak()")
        out.append('#section("Technical Skills")')
        out.append("#columns(2, gutter: 8pt)[")
        for area, items in doc["skills"]:
            body = ["#stack(spacing: 3pt)["]
            for item in items:
                body.append(f"  #skill-bullet[{para(item)}]")
            body.append("]")
            out.append(
                f"#skill-card({typst_str(area)})[\n  "
                + "\n  ".join(body)
                + "\n]"
            )
        out.append("]")

    out.append('#section("Key Projects")')
    for proj in doc["projects"]:
        body: list[str] = []
        for paragraph in proj.description_paragraphs:
            body.append(f"#project-summary[{para(paragraph)}]")
            body.append("#v(3pt)")
        for label_name, items in proj.labeled_lists:
            joined = "; ".join(para(item) for item in items if item)
            body.append(f'#project-row({typst_str(label_name + ":")}, [{joined}])')
            body.append("#v(3pt)")
        for label_name, value in proj.labeled_lines:
            body.append(f'#project-row({typst_str(label_name + ":")}, [{para(value)}])')
            body.append("#v(3pt)")
        for paragraph in proj.trailing_paragraphs:
            body.append(f"#project-summary[{para(paragraph)}]")
            body.append("#v(3pt)")

        while body and body[-1].strip() in {"#v(3pt)", "#v(4pt)", "#v(5pt)", "#v(6pt)", ""}:
            body.pop()

        out.append(
            f"#project({typst_str(proj.name)}, {typst_str(proj.url)})[\n  "
            + "\n  ".join(body)
            + "\n]"
        )
    out.append('#section-compact("Qualifications")')
    qualifications = doc["qualifications"]
    out.append(
        f'#text(size: 9.9pt, weight: "bold")[{para(qualifications["degree"])}]'
        + " #h(4pt) "
        + f'#text(size: 9.5pt, fill: accent, weight: "bold")[{para(qualifications["honours"])}]'
        + " #h(4pt) "
        + f'#text(size: 9.1pt, fill: muted)[{para(qualifications["institution"])}]'
    )

    out.append("")
    return "\n".join(out)


def render_typst_bruyerre(doc: dict) -> str:
    def para(text: str) -> str:
        return escape_typst_markup(text)

    out: list[str] = []
    out.append(
        """
#set page(
  paper: "a4",
  margin: (top: 14mm, bottom: 14mm, left: 15mm, right: 15mm),
  fill: rgb("#071a3a"),
)

#let accent = rgb("#6cb6ff")
#let text-col = rgb("#f5f8ff")
#let muted = rgb("#c3cde3")
#let rule-col = rgb("#1a346b")
#let sidebar = rgb("#0b2556")
#let panel = rgb("#0a214c")

#let font-body = ("Bitter", "Avenir Next", "Helvetica Neue", "Helvetica")
#let font-name = ("SignPainter-HouseScript", "Baskerville", "Bitter", "Avenir Next")

#set text(font: font-body, size: 10.5pt, fill: text-col)
#set par(justify: false, leading: 1.3em)
#set list(indent: 14pt, body-indent: 6pt, spacing: 4pt)

#let hr() = rect(height: 1pt, fill: rule-col)

#let heading(title) = [
  #text(size: 9.5pt, weight: "bold", fill: muted)[#upper(title)]
  #v(3pt)
  #hr()
  #v(8pt)
]

#let project(name, url, body) = block(breakable: false)[
  #block(
    fill: panel,
    radius: 10pt,
    inset: (x: 10pt, y: 8pt),
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(size: 12pt, weight: "bold")[#name]
    #v(2pt)
    #link(url)[#text(size: 10pt, fill: accent)[#url]]
    #v(6pt)
    #body
  ]
  #v(10pt)
]
""".strip("\n")
    )

    about = doc["about"]
    intro = about["pre_paragraphs"][0] if about["pre_paragraphs"] else ""

    out.append("#grid(")
    out.append("  columns: (31%, 69%),")
    out.append("  gutter: 20pt,")

    out.append("  [")
    out.append("    #block(fill: sidebar, radius: 14pt, inset: (x: 12pt, y: 12pt))[")
    out.append(
        '      #block(fill: accent, radius: 10pt, inset: 8pt)[#text(fill: rgb("#06102a"), weight: "bold")[RW]]'
    )
    out.append("      #v(14pt)")
    out.append("      " + '#heading("Contact")')
    contact = doc["contact"]
    out.append("      #stack(")
    out.append("        spacing: 6pt,")
    out.append(
        f"        [#text(weight: \"bold\")[Location] #h(6pt) {para(contact['location'])}],"
    )
    out.append(
        "        [#text(weight: \"bold\")[Email] #h(6pt) "
        + f"#link({typst_str('mailto:' + contact['email'])})[#raw({typst_str(contact['email'])})]"
        + "],"
    )
    out.append(
        "        [#text(weight: \"bold\")[GitHub] #h(6pt) "
        + f"#link({typst_str(contact['github'])})[{para(contact['github'].removeprefix('https://').removeprefix('http://'))}]"
        + "],"
    )
    out.append("      )")
    out.append("      #v(16pt)")
    out.append('      #heading("Technical Skills")')
    out.append("      #stack(")
    out.append("        spacing: 10pt,")
    for area, items in doc["skills"]:
        joined_items = " • ".join(para(item) for item in items)
        out.append(
            "        [#text(weight: \"bold\")["
            + para(area)
            + "]"
            + " #linebreak() "
            + f"#text(size: 9.5pt, fill: muted)[{joined_items}]],"
        )
    out.append("      )")
    out.append("    ]")
    out.append("  ],")

    out.append("  [")
    out.append(
        f'    #text(font: font-name, size: 34pt, fill: text-col)[{para(doc["name"])}]'
    )
    out.append("#v(3pt)")
    out.append(f'    #text(size: 11pt, fill: muted)[{para(doc["tagline"])}]')
    out.append("#v(9pt)")
    if intro:
        out.append(f"    {para(intro)}")
        out.append("#v(10pt)")

    out.append('    #heading("About Me")')
    for paragraph in about["pre_paragraphs"][1:]:
        out.append(f"    {para(paragraph)}")
        out.append("")
    if about["list_items"]:
        out.append(
            "    "
            + (para(about["list_heading"]) if about["list_heading"] else "My work focuses on:")
        )
        out.append("    #v(3pt)")
        out.append("    #set list(spacing: 7pt)")
        for item in about["list_items"]:
            out.append(f"    - {para(item)}")
        out.append("    #set list(spacing: 4pt)")
        out.append("")
    for paragraph in about["post_paragraphs"]:
        out.append(f"    {para(paragraph)}")
        out.append("")

    projects = list(doc["projects"])
    if projects:
        first_proj = projects[0]
        out.append("    #block(breakable: false)[")
        out.append('      #heading("Key Projects")')

        body: list[str] = []
        for paragraph in first_proj.description_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")
        for label_name, items in first_proj.labeled_lists:
            body.append(f"*{para(label_name)}:*")
            for item in items:
                body.append(f"- {para(item)}")
            body.append("#v(4pt)")
        for label_name, value in first_proj.labeled_lines:
            body.append(f"*{para(label_name)}:* {para(value)}")
            body.append("#v(4pt)")
        for paragraph in first_proj.trailing_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")
        while body and body[-1].strip() in {"#v(4pt)", ""}:
            body.pop()

        out.append(
            f"      #project({typst_str(first_proj.name)}, {typst_str(first_proj.url)})[\n        "
            + "\n        ".join(body)
            + "\n      ]"
        )
        out.append("    ]")

    for proj in projects[1:]:
        body: list[str] = []
        for paragraph in proj.description_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")
        for label_name, items in proj.labeled_lists:
            body.append(f"*{para(label_name)}:*")
            for item in items:
                body.append(f"- {para(item)}")
            body.append("#v(4pt)")
        for label_name, value in proj.labeled_lines:
            body.append(f"*{para(label_name)}:* {para(value)}")
            body.append("#v(4pt)")
        for paragraph in proj.trailing_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")
        while body and body[-1].strip() in {"#v(4pt)", ""}:
            body.pop()
        out.append(
            f"    #project({typst_str(proj.name)}, {typst_str(proj.url)})[\n      "
            + "\n      ".join(body)
            + "\n    ]"
        )

    out.append('    #heading("Professional Experience")')
    out.append("    #grid(")
    out.append("      columns: (1fr, 1fr),")
    out.append("      gutter: 14pt,")
    for entry in doc["experience"]:
        out.append(
            "      ["
            + "#rect(width: 5pt, height: 5pt, radius: 99pt, fill: accent)"
            + " #h(7pt) "
            + f'#text(weight: "bold")[{para(entry.title)}]'
            + " #h(6pt) "
            + f"#text(fill: muted)[{para(entry.company)}]"
            + "],"
        )
    out.append("    )")
    out.append("")

    out.append('    #heading("Qualifications")')
    edu = doc["qualifications"]
    out.append(f"    *{para(edu['degree'])}* — {para(edu['honours'])}")
    out.append(f"    {para(edu['institution'])}")

    out.append("  ],")
    out.append(")")

    out.append("")
    return "\n".join(out)


def render_typst_cyber(doc: dict) -> str:
    def para(text: str) -> str:
        return escape_typst_markup(text)

    out: list[str] = []
    out.append(
        """
#set page(
  paper: "a4",
  margin: (top: 10mm, bottom: 10mm, left: 14mm, right: 14mm),
  fill: rgb("#0f141a"),
)

#let accent = rgb("#6cb6ff")
#let muted = rgb("#cdd3da")
#let rule-col = rgb("#27303a")
#let font-body = ("Avenir Next", "Helvetica Neue", "Helvetica")
#let font-display = ("Avenir Next", "Helvetica Neue", "Helvetica")

#set text(font: font-body, size: 10.2pt, fill: rgb("#eef1f4"))
#set par(justify: false, leading: 1.28em)

#let hr() = rect(height: 1pt, fill: rule-col)

#let section(title) = [
  #v(5pt)
  #block(
    fill: rgb("#171d25"),
    inset: (x: 9pt, y: 5pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 13pt, weight: "bold")[#title]
  ]
  #v(5pt)
]

#let experience-card(title, company, meta, body) = block[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 8pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 11pt, weight: "bold")[#title]
    #h(7pt)
    #text(size: 9.1pt, fill: accent, weight: "bold")[#company]
    #v(1.5pt)
    #text(size: 8.4pt, fill: muted)[#meta]
    #v(3.5pt)
    #body
  ]
  #v(4pt)
]

#let experience-bullet(content) = grid(
  columns: (8pt, 1fr),
  gutter: 4pt,
  [#text(size: 8pt, fill: muted)[•]],
  [#block(width: 100%)[
    #set par(leading: 0.60em)
    #text(size: 8.9pt)[#content]
  ]],
)

#let skill-item(title, content) = block(breakable: false)[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 8pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 10.2pt, weight: "bold")[#title]
    #v(2.5pt)
    #text(size: 8.9pt, fill: muted)[#content]
  ]
  #v(4pt)
]

#let project(name, url, body) = block(breakable: false)[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 8pt, y: 6pt),
    radius: 8pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(font: font-display, size: 11.2pt, weight: "bold")[#name]
    #v(1.5pt)
    #link(url)[#text(size: 9.2pt, fill: accent)[#url]]
    #v(3pt)
    #body
  ]
  #v(4pt)
]

#let qualification-line(content) = [
  #text(size: 9.8pt, weight: "bold")[#content]
]
""".strip("\n")
    )

    out.append(f'#text(font: font-display, size: 28pt, weight: "bold")[{para(doc["name"])}]')
    out.append("#v(2pt)")
    out.append(f'#text(size: 11pt, fill: muted)[{para(doc["tagline"])}]')
    out.append("#v(5pt)")
    contact = doc["contact"]
    out.append(
        "#text(size: 9.5pt, fill: muted)["
        + f'{para(contact["location"])}  •  Email: '
        + f'#link({typst_str("mailto:" + contact["email"])})[#text({typst_str(contact["email"])})]'
        + "  •  GitHub: "
        + f'#link({typst_str(contact["github"])})[#text({typst_str(contact["github"].removeprefix("https://").removeprefix("http://"))})]'
        + "]"
    )
    out.append("#v(6pt)")
    out.append("#hr()")
    out.append("#v(5pt)")

    out.append('#section("About Me")')
    about = doc["about"]
    for paragraph in about["pre_paragraphs"]:
        out.append(para(paragraph))
        out.append("")
    if about["list_items"]:
        out.append(para(about["list_heading"]) if about["list_heading"] else "Core focus:")
        out.append("#set list(spacing: 5pt)")
        for item in about["list_items"]:
            out.append(f"- {para(item)}")
        out.append("#set list(spacing: 2pt)")
        out.append("")
    for paragraph in about["post_paragraphs"]:
        out.append(para(paragraph))
        out.append("")

    if doc["skills"]:
        out.append('#section("Technical Skills")')
        out.append("#columns(2, gutter: 8pt)[")
        for area, items in doc["skills"]:
            joined = " • ".join(para(item) for item in items)
            out.append(f"#skill-item({typst_str(area)}, [{joined}])")
        out.append("]")

    experience_entries = list(doc["experience"])
    if experience_entries:
        out.append('#section("Professional Experience")')
        for entry in experience_entries:
            body: list[str] = []
            if entry.bullets:
                body.append("#stack(spacing: 2.5pt)[")
                for bullet in entry.bullets:
                    body.append(f"  #experience-bullet[{para(bullet)}]")
                body.append("]")
            meta_parts = [part for part in [entry.location, entry.dates] if part]
            meta = " | ".join(meta_parts) if meta_parts else ""
            out.append(
                f"#experience-card({typst_str(entry.title)}, {typst_str(entry.company)}, {typst_str(meta)})[\n  "
                + "\n  ".join(body)
                + "\n]"
            )

    projects = list(doc["projects"])
    if projects:
        out.append('#section("Key Projects")')
        for proj in projects:
            body: list[str] = []
            for paragraph in proj.description_paragraphs:
                body.append(f"#text(size: 9.2pt)[{para(paragraph)}]")
                body.append("#v(2.5pt)")
            for label_name, items in proj.labeled_lists:
                joined = "; ".join(para(item) for item in items if item)
                body.append(f'#text(size: 9pt, fill: muted, weight: "bold")[{para(label_name + ":")}] #h(4pt) #text(size: 9pt)[{joined}]')
                body.append("#v(2.5pt)")
            for label_name, value in proj.labeled_lines:
                body.append(f'#text(size: 9pt, fill: muted, weight: "bold")[{para(label_name + ":")}] #h(4pt) #text(size: 9pt)[{para(value)}]')
                body.append("#v(2.5pt)")
            for paragraph in proj.trailing_paragraphs:
                body.append(f"#text(size: 9.2pt)[{para(paragraph)}]")
                body.append("#v(2.5pt)")
            while body and body[-1].strip() in {"#v(2.5pt)", ""}:
                body.pop()
            out.append(
                f"#project({typst_str(proj.name)}, {typst_str(proj.url)})[\n  "
                + "\n  ".join(body)
                + "\n]"
            )

    out.append('#section("Qualifications")')
    qualifications = doc["qualifications"]
    out.append(
        f'#text(size: 9.8pt, weight: "bold")[{para(qualifications["degree"])}]'
        + " #h(5pt) "
        + f'#text(size: 9.4pt, fill: accent, weight: "bold")[{para(qualifications["honours"])}]'
        + " #h(5pt) "
        + f'#text(size: 9.1pt, fill: muted)[{para(qualifications["institution"])}]'
    )

    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build MarCVPDF.pdf from MarCV2026.md (Markdown stays the editable source)."
    )
    parser.add_argument(
        "--md",
        default="MarCV2026.md",
        help="Input Markdown file (default: MarCV2026.md)",
    )
    parser.add_argument(
        "--out",
        default="MarCVPDF.pdf",
        help="Output PDF path (default: MarCVPDF.pdf)",
    )
    parser.add_argument(
        "--style",
        choices=["cards", "bruyerre", "cyber"],
        default="cards",
        help="PDF layout/style preset (default: cards)",
    )
    args = parser.parse_args()

    md_path = Path(args.md)
    out_path = Path(args.out)
    if not md_path.exists():
        die(f"missing input file: {md_path}")

    doc = parse_markdown(md_path.read_text(encoding="utf-8"))
    if args.style == "cards":
        typst = render_typst_cards(doc)
    elif args.style == "bruyerre":
        typst = render_typst_bruyerre(doc)
    else:
        typst = render_typst_cyber(doc)

    with tempfile.TemporaryDirectory(prefix="marcv-") as tmp:
        typ_path = Path(tmp) / "MarCV.generated.typ"
        typ_path.write_text(typst, encoding="utf-8")

        try:
            subprocess.run(
                ["typst", "compile", str(typ_path), str(out_path)],
                check=True,
            )
        except FileNotFoundError:
            die("typst not found; install it (e.g., `brew install typst`) and retry")
        except subprocess.CalledProcessError as exc:
            die(f"typst compile failed (exit {exc.returncode})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
