/* 关系识别面板 —— 左侧导航 / 方角 / 真接口 */
(function () {
  "use strict";

  var PLUGIN = "astrbot_plugin_user_tag";
  var API_PREFIX = "page";
  var HTTP_BASES = [
    "/api/v1/plugins/extensions/" + PLUGIN + "/page",
    "/api/plug/" + PLUGIN + "/page"
  ];

  var NAV = [
    { title: "关系", items: [{ id: "users", label: "用户关系" }, { id: "user", label: "单个用户" }] },
    { title: "概览", items: [{ id: "overview", label: "总览" }, { id: "stats", label: "互动统计" }, { id: "history", label: "变更历史" }] },
    { title: "对接", items: [{ id: "export", label: "对外对接" }] },
    { title: "规则", items: [{ id: "rules", label: "关系规则" }, { id: "templates", label: "提示词模板" }] }
  ];

  var TITLES = {
    users: "用户关系", user: "单个用户", overview: "总览", stats: "互动统计",
    history: "变更历史", export: "对外对接", rules: "关系规则", templates: "提示词模板"
  };

  var state = {
    view: "users",
    bot: "",
    uid: "",
    q: "",
    cat: "",
    sort: "recent",
    tpl: "",
    meta: null
  };

  /* ---------------- 工具 ---------------- */

  function $(id) { return document.getElementById(id); }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function fmtTime(ts) {
    var v = num(ts);
    if (!v) return "—";
    var d = new Date(v * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fmtDays(ts) {
    var v = num(ts);
    if (!v) return "—";
    var days = Math.floor((Date.now() / 1000 - v) / 86400);
    if (days <= 0) return "今天";
    return days + " 天";
  }

  function setStatus(text, cls) {
    var n = $("navStatus");
    if (!n) return;
    n.textContent = text;
    n.className = cls || "dot-idle";
  }

  function banner(msg, kind) {
    var b = $("banner");
    if (!b) return;
    if (!msg) { b.hidden = true; b.textContent = ""; return; }
    b.hidden = false;
    b.className = "banner" + (kind === "warn" ? " warn" : "");
    b.textContent = msg;
  }

  function hint(text) { var n = $("viewHint"); if (n) n.textContent = text || ""; }

  /* ---------------- 桥 / 接口 ---------------- */

  function getBridge() {
    return window.AstrBotPluginView || window.AstrBotPluginPage || null;
  }

  async function waitBridge(timeout) {
    timeout = timeout || 5000;
    var end = Date.now() + timeout;
    var b = getBridge();
    while (!b && Date.now() < end) {
      await sleep(80);
      b = getBridge();
    }
    if (b && typeof b.ready === "function") {
      try { await Promise.race([b.ready(), sleep(2000)]); } catch (e) { /* 桥没给上下文也能用 */ }
    }
    return b;
  }

  function unwrap(data) {
    if (data && typeof data === "object") {
      if (data.status === "error") throw new Error(data.message || "接口返回错误");
      if (data.ok === false) throw new Error(data.error || "接口返回错误");
      // 插件接口统一回 {ok:true, data:{...}}，把里层掏出来
      if (data.ok === true && Object.prototype.hasOwnProperty.call(data, "data")) {
        return data.data;
      }
    }
    return data;
  }

  async function httpReq(path, params, body) {
    var qs = "";
    if (params) {
      var us = [];
      for (var k in params) {
        if (!Object.prototype.hasOwnProperty.call(params, k)) continue;
        if (params[k] === "" || params[k] == null) continue;
        us.push(encodeURIComponent(k) + "=" + encodeURIComponent(params[k]));
      }
      if (us.length) qs = "?" + us.join("&");
    }
    var lastErr = null;
    for (var i = 0; i < HTTP_BASES.length; i++) {
      try {
        var r = await fetch(HTTP_BASES[i] + "/" + path + qs, {
          method: body === undefined ? "GET" : "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: body === undefined ? {} : { "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body)
        });
        var data = null;
        try { data = await r.json(); } catch (e2) { data = null; }
        if (!data) throw new Error("HTTP " + r.status);
        return unwrap(data);
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr || new Error("请求失败");
  }

  async function api(path, params, body) {
    var b = await waitBridge(3000);
    if (b && typeof b.apiGet === "function") {
      var data = body === undefined
        ? await b.apiGet(API_PREFIX + "/" + path, params || {})
        : await b.apiPost(API_PREFIX + "/" + path, body);
      return unwrap(data);
    }
    return httpReq(path, params, body);
  }

  async function getMeta() {
    if (state.meta) return state.meta;
    state.meta = await api("meta");
    return state.meta;
  }

  /* ---------------- 导航 ---------------- */

  function renderNav() {
    var box = $("nav");
    box.innerHTML = NAV.map(function (group) {
      var items = group.items.map(function (it) {
        return '<a class="nav-item' + (it.id === state.view ? " on" : "") +
          '" data-view="' + it.id + '">' + esc(it.label) + "</a>";
      }).join("");
      return '<details class="nav-group" open><summary>' + esc(group.title) +
        "</summary>" + items + "</details>";
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".nav-item"), function (a) {
      a.addEventListener("click", function () { go(a.getAttribute("data-view")); });
    });
  }

  function markNav() {
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (a) {
      a.classList.toggle("on", a.getAttribute("data-view") === state.view);
    });
    $("viewTitle").textContent = TITLES[state.view] || "关系识别";
  }

  function go(view, opts) {
    opts = opts || {};
    state.view = view;
    if (opts.bot !== undefined) state.bot = opts.bot;
    if (opts.uid !== undefined) state.uid = opts.uid;
    markNav();
    render();
  }

  async function render() {
    var box = $("view");
    box.innerHTML = '<div class="empty">加载中…</div>';
    try {
      if (state.view === "users") await viewUsers(box);
      else if (state.view === "user") await viewUser(box);
      else if (state.view === "overview") await viewOverview(box);
      else if (state.view === "stats") await viewStats(box);
      else if (state.view === "history") await viewHistory(box);
      else if (state.view === "export") await viewExport(box);
      else if (state.view === "rules") await viewRules(box);
      else if (state.view === "templates") await viewTemplates(box);
      else box.innerHTML = '<div class="empty">没有这个页面。</div>';
      setStatus("已连接", "dot-ok");
    } catch (e) {
      var msg = (e && e.message) || String(e);
      var extra = "";
      if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
        extra = "\n请求根本没发出去：页面没拿到 AstrBot 的桥对象（window.AstrBotPluginView），HTTP 兜底也被同源策略挡住。通常是 AstrBot 没有往这个视图注入桥脚本——检查 views/关系面板/index.html 里是否出现 bridge 脚本地址字样（出现会阻止自动注入）。";
      }
      box.innerHTML = '<div class="empty">这一页没加载出来：' + esc(msg) + "</div>";
      banner("连接失败：" + msg + extra);
      setStatus("连接失败", "dot-bad");
    }
  }

  /* ---------------- 视图：用户关系 ---------------- */

  async function viewUsers(box) {
    var meta = await getMeta();
    var data = await api("users", {
      q: state.q, category: state.cat, sort: state.sort, limit: 200
    });
    hint(data.total + " 条关系 · 第 " + data.page + "/" + data.pages + " 页");

    var catOpts = ['<option value="">全部类别</option>'].concat(
      (meta.categories || []).map(function (c) {
        return '<option value="' + esc(c.name) + '"' + (state.cat === c.name ? " selected" : "") +
          ">" + esc(c.name) + "（" + c.count + "）</option>";
      })
    ).join("");

    var sortOpts = [["recent", "按最近互动"], ["affection", "按好感度"], ["name", "按关系名"], ["uid", "按 QQ"]]
      .map(function (p) {
        return '<option value="' + p[0] + '"' + (state.sort === p[0] ? " selected" : "") + ">" + p[1] + "</option>";
      }).join("");

    var cards = (data.items || []).map(function (r) {
      return '<div class="card" data-bot="' + esc(r.bot) + '" data-uid="' + esc(r.uid) + '">' +
        '<div class="row1"><span class="rel">' + esc(r.relation || "（无）") + "</span>" +
        '<span class="uid">' + esc(r.uid) + "</span>" +
        '<span class="tag">' + esc(r.bot) + "</span>" +
        (r.expired ? '<span class="tag bad">已过期</span>' : "") + "</div>" +
        '<div class="row2">' +
        "<span>" + esc(r.category || "自定义") + "</span>" +
        "<span>好感 <b>" + r.affection + "</b> · " + esc(r.phase || "—") + "</span>" +
        "<span>互动 <b>" + r.count + "</b> 次</span>" +
        "<span>最近 " + esc(r.last_text || "—") + "</span>" +
        "</div>" +
        (r.note ? '<div class="row2"><span>备注：' + esc(r.note) + "</span></div>" : "") +
        "</div>";
    }).join("");

    box.innerHTML =
      '<div class="block"><div class="block-body">' +
      '<div class="row">' +
      '<div class="field" style="flex:1 1 220px"><label>搜（QQ / 关系 / 备注）</label>' +
      '<input type="text" id="q" value="' + esc(state.q) + '" placeholder="输入后回车"></div>' +
      '<div class="field"><label>类别</label><select id="cat">' + catOpts + "</select></div>" +
      '<div class="field"><label>排序</label><select id="sort">' + sortOpts + "</select></div>" +
      '<button class="btn" id="btnFind">查询</button>' +
      "</div></div></div>" +
      (cards ? '<div class="grid">' + cards + "</div>"
             : '<div class="block"><div class="empty">没有符合条件的关系。</div></div>');

    $("btnFind").addEventListener("click", function () {
      state.q = $("q").value.trim();
      state.cat = $("cat").value;
      state.sort = $("sort").value;
      render();
    });
    $("q").addEventListener("keydown", function (e) {
      if (e.key === "Enter") $("btnFind").click();
    });
    $("cat").addEventListener("change", function () { $("btnFind").click(); });
    $("sort").addEventListener("change", function () { $("btnFind").click(); });

    Array.prototype.forEach.call(box.querySelectorAll(".card"), function (c) {
      c.addEventListener("click", function () {
        go("user", { bot: c.getAttribute("data-bot"), uid: c.getAttribute("data-uid") });
      });
    });
  }

  /* ---------------- 视图：单个用户 ---------------- */

  async function viewUser(box) {
    if (!state.uid) {
      var pick = await api("users", { limit: 1 });
      var first = (pick.items || [])[0];
      if (first) {
        state.bot = first.bot;
        state.uid = first.uid;
      } else {
        hint("");
        box.innerHTML = '<div class="block"><div class="empty">还没有任何用户关系。</div></div>';
        return;
      }
    }
    var d = await api("user", { bot: state.bot, uid: state.uid });
    var meta = await getMeta();
    var allNames = [];
    (meta.categories || []).forEach(function (c) { allNames = allNames.concat(c.names || []); });
    var dl = '<datalist id="relList">' + allNames.map(function (n) {
      return '<option value="' + esc(n) + '"></option>';
    }).join("") + "</datalist>";

    hint(d.uid + " · " + (d.relation || "无关系"));

    var trend = (d.trend || []).map(function (t) {
      return '<tr><td class="mono">' + esc(t.date) + '</td><td class="num">' + t.count + "</td></tr>";
    }).join("");

    var hist = (d.history || []).map(function (h) {
      return "<tr><td>" + esc(h.at_text) + "</td><td>" + esc(h.from || "（无）") + "</td><td>" +
        esc(h.to || "（清除）") + "</td><td>" + esc(h.by_text || h.by) + "</td><td>" +
        esc(h.note || "") + "</td></tr>";
    }).join("");

    var groups = (d.groups || []).map(function (g) {
      return "<tr><td class=\"mono\">" + esc(g.umo) + "</td><td>" + esc(g.relation) + "</td></tr>";
    }).join("");

    box.innerHTML = dl +
      '<div class="block"><h2>这个人 <span class="sub">' + esc(d.uid) + "</span></h2>" +
      '<div class="block-body"><div class="kv">' +
      '<div class="k">当前关系</div><div class="v">' + esc(d.relation || "（无）") +
      (d.category ? ' <span class="tag">' + esc(d.category) + "</span>" : "") + "</div>" +
      '<div class="k">好感度 / 阶段</div><div class="v">' + d.affection + " · " + esc(d.phase || "—") + "</div>" +
      '<div class="k">备注</div><div class="v">' + esc(d.note || "（无）") + "</div>" +
      '<div class="k">来源</div><div class="v">' + esc(d.src_text || d.src || "—") + "</div>" +
      '<div class="k">互动次数</div><div class="v">' + d.count + " 次</div>" +
      '<div class="k">认识 / 最近</div><div class="v">' + esc(d.first_text || "—") + " · " + esc(d.last_text || "—") + "</div>" +
      "</div></div></div>" +

      '<div class="block"><h2>改关系</h2><div class="block-body">' +
      '<div class="row">' +
      '<div class="field" style="flex:1 1 200px"><label>关系名（可输任意自定义）</label>' +
      '<input type="text" id="rel" list="relList" value="' + esc(d.relation) + '" placeholder="例如 挚友"></div>' +
      '<div class="field" style="flex:1 1 200px"><label>备注（可留空，最长 60）</label>' +
      '<input type="text" id="note" value="' + esc(d.note) + '" placeholder="例如 他叫小明"></div>' +
      '<button class="btn btn-primary" id="btnSave">保存关系</button>' +
      '<button class="btn" id="btnNote">只改备注</button>' +
      '<button class="btn btn-danger" id="btnClear">清除关系</button>' +
      "</div></div></div>" +

      '<div class="block"><h2>注入预览 <span class="sub">下次开口时真正塞进提示词的那段</span></h2>' +
      '<div class="block-body"><pre class="out">' + esc(d.hint_preview || "（无）") + "</pre></div></div>" +

      '<div class="block"><h2>群内覆盖 <span class="sub">同一个 QQ 在不同群可以不同关系</span></h2>' +
      '<div class="block-body">' +
      '<div class="row">' +
      '<div class="field" style="flex:2 1 280px"><label>群的 umo（例如 aiocqhttp:GroupMessage:123456）</label>' +
      '<input type="text" id="umo" placeholder="aiocqhttp:GroupMessage:"></div>' +
      '<div class="field" style="flex:1 1 160px"><label>该群里的关系</label>' +
      '<input type="text" id="grel" list="relList" placeholder="例如 同学"></div>' +
      '<button class="btn" id="btnGroup">写入</button>' +
      "</div>" +
      (groups ? '<div class="spliter"></div><table class="tbl"><thead><tr><th>群</th><th>关系</th></tr></thead><tbody>' +
        groups + "</tbody></table>" : '<div class="muted" style="margin-top:10px">还没有任何群内覆盖。</div>') +
      "</div></div>" +

      '<div class="block"><h2>变更历史 <span class="sub">最近 ' + (d.history || []).length + " 条</span></h2>" +
      '<div class="block-body tight">' +
      (hist ? '<table class="tbl"><thead><tr><th>时间</th><th>从</th><th>到</th><th>谁改的</th><th>备注</th></tr></thead><tbody>' +
        hist + "</tbody></table>" : '<div class="empty">还没有变更记录。</div>') +
      "</div></div>" +

      '<div class="block"><h2>近 14 天互动</h2><div class="block-body tight">' +
      (trend ? '<table class="tbl"><thead><tr><th>日期</th><th>句数</th></tr></thead><tbody>' + trend + "</tbody></table>"
             : '<div class="empty">还没有互动统计。</div>') +
      "</div></div>";

    $("btnSave").addEventListener("click", function () {
      act("保存关系", api("set", undefined, {
        bot: d.bot, uid: d.uid, relation: $("rel").value.trim(), note: $("note").value
      }));
    });
    $("btnNote").addEventListener("click", function () {
      act("改备注", api("note", undefined, { bot: d.bot, uid: d.uid, note: $("note").value }));
    });
    $("btnClear").addEventListener("click", function () {
      if (!window.confirm("确定清除 " + d.uid + " 的关系？")) return;
      act("清除关系", api("clear", undefined, { bot: d.bot, uid: d.uid }));
    });
    $("btnGroup").addEventListener("click", function () {
      var umo = $("umo").value.trim();
      if (!umo) { banner("先填群的 umo。", "warn"); return; }
      act("写入群内覆盖", api("group-set", undefined, {
        bot: d.bot, uid: d.uid, umo: umo, relation: $("grel").value.trim()
      }));
    });
  }

  async function act(label, promise) {
    banner("");
    try {
      var res = await promise;
      banner(label + "：成功" + (res && res.saved === false ? "（但没写进磁盘，看插件日志）" : ""), "warn");
      await render();
    } catch (e) {
      banner(label + "：失败 —— " + ((e && e.message) || e));
    }
  }

  /* ---------------- 视图：总览 ---------------- */

  async function viewOverview(box) {
    var d = await api("overview");
    hint("生成于 " + fmtTime(d.generated_at));
    var cats = (d.categories || []).map(function (c) {
      var max = (d.categories || []).reduce(function (m, x) { return Math.max(m, x.count); }, 1) || 1;
      var w = Math.round((c.count / max) * 100);
      return "<tr><td>" + esc(c.name) + '</td><td class="num">' + c.count +
        '</td><td style="width:55%"><div class="bar-track"><div class="bar-fill" style="width:' + w + '%"></div></div></td></tr>';
    }).join("");
    var ev = (d.recent_events || []).map(function (e) {
      return "<tr><td>" + esc(e.at_text) + "</td><td>" + esc(e.kind) + "</td><td class=\"mono\">" +
        esc(e.uid) + "</td><td>" + esc(e.text) + "</td></tr>";
    }).join("");

    box.innerHTML =
      '<div class="tiles">' +
      tile("机器人", d.bots) + tile("有关系的用户", d.total_users) +
      tile("今天说过话", d.active_today) + tile("已过期", d.expiring) +
      "</div>" +
      '<div class="block"><h2>关系类别分布</h2><div class="block-body tight">' +
      (cats ? '<table class="tbl"><thead><tr><th>类别</th><th>人数</th><th>占比</th></tr></thead><tbody>' +
        cats + "</tbody></table>" : '<div class="empty">还没有任何关系。</div>') +
      "</div></div>" +
      '<div class="block"><h2>最近变更 <span class="sub">最近 12 条</span></h2><div class="block-body tight">' +
      (ev ? '<table class="tbl"><thead><tr><th>时间</th><th>类型</th><th>QQ</th><th>内容</th></tr></thead><tbody>' +
        ev + "</tbody></table>" : '<div class="empty">还没有变更。</div>') +
      "</div></div>";
  }

  function tile(k, v) {
    return '<div class="tile"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + "</div></div>";
  }

  /* ---------------- 视图：互动统计 ---------------- */

  async function viewStats(box) {
    var data = await api("users", { sort: "affection", limit: 200 });
    var items = (data.items || []).slice().sort(function (a, b) { return b.count - a.count; });
    hint("按互动次数排序 · 共 " + data.total + " 人");
    var rows = items.map(function (r) {
      return "<tr><td class=\"mono\">" + esc(r.uid) + "</td><td>" + esc(r.relation || "（无）") +
        '</td><td class="num">' + r.count + '</td><td class="num">' + r.affection +
        "</td><td>" + esc(r.phase || "—") + "</td><td>" + esc(fmtDays(r.first_at)) +
        "</td><td>" + esc(r.last_text) + "</td></tr>";
    }).join("");
    box.innerHTML = '<div class="block"><h2>互动统计</h2><div class="block-body tight">' +
      (rows ? '<table class="tbl"><thead><tr><th>QQ</th><th>关系</th><th>句数</th><th>好感</th><th>阶段</th><th>认识</th><th>最近</th></tr></thead><tbody>' +
        rows + "</tbody></table>" : '<div class="empty">还没有互动记录。这个表靠插件在每次请求时累加，装了之后开始记。</div>') +
      "</div></div>";
  }

  /* ---------------- 视图：变更历史 ---------------- */

  async function viewHistory(box) {
    var data = await api("events", { limit: 200 });
    var items = data.items || [];
    hint("共 " + items.length + " 条");
    var rows = items.slice().reverse().map(function (e) {
      return "<tr><td>" + esc(e.at_text) + "</td><td>" + esc(e.kind) + '</td><td class="mono">' +
        esc(e.bot) + '</td><td class="mono">' + esc(e.uid) + "</td><td>" + esc(e.text) + "</td></tr>";
    }).join("");
    box.innerHTML = '<div class="block"><h2>全部变更</h2><div class="block-body tight">' +
      (rows ? '<table class="tbl"><thead><tr><th>时间</th><th>类型</th><th>机器人</th><th>QQ</th><th>内容</th></tr></thead><tbody>' +
        rows + "</tbody></table>" : '<div class="empty">还没有任何变更。</div>') +
      "</div></div>";
  }

  /* ---------------- 视图：对外对接 ---------------- */

  async function viewExport(box) {
    var st = await api("export/status");
    var pv = null;
    try { pv = await api("export/preview"); } catch (e) { pv = { error: (e && e.message) || String(e) }; }

    hint(st.enabled ? "已开启" : "未开启");

    var occupied = st.occupied_by ? '<span class="tag warn">' + esc(st.occupied_by) + "</span>" : "";
    var effective = st.effective
      ? '<span class="tag ok">下游能读到</span>'
      : '<span class="tag bad">下游读不到</span>' + occupied;

    var kv = [
      ["开关", st.enabled ? "开" : "关（插件配置 export_to_social）"],
      ["下游是否读得到", effective],
      ["实际读取路径", '<span class="mono">' + esc(st.read_path || "—") + "</span>"],
      ["写入路径", '<span class="mono">' + esc(st.path || "—") + "</span>"],
      ["文件存在", st.exists ? "是" : "否"],
      ["上次写入", esc(st.last_written_at ? fmtTime(st.last_written_at) : "—")],
      ["上次结果", esc(st.last_status || "—") + (st.last_error ? "：" + esc(st.last_error) : "")],
      ["跳过原因", esc(st.skipped_reason || "—")],
      ["心跳间隔", st.interval_min + " 分钟"],
      ["过期阈值", Math.round((st.stale_seconds || 0) / 60) + " 分钟"]
    ].map(function (p) {
      return '<div class="k">' + esc(p[0]) + '</div><div class="v">' + p[1] + "</div>";
    }).join("");

    var pvText = pv && pv.payload ? JSON.stringify(pv.payload, null, 2)
      : (pv && pv.error ? "预览失败：" + pv.error : "（无）");

    box.innerHTML =
      '<div class="block"><h2>导出状态</h2><div class="block-body"><div class="kv">' + kv + "</div>" +
      '<div class="spliter"></div>' +
      '<div class="row"><button class="btn btn-primary" id="btnRun">立即写一次</button>' +
      '<button class="btn" id="btnForce">强制覆盖一次</button></div>' +
      '<div class="muted" style="margin-top:8px">装了 Humanoid Core 时，它自己那份状态优先，这个导出会自动让位（不覆盖别人两兆多的文件）。</div>' +
      "</div></div>" +
      '<div class="block"><h2>将要写出去的内容 <span class="sub">schema / 7 项 capabilities / fields_hash</span></h2>' +
      '<div class="block-body"><pre class="out">' + esc(pvText) + "</pre></div></div>";

    $("btnRun").addEventListener("click", function () { act("写一次", api("export/run", undefined, { force: false })); });
    $("btnForce").addEventListener("click", function () {
      if (!window.confirm("强制覆盖会把目标文件整个换成我们的内容，确定？")) return;
      act("强制覆盖", api("export/run", undefined, { force: true }));
    });
  }

  /* ---------------- 视图：关系规则 ---------------- */

  async function viewRules(box) {
    var d = await api("rules");
    hint("只读");
    var rows = (d.rules || []).map(function (r) {
      return "<tr><td><b>" + esc(r.category) + "</b></td><td>" + esc(r.blurb) + '</td><td class="num">' +
        r.affection[0] + " – " + r.affection[1] + "</td></tr>";
    }).join("");
    var phases = (d.phases || []).map(function (p) {
      return "<tr><td>" + esc(p.name) + '</td><td class="num">' + p.threshold + "</td></tr>";
    }).join("");
    box.innerHTML =
      '<div class="block"><h2>类别与好感区间</h2><div class="block-body tight">' +
      '<table class="tbl"><thead><tr><th>类别</th><th>这类关系怎么相处</th><th>好感区间</th></tr></thead><tbody>' +
      rows + "</tbody></table></div></div>" +
      '<div class="block"><h2>阶段门槛</h2><div class="block-body tight">' +
      '<table class="tbl"><thead><tr><th>阶段</th><th>好感达到</th></tr></thead><tbody>' + phases +
      "</tbody></table></div></div>" +
      '<div class="block"><h2>怎么算的</h2><div class="block-body"><div class="muted">' + esc(d.formula) + "</div></div></div>";
  }

  /* ---------------- 视图：提示词模板 ---------------- */

  async function viewTemplates(box) {
    var d = await api("templates");
    if (state.tpl === "" && d.current) state.tpl = d.current;
    var chips = (d.variables || []).map(function (v) {
      return '<span class="chip" data-v="' + esc(v.name) + '" title="' + esc(v.desc) + '">' +
        esc(v.name) + "</span>";
    }).join("");

    box.innerHTML =
      '<div class="block"><h2>模板 <span class="sub">留空 = 用默认那句</span></h2><div class="block-body">' +
      '<div class="field"><label>变量（点一下就插到光标处）</label><div class="chips">' + chips + "</div></div>" +
      '<div class="field"><textarea id="tpl" rows="5" placeholder="留空就用默认那句（默认写在下面）">' +
      esc(state.tpl || d.current) + "</textarea></div>" +
      '<div class="row">' +
      '<button class="btn btn-primary" id="btnSaveTpl">保存到插件配置</button>' +
      '<button class="btn" id="btnPreview">预览</button>' +
      '<button class="btn" id="btnDefault">填回默认</button>' +
      '<button class="btn btn-danger" id="btnReset">清空（回到默认行为）</button>' +
      "</div>" +
      '<div class="muted" style="margin-top:8px">默认：<span class="mono">' + esc(d.default) + "</span></div>" +
      "</div></div>" +
      '<div class="block"><h2>预览 <span class="sub">拿真实用户渲染，不会改配置</span></h2>' +
      '<div class="block-body"><pre class="out" id="tplOut">点「预览」看效果。</pre></div></div>';

    var ta = $("tpl");

    Array.prototype.forEach.call(box.querySelectorAll(".chip"), function (c) {
      c.addEventListener("click", function () {
        var v = c.getAttribute("data-v");
        var s = ta.selectionStart || 0;
        var e = ta.selectionEnd || 0;
        ta.value = ta.value.slice(0, s) + v + ta.value.slice(e);
        ta.focus();
        ta.selectionStart = ta.selectionEnd = s + v.length;
        state.tpl = ta.value;
      });
    });

    ta.addEventListener("input", function () { state.tpl = ta.value; });

    $("btnPreview").addEventListener("click", async function () {
      try {
        var r = await api("templates/preview", undefined, { template: ta.value });
        $("tplOut").textContent = r.text || "（空）";
        if (r.error) banner("预览出错：" + r.error);
        else if (r.fallback) banner("模板里的变量没对上，实际注入会退回默认那句。", "warn");
        else banner("");
      } catch (e) {
        banner("预览失败：" + ((e && e.message) || e));
      }
    });

    $("btnSaveTpl").addEventListener("click", async function () {
      try {
        var r = await api("templates/save", undefined, { template: ta.value });
        banner(r.persisted ? "模板已保存到插件配置。" : (r.note || "只在本进程生效。"), r.persisted ? "warn" : "warn");
        state.tpl = ta.value;
      } catch (e) {
        banner("保存失败：" + ((e && e.message) || e));
      }
    });

    $("btnDefault").addEventListener("click", function () { ta.value = d.default; state.tpl = d.default; });
    $("btnReset").addEventListener("click", function () { ta.value = ""; state.tpl = ""; });
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    var deep = (location.hash || "").replace(/^#\/?/, "");
    if (deep && TITLES[deep]) state.view = deep;
    renderNav();
    markNav();
    $("btnReload").addEventListener("click", function () { render(); });

    var b = await waitBridge(4000);
    if (!b) {
      banner("没拿到 AstrBot 的页面桥（window.AstrBotPluginView）。这通常是 AstrBot 没往这个视图注入桥脚本——检查 views/关系面板/index.html 里有没有出现桥脚本地址字样。", "warn");
      setStatus("没有桥", "dot-bad");
    } else {
      try {
        var ctx = (typeof b.getContext === "function" && b.getContext()) || {};
        if (ctx.isDark) document.documentElement.setAttribute("data-theme", "dark");
        else document.documentElement.setAttribute("data-theme", "light");
        if (typeof b.onContext === "function") {
          b.onContext(function (c) {
            document.documentElement.setAttribute("data-theme", c && c.isDark ? "dark" : "light");
          });
        }
      } catch (e) { /* 主题拿不到就算了 */ }
    }

    try {
      var meta = await getMeta();
      $("navVer").textContent = "v" + (meta.plugin_version || "?") + " · 数据 v" + meta.data_version;
      state.bot = state.bot || (meta.bots && meta.bots[0]) || "";
    } catch (e) { /* 交给 render 报错 */ }

    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
