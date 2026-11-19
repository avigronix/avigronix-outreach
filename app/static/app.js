// Small page behaviours: toasts, Ctrl+K search, Replies shortcuts, ready-made replies, template editor helpers.
// Plain JS, no build step. Each part only runs when its elements are on the page.

const typing = el => el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));

function insertAtCursor(field, text) {
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? field.value.length;
  field.value = field.value.slice(0, start) + text + field.value.slice(end);
  field.selectionStart = field.selectionEnd = start + text.length;
  field.focus();
  field.dispatchEvent(new Event("input", { bubbles: true }));  // live preview / word count follow
}

// ---------- toasts: close button, auto-hide (errors stay, Undo stays longer) ----------
document.querySelectorAll(".toast").forEach(toast => {
  const hide = () => { toast.classList.add("toast-hide"); setTimeout(() => toast.remove(), 250); };
  toast.querySelector(".toast-close").addEventListener("click", hide);
  if (toast.classList.contains("toast-ok")) setTimeout(hide, toast.dataset.undo === "1" ? 10000 : 5000);
});

// ---------- Ctrl+K / ⌘K search ----------
const dialog = document.getElementById("search-dialog");
if (dialog) {
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  let active = -1;
  const items = () => [...results.querySelectorAll(".search-item")];
  const highlight = i => {
    const list = items();
    if (!list.length) return;
    active = (i + list.length) % list.length;
    list.forEach((el, n) => el.classList.toggle("search-item-on", n === active));
    list[active].scrollIntoView({ block: "nearest" });
  };
  const open = () => {
    input.value = "";
    dialog.showModal();
    input.focus();
    htmx.ajax("GET", "/search", { target: "#search-results" });
  };
  document.querySelectorAll("[data-open-search]").forEach(b => b.addEventListener("click", open));
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); dialog.open ? dialog.close() : open(); }
  });
  results.addEventListener("htmx:afterSwap", () => { active = -1; highlight(0); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); highlight(active + 1); }
    if (e.key === "ArrowUp") { e.preventDefault(); highlight(active - 1); }
    if (e.key === "Enter" && items()[active]) { e.preventDefault(); location.href = items()[active].href; }
  });
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });  // click outside the box
}

// ---------- Replies: keyboard shortcuts ----------
if (document.querySelector(".inbox")) {
  document.addEventListener("keydown", e => {
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey || (dialog && dialog.open)) return;
    const list = [...document.querySelectorAll(".inbox-list .inbox-item")];
    const current = list.findIndex(el => el.classList.contains("inbox-item-on"));
    if (e.key === "j" && list[current + 1]) location.href = list[current + 1].href;
    else if (e.key === "k" && current > 0) location.href = list[current - 1].href;
    else if (e.key === "r") { const box = document.getElementById("reply-body"); if (box) { e.preventDefault(); box.focus(); } }
    else if (/^[1-9]$/.test(e.key)) {
      const button = document.querySelectorAll(".outcome-grid .outcome")[Number(e.key) - 1];
      const risky = button && button.classList.contains("outcome-danger");  // unsubscribe can't be undone
      if (button && (!risky || confirm(`${button.textContent.trim()}: add this person to the suppression list?`))) {
        button.closest("form").requestSubmit();
      }
    }
  });
}

// ---------- ready-made replies ----------
document.querySelectorAll(".snippet-pick").forEach(pick => {
  pick.addEventListener("change", () => {
    const tpl = document.querySelector(`template[data-snippet="${pick.value}"]`);
    const box = document.getElementById(pick.dataset.snippetTarget);
    if (tpl && box) insertAtCursor(box, tpl.content.textContent);
    pick.value = "";
  });
});

// ---------- template editor: merge-field chips + live word count ----------
const tplForm = document.getElementById("tpl-form");
if (tplForm) {
  let lastField = tplForm.querySelector("[name=body_text]");
  tplForm.addEventListener("focusin", e => { if (e.target.matches("input[type=text], textarea")) lastField = e.target; });
  document.querySelectorAll("[data-insert]").forEach(chip =>
    chip.addEventListener("click", () => insertAtCursor(lastField, chip.dataset.insert)));

  const body = tplForm.querySelector("[name=body_text]");
  const counter = document.getElementById("word-count");
  const count = () => {
    // Same rule as the server-side check: a merge field counts as one word.
    const words = body.value.replace(/\{\{[^}]*\}\}/g, "name").split(/\s+/).filter(Boolean).length;
    counter.textContent = `${words} words · aim for 50–150`;
    counter.className = "word-count " + (words >= 50 && words <= 150 ? "word-count-ok" : "word-count-off");
  };
  if (body && counter) { body.addEventListener("input", count); count(); }
}

// ---------- show / hide password ----------
document.querySelectorAll("[data-toggle-password]").forEach(btn => {
  const field = document.getElementById(btn.dataset.togglePassword);
  btn.addEventListener("click", () => {
    const show = field.type === "password";
    field.type = show ? "text" : "password";
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    btn.classList.toggle("on", show);
    field.focus();
  });
});

// ---------- login: show the spinner once, never double-submit ----------
document.querySelectorAll("form.login-card").forEach(form => {
  form.addEventListener("submit", () => {
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.textContent = "Logging in…";
  });
});
