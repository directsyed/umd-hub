// UMD Hub — tiny vanilla helpers: no-reload item actions, feed read, refresh spinner.
(function () {
  const H = { "X-Requested-With": "fetch", "Accept": "application/json" };

  function post(url, body) {
    return fetch(url, { method: "POST", headers: H, body: body || undefined, credentials: "same-origin" })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // Item actions: done / snooze / cancel / reopen
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const li = btn.closest("li.item");
    if (!li) return;
    e.preventDefault();
    const id = li.dataset.id, act = btn.dataset.act;
    const fd = new FormData();
    if (act === "snooze") fd.append("until", btn.dataset.until || "tomorrow");
    btn.disabled = true;
    post(`/item/${id}/${act}`, act === "snooze" ? fd : undefined)
      .then(() => {
        if (act === "reopen") { location.reload(); return; }
        li.classList.add("gone");
        setTimeout(() => li.remove(), 400);
      })
      .catch(() => { btn.disabled = false; alert("action failed"); });
  });

  // Feed: mark read
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-feed-read]");
    if (!btn) return;
    e.preventDefault();
    post(`/feed/${btn.dataset.feedRead}/read`).then(() => {
      const li = btn.closest("li"); if (li) li.classList.remove("unread"); btn.remove();
    }).catch(() => alert("failed"));
  });

  // Refresh now + poll until done, then reload
  const rb = document.getElementById("refresh-btn");
  if (rb) {
    let polling = null;
    const poll = () => {
      fetch("/refresh/status", { headers: H, credentials: "same-origin" }).then(r => r.json()).then(s => {
        if (s.in_progress) { rb.classList.add("busy"); rb.textContent = "⟳ running"; }
        else { clearInterval(polling); polling = null; rb.classList.remove("busy"); rb.textContent = "⟳"; location.reload(); }
      }).catch(() => {});
    };
    rb.addEventListener("click", () => {
      rb.classList.add("busy"); rb.textContent = "⟳ starting";
      post("/refresh").then(() => { if (!polling) polling = setInterval(poll, 3000); })
        .catch(() => { rb.classList.remove("busy"); rb.textContent = "⟳"; alert("could not start refresh"); });
    });
    if (rb.classList.contains("busy") && !polling) polling = setInterval(poll, 3000);
  }
})();
