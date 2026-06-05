from pathlib import Path

from memory_store import MemoryStore


def test_word_lookup_is_persisted_across_store_instances(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"

    first_store = MemoryStore(db_path)
    first_store.save_lookup(
        word="ascribe",
        markdown="## ascribe\n\nattribute something to a cause.",
        relations=[
            {"target": "attribute", "relation": "synonym", "label": "意近词"},
            {"target": "ascription", "relation": "word_family", "label": "词族"},
        ],
    )

    second_store = MemoryStore(db_path)
    remembered = second_store.get_word("ascribe")

    assert remembered is not None
    assert remembered["word"] == "ascribe"
    assert remembered["lookup_count"] == 1
    assert "attribute something" in remembered["markdown"]


def test_graph_contains_words_and_relation_edges(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save_lookup(
        word="ascribe",
        markdown="content",
        relations=[
            {"target": "attribute", "relation": "synonym", "label": "意近词"},
            {"target": "scribe", "relation": "confusable", "label": "形近词"},
        ],
    )
    store.save_lookup(
        word="attribute",
        markdown="content",
        relations=[{"target": "quality", "relation": "collocation", "label": "搭配"}],
    )

    graph = store.get_graph()

    node_ids = {node["id"] for node in graph["nodes"]}
    edge_pairs = {(edge["source"], edge["target"], edge["relation"]) for edge in graph["links"]}

    assert {"ascribe", "attribute", "scribe", "quality"}.issubset(node_ids)
    assert ("ascribe", "attribute", "synonym") in edge_pairs
    assert ("ascribe", "scribe", "confusable") in edge_pairs
    assert ("attribute", "quality", "collocation") in edge_pairs


def test_mastery_and_review_schedule_can_be_updated(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save_lookup(word="ascribe", markdown="content", relations=[])

    store.update_mastery("ascribe", "掌握")
    store.record_review("ascribe", correct=True)

    remembered = store.get_word("ascribe")

    assert remembered["mastery_level"] == "掌握"
    assert remembered["review_count"] == 1
    assert remembered["last_reviewed_at"] is not None
    assert remembered["next_review_at"] is not None


def test_due_review_words_are_ordered_by_next_review_time(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.import_words(["ascribe", "attribute", "ascribe"])
    store.update_mastery("attribute", "掌握")

    due_words = store.list_due_reviews()

    assert [item["word"] for item in due_words] == ["ascribe", "attribute"]
    assert due_words[0]["lookup_count"] == 0


def test_import_words_deduplicates_and_preserves_existing_memory(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save_lookup(word="ascribe", markdown="content", relations=[])

    imported = store.import_words(["ascribe", "attribute", "  attribute  ", ""])

    assert imported == ["attribute"]
    assert store.get_word("ascribe")["markdown"] == "content"
    assert store.get_word("attribute")["lookup_count"] == 0


def test_graph_can_be_filtered_by_relation(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save_lookup(
        word="ascribe",
        markdown="content",
        relations=[
            {"target": "attribute", "relation": "synonym", "label": "意近词"},
            {"target": "scribe", "relation": "confusable", "label": "形近词"},
        ],
    )

    graph = store.get_graph(relation="synonym")

    assert [edge["relation"] for edge in graph["links"]] == ["synonym"]
    assert {node["id"] for node in graph["nodes"]} == {"ascribe", "attribute"}


def test_quiz_results_are_persisted_and_update_review_state(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save_lookup(word="ascribe", markdown="content", relations=[])

    store.save_quiz_result(
        word="ascribe",
        question_type="choice",
        correct=True,
        question="What does ascribe mean?",
    )

    remembered = store.get_word("ascribe")
    assert remembered["review_count"] == 1
    assert remembered["mastery_level"] == "掌握"
    assert store.list_quiz_results()[0]["word"] == "ascribe"
