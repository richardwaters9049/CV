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


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def strip_md_line_breaks(text: str) -> str:
    return re.sub(r"\s{2,}$", "", text)


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

    def section_block(title: str) -> list[str]:
        start = find_line_index(rf"##\s+{re.escape(title)}")
        if start == -1:
            die(f"missing section '## {title}'")
        start += 1
        # Consume until next '---' or next '## ' heading.
        block: list[str] = []
        index = start
        while index < len(lines):
            line = lines[index]
            if line.strip() == "---" or re.match(r"##\s+", line):
                break
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

    key_projects_start = find_line_index(r"##\s+Key Projects")
    if key_projects_start == -1:
        die("missing section '## Key Projects'")

    key_projects_end = find_line_index(r"##\s+Technical Skills")
    if key_projects_end == -1:
        die("missing section '## Technical Skills' (needed to delimit projects)")

    project_lines = lines[key_projects_start + 1 : key_projects_end]

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

    tech_lines = section_block("Technical Skills")
    table_rows = [line for line in tech_lines if line.strip().startswith("|")]
    if len(table_rows) < 3:
        die("Technical Skills section must be a Markdown table")
    # Drop header and separator
    data_rows = table_rows[2:]
    skills: list[tuple[str, str]] = []
    for row in data_rows:
        cols = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cols) < 2:
            continue
        area, items = cols[0], cols[1]
        if area and items:
            skills.append((area, items))
    if not skills:
        die("no skills parsed from Technical Skills table")

    exp_lines = section_block("Professional Experience")
    exp_compact = [l for l in exp_lines if l.strip()]
    experience: list[tuple[str, str]] = []
    idx = 0
    while idx + 1 < len(exp_compact):
        role = exp_compact[idx].strip()
        company = exp_compact[idx + 1].strip()
        experience.append((role, company))
        idx += 2
    if not experience:
        die("no experience entries parsed")

    edu_lines = section_block("Education")
    edu_compact = [l for l in edu_lines if l.strip()]
    if len(edu_compact) < 3:
        die("Education section must include degree line, honours line, and institution line")
    education = {
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
        "education": education,
    }


def render_typst(doc: dict) -> str:
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

#let accent = rgb("#f2b36b")
#let muted = rgb("#cdd3da")
#let rule-col = rgb("#27303a")

#set text(font: "Helvetica", size: 11pt, fill: rgb("#eef1f4"))
#set par(justify: false, leading: 1.32em)
#set list(indent: 14pt, body-indent: 6pt, spacing: 2pt)

#let hr() = rect(height: 1pt, fill: rule-col)

#let section(title) = [
  #v(10pt)
  #block(
    fill: rgb("#171d25"),
    inset: (x: 10pt, y: 6pt),
    radius: 7pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(size: 15pt, weight: "bold")[#title]
  ]
  #v(8pt)
]

#let section-compact(title) = [
  #block(
    fill: rgb("#171d25"),
    inset: (x: 9pt, y: 5pt),
    radius: 7pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(size: 13.5pt, weight: "bold")[#title]
  ]
  #v(6pt)
]

#let label(name) = text(fill: muted, weight: "bold")[#name]

#let project(name, url, body) = block(breakable: false)[
  #block(
    fill: rgb("#171d25"),
    inset: (x: 10pt, y: 8pt),
    radius: 7pt,
    stroke: (paint: rule-col, thickness: 0.8pt),
  )[
    #text(size: 12.5pt, weight: "bold")[#name]
    #v(2pt)
    #link(url)[#text(size: 10pt, fill: accent)[#url]]
    #v(6pt)
    #body
  ]
  #v(10pt)
]
""".strip(
            "\n"
        )
    )

    out.append(f'#text(size: 30pt, weight: "bold")[{para(doc["name"])}]')
    out.append("#v(3pt)")
    out.append(f'#text(size: 12.5pt, fill: muted)[{para(doc["tagline"])}]')
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
    out.append("#v(9pt)")
    out.append("#hr()")
    out.append("#v(8pt)")

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
        for item in about["list_items"]:
            out.append(f"- {para(item)}")
        out.append("")

    for paragraph in about["post_paragraphs"]:
        out.append(para(paragraph))
        out.append("")

    out.append('#section("Key Projects")')
    for proj in doc["projects"]:
        body: list[str] = []
        for paragraph in proj.description_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")
        for label_name, items in proj.labeled_lists:
            joined = "; ".join(para(item) for item in items if item)
            body.append(f'#label({typst_str(label_name + ":")}) {joined}')
            body.append("#v(4pt)")
        for label_name, value in proj.labeled_lines:
            body.append(f'#label({typst_str(label_name + ":")}) {para(value)}')
            body.append("#v(4pt)")
        for paragraph in proj.trailing_paragraphs:
            body.append(para(paragraph))
            body.append("#v(4pt)")

        while body and body[-1].strip() in {"#v(4pt)", ""}:
            body.pop()

        out.append(
            f"#project({typst_str(proj.name)}, {typst_str(proj.url)})[\n  "
            + "\n  ".join(body)
            + "\n]"
        )

    out.append('#section("Technical Skills")')
    out.append("#set text(size: 9.25pt)")
    out.append(
        "#table(\n"
        "  columns: (24%, 76%),\n"
        "  inset: 5pt,\n"
        "  align: left,\n"
        "  stroke: (paint: rule-col, thickness: 0.6pt),\n"
        '  fill: (rgb("#1c2128"),),\n'
        "  [*Area*], [*Skills*],"
    )
    for area, items in doc["skills"]:
        out.append(f"  [{para(area)}], [{para(items)}],")
    out.append(")")
    out.append("#set text(size: 10pt)")

    out.append("#block(breakable: false)[")
    out.append('  #section-compact("Professional Experience")')
    for role, company in doc["experience"]:
        out.append(f"  - {para(role)} — {para(company)}")
    out.append("")

    out.append('  #section-compact("Education")')
    edu = doc["education"]
    out.append(f'  {para(edu["degree"])} — {para(edu["honours"])}')
    out.append("")
    out.append(f"  {para(edu['institution'])}")
    out.append("]")

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
    args = parser.parse_args()

    md_path = Path(args.md)
    out_path = Path(args.out)
    if not md_path.exists():
        die(f"missing input file: {md_path}")

    doc = parse_markdown(md_path.read_text(encoding="utf-8"))
    typst = render_typst(doc)

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
