/* Dashboard behaviour: collect params, POST /api/execute, render the response. */
(function () {
  "use strict";

  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  /* ================= tag discovery + selection ================= */

  /* Tags offered by the pickers. Starts as the built-in demo catalogue and is
     replaced by whatever client.search() returns from the tag browser. */
  const catalogue = new Map();
  document.querySelectorAll("#known-tags option").forEach((o) => {
    catalogue.set(o.value, "");
  });

  /* Tags ticked in the browser results table. */
  const browserSelection = new Set();

  function renderSelectionChips() {
    const host = document.getElementById("selection-chips");
    if (!host) return;
    if (!browserSelection.size) {
      host.innerHTML = `<span class="muted">nothing selected yet</span>`;
      return;
    }
    host.innerHTML = Array.from(browserSelection)
      .map((t) => `<span class="sel-chip">${esc(t)}</span>`)
      .join("");
  }

  function renderBrowseResult(data) {
    const host = document.getElementById("browse-result");
    const rows = data.tags
      .map(
        (t) => `<tr>
          <td><label class="check">
            <input type="checkbox" class="browse-tag" value="${esc(t.name)}"
              ${browserSelection.has(t.name) ? "checked" : ""}>
            <code>${esc(t.name)}</code></label></td>
          <td>${esc(t.description || "")}</td>
        </tr>`
      )
      .join("");

    host.innerHTML = `
      <div class="metrics">
        <span class="metric">found <b>${data.count}</b></span>
        <span class="metric">endpoint <b>${esc(data.endpoint)}</b></span>
        <span class="metric">pattern <b>${esc(data.pattern)}</b></span>
        <span class="metric">${data.elapsed_ms} ms</span>
      </div>
      <div class="browse-toolbar">
        <button type="button" class="btn-mini" id="browse-all">Select all</button>
        <button type="button" class="btn-mini" id="browse-none">Select none</button>
      </div>
      <div class="scroll-y"><table class="data browse-table">
        <thead><tr><th>Tag</th><th>Description</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="2">No tags matched.</td></tr>`}</tbody>
      </table></div>
      ${block(
        `Intercepted HTTP (${data.http_calls.length})`,
        httpCalls(data.http_calls)
      )}
      ${block("Discovery call", `<pre>${esc(data.code)}</pre>`)}`;
    host.hidden = false;

    host.querySelectorAll(".browse-tag").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) browserSelection.add(cb.value);
        else browserSelection.delete(cb.value);
        renderSelectionChips();
      });
    });
    const setAll = (state) => {
      host.querySelectorAll(".browse-tag").forEach((cb) => {
        cb.checked = state;
        if (state) browserSelection.add(cb.value);
        else browserSelection.delete(cb.value);
      });
      renderSelectionChips();
    };
    host.querySelector("#browse-all").addEventListener("click", () => setAll(true));
    host.querySelector("#browse-none").addEventListener("click", () => setAll(false));
  }

  async function browseTags() {
    const status = document.getElementById("browse-status");
    const btn = document.getElementById("browse-run");
    const pattern = document.getElementById("browse-pattern").value.trim() || "*";
    const description = document.getElementById("browse-description").value.trim();
    const limit = document.getElementById("browse-limit").value.trim() || "500";

    btn.disabled = true;
    status.textContent = "querying IP.21...";
    try {
      const qs = new URLSearchParams({ pattern, limit });
      if (description) qs.set("description", description);
      const res = await fetch(`/api/tags?${qs}`);
      const data = await res.json();
      if (!res.ok) {
        status.textContent = "failed";
        document.getElementById("browse-result").innerHTML =
          `<div class="err">${esc(data.detail || "Discovery failed.")}</div>`;
        document.getElementById("browse-result").hidden = false;
        return;
      }
      /* Merge into the offered options rather than replacing, so you can
         browse several patterns and build up a working set. */
      data.tags.forEach((t) => catalogue.set(t.name, t.description || ""));
      renderBrowseResult(data);
      refreshAllPickers();
      status.textContent =
        `${data.count} tag(s) via ${data.endpoint}` +
        ` · ${catalogue.size} available in pickers`;
    } catch (e) {
      status.textContent = "failed";
    } finally {
      btn.disabled = false;
    }
  }

  /* ---------- per-card tag pickers ---------- */
  function pickerSelection(picker) {
    return (picker.dataset.selected || "")
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
  }

  /* Update selection without rebuilding the DOM, so scroll position, focus and
     the shift-click anchor all survive. */
  function setPickerSelection(picker, tags) {
    const known = new Set(pickerNames(picker));
    const next = Array.from(new Set(tags)).filter((t) => known.has(t));
    picker.dataset.selected = next.join(",");
    syncPicker(picker);
  }

  function syncPicker(picker) {
    const selected = new Set(pickerSelection(picker));
    const chips = picker.querySelectorAll(".tag-chip");
    chips.forEach((chip) => {
      const cb = chip.querySelector("input");
      const on = selected.has(cb.value);
      cb.checked = on;
      chip.classList.toggle("on", on);
    });
    const count = picker.querySelector(".tagpicker-count");
    if (count) {
      count.textContent = `${selected.size} of ${chips.length} selected`;
      count.className = "tagpicker-count" + (selected.size ? " has" : "");
    }
    const exec = (picker.closest(".op") || document).querySelector(".btn-exec");
    if (exec) {
      const empty = selected.size === 0;
      exec.classList.toggle("needs-tags", empty);
      exec.title = empty ? "Select at least one tag first" : "";
    }
  }

  function pickerNames(picker) {
    /* Every catalogue tag, plus any selected tag not in the catalogue. */
    return Array.from(new Set([...catalogue.keys(), ...pickerSelection(picker)]));
  }

  function renderPicker(picker) {
    const selected = pickerSelection(picker);
    const selectedSet = new Set(selected);
    const names = pickerNames(picker);
    const chips = names
      .map((name, i) => {
        const on = selectedSet.has(name);
        const title = catalogue.get(name) || "";
        return `<label class="tag-chip ${on ? "on" : ""}" title="${esc(title)}">
            <input type="checkbox" value="${esc(name)}" data-idx="${i}" ${
          on ? "checked" : ""
        }>
            <span>${esc(name)}</span></label>`;
      })
      .join("");

    const host = picker.querySelector(".tagpicker-chips");
    host.innerHTML =
      chips || `<span class="muted">No tags available. Browse for tags above.</span>`;

    host.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      /* Shift-click extends the previous click into a contiguous range, so a
         whole block of tags can be (de)selected in one gesture. */
      cb.addEventListener("click", (event) => {
        const idx = Number(cb.dataset.idx);
        const next = new Set(pickerSelection(picker));
        const anchor = picker._anchorIdx;

        if (event.shiftKey && anchor !== undefined && anchor !== idx) {
          const [lo, hi] = anchor < idx ? [anchor, idx] : [idx, anchor];
          for (let i = lo; i <= hi; i++) {
            if (cb.checked) next.add(names[i]);
            else next.delete(names[i]);
          }
        } else if (cb.checked) {
          next.add(cb.value);
        } else {
          next.delete(cb.value);
        }

        picker._anchorIdx = idx;
        setPickerSelection(picker, Array.from(next));
      });
    });

    syncPicker(picker);
  }

  function refreshAllPickers() {
    document.querySelectorAll(".tagpicker").forEach(renderPicker);
  }

  function initPickers() {
    document.querySelectorAll(".tagpicker").forEach((picker) => {
      renderPicker(picker);

      const manual = picker.querySelector(".tagpicker-manual");
      const addManual = () => {
        const value = manual.value.trim();
        if (!value) return;
        const next = pickerSelection(picker);
        value
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean)
          .forEach((t) => {
            if (!catalogue.has(t)) catalogue.set(t, "(typed manually)");
            next.push(t);
          });
        manual.value = "";
        refreshAllPickers();
        setPickerSelection(picker, next);
      };
      picker.querySelector(".tagpicker-add").addEventListener("click", addManual);
      manual.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          addManual();
        }
      });

      picker
        .querySelector(".tagpicker-from-browser")
        .addEventListener("click", () => {
          if (!browserSelection.size) {
            alert("Tick some tags in the tag browser first.");
            return;
          }
          setPickerSelection(picker, Array.from(browserSelection));
        });

      picker
        .querySelector(".tagpicker-all")
        .addEventListener("click", () => setPickerSelection(picker, pickerNames(picker)));

      picker
        .querySelector(".tagpicker-clear")
        .addEventListener("click", () => setPickerSelection(picker, []));
    });
  }

  const browseBtn = document.getElementById("browse-run");
  if (browseBtn) {
    browseBtn.addEventListener("click", browseTags);
    document.getElementById("apply-selection").addEventListener("click", () => {
      if (!browserSelection.size) {
        alert("Tick some tags in the tag browser first.");
        return;
      }
      document
        .querySelectorAll(".tagpicker")
        .forEach((p) => setPickerSelection(p, Array.from(browserSelection)));
    });
  }

  /* ---------- expand / collapse ---------- */
  document.querySelectorAll(".op-head").forEach((head) => {
    head.addEventListener("click", () => {
      const body = head.parentElement.querySelector(".op-body");
      const open = head.getAttribute("aria-expanded") === "true";
      head.setAttribute("aria-expanded", String(!open));
      body.hidden = open;
    });
  });

  /* ---------- gather form values ---------- */
  function collect(card) {
    const params = {};
    card.querySelectorAll(".param").forEach((param) => {
      const name = param.dataset.field;
      const type = param.dataset.type;

      if (type === "tagpicker") {
        params[name] = pickerSelection(param.querySelector(".tagpicker"));
        return;
      }
      if (type === "multiselect") {
        params[name] = Array.from(
          param.querySelectorAll("input[type=checkbox]:checked")
        ).map((cb) => cb.value);
        return;
      }
      if (type === "checkbox") {
        params[name] = param.querySelector("input[type=checkbox]").checked;
        return;
      }
      const input = param.querySelector("input, select");
      if (!input) return;
      const raw = input.value.trim();
      if (type === "tags") {
        params[name] = raw
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean);
      } else if (type === "number") {
        params[name] = raw === "" ? null : Number(raw);
      } else {
        params[name] = raw;
      }
    });
    return params;
  }

  /* ---------- renderers ---------- */
  function block(title, inner) {
    return `<div class="res-block"><h4>${esc(title)}</h4>${inner}</div>`;
  }

  function metrics(data) {
    const items = [];
    items.push(`<span class="metric">total <b>${data.elapsed_ms} ms</b></span>`);
    items.push(
      `<span class="metric">HTTP calls <b>${data.http_call_count}</b></span>`
    );
    if (typeof data.row_count === "number" && data.row_count > 0) {
      items.push(`<span class="metric">rows <b>${data.row_count}</b></span>`);
    }
    if (data.result_kind) {
      items.push(`<span class="metric">kind <b>${esc(data.result_kind)}</b></span>`);
    }
    if (typeof data.attempts === "number") {
      const cls = data.retry_observed ? "metric-warn" : "metric";
      items.push(
        `<span class="metric ${cls}">attempts <b>${data.attempts}</b></span>`
      );
    }
    if (data.error) {
      items.push(`<span class="metric metric-bad">raised <b>exception</b></span>`);
    } else if (data.ok) {
      items.push(`<span class="metric metric-ok">ok</span>`);
    }
    return `<div class="metrics">${items.join("")}</div>`;
  }

  function dataTable(table) {
    if (!table || !table.columns.length) {
      return `<p class="table-cap">Empty DataFrame.</p>`;
    }
    const head = table.columns.map((c) => `<th>${esc(c)}</th>`).join("");
    const rows = table.rows
      .map(
        (row) =>
          "<tr>" +
          row
            .map((cell) =>
              cell === null || cell === undefined
                ? `<td class="na">NA</td>`
                : `<td>${esc(cell)}</td>`
            )
            .join("") +
          "</tr>"
      )
      .join("");
    let cap = `<p class="table-cap">${table.rows.length} row(s) &times; ${
      table.columns.length - 1
    } column(s), indexed by <code>${esc(table.index_name || "index")}</code>.</p>`;
    const desc = table.descriptions || {};
    const keys = Object.keys(desc);
    if (keys.length) {
      cap += `<p class="table-cap">df.attrs["tag_descriptions"]: ${keys
        .map((k) => `<code>${esc(k)}</code> = ${esc(desc[k])}`)
        .join(" &middot; ")}</p>`;
    }
    return `<div class="scroll-x"><table class="data"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>${cap}`;
  }

  function comparison(comparisons) {
    const baseline = new Set((comparisons[0] && comparisons[0].keys) || []);
    const cards = comparisons
      .map((c) => {
        const pills = c.keys
          .map(
            (k) =>
              `<span class="key-pill ${
                baseline.has(k) ? "" : "key-new"
              }">${esc(k)}</span>`
          )
          .join("");
        const sample = c.sample
          ? `<pre>${esc(JSON.stringify(c.sample[0], null, 2))}</pre>`
          : c.table
          ? dataTable(c.table)
          : "";
        return `<div class="cmp-card">
          <h5>IncludeFields.${esc(c.include)}</h5>
          <div class="cmp-keys">${pills}</div>
          <p class="table-cap">${c.row_count} row(s) &middot; ${c.elapsed_ms} ms</p>
          ${sample}
        </div>`;
      })
      .join("");
    return `<div class="cmp">${cards}</div>
      <p class="table-cap">Green pills are fields absent from the first variant.</p>`;
  }

  function timing(data) {
    const s = data.summary || {};
    const rows = (data.timings || [])
      .map(
        (t) =>
          `<tr><td>${esc(t.label)}</td><td class="num">${t.elapsed_ms}</td>
           <td class="num">${t.row_count}</td><td class="num">${t.http_calls_so_far}</td></tr>`
      )
      .join("");
    const probe = Object.entries(s.cache_api_probe || {})
      .map(
        ([k, v]) =>
          `<span class="key-pill ${v ? "key-new" : ""}">${esc(k)}: ${
            v ? "present" : "absent"
          }</span>`
      )
      .join(" ");
    return `<div class="scroll-x"><table class="data">
        <thead><tr><th>Call</th><th>Elapsed (ms)</th><th>Rows</th><th>HTTP calls so far</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p class="table-cap">Delta: <b>${s.delta_ms} ms</b> &middot;
        total HTTP requests intercepted: <b>${s.total_http_calls}</b> &middot;
        <code>cache=</code> kwarg accepted: <b>${
          s.accepts_cache_kwarg ? "yes" : "no"
        }</b></p>
      <div class="cmp-keys" style="margin-top:8px">${probe}</div>`;
  }

  function errorBox(err) {
    let html = `<div class="err"><div><code>${esc(err.type)}</code></div>
      <div style="margin-top:5px">${esc(err.message)}</div>`;
    if (err.underlying) {
      html += `<div style="margin-top:7px">last attempt raised: <code>${esc(
        err.underlying
      )}</code></div>`;
    }
    if (err.chain && err.chain.length > 1) {
      html += `<div style="margin-top:7px">exception chain: ${err.chain
        .map((c) => `<code>${esc(c)}</code>`)
        .join(" &larr; ")}</div>`;
    }
    return html + "</div>";
  }

  function httpCalls(calls) {
    if (!calls.length) {
      return `<p class="table-cap">No HTTP calls were made.</p>`;
    }
    const items = calls
      .map((c, i) => {
        const cls = c.status >= 500 ? "s5xx" : c.status >= 400 ? "s4xx" : "s2xx";
        const body = c.request_body
          ? `<div class="call-body">${esc(c.request_body)}</div>`
          : "";
        const summary = c.response_summary
          ? `<div class="call-summary">&rarr; ${esc(c.response_summary)}</div>`
          : "";
        return `<div class="call">
          <div class="call-head">
            <span class="call-method">#${i + 1} ${esc(c.method)}</span>
            <span class="call-kind">${esc(c.kind)}</span>
            <span class="call-url">${esc(c.url)}</span>
            <span class="call-status ${cls}">${c.status}</span>
          </div>${body}${summary}</div>`;
      })
      .join("");
    return `<div class="calls">${items}</div>`;
  }

  function render(data) {
    let html = metrics(data);

    if (data.unexpected_error) {
      html += block(
        "Demo error (this is a bug in the dashboard, not in aspy21)",
        `<div class="err"><code>${esc(
          data.unexpected_error.type
        )}</code><div style="margin-top:5px">${esc(
          data.unexpected_error.message
        )}</div></div><pre>${esc(data.unexpected_error.traceback || "")}</pre>`
      );
    }

    if (data.error) {
      html += block("Exception raised by aspy21", errorBox(data.error));
    }

    if (data.result_kind === "dataframe") {
      html += block("Result - pandas DataFrame", dataTable(data.table));
    } else if (data.result_kind === "comparison") {
      html += block("Result - IncludeFields comparison", comparison(data.comparisons));
    } else if (data.result_kind === "timing") {
      html += block("Result - repeat call timing", timing(data));
    } else if (data.records !== null && data.records !== undefined) {
      html += block(
        "Result - JSON",
        `<pre>${esc(JSON.stringify(data.records, null, 2))}</pre>`
      );
    }

    if (data.notes && data.notes.length) {
      html += block(
        "What happened",
        `<ul class="notes">${data.notes
          .map((n) => `<li>${esc(n)}</li>`)
          .join("")}</ul>`
      );
    }

    html += block(
      `Intercepted HTTP (${data.http_call_count})`,
      httpCalls(data.http_calls || [])
    );

    if (data.code) {
      html += block("Equivalent aspy21 code", `<pre>${esc(data.code)}</pre>`);
    }
    return html;
  }

  /* ---------- execute ---------- */
  document.querySelectorAll(".btn-exec").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".op");
      const out = card.querySelector(".response");
      const status = card.querySelector(".status");
      const params = collect(card);

      if (Array.isArray(params.tags) && params.tags.length === 0) {
        out.hidden = false;
        out.innerHTML = `<div class="err">Select at least one tag before executing.
          Use <b>Browse tags</b> above to discover them from IP.21, or the
          <b>All</b> button on this card.</div>`;
        status.textContent = "no tags selected";
        return;
      }

      btn.disabled = true;
      status.textContent = "executing...";
      out.hidden = false;
      out.innerHTML = `<p class="table-cap">Calling aspy21 against the mocked backend...</p>`;

      const t0 = performance.now();
      try {
        const res = await fetch("/api/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operation: card.dataset.op, params }),
        });
        const data = await res.json();
        if (!res.ok) {
          out.innerHTML = `<div class="err"><code>HTTP ${res.status}</code>
            <div style="margin-top:5px">${esc(
              data.detail || "Request failed."
            )}</div></div>`;
          status.textContent = "failed";
        } else {
          out.innerHTML = render(data);
          status.textContent = `${Math.round(performance.now() - t0)} ms round trip`;
        }
      } catch (e) {
        out.innerHTML = `<div class="err"><code>${esc(
          e.name
        )}</code><div style="margin-top:5px">${esc(e.message)}</div></div>`;
        status.textContent = "failed";
      } finally {
        btn.disabled = false;
      }
    });
  });

  /* ---------- reset ---------- */
  document.querySelectorAll(".btn-reset").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".op");
      card.querySelectorAll(".param:not([data-type=tagpicker]) input, .param:not([data-type=tagpicker]) select")
        .forEach((el) => {
          if (el.type === "checkbox") {
            el.checked = el.defaultChecked;
          } else if (el.tagName === "SELECT") {
            Array.from(el.options).forEach((o) => (o.selected = o.defaultSelected));
          } else {
            el.value = el.defaultValue;
          }
        });
      /* restore the card's original tag selection */
      card.querySelectorAll(".tagpicker").forEach((picker) => {
        setPickerSelection(picker, (picker.dataset.default || "").split(",").filter(Boolean));
      });
      const out = card.querySelector(".response");
      out.hidden = true;
      out.innerHTML = "";
      card.querySelector(".status").textContent = "";
    });
  });

  /* ---------- init ---------- */
  initPickers();
  renderSelectionChips();

  /* open the first card so the page is not a wall of collapsed rows */
  const first = document.querySelector(".op-head");
  if (first) first.click();
})();
