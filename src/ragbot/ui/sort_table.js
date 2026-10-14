// Click-to-sort for the answer tables (ragbot/ui/view.sortable_table_html). One document-level listener, bound once
// however many tables the page holds: a click on a column name sorts ascending, then descending, then back to the
// order the answer gave. Numbers and dates compare as values, text naturally ("TAL-26-1741-2" before "…-10");
// blank cells stay last; a Total row stays at the bottom. Sorting only reorders rows already on the page.
(function () {
  if (window.__kaSortBound) return;
  window.__kaSortBound = true;
  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  function value(text, kind) {
    var t = (text || "").trim();
    if (t === "" || t === "—") return null;
    if (kind === "num") {
      var n = parseFloat(t.replace(/,/g, "").replace(/%$/, ""));
      return isNaN(n) ? null : n;
    }
    if (kind === "date") {
      var d = Date.parse(t.replace(" ", "T"));
      return isNaN(d) ? null : d;
    }
    return t;
  }

  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("button.ka-sort") : null;
    if (!btn) return;
    var th = btn.closest("th"), table = btn.closest("table");
    if (!th || !table || !table.tBodies.length) return;
    var col = Array.prototype.indexOf.call(th.parentNode.children, th);
    var kind = th.getAttribute("data-kind") || "text";
    var now = th.getAttribute("aria-sort");
    var next = now === "ascending" ? "descending" : now === "descending" ? null : "ascending";
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (h) { h.removeAttribute("aria-sort"); });
    if (next) th.setAttribute("aria-sort", next);

    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);
    var totals = rows.filter(function (r) { return r.classList.contains("ka-total"); });
    var body = rows.filter(function (r) { return !r.classList.contains("ka-total"); });
    var sign = next === "descending" ? -1 : 1;
    body.sort(function (a, b) {
      if (!next) return Number(a.getAttribute("data-i")) - Number(b.getAttribute("data-i"));
      var x = value(a.cells[col] ? a.cells[col].textContent : "", kind);
      var y = value(b.cells[col] ? b.cells[col].textContent : "", kind);
      if (x === null && y === null) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return sign * (kind === "text" ? collator.compare(x, y) : x - y);
    });
    body.concat(totals).forEach(function (r) { tbody.appendChild(r); });
  });
})();
