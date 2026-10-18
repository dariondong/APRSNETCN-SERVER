/* APRSNETCN SERVER 控制台 */
(function () {
  'use strict';

  var token = sessionStorage.getItem('acnAdmin') || '';
  var state = {
    tab: 'overview',
    data: null,
    accounts: [],
    st: { offset: 0, total: 0 },
    au: { kind: '', q: '', before: null, rows: [], touched: false },
    set: { rows: [], original: {} },
    map: null
  };
  var timers = { overview: 0, map: 0, packets: 0 };

  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function stamp(iso) {
    return String(iso || '').slice(5, 16).replace('T', ' ');
  }

  function clockOf(epoch) {
    if (!epoch) return '—';
    var date = new Date(epoch * 1000);
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return two(date.getMonth() + 1) + '-' + two(date.getDate()) + ' ' +
      two(date.getHours()) + ':' + two(date.getMinutes());
  }

  function num(value, digits) {
    return (typeof value === 'number' && !isNaN(value)) ? value.toFixed(digits) : '—';
  }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({
      'X-ACN-Token': token,
      'Content-Type': 'application/json'
    }, options.headers || {});
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (res.status === 401) {
          logout('口令不对，或者已经被改掉了。');
          throw new Error('unauthorized');
        }
        if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
        return body;
      });
    });
  }

  /* ── 登录 ── */
  function enter() {
    var value = $('pw').value.trim();
    if (!value) { $('loginNote').textContent = '先填口令。'; return; }
    token = value;
    sessionStorage.setItem('acnAdmin', token);
    $('login').hidden = true;
    $('tabs').hidden = false;
    $('app').hidden = false;
    $('refreshBtn').hidden = false;
    $('logoutBtn').hidden = false;
    loadState();
    showTab(location.hash ? location.hash.slice(1) : 'overview', true);
  }

  function logout(note) {
    sessionStorage.removeItem('acnAdmin');
    token = '';
    stopTimer('overview'); stopTimer('map'); stopTimer('packets');
    $('login').hidden = false;
    $('tabs').hidden = true;
    $('app').hidden = true;
    $('refreshBtn').hidden = true;
    $('logoutBtn').hidden = true;
    $('pw').value = '';
    $('loginNote').textContent = note || '';
    $('pw').focus();
  }

  /* ── 标签页 ── */
  var LOADERS = {};

  function showTab(name, force) {
    var known = { overview: 1, map: 1, clients: 1, packets: 1, stations: 1,
      names: 1, settings: 1, audits: 1 };
    if (!known[name]) name = 'overview';
    if (state.tab === name && !force) return;
    state.tab = name;
    var buttons = $('tabs').getElementsByTagName('button');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].className = buttons[i].getAttribute('data-tab') === name ? 'on' : '';
    }
    var panels = document.querySelectorAll('.tab');
    for (var j = 0; j < panels.length; j++) {
      panels[j].className = panels[j].getAttribute('data-panel') === name ? 'tab on' : 'tab';
    }
    if (history.replaceState) history.replaceState(null, '', '#' + name);
    stopTimer('overview'); stopTimer('map'); stopTimer('packets');
    if (LOADERS[name]) LOADERS[name]();
    $('topSub').textContent = {
      overview: '总览', map: '台站地图', clients: '在线连接', packets: '报文',
      stations: '24 小时台站', names: '名单与账号', settings: '运行参数', audits: '审计'
    }[name];
  }

  function stopTimer(name) {
    if (timers[name]) { window.clearInterval(timers[name]); timers[name] = 0; }
  }

  /* ── 总览 ── */
  function loadState() {
    return api('/api/admin/state').then(function (body) {
      state.data = body;
      state.accounts = body.accounts || [];
      paintCards(body.stats || {}, body.meta || {});
      paintParams(body.stats || {}, body.meta || {});
      paintHours((body.stats || {}).hourly || []);
      paintRecentAudits(body.audits || []);
      paintAccounts(state.accounts);
      paintWhitelist(body.meta || {});
      $('topWhen').textContent = '最后刷新 ' + stamp(body.time);
      return body;
    }).catch(function (error) {
      if (error.message !== 'unauthorized') $('topWhen').textContent = '读不到：' + error.message;
    });
  }

  function paintCards(stats, meta) {
    var rows = [
      ['当前连接', stats.connections],
      ['累计登录', stats.logins_ok],
      ['登录失败', stats.logins_failed],
      ['收报文', stats.packets_in],
      ['发报文', stats.packets_out],
      ['24h 台站', meta.station_count],
      ['名单版本', meta.whitelist_version],
      ['管理拒绝', meta.admin_denied || 0]
    ];
    var html = '';
    rows.forEach(function (row) {
      html += '<div><div class="k">' + row[0] + '</div><div class="v">' +
        esc(row[1] == null ? '—' : row[1]) + '</div></div>';
    });
    $('cards').innerHTML = html;
  }

  function paintParams(stats, meta) {
    var archive = meta.archive || {};
    var ingest = meta.ingest || {};
    var ingestText;
    if (!ingest.enabled) {
      ingestText = '关';
    } else if (!ingest.connected) {
      ingestText = '没连上' + (ingest.error ? '（' + ingest.error + '）' : '');
    } else {
      ingestText = (ingest.host || '') + ' · 收 ' + ingest.lines + ' · 入库 ' + ingest.stored;
      if (ingest.dropped) {
        ingestText += ' · 丢 ' + ingest.dropped
            + '（坏字符 ' + (ingest.bad_chars || 0)
            + ' / 解析不了 ' + (ingest.unparsable || 0) + '）';
      }
      // 连着但半天没一行：最容易看成「没台站在报」
      if (ingest.stale) {
        ingestText += ' · 已 ' + Math.round((ingest.idle_seconds || 0) / 60) + ' 分钟没新行';
      }
    }
    var rows = [
      ['端口', (stats.ports || []).join(' / ')],
      ['台站数据', meta.public_api ? '对外（/stations）' : '不对外'],
      ['passcode', meta.passcode_mode],
      ['来源策略', meta.src_policy],
      ['账号鉴权', meta.auth_mode],
      ['位置保留', meta.retention_hours + ' 小时'],
      ['报文存档', archive.enabled ? (archive.lines + ' 行 / ' + archive.hours + ' 小时') : '关'],
      ['外部拉取', ingestText],
      ['名单同步', meta.whitelist_synced_at ? stamp(meta.whitelist_synced_at) : '从未'],
      ['外部访问', meta.public_url || '—']
    ];
    var html = '';
    rows.forEach(function (row) {
      html += '<div class="f"><span class="k">' + row[0] + '</span><span class="v">' +
        esc(row[1]) + '</span></div>';
    });
    $('params').innerHTML = html;
  }

  function paintHours(rows) {
    if (!rows.length) { $('hours').innerHTML = '<p class="note" style="padding:10px">还没有收发记录。</p>'; return; }
    var html = '<table><thead><tr><th>小时</th><th>收</th><th>发</th></tr></thead><tbody>';
    rows.forEach(function (row) {
      html += '<tr><td class="mono">' + stamp(row.hour) + '</td><td class="mono">' +
        row['in'] + '</td><td class="mono">' + row.out + '</td></tr>';
    });
    $('hours').innerHTML = html + '</tbody></table>';
  }

  function paintRecentAudits(rows) {
    var html = '<table><tbody>';
    rows.slice(0, 12).forEach(function (row) {
      html += '<tr><td class="mono" style="white-space:nowrap">' + esc(stamp(row.time)) +
        '</td><td>' + esc(row.kind) + '</td><td class="note mono">' +
        esc(JSON.stringify(row.detail)) + '</td></tr>';
    });
    $('recentAudits').innerHTML = rows.length
      ? html + '</tbody></table>'
      : '<p class="note" style="padding:10px">还没有审计记录。</p>';
  }

  /* ── 连接 ── */
  function paintClients(clients) {
    var html = '';
    clients.forEach(function (row) {
      html += '<tr>' +
        '<td class="mono">' + esc(row.call) + '</td>' +
        '<td class="mono">' + esc(row.address) + '</td>' +
        '<td>' + (row.verified ? '<span class="ok">verified</span>'
          : '<span class="bad">unverified</span>') +
        (row.read_only ? ' <span class="tag">只收</span>' : '') + '</td>' +
        '<td class="note">' + esc(row.vers || '—') + '</td>' +
        '<td class="mono">' + clockOf(row.since) + '</td>' +
        '<td class="mono">' + row.received + '</td>' +
        '<td class="mono">' + row.sent + '</td>' +
        '<td class="mono">' + row.dropped + '</td>' +
        '<td class="note">' + esc((row.filters || []).join(', ') || '—') + '</td>' +
        '<td><button class="danger" data-kick="' + esc(row.call) + '" type="button">踢</button></td>' +
        '</tr>';
    });
    $('clients').innerHTML = html ||
      '<tr><td colspan="10" class="note">现在没有连接</td></tr>';
    $('clientsNote').textContent = clients.length + ' 条在线；踢掉的连接会记一条审计。';
  }

  /* ── 报文 ── */
  function search() {
    var scope = $('qScope').value;
    var query = '?q=' + encodeURIComponent($('qText').value.trim()) +
      '&dir=' + encodeURIComponent($('qDir').value) +
      '&limit=' + (parseInt($('qLimit').value, 10) || 100);
    api('/api/admin/' + scope + query).then(paintPackets).catch(function (error) {
      $('qNote').textContent = '查失败：' + error.message;
    });
  }

  function paintPackets(body) {
    var html = '';
    (body.lines || []).forEach(function (row) {
      html += '<tr>' +
        '<td class="mono" style="white-space:nowrap">' + stamp(row.time) + '</td>' +
        '<td>' + (row.dir === 'out' ? '发' : '收') + '</td>' +
        '<td class="mono">' + esc(row.src || '—') + '</td>' +
        '<td class="mono">' + esc(row.dst || '—') + '</td>' +
        '<td class="pkt">' + esc(row.line) + '</td>' +
        '</tr>';
    });
    $('packets').innerHTML = html ||
      '<tr><td colspan="5" class="note">没查到</td></tr>';
    if (body.source === 'archive') {
      $('qNote').textContent = body.enabled
        ? ('存档共 ' + body.total + ' 行，保留 ' + body.hours + ' 小时；这次 ' + body.count + ' 行')
        : '存档没开，去设置页把「报文存档」打开。';
    } else {
      $('qNote').textContent = '内存环 ' + body.capacity + ' 行；这次 ' + body.count + ' 行';
    }
  }

  function startAuto() {
    stopTimer('packets');
    if (!$('qAuto').checked) return;
    timers.packets = window.setInterval(function () {
      if (state.tab !== 'packets') return;
      if ($('qScope').value !== 'packets') return;
      search();
    }, 2000);
  }

  /* ── 台站 ── */
  function paintDevices(body) {
    var rows = body.devices || [];
    var total = body.total_stations || 0;
    var top = rows.length ? rows[0].stations : 0;
    var chips = '';
    (body.classes || []).forEach(function (row) {
      chips += '<span class="note" style="border:1px solid var(--line);padding:1px 7px">'
        + esc(row.cls) + ' ' + row.stations + '</span>';
    });
    $('devClasses').innerHTML = chips;
    var html = '';
    rows.forEach(function (row) {
      var share = total ? (row.stations * 100 / total) : 0;
      var width = top ? Math.max(2, Math.round(row.stations * 100 / top)) : 0;
      html += '<tr>' +
        '<td>' + esc(row.model) + '</td>' +
        '<td class="note">' + esc(row.cls || '未识别') + '</td>' +
        '<td class="mono">' + row.stations + '</td>' +
        '<td><span class="devbar" style="width:' + width + '%"></span>' +
        '<span class="note mono">' + share.toFixed(1) + '%</span></td>' +
        '<td class="mono">' + esc((row.to_calls || []).join(' ')) + '</td>' +
        '</tr>';
    });
    $('devices').innerHTML = html ||
      '<tr><td colspan="5" class="note">还没有可归堆的台站</td></tr>';
    $('devNote').textContent = '按每个台站最后一条位置包的目的呼号（TOCALL）归堆，'
      + '共 ' + total + ' 个台站，认出设备名 ' + (body.identified || 0) + ' 个。'
      + '表里没有的直接写目的呼号。';
  }

  function loadDevices() {
    api('/api/admin/devices?window=24h').then(paintDevices).catch(function (error) {
      $('devNote').textContent = '设备排行没取到：' + error.message;
    });
  }

  function loadStations(reset) {
    var call_ = $('stCall').value.trim().toUpperCase();
    var limit = parseInt($('stLimit').value, 10) || 300;
    if (reset) state.st.offset = 0;
    if (call_) {
      api('/api/admin/stations/' + encodeURIComponent(call_)).then(function (body) {
        paintStations([body]);
        $('stNote').textContent = '单台站：' + body.call + '，' + body.packets + ' 个位置。';
        $('stPage').textContent = '';
        $('stPrev').disabled = true;
        $('stNext').disabled = true;
        loadTrack(body.call);
      }).catch(function (error) {
        $('stNote').textContent = '查失败：' + error.message;
      });
      return;
    }
    api('/api/admin/stations?window=24h&limit=' + limit + '&offset=' + state.st.offset)
      .then(function (body) {
        state.st.total = body.total != null ? body.total : body.count;
        paintStations(body.stations || []);
        $('stNote').textContent = body.count + ' 台，' + body.window +
          ' 窗口，' + stamp(body.updated) + ' 取的这一份。';
        if (!state.st.total) {
          $('stPage').textContent = '24 小时里没有台站';
        } else {
          $('stPage').textContent = '共 ' + state.st.total + ' 台，这次看第 ' +
            (state.st.offset + 1) + '–' + (state.st.offset + body.count) + ' 台。';
        }
        $('stPrev').disabled = state.st.offset <= 0;
        $('stNext').disabled = state.st.offset + body.count >= state.st.total;
        $('stTrackBox').hidden = true;
      }).catch(function (error) {
        $('stNote').textContent = '查失败：' + error.message;
      });
  }

  function paintStations(rows) {
    var html = '';
    rows.forEach(function (row) {
      html += '<tr>' +
        '<td class="mono">' + esc(row.call) + '</td>' +
        '<td class="mono">' + stamp(row.last_seen) + '</td>' +
        '<td class="mono">' + num(row.lat, 4) + ', ' + num(row.lon, 4) + '</td>' +
        '<td>' + (row.via === 'rf' ? '<span class="bad">射频</span>' : '网络') + '</td>' +
        '<td class="mono">' + row.packets + '</td>' +
        '<td><button data-track="' + esc(row.call) + '" type="button">轨迹</button></td>' +
        '</tr>';
    });
    $('stations').innerHTML = html ||
      '<tr><td colspan="6" class="note">24 小时里没有台站</td></tr>';
  }

  function loadTrack(call_) {
    api('/api/admin/stations/' + encodeURIComponent(call_)).then(function (body) {
      var rows = body.track || [];
      var html = '';
      rows.forEach(function (row) {
        html += '<tr>' +
          '<td class="mono">' + stamp(row.time) + '</td>' +
          '<td class="mono">' + num(row.lat, 5) + '</td>' +
          '<td class="mono">' + num(row.lon, 5) + '</td>' +
          '<td class="mono">' + num(row.speed, 0) + '</td>' +
          '<td class="mono">' + num(row.course, 0) + '</td>' +
          '</tr>';
      });
      $('stTrack').innerHTML = html ||
        '<tr><td colspan="5" class="note">没有轨迹点</td></tr>';
      $('stTrackNote').textContent = call_ + '：' + rows.length + ' 个点' +
        (body.comment ? '，「' + body.comment + '」' : '');
      $('stTrackBox').hidden = false;
    }).catch(function (error) {
      $('stNote').textContent = '轨迹没取到：' + error.message;
    });
  }

  /* ── 名单 ── */
  function paintWhitelist(meta) {
    var wl = meta.whitelist || {};
    var by = wl.by_status || {};
    var age = wl.max_age_hours;
    var rows = [
      ['名单版本', wl.version],
      ['账号总数', wl.entries],
      ['设了口令', wl.with_password],
      ['已授权', by.authorized || 0],
      ['待审核', by.pending || 0],
      ['已暂停', by.suspended || 0],
      ['已撤销', by.revoked || 0],
      ['已过期', by.expired || 0],
      ['最近同步', wl.synced_at ? stamp(wl.synced_at) : '从未'],
      ['同步时效', age ? ('超过 ' + age + ' 小时拒新连接') : '不限制']
    ];
    var html = '';
    rows.forEach(function (row) {
      html += '<div><div class="k">' + row[0] + '</div><div class="v">' +
        esc(row[1] == null ? '—' : row[1]) + '</div></div>';
    });
    $('wlSum').innerHTML = html;
  }

  function paintAccounts(accounts) {
    var html = '';
    accounts.forEach(function (row) {
      html += '<tr>' +
        '<td class="mono">' + esc(row.call) + '</td>' +
        '<td class="mono">' + esc(row.account || '') + '</td>' +
        '<td><span class="tag">' + esc(row.effective || row.status) + '</span></td>' +
        '<td class="mono">' + esc(row.valid_from || '—') + ' → ' +
        esc(row.valid_to || '长期') + '</td>' +
        '<td class="note">' + (row.has_password ? '已设' : '未设') + '</td>' +
        '<td><button data-edit="' + esc(row.call) + '" type="button">改</button> ' +
        '<button class="danger" data-del="' + esc(row.call) + '" type="button">删除</button></td>' +
        '</tr>';
    });
    $('accounts').innerHTML = html || '<tr><td colspan="6" class="note">还没有账号</td></tr>';
  }

  function fillForm(call_) {
    var row = null;
    (state.accounts || []).forEach(function (item) {
      if (item.call === call_) row = item;
    });
    if (!row) return;
    $('fCall').value = row.call || '';
    $('fAccount').value = row.account || '';
    $('fPassword').value = '';
    $('fStatus').value = row.status || 'authorized';
    $('fFrom').value = row.valid_from || '';
    $('fTo').value = row.valid_to || '';
    $('fNote').value = row.note || '';
    $('saveNote').textContent = '已把 ' + row.call + ' 填进表单，改完按保存。';
    $('fCall').scrollIntoView({ block: 'center' });
  }

  /* ── 设置 ── */
  function loadSettings() {
    $('setNote').textContent = '正在读…';
    api('/api/admin/settings').then(paintSettings).catch(function (error) {
      $('setNote').textContent = '读不到设置：' + error.message;
    });
  }

  function controlHtml(row) {
    var value = row.value;
    var key = esc(row.key);
    if (row.kind === 'bool') {
      var on = (value === true || value === 1 || value === '1');
      return '<select data-key="' + key + '">' +
        '<option value="1"' + (on ? ' selected' : '') + '>开</option>' +
        '<option value="0"' + (on ? '' : ' selected') + '>关</option></select>';
    }
    if (row.kind === 'enum') {
      var options = row.options || [];
      var html = '<select data-key="' + key + '">';
      options.forEach(function (option) {
        html += '<option value="' + esc(option) + '"' +
          (String(value) === option ? ' selected' : '') + '>' + esc(option) + '</option>';
      });
      return html + '</select>';
    }
    if (row.kind === 'secret') {
      var set = value === '已设置';
      return '<input type="password" data-key="' + key + '" value="" autocomplete="new-password" ' +
        'placeholder="' + (set ? '已设置，留空不改' : '没设，控制台关着') + '">';
    }
    if (row.kind === 'int') {
      return '<input type="number" data-key="' + key + '" value="' + esc(value) + '"' +
        (row.min != null ? ' min="' + row.min + '"' : '') +
        (row.max != null ? ' max="' + row.max + '"' : '') + '>' +
        (row.unit ? '<span class="note">' + esc(row.unit) + '</span>' : '');
    }
    return '<input type="text" data-key="' + key + '" value="' + esc(value) + '">';
  }

  function paintSettings(body) {
    var rows = body.rows || [];
    state.set.rows = rows;
    state.set.original = {};
    var groups = body.groups || [];
    var html = '';
    groups.forEach(function (group) {
      var mine = rows.filter(function (row) { return row.group === group; });
      if (!mine.length) return;
      html += '<div class="set-group"><h2>' + esc(group) + '</h2>';
      mine.forEach(function (row) {
        html += '<div class="set-row" data-row="' + esc(row.key) + '">' +
          '<div class="label">' + esc(row.label) +
            '<span class="key">' + esc(row.key.replace(/^ACN_/, '')) + '</span></div>' +
          '<div class="ctl">' + controlHtml(row) + '</div>' +
          '<div class="hint">' + esc(row.hint || '') +
            '<span class="src"> · 来自' + (row.source === 'db' ? '库' : 'env') + '</span></div>' +
          '</div>';
      });
      html += '</div>';
    });
    $('setGroups').innerHTML = html;
    var inputs = $('setGroups').querySelectorAll('[data-key]');
    for (var i = 0; i < inputs.length; i++) {
      state.set.original[inputs[i].getAttribute('data-key')] = inputs[i].value;
    }
    var facts = '';
    (body.restart_only || []).forEach(function (row) {
      facts += '<div class="f"><span class="k">' + esc(row.label) + '</span>' +
        '<span class="v">' + esc(row.value) + '</span></div>';
    });
    $('setRestart').innerHTML = facts;
    $('setNote').textContent = '共 ' + rows.length + ' 项，读于 ' + stamp(body.time) + '。';
  }

  function markDirty(target) {
    var wrap = target.closest('.set-row');
    if (!wrap) return;
    var key = wrap.getAttribute('data-row');
    var dirty = target.value !== state.set.original[key];
    wrap.className = 'set-row' + (dirty ? ' dirty' : '');
  }

  function saveSettings() {
    var values = {};
    var count = 0;
    var inputs = $('setGroups').querySelectorAll('[data-key]');
    for (var i = 0; i < inputs.length; i++) {
      var key = inputs[i].getAttribute('data-key');
      if (inputs[i].value !== state.set.original[key]) {
        values[key] = inputs[i].value;
        count += 1;
      }
    }
    if (!count) { $('setNote').textContent = '没有改动。'; return; }
    $('setSave').disabled = true;
    api('/api/admin/settings', { method: 'POST', body: JSON.stringify({ values: values }) })
      .then(function (body) {
        var names = (body.changed || []).map(function (row) { return row.label; });
        var note = names.length
          ? ('已保存 ' + names.join('、') + (body.ingest_restarted ? '，外部拉取已按新参数重连。' : ''))
          : '这些值本来就是这个。';
        if (body.admin_password_changed) {
          token = values.ACN_ADMIN_PASSWORD;
          sessionStorage.setItem('acnAdmin', token);
          note += ' 管理口令已换成新的，旧口令失效。';
        }
        paintSettings(body);
        $('setNote').textContent = note;
        loadState();
      }).catch(function (error) {
        $('setNote').textContent = '没保存成：' + error.message;
      }).then(function () { $('setSave').disabled = false; });
  }

  function resetSettings() {
    if (!window.confirm('全部退回 env 里的值？管理口令不会被改。')) return;
    api('/api/admin/settings/reset', { method: 'POST', body: '{}' }).then(function (body) {
      var count = (body.changed || []).length;
      paintSettings(body);
      $('setNote').textContent = '已退回 env 默认（改了 ' + count + ' 项）。';
      loadState();
    }).catch(function (error) {
      $('setNote').textContent = '没退成：' + error.message;
    });
  }

  /* ── 审计 ── */
  function paintAuditKinds(kinds) {
    var select = $('auKind');
    var signature = (kinds || []).map(function (row) {
      return row.kind + ':' + row.count;
    }).join(',');
    if (select.getAttribute('data-sig') === signature) return;
    var html = '<option value="">所有类型</option>';
    (kinds || []).forEach(function (row) {
      html += '<option value="' + esc(row.kind) + '">' + esc(row.kind) +
        '（' + row.count + '）</option>';
    });
    select.innerHTML = html;
    select.setAttribute('data-sig', signature);
    select.value = state.au.kind || '';
  }

  function paintAudits(rows, body) {
    var html = '';
    rows.forEach(function (row) {
      html += '<tr><td class="mono" style="white-space:nowrap">' + esc(row.time) +
        '</td><td>' + esc(row.kind) + '</td><td class="note mono">' +
        esc(JSON.stringify(row.detail)) + '</td></tr>';
    });
    $('audits').innerHTML = html ||
      '<tr><td colspan="3" class="note">没有这类记录</td></tr>';
    var total = body && body.total != null ? body.total : rows.length;
    var filter = [state.au.kind, state.au.q].filter(Boolean).join(' / ');
    $('auNote').textContent = '共 ' + total + ' 条，这次显示 ' + rows.length + ' 条' +
      (filter ? '（' + filter + '）' : '') + '。';
    $('auMore').disabled = rows.length >= total;
  }

  function loadAudits(reset) {
    if (reset) {
      state.au.before = null;
      state.au.rows = [];
    }
    var query = '?limit=60';
    if (state.au.kind) query += '&kind=' + encodeURIComponent(state.au.kind);
    if (state.au.q) query += '&q=' + encodeURIComponent(state.au.q);
    if (!reset && state.au.before) query += '&before=' + state.au.before;
    api('/api/admin/audits' + query).then(function (body) {
      state.au.rows = reset ? body.rows : state.au.rows.concat(body.rows);
      state.au.before = body.next_before;
      state.au.touched = true;
      paintAuditKinds(body.kinds);
      paintAudits(state.au.rows, body);
    }).catch(function (error) {
      $('auNote').textContent = '审计没取到：' + error.message;
    });
  }

  /* ── 地图 ── */
  function project(lon, lat) {
    var x = (lon + 180) / 360;
    var s = Math.sin(Math.max(-85, Math.min(85, lat)) * Math.PI / 180);
    var y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return [x, y];
  }

  function unproject(x, y) {
    var lon = x * 360 - 180;
    var n = Math.PI * (1 - 2 * y);
    var lat = 180 / Math.PI * Math.atan((Math.exp(n) - Math.exp(-n)) / 2);
    return [lon, lat];
  }

  function mapInit() {
    if (state.map) return state.map;
    var canvas = $('map');
    var m = {
      canvas: canvas, ctx: canvas.getContext('2d'),
      coast: [], stations: [], track: null, picked: null, filter: '',
      view: { lon: 107, lat: 33, z: 0 }, size: { w: 0, h: 0, dpr: 1 }, drag: null,
      pointers: {}, pinch: null, pinched: false
    };
    state.map = m;

    function localPoint(ev) {
      var rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }

    function pinchBase() {
      var ids = Object.keys(m.pointers);
      if (ids.length < 2) return null;
      var a = m.pointers[ids[0]], b = m.pointers[ids[1]];
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      var anchor = unproject(
        project(m.view.lon, m.view.lat)[0] + (mx - m.size.w / 2) / m.view.z,
        project(m.view.lon, m.view.lat)[1] + (my - m.size.h / 2) / m.view.z);
      return { dist: Math.max(8, Math.hypot(a.x - b.x, a.y - b.y)),
        mx: mx, my: my, z: m.view.z, anchor: anchor };
    }

    function applyPinch() {
      var ids = Object.keys(m.pointers);
      if (ids.length < 2 || !m.pinch) return;
      var a = m.pointers[ids[0]], b = m.pointers[ids[1]];
      var dist = Math.max(8, Math.hypot(a.x - b.x, a.y - b.y));
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      var floor = Math.max(m.size.w, m.size.h) * 0.9;
      var z = Math.max(floor, Math.min(400000, m.pinch.z * (dist / m.pinch.dist)));
      /* 双指按下时中点压着的那块地，缩放后仍压在中点下，顺带跟着手指平移 */
      var p = project(m.pinch.anchor[0], m.pinch.anchor[1]);
      var c = unproject(p[0] - (mx - m.size.w / 2) / z,
                        p[1] - (my - m.size.h / 2) / z);
      m.view.z = z;
      m.view.lon = c[0];
      m.view.lat = c[1];
      mapClamp(m);
      m.pinched = true;
      mapDraw(m);
    }

    canvas.addEventListener('pointerdown', function (ev) {
      try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* 老浏览器不认 */ }
      var p = localPoint(ev);
      m.pointers[ev.pointerId] = p;
      var count = Object.keys(m.pointers).length;
      if (count === 1) {
        m.drag = { x: p.x, y: p.y, lon: m.view.lon, lat: m.view.lat, moved: 0 };
        m.pinch = null;
        m.pinched = false;
      } else if (count === 2) {
        m.drag = null;                     /* 双指不再当拖动，改成缩放 */
        m.pinch = pinchBase();
      }
      canvas.className = 'dragging';
    });

    canvas.addEventListener('pointermove', function (ev) {
      if (!m.pointers[ev.pointerId]) return;
      var p = localPoint(ev);
      m.pointers[ev.pointerId] = p;
      if (Object.keys(m.pointers).length >= 2) {
        if (!m.pinch) m.pinch = pinchBase();
        applyPinch();
        return;
      }
      if (!m.drag) return;
      var dx = p.x - m.drag.x, dy = p.y - m.drag.y;
      m.drag.moved = Math.max(m.drag.moved, Math.abs(dx) + Math.abs(dy));
      var c = project(m.drag.lon, m.drag.lat);
      var moved = unproject(c[0] - dx / m.view.z, c[1] - dy / m.view.z);
      m.view.lon = moved[0];
      m.view.lat = moved[1];
      mapClamp(m);
      mapDraw(m);
    });

    function release(ev, allowClick) {
      var p = m.pointers[ev.pointerId];
      delete m.pointers[ev.pointerId];
      var left = Object.keys(m.pointers).length;
      if (allowClick && left === 0 && m.drag && m.drag.moved < 5 && !m.pinched && p) {
        var hit = mapHit(m, p.x, p.y);
        if (hit) mapShow(m, hit); else mapHide(m);
      }
      if (left === 0) {
        m.drag = null;
        m.pinch = null;
        m.pinched = false;
        canvas.className = '';
      } else if (left === 1) {
        /* 抬起一根手指：剩下的那根接着当拖动，基准重算 */
        var only = m.pointers[Object.keys(m.pointers)[0]];
        m.drag = { x: only.x, y: only.y, lon: m.view.lon, lat: m.view.lat, moved: 99 };
        m.pinch = null;
      }
    }

    canvas.addEventListener('pointerup', function (ev) { release(ev, true); });
    canvas.addEventListener('pointercancel', function (ev) { release(ev, false); });

    canvas.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      mapZoom(m, ev.deltaY < 0 ? 1.25 : 0.8, ev.clientX, ev.clientY);
    }, { passive: false });

    window.addEventListener('resize', function () {
      if (state.tab !== 'map') return;
      mapResize(m);
    });

    /* 调试入口：探针读视角与选中的台站，页面上没有别的地方用它 */
    window.__acnMap = {
      state: function () {
        return { z: m.view.z, lon: m.view.lon, lat: m.view.lat,
          pointers: Object.keys(m.pointers).length,
          stations: m.stations.length, picked: m.picked ? m.picked.call : '' };
      }
    };

    return m;
  }

  function mapClamp(m) {
    var floor = Math.max(m.size.w, m.size.h) * 0.9;
    m.view.z = Math.max(floor, Math.min(400000, m.view.z));
    m.view.lat = Math.max(-72, Math.min(78, m.view.lat));
    m.view.lon = Math.max(-180, Math.min(180, m.view.lon));
  }

  function mapResize(m) {
    m.size.dpr = window.devicePixelRatio || 1;
    m.size.w = m.canvas.clientWidth;
    m.size.h = m.canvas.clientHeight;
    m.canvas.width = Math.round(m.size.w * m.size.dpr);
    m.canvas.height = Math.round(m.size.h * m.size.dpr);
    m.ctx.setTransform(m.size.dpr, 0, 0, m.size.dpr, 0, 0);
    if (!m.view.z) m.view.z = m.size.w * 6;
    mapClamp(m);
    mapDraw(m);
  }

  function mapScreenX(m, lon, off) {
    var p = project(lon, m.view.lat);
    var c = project(m.view.lon, m.view.lat);
    return (p[0] - c[0]) * m.view.z + m.size.w / 2 + off;
  }

  function mapScreenY(m, lat) {
    var p = project(0, lat);
    var c = project(0, m.view.lat);
    return (p[1] - c[1]) * m.view.z + m.size.h / 2;
  }

  function mapAge(st) {
    var t = Date.parse(st.last_seen || '');
    if (isNaN(t)) return 1e9;
    return (Date.now() - t) / 1000;
  }

  function mapAlpha(age) {
    if (age <= 1800) return 1;
    if (age <= 10800) return 0.6;
    return 0.3;
  }

  function mapDraw(m) {
    var ctx = m.ctx, w = m.size.w, h = m.size.h, o, i, r;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#f1f1ed';
    ctx.fillRect(0, 0, w, h);
    var offs = [-m.view.z, 0, m.view.z];

    ctx.fillStyle = '#e2ded2';
    ctx.strokeStyle = '#b7b1a2';
    ctx.lineWidth = 0.7;
    for (o = 0; o < offs.length; o++) {
      for (r = 0; r < m.coast.length; r++) {
        var ring = m.coast[r];
        ctx.beginPath();
        for (i = 0; i < ring.length; i += 2) {
          var px = mapScreenX(m, ring[i], offs[o]);
          var py = mapScreenY(m, ring[i + 1]);
          if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    }

    ctx.strokeStyle = 'rgba(31,36,41,0.09)';
    ctx.lineWidth = 0.6;
    var step = m.view.z > 20000 ? 1 : (m.view.z > 7000 ? 5 : 10);
    for (o = 0; o < offs.length; o++) {
      for (var lon = -180; lon <= 180; lon += step) {
        var lx = mapScreenX(m, lon, offs[o]);
        if (lx < -1 || lx > w + 1) continue;
        ctx.beginPath();
        ctx.moveTo(lx, mapScreenY(m, -85));
        ctx.lineTo(lx, mapScreenY(m, 85));
        ctx.stroke();
      }
    }
    for (var lat = -80; lat <= 80; lat += step) {
      var ly = mapScreenY(m, lat);
      if (ly < -1 || ly > h + 1) continue;
      ctx.beginPath();
      ctx.moveTo(0, ly);
      ctx.lineTo(w, ly);
      ctx.stroke();
    }

    if (m.track && m.track.track && m.track.track.length > 1) {
      ctx.strokeStyle = '#b3141f';
      ctx.lineWidth = 1.2;
      for (o = 0; o < offs.length; o++) {
        ctx.beginPath();
        for (i = 0; i < m.track.track.length; i++) {
          var tp = m.track.track[i];
          var tx = mapScreenX(m, tp.lon, offs[o]);
          var ty = mapScreenY(m, tp.lat);
          if (i === 0) ctx.moveTo(tx, ty); else ctx.lineTo(tx, ty);
        }
        ctx.stroke();
      }
    }

    for (o = 0; o < offs.length; o++) {
      for (var n = 0; n < m.stations.length; n++) {
        var st = m.stations[n];
        if (m.filter && st.call.indexOf(m.filter) !== 0) continue;
        var dx = mapScreenX(m, st.lon, offs[o]);
        var dy = mapScreenY(m, st.lat);
        if (dx < -20 || dy < -20 || dx > w + 20 || dy > h + 20) continue;
        var age = mapAge(st);
        ctx.globalAlpha = mapAlpha(age);
        ctx.fillStyle = st.via === 'rf' ? '#b3141f' : '#15181c';
        ctx.beginPath();
        ctx.arc(dx, dy, age <= 1800 ? 3.2 : 2.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;

    if (m.picked) {
      var mx = mapScreenX(m, m.picked.lon, 0);
      var my = mapScreenY(m, m.picked.lat);
      ctx.strokeStyle = '#b3141f';
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(mx, my, 7, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = '#b3141f';
      ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      ctx.fillText(m.picked.call, mx + 10, my - 8);
    }
  }

  function mapHit(m, x, y) {
    var best = null, bestD = 12;
    for (var i = 0; i < m.stations.length; i++) {
      var st = m.stations[i];
      if (m.filter && st.call.indexOf(m.filter) !== 0) continue;
      var d = Math.hypot(mapScreenX(m, st.lon, 0) - x, mapScreenY(m, st.lat) - y);
      if (d < bestD) { bestD = d; best = st; }
    }
    return best;
  }

  function mapZoom(m, factor, clientX, clientY) {
    var rect = m.canvas.getBoundingClientRect();
    var mx = (clientX == null ? rect.width / 2 : clientX - rect.left);
    var my = (clientY == null ? rect.height / 2 : clientY - rect.top);
    var before = unproject(
      project(m.view.lon, m.view.lat)[0] + (mx - m.size.w / 2) / m.view.z,
      project(m.view.lon, m.view.lat)[1] + (my - m.size.h / 2) / m.view.z);
    m.view.z *= factor;
    mapClamp(m);
    var after = project(before[0], before[1]);
    var c = unproject(after[0] - (mx - m.size.w / 2) / m.view.z,
                      after[1] - (my - m.size.h / 2) / m.view.z);
    m.view.lon = c[0];
    m.view.lat = c[1];
    mapClamp(m);
    mapDraw(m);
  }

  function mapGrid(lat, lon) {
    var A = 'ABCDEFGHIJKLMNOPQRSTUVWX';
    var lo = lon + 180, la = lat + 90;
    return A.charAt(Math.floor(lo / 20)) + A.charAt(Math.floor(la / 10)) +
      Math.floor((lo % 20) / 2) + Math.floor(la % 10);
  }

  function mapShow(m, st) {
    m.picked = st;
    m.track = null;
    $('mapCall').textContent = st.call;
    var rows = [
      ['最后听到', stamp(st.last_seen)],
      ['网格', mapGrid(st.lat, st.lon)],
      ['位置', st.lat.toFixed(4) + ', ' + st.lon.toFixed(4)],
      ['来源', st.via === 'rf' ? '射频' : '网络'],
      ['报文', st.packets + ' 条']
    ];
    if (st.alt != null) rows.push(['高度', Math.round(st.alt) + ' m']);
    if (st.speed) rows.push(['速度', Math.round(st.speed) + ' km/h']);
    if (st.course) rows.push(['方向', Math.round(st.course) + '°']);
    var html = '';
    rows.forEach(function (row) {
      html += '<dt>' + row[0] + '</dt><dd>' + esc(String(row[1])) + '</dd>';
    });
    $('mapFields').innerHTML = html;
    $('mapNote').textContent = st.comment || '';
    $('mapPanel').className = 'map-panel on';
    mapDraw(m);
    api('/api/admin/stations/' + encodeURIComponent(st.call)).then(function (body) {
      if (!m.picked || m.picked.call !== st.call) return;
      m.track = body;
      $('mapNote').textContent = (body.comment || '') + (body.comment ? ' · ' : '') +
        (body.track || []).length + ' 个位置点' +
        (body.first_seen ? '，首次 ' + stamp(body.first_seen) : '');
      mapDraw(m);
    }).catch(function () { /* 轨迹拿不到就不画 */ });
  }

  function mapHide(m) {
    m.picked = null;
    m.track = null;
    $('mapPanel').className = 'map-panel';
    mapDraw(m);
  }

  function mapLoad(m) {
    $('mapState').textContent = '正在读台站…';
    api('/api/admin/stations?window=24h&limit=5000').then(function (body) {
      m.stations = body.stations || [];
      $('mapState').textContent = m.stations.length ? '' : '24 小时里还没有台站';
      var rf = 0;
      m.stations.forEach(function (st) { if (st.via === 'rf') rf += 1; });
      $('mapCount').textContent = m.stations.length + ' 台 · 射频 ' + rf +
        ' · ' + stamp(body.updated) + ' 更新';
      mapDraw(m);
    }).catch(function (error) {
      $('mapState').textContent = '读不到台站数据（' + error.message + '）';
    });
  }

  /* ── 载入器 ── */
  function refreshWithClients() {
    return loadState().then(function (body) {
      if (body) paintClients(((body.stats || {}).clients) || []);
    });
  }

  LOADERS.overview = function () {
    refreshWithClients();
    timers.overview = window.setInterval(function () {
      if (state.tab === 'overview' || state.tab === 'clients') refreshWithClients();
    }, 20000);
  };
  LOADERS.clients = LOADERS.overview;
  LOADERS.names = function () { loadState(); };
  LOADERS.packets = function () { search(); };
  LOADERS.stations = function () { loadStations(true); loadDevices(); };
  LOADERS.settings = function () { loadSettings(); };
  LOADERS.audits = function () {
    if (state.au.touched) loadAudits(false); else loadAudits(true);
  };
  LOADERS.map = function () {
    var m = mapInit();
    mapResize(m);
    if (!m.coast.length) {
      fetch('/assets/coast.json').then(function (r) { return r.ok ? r.json() : {}; })
        .then(function (data) { m.coast = data.rings || []; mapDraw(m); })
        .catch(function () { /* 没有底图也能画点 */ });
    }
    mapLoad(m);
    timers.map = window.setInterval(function () {
      if (state.tab === 'map') mapLoad(state.map);
    }, 60000);
  };

  /* ── 事件 ── */
  $('loginBtn').addEventListener('click', enter);
  $('pw').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') enter();
  });
  $('logoutBtn').addEventListener('click', function () { logout(''); });
  $('refreshBtn').addEventListener('click', function () {
    var load = LOADERS[state.tab];
    if (load) load();
  });

  var tabButtons = $('tabs').getElementsByTagName('button');
  for (var t = 0; t < tabButtons.length; t++) {
    tabButtons[t].addEventListener('click', function (ev) {
      showTab(ev.currentTarget.getAttribute('data-tab'));
    });
  }
  $('toAudits').addEventListener('click', function () { showTab('audits'); });
  $('toSettings').addEventListener('click', function () { showTab('settings'); });

  $('clients').addEventListener('click', function (ev) {
    var target = ev.target.getAttribute && ev.target.getAttribute('data-kick');
    if (!target) return;
    if (!window.confirm('踢掉 ' + target + ' 的连接？')) return;
    api('/api/admin/kick', { method: 'POST', body: JSON.stringify({ call: target }) })
      .then(function (body) {
        $('clientsNote').textContent = '踢掉 ' + body.kicked + ' 条：' + body.call;
        refreshWithClients();
      }).catch(function (error) {
        $('clientsNote').textContent = '没踢掉：' + error.message;
      });
  });

  $('qBtn').addEventListener('click', search);
  $('qText').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') search();
  });
  $('qAuto').addEventListener('change', startAuto);
  $('qScope').addEventListener('change', function () {
    if ($('qScope').value === 'archive' && $('qAuto').checked) {
      $('qNote').textContent = '存档是库里的旧行，不跟着刷；要看新的把范围切回内存环。';
    }
  });

  $('stBtn').addEventListener('click', function () { loadStations(true); });
  $('stLimit').addEventListener('change', function () { loadStations(true); });
  $('stPrev').addEventListener('click', function () {
    var limit = parseInt($('stLimit').value, 10) || 300;
    state.st.offset = Math.max(0, state.st.offset - limit);
    loadStations(false);
  });
  $('stNext').addEventListener('click', function () {
    var limit = parseInt($('stLimit').value, 10) || 300;
    state.st.offset += limit;
    loadStations(false);
  });
  $('stCall').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') loadStations(true);
  });
  $('stations').addEventListener('click', function (ev) {
    var target = ev.target.getAttribute && ev.target.getAttribute('data-track');
    if (target) loadTrack(target);
  });

  $('accounts').addEventListener('click', function (ev) {
    var edit = ev.target.getAttribute && ev.target.getAttribute('data-edit');
    if (edit) { fillForm(edit); return; }
    var call_ = ev.target.getAttribute && ev.target.getAttribute('data-del');
    if (!call_) return;
    if (!window.confirm('删除 ' + call_ + ' ？')) return;
    api('/api/admin/account/delete', { method: 'POST', body: JSON.stringify({ call: call_ }) })
      .then(function () { loadState(); });
  });

  $('fPwEye').addEventListener('click', function () {
    var box = $('fPassword');
    var show = box.type === 'password';
    box.type = show ? 'text' : 'password';
    $('fPwEye').textContent = show ? '隐藏' : '显示';
  });

  $('saveBtn').addEventListener('click', function () {    var payload = {
      call: $('fCall').value.trim(),
      account: $('fAccount').value.trim(),
      status: $('fStatus').value,
      valid_from: $('fFrom').value.trim() || null,
      valid_to: $('fTo').value.trim() || null,
      note: $('fNote').value.trim() || null
    };
    if (!$('fPassword').value) {
      /* 留空不改口令，发 null 让后端保留原值 */
      payload.password = null;
    } else {
      payload.password = $('fPassword').value;
    }
    api('/api/admin/account', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (body) {
        $('saveNote').textContent = '已保存，名单版本 ' + body.version;
        $('fPassword').value = '';
        loadState();
      }).catch(function (error) {
        $('saveNote').textContent = '保存失败：' + error.message;
      });
  });

  $('publishBtn').addEventListener('click', function () {
    api('/api/admin/publish', { method: 'POST', body: '{}' }).then(function (body) {
      $('saveNote').textContent = '已发布，名单版本 ' + body.version;
      $('wlNote').textContent = '已发布，名单版本 ' + body.version;
      loadState();
    }).catch(function (error) {
      $('wlNote').textContent = '没发布成：' + error.message;
    });
  });

  $('wlRefresh').addEventListener('click', function () {
    $('wlNote').textContent = '正在重读…';
    loadState();
  });

  $('exportBtn').addEventListener('click', function () {
    api('/api/admin/whitelist').then(function (body) {
      var text = JSON.stringify(body, null, 2);
      var url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      var link = document.createElement('a');
      link.href = url;
      link.download = 'aprsnetcn-whitelist-v' + body.version + '.json';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      $('exportNote').textContent = '导出了 ' + (body.records || []).length +
        ' 条，名单版本 ' + body.version + '。';
    }).catch(function (error) {
      $('exportNote').textContent = '导出失败：' + error.message;
    });
  });

  $('setSave').addEventListener('click', saveSettings);
  $('setReset').addEventListener('click', resetSettings);
  $('setReload').addEventListener('click', loadSettings);
  $('setGroups').addEventListener('input', function (ev) {
    if (ev.target.getAttribute && ev.target.getAttribute('data-key')) markDirty(ev.target);
  });
  $('setGroups').addEventListener('change', function (ev) {
    if (ev.target.getAttribute && ev.target.getAttribute('data-key')) markDirty(ev.target);
  });

  $('auBtn').addEventListener('click', function () {
    state.au.kind = $('auKind').value;
    state.au.q = $('auText').value.trim();
    loadAudits(true);
  });
  $('auText').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') $('auBtn').click();
  });
  $('auKind').addEventListener('change', function () { $('auBtn').click(); });
  $('auMore').addEventListener('click', function () { loadAudits(false); });

  $('mapReload').addEventListener('click', function () { mapLoad(mapInit()); });
  $('mapQ').addEventListener('input', function () {
    var m = mapInit();
    m.filter = $('mapQ').value.trim().toUpperCase();
    mapDraw(m);
  });
  $('mapZin').addEventListener('click', function () { mapZoom(mapInit(), 1.4); });
  $('mapZout').addEventListener('click', function () { mapZoom(mapInit(), 0.7); });
  $('mapClose').addEventListener('click', function () { mapHide(mapInit()); });

  /* ── 开张 ── */
  if (token) {
    $('login').hidden = true;
    $('tabs').hidden = false;
    $('app').hidden = false;
    $('refreshBtn').hidden = false;
    $('logoutBtn').hidden = false;
    showTab(location.hash ? location.hash.slice(1) : 'overview', true);
  } else {
    $('pw').focus();
  }

})();
