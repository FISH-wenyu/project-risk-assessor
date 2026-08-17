/* Contract module - chat: layout, selection, the summary bar and sending.
 *
 * Presentation is deliberately not its own implementation: messages are
 * rendered by app.js's `appendChatMessageToTarget` and revealed by its
 * `typeChatText`, borrowed through the shell. Two renderers for the same
 * bubble drift apart; one does not.
 *
 * Depends on contract-shell.js and contract-ledger.js.
 */


const CHAT_LAYOUTS = ["wide-list", "balanced", "wide-chat", "stacked"];
const chatState = { layout: "balanced", selected: new Set() };

function loadChatLayout() {
  try {
    const saved = localStorage.getItem("contractChatLayout");
    if (CHAT_LAYOUTS.includes(saved)) chatState.layout = saved;
  } catch (error) {
    chatState.layout = "balanced";
  }
}

function applyChatLayout() {
  const split = document.getElementById("chatSplit");
  if (!split) return;
  for (const layout of CHAT_LAYOUTS) split.classList.remove(`layout-${layout}`);
  split.classList.add(`layout-${chatState.layout}`);
  for (const button of document.querySelectorAll("[data-chat-layout]")) {
    button.classList.toggle("active", button.dataset.chatLayout === chatState.layout);
  }
}

function llmStatusLabel() {
  const configured = shared.llmConfigured();
  if (configured === null) return "未知";
  return configured ? "已配置" : "未配置";
}

function renderChatMetrics() {
  const host = document.getElementById("chatMetrics");
  if (!host) return;
  const rows = contractState.rows;
  const selected = rows.filter(r => chatState.selected.has(r.contract_ref));
  const cards = [
    ["可分析合同", rows.length],
    ["已选择", selected.length],
    ["选中命中数", selected.reduce((n, r) => n + (Number(r.finding_count) || 0), 0)],
    ["立即处理", selected.filter(r => r.tier === "act_now").length],
    ["涉及组织", new Set(selected.map(r => r.org_id).filter(Boolean)).size],
    // Read from the server's own answer. This card said 未开放 for a day after
    // the contract chat shipped, which is a card telling the user a working
    // feature does not work.
    ["LLM", llmStatusLabel()],
  ];
  host.replaceChildren();
  for (const [label, value] of cards) {
    const cell = document.createElement("div");
    cell.className = "metric";
    const name = document.createElement("span");
    name.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    cell.append(name, strong);
    host.appendChild(cell);
  }
}

/* Select-all operates on what is IN THE LIST, which is the whole loaded
 * ledger. The API caps a request at 20 contracts, so selecting everything would
 * silently drop 45 of them; the hint says so before the user sends, rather
 * than letting them read an answer built from a third of what they chose. */
const CHAT_MAX_CONTRACTS = 20;

function selectableChatRefs() {
  return contractState.rows.map(row => row.contract_ref).filter(Boolean);
}

function renderChatSelectionControls() {
  const box = document.getElementById("chatSelectAll");
  const label = document.getElementById("chatSelectAllLabel");
  const hint = document.getElementById("chatSelectionHint");
  if (!box) return;

  const refs = selectableChatRefs();
  const chosen = refs.filter(ref => chatState.selected.has(ref)).length;
  box.disabled = refs.length === 0;
  box.checked = refs.length > 0 && chosen === refs.length;
  // Some-but-not-all is a third state, and a plain checkbox showing "off"
  // there tells the user nothing is selected when things are.
  box.indeterminate = chosen > 0 && chosen < refs.length;
  label.textContent = box.checked ? "取消全选" : "全选";

  if (!chosen) {
    hint.textContent = refs.length ? `共 ${refs.length} 份合同，尚未选择。` : "";
    hint.classList.remove("warn-text");
    return;
  }
  if (chosen > CHAT_MAX_CONTRACTS) {
    hint.textContent =
      `已选 ${chosen} 份，但每次提问最多发送 ${CHAT_MAX_CONTRACTS} 份，`
      + `超出的部分不会进入本次回答。建议先用台账筛选后再提问。`;
    hint.classList.add("warn-text");
    return;
  }
  hint.textContent = `已选 ${chosen} / ${refs.length} 份。`;
  hint.classList.remove("warn-text");
}

function setAllChatSelection(selected) {
  if (selected) for (const ref of selectableChatRefs()) chatState.selected.add(ref);
  else chatState.selected.clear();
  renderChatContractList();
  renderChatMetrics();
}

function renderChatContractList() {
  renderChatSelectionControls();
  const host = document.getElementById("chatContractList");
  if (!host) return;
  host.replaceChildren();
  if (!contractState.rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "尚未加载合同数据，请先打开「合同风险台账」并加载。";
    host.appendChild(empty);
    return;
  }
  for (const row of contractState.rows) {
    const label = document.createElement("label");
    label.className = "chat-contract-item";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = chatState.selected.has(row.contract_ref);
    box.addEventListener("change", () => {
      if (box.checked) chatState.selected.add(row.contract_ref);
      else chatState.selected.delete(row.contract_ref);
      // Keep the header control in step: ticking the last row by hand must
      // flip 全选 on, and unticking one must take it out of the all state.
      renderChatSelectionControls();
      renderChatMetrics();
    });
    const text = document.createElement("span");
    // textContent, never innerHTML: contract names are server data.
    text.textContent = `${row.contract_name || "(未命名)"} · ${row.risk_level || "-"}(${row.risk_score ?? 0})`;
    label.append(box, text);
    host.appendChild(label);
  }
}

function initContractChatView() {
  loadChatLayout();
  applyChatLayout();
  document.getElementById("chatSelectAll").addEventListener("change", event => {
    setAllChatSelection(event.target.checked);
  });
  document.getElementById("chatClearSelection").addEventListener("click", () => {
    setAllChatSelection(false);
  });
  for (const button of document.querySelectorAll("[data-chat-layout]")) {
    button.addEventListener("click", () => {
      chatState.layout = button.dataset.chatLayout;
      try { localStorage.setItem("contractChatLayout", chatState.layout); } catch (e) { /* ignore */ }
      applyChatLayout();
    });
  }
  document.getElementById("chatLayoutReset").addEventListener("click", () => {
    chatState.layout = "balanced";
    try { localStorage.setItem("contractChatLayout", chatState.layout); } catch (e) { /* ignore */ }
    applyChatLayout();
  });
  document.getElementById("chatSelectFiltered").addEventListener("click", () => {
    for (const row of filterContractRows()) chatState.selected.add(row.contract_ref);
    renderChatContractList();
    renderChatMetrics();
  });
  document.getElementById("chatContractSend").addEventListener("click", sendContractChat);
  document.getElementById("chatContractInput").addEventListener("keydown", event => {
    // Enter sends, Shift+Enter newlines. isComposing guards IME input, where
    // Enter commits the candidate rather than submitting.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendContractChat();
    }
  });
  document.getElementById("chatClearSelection").addEventListener("click", () => {
    chatState.selected.clear();
    renderChatContractList();
    renderChatMetrics();
  });
}

/* ------------------------------------------------------- contract chat send */

const CHAT_SIGNAL_LABELS = {
  llm_not_configured: "未配置大模型，以下为本地规则摘要",
  llm_call_failed: "大模型调用失败，已降级为本地摘要",
  no_clause_text_available: "未检索到合同正文，回答仅基于规则命中",
  no_clause_matched_the_question: "合同正文已读取，但没有条款与该问题相关，回答仅基于规则命中",
  all_clauses_dropped_by_redaction_gate: "全部条款因无法安全脱敏而未参与分析",
  some_clauses_dropped_by_redaction_gate: "部分条款因无法安全脱敏而未参与分析",
  clause_payload_truncated: "条款内容超出上限已截断",
  clause_selection_fell_back_to_top_findings: "问题未匹配到主题，已改用风险最高的条款",
  clause_split_fell_back_to_paragraphs: "文档无可识别的条款标记，已按段落切分",
  sparse_text_layer_possible_scan: "文档疑似扫描件，仅提取到少量文字",
  contract_document_unreadable: "部分合同文档无法读取",
  answer_contains_unverified_references: "回答含无法核实的引用，请人工核对",
  answer_contains_unmatched_amounts: "回答含未直接出现在数据中的金额",
  contract_list_truncated: "所选合同过多，仅分析前 20 份",
};

// Presentation is deliberately the project chat's, not a lookalike: this
// reuses appendChatMessageToTarget and typeChatText from app.js so the two
// conversations are the same component rather than two that drift apart.
const contractChat = { requestToken: 0, typewriterToken: 0, typewriterTimer: null };

function cancelContractChatTypewriter() {
  contractChat.typewriterToken += 1;
  if (contractChat.typewriterTimer) {
    clearTimeout(contractChat.typewriterTimer);
    contractChat.typewriterTimer = null;
  }
}

function appendContractChatMessage(message) {
  return shared.appendChatMessageToTarget("contractChatLog", message);
}

function typeContractChatAnswer(content, requestToken) {
  // A stale reply must not overwrite a newer one, and must not keep typing
  // after the user has moved on.
  if (requestToken !== contractChat.requestToken) return;
  cancelContractChatTypewriter();
  const rendered = appendContractChatMessage({role: "assistant", content: ""});
  const token = ++contractChat.typewriterToken;
  shared.typeChatText(
    rendered.bubble,
    content || "",
    token,
    () => contractChat.typewriterToken,
    timer => { contractChat.typewriterTimer = timer; },
    () => {
      const log = document.getElementById("contractChatLog");
      log.scrollTop = log.scrollHeight;
    }
  );
}

async function sendContractChat() {
  const input = document.getElementById("chatContractInput");
  const button = document.getElementById("chatContractSend");
  const note = document.getElementById("contractChatSignals");
  const question = input.value.trim();
  if (!question || contractChat.sending) return;

  const refs = [...chatState.selected];
  if (!refs.length) {
    note.textContent = "请先在左侧勾选要分析的合同。";
    note.classList.remove("hidden");
    return;
  }

  // Echo immediately, before the request. Waiting for the round trip makes
  // the app feel unresponsive on a call that can take many seconds.
  appendContractChatMessage({role: "user", content: question});
  input.value = "";
  contractChat.sending = true;
  button.disabled = true;
  const requestToken = ++contractChat.requestToken;
  cancelContractChatTypewriter();
  note.classList.add("hidden");

  try {
    const data = await shared.api("/api/contracts/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, contract_refs: refs}),
    });
    if (requestToken !== contractChat.requestToken) return;
    typeContractChatAnswer(data.answer || "（无回答）", requestToken);
    renderChatSignals(data);
  } catch (error) {
    if (requestToken !== contractChat.requestToken) return;
    appendContractChatMessage({
      role: "assistant",
      content: "请求失败，请确认口令与所选合同后重试。",
    });
  } finally {
    contractChat.sending = false;
    button.disabled = false;
  }
}

function renderChatSignals(data) {
  const note = document.getElementById("contractChatSignals");
  const stats = data.clause_stats || {};
  const parts = (data.signals || []).map(s => CHAT_SIGNAL_LABELS[s] || s);
  // Always state the coverage, not only the problems: "3 clauses consulted"
  // is what stops a thin answer being read as a thorough one.
  parts.push(
    `已分析 ${stats.contracts ?? 0} 份合同、${stats.clauses_sent ?? 0} 条条款` +
    (stats.redacted_dropped ? `，脱敏丢弃 ${stats.redacted_dropped} 条` : "")
  );
  note.textContent = parts.join("；") + "。";
  note.classList.remove("hidden");
}
