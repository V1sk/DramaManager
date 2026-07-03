import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import ai_jobs
from app.ai_translate_client import AITranslateError, _parse_json_array, _parse_json_object


def test_model_format_errors_are_retryable():
    for parse in (_parse_json_object, _parse_json_array):
        try:
            parse("not json")
        except AITranslateError as e:
            assert e.retryable is True
        else:  # pragma: no cover
            raise AssertionError("expected AITranslateError")


def test_translation_job_retries_retryable_errors(monkeypatch):
    calls = {"executor": 0}
    statuses = []

    async def fake_executor(job, all_langs):
        calls["executor"] += 1
        if calls["executor"] < 3:
            raise AITranslateError("AI 翻译返回的不是有效 JSON", retryable=True)

    async def fake_sleep(_seconds):
        return None

    def fake_set_status(job_id, status, *, error=None, bump_attempts=False):
        statuses.append({
            "job_id": job_id,
            "status": status,
            "error": error,
            "bump_attempts": bump_attempts,
        })

    monkeypatch.setitem(ai_jobs._EXECUTORS, "tag", fake_executor)
    monkeypatch.setattr(ai_jobs, "_lang_label_map", lambda: {})
    monkeypatch.setattr(ai_jobs.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ai_jobs.db, "set_translation_job_status", fake_set_status)

    asyncio.run(ai_jobs._run_with_retry({"id": 7, "kind": "tag"}))

    assert calls["executor"] == 3
    assert [s["status"] for s in statuses] == ["running", "running", "done"]
    assert all(s["status"] != "failed" for s in statuses)
    assert statuses[0]["error"].startswith("重试 1/2")
    assert statuses[1]["error"].startswith("重试 2/2")
