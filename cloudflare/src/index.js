// Monitor on Cloudflare Workers + D1 (free, no domain needed).
// Same API as ../server.py, so monitor-js and monitor_sdk work unchanged; the dashboard polls instead of streaming.
const MAX_BODY = 1_000_000;
const KEEP_EVENTS = 50; // per issue
const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'POST, OPTIONS',
  'access-control-allow-headers': '*',
};

const json = (data, status = 200, extra = {}) =>
  new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json', ...extra } });

function same(a, b) { // constant-time string compare
  const x = new TextEncoder().encode(a || ''), y = new TextEncoder().encode(b || '');
  if (x.length !== y.length || !y.length) return false;
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

/** -> [title, culprit, grouping key]. Same key = same issue. Mirrors group() in server.py. */
export function group(event) {
  const excs = (event.exception && event.exception.values) || [];
  let title, culprit, key;
  if (excs.length) {
    const e = excs[excs.length - 1]; // outermost exception
    const frames = (e.stacktrace && e.stacktrace.frames) || [];
    const app = frames.filter(f => f.in_app);
    const top = (app.length ? app : frames).at(-1) || {};
    let where = top.filename || top.module || top.abs_path;
    // bundle names carry a per-build hash (app-3f9a1c2b.js); drop it so a deploy doesn't re-open every issue
    if (where) where = where.replace(/[-.][0-9a-f]{8,}(?=\.)|\?.*$|^https?:\/\/[^/]+/g, '');
    title = `${e.type || 'Error'}: ${e.value || ''}`.replace(/[: ]+$/, '');
    culprit = where ? `${top.function || '?'} in ${where}` : '';
    key = where ? [e.type ?? null, where, top.function ?? null] : [e.type ?? null, e.value ?? null];
  } else {
    let msg = event.logentry || event.message || 'Unknown event';
    if (typeof msg === 'object') msg = msg.formatted || msg.message || 'Unknown event';
    title = String(msg);
    culprit = event.transaction || '';
    key = [String(msg)];
  }
  const fp = event.fingerprint;
  if (Array.isArray(fp) && fp.length && !(fp.length === 1 && fp[0] === '{{ default }}')) key = fp;
  return [title.slice(0, 300), culprit.slice(0, 300), key];
}

// MONITOR_PROJECTS "1=web,2=mobile" -> { 1: 'web', 2: 'mobile' }
const projectNames = env => Object.fromEntries((env.MONITOR_PROJECTS || '').split(',').filter(p => p.includes('='))
  .map(p => p.trim().split('=')));

function named(env, issue) {
  return { ...issue, project_name: projectNames(env)[issue.project] || `project ${issue.project}` };
}

async function store(env, project, event) {
  const [title, culprit, key] = group(event);
  const digest = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(JSON.stringify([project, key])));
  const fp = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  const now = Date.now() / 1000;
  const before = await env.DB.prepare('select status from issues where fingerprint=?').bind(fp).first();
  // upsert: two simultaneous first events can't create duplicate issues
  const issue = await env.DB.prepare(
    `insert into issues(project,fingerprint,title,culprit,level,platform,environment,count,first_seen,last_seen)
     values(?,?,?,?,?,?,?,1,?,?)
     on conflict(fingerprint) do update set count=count+1, last_seen=excluded.last_seen, title=excluded.title,
       environment=excluded.environment, status=case when status='resolved' then 'unresolved' else status end
     returning *`,
  ).bind(project, fp, title, culprit, event.level || 'error', event.platform ?? null, event.environment ?? null, now, now).first();
  await env.DB.batch([
    env.DB.prepare('insert into events(issue_id, received, data) values(?,?,?)').bind(issue.id, now, JSON.stringify(event)),
    env.DB.prepare('delete from events where issue_id=? and id not in (select id from events where issue_id=? order by id desc limit ?)')
      .bind(issue.id, issue.id, KEEP_EVENTS),
  ]);
  const alert = !before ? 'new' : before.status === 'resolved' ? 'regression' : null;
  return { issue: named(env, issue), alert };
}

async function notify(env, origin, issue, alert, event) {
  const head = `${alert === 'new' ? 'New error' : 'Error is back'} in ${issue.project_name}`;
  const link = `${origin}/#issue-${issue.id}`;
  const jobs = [];
  if (env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID) { // free phone push: run ./telegram.sh once
    jobs.push(fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: env.TELEGRAM_CHAT_ID,
        text: [`🚨 ${head}`, '', issue.title, issue.culprit, event.environment && `env: ${event.environment}`, '', link]
          .filter(line => typeof line === 'string').join('\n'),
        disable_web_page_preview: true,
      }),
    }));
  }
  if (env.RESEND_API_KEY && env.ALERT_EMAIL) { // free email (resend.com) to your own address, no domain needed
    const lines = [head, '', issue.title, issue.culprit, '',
      `Environment: ${event.environment || '-'}   Release: ${event.release || '-'}`,
      event.request && event.request.url ? `URL: ${event.request.method || ''} ${event.request.url}` : '',
      `Open: ${link}`];
    jobs.push(fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, 'content-type': 'application/json' },
      body: JSON.stringify({
        from: 'Monitor <onboarding@resend.dev>',
        to: [env.ALERT_EMAIL],
        subject: `[${issue.project_name}] ${alert === 'new' ? 'New' : 'Regressed'}: ${issue.title.slice(0, 120)}`,
        text: lines.join('\n'),
      }),
    }));
  }
  const results = await Promise.allSettled(jobs);
  for (const r of results) {
    if (r.status === 'rejected') console.log('alert failed:', r.reason);
    else if (!r.value.ok) console.log('alert failed:', r.value.url, r.value.status, await r.value.text());
  }
}

/** Per project: issue counts by status, total events, and events per hour for the last 24h. Mirrors server.py. */
async function overview(env) {
  const now = Date.now() / 1000;
  const names = projectNames(env);
  const projects = new Map();
  const get = id => {
    if (!projects.has(id)) projects.set(id, { id, name: names[id] || `project ${id}`, unresolved: 0, resolved: 0, ignored: 0, events: 0, hourly: Array(24).fill(0) });
    return projects.get(id);
  };
  Object.keys(names).forEach(get);
  const [byStatus, byHour] = await env.DB.batch([
    env.DB.prepare('select project, status, count(*) n, sum(count) ev from issues group by project, status'),
    env.DB.prepare(`select i.project, cast((? - e.received) / 3600 as int) h, count(*) n from events e
      join issues i on i.id = e.issue_id where e.received > ? group by 1, 2`).bind(now, now - 86400),
  ]);
  for (const r of byStatus.results) { const p = get(r.project); p[r.status] = r.n; p.events += r.ev || 0; }
  for (const r of byHour.results) get(r.project).hourly[23 - Math.min(r.h, 23)] += r.n;
  return [...projects.values()].sort((a, b) => (+a.id || 1e9) - (+b.id || 1e9));
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const path = url.pathname;
    let m;

    // ----- ingest (what the SDKs call) -----
    if (req.method === 'OPTIONS') return new Response(null, { headers: CORS });
    if ((m = path.match(/^\/ingest\/(\d+)$/)) && req.method === 'POST') {
      if (!same(url.searchParams.get('key'), env.MONITOR_KEY)) return json({ error: 'bad key' }, 401, CORS);
      const text = await req.text();
      if (text.length > MAX_BODY) return json({ error: 'payload too large' }, 413, CORS);
      let event;
      try { event = JSON.parse(text); } catch { return json({ error: 'bad json' }, 400, CORS); }
      const { issue, alert } = await store(env, m[1], event);
      if (alert) ctx.waitUntil(notify(env, url.origin, issue, alert, event));
      return json({ id: event.event_id }, 200, CORS);
    }

    // ----- dashboard API (dashboard page itself is served from public/) -----
    if (path.startsWith('/d/')) {
      if (!same(url.searchParams.get('token'), env.MONITOR_TOKEN)) return json({ error: 'bad token' }, 401);
      if (path === '/d/issues') {
        const { results } = await env.DB.prepare('select * from issues where status=? order by last_seen desc limit 500')
          .bind(url.searchParams.get('status') || 'unresolved').all();
        return json(results.map(r => named(env, r)));
      }
      if (path === '/d/overview') return json(await overview(env));
      if ((m = path.match(/^\/d\/issues\/(\d+)$/))) {
        const issue = await env.DB.prepare('select * from issues where id=?').bind(+m[1]).first();
        if (!issue) return json({ error: 'not found' }, 404);
        const { results } = await env.DB.prepare('select received, data from events where issue_id=? order by id desc')
          .bind(+m[1]).all();
        return json({ issue: named(env, issue), event: results.length ? JSON.parse(results[0].data) : null,
          recent: results.map(r => r.received) });
      }
      if ((m = path.match(/^\/d\/issues\/(\d+)\/(unresolved|resolved|ignored)$/)) && req.method === 'POST') {
        const issue = await env.DB.prepare('update issues set status=? where id=? returning *').bind(m[2], +m[1]).first();
        return issue ? json(named(env, issue)) : json({ error: 'not found' }, 404);
      }
    }
    return json({ error: 'not found' }, 404);
  },
};
