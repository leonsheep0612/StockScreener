import SETTINGS_HTML from './settings.html';
import STRATEGIES from '../../strategies.json';

const CONFIG_PATH = 'config.json';
const RESULTS_PATH = 'data/results.json';
const FIRST_CRON = '0 9 * * MON-FRI'; // wrangler.toml 的第一個 cron（17:00）；另一個是 17:30 補跑
const MAX_PRESETS = 12; // Flex carousel 最多 12 張卡
const WEEKDAYS = ['一', '二', '三', '四', '五'];
const STRATEGY_INFO = Object.fromEntries(STRATEGIES.strategies.map((s) => [s.key, s]));
const CUSTOM_INFO = { icon: '🧩', name: '自訂組合', desc: '自由搭配各種條件' };

export default {
  // wrangler.toml 的 cron：每個交易日 17:00 觸發篩選，依各組合的推播日推播；17:30 補跑（已成功就略過）
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerWorkflow(env, '', event.cron === FIRST_CRON ? 'first' : 'retry'));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === 'GET' && url.pathname === '/') {
        const html = SETTINGS_HTML.replace('__LIFF_ID__', env.LIFF_ID || '')
          .replace('__STRATEGIES__', JSON.stringify(STRATEGIES).replace(/</g, '\\u003c'));
        return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }
      if (request.method === 'POST' && url.pathname === '/webhook') {
        return await handleWebhook(request, env);
      }
      if (url.pathname.startsWith('/api/')) {
        await authorize(request, env);
        if (request.method === 'GET' && url.pathname === '/api/config') {
          const [{ config, sha }, quota] = await Promise.all([readJson(env, CONFIG_PATH), getQuota(env)]);
          return json({ config, sha, quota });
        }
        if (request.method === 'PUT' && url.pathname === '/api/config') {
          const body = await request.json();
          const { config: old } = await readJson(env, CONFIG_PATH);
          const config = validateConfig(body.config, old);
          const sha = await writeJson(env, CONFIG_PATH, config, body.sha);
          // 結果已是最新就不重跑，請使用者到選單「查看結果」（免費）
          const fresh = body.run && freshResult(config.presets.find((p) => p.id === body.run), await loadResults(env));
          if (body.run && !fresh) await triggerWorkflow(env, body.run);
          return json({ sha, config, fresh: fresh ? fresh.date : null });
        }
        if (request.method === 'POST' && url.pathname === '/api/run') {
          const { preset } = await request.json();
          const { presets, results } = await loadAll(env);
          const fresh = freshResult(presets.find((p) => p.id === preset), results);
          if (!fresh) await triggerWorkflow(env, preset);
          return json({ ok: true, fresh: fresh ? fresh.date : null });
        }
      }
      return new Response('Not found', { status: 404 });
    } catch (e) {
      return json({ error: e.message, code: e.code }, e.status || 500);
    }
  },
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status });
}

// ---------- 身分驗證：LIFF 的 ID token 必須屬於擁有者 ----------

async function authorize(request, env) {
  const token = (request.headers.get('Authorization') || '').replace(/^Bearer /, '');
  if (!token || token === 'null') throw httpError(401, '取不到登入資訊：請在 LINE Developers 的 LIFF 設定勾選 openid');
  const res = await fetch('https://api.line.me/oauth2/v2.1/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    // LIFF ID 的「-」前面就是 LINE Login channel 的 Channel ID，直接從這裡取最不會填錯
    body: new URLSearchParams({ id_token: token, client_id: (env.LIFF_ID || '').split('-')[0] || env.LIFF_CHANNEL_ID }),
  });
  if (!res.ok) {
    const reason = (await res.json().catch(() => ({}))).error_description || `HTTP ${res.status}`;
    console.log('verify failed', reason);
    if (/expired/i.test(reason)) throw Object.assign(httpError(401, '登入已過期，重新登入中…'), { code: 'expired' });
    if (/audience/i.test(reason)) throw httpError(401, 'LIFF_CHANNEL_ID 設定錯誤：要填 LIFF 所在 LINE Login channel 的 Channel ID');
    throw httpError(401, `登入驗證失敗：${reason}`);
  }
  const { sub } = await res.json();
  if (sub !== env.LINE_USER_ID) {
    console.log('user mismatch', sub);
    throw httpError(403, '此設定頁僅限擁有者使用（若你就是擁有者：LINE Login channel 必須和官方帳號在同一個 Provider，且 LINE_USER_ID 要正確）');
  }
}

// ---------- GitHub：讀寫 config.json、讀最新結果、觸發排程 ----------

async function github(env, path, init = {}) {
  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'stock-screener-line',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  if (res.status === 404) throw httpError(404, '找不到檔案');
  if (res.status === 409 || res.status === 422) {
    throw httpError(409, '設定已在別處被修改，請重新整理後再存一次');
  }
  if (!res.ok) throw httpError(502, `GitHub API 錯誤 ${res.status}：${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

async function readJson(env, path) {
  const file = await github(env, `/contents/${path}?ref=${env.GITHUB_BRANCH}`);
  // 超過 1 MB 的檔案 contents API 不給內容，改抓 raw
  let text;
  if (file.content) {
    const bytes = Uint8Array.from(atob(file.content.replace(/\n/g, '')), (c) => c.charCodeAt(0));
    text = new TextDecoder().decode(bytes);
  } else {
    const raw = await fetch(file.download_url, { headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}` } });
    text = await raw.text();
  }
  return { config: JSON.parse(text), sha: file.sha };
}

async function writeJson(env, path, data, sha) {
  const bytes = new TextEncoder().encode(JSON.stringify(data, null, 2) + '\n');
  let binary = '';
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  const res = await github(env, `/contents/${path}`, {
    method: 'PUT',
    body: JSON.stringify({ message: 'Update screener config from LINE', content: btoa(binary), sha, branch: env.GITHUB_BRANCH }),
  });
  return res.content.sha;
}

// auto：排程觸發才有，first = 17:00、retry = 17:30 補跑；從 LINE 手動觸發時是空字串
async function triggerWorkflow(env, presetId, auto = '') {
  await github(env, `/actions/workflows/${env.WORKFLOW_FILE}/dispatches`, {
    method: 'POST',
    body: JSON.stringify({ ref: env.GITHUB_BRANCH, inputs: { preset: presetId || '', auto } }),
  });
}

// ---------- LINE 推播額度 ----------

async function lineGet(env, path) {
  const res = await fetch(`https://api.line.me/v2/bot${path}`, {
    headers: { Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}` },
  });
  return res.ok ? res.json() : null;
}

async function getQuota(env) {
  const [quota, used] = await Promise.all([lineGet(env, '/message/quota'), lineGet(env, '/message/quota/consumption')]);
  if (!quota || !used) return null;
  return { limit: quota.type === 'limited' ? quota.value : null, used: used.totalUsage };
}

// ---------- 設定檢查：只保留認得的欄位，數值要在合理範圍 ----------
// 每個欄位：'bool'、'range'（[名稱, 最小, 最大]）或 [名稱, 最小, 最大, 是否整數]

const SPEC = {
  filters: {
    require_above_ma20: 'bool',
    min_avg_volume_lots: ['月均量下限', 0, 1e6, true],
    volume_surge: ['爆量倍數', 1, 100],
  },
  strong: {
    enabled: 'bool', plus_di_min: ['+DI 下限', 0, 100], minus_di_max: ['−DI 上限', 0, 100], di_adx_order: 'bool',
    pctb_min: ['%b 下限', -5, 5], bandwidth_max: ['布林帶寬上限', 0, 5], min_volume_lots: ['成交量下限', 0, 1e7, true],
    kd_daily: 'bool', kd_weekly: 'bool', kd_monthly: 'bool', ma5_above_ma20: 'bool', big_holder_no_decrease: 'bool',
  },
  growth: {
    enabled: 'bool', eps_q_yoy: ['單季 EPS 年增', -100, 10000], eps_q_count: ['EPS 連續季數', 1, 3, true],
    eps_y_yoy: ['年度 EPS 年增', -100, 10000], eps_y_count: ['年度 EPS 連續年數', 1, 2, true],
    rev_q_yoy: ['單季營收年增', -100, 10000], rev_q_count: ['營收連續季數', 1, 3, true],
    roe_min: ['ROE 下限', -100, 1000], cum_eps_auto: 'bool', cum_eps_min: ['累季 EPS 下限', -100, 1000],
  },
  value: {
    enabled: 'bool', cash_div_min: ['現金股利下限', 0, 1000], yield_min: ['殖利率下限', 0, 100],
    per_max: ['本益比上限', 0, 1000], pbr_max: ['股價淨值比上限', 0, 100], above_ma60: 'bool', ma60_rising: 'bool',
  },
  big_holder: {
    enabled: 'bool', min_change: ['大戶週增幅下限', 0, 1000], surge: ['單週大增門檻', 0, 1000],
    ratio_range: ['range', '大戶比例', 0, 100], sweet_spot: ['range', '甜蜜區', 0, 100],
    stock_change_tolerance: ['庫存變動容忍', 0, 100], max_capital_yi: ['股本上限', 1, 100000],
  },
  consecutive: { enabled: 'bool', days: ['連續買超天數', 2, 30, true] },
  cum_sync: { enabled: 'bool', days: ['累計天數', 5, 120, true] },
  monthly_ratio: { enabled: 'bool', foreign_pct: ['外資當月佔比', -100, 100], trust_pct: ['投信當月佔比', -100, 100] },
  daily: { enabled: 'bool', min_trust_lots: ['投信買超下限', 0, 1e6, true] },
};
const SECTIONS = Object.keys(SPEC).filter((k) => k !== 'filters');

function num(value, name, min, max, integer = false) {
  const n = Number(value);
  if (value === '' || value === null || !Number.isFinite(n)) throw httpError(400, `「${name}」請輸入數字`);
  if (integer && !Number.isInteger(n)) throw httpError(400, `「${name}」請輸入整數`);
  if (n < min || n > max) throw httpError(400, `「${name}」需介於 ${min}～${max}`);
  return n;
}

function range(value, name, min, max) {
  if (!Array.isArray(value) || value.length !== 2) throw httpError(400, `「${name}」格式錯誤`);
  const low = num(value[0], `${name}下限`, min, max);
  const high = num(value[1], `${name}上限`, min, max);
  if (low >= high) throw httpError(400, `「${name}」下限需小於上限`);
  return [low, high];
}

function validateSection(input, spec, at) {
  const out = {};
  for (const [key, rule] of Object.entries(spec)) {
    const value = input?.[key];
    if (rule === 'bool') out[key] = Boolean(value);
    else if (rule[0] === 'range') out[key] = range(value, at + rule[1], rule[2], rule[3]);
    else out[key] = num(value, at + rule[0], rule[1], rule[2], rule[3]);
  }
  return out;
}

function validatePreset(c) {
  const name = String(c.name || '').trim();
  if (!name || name.length > 20) throw httpError(400, '組合名稱需為 1～20 個字');
  const at = `【${name}】`;
  const id = String(c.id || '');
  if (!/^[a-z0-9-]{1,32}$/.test(id)) throw httpError(400, `${at}id 格式錯誤`);
  const preset = {
    id,
    name,
    strategy: STRATEGY_INFO[c.strategy] ? c.strategy : 'custom',
    notify_days: [...new Set((c.notify_days || []).map(Number))].filter((d) => d >= 0 && d <= 4).sort(),
    combine: c.combine === 'intersect' ? 'intersect' : 'separate',
    top_n: num(c.top_n, `${at}每區顯示幾檔`, 1, 50, true),
  };
  for (const [key, spec] of Object.entries(SPEC)) preset[key] = validateSection(c[key], spec, at);
  preset.daily.foreign_combos = (c.daily?.foreign_combos || []).map((x, i) => ({
    pct: num(x.pct, `${at}外資組合 ${i + 1} 佔比`, 0, 100),
    lots: num(x.lots, `${at}外資組合 ${i + 1} 張數`, 0, 1e6, true),
  }));
  if (preset.daily.enabled && preset.daily.foreign_combos.length === 0) {
    throw httpError(400, `${at}單日大買已開啟，至少需要一組外資條件`);
  }
  if (!SECTIONS.some((k) => preset[k].enabled)) throw httpError(400, `${at}至少要加入一個條件區塊`);
  return preset;
}

function validateConfig(c, old) {
  const presets = (c?.presets || []).map(validatePreset);
  if (presets.length === 0) throw httpError(400, '至少要保留一組條件組合');
  if (presets.length > MAX_PRESETS) throw httpError(400, `條件組合最多 ${MAX_PRESETS} 組`);
  if (new Set(presets.map((p) => p.id)).size !== presets.length) throw httpError(400, '條件組合 id 重複');
  if (new Set(presets.map((p) => p.name)).size !== presets.length) throw httpError(400, '條件組合名稱不可重複');

  // 條件有變動的組合記下修改時間，查看結果時可提醒「結果是修改前的」
  const before = Object.fromEntries((old?.presets || []).map((p) => [p.id, p]));
  const now = new Date().toISOString();
  for (const p of presets) {
    const prev = before[p.id];
    const { updated_at: prevTime, ...prevBody } = prev || {};
    p.updated_at = prev && JSON.stringify(prevBody) === JSON.stringify(p) ? prevTime : now;
  }
  return { presets };
}

function notifyText(days) {
  if (!days.length) return '不推播';
  if (days.length === 5) return '每個交易日推播';
  return '每週' + days.map((d) => WEEKDAYS[d]).join('、') + '推播';
}

// ---------- LINE webhook：圖文選單與文字指令（全部用回覆訊息，不計推播額度） ----------

async function verifySignature(body, signature, secret) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(body));
  return btoa(String.fromCharCode(...new Uint8Array(mac))) === signature;
}

async function reply(env, replyToken, messages) {
  const res = await fetch('https://api.line.me/v2/bot/message/reply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}` },
    body: JSON.stringify({ replyToken, messages }),
  });
  if (!res.ok) console.log('reply failed', res.status, await res.text());
}

async function handleWebhook(request, env) {
  const body = await request.text();
  if (!(await verifySignature(body, request.headers.get('X-Line-Signature') || '', env.LINE_CHANNEL_SECRET))) {
    console.log('bad signature：LINE_CHANNEL_SECRET 與 Messaging API channel 的 Channel secret 不符');
    return new Response('Bad signature', { status: 401 });
  }
  const events = JSON.parse(body).events || [];
  console.log('webhook events', events.map((e) => `${e.type}:${e.postback?.data || e.message?.text || ''}`).join(', '));
  for (const event of events) {
    if (!event.replyToken) continue;
    if (event.source?.userId !== env.LINE_USER_ID) {
      console.log('ignored user', event.source?.userId);
      continue;
    }
    let params;
    if (event.type === 'postback') {
      params = new URLSearchParams(event.postback.data);
    } else if (event.type === 'message' && event.message.type === 'text') {
      params = new URLSearchParams({ action: event.message.text.trim() });
    } else {
      continue;
    }
    try {
      const messages = await runCommand(env, params.get('action'), params);
      if (messages) await reply(env, event.replyToken, messages);
    } catch (e) {
      await reply(env, event.replyToken, [{ type: 'text', text: `⚠️ ${e.message}` }]);
    }
  }
  return new Response('OK');
}

// 卡片標題：淺黃底黑字、固定高度，名稱一行、說明最多兩行，每張卡標題對齊
function cardHeader(title, desc) {
  return {
    type: 'box', layout: 'vertical', backgroundColor: '#FFF4CC', paddingAll: '14px', height: '92px', contents: [
      { type: 'text', text: title, color: '#1d2433', weight: 'bold', size: 'lg' },
      { type: 'text', text: desc, color: '#6b6250', size: 'xs', wrap: true, maxLines: 2, margin: 'sm' },
    ],
  };
}

const liffUrl = (env, presetId) => `https://liff.line.me/${env.LIFF_ID}${presetId ? `?preset=${presetId}` : ''}`;

// 每個條件組合一張卡：策略圖示、名稱、最新結果摘要、三個按鈕
function presetBubble(env, p, result, resultsDate) {
  const info = STRATEGY_INFO[p.strategy] || CUSTOM_INFO;
  const stale = result && p.updated_at && resultsDate && p.updated_at > resultsDate;
  const lines = !result
    ? [{ type: 'text', text: '還沒有結果，17:00 排程後就會有', size: 'sm', color: '#8c8c8c', wrap: true }]
    : (result.summary || []).slice(0, 6).map((s) => ({
        type: 'box', layout: 'horizontal', contents: [
          { type: 'text', text: s.kind, size: 'sm', color: '#555555', flex: 5, wrap: true },
          { type: 'text', text: s.count === null ? '略過' : `${s.count} 檔`, size: 'sm', align: 'end', flex: 2,
            weight: 'bold', color: s.count ? '#06c755' : '#aaaaaa' },
        ],
      }));
  return {
    type: 'bubble', size: 'kilo',
    header: cardHeader(`${info.icon} ${p.name}`, info.desc),
    body: {
      type: 'box', layout: 'vertical', spacing: 'sm', contents: [
        ...(result ? [{ type: 'text', text: `資料日期 ${result.date}${stale ? '（條件已修改）' : ''}`, size: 'xs', color: stale ? '#e5484d' : '#8c8c8c' }] : []),
        ...lines,
        { type: 'separator', margin: 'md' },
        { type: 'text', text: p.notify_days.length ? `🔔 ${notifyText(p.notify_days)}` : '🔕 不推播，點下方取最新結果', size: 'xs', color: '#8c8c8c', margin: 'md' },
      ],
    },
    footer: {
      type: 'box', layout: 'vertical', spacing: 'sm', contents: [
        { type: 'button', style: 'primary', color: '#06c755', height: 'sm',
          action: { type: 'postback', label: '📊 看最新結果', data: `action=result&id=${p.id}`, displayText: `📊 ${p.name}` } },
        { type: 'box', layout: 'horizontal', spacing: 'sm', contents: [
          { type: 'button', style: 'secondary', height: 'sm', action: { type: 'uri', label: '⚙️ 改條件', uri: liffUrl(env, p.id) } },
          { type: 'button', style: 'secondary', height: 'sm',
            action: { type: 'postback', label: '▶️ 立即篩選', data: `action=run&id=${p.id}`, displayText: `立即篩選 ${p.name}` } },
        ] },
      ],
    },
  };
}

function presetQuickReply(presets) {
  return {
    items: presets.slice(0, 13).map((p) => ({
      type: 'action',
      action: { type: 'postback', label: p.name.slice(0, 20), data: `action=result&id=${p.id}`, displayText: `📊 ${p.name}` },
    })),
  };
}

const loadResults = (env) => readJson(env, RESULTS_PATH).then((r) => r.config).catch(() => null);

async function loadAll(env) {
  const [{ config }, results] = await Promise.all([readJson(env, CONFIG_PATH), loadResults(env)]);
  return { presets: config.presets, results };
}

// 最近一個已收盤的交易日（台灣時間，YYYY-MM-DD）：平日 16:00 後算當天，否則往前找到平日
// 國定假日不在這裡判斷，遇到時會多跑一次篩選，結果一樣
function lastCloseDate(now = new Date()) {
  const tw = new Date(now.getTime() + 8 * 3600e3);
  let d = new Date(Date.UTC(tw.getUTCFullYear(), tw.getUTCMonth(), tw.getUTCDate(), 8)); // 台灣 16:00
  while (d > now || [0, 6].includes(d.getUTCDay())) d = new Date(d.getTime() - 86400e3);
  return d.toISOString().slice(0, 10);
}

// 這組的結果已經是最新資料、條件也沒改過：回傳結果（含資料日期），否則 null
function freshResult(preset, results, now = new Date()) {
  const result = preset && results?.results?.[preset.id];
  if (!result || results.date < lastCloseDate(now)) return null;
  if (preset.updated_at && preset.updated_at > new Date(results.generated_at).toISOString()) return null;
  return { ...result, date: results.date };
}

async function runCommand(env, action, params) {
  if (['results', '查看結果', '結果', '策略'].includes(action)) {
    const { presets, results } = await loadAll(env);
    const resultsDate = results ? new Date(results.generated_at).toISOString() : null;
    const bubbles = presets.slice(0, MAX_PRESETS).map((p) =>
      presetBubble(env, p, results?.results?.[p.id] && { ...results.results[p.id], date: results.date }, resultsDate));
    bubbles.push({
      type: 'bubble', size: 'kilo',
      header: cardHeader('＋ 新增條件組合', '從四種策略範本開始，或自由搭配各種條件'),
      body: { type: 'box', layout: 'vertical', justifyContent: 'center', contents: [
        { type: 'text', text: '＋', size: '4xl', align: 'center', color: '#06c755' },
      ] },
      footer: { type: 'box', layout: 'vertical', contents: [
        { type: 'button', style: 'primary', color: '#06c755', height: 'sm', action: { type: 'uri', label: '開啟設定', uri: liffUrl(env, 'new') } },
      ] },
    });
    return [{ type: 'flex', altText: '選擇要查看的策略', contents: { type: 'carousel', contents: bubbles.slice(0, 12) } }];
  }

  if (action === 'result') {
    const { presets, results } = await loadAll(env);
    const preset = presets.find((p) => p.id === params.get('id'));
    const result = results?.results?.[params.get('id')];
    let text;
    if (!preset) {
      text = '這組條件已被刪除。';
    } else if (!result) {
      text = `「${preset.name}」還沒有結果，下次 17:00 排程後就會有；急的話可按卡片上的「立即篩選」（會用掉 1 則推播）。`;
    } else {
      text = result.text;
      if (preset.updated_at && preset.updated_at > new Date(results.generated_at).toISOString()) {
        text = '⚠️ 這組條件在上次篩選後有修改，以下仍是修改前的結果。\n\n' + text;
      }
    }
    if (text.length > 4900) text = text.slice(0, 4900) + '\n…（訊息過長已截斷）';
    return [{ type: 'text', text, quickReply: presetQuickReply(presets) }];
  }

  if (action === 'run') {
    const { presets, results } = await loadAll(env);
    const preset = presets.find((p) => p.id === params.get('id'));
    if (!preset) return [{ type: 'text', text: '這組條件已被刪除。' }];
    // 已經是最新結果就直接回覆（回覆訊息免費），不用再篩選一次
    const fresh = freshResult(preset, results);
    if (fresh) {
      let text = `ℹ️ 目前已是最新結果（資料日期 ${fresh.date}），不用重新篩選。\n\n${fresh.text}`;
      if (text.length > 4900) text = text.slice(0, 4900) + '\n…（訊息過長已截斷）';
      return [{ type: 'text', text, quickReply: presetQuickReply(presets) }];
    }
    return [{
      type: 'template', altText: '確認立即篩選',
      template: {
        type: 'confirm',
        text: `立即篩選「${preset.name}」？\n會用掉 1 則推播額度，約 1～2 分鐘後收到。`,
        actions: [
          { type: 'postback', label: '確定', data: `action=run_confirm&id=${preset.id}`, displayText: '確定' },
          { type: 'postback', label: '取消', data: 'action=cancel', displayText: '取消' },
        ],
      },
    }];
  }

  if (action === 'run_confirm') {
    await triggerWorkflow(env, params.get('id'));
    return [{ type: 'text', text: '▶️ 已開始篩選，約 1～2 分鐘後推播結果。' }];
  }

  if (action === 'cancel') return [{ type: 'text', text: '已取消。' }];

  if (['status', '用量', '目前條件'].includes(action)) {
    const [{ config }, quota] = await Promise.all([readJson(env, CONFIG_PATH), getQuota(env)]);
    const lines = ['📋 條件組合'];
    config.presets.forEach((p, i) => {
      const info = STRATEGY_INFO[p.strategy] || CUSTOM_INFO;
      lines.push(`${i + 1}. ${info.icon} ${p.name}｜${notifyText(p.notify_days)}`);
    });
    const pushDays = new Set(config.presets.flatMap((p) => p.notify_days)).size;
    lines.push('', '📨 推播額度');
    if (quota?.limit) {
      lines.push(`本月已用 ${quota.used} / ${quota.limit} 則，剩 ${quota.limit - quota.used} 則`);
    } else if (quota) {
      lines.push(`本月已用 ${quota.used} 則（無上限方案）`);
    }
    lines.push(`排程每週約推播 ${pushDays} 則（同一天多組合會合併成 1 則）`);
    lines.push('點選單「查看結果」是回覆訊息，不算額度 👍');
    return [{ type: 'text', text: lines.join('\n') }];
  }

  if (['settings', '設定', '條件設定'].includes(action)) {
    return [{
      type: 'template',
      altText: '開啟條件設定',
      template: {
        type: 'buttons',
        text: '點下方按鈕開啟條件設定頁',
        actions: [{ type: 'uri', label: '⚙️ 條件設定', uri: liffUrl(env) }],
      },
    }];
  }
  return null;
}
