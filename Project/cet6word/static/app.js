(function () {
  "use strict";

  const lookupForm = document.getElementById("lookupForm");
  const wordInput = document.getElementById("wordInput");
  const searchBtn = document.getElementById("searchBtn");
  const statusEl = document.getElementById("status");
  const resultEl = document.getElementById("result");
  const memoryList = document.getElementById("memoryList");
  const memoryCount = document.getElementById("memoryCount");
  const dueList = document.getElementById("dueList");
  const importForm = document.getElementById("importForm");
  const importWords = document.getElementById("importWords");
  const quizBtn = document.getElementById("quizBtn");
  const quizCard = document.getElementById("quizCard");
  const graphSvg = document.getElementById("graphSvg");
  const graphHint = document.getElementById("graphHint");
  const graphRelation = document.getElementById("graphRelation");
  const graphSearch = document.getElementById("graphSearch");
  const chainSteps = Array.from(document.querySelectorAll(".chain-step"));

  let source = null;
  let markdown = "";
  let graphState = { nodes: [], links: [] };
  let currentQuiz = null;

  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true });
  }
  if (window.lucide) {
    lucide.createIcons();
  }

  function setStatus(message, isError) {
    statusEl.textContent = message || "Ready";
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function setBusy(isBusy) {
    searchBtn.disabled = isBusy;
    wordInput.disabled = isBusy;
  }

  function activateStep(name) {
    chainSteps.forEach((step) => {
      step.classList.toggle("active", step.dataset.step === name);
    });
  }

  function renderMarkdown(value) {
    if (!value.trim()) {
      resultEl.innerHTML = '<div class="empty-state"><span class="empty-mark">Aa</span><p>输入一个单词，开始构建你的长期词汇记忆。</p></div>';
      return;
    }
    resultEl.innerHTML = window.marked ? marked.parse(value) : value;
  }

  function lookup(word) {
    const normalized = word.trim().toLowerCase();
    if (!normalized) return;
    if (source) source.close();

    markdown = "";
    renderMarkdown("");
    setBusy(true);
    setStatus("Preparing LCEL chain...");
    activateStep("prompt");

    source = new EventSource(`/api/lookup?word=${encodeURIComponent(normalized)}`);

    source.addEventListener("status", (event) => {
      const payload = JSON.parse(event.data);
      setStatus(payload.message);
      activateStep("llm");
    });

    source.addEventListener("token", (event) => {
      const payload = JSON.parse(event.data);
      markdown += payload.text;
      renderMarkdown(markdown);
      activateStep("parser");
    });

    source.addEventListener("saved", (event) => {
      const payload = JSON.parse(event.data);
      activateStep("memory");
      renderDashboard(payload);
      window.setTimeout(() => activateStep("graph"), 240);
    });

    source.addEventListener("done", (event) => {
      const payload = JSON.parse(event.data);
      setStatus(payload.message);
      setBusy(false);
      if (source) source.close();
      source = null;
      wordInput.disabled = false;
      wordInput.focus();
    });

    source.addEventListener("error", (event) => {
      let message = "查询失败，请检查服务或 API Key。";
      if (event.data) {
        try {
          message = JSON.parse(event.data).message || message;
        } catch (_) {
          message = event.data;
        }
      }
      setStatus(message, true);
      setBusy(false);
      if (source) {
        source.close();
        source = null;
      }
    });
  }

  function renderDashboard(payload) {
    renderMemory(payload.memory || []);
    renderDue(payload.due || []);
    if (graphRelation.value || graphSearch.value.trim()) {
      fetchGraph();
    } else {
      graphState = payload.graph || graphState;
      renderGraph(graphState);
    }
  }

  function renderMemory(words) {
    memoryCount.textContent = `${words.length} ${words.length === 1 ? "word" : "words"}`;
    if (!words.length) {
      memoryList.innerHTML = '<div class="memory-meta">No saved words yet</div>';
      return;
    }
    memoryList.innerHTML = words.map((item) => renderMemoryRow(item)).join("");
  }

  function renderDue(words) {
    if (!words.length) {
      dueList.innerHTML = '<div class="memory-meta">今日暂无到期复习。</div>';
      return;
    }
    dueList.innerHTML = words.map((item) => `
      <button class="memory-item" data-action="open-word" data-word="${escapeHtml(item.word)}" data-pending="${item.lookup_count ? "0" : "1"}">
        <span class="memory-word">${escapeHtml(item.word)}</span>
        <span class="memory-meta">${escapeHtml(item.mastery_level || "陌生")} · ${item.review_count || 0}x</span>
      </button>
    `).join("");
  }

  function renderMemoryRow(item) {
    return `
      <div class="memory-row">
        <button class="memory-item" data-action="open-word" data-word="${escapeHtml(item.word)}" data-pending="${item.lookup_count ? "0" : "1"}">
          <span class="memory-word">${escapeHtml(item.word)}</span>
          <span class="memory-meta">${item.lookup_count ? `${item.lookup_count} lookup` : "pending"}</span>
          <span class="mastery-badge">${escapeHtml(item.mastery_level || "陌生")} · ${item.review_count || 0}x</span>
        </button>
        <select class="mastery-select" data-word="${escapeHtml(item.word)}" aria-label="mastery">
          ${["陌生", "模糊", "掌握"].map((level) => `
            <option value="${level}" ${level === item.mastery_level ? "selected" : ""}>${level}</option>
          `).join("")}
        </select>
      </div>
    `;
  }

  async function loadWord(word, isPending) {
    if (isPending) {
      wordInput.value = word;
      lookup(word);
      return;
    }
    const response = await fetch(`/api/word/${encodeURIComponent(word)}`);
    if (!response.ok) {
      lookup(word);
      return;
    }
    const item = await response.json();
    markdown = item.markdown || "";
    renderMarkdown(markdown);
    setStatus(`${item.word} loaded from memory`);
    activateStep("memory");
  }

  async function updateMastery(word, masteryLevel) {
    const response = await fetch(`/api/word/${encodeURIComponent(word)}/mastery`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mastery_level: masteryLevel }),
    });
    const payload = await readJson(response);
    if (!response.ok) throw new Error(payload.detail || payload.message || "掌握度更新失败");
    renderDashboard(payload);
    setStatus(`${word} 标记为 ${masteryLevel}`);
  }

  async function importBatch() {
    const text = importWords.value.trim();
    if (!text) return;
    const response = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ words: text }),
    });
    const payload = await readJson(response);
    if (!response.ok) throw new Error(payload.detail || "批量导入失败");
    importWords.value = "";
    renderDashboard(payload);
    setStatus(`已导入 ${payload.imported.length} 个新单词`);
  }

  async function generateQuiz() {
    quizBtn.disabled = true;
    quizCard.innerHTML = "<p>正在生成测验...</p>";
    try {
      const response = await fetch("/api/quiz", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: 1 }),
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(payload.detail || "测验生成失败");
      currentQuiz = payload.quiz;
      renderQuiz(currentQuiz);
      setStatus(`已生成 ${currentQuiz.word} 的测验`);
    } catch (error) {
      quizCard.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
      setStatus(error.message, true);
    } finally {
      quizBtn.disabled = false;
    }
  }

  function renderQuiz(quiz) {
    quizCard.innerHTML = `
      <p class="quiz-question">${escapeHtml(quiz.question)}</p>
      <div class="quiz-options">
        ${quiz.options.map((option) => `
          <button class="quiz-option" data-answer="${escapeHtml(option)}">${escapeHtml(option)}</button>
        `).join("")}
      </div>
      <p class="quiz-explanation">选择一个答案后，智能体会更新复习计划。</p>
    `;
  }

  async function answerQuiz(answerButton) {
    if (!currentQuiz) return;
    const selected = answerButton.dataset.answer;
    const correct = selected === currentQuiz.answer;
    quizCard.querySelectorAll(".quiz-option").forEach((button) => {
      button.disabled = true;
      if (button.dataset.answer === currentQuiz.answer) button.classList.add("correct");
      if (button === answerButton && !correct) button.classList.add("wrong");
    });
    const explanation = quizCard.querySelector(".quiz-explanation");
    explanation.textContent = `${correct ? "回答正确" : "回答错误"}：${currentQuiz.explanation}`;

    const response = await fetch("/api/quiz/result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        word: currentQuiz.word,
        question_type: currentQuiz.type,
        question: currentQuiz.question,
        correct,
      }),
    });
    const payload = await readJson(response);
    if (!response.ok) throw new Error(payload.detail || "测验结果保存失败");
    renderDashboard(payload);
    setStatus(`${currentQuiz.word} 测验结果已写入复习计划`);
  }

  async function hydrate() {
    const [memoryResponse, dueResponse, graphResponse] = await Promise.all([
      fetch("/api/memory?include_pending=true"),
      fetch("/api/review/due"),
      fetch("/api/graph"),
    ]);
    const memoryPayload = await memoryResponse.json();
    const duePayload = await dueResponse.json();
    graphState = await graphResponse.json();
    renderMemory(memoryPayload.words || []);
    renderDue(duePayload.words || []);
    renderGraph(graphState);
  }

  async function fetchGraph() {
    const params = new URLSearchParams();
    if (graphRelation.value) params.set("relation", graphRelation.value);
    if (graphSearch.value.trim()) params.set("q", graphSearch.value.trim());
    const response = await fetch(`/api/graph?${params.toString()}`);
    graphState = await response.json();
    renderGraph(graphState);
  }

  function renderGraph(graph) {
    const nodes = (graph.nodes || []).map((node) => ({ ...node }));
    const links = (graph.links || []).map((link) => ({ ...link }));
    graphSvg.innerHTML = "";
    graphHint.style.display = links.length ? "none" : "grid";
    if (!nodes.length) return;

    const width = graphSvg.clientWidth || 340;
    const height = graphSvg.clientHeight || 338;
    const nodeById = new Map(nodes.map((node, index) => {
      const angle = (index / Math.max(1, nodes.length)) * Math.PI * 2;
      node.x = width / 2 + Math.cos(angle) * width * 0.28;
      node.y = height / 2 + Math.sin(angle) * height * 0.28;
      return [node.id, node];
    }));

    for (let tick = 0; tick < 130; tick += 1) {
      nodes.forEach((a, i) => {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          const dx = a.x - b.x || 0.01;
          const dy = a.y - b.y || 0.01;
          const distanceSq = dx * dx + dy * dy;
          const force = Math.min(1900 / distanceSq, 1.8);
          a.x += dx * force * 0.018;
          a.y += dy * force * 0.018;
          b.x -= dx * force * 0.018;
          b.y -= dy * force * 0.018;
        }
      });

      links.forEach((link) => {
        const sourceNode = nodeById.get(link.source);
        const targetNode = nodeById.get(link.target);
        if (!sourceNode || !targetNode) return;
        const dx = targetNode.x - sourceNode.x;
        const dy = targetNode.y - sourceNode.y;
        sourceNode.x += dx * 0.012;
        sourceNode.y += dy * 0.012;
        targetNode.x -= dx * 0.012;
        targetNode.y -= dy * 0.012;
      });

      nodes.forEach((node) => {
        node.x += (width / 2 - node.x) * 0.008;
        node.y += (height / 2 - node.y) * 0.008;
        node.x = Math.max(42, Math.min(width - 42, node.x));
        node.y = Math.max(34, Math.min(height - 34, node.y));
      });
    }

    const fragment = document.createDocumentFragment();
    links.forEach((link) => {
      const sourceNode = nodeById.get(link.source);
      const targetNode = nodeById.get(link.target);
      if (!sourceNode || !targetNode) return;
      const line = svgEl("line", {
        x1: sourceNode.x,
        y1: sourceNode.y,
        x2: targetNode.x,
        y2: targetNode.y,
        class: `graph-link ${link.relation || "related"}`,
      });
      line.appendChild(svgEl("title", {}, `${link.source} -> ${link.target} (${link.label})`));
      fragment.appendChild(line);
    });

    nodes.forEach((node) => {
      const group = svgEl("g", {
        class: `graph-node ${node.remembered ? "remembered" : ""}`,
        transform: `translate(${node.x}, ${node.y})`,
      });
      const radius = Math.min(18, 7 + Number(node.weight || 1) * 2);
      group.appendChild(svgEl("circle", { r: radius }));
      group.appendChild(svgEl("text", { x: 0, y: radius + 13, "text-anchor": "middle" }, node.label));
      group.addEventListener("click", () => loadWord(node.id, !node.remembered));
      fragment.appendChild(group);
    });

    graphSvg.appendChild(fragment);
  }

  function svgEl(name, attrs, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs || {}).forEach(([key, value]) => element.setAttribute(key, value));
    if (text) element.textContent = text;
    return element;
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  lookupForm.addEventListener("submit", (event) => {
    event.preventDefault();
    lookup(wordInput.value);
  });

  memoryList.addEventListener("click", (event) => {
    const item = event.target.closest("[data-action='open-word']");
    if (item) loadWord(item.dataset.word, item.dataset.pending === "1");
  });

  memoryList.addEventListener("change", (event) => {
    if (!event.target.matches(".mastery-select")) return;
    updateMastery(event.target.dataset.word, event.target.value).catch((error) => {
      setStatus(error.message, true);
    });
  });

  dueList.addEventListener("click", (event) => {
    const item = event.target.closest("[data-action='open-word']");
    if (item) loadWord(item.dataset.word, item.dataset.pending === "1");
  });

  importForm.addEventListener("submit", (event) => {
    event.preventDefault();
    importBatch().catch((error) => setStatus(error.message, true));
  });

  quizBtn.addEventListener("click", generateQuiz);

  quizCard.addEventListener("click", (event) => {
    const option = event.target.closest(".quiz-option");
    if (option) answerQuiz(option).catch((error) => setStatus(error.message, true));
  });

  graphRelation.addEventListener("change", fetchGraph);
  graphSearch.addEventListener("input", () => {
    window.clearTimeout(graphSearch._timer);
    graphSearch._timer = window.setTimeout(fetchGraph, 220);
  });
  window.addEventListener("resize", () => renderGraph(graphState));

  hydrate().catch((error) => setStatus(error.message, true));
  wordInput.focus();
})();
