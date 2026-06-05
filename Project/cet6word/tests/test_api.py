import json
from pathlib import Path

import server
from fastapi.testclient import TestClient
from memory_store import MemoryStore


class FakeQuizChain:
    def invoke(self, payload):
        word = payload["words"][0]
        return json.dumps(
            {
                "word": word,
                "type": "choice",
                "question": f"{word} 最接近下列哪个含义？",
                "options": ["归因于", "订阅", "描写", "规定"],
                "answer": "归因于",
                "explanation": "ascribe 表示把某事归因于某人或某物。",
            },
            ensure_ascii=False,
        )


def make_client(tmp_path: Path, monkeypatch):
    test_memory = MemoryStore(tmp_path / "api.sqlite3")
    monkeypatch.setattr(server, "memory", test_memory)
    monkeypatch.setattr(server, "quiz_chain", FakeQuizChain())
    return TestClient(server.app), test_memory


def test_import_api_creates_pending_words(tmp_path: Path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)

    response = client.post("/api/import", json={"words": "ascribe\nattribute\nascribe"})

    assert response.status_code == 200
    assert response.json()["imported"] == ["ascribe", "attribute"]
    assert [item["word"] for item in store.list_words(include_pending=True)] == [
        "ascribe",
        "attribute",
    ]


def test_mastery_api_updates_word_state(tmp_path: Path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    store.save_lookup("ascribe", "content", [])

    response = client.post("/api/word/ascribe/mastery", json={"mastery_level": "模糊"})

    assert response.status_code == 200
    assert response.json()["word"]["mastery_level"] == "模糊"


def test_due_review_api_returns_due_words(tmp_path: Path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    store.import_words(["ascribe"])

    response = client.get("/api/review/due")

    assert response.status_code == 200
    assert response.json()["words"][0]["word"] == "ascribe"


def test_quiz_api_generates_structured_question(tmp_path: Path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    store.save_lookup("ascribe", "content", [])

    response = client.post("/api/quiz", json={"count": 1})

    assert response.status_code == 200
    assert response.json()["quiz"]["word"] == "ascribe"
    assert response.json()["quiz"]["options"][0] == "归因于"


def test_quiz_result_api_updates_review_state(tmp_path: Path, monkeypatch):
    client, store = make_client(tmp_path, monkeypatch)
    store.save_lookup("ascribe", "content", [])

    response = client.post(
        "/api/quiz/result",
        json={
            "word": "ascribe",
            "question_type": "choice",
            "question": "meaning?",
            "correct": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["word"]["review_count"] == 1
    assert response.json()["word"]["mastery_level"] == "掌握"
