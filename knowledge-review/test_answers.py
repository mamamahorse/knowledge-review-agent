import unittest
from pathlib import Path

from pydantic import ValidationError
from answers import NoteAnswer, validate_answer, render_answer
from sources import Source


SOURCES = [Source("doc-a", Path("a.md"), "# A\n\nOriginal"), Source("doc-b", Path("b.md"), "# B\nOther")]


def payload(kind="grounded", source_id="doc-a", start=3, end=3):
    return {"coverage": "sufficient", "coverage_note": "", "blocks": [{
        "kind": kind, "text": "Explanation", "citations": [
            {"source_id": source_id, "line_start": start, "line_end": end}
        ] if kind != "supplement" else [],
    }]}


class AnswersTests(unittest.TestCase):
    def test_grounded_and_inference_with_real_ranges(self):
        for kind in ["grounded", "inference"]:
            answer = NoteAnswer.model_validate(payload(kind))
            validate_answer(answer, SOURCES)
            self.assertIn("a.md", render_answer(answer, SOURCES))
            self.assertIn("L3", render_answer(answer, SOURCES))

    def test_supplement_when_no_evidence_and_strict_rejection(self):
        data = payload("supplement")
        data.update(coverage="none", coverage_note="Not in these notes.")
        answer = NoteAnswer.model_validate(data)
        validate_answer(answer, SOURCES)
        with self.assertRaises(RuntimeError):
            validate_answer(answer, SOURCES, strict=True)
        data["blocks"] = []
        validate_answer(NoteAnswer.model_validate(data), SOURCES, strict=True)

    def test_mixed_answer(self):
        data = payload()
        data.update(coverage="partial", coverage_note="Some background is missing.")
        data["blocks"] += payload("supplement")["blocks"]
        validate_answer(NoteAnswer.model_validate(data), SOURCES)

    def test_wrong_document_or_line(self):
        for source_id, start, end in [("missing", 1, 1), ("doc-a", 0, 1), ("doc-a", 3, 2), ("doc-b", 3, 3)]:
            with self.subTest(source=source_id, start=start, end=end), self.assertRaises(RuntimeError):
                validate_answer(NoteAnswer.model_validate(payload(source_id=source_id, start=start, end=end)), SOURCES)

    def test_contract_contradictions(self):
        cases = []
        data = payload(); data["blocks"][0]["citations"] = []; cases.append(data)
        data = payload(); data["blocks"][0]["kind"] = "supplement"; cases.append(data)
        data = payload(); data["blocks"][0]["text"] = "  "; cases.append(data)
        data = payload(); data["blocks"][0]["text"] = "Text [L999]"; cases.append(data)
        data = payload(); data["coverage_note"] = "See [L999]"; cases.append(data)
        data = payload(); data["coverage"] = "none"; data["coverage_note"] = "Missing"; cases.append(data)
        data = payload(); data["coverage"] = "partial"; cases.append(data)
        data = payload("supplement"); cases.append(data)
        data = payload(); data["blocks"] = []; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                validate_answer(NoteAnswer.model_validate(data), SOURCES)

    def test_strict_schema(self):
        for value in [True, "3", 1.5]:
            data = payload(start=value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                NoteAnswer.model_validate(data)


if __name__ == "__main__":
    unittest.main()
