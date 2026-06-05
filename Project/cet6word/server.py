"""CET-6 word studio: LCEL streaming lookup, memory, review, quiz, and graph API."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from memory_store import MASTERY_LEVELS, MemoryStore


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
ROOT_DIR = next((path for path in [APP_DIR, *APP_DIR.parents] if (path / ".env").exists()), APP_DIR)
DATA_DIR = Path(os.getenv("CET6WORD_DATA_DIR", "/tmp/cet6word" if os.getenv("VERCEL") else str(APP_DIR / "data")))

load_dotenv(ROOT_DIR / ".env", override=True)

memory = MemoryStore(DATA_DIR / "cet6_memory.sqlite3")
lookup_chain = None
quiz_chain = None


class ImportRequest(BaseModel):
    words: str = Field(..., min_length=1)


class MasteryRequest(BaseModel):
    mastery_level: str


class QuizRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=5)


class QuizResultRequest(BaseModel):
    word: str = Field(..., min_length=1)
    question_type: str = Field(default="choice", min_length=1)
    question: str = Field(..., min_length=1)
    correct: bool


app = FastAPI(title="CET-6 Word Studio", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/lookup")
async def lookup(word: str = Query(..., min_length=1, description="English word to lookup")):
    normalized = _normalize_word(word)

    async def event_stream():
        full_text = ""
        try:
            yield _sse("status", {"message": f"正在解析 {normalized} 的词义、搭配与关系..."})
            chain = _get_lookup_chain()
            for chunk in chain.stream({"vocabulary": normalized}):
                full_text += chunk
                yield _sse("token", {"text": chunk})

            relations = extract_relations(normalized, full_text)
            memory.save_lookup(normalized, full_text, relations)
            yield _sse("saved", _dashboard_payload(word=normalized, relations=relations))
            yield _sse("done", {"message": "查询完成，已写入长期记忆。"})
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/memory")
async def list_memory(
    limit: int = Query(80, ge=1, le=300),
    include_pending: bool = Query(False),
):
    return {"words": memory.list_words(limit=limit, include_pending=include_pending)}


@app.get("/api/review/due")
async def list_due_reviews(limit: int = Query(40, ge=1, le=200)):
    return {"words": memory.list_due_reviews(limit=limit)}


@app.get("/api/word/{word}")
async def get_word(word: str):
    item = memory.get_word(word)
    if item is None or not item.get("markdown"):
        return JSONResponse({"message": "word not found"}, status_code=404)
    return item


@app.post("/api/word/{word}/mastery")
async def update_mastery(word: str, request: MasteryRequest):
    if request.mastery_level not in MASTERY_LEVELS:
        raise HTTPException(status_code=400, detail="mastery_level must be one of: 陌生, 模糊, 掌握")
    item = memory.update_mastery(word, request.mastery_level)
    return {**_dashboard_payload(word=item["word"]), "word": item}


@app.post("/api/import")
async def import_words(request: ImportRequest):
    words = _split_import_words(request.words)
    imported = memory.import_words(words)
    return {**_dashboard_payload(), "imported": imported}


@app.post("/api/quiz")
async def generate_quiz(request: QuizRequest):
    candidates = memory.list_due_reviews(limit=20) or memory.list_words(limit=20)
    remembered = [item["word"] for item in candidates if item.get("lookup_count", 0) > 0]
    if not remembered:
        raise HTTPException(status_code=400, detail="请先查询并保存至少一个单词，再生成测验。")

    selected_words = remembered[: request.count]
    try:
        raw = _get_quiz_chain().invoke({"words": selected_words, "count": request.count})
        quiz = _parse_quiz_payload(str(raw))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"测验生成失败: {exc}") from exc

    return {"quiz": quiz, "words": selected_words}


@app.post("/api/quiz/result")
async def save_quiz_result(request: QuizResultRequest):
    item = memory.save_quiz_result(
        word=request.word,
        question_type=request.question_type,
        correct=request.correct,
        question=request.question,
    )
    return {**_dashboard_payload(word=item["word"]), "word": item}


@app.get("/api/graph")
async def get_graph(
    relation: str | None = Query(None),
    q: str | None = Query(None),
):
    return memory.get_graph(relation=relation, query=q)


def extract_relations(word: str, markdown: str) -> list[dict[str, str]]:
    relation_map = {
        "词族": "word_family",
        "意近词": "synonym",
        "近义词": "synonym",
        "同义词": "synonym",
        "形近词": "confusable",
        "搭配": "collocation",
    }
    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for label, relation_type in relation_map.items():
        pattern = rf"[-*]\s*{re.escape(label)}\s*[:：]\s*(.+)"
        for match in re.finditer(pattern, markdown, flags=re.IGNORECASE):
            for target in _split_relation_targets(match.group(1)):
                normalized_target = _normalize_graph_node(target)
                if not normalized_target or normalized_target == word:
                    continue
                edge_key = (normalized_target, relation_type)
                if edge_key in seen:
                    continue
                seen.add(edge_key)
                relations.append(
                    {
                        "target": normalized_target,
                        "relation": relation_type,
                        "label": label,
                    }
                )

    return relations[:24]


def _get_lookup_chain():
    global lookup_chain
    if lookup_chain is None:
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_deepseek import ChatDeepSeek

        llm = ChatDeepSeek(model="deepseek-v4-flash", temperature=0.7)
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一个严谨、耐心的 CET-6 词汇老师。请参考主流词典解释英文单词，"
                    "用结构化 Markdown 输出，适合长期复习和前端渲染。要求：\n"
                    "1. 给出英美双版音标、词性、中文核心释义。\n"
                    "2. 按常见程度输出不超过 3 个义项，每个义项包含英文释义、中文解释、例句与译文。\n"
                    "3. 给出常用搭配、同义/近义辨析、形近词辨析。\n"
                    "4. 最后必须追加一个“## 知识图谱线索”小节，严格使用以下四行格式：\n"
                    "- 词族: word1, word2\n"
                    "- 意近词: word1, word2\n"
                    "- 形近词: word1, word2\n"
                    "- 搭配: phrase1, phrase2\n"
                    "如果某一类没有可靠结果，写“无”。不要编造罕见或不确定的关系。",
                ),
                ("user", "{vocabulary}"),
            ]
        )
        lookup_chain = prompt | llm | StrOutputParser()
    return lookup_chain


def _get_quiz_chain():
    global quiz_chain
    if quiz_chain is None:
        from langchain_core.output_parsers import StrOutputParser
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_deepseek import ChatDeepSeek

        llm = ChatDeepSeek(model="deepseek-v4-flash", temperature=0.4)
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是 CET-6 词汇测验出题智能体。请基于给定单词生成一道选择题。"
                    "必须只输出 JSON，不要 Markdown，不要解释 JSON 外内容。字段："
                    "word, type, question, options, answer, explanation。"
                    "options 必须是 4 个中文短选项，answer 必须等于其中一个选项。",
                ),
                ("user", "words={words}; count={count}"),
            ]
        )
        quiz_chain = prompt | llm | StrOutputParser()
    return quiz_chain


def _dashboard_payload(word: str | None = None, relations: list[dict[str, str]] | None = None) -> dict:
    return {
        "word": word,
        "relations": relations or [],
        "memory": memory.list_words(include_pending=True),
        "due": memory.list_due_reviews(),
        "graph": memory.get_graph(),
    }


def _parse_quiz_payload(raw: str) -> dict:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    payload = json.loads(text)
    required = {"word", "type", "question", "options", "answer", "explanation"}
    missing = required - set(payload)
    if missing:
        raise ValueError(f"quiz JSON missing fields: {', '.join(sorted(missing))}")
    if not isinstance(payload["options"], list) or len(payload["options"]) != 4:
        raise ValueError("quiz options must contain exactly 4 items")
    return payload


def _split_import_words(text: str) -> list[str]:
    return [_normalize_word(item) for item in re.split(r"[\s,，;；、]+", text) if item.strip()]


def _split_relation_targets(text: str) -> Iterable[str]:
    cleaned = re.sub(r"[`*_#>]", "", text)
    cleaned = re.sub(r"\([^)]*\)|（[^）]*）", "", cleaned)
    if cleaned.strip() in {"无", "none", "None", "N/A", "n/a"}:
        return []
    return [item.strip() for item in re.split(r"[,，;；、/]+", cleaned) if item.strip()]


def _normalize_graph_node(text: str) -> str:
    text = text.strip().lower()
    match = re.search(r"[a-z][a-z\s'-]{0,38}", text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(0)).strip(" -'")


def _normalize_word(word: str) -> str:
    return word.strip().lower()


def _sse(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="info")
