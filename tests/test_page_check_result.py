from tests import run_ingestor_page_test as page_check


def test_empty_fixture_directory_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "files/pdf").mkdir(parents=True)
    assert page_check.main([]) == 1


def test_each_document_has_one_result_even_when_extraction_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pdfs = tmp_path / "files/pdf"
    pdfs.mkdir(parents=True)
    for name in ("absent", "good", "low"):
        (pdfs / f"{name}.pdf").touch()

    def extract(path):
        if path.endswith("absent.pdf"):
            raise ValueError("could not extract baseline")
        return "one two", {}

    monkeypatch.setattr(page_check, "get_pdf_text_baseline", extract)
    monkeypatch.setattr(
        page_check,
        "get_ingestor_text",
        lambda name: "one" if name == "low" else "one two",
    )
    results = page_check.run_test()
    assert len(results) == 3
    by_name = dict(results)
    assert by_name["good"]["coverage"] == 1
    assert "could not extract baseline" in by_name["absent"]["error"]
    assert "50.00%" in by_name["low"]["error"]
    assert page_check.main([]) == 1
    assert page_check.main(["--doc_id", "good"]) == 0
