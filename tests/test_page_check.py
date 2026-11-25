from tests import run_ingestor_page_test as page_check


def test_empty_fixture_directory_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "files/pdf").mkdir(parents=True)
    assert page_check.main([]) == 1


def test_missing_document_fails_with_its_own_error_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert page_check.main(["--doc_id", "missing"]) == 1
    report = (tmp_path / "files/debug/missing_comparison.txt").read_text()
    assert "files/pdf/missing.pdf" in report
