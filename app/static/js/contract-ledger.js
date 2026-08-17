/* Contract module - ledger: loading, filtering, the table, detail and export.
 *
 * Depends on contract-shell.js for `contractState`, `contractView`, the label
 * maps and the borrowed app.js helpers, so it must load after it.
 */


/* The ledger reads every active contract from the remote source database and
 * takes around twelve seconds. A static "加载中…" over that stretch is
 * indistinguishable from a hung page, so show the shape of the result and a
 * running elapsed count while the request is in flight. */
const SKELETON_ROWS = 8;
const LEDGER_EXPECTED_SECONDS = 12;

function showLedgerSkeleton(show) {
  const host = document.getElementById("contractLoadingSkeleton");
  host.classList.toggle("hidden", !show);
  if (!show) {
    host.replaceChildren();
    return;
  }
  host.replaceChildren();
  for (let index = 0; index < SKELETON_ROWS; index += 1) {
    const row = document.createElement("div");
    row.className = index === 0 ? "skeleton-row head" : "skeleton-row";
    // Stagger the sweep so the block reads as loading rather than as a
    // rendered striped table.
    row.style.animationDelay = `${index * 0.08}s`;
    host.appendChild(row);
  }
}

function loadContractLedgerView() {
  /* Re-entrancy guard. `showView` starts a load whenever the ledger has not
   * loaded yet, so leaving the view and coming back during a twelve-second
   * request starts a second one. The two then race in `finally`: whichever
   * settles first hides the skeleton and re-enables the reload button while
   * the other is still in flight, and the survivor's ticker overwrites the
   * loser's error message a second later. Observed on 2026-08-14 while
   * verifying the skeleton. Callers get the in-flight promise instead. */
  if (contractState.loadPromise) return contractState.loadPromise;
  const promise = runContractLedgerLoad().finally(() => {
    contractState.loadPromise = null;
  });
  contractState.loadPromise = promise;
  return promise;
}

/* An error next to the thing that failed, saying which failure it was. The
 * previous single "加载失败，请确认口令后重试" told an operator whose password
 * was fine to go and re-enter it. */
function showLedgerError(message) {
  const host = document.getElementById("contractLoadError");
  host.textContent = message || "";
  host.classList.toggle("hidden", !message);
}

async function runContractLedgerLoad() {
  showLedgerError("");
  const button = document.getElementById("contractReloadButton");
  const banner = document.getElementById("contractScopeBanner");
  button.disabled = true;
  document.getElementById("contractTableHost").replaceChildren();
  showLedgerSkeleton(true);
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    banner.textContent =
      `正在从源库读取全部有效合同，已用 ${seconds} 秒（通常约 ${LEDGER_EXPECTED_SECONDS} 秒）…`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const data = await shared.api("/api/contracts/ledger");
    contractState.rows = Array.isArray(data.rows) ? data.rows : [];
    contractState.meta = data;
    contractState.loaded = true;
    populateOrgFilter(contractState.rows);
    populateOwnerFilter(contractState.rows);
    renderContractView();
  } catch (error) {
    // Clear first: leaving the previous result on screen beside a failure
    // message makes stale data look current.
    contractState.rows = [];
    contractState.meta = null;
    contractState.loaded = false;
    document.getElementById("contractTableHost").replaceChildren();
    document.getElementById("contractMetrics").replaceChildren();
    renderLedgerChrome([]);
    banner.textContent = "";
    // Distinguish the two failures the operator can actually act on. "加载失败"
    // alone sent people to re-enter a password that was already correct.
    const unauthorized = /\b401\b/.test(String(error && error.message));
    showLedgerError(unauthorized
      ? "口令无效或已过期。请在页面顶部重新输入 Agent 口令后重试。"
      : "读取源库失败。源库可能不可达或查询超时，请稍后重试；若持续失败请检查 .env 中的数据库配置。");
  } finally {
    // In `finally`, so a failed load cannot leave the ticker running and keep
    // overwriting the error message with an ever-growing elapsed time.
    clearInterval(timer);
    showLedgerSkeleton(false);
    button.disabled = false;
  }
}

function populateOrgFilter(rows) {
  const select = document.getElementById("filterOrg");
  const current = select.value;
  const orgs = [...new Set(rows.map(row => String(row.org_id || "")).filter(Boolean))].sort();
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部";
  select.appendChild(all);
  for (const org of orgs) {
    const option = document.createElement("option");
    option.value = org;
    // org_id only. There is no organisation table in the read allowlist, so
    // no names and no tree are available; a flat list is the honest option.
    option.textContent = `组织 ${org}`;
    select.appendChild(option);
  }
  select.value = current;
}

/* ----------------------------------------------------------------- filtering */

const ANNOTATION_LABELS = {
  open: "待处理",
  acknowledged: "已确认",
  accepted: "已接受风险",
};

function matchesAnnotationFilter(row, choice) {
  // `stale` and `unassigned` are questions about the annotation rather than
  // states it can hold, so they are answered here rather than by equality.
  if (choice === "stale") return Boolean(row.annotation_stale);
  if (choice === "unassigned") return !String(row.owner || "");
  // A stale acknowledgement reports as open, matching the server's own view:
  // the decision on record no longer covers the score the row now carries.
  return String(row.annotation_state || "open") === choice;
}

function populateOwnerFilter(rows) {
  const select = document.getElementById("filterOwner");
  const current = select.value;
  const owners = [...new Set(rows.map(row => String(row.owner || "")).filter(Boolean))].sort();
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部";
  select.appendChild(all);
  for (const owner of owners) {
    const option = document.createElement("option");
    option.value = owner;
    option.textContent = owner;
    select.appendChild(option);
  }
  // Keep a chosen owner selected even after a reload drops them from the list,
  // or the table silently widens without the filter control changing.
  select.value = owners.includes(current) ? current : "";
}

function filterContractRows() {
  const riskLevel = document.getElementById("filterRiskLevel").value;
  const execution = document.getElementById("filterExecutionStatus").value;
  const approval = document.getElementById("filterApprovalStatus").value;
  const org = document.getElementById("filterOrg").value;
  const link = document.getElementById("filterLink").value;
  const endFrom = document.getElementById("filterEndFrom").value;
  const endTo = document.getElementById("filterEndTo").value;
  const annotation = document.getElementById("filterAnnotation").value;
  const owner = document.getElementById("filterOwner").value;
  const search = document.getElementById("filterSearch").value.trim().toLowerCase();

  return contractState.rows.filter(row => {
    if (riskLevel && row.risk_level !== riskLevel) return false;
    if (execution && row.execution_status !== execution) return false;
    if (approval && row.approval_status !== approval) return false;
    if (org && String(row.org_id) !== org) return false;
    if (link && row.link_status !== link) return false;
    // The overview tab is defined as everything the project entry cannot see.
    if (contractState.tab === "overview"
        && !["standalone", "orphaned"].includes(row.link_status)) return false;
    const end = String(row.end_date || "").slice(0, 10);
    if (endFrom && (!end || end < endFrom)) return false;
    if (endTo && (!end || end > endTo)) return false;
    if (annotation && !matchesAnnotationFilter(row, annotation)) return false;
    if (owner && String(row.owner || "") !== owner) return false;
    if (search) {
      const haystack = `${row.contract_name || ""} ${row.contract_ref || ""}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}

/* ----------------------------------------------------------------- rendering */

function activeContractFilters() {
  const active = [];
  for (const filter of CONTRACT_FILTERS) {
    const element = document.getElementById(filter.id);
    const value = (element?.value || "").trim();
    if (!value) continue;
    // A disabled control is not filtering anything, whatever value it holds:
    // the overview tab disables the link filter, and reporting it as active
    // would claim a filter the user cannot see or clear.
    if (element.disabled) continue;
    // A select can name its own value; a date or text input cannot.
    const text = element.tagName === "SELECT"
      ? element.options[element.selectedIndex]?.textContent?.trim() || value
      : value;
    active.push(`${filter.label}=${text}`);
  }
  return active;
}

/* "没有数据" as one grey line reads as a broken page. An empty state has to say
 * which of the three situations this is, because the action differs: nothing
 * loaded yet, the filter excluded everything, or the source genuinely has no
 * contracts. The reset button only appears in the middle case. */
function renderContractEmptyState(rows) {
  const host = document.getElementById("contractEmptyHint");
  host.classList.toggle("hidden", rows.length > 0);
  if (rows.length) return;

  const title = document.getElementById("contractEmptyTitle");
  const body = document.getElementById("contractEmptyBody");
  const reset = document.getElementById("contractEmptyReset");
  const active = activeContractFilters();

  if (!contractState.loaded) {
    title.textContent = "尚未加载台账";
    body.textContent = "点击「加载数据」从源库读取合同。首次加载约需 12 秒。";
    reset.classList.add("hidden");
  } else if (active.length) {
    title.textContent = "没有符合当前筛选的合同";
    body.textContent = `共 ${contractState.rows.length} 份合同，当前筛选（${active.join(" · ")}）没有匹配项。`;
    reset.classList.remove("hidden");
  } else {
    title.textContent = "源库中没有活跃合同";
    body.textContent = "这不是筛选造成的，当前视图未设置任何筛选条件。";
    reset.classList.add("hidden");
  }
}

/* The chrome that describes the current result set: the filter summary and the
 * export button's scope. Called from the success path AND the failure path,
 * because a failed load used to leave the export button enabled and labelled
 * "导出 CSV" with no count, next to an empty table - it did nothing when
 * pressed, which reads as a broken button rather than an empty result. */
function renderLedgerChrome(rows) {
  const panel = document.getElementById("contractFilterPanel");
  const summary = document.getElementById("contractFilterSummary");
  const total = contractState.rows.length;
  const active = activeContractFilters();
  panel.classList.toggle("is-filtered", active.length > 0);
  summary.textContent = active.length
    ? `${active.join(" · ")}　→　${rows.length} / ${total} 条`
    : `全部合同（${total} 条）`;

  // Name the scope on the button. "导出 CSV" beside a filtered table does not
  // say whether it exports the filter or everything, and the answer matters.
  const exportButton = document.getElementById("contractExportButton");
  exportButton.textContent = rows.length === total
    ? `导出 CSV（全部 ${rows.length} 条）`
    : `导出 CSV（当前筛选 ${rows.length} 条）`;
  exportButton.disabled = !rows.length;

  renderUndatedWarning();
}

/* A date filter excludes every contract with no 到期日, which is not a corner
 * case here: most live contracts have no end date, so filtering by date
 * silently hides nearly two thirds of the ledger. Excluding them is right -
 * a contract with no date does not fall in a range - but doing it without
 * saying so is not. */
function renderUndatedWarning() {
  const note = document.getElementById("contractDateFilterNote");
  const filtering = Boolean(document.getElementById("filterEndFrom").value
    || document.getElementById("filterEndTo").value);
  const undated = contractState.rows.filter(
    row => !String(row.end_date || "").trim()
  ).length;
  const show = filtering && undated > 0;
  note.classList.toggle("hidden", !show);
  if (show) {
    note.textContent =
      `另有 ${undated} 份合同没有到期日，不在日期筛选范围内（清空日期即可看到）。`;
  }
}

function renderContractView() {
  writeFiltersToUrl();
  const rows = filterContractRows();
  renderContractBreadcrumb();
  renderContractStats(rows);
  const host = document.getElementById("contractTableHost");
  host.replaceChildren();

  renderContractEmptyState(rows);
  renderLedgerChrome(rows);

  applyContractDensity();

  if (contractState.tab === "overview") {
    renderOverviewCards(host, rows);
  } else if (contractState.tab === "org") {
    renderGrouped(host, rows, row => `组织 ${row.org_id || "未知"}`);
  } else {
    renderGrouped(host, rows, row => TIER_LABELS[row.tier] || row.tier);
  }
}

function renderContractBreadcrumb() {
  const crumb = document.getElementById("contractBreadcrumb");
  const parts = ["合同风险评估", "合同风险台账"];
  if (contractState.tab === "overview") parts.push("合同风险总览");
  crumb.textContent = parts.join(" › ");
  document.getElementById("contractViewTitle").textContent =
    contractState.tab === "overview" ? "合同风险总览" : "合同风险台账";
}

/* Six resident metric cards cost about 90px of vertical space above a table
 * the operator came here to read, and after the first glance they are
 * reference numbers rather than working data. Collapsed to one line by
 * default, expandable to the full cards, and the choice persists.
 *
 * The strip is not a summary of the cards - it IS the cards, laid out inline,
 * so nothing is hidden in the collapsed state. */
function renderMetricStrip(host, cards, storageKey) {
  let expanded = false;
  try { expanded = localStorage.getItem(storageKey) === "1"; } catch (_) {}

  host.replaceChildren();
  host.classList.toggle("metrics-collapsed", !expanded);

  if (expanded) {
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
  } else {
    const strip = document.createElement("div");
    strip.className = "metric-strip";
    for (const [label, value] of cards) {
      const item = document.createElement("span");
      item.className = "metric-strip-item";
      const name = document.createElement("span");
      name.className = "muted";
      name.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = String(value);
      item.append(name, strong);
      strip.appendChild(item);
    }
    host.appendChild(strip);
  }

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "link metric-strip-toggle";
  toggle.textContent = expanded ? "收起指标" : "展开指标";
  toggle.addEventListener("click", () => {
    try { localStorage.setItem(storageKey, expanded ? "0" : "1"); } catch (_) {}
    renderMetricStrip(host, cards, storageKey);
  });
  host.appendChild(toggle);
}

function renderContractStats(rows) {
  const meta = contractState.meta || {};
  const coverage = meta.project_entry_coverage || {};
  const tiers = meta.by_tier || {};

  // Metric cards in the same .metrics style as the project page, so the two
  // modules read as one product rather than two.
  const cards = contractState.tab === "overview"
    ? [
        ["当前筛选", rows.length],
        ["无项目关联", rows.filter(r => r.link_status === "standalone").length],
        ["孤儿合同", rows.filter(r => r.link_status === "orphaned").length],
        ["项目入口可触达", `${coverage.reachable ?? 0}/${coverage.total ?? 0}`],
        ["立即处理", rows.filter(r => r.tier === "act_now").length],
        ["命中总数", rows.reduce((sum, r) => sum + (Number(r.finding_count) || 0), 0)],
      ]
    : [
        ["当前筛选", rows.length],
        ["合同总数", meta.contract_total ?? "-"],
        // Counted over the FILTERED rows, not the whole ledger: these cards sit
        // above the table and have to describe what is in it. The other cards
        // are ledger-wide totals and are labelled as such.
        ["待处理", rows.filter(row => row.needs_attention !== false).length],
        ["已确认", rows.filter(row => row.needs_attention === false).length],
        ["立即处理", tiers.act_now ?? 0],
        ["确认已失效", rows.filter(row => row.annotation_stale).length],
      ];

  renderMetricStrip(document.getElementById("contractMetrics"), cards, "contractMetricsExpanded");
  const banner = document.getElementById("contractScopeBanner");
  if (!contractState.loaded) {
    banner.textContent = "尚未加载，点击右上角「加载数据」。";
    return;
  }
  banner.textContent = contractState.tab === "overview"
    ? `本视图只显示按项目入口无法触达的合同：无项目关联与孤儿合同。`
      + `全部 ${coverage.total ?? 0} 份合同中，按项目入口仅能触达 ${coverage.reachable ?? 0} 份。`
    : `覆盖全部 ${coverage.total ?? 0} 份有效合同。组织以 org_id 标识（读表白名单内没有组织表，`
      + `因此没有组织名称与层级）。结果基于合同元数据，未解析合同正文。`;
}

function renderGrouped(host, rows, keyOf) {
  const groups = new Map();
  for (const row of rows) {
    const key = keyOf(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  for (const [key, groupRows] of groups) {
    const details = document.createElement("details");
    details.className = "contract-group";
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = `${key}（${groupRows.length}）`;
    details.appendChild(summary);
    details.appendChild(buildContractTable(groupRows));
    host.appendChild(details);
  }
}

function renderOverviewCards(host, rows) {
  const buckets = [
    ["无项目关联合同", rows.filter(r => r.link_status === "standalone")],
    ["孤儿合同（关联项目已不存在）", rows.filter(r => r.link_status === "orphaned")],
  ];
  for (const [title, bucketRows] of buckets) {
    const card = document.createElement("section");
    card.className = "overview-card";
    const heading = document.createElement("h3");
    heading.textContent = `${title} · ${bucketRows.length} 份`;
    card.appendChild(heading);
    if (!bucketRows.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = "无符合条件的合同。";
      card.appendChild(empty);
    } else {
      card.appendChild(buildContractTable(bucketRows));
    }
    host.appendChild(card);
  }
}

function buildContractTable(rows) {
  rows = sortContractRows(rows);
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  // Deliberately NOT `project-table`. That class carries table-layout:fixed
  // plus per-column pixel widths keyed to the PROJECT column order, so the
  // contract name inherited the 52px checkbox width and the code inherited
  // the 78px id width, making the two columns overlap.
  table.className = "contract-table";
  const columns = visibleContractColumns();

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    // Sizing is keyed to this class, not to the cell's position, so hiding a
    // column cannot make the rules address a different one.
    th.className = `col-${column.key}`;
    if (column.key === "action") {
      th.textContent = column.label;
    } else {
      // Sortable. Scanning every row for "the most expensive" or "the soonest to
      // expire" by eye is exactly the work a table should do for you.
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sort-header";
      const active = contractView.sortKey === column.key;
      button.textContent = column.label + (active ? (contractView.sortAsc ? " ▲" : " ▼") : "");
      button.addEventListener("click", () => {
        if (contractView.sortKey === column.key) contractView.sortAsc = !contractView.sortAsc;
        else { contractView.sortKey = column.key; contractView.sortAsc = true; }
        saveContractViewSettings();
        renderContractView();
      });
      th.appendChild(button);
    }
    headRow.appendChild(th);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement("tbody");
  for (const row of rows) {
    body.appendChild(buildContractRow(row, columns));
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

function buildContractRow(row, columns) {
  const tr = document.createElement("tr");
  for (const column of columns) {
    const cell = buildContractCell(row, column.key);
    cell.classList.add(`col-${column.key}`);
    tr.appendChild(cell);
  }
  return tr;
}

function buildContractCell(row, key) {
  if (key === "name") {
    const td = document.createElement("td");
    const link = document.createElement("button");
    link.type = "button";
    link.className = "link-button";
    link.textContent = row.contract_name || "(未命名)";
    link.addEventListener("click", () => openContractDetail(row));
    td.appendChild(link);
    return td;
  }
  if (key === "ref") return textCell(String(row.contract_ref || "").replace(/^code:|^id:/, ""));
  if (key === "amount") return textCell(formatContractAmount(row.total_amount));
  if (key === "risk") return badgeCell(row.risk_level, shared.riskClass(row.risk_level));
  if (key === "approval") return textCell(row.approval_status);
  if (key === "execution") return textCell(row.execution_status);
  if (key === "link") {
    return textCell(
      row.link_status === "project_linked"
        ? `项目 ${row.project_id}`
        : (LINK_LABELS[row.link_status] || row.link_status)
    );
  }
  if (key === "end") return textCell(String(row.end_date || "").slice(0, 10) || "-");
  if (key === "state") {
    const td = document.createElement("td");
    const state = String(row.annotation_state || "open");
    const badge = document.createElement("span");
    badge.className = `annotation-badge state-${state}`;
    badge.textContent = ANNOTATION_LABELS[state] || state;
    td.appendChild(badge);
    if (row.annotation_stale) {
      // A stale acknowledgement is not the same as never having been looked
      // at, and the difference is what tells someone to re-check rather than
      // start from scratch.
      const stale = document.createElement("span");
      stale.className = "annotation-stale";
      stale.textContent = "确认已失效";
      stale.title = "风险分已高于确认时的分数，该确认不再覆盖当前风险";
      td.appendChild(stale);
    }
    return td;
  }
  if (key === "owner") return textCell(row.owner || "未指派");
  if (key === "action") {
    const td = document.createElement("td");
    const view = document.createElement("button");
    view.type = "button";
    view.className = "secondary small-button";
    view.textContent = "查看详情";
    view.addEventListener("click", () => openContractDetail(row));
    td.appendChild(view);
    return td;
  }
  return textCell("");
}

function textCell(value) {
  const td = document.createElement("td");
  td.textContent = value == null || value === "" ? "-" : String(value);
  return td;
}

function badgeCell(value, className) {
  const td = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `badge ${className}`;
  badge.textContent = value || "-";
  td.appendChild(badge);
  return td;
}

function formatContractAmount(value) {
  const amount = Number(value);
  // Most contracts carry no amount, so "missing" needs to read as a fact
  // rather than as zero yuan.
  if (!value || Number.isNaN(amount) || amount === 0) return "未填写";
  return `¥${amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/* -------------------------------------------------------------- detail modal */

function openContractDetail(row) {
  const modal = document.getElementById("contractDetailModal");
  document.getElementById("contractDetailTitle").textContent = row.contract_name || "(未命名合同)";

  const grid = document.getElementById("contractDetailGrid");
  grid.replaceChildren();
  const fields = [
    ["合同编号", String(row.contract_ref || "").replace(/^code:|^id:/, "")],
    ["合同金额", formatContractAmount(row.total_amount)],
    ["风险等级", `${row.risk_level || "-"}（${row.risk_score ?? "-"} 分）`],
    ["处理层级", TIER_LABELS[row.tier] || row.tier || "-"],
    ["审核状态", row.approval_status || "-"],
    ["执行状态", row.execution_status || "-"],
    ["所属组织", row.org_id ? `组织 ${row.org_id}` : "未知"],
    ["关联状态", LINK_LABELS[row.link_status] || row.link_status || "-"],
    ["关联项目", row.link_status === "project_linked" ? `项目 ${row.project_id}` : "无"],
    ["签订日期", String(row.sign_date || "").slice(0, 10) || "未填写"],
    ["到期日期", String(row.end_date || "").slice(0, 10) || "未填写"],
    ["命中规则数", row.finding_count ?? 0],
  ];
  for (const [label, value] of fields) {
    const cell = document.createElement("div");
    cell.className = "detail-field";
    const dt = document.createElement("span");
    dt.className = "muted small";
    dt.textContent = label;
    const dd = document.createElement("strong");
    dd.textContent = String(value);
    cell.append(dt, dd);
    grid.appendChild(cell);
  }

  // Full hit list. Showing only the top reason left the reader unable to see
  // which other rules fired, which is the main question a detail view answers.
  const list = document.getElementById("contractDetailFindings");
  list.replaceChildren();
  const findings = Array.isArray(row.findings) ? row.findings : [];
  if (!findings.length) {
    const none = document.createElement("p");
    none.className = "muted small";
    none.textContent = "未命中任何风险规则。";
    list.appendChild(none);
  } else {
    for (const finding of [...findings].sort((a, b) => (b.severity || 0) - (a.severity || 0))) {
      const item = document.createElement("div");
      item.className = "finding-row";
      const badge = document.createElement("span");
      // Reuses the existing helper so contract severity colours match the
      // ones already used in the project risk modal.
      badge.className = `contract-severity ${shared.contractSeverityClass(finding.severity)}`;
      badge.textContent = String(finding.severity ?? "-");
      const body = document.createElement("div");
      const reasonText = document.createElement("strong");
      reasonText.textContent = finding.reason || finding.rule || "";
      const evidence = document.createElement("span");
      evidence.className = "muted small";
      evidence.textContent = finding.evidence || "";
      body.append(reasonText, document.createElement("br"), evidence);
      item.append(badge, body);
      list.appendChild(item);
    }
  }

  const reason = document.getElementById("contractDetailReason");
  reason.textContent = findings.length
    ? `共命中 ${findings.length} 条规则，最高严重度 ${Math.max(...findings.map(f => f.severity || 0))}。`
    : "未命中风险规则。";

  // Say what is NOT here, so an empty section is not read as "no problems".
  document.getElementById("contractDetailNote").textContent =
    "本弹窗展示合同元数据与规则命中。合同条款、履约记录、签约方等字段未读取："
    + "条款需要解析合同正文，签约方等自由文本在脱敏边界之外。";

  renderAnnotationPanel(row);

  modal.hidden = false;
}

/* ------------------------------------------------------------- annotations */

// The row the annotation panel is currently editing. Held rather than passed,
// because the save handler is registered once at startup and fires long after
// the modal was opened.
let annotationTarget = null;

function renderAnnotationPanel(row) {
  annotationTarget = row;
  const annotation = row.annotation || {};
  document.getElementById("annotationState").value = annotation.state || "open";
  document.getElementById("annotationOwner").value = annotation.owner || "";
  document.getElementById("annotationNote").value = annotation.note || "";
  const status = document.getElementById("annotationStatus");
  if (row.annotation_stale) {
    status.textContent =
      `此前在 ${annotation.acknowledged_score} 分时确认，当前为 ${row.risk_score} 分，`
      + "该确认已不覆盖当前风险，请重新判断。";
  } else if (annotation.updated_at) {
    status.textContent = `最后更新：${annotation.updated_at}（${annotation.updated_by || "本地"}）`;
  } else {
    status.textContent = "尚未记录处理状态。";
  }
  loadAnnotationHistory(row.contract_ref);
}

async function loadAnnotationHistory(contractRef) {
  const host = document.getElementById("annotationHistory");
  host.replaceChildren();
  try {
    const data = await shared.api(`/api/contracts/annotations/${encodeURIComponent(contractRef)}`);
    const history = Array.isArray(data.history) ? data.history : [];
    if (!history.length) return;
    const title = document.createElement("p");
    title.className = "muted small";
    title.textContent = "处理记录";
    host.appendChild(title);
    for (const entry of history.slice(0, 8)) {
      const line = document.createElement("div");
      line.className = "annotation-history-row";
      // textContent throughout: the note is operator free text.
      const when = document.createElement("span");
      when.className = "muted small";
      when.textContent = entry.recorded_at || "";
      const what = document.createElement("span");
      what.textContent =
        `${ANNOTATION_LABELS[entry.state] || entry.state}`
        + `${entry.owner ? " · " + entry.owner : ""}`
        + `${entry.note ? " · " + entry.note : ""}`;
      line.append(when, what);
      host.appendChild(line);
    }
  } catch (error) {
    const failed = document.createElement("p");
    failed.className = "muted small";
    failed.textContent = "处理记录加载失败。";
    host.appendChild(failed);
  }
}

async function saveAnnotation() {
  if (!annotationTarget) return;
  const button = document.getElementById("annotationSave");
  const status = document.getElementById("annotationStatus");
  button.disabled = true;
  status.textContent = "保存中…";
  try {
    const body = {
      contract_ref: annotationTarget.contract_ref,
      state: document.getElementById("annotationState").value,
      owner: document.getElementById("annotationOwner").value,
      note: document.getElementById("annotationNote").value,
      // The score at the moment of the decision, so a later increase can
      // invalidate it rather than letting the row stay quietly dismissed.
      risk_score: annotationTarget.risk_score,
    };
    const data = await shared.api("/api/contracts/annotations", {
      method: "POST",
      body: JSON.stringify(body),
    });
    // Update the loaded row in place instead of refetching the whole ledger,
    // which is a twelve-second round trip against the source database.
    applyAnnotationToRow(annotationTarget, data.annotation);
    populateOwnerFilter(contractState.rows);
    renderContractView();
    renderAnnotationPanel(annotationTarget);
    status.textContent = "已保存（仅本地）。";
  } catch (error) {
    status.textContent = "保存失败，请重试。";
  } finally {
    button.disabled = false;
  }
}

function applyAnnotationToRow(row, annotation) {
  row.annotation = annotation || null;
  const state = annotation ? String(annotation.state || "open") : "open";
  const stale = Boolean(annotation && annotation.stale);
  row.annotation_stale = stale;
  row.annotation_state = state !== "open" && stale ? "open" : state;
  row.owner = annotation ? String(annotation.owner || "") : "";
  row.needs_attention = row.annotation_state === "open";
}

function closeContractDetail() {
  document.getElementById("contractDetailModal").hidden = true;
  annotationTarget = null;
}

/* -------------------------------------------------------------------- export */

async function exportFilteredContracts() {
  const rows = filterContractRows();
  if (!rows.length) return;
  // The column list comes from the server, not from a copy kept here. Two
  // hand-maintained lists drift, and a CSV whose shape depends on which export
  // button you pressed is worse than no filtered export at all.
  const columns = contractState.meta?.csv_columns;
  if (!Array.isArray(columns) || !columns.length) {
    document.getElementById("contractScopeBanner").textContent =
      "导出失败：服务端未提供列定义，请重新加载台账。";
    return;
  }
  const escape = value => {
    const text = value == null ? "" : String(value);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [columns.join(",")];
  for (const row of rows) {
    lines.push(columns.map(column => escape(row[column])).join(","));
  }
  // Exports what is on screen, not what the server holds, because the filter
  // lives in the browser; the button says which. The BOM keeps Excel from
  // mangling Chinese, matching what the server export sends.
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "contract-ledger-filtered.csv";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

document.addEventListener("DOMContentLoaded", () => {
  initContractNavigation();
  document.getElementById("contractDetailClose").addEventListener("click", closeContractDetail);
  document.getElementById("contractDetailModal").addEventListener("click", event => {
    if (event.target.id === "contractDetailModal") closeContractDetail();
  });
  document.getElementById("annotationSave").addEventListener("click", saveAnnotation);
  document.getElementById("contractEmptyReset").addEventListener("click", resetContractFilters);
  document.getElementById("contractDetailAnalyze").addEventListener("click", () => {
    // Carry the contract across. Landing on an empty selection meant the user
    // had to find, in a list of 65, the row they had just been reading.
    const target = annotationTarget;
    closeContractDetail();
    if (target && target.contract_ref) {
      chatState.selected.clear();
      chatState.selected.add(target.contract_ref);
    }
    showView("viewContractChat");
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeContractDetail();
  });
});

/* ======================================================== saved views
 * The URL already makes a filter shareable; this makes it repeatable. Stored
 * locally, because a "view" is one person's habit rather than shared
 * configuration, and because putting it on the server would mean deciding
 * whose view it is - this app has no user identity.
 */

const SAVED_VIEWS_KEY = "contractSavedViews";
const MAX_SAVED_VIEWS = 12;
const MAX_VIEW_NAME_CHARS = 40;

function loadSavedViews() {
  try {
    const raw = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    // Filter to the shape we expect rather than trusting storage: this value
    // survives deploys, so an older or hand-edited entry must not throw during
    // render and take the whole filter bar down with it.
    return raw
      .filter(item => item && typeof item.name === "string" && item.filters && typeof item.filters === "object")
      .slice(0, MAX_SAVED_VIEWS);
  } catch (error) {
    return [];
  }
}

function persistSavedViews(views) {
  try {
    localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views.slice(0, MAX_SAVED_VIEWS)));
  } catch (error) {
    /* storage disabled; views simply do not persist */
  }
}

function currentFilterSnapshot() {
  const filters = {};
  for (const filter of CONTRACT_FILTERS) {
    const value = (document.getElementById(filter.id)?.value || "").trim();
    if (value) filters[filter.id] = value;
  }
  return { filters, tab: contractState.tab };
}

function saveCurrentView() {
  const input = document.getElementById("savedViewName");
  const name = input.value.trim().slice(0, MAX_VIEW_NAME_CHARS);
  if (!name) {
    input.focus();
    return;
  }
  const snapshot = currentFilterSnapshot();
  const views = loadSavedViews().filter(view => view.name !== name);
  views.unshift({ name, ...snapshot });
  persistSavedViews(views);
  input.value = "";
  renderSavedViews();
}

function applySavedView(view) {
  // Clear first: applying a view must replace the current filter, not merge
  // with it, or the result is neither the saved view nor what was there.
  for (const filter of CONTRACT_FILTERS) {
    const element = document.getElementById(filter.id);
    if (element) element.value = "";
  }
  for (const [id, value] of Object.entries(view.filters || {})) {
    const element = document.getElementById(id);
    if (element) element.value = value;
  }
  if (view.tab && ["tiered", "org", "overview"].includes(view.tab)) {
    contractState.tab = view.tab;
    for (const button of document.querySelectorAll(".tab-strip .tab")) {
      button.classList.toggle("active", button.dataset.tab === view.tab);
    }
    document.getElementById("filterLink").disabled = view.tab === "overview";
  }
  renderContractView();
}

function renderSavedViews() {
  const host = document.getElementById("savedViewList");
  host.replaceChildren();
  const views = loadSavedViews();
  if (!views.length) {
    const empty = document.createElement("span");
    empty.className = "small muted";
    empty.textContent = "（尚未保存）";
    host.appendChild(empty);
    return;
  }
  for (const view of views) {
    const chip = document.createElement("span");
    chip.className = "saved-view-chip";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "link-button";
    // textContent: the name is typed by the operator.
    apply.textContent = view.name;
    apply.title = describeSavedView(view);
    apply.addEventListener("click", () => applySavedView(view));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "saved-view-remove";
    remove.textContent = "×";
    remove.title = "删除该视图";
    remove.setAttribute("aria-label", `删除视图 ${view.name}`);
    remove.addEventListener("click", () => {
      persistSavedViews(loadSavedViews().filter(item => item.name !== view.name));
      renderSavedViews();
    });
    chip.append(apply, remove);
    host.appendChild(chip);
  }
}

function describeSavedView(view) {
  const labels = CONTRACT_FILTERS
    .filter(filter => view.filters?.[filter.id])
    .map(filter => `${filter.label}=${savedViewValueLabel(filter, view.filters[filter.id])}`);
  return labels.length ? labels.join(" · ") : "无筛选条件";
}

function savedViewValueLabel(filter, value) {
  // Show what the control shows. A tooltip reading "处理状态=open" describes
  // the stored value rather than the choice the user made.
  const element = document.getElementById(filter.id);
  if (element && element.tagName === "SELECT") {
    const option = [...element.options].find(item => item.value === value);
    if (option) return option.textContent.trim();
  }
  return value;
}

/* ===================================================== filters in the URL
 * A carefully built filter used to vanish on reload and could not be sent to a
 * colleague. The filter state now lives in the query string, which also makes
 * the active view bookmarkable.
 */

function writeFiltersToUrl() {
  const params = new URLSearchParams();
  for (const filter of CONTRACT_FILTERS) {
    const value = (document.getElementById(filter.id)?.value || "").trim();
    if (value) params.set(filter.param, value);
  }
  if (contractState.tab !== "tiered") params.set("tab", contractState.tab);
  const query = params.toString();
  // replaceState, not pushState: typing in the search box must not create a
  // history entry per keystroke.
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

function readFiltersFromUrl() {
  const params = new URLSearchParams(location.search);
  let applied = false;
  for (const filter of CONTRACT_FILTERS) {
    const value = params.get(filter.param);
    const element = document.getElementById(filter.id);
    if (value !== null && element) {
      element.value = value;
      applied = true;
    }
  }
  const tab = params.get("tab");
  if (tab && ["tiered", "org", "overview"].includes(tab)) {
    contractState.tab = tab;
    for (const button of document.querySelectorAll(".tab-strip .tab")) {
      button.classList.toggle("active", button.dataset.tab === tab);
    }
    document.getElementById("filterLink").disabled = tab === "overview";
    applied = true;
  }
  return applied;
}

