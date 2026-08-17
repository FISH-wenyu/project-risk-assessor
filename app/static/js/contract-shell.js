/* Contract module - shell: shared state, labels, view settings, navigation.
 *
 * The contract views are three scripts, not one. They were one 1,000-line file
 * that held navigation, the ledger table, the detail modal and the chat, which
 * is the shape `main.py` had before it was broken up; splitting it now is
 * cheap and gets steadily less so.
 *
 * Load order is shell -> ledger -> chat, and it matters: `const` bindings are
 * not hoisted across scripts, so the shared state below must be evaluated
 * before either of the others runs. Calls in the other direction (the shell's
 * navigation invoking the ledger's loader) resolve fine because they happen
 * inside handlers, long after every script has run.
 *
 * Rendering rule inherited from the rest of this app: build nodes with
 * createElement/textContent, never innerHTML with server data. Contract names
 * are server data.
 */

/* Everything this module borrows from app.js, reached through one named object.
 * These used to be picked up implicitly from the global scope, so renaming one
 * in app.js failed here at runtime, inside a handler, with no test to catch it.
 *
 * Deliberately not destructured. Classic scripts share one global scope, so
 * `const { api } = ...` here is a redeclaration of app.js's `function api` and
 * throws a SyntaxError that kills this whole file - silently, since the page
 * still renders and only the contract views stop working. Reaching through
 * `shared.` also makes each borrowed call visible where it happens. */
const shared = window.RiskAgent;

const contractState = {
  rows: [],
  meta: null,
  tab: "tiered",
  loaded: false,
  // The in-flight ledger request, or null. Held here so a second caller joins
  // the running load instead of starting a competing one.
  loadPromise: null,
};

const LINK_LABELS = {
  project_linked: "已关联项目",
  orphaned: "孤儿合同",
  standalone: "无项目关联",
  link_unverified: "关联未校验",
};

const TIER_LABELS = {
  act_now: "立即处理",
  plan: "限期计划",
  monitor: "持续监控",
  record: "仅登记",
};

/* View settings. The project page's layout presets resize its list/detail
 * split; this page has no split to resize, so the equivalent affordances are
 * row density and column visibility, which is what actually helps on a
 * nine-column table. Both persist, matching the project page's behaviour. */
const CONTRACT_COLUMNS = [
  { key: "name", label: "合同名称", locked: true },
  { key: "ref", label: "合同编号" },
  { key: "amount", label: "金额" },
  { key: "risk", label: "风险等级" },
  { key: "approval", label: "审核状态" },
  { key: "execution", label: "执行状态" },
  { key: "link", label: "关联项目" },
  { key: "end", label: "到期日" },
  { key: "state", label: "处理状态" },
  { key: "owner", label: "责任人" },
  { key: "action", label: "操作", locked: true },
];
const DENSITIES = ["compact", "standard", "roomy"];

/* The one description of the ledger's filter controls.
 *
 * This list existed five times: the change-listener registrations, the reset
 * handler, the URL id list, the URL parameter map, and the summary labels.
 * Adding a filter meant editing five places, and missing one produced a
 * control that silently does not persist, or does not reset, or does not
 * appear in the collapsed summary - none of which announces itself. Same
 * failure the ledger CSV had with two column lists.
 *
 * `event` is what the control emits when the user changes it: text and date
 * inputs should re-render as you type, selects only on commit. */
const CONTRACT_FILTERS = [
  { id: "filterRiskLevel", param: "risk", label: "风险", event: "change" },
  { id: "filterExecutionStatus", param: "exec", label: "执行", event: "change" },
  { id: "filterApprovalStatus", param: "approval", label: "审核", event: "change" },
  { id: "filterOrg", param: "org", label: "组织", event: "change" },
  { id: "filterLink", param: "link", label: "关联", event: "change" },
  { id: "filterEndFrom", param: "from", label: "到期自", event: "change" },
  { id: "filterEndTo", param: "to", label: "到期至", event: "change" },
  { id: "filterAnnotation", param: "state", label: "处理状态", event: "change" },
  { id: "filterOwner", param: "owner", label: "责任人", event: "change" },
  { id: "filterSearch", param: "q", label: "搜索", event: "input" },
];

const contractView = {
  density: "standard",
  hidden: new Set(),
  sortKey: "",
  sortAsc: true,
};

// Values used for sorting. Amounts and dates must compare numerically and
// chronologically, not as strings, or "9" sorts above "10".
const SORT_VALUES = {
  name: row => String(row.contract_name || ""),
  ref: row => String(row.contract_ref || ""),
  amount: row => Number(row.total_amount) || 0,
  risk: row => Number(row.risk_score) || 0,
  approval: row => String(row.approval_status || ""),
  execution: row => String(row.execution_status || ""),
  link: row => String(row.link_status || ""),
  state: row => String(row.annotation_state || "open"),
  owner: row => String(row.owner || ""),
  // Blank dates sort last in both directions rather than pretending to be
  // 1970, which would put every undated contract at the "most urgent" end.
  end: row => String(row.end_date || "").slice(0, 10) || "9999-99-99",
};

function sortContractRows(rows) {
  const getter = SORT_VALUES[contractView.sortKey];
  if (!getter) return rows;
  const direction = contractView.sortAsc ? 1 : -1;
  return [...rows].sort((a, b) => {
    const left = getter(a);
    const right = getter(b);
    if (left < right) return -direction;
    if (left > right) return direction;
    return 0;
  });
}

function loadContractViewSettings() {
  try {
    const density = localStorage.getItem("contractDensity");
    if (DENSITIES.includes(density)) contractView.density = density;
    const hidden = JSON.parse(localStorage.getItem("contractHiddenColumns") || "[]");
    if (Array.isArray(hidden)) {
      // Locked columns can never be hidden, even via a stale stored value.
      const lockable = new Set(CONTRACT_COLUMNS.filter(c => !c.locked).map(c => c.key));
      contractView.hidden = new Set(hidden.filter(key => lockable.has(key)));
    }
    const sort = JSON.parse(localStorage.getItem("contractSort") || "null");
    if (sort && SORT_VALUES[sort.key]) {
      contractView.sortKey = sort.key;
      contractView.sortAsc = Boolean(sort.asc);
    }
  } catch (error) {
    // Corrupt storage must not break the page; fall back to defaults.
    contractView.density = "standard";
    contractView.hidden = new Set();
  }
}

function saveContractViewSettings() {
  try {
    localStorage.setItem("contractDensity", contractView.density);
    localStorage.setItem("contractHiddenColumns", JSON.stringify([...contractView.hidden]));
    localStorage.setItem("contractSort", JSON.stringify({key: contractView.sortKey, asc: contractView.sortAsc}));
  } catch (error) {
    /* storage disabled; settings simply do not persist */
  }
}

function applyContractDensity() {
  const host = document.getElementById("contractTableHost");
  for (const density of DENSITIES) host.classList.remove(`density-${density}`);
  host.classList.add(`density-${contractView.density}`);
  for (const button of document.querySelectorAll("[data-density]")) {
    button.classList.toggle("active", button.dataset.density === contractView.density);
  }
}

function renderContractViewSettings() {
  const panel = document.getElementById("contractViewSettings");
  panel.replaceChildren();
  const title = document.createElement("span");
  title.className = "small muted";
  title.textContent = "显示列";
  panel.appendChild(title);
  for (const column of CONTRACT_COLUMNS) {
    const label = document.createElement("label");
    label.className = "column-toggle";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !contractView.hidden.has(column.key);
    box.disabled = Boolean(column.locked);
    box.addEventListener("change", () => {
      if (box.checked) contractView.hidden.delete(column.key);
      else contractView.hidden.add(column.key);
      saveContractViewSettings();
      renderContractView();
    });
    const text = document.createElement("span");
    text.textContent = column.label + (column.locked ? "（固定）" : "");
    label.append(box, text);
    panel.appendChild(label);
  }
}

function visibleContractColumns() {
  return CONTRACT_COLUMNS.filter(column => !contractView.hidden.has(column.key));
}

/* ---------------------------------------------------------------- navigation */

function showView(viewId) {
  for (const section of document.querySelectorAll(".app-view")) {
    section.hidden = section.id !== viewId;
  }
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("active", button.dataset.view === viewId);
  }
  if (viewId === "viewContractChat") {
    renderChatContractList();
    renderChatMetrics();
    applyChatLayout();
  }
  if (viewId === "viewContractLedger") {
    // Render the chrome immediately. It used to appear only after a
    // successful fetch, so a failed or pending load left the page blank with
    // no breadcrumb and no title.
    renderContractBreadcrumb();
    renderContractStats(filterContractRows());
    if (!contractState.loaded) loadContractLedgerView();
  }
}

function initContractNavigation() {
  for (const button of document.querySelectorAll(".nav-item")) {
    button.addEventListener("click", () => showView(button.dataset.view));
  }
  for (const tab of document.querySelectorAll(".tab-strip .tab")) {
    tab.addEventListener("click", () => {
      contractState.tab = tab.dataset.tab;
      for (const other of document.querySelectorAll(".tab-strip .tab")) {
        other.classList.toggle("active", other === tab);
      }
      // The overview tab IS the unreachable-contract filter, so switching to
      // it changes the link filter rather than fetching anything new.
      const link = document.getElementById("filterLink");
      if (contractState.tab === "overview") {
        link.value = "";
        link.disabled = true;
      } else {
        link.disabled = false;
      }
      renderContractView();
    });
  }
  const rerender = () => renderContractView();
  for (const filter of CONTRACT_FILTERS) {
    document.getElementById(filter.id).addEventListener(filter.event, rerender);
  }
  document.getElementById("filterResetButton").addEventListener("click", resetContractFilters);
  document.getElementById("contractReloadButton").addEventListener("click", loadContractLedgerView);
  document.getElementById("contractExportButton").addEventListener("click", exportFilteredContracts);
  document.getElementById("ledgerStartChatButton").addEventListener("click", startChatFromLedger);

  for (const button of document.querySelectorAll("[data-density]")) {
    button.addEventListener("click", () => {
      contractView.density = button.dataset.density;
      saveContractViewSettings();
      applyContractDensity();
    });
  }
  document.getElementById("contractViewSettingsToggle").addEventListener("click", () => {
    const panel = document.getElementById("contractViewSettings");
    const open = panel.classList.toggle("hidden");
    document.getElementById("contractViewSettingsToggle").textContent = open ? "显示列" : "收起显示列";
  });
  document.getElementById("contractViewReset").addEventListener("click", () => {
    contractView.density = "standard";
    contractView.hidden = new Set();
    saveContractViewSettings();
    renderContractViewSettings();
    applyContractDensity();
    renderContractView();
  });

  const panel = document.getElementById("contractFilterPanel");
  readFiltersFromUrl();
  // An active filter forces the panel open regardless of the stored
  // preference. Restoring a filter from the URL into a collapsed panel would
  // show a narrowed table with the reason folded away. Read from the controls
  // rather than from what the URL carried, because a restored `tab` is not a
  // filter and must not force the panel open on its own.
  let open = activeContractFilters().length > 0;
  if (!open) {
    try { open = localStorage.getItem("contractFilterPanelOpen") === "true"; } catch (e) { open = false; }
  }
  panel.open = open;

  // Persist only what the user chose. `toggle` also fires for the assignment
  // above, and it fires as a queued task rather than synchronously, so
  // registering the listener afterwards is not enough to miss it - without
  // this guard, opening a link that carries a filter would be recorded as a
  // preference and leave the panel open on every later unfiltered visit.
  // Comparing against the state we set is ordering-independent.
  let expectedOpen = panel.open;
  panel.addEventListener("toggle", () => {
    if (panel.open === expectedOpen) return;
    expectedOpen = panel.open;
    try { localStorage.setItem("contractFilterPanelOpen", String(panel.open)); } catch (e) { /* ignore */ }
  });

  document.getElementById("savedViewSave").addEventListener("click", saveCurrentView);
  document.getElementById("savedViewName").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      saveCurrentView();
    }
  });

  loadContractViewSettings();
  renderContractViewSettings();
  renderSavedViews();
  applyContractDensity();
  initContractChatView();
}

function startChatFromLedger() {
  // Carry the current filter through, mirroring the project page where
  // "开始对话" acts on what you are looking at rather than on nothing.
  const rows = filterContractRows();
  if (rows.length) {
    chatState.selected = new Set(rows.map(row => row.contract_ref));
  }
  showView("viewContractChat");
}

function resetContractFilters() {
  for (const filter of CONTRACT_FILTERS) {
    document.getElementById(filter.id).value = "";
  }
  renderContractView();
}

