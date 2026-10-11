// Review desk: plain JavaScript talking to the FastAPI backend with fetch().

const METHOD_LABELS = {
  xml: "XML e-invoice",
  xml_embedded: "ZUGFeRD PDF",
  llm_text: "PDF read by AI",
  llm_vision: "Scan read by AI",
  failed: "Could not be read",
};
const STATUS_LABELS = { approved: "Approved", needs_review: "To review", rejected: "Rejected" };
const HEADER_FIELDS = [
  "document_type", "supplier_name", "supplier_vat_id", "buyer_name", "invoice_number",
  "invoice_date", "due_date", "po_number", "currency", "net_total", "vat_total", "gross_total",
];
const NUMBER_FIELDS = ["net_total", "vat_total", "gross_total"];

const state = { status: "needs_review", selectedId: null };
const money = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
const $ = (selector, root = document) => root.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 5000);
}

function formatMoney(value, currency) {
  if (value === null || value === undefined) return "";
  if (currency && currency !== "EUR") return `${value.toFixed(2)} ${currency}`;
  return money.format(value);
}

function parseNumber(text) {
  const value = String(text ?? "").trim().replace(/[€\s]/g, "");
  if (!value) return null;
  const normalised = value.includes(",") ? value.replace(/\./g, "").replace(",", ".") : value;
  const number = Number(normalised);
  return Number.isFinite(number) ? number : null;
}

// --- Stats and list -----------------------------------------------------------------

async function loadStats() {
  const stats = await api("/api/stats");
  const auto = Math.round(stats.auto_approval_rate * 100);
  $("#stats-line").textContent = stats.total
    ? `${stats.total} documents processed. ${auto}% approved automatically, ${stats.awaiting_review} waiting for review.`
    : "No documents yet. Upload invoices to get started.";
  for (const status of Object.keys(STATUS_LABELS)) {
    const count = stats.by_status[status] || 0;
    $(`#count-${status}`).textContent = count ? `(${count})` : "";
  }
}

async function loadList() {
  const query = state.status ? `?status=${state.status}` : "";
  const invoices = await api(`/api/invoices${query}`);
  const list = $("#invoice-list");
  list.innerHTML = "";
  if (!invoices.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = state.status === "needs_review"
      ? "Nothing to review. Everything that came in was clean."
      : "No documents here yet.";
    list.append(empty);
    return invoices;
  }
  for (const invoice of invoices) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = invoice.id;
    if (invoice.id === state.selectedId) button.setAttribute("aria-current", "true");

    const who = document.createElement("span");
    who.className = "who";
    who.textContent = invoice.supplier_name || invoice.original_filename;
    const amount = document.createElement("span");
    amount.className = "amount";
    amount.textContent = formatMoney(invoice.gross_total, invoice.currency);
    const sub = document.createElement("span");
    sub.className = "sub";
    const parts = [invoice.invoice_number, invoice.invoice_date, METHOD_LABELS[invoice.extraction_method]].filter(Boolean);
    sub.textContent = parts.join(", ");
    if (!state.status) {
      const mark = document.createElement("span");
      mark.className = `mark ${invoice.status}`;
      mark.textContent = STATUS_LABELS[invoice.status];
      sub.append(" ", mark);
    }
    button.append(who, amount, sub);
    if (invoice.status === "needs_review" && invoice.issues.length) {
      const reason = document.createElement("span");
      reason.className = "reason";
      reason.textContent = invoice.issues[0].message + (invoice.issues.length > 1 ? ` (+${invoice.issues.length - 1} more)` : "");
      button.append(reason);
    }
    button.addEventListener("click", () => selectInvoice(invoice.id));
    item.append(button);
    list.append(item);
  }
  return invoices;
}

async function refresh() {
  await Promise.all([loadStats(), loadList()]);
}

// --- Detail ---------------------------------------------------------------------------

function addLineRow(tbody, line = {}) {
  const row = document.createElement("tr");
  for (const key of ["description", "quantity", "unit_price", "vat_rate", "line_total"]) {
    const cell = document.createElement("td");
    const input = document.createElement("input");
    input.name = key;
    input.value = line[key] ?? "";
    input.setAttribute("aria-label", key.replace("_", " "));
    if (key !== "description") input.inputMode = "decimal";
    cell.append(input);
    row.append(cell);
  }
  const removeCell = document.createElement("td");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-line";
  remove.setAttribute("aria-label", "Remove line");
  remove.textContent = "×";
  remove.addEventListener("click", () => row.remove());
  removeCell.append(remove);
  row.append(removeCell);
  tbody.append(row);
}

async function renderViewer(frame, invoice) {
  const url = `/api/invoices/${invoice.id}/file`;
  const name = invoice.original_filename.toLowerCase();
  if (name.endsWith(".pdf")) {
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.title = `Original document ${invoice.original_filename}`;
    frame.append(iframe);
  } else if (name.endsWith(".xml")) {
    const pre = document.createElement("pre");
    pre.textContent = await (await fetch(url)).text();
    frame.append(pre);
  } else {
    const img = document.createElement("img");
    img.src = url;
    img.alt = `Scan of ${invoice.original_filename}`;
    frame.append(img);
  }
}

function showStamp(stamp, invoice, animate) {
  stamp.hidden = false;
  stamp.classList.toggle("rejected", invoice.status === "rejected");
  stamp.firstChild.textContent = invoice.status === "rejected" ? "Abgelehnt" : "Geprüft";
  $("small", stamp).textContent = invoice.status === "rejected" ? "rejected" : (invoice.erp_reference || "approved");
  if (animate) {
    stamp.classList.remove("press");
    void stamp.offsetWidth; // restart the animation
    stamp.classList.add("press");
  }
}

async function selectInvoice(id, { animateStamp = false } = {}) {
  state.selectedId = id;
  document.querySelectorAll(".invoice-list button").forEach((button) => {
    if (Number(button.dataset.id) === id) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });

  const invoice = await api(`/api/invoices/${id}`);
  const detail = $("#detail");
  detail.innerHTML = "";
  detail.append($("#detail-template").content.cloneNode(true));
  const form = $("form.review", detail);

  $(".doc-title", detail).textContent = invoice.supplier_name || invoice.original_filename;
  $(".doc-meta", detail).textContent =
    `${invoice.original_filename}. ${METHOD_LABELS[invoice.extraction_method] || invoice.extraction_method}` +
    (invoice.processing_ms != null ? ` in ${(invoice.processing_ms / 1000).toFixed(1)} s.` : ".") +
    ` Status: ${STATUS_LABELS[invoice.status]}.`;

  await renderViewer($(".viewer-frame", detail), invoice);

  // Issues, and highlight the fields they point at.
  const issues = $(".issues", detail);
  if (!invoice.issues.length) {
    const ok = document.createElement("li");
    ok.className = "ok";
    ok.textContent = invoice.status === "approved" ? "All checks passed." : "No problems found.";
    issues.append(ok);
  }
  for (const issue of invoice.issues) {
    const item = document.createElement("li");
    item.className = issue.severity;
    item.textContent = issue.message;
    issues.append(item);
    const input = issue.field && form.elements[issue.field];
    if (input) input.classList.add("flagged", issue.severity);
  }

  for (const field of HEADER_FIELDS) {
    if (form.elements[field]) form.elements[field].value = invoice[field] ?? "";
  }
  const tbody = $(".lines tbody", detail);
  invoice.line_items.forEach((line) => addLineRow(tbody, line));
  $(".add-line", detail).addEventListener("click", () => addLineRow(tbody));

  const audit = $(".audit", detail);
  for (const entry of invoice.audit_entries) {
    const item = document.createElement("li");
    const when = new Date(entry.timestamp).toLocaleString("de-DE");
    item.textContent = `${when}: ${entry.action.replaceAll("_", " ")} by ${entry.actor}`;
    audit.append(item);
  }

  const decided = invoice.status !== "needs_review";
  if (decided) {
    showStamp($(".stamp", detail), invoice, animateStamp);
    form.querySelectorAll("input, select, .actions button, .add-line, .remove-line").forEach((el) => { el.disabled = true; });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    approve(invoice.id, form);
  });
  $(".reject", detail).addEventListener("click", () => reject(invoice.id));
}

function collectCorrections(form) {
  const data = {};
  for (const field of HEADER_FIELDS) {
    const raw = form.elements[field].value.trim();
    data[field] = NUMBER_FIELDS.includes(field) ? parseNumber(raw) : (raw || null);
  }
  data.document_type = data.document_type || "invoice";
  data.currency = (data.currency || "EUR").toUpperCase();
  data.line_items = [...form.querySelectorAll(".lines tbody tr")].map((row) => ({
    description: row.querySelector('[name="description"]').value.trim(),
    quantity: parseNumber(row.querySelector('[name="quantity"]').value),
    unit_price: parseNumber(row.querySelector('[name="unit_price"]').value),
    vat_rate: parseNumber(row.querySelector('[name="vat_rate"]').value),
    line_total: parseNumber(row.querySelector('[name="line_total"]').value),
  })).filter((line) => line.description || line.line_total !== null);
  return data;
}

async function approve(id, form) {
  const button = $(".approve", form);
  button.disabled = true;
  try {
    const invoice = await api(`/api/invoices/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ corrections: collectCorrections(form) }),
    });
    if (invoice.status === "approved") {
      toast(`Approved and sent to accounting as ${invoice.erp_reference}.`);
    } else {
      toast("Approved, but sending to accounting failed. Check the message and approve again to retry.");
    }
    await refresh();
    await selectInvoice(id, { animateStamp: invoice.status === "approved" });
  } catch (error) {
    toast(`Not approved: ${error.message}`);
    button.disabled = false;
  }
}

async function reject(id) {
  const reason = window.prompt("Why is this document rejected? (saved in the history)");
  if (reason === null) return;
  if (reason.trim().length < 3) {
    toast("Not rejected: write a short reason, at least 3 characters.");
    return;
  }
  try {
    await api(`/api/invoices/${id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason.trim() }),
    });
    toast("Rejected. It will not be sent to accounting.");
    await refresh();
    await selectInvoice(id, { animateStamp: true });
  } catch (error) {
    toast(`Not rejected: ${error.message}`);
  }
}

// --- Upload ---------------------------------------------------------------------------

async function upload(files) {
  if (!files.length) return;
  try {
    const results = [];
    for (const [index, file] of files.entries()) {
      toast(`Processing file ${index + 1} of ${files.length}: ${file.name}…`);
      clearTimeout(toast.timer); // keep the progress message up until the next file or the summary
      const body = new FormData();
      body.append("files", file);
      try {
        results.push(...(await api("/api/documents", { method: "POST", body })));
      } catch (error) {
        // one failed file shouldn't stop the rest; report it in the summary instead
        results.push({ filename: file.name, error: error.message });
      }
      await refresh();
    }
    const approved = results.filter((r) => r.status === "approved" && !r.duplicate_upload).length;
    const review = results.filter((r) => r.status === "needs_review" && !r.duplicate_upload).length;
    const repeats = results.filter((r) => r.duplicate_upload).length;
    const errors = results.filter((r) => r.error);
    const parts = [];
    if (approved) parts.push(`${approved} approved automatically`);
    if (review) parts.push(`${review} need review`);
    if (repeats) parts.push(`${repeats} already uploaded before`);
    if (errors.length) parts.push(`${errors.length} not accepted (${errors.map((e) => `${e.filename}: ${e.error}`).join("; ")})`);
    toast(parts.join(", ") + ".");
  } catch (error) {
    toast(`Upload failed: ${error.message}`);
  }
}

$("#file-input").addEventListener("change", (event) => {
  upload([...event.target.files]);
  event.target.value = "";
});

let dragDepth = 0;
window.addEventListener("dragenter", (event) => { event.preventDefault(); dragDepth += 1; $("#drop-hint").hidden = false; });
window.addEventListener("dragleave", () => { dragDepth -= 1; if (dragDepth <= 0) $("#drop-hint").hidden = true; });
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  $("#drop-hint").hidden = true;
  upload([...event.dataTransfer.files]);
});

document.querySelectorAll(".tabs button").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.status = tab.dataset.status;
    document.querySelectorAll(".tabs button").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    loadList();
  });
});

refresh().catch((error) => toast(`Could not reach the server: ${error.message}`));
