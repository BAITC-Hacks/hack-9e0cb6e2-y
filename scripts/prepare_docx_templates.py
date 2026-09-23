"""Distill the two approved DOCX references into content-free reusable components."""

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from lxml import etree


def replace_text(element, value):
    nodes = list(element.iter(qn("w:t")))
    assert nodes
    nodes[0].text = value
    for node in nodes[1:]:
        node.text = ""


def prepare(reference, number, destination):
    slots = {"title": 0, "organization": 1, "subject": 2, "heading": 3, "speaker": 4, "body": 5}
    slots.update(
        {"summary_heading": 70, "subheading": 71, "actions": 73, "divider": 18}
        if number == 1
        else {"summary_heading": 60, "subheading": 63, "reports": 62, "actions": 64}
    )
    preserved = {}
    with ZipFile(reference) as source:
        root = parse_xml(source.read("word/document.xml"))
        body = root.find(qn("w:body"))
        components = []
        for slot, index in slots.items():
            component = deepcopy(body[index])
            if slot == "divider":
                run = OxmlElement("w:r")
                text = OxmlElement("w:t")
                run.append(text)
                component.append(run)
            if component.tag == qn("w:tbl"):
                rows = component.findall(qn("w:tr"))
                for row in rows[2:]:
                    component.remove(row)
                for cell in component.iter(qn("w:tc")):
                    replace_text(cell, "")
                replace_text(component, "{{" + slot + "}}")
            else:
                replace_text(component, "{{" + slot + "}}")
            components.append(component)
        section = deepcopy(body.find(qn("w:sectPr")))
        for element in list(body):
            body.remove(element)
        body.extend(components + [section])
        destination.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(destination, "w") as output:
            for member in source.infolist():
                content = source.read(member.filename)
                if member.filename == "word/document.xml":
                    content = etree.tostring(
                        root, xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                elif member.filename == "docProps/core.xml":
                    content = b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"/>'
                elif member.filename == "docProps/custom.xml":
                    content = b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"/>'
                else:
                    if member.filename in (
                        "word/comments.xml",
                        "word/footnotes.xml",
                        "word/endnotes.xml",
                    ):
                        assert not list(parse_xml(content).iter(qn("w:t"))), (
                            "Unexpected private text"
                        )
                    preserved[member.filename] = hashlib.sha256(content).hexdigest()
                output.writestr(member, content)
    return {
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "template_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "slots": slots,
        "preserved_parts": preserved,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_1", type=Path)
    parser.add_argument("reference_2", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    inventory = {
        str(number): prepare(
            reference, number, root / f"src/autoprotocol/templates/protocol-{number}.docx"
        )
        for number, reference in enumerate((args.reference_1, args.reference_2), 1)
    }
    (root / "docs/docx-template-manifest.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )
    print("Prepared two sanitized reference-derived templates")


if __name__ == "__main__":
    main()
