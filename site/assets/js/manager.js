/* WITTE — кабинет менеджера: заявки и каталог оборудования. Работает через /api/manager/*. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function api(method, url, body) {
    return fetch("/api" + url, { method: method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) {
        return r.text().then(function (t) {
          var j; try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { error: "Сервер вернул неожиданный ответ (" + r.status + ")" }; }
          if (!r.ok) { var err = new Error(j.error || ("Ошибка " + r.status)); err.status = r.status; throw err; }
          return j;
        });
      });
  }
  var root = $("[data-manager]"); if (!root) return;
  var state = { status: null, products: [], catalog: null, documents: [], editing: null, images: [], newImages: [] };

  function show(view) { $$("[data-view]", root).forEach(function (v) { v.hidden = v.getAttribute("data-view") !== view; }); }
  function toast(msg, err) { var t = $("[data-toast]", root); t.textContent = msg; t.className = "mgr-toast" + (err ? " err" : " ok"); t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(function () { t.hidden = true; }, err ? 9000 : 5000); }

  /* ---------- вход ---------- */
  $("[data-login-form]", root).addEventListener("submit", function (e) {
    e.preventDefault();
    var f = e.target, msg = $("[data-login-msg]", f);
    msg.textContent = "";
    api("POST", "/manager/login", { password: f.password.value }).then(function () { f.reset(); start(); })
      .catch(function (err) { msg.textContent = err.message === "Failed to fetch" ? "Сервер недоступен." : err.message; });
  });
  $("[data-logout]", root).addEventListener("click", function () { api("POST", "/manager/logout").then(function () { show("login"); }); });

  function start() {
    api("GET", "/manager/status").then(function (s) {
      state.status = s; show("app"); renderStatus(); openTab("requests");
    }).catch(function (err) {
      show("login");
      if (err.status !== 401) $("[data-login-msg]", root).textContent = err.message === "Failed to fetch" ? "Сервер кабинета не запущен или недоступен." : err.message;
    });
  }

  /* ---------- вкладки ---------- */
  $$("[data-tab]", root).forEach(function (b) { b.addEventListener("click", function () { openTab(b.getAttribute("data-tab")); }); });
  function openTab(name) {
    $$("[data-tab]", root).forEach(function (b) { b.setAttribute("aria-selected", b.getAttribute("data-tab") === name ? "true" : "false"); });
    $$("[data-panel]", root).forEach(function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
    if (name === "requests") loadRequests();
    if (name === "products") loadProducts();
  }

  /* ---------- подключения ---------- */
  function renderStatus() {
    var s = state.status, ok = function (v) { return v ? '<b class="ok">подключено</b>' : '<b class="bad">не подключено</b>'; };
    $("[data-status]", root).innerHTML =
      "<tr><th>База заявок</th><td>" + (s.db_persistent ? '<b class="ok">PostgreSQL</b>' : '<b class="bad">' + esc(s.db) + "</b> — заявки не сохраняются надолго, подключите базу на Layero") + "</td></tr>" +
      "<tr><th>Отправка на почту</th><td>" + ok(s.smtp) + " → " + esc(s.mail_to) + "</td></tr>" +
      "<tr><th>Сохранение каталога</th><td>" + (s.repo === "github" ? '<b class="ok">GitHub: ' + esc(s.github_repo) + "</b> — после сохранения сайт пересобирается автоматически" : '<b class="bad">локально</b> — задайте GITHUB_TOKEN, чтобы изменения попадали на сайт') + "</td></tr>";
  }

  /* ---------- заявки ---------- */
  var reqFilter = $("[data-req-filter]", root);
  reqFilter.addEventListener("change", loadRequests);
  var rq; $("[data-req-search]", root).addEventListener("input", function () { clearTimeout(rq); rq = setTimeout(loadRequests, 300); });
  function loadRequests() {
    var box = $("[data-requests]", root), st = reqFilter.value, q = $("[data-req-search]", root).value.trim();
    box.innerHTML = '<p class="muted">Загрузка…</p>';
    api("GET", "/manager/requests?" + new URLSearchParams({ status: st, q: q }).toString()).then(function (d) {
      var labels = state.status.statuses, total = 0;
      Object.keys(d.counts).forEach(function (k) { total += d.counts[k]; });
      $("[data-req-count]", root).textContent = d.counts["new"] ? "(" + d.counts["new"] + " новых)" : "";
      reqFilter.innerHTML = '<option value="">Все (' + total + ")</option>" + Object.keys(labels).map(function (k) {
        return '<option value="' + k + '"' + (k === st ? " selected" : "") + ">" + esc(labels[k]) + " (" + (d.counts[k] || 0) + ")</option>";
      }).join("");
      if (!d.items.length) { box.innerHTML = '<p class="empty">Заявок нет.</p>'; return; }
      box.innerHTML = d.items.map(function (r) {
        var opts = Object.keys(labels).map(function (k) { return '<option value="' + k + '"' + (k === r.status ? " selected" : "") + ">" + esc(labels[k]) + "</option>"; }).join("");
        var mail = r.mailed === "sent" ? "письмо отправлено на почту" : r.mailed === "smtp-not-configured" ? "почта не настроена — письмо не отправлялось" : (r.mailed ? "ошибка отправки письма: " + r.mailed.replace(/^error: /, "") : "письмо отправляется…");
        return '<article class="req req-' + esc(r.status) + '" data-id="' + esc(r.id) + '">' +
          '<header><b>№ ' + esc(r.id) + "</b><span>" + esc(r.created) + '</span><select data-req-status aria-label="Статус заявки">' + opts + "</select></header>" +
          '<div class="req-grid"><div><span>Клиент</span>' + esc(r.name) + (r.company ? "<br><small>" + esc(r.company) + "</small>" : "") + "</div>" +
          '<div><span>Телефон</span><a href="tel:' + esc(String(r.phone).replace(/[^0-9+]/g, "")) + '">' + esc(r.phone) + "</a></div>" +
          '<div><span>E-mail</span>' + (r.email ? '<a href="mailto:' + esc(r.email) + '">' + esc(r.email) + "</a>" : "—") + "</div>" +
          '<div><span>Регион / направление</span>' + esc(r.region || "—") + " / " + esc(r.direction || "—") + "</div></div>" +
          (r.products ? '<p><span>Оборудование</span>' + esc(r.products) + "</p>" : "") +
          (r.comment ? '<p><span>Комментарий</span>' + esc(r.comment) + "</p>" : "") +
          '<label class="req-note"><span>Заметка менеджера</span><textarea data-req-note rows="2">' + esc(r.note || "") + "</textarea></label>" +
          '<footer><small>' + esc(mail) + (r.page ? " · со страницы " + esc(r.page) : "") + "</small></footer></article>";
      }).join("");
    }).catch(function (err) { box.innerHTML = '<p class="empty">' + esc(err.message) + "</p>"; if (err.status === 401) show("login"); });
  }
  $("[data-requests]", root).addEventListener("change", function (e) {
    var card = e.target.closest(".req"); if (!card) return;
    var body = e.target.matches("[data-req-status]") ? { status: e.target.value } : e.target.matches("[data-req-note]") ? { note: e.target.value } : null;
    if (!body) return;
    api("PATCH", "/manager/requests/" + encodeURIComponent(card.getAttribute("data-id")), body)
      .then(function () { toast("Сохранено"); if (body.status) { card.className = "req req-" + body.status; } })
      .catch(function (err) { toast(err.message, true); });
  });

  /* ---------- оборудование: список ---------- */
  function loadProducts() {
    var box = $("[data-products]", root);
    box.innerHTML = '<p class="muted">Загрузка…</p>';
    api("GET", "/manager/products").then(function (d) {
      state.products = d.items; state.catalog = d.catalog; state.documents = d.documents;
      renderProducts();
    }).catch(function (err) { box.innerHTML = '<p class="empty">' + esc(err.message) + "</p>"; });
  }
  var ps; $("[data-prod-search]", root).addEventListener("input", function () { clearTimeout(ps); ps = setTimeout(renderProducts, 150); });
  function renderProducts() {
    var q = $("[data-prod-search]", root).value.trim().toLowerCase();
    var list = state.products.filter(function (p) { return !q || (p.name + " " + p.model + " " + p.id).toLowerCase().indexOf(q) >= 0; });
    $("[data-prod-count]", root).textContent = list.length + " из " + state.products.length;
    $("[data-products]", root).innerHTML = '<table class="tbl mgr-tbl"><thead><tr><th>Название</th><th>Модель</th><th>Код</th><th></th></tr></thead><tbody>' +
      list.slice(0, 300).map(function (p) {
        return "<tr><td>" + esc(p.name) + "</td><td>" + esc(p.model) + "</td><td><code>" + esc(p.id) + "</code></td><td class=\"nowrap\">" +
          (p.editable ? '<button type="button" class="btn btn-ghost btn-sm" data-edit="' + esc(p.id) + '">Изменить</button> <button type="button" class="btn btn-ghost btn-sm" data-del="' + esc(p.id) + '">Удалить</button>'
            : '<small class="muted">серия «' + esc(p.series) + "»</small>") + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  $("[data-products]", root).addEventListener("click", function (e) {
    var ed = e.target.closest("[data-edit]"), del = e.target.closest("[data-del]");
    if (ed) openEditor(state.products.filter(function (p) { return p.id === ed.getAttribute("data-edit"); })[0].product);
    if (del) {
      var id = del.getAttribute("data-del");
      if (!confirm("Удалить изделие «" + id + "» с сайта?")) return;
      api("DELETE", "/manager/products/" + encodeURIComponent(id)).then(function (r) { toast(savedMsg(r, "Удалено")); loadProducts(); }).catch(function (err) { toast(err.message, true); });
    }
  });
  $("[data-new-product]", root).addEventListener("click", function () { openEditor(null); });

  /* ---------- редактор ---------- */
  var form = $("[data-editor]", root);
  var TR = { а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "c", ч: "ch", ш: "sh", щ: "sch", ы: "y", э: "e", ю: "yu", я: "ya" };
  function slug(s) { return String(s).toLowerCase().split("").map(function (c) { return TR[c] != null ? TR[c] : c; }).join("").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 70); }
  function lines(v) { return (v || []).join("\n"); }
  function splitLines(s) { return s.split("\n").map(function (x) { return x.trim(); }).filter(Boolean); }

  function pairRow(k, v, kp, vp) {
    return '<div class="pair"><input value="' + esc(k) + '" placeholder="' + kp + '" aria-label="' + kp + '"><input value="' + esc(v) + '" placeholder="' + vp + '" aria-label="' + vp + '"><button type="button" class="icon-btn" data-rm aria-label="Удалить строку">×</button></div>';
  }
  function tableBlock(t) {
    var text = t ? [t.cols.join(" | ")].concat(t.rows.map(function (r) { return r.join(" | "); })).join("\n") : "";
    return '<div class="tblock"><input data-t-title value="' + esc(t ? t.title : "") + '" placeholder="Заголовок таблицы, напр. «Модельный ряд»" aria-label="Заголовок таблицы">' +
      '<textarea data-t-body rows="6" placeholder="Первая строка — названия колонок, далее строки. Ячейки разделяйте символом |&#10;Модель | Мощность, кВт | Масса, кг&#10;HY-1 | 1,1 | 120">' + esc(text) + '</textarea><button type="button" class="btn btn-ghost btn-sm" data-rm>Удалить таблицу</button></div>';
  }
  function docRow(d) {
    var opts = state.documents.map(function (x) { return '<option value="' + esc(x.id) + '"' + (d && d.doc === x.id ? " selected" : "") + ">" + esc(x.title) + "</option>"; }).join("");
    return '<div class="pair"><select aria-label="Документ">' + opts + '</select><input value="' + esc(d ? d.pages : "") + '" placeholder="Страницы, напр. 3–4" aria-label="Страницы"><button type="button" class="icon-btn" data-rm aria-label="Удалить строку">×</button></div>';
  }
  function renderImages() {
    var box = $("[data-images]", form);
    box.innerHTML = state.images.map(function (n, i) {
      return '<figure><img src="/assets/img/p/' + esc(n) + '.jpg" alt=""><figcaption>' + (i === 0 ? "главное" : "") + '</figcaption><div><button type="button" class="icon-btn" data-img-up="' + i + '" aria-label="Сделать главным">↑</button><button type="button" class="icon-btn" data-img-rm="' + i + '" aria-label="Убрать фото">×</button></div></figure>';
    }).join("") + state.newImages.map(function (f, i) {
      return '<figure class="new"><img src="' + f.data + '" alt=""><figcaption>новое</figcaption><div><button type="button" class="icon-btn" data-new-rm="' + i + '" aria-label="Убрать фото">×</button></div></figure>';
    }).join("") || '<p class="muted">Фото пока нет.</p>';
  }

  function openEditor(p) {
    state.editing = p ? p.id : null;
    state.images = p && p.img ? p.img.slice() : [];
    state.newImages = [];
    form.reset();
    $("[data-editor-title]", root).textContent = p ? "Изменить: " + p.name : "Новое оборудование";
    var f = form.elements;
    f.id.value = p ? p.id : ""; f.id.dataset.auto = p ? "" : "1";
    ["name", "model", "summary", "description", "purpose", "variants", "maker"].forEach(function (k) { f[k].value = p && p[k] ? p[k] : ""; });
    f.features.value = lines(p && p.features); f.kit.value = lines(p && p.kit); f.missing.value = lines(p && p.missing);
    f.air_min.value = p && p.airflow ? p.airflow[0] : ""; f.air_max.value = p && p.airflow ? p.airflow[1] : "";
    // разделы каталога
    var cats = (p && p.cats) || [];
    $("[data-cats]", form).innerHTML = state.catalog.directions.map(function (d) {
      return "<fieldset><legend>" + esc(d.name) + "</legend>" + d.categories.filter(function (c) { return c.subcategories.length; }).map(function (c) {
        return "<div><b>" + esc(c.name) + "</b>" + c.subcategories.map(function (s) {
          var path = d.slug + "/" + c.slug + "/" + s.slug;
          return '<label class="check"><input type="checkbox" value="' + esc(path) + '"' + (cats.indexOf(path) >= 0 ? " checked" : "") + "> " + esc(s.name) + "</label>";
        }).join("") + "</div>";
      }).join("") + "</fieldset>";
    }).join("");
    $("[data-params]", form).innerHTML = ((p && p.params) || [["", ""]]).map(function (r) { return pairRow(r[0], r[1], "Параметр", "Значение"); }).join("");
    $("[data-facets]", form).innerHTML = Object.keys((p && p.facets) || {}).map(function (k) { return pairRow(k, p.facets[k], "Фильтр, напр. Размер", "Значение, напр. 600×600"); }).join("");
    $("[data-tables]", form).innerHTML = ((p && p.tables) || []).map(tableBlock).join("");
    $("[data-docs]", form).innerHTML = ((p && p.docs) || []).map(docRow).join("");
    $("[data-related-list]", root).innerHTML = state.products.map(function (x) { return '<option value="' + esc(x.id) + '">' + esc(x.name) + "</option>"; }).join("");
    f.related.value = ((p && p.related) || []).join(", ");
    renderImages();
    $("[data-editor-msg]", form).textContent = "";
    show("app"); $("[data-panel=products]", root).hidden = true; $("[data-panel=editor]", root).hidden = false;
    window.scrollTo(0, 0);
  }
  function closeEditor() { $("[data-panel=editor]", root).hidden = true; openTab("products"); }
  $("[data-editor-cancel]", root).addEventListener("click", closeEditor);

  form.elements.name.addEventListener("input", autoId); form.elements.model.addEventListener("input", autoId);
  form.elements.id.addEventListener("input", function () { form.elements.id.dataset.auto = ""; });
  function autoId() { var f = form.elements; if (f.id.dataset.auto) f.id.value = slug(f.model.value || f.name.value); }

  form.addEventListener("click", function (e) {
    var t = e.target;
    if (t.closest("[data-rm]")) { t.closest(".pair, .tblock").remove(); }
    if (t.matches("[data-add-param]")) $("[data-params]", form).insertAdjacentHTML("beforeend", pairRow("", "", "Параметр", "Значение"));
    if (t.matches("[data-add-facet]")) $("[data-facets]", form).insertAdjacentHTML("beforeend", pairRow("", "", "Фильтр, напр. Размер", "Значение, напр. 600×600"));
    if (t.matches("[data-add-table]")) $("[data-tables]", form).insertAdjacentHTML("beforeend", tableBlock(null));
    if (t.matches("[data-add-doc]")) $("[data-docs]", form).insertAdjacentHTML("beforeend", docRow(null));
    var up = t.closest("[data-img-up]"), rm = t.closest("[data-img-rm]"), nrm = t.closest("[data-new-rm]");
    if (up) { var i = +up.getAttribute("data-img-up"); state.images.unshift(state.images.splice(i, 1)[0]); renderImages(); }
    if (rm) { state.images.splice(+rm.getAttribute("data-img-rm"), 1); renderImages(); }
    if (nrm) { state.newImages.splice(+nrm.getAttribute("data-new-rm"), 1); renderImages(); }
  });
  form.elements.photos.addEventListener("change", function (e) {
    Array.prototype.forEach.call(e.target.files, function (file) {
      if (!/^image\//.test(file.type)) { toast("«" + file.name + "» — не изображение", true); return; }
      if (file.size > 15 * 1024 * 1024) { toast("«" + file.name + "» больше 15 МБ", true); return; }
      var rd = new FileReader();
      rd.onload = function () { state.newImages.push({ name: file.name, data: rd.result }); renderImages(); };
      rd.readAsDataURL(file);
    });
    e.target.value = "";
  });

  function collect() {
    var f = form.elements;
    var pairs = function (box) { return $$(".pair", box).map(function (r) { var i = $$("input", r); return [i[0].value.trim(), i[1].value.trim()]; }).filter(function (x) { return x[0] && x[1]; }); };
    var facets = {}; pairs($("[data-facets]", form)).forEach(function (x) { facets[x[0]] = x[1]; });
    var tables = $$(".tblock", form).map(function (b) {
      var rows = $("[data-t-body]", b).value.split("\n").map(function (l) { return l.split("|").map(function (c) { return c.trim(); }); }).filter(function (r) { return r.join("") !== ""; });
      return { title: $("[data-t-title]", b).value.trim(), cols: rows[0] || [], rows: rows.slice(1) };
    }).filter(function (t) { return t.cols.length && t.rows.length; });
    var docs = $$(".pair", $("[data-docs]", form)).map(function (r) { return { doc: $("select", r).value, pages: $("input", r).value.trim() }; });
    var p = {
      id: f.id.value.trim(), name: f.name.value.trim(), model: f.model.value.trim(), summary: f.summary.value.trim(),
      description: f.description.value.trim(), purpose: f.purpose.value.trim(), variants: f.variants.value.trim(), maker: f.maker.value.trim(),
      cats: $$("[data-cats] input:checked", form).map(function (c) { return c.value; }),
      features: splitLines(f.features.value), kit: splitLines(f.kit.value), missing: splitLines(f.missing.value),
      params: pairs($("[data-params]", form)), facets: facets, tables: tables, docs: docs,
      related: f.related.value.split(/[\s,]+/).filter(Boolean), img: state.images.slice()
    };
    if (f.air_min.value && f.air_max.value) p.airflow = [f.air_min.value, f.air_max.value];
    return p;
  }
  function savedMsg(r, what) {
    return r.mode === "github" ? what + ". Сайт пересоберётся и обновится через 1–3 минуты (коммит " + r.commit + ")." : what + " и сайт пересобран (локальный режим).";
  }
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var msg = $("[data-editor-msg]", form), btn = $("[type=submit]", form);
    var p = collect();
    if (!p.cats.length) { msg.textContent = "Отметьте хотя бы один раздел каталога."; return; }
    btn.disabled = true; btn.textContent = "Сохранение…"; msg.textContent = "";
    api("POST", "/manager/products", { original_id: state.editing, product: p, new_images: state.newImages })
      .then(function (r) { toast(savedMsg(r, "Сохранено")); closeEditor(); })
      .catch(function (err) { msg.textContent = err.message; })
      .then(function () { btn.disabled = false; btn.textContent = "Сохранить"; });
  });

  start();
})();
