"""Local Kanban dashboard (stdlib only).

  python -m src.dashboard      # then open http://127.0.0.1:8788

Read-only view of the board + a few operator actions (start / ignore / unblock).
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, engines, poller, worktree


def kick_tick():
    """클릭 즉시 tick을 깨워 리뷰/승인이 바로 시작되게 함 (5분 주기 대기 회피).
    이미 도는 tick이 있으면 flock 때문에 새 프로세스는 즉시 종료(무해)."""
    try:
        subprocess.Popen([os.sys.executable, "-m", "src.tick"],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:  # noqa: BLE001
        pass

PORT = 8788

LANES = [
    ("triage", "📥 Triage (리뷰 대기)"),
    ("intake", "⏳ 시작됨"),
    ("reviewing", "🔍 리뷰 중"),
    ("verifying", "🧪 검증 중"),
    ("commenting", "✍️ 댓글 작성"),
    ("commented", "💬 댓글 완료"),
    ("lgtm", "✅ LGTM"),
    ("approve_blocked", "🔒 승인 대기"),
    ("approving", "🚀 승인 중"),
    ("done", "🏁 완료 · 머지 대기"),
]


def build_board():
    with db.connect() as c:
        cards = c.execute(
            "SELECT * FROM cards WHERE kind!='root' AND status!='archived' ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for card in cards:
            meta = json.loads(card["payload"]) if card["payload"] else {}
            if isinstance(meta, str):  # tolerate legacy double-encoded payloads
                meta = json.loads(meta)
            findings = []
            for f in db.findings_for_card(c, card["id"]):
                detail = json.loads(f["body"]) if f["body"] else {}
                findings.append({
                    "status": f["status"], "severity": f["severity"],
                    "confidence": f["confidence"], "file": f["file"], "line": f["line"],
                    "title": f["title"], "problem": detail.get("problem", ""),
                    "fix": detail.get("fix", ""),
                })
            ev = c.execute(
                "SELECT type, detail FROM events WHERE key=? AND type IN ('comment_dryrun','comment_posted') ORDER BY id",
                (card["key"],),
            ).fetchall()
            comments = []
            for e in ev:
                d = json.loads(e["detail"]) if e["detail"] else {}
                comments.append({"type": e["type"], "body": d.get("body", ""), "url": d.get("url", "")})
            clo = db.closure_counts(c, card["repo"], card["pr_number"])
            out.append({
                "id": card["id"], "kind": card["kind"], "status": card["status"],
                "engine": card["engine"] or "claude",
                "repo": card["repo"], "pr": card["pr_number"],
                "head": (card["head_sha"] or "")[:10], "blocked": card["blocked"],
                "title": meta.get("title", ""), "url": meta.get("url", ""),
                "author": meta.get("author", ""),
                "findings": findings, "comments": comments,
                "closure": {"resolved": clo.get("resolved", 0),
                            "unresolved": clo.get("unresolved", 0)},
            })
        return out


ACTIVE_REVIEW = ("intake", "reviewing", "verifying", "commenting")


def do_action(action, card_id, engine="claude"):
    if engine not in ("claude", "codex"):
        engine = "claude"
    kick = False
    stop_target = None
    with db.connect() as c:
        card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not card:
            return False
        if action == "start" and card["kind"] == "review" and card["status"] == "triage":
            if not engines.is_ready(engine):  # 로그인/설치 안 된 엔진으로 시작 차단
                db.log_event(c, "operator_start_blocked", card["key"], {"engine": engine})
                return False
            db.set_engine(c, card["id"], engine)
            db.set_status(c, card["id"], "intake")
            db.log_event(c, "operator_start", card["key"], {"engine": engine})
            kick = True
        elif action == "ignore":
            db.set_status(c, card["id"], "archived")
            db.log_event(c, "operator_ignore", card["key"])
        elif action == "unblock" and card["kind"] == "approve":
            db.set_status(c, card["id"], "approving", blocked=0)
            db.log_event(c, "operator_unblock", card["key"])
            kick = True
        elif action == "stop" and card["status"] in ACTIVE_REVIEW:
            db.set_status(c, card["id"], "archived")  # terminal → 워커가 되살리지 않음
            db.log_event(c, "review_stopped", card["key"], {"from": card["status"]})
            stop_target = (card["repo"], card["pr_number"])
        else:
            return False
    if stop_target:
        worktree.kill_review_process(*stop_target)  # 진행 중 LLM 프로세스 강제 종료
    if kick:
        kick_tick()
    return True


def refresh_poll():
    """Run the poller now (bypass the interval) — pull new PRs/heads into triage."""
    with db.connect() as c:
        before = len(db.cards_in(c, ["triage"]))
        poller.poll(c)
        after = len(db.cards_in(c, ["triage"]))
    return {"added": max(0, after - before), "total": after}


def build_mentions():
    with db.connect() as c:
        rows = db.list_mentions(c)
        return [{
            "id": r["id"],
            "channel": r["channel_name"] or r["channel_id"],
            "user": r["user_name"] or r["user_id"],
            "text": r["text"], "ts": r["ts"],
            "permalink": r["permalink"], "status": r["status"],
        } for r in rows]


def do_mention_action(action, mention_id):
    if action not in ("read", "archive"):
        return False
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM mentions WHERE id=?", (mention_id,)).fetchone():
            return False
        db.set_mention_status(c, mention_id, "read" if action == "read" else "archived")
    return True


HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lookout</title>
<style>
:root{--bg:#11151c;--panel:#1b212b;--panel2:#232b38;--line:#333d4d;--ink:#f1f5fb;
--muted:#9fabbe;--dim:#6b7688;--accent:#2dd4bf;--purple:#a78bfa;--good:#4ade80;--warn:#fbbf24;--bad:#fb7185;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5;
font-family:-apple-system,BlinkMacSystemFont,"Pretendard",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:20;padding:13px 22px;display:flex;align-items:center;gap:12px;
background:rgba(11,13,19,.86);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0;font-weight:750;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.board{display:flex;gap:12px;padding:18px;overflow-x:auto;align-items:flex-start}
.board.stack{display:block}
.toggle{display:flex;gap:6px;margin-left:6px}
.toggle button.active{background:var(--accent);color:#06101f;border-color:var(--accent);font-weight:650}
.sec{margin:0 0 16px}
.sec h2{font-size:14px;margin:0 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.sec .cards{display:flex;flex-direction:row;flex-wrap:wrap;gap:10px;padding:0}
.sec .card{width:262px}
.statuspill{font-size:11px;font-weight:650;padding:2px 9px;border-radius:20px;white-space:nowrap}
.col{background:var(--panel);border:1px solid var(--line);border-radius:14px;min-width:272px;max-width:300px;flex:0 0 auto}
.col h2{font-size:12.5px;font-weight:700;margin:0;padding:13px 15px;border-bottom:1px solid var(--line);color:var(--ink);display:flex;justify-content:space-between;align-items:center;gap:8px}
.col h2 .lh{display:flex;align-items:center;gap:8px}
.col .n{background:var(--panel2);border-radius:20px;padding:1px 9px;color:var(--muted);font-size:11.5px}
.cards{padding:11px;display:flex;flex-direction:column;gap:11px;min-height:30px}
.card{position:relative;background:var(--panel2);border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:13px;cursor:pointer;transition:border-color .14s,transform .08s,box-shadow .14s}
.card:hover{border-color:var(--accent);transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,0,0,.28)}
.pr{font-size:13px;font-weight:700;color:var(--ink);display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:7px}
.pr .num{font-size:14.5px}
.pr a{color:var(--ink);text-decoration:none}
.title{font-size:13px;color:var(--ink);opacity:.92;margin-bottom:9px;line-height:1.45;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.high{background:var(--bad)}.medium{background:var(--warn)}.low{background:var(--accent)}
.btns{display:flex;gap:7px;margin-top:11px}
.rev{display:flex;gap:7px;margin-top:11px}
.rev button{flex:1}
.rev button:disabled{background:var(--panel);border-color:var(--line);color:var(--dim);filter:none;cursor:not-allowed}
.engnote{margin-top:11px;font-size:11.5px;color:var(--warn);line-height:1.45;
  background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.32);border-radius:9px;padding:8px 10px}
.engnote code{background:var(--bg);padding:1px 5px;border-radius:5px;color:var(--warn)}
button{font:inherit;font-size:12.5px;font-weight:600;border:1px solid var(--line);background:var(--panel2);
color:var(--ink);border-radius:9px;padding:7px 12px;cursor:pointer;transition:filter .12s,transform .05s}
button:hover{filter:brightness(1.13)}
button:active{transform:translateY(1px)}
button:disabled{opacity:.6;cursor:default}
button.go{background:var(--accent);border-color:var(--accent);color:#06101f;font-weight:700}
button.claude{background:var(--accent);border-color:var(--accent);color:#06101f;font-weight:700}
button.codex{background:var(--purple);border-color:var(--purple);color:#0a0612;font-weight:700}
button.stop{background:transparent;border-color:var(--bad);color:var(--bad)}
button.stop:hover{background:var(--bad);color:#1a0608}
.filterbar{display:flex;gap:7px;flex-wrap:wrap;padding:11px 22px;border-bottom:1px solid var(--line);
  position:sticky;top:51px;z-index:15;background:rgba(11,13,19,.86);backdrop-filter:blur(8px)}
.chip{font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--panel2);color:var(--muted);
  border-radius:20px;padding:5px 12px;cursor:pointer;display:flex;align-items:center;gap:6px}
.chip:hover{filter:brightness(1.12)}
.chip.on{background:var(--accent);color:#06101f;border-color:var(--accent)}
.chip b{font-weight:700}
.rdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:0 0 auto}
.repopill{font-size:10.5px;font-weight:650;padding:2px 8px;border-radius:20px;border:1px solid var(--line);
  display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.toggle{margin-left:4px;border:1px solid var(--line);border-radius:9px;overflow:hidden;gap:0}
.toggle button{border:none;border-radius:0;background:transparent;color:var(--muted);padding:7px 13px}
.toggle button.active{background:var(--accent);color:#06101f;font-weight:700}
/* ignore: small, muted, corner — hard to hit by accident, asks to confirm */
.xbtn{position:absolute;top:7px;right:7px;font-size:11px;line-height:1;color:var(--muted);
background:transparent;border:none;padding:3px 5px;border-radius:6px;opacity:.4}
.xbtn:hover{opacity:1;color:var(--bad);background:var(--panel)}
.empty{color:var(--muted);font-size:11px;text-align:center;padding:8px 0}
/* modal */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:24px}
.ov.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:720px;width:100%;max-height:86vh;overflow:auto;padding:22px}
.modal h3{margin:0 0 6px;font-size:18px}
.finding{border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:11px;padding:14px;margin:12px 0;background:var(--panel2)}
.finding .ft{font-weight:700;font-size:14px;margin-bottom:7px;color:var(--ink)}
.finding .meta{font-size:12px;color:var(--muted);margin-bottom:9px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.lbl{font-size:11.5px;font-weight:700;color:var(--accent);margin-top:14px;margin-bottom:4px;letter-spacing:.03em;text-transform:uppercase}
.cmt{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:13px;white-space:pre-wrap;font-size:12.5px;line-height:1.5;margin-top:8px}
.close{float:right;cursor:pointer;color:var(--muted);font-weight:600}
.close:hover{color:var(--ink)}
.modal code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg);padding:1px 6px;border-radius:5px;color:var(--accent)}
.msub{color:var(--muted);font-size:12.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.msub a{color:var(--accent);word-break:break-all}
.mlink{margin:10px 0}.mlink a{color:var(--accent);font-weight:600}
.pre{white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.65;color:var(--ink)}
.lbl2{font-size:11.5px;font-weight:700;color:var(--muted);margin-top:12px;margin-bottom:3px}
.sevtag{font-size:11px;font-weight:700;padding:1px 8px;border-radius:20px;text-transform:uppercase}
.fstatus{margin-left:auto;font-size:11px;color:var(--dim)}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(12px);
  background:var(--panel2);color:var(--ink);border:1px solid var(--accent);
  padding:11px 18px;border-radius:12px;font-size:13.5px;font-weight:600;
  box-shadow:0 8px 30px rgba(0,0,0,.45);opacity:0;pointer-events:none;z-index:100;
  display:flex;align-items:center;gap:9px;transition:opacity .18s,transform .18s}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.spin{width:13px;height:13px;border:2px solid var(--accent);border-top-color:transparent;
  border-radius:50%;display:inline-block;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--panel);border:1px solid var(--line);color:var(--muted);white-space:nowrap}
/* mentions */
.mentions{padding:14px 18px 0}
.mentions h2{font-size:14px;margin:0 0 10px;display:flex;gap:8px;align-items:center}
.mlist{display:flex;flex-direction:column;gap:8px}
.m{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px;display:flex;gap:12px;align-items:flex-start}
.m.read{opacity:.55}
.m .body{flex:1;min-width:0}
.m .meta{font-size:11px;color:var(--muted);margin-bottom:4px}
.m .meta b{color:var(--ink)}
.m .txt{font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.m .acts{display:flex;gap:6px;flex:0 0 auto}
.m a.open{text-decoration:none}
.mempty{color:var(--muted);font-size:12px;padding:4px 0 12px}
.unreaddot{width:8px;height:8px;border-radius:50%;background:var(--accent);flex:0 0 auto;margin-top:5px}
</style></head><body>
<header><h1>👁 Lookout</h1>
<div class="toggle"><button id="tLane" class="active" onclick="setView('lane')">레인별</button><button id="tAuthor" onclick="setView('author')">사람별</button></div>
<button id="refreshBtn" onclick="refresh()">🔄 PR 가져오기</button>
<span class="sub" id="sub">로딩…</span>
<span class="sub" id="engStat" style="margin-left:14px"></span>
<span class="sub" style="margin-left:auto">5초마다 자동 새로고침</span></header>
<div class="filterbar" id="filterbar"></div>
<section class="mentions" id="mentions" style="display:none"></section>
<div class="board" id="board"></div>
<div class="ov" id="ov"><div class="modal" id="modal"></div></div>
<script>
const LANES=__LANES__;
// Slack 미연동 — 멘션 섹션 숨김. Slack 연결 시 true 로 바꾸면 부활.
const SHOW_MENTIONS=false;
let DATA=[];let VIEW='lane';let REPO='all';
// 엔진 가용성 — 초기엔 낙관적(true)으로 두고 /api/engines 응답으로 갱신
let ENGINES={claude:{installed:true,logged_in:true,ready:true},codex:{installed:true,logged_in:true,ready:true}};
function engReady(e){return !!(ENGINES&&ENGINES[e]&&ENGINES[e].ready);}
function engReason(e){const s=ENGINES&&ENGINES[e];
  if(!s)return '상태 확인 중';
  if(!s.installed)return e+' CLI 미설치';
  if(!s.logged_in)return e+' 로그인 필요';
  return '';}
const STATUS_META={
  triage:{c:'#2dd4bf',ko:'대기'}, intake:{c:'#6b7688',ko:'시작됨'},
  reviewing:{c:'#fbbf24',ko:'리뷰중'}, verifying:{c:'#fbbf24',ko:'검증중'},
  commenting:{c:'#fbbf24',ko:'댓글작성'}, commented:{c:'#4ade80',ko:'댓글완료'},
  lgtm:{c:'#4ade80',ko:'LGTM'}, approve_blocked:{c:'#a78bfa',ko:'승인대기'},
  approving:{c:'#a78bfa',ko:'승인중'}, done:{c:'#6b7688',ko:'완료'}};
function smeta(s){return STATUS_META[s]||{c:'#6b7688',ko:s};}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function repoShort(r){return (r||'').split('/')[1]||r;}
const REPO_COLORS=['#2dd4bf','#a78bfa','#fbbf24','#60a5fa','#4ade80','#fb7185'];
function repoColor(r){let h=0;for(const ch of (r||''))h=(h*31+ch.charCodeAt(0))>>>0;return REPO_COLORS[h%REPO_COLORS.length];}
function setRepo(r){REPO=r;renderFilter();render();}
function viewData(){return REPO==='all'?DATA:DATA.filter(c=>c.repo===REPO);}
function renderFilter(){
  const repos=[...new Set(DATA.map(c=>c.repo))].sort();
  const bar=document.getElementById('filterbar');
  let h=`<button class="chip ${REPO==='all'?'on':''}" onclick="setRepo('all')">전체 <b>${DATA.length}</b></button>`;
  repos.forEach(r=>{const n=DATA.filter(c=>c.repo===r).length;const col=repoColor(r);
    const on=REPO===r;
    h+=`<button class="chip ${on?'on':''}" style="${on?`background:${col};color:#06101f;border-color:${col}`:''}" onclick="setRepo('${r}')"><span class="rdot" style="background:${col}"></span>${esc(repoShort(r))} <b>${n}</b></button>`;});
  bar.innerHTML=h;
}
function setView(v){VIEW=v;
  document.getElementById('tLane').classList.toggle('active',v==='lane');
  document.getElementById('tAuthor').classList.toggle('active',v==='author');
  render();}
async function load(){
  const [rb,re]=await Promise.all([fetch('/api/board'),fetch('/api/engines')]);
  DATA=await rb.json();
  try{ENGINES=await re.json();}catch(e){}
  document.getElementById('sub').textContent=DATA.length+'개 카드';
  renderEngStat();
  renderFilter();
  render();
  if(SHOW_MENTIONS)loadMentions();
}
function renderEngStat(){
  const el=document.getElementById('engStat');if(!el)return;
  const parts=[['claude','Claude'],['codex','Codex']].map(([e,n])=>{
    const r=engReady(e);
    return `<span title="${r?(n+' 사용 가능'):engReason(e)}" style="color:${r?'var(--good)':'var(--dim)'}">${n} ${r?'✓':'✗'}</span>`;
  });
  el.innerHTML='⚙️ '+parts.join(' · ');
}
function reviewButtons(id){
  const defs=[['claude','리뷰 (Claude)'],['codex','리뷰 (Codex)']];
  if(!defs.some(([e])=>engReady(e)))
    return `<div class="engnote">⚠️ 리뷰 엔진 미설정 — <code>claude</code> 또는 <code>codex</code> CLI 로그인이 필요합니다.</div>`;
  let h='<div class="rev">';
  defs.forEach(([e,label])=>{
    h+= engReady(e)
      ? `<button class="${e}" onclick="act(event,'start',${id},'${e}')">${label}</button>`
      : `<button class="${e}" disabled title="${engReason(e)}">${label}</button>`;
  });
  return h+'</div>';
}
async function loadMentions(){
  const r=await fetch('/api/mentions');const M=await r.json();
  const wrap=document.getElementById('mentions');wrap.style.display='';
  const unread=M.filter(m=>m.status==='unread').length;
  let h=`<h2>📢 멘션 / 확인요청 ${unread?`<span class="pill">안읽음 ${unread}</span>`:''}</h2>`;
  if(!M.length){wrap.innerHTML=h+'<div class="mempty">아직 멘션 없음</div>';return;}
  h+='<div class="mlist">';
  M.forEach(m=>{
    const dot=m.status==='unread'?'<span class="unreaddot"></span>':'<span style="width:8px;flex:0 0 auto"></span>';
    const open=m.permalink?`<a class="open" href="${m.permalink}" target="_blank"><button>열기↗</button></a>`:'';
    h+=`<div class="m ${m.status==='unread'?'':'read'}">${dot}
      <div class="body"><div class="meta"><b>@${esc(m.user)}</b> · ${esc(m.channel)}</div>
      <div class="txt">${esc(m.text)}</div></div>
      <div class="acts">${open}
      ${m.status==='unread'?`<button onclick="mAct(${m.id},'read')">읽음</button>`:''}
      <button onclick="mAct(${m.id},'archive')">✕</button></div></div>`;
  });
  wrap.innerHTML=h+'</div>';
}
async function mAct(id,action){
  await fetch('/api/mention-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,mention_id:id})});
  loadMentions();
}
function render(){VIEW==='author'?renderByAuthor():renderLanes();}
function renderLanes(){
  const byLane={};LANES.forEach(([k])=>byLane[k]=[]);
  viewData().forEach(c=>{if(byLane[c.status])byLane[c.status].push(c)});
  const board=document.getElementById('board');board.className='board';board.innerHTML='';
  for(const [key,label] of LANES){
    const list=byLane[key]||[];
    const col=document.createElement('div');col.className='col';
    col.innerHTML=`<h2><span class="lh"><span class="dot" style="background:${smeta(key).c}"></span>${label}</span><span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';
    if(!list.length)cc.innerHTML='<div class="empty">—</div>';
    list.forEach(c=>cc.appendChild(tile(c)));
    col.appendChild(cc);board.appendChild(col);
  }
}
function renderByAuthor(){
  const byA={};viewData().forEach(c=>{(byA[c.author||'(unknown)']=byA[c.author||'(unknown)']||[]).push(c)});
  const board=document.getElementById('board');board.className='board stack';board.innerHTML='';
  Object.keys(byA).sort((a,b)=>byA[b].length-byA[a].length).forEach(author=>{
    const list=byA[author];
    const sec=document.createElement('div');sec.className='sec';
    sec.innerHTML=`<h2>👤 ${esc(author)} <span class="n">${list.length}</span></h2>`;
    const cc=document.createElement('div');cc.className='cards';
    list.forEach(c=>cc.appendChild(tile(c)));
    sec.appendChild(cc);board.appendChild(sec);
  });
}
function tile(c){
  const el=document.createElement('div');el.className='card';
  const dots=c.findings.map(f=>`<span class="dot ${f.severity||'low'}"></span>`).join('');
  let btns='', xbtn='';
  if(c.status==='triage'){
    btns=reviewButtons(c.id);
    xbtn=`<button class="xbtn" title="목록에서 제외" onclick="ignoreCard(event,${c.id})">✕</button>`;
  }
  if(c.status==='approve_blocked')btns=`<div class="btns"><button class="go" onclick="act(event,'unblock',${c.id})">🔓 승인(Unblock)</button></div>`;
  if(['intake','reviewing','verifying','commenting'].includes(c.status))
    btns=`<div class="btns"><button class="stop" onclick="stopReview(event,${c.id})">🛑 리뷰 중지</button></div>`;
  const sm=smeta(c.status);
  el.style.borderLeftColor=sm.c;
  const statusPill=`<span class="statuspill" style="background:${sm.c}22;color:${sm.c};border:1px solid ${sm.c}55">${sm.ko}</span>`;
  const enginePill=(c.status!=='triage')?`<span class="pill">${c.engine}</span>`:'';
  const clo=c.closure&&(c.closure.resolved||c.closure.unresolved)?`<span class="pill">✅${c.closure.resolved} ⚠️${c.closure.unresolved}</span>`:'';
  const rc=repoColor(c.repo);
  const repoPill=`<span class="repopill" style="background:${rc}1f;color:${rc};border-color:${rc}66"><span class="rdot" style="background:${rc}"></span>${esc(repoShort(c.repo))}</span>`;
  el.innerHTML=`${xbtn}<div class="pr">${repoPill} <span class="num">#${c.pr}</span></div>
    <div class="title">${esc(c.title)||'(제목없음)'}</div>
    <div class="row">${statusPill}<span class="pill">${esc(c.author)}</span>${enginePill}</div>
    <div class="row"><span>@${c.head}</span>${dots?`<span class="row">${dots} ${c.findings.length}건</span>`:''}${clo}</div>${btns}`;
  el.onclick=()=>openModal(c);
  return el;
}
const SEVC={high:'#fb7185',medium:'#fbbf24',low:'#2dd4bf'};
function openModal(c){
  const m=document.getElementById('modal');const sm=smeta(c.status);
  let html=`<span class="close" onclick="closeM()">✕ 닫기</span>
    <h3>#${c.pr} ${esc(c.title)}</h3>
    <div class="msub">${esc(c.repo)} · @${esc(c.author)} · <code>${c.head}</code>
      <span class="statuspill" style="background:${sm.c}22;color:${sm.c};border:1px solid ${sm.c}55">${sm.ko}</span></div>`;
  if(c.url)html+=`<div class="mlink"><a href="${c.url}" target="_blank">GitHub에서 열기 ↗</a></div>`;
  if(c.closure&&(c.closure.resolved||c.closure.unresolved))
    html+=`<div class="lbl">이전 지적 추적</div><div class="pre">✅ ${c.closure.resolved} 해결 · ⚠️ ${c.closure.unresolved} 미해결</div>`;
  if(c.findings.length){html+=`<div class="lbl">리뷰 결과 · ${c.findings.length}건</div>`;
    c.findings.forEach(f=>{const sc=SEVC[f.severity]||'#6b7688';
      html+=`<div class="finding" style="border-left-color:${sc}">
        <div class="ft">${esc(f.title)}</div>
        <div class="meta">
          <span class="sevtag" style="background:${sc}22;color:${sc};border:1px solid ${sc}55">${esc(f.severity||'?')}</span>
          <span>확신도 ${esc(f.confidence||'?')}</span><span>·</span>
          <code>${esc(f.file||'')}${f.line?(':'+esc(f.line)):''}</code>
          <span class="fstatus">${esc(f.status)}</span>
        </div>
        <div class="pre">${esc(f.problem)}</div>
        ${f.fix?`<div class="lbl2">제안</div><div class="pre">${esc(f.fix)}</div>`:''}
      </div>`});
  }else html+='<p class="sub">아직 finding 없음</p>';
  if(c.comments.length){html+='<div class="lbl">게시된 / 게시될 댓글</div>';
    c.comments.forEach(cm=>{html+=`<div class="cmt">${esc(cm.body)}</div>${cm.url?`<div class="msub"><a href="${cm.url}" target="_blank">${esc(cm.url)}</a></div>`:'<div class="msub">(dry-run · 미게시)</div>'}`})}
  m.innerHTML=html;document.getElementById('ov').classList.add('show');
}
function closeM(){document.getElementById('ov').classList.remove('show')}
document.getElementById('ov').onclick=e=>{if(e.target.id==='ov')closeM()};
const ACT_MSG={start:'리뷰 시작 — 곧 분석을 시작합니다 ⏳',unblock:'승인 진행 중 🔓',ignore:'목록에서 제외됨',stop:'리뷰 중지됨 🛑'};
async function act(e,action,id,engine){e.stopPropagation();
  showToast(ACT_MSG[action]||'처리됨', action!=='ignore');
  let j={};
  try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,card_id:id,engine:engine||'claude'})});j=await r.json();}catch(err){}
  if(action==='start'&&j&&j.ok===false)
    showToast('시작할 수 없습니다 — '+(engine?engReason(engine)||'엔진 상태 확인':'엔진 상태 확인'),false);
  load();}
function showToast(msg,spin){
  let t=document.getElementById('toast');
  if(!t){t=document.createElement('div');t.id='toast';t.className='toast';document.body.appendChild(t);}
  t.innerHTML=(spin?'<span class="spin"></span>':'')+msg;
  t.classList.add('show');clearTimeout(window._tt);
  window._tt=setTimeout(()=>t.classList.remove('show'),2600);
}
function stopReview(e,id){e.stopPropagation();
  if(confirm('이 리뷰를 강제 중지할까요? (진행 중인 분석을 종료하고 목록에서 제외)'))act(e,'stop',id);}
function ignoreCard(e,id){e.stopPropagation();
  if(confirm('이 PR을 목록에서 제외할까요? (리뷰하지 않음)'))act(e,'ignore',id);}
async function refresh(){
  const b=document.getElementById('refreshBtn');const old=b.textContent;
  b.textContent='가져오는 중…';b.disabled=true;
  try{
    const r=await fetch('/api/refresh',{method:'POST'});const j=await r.json();
    await load();
    b.textContent=j.added>0?`+${j.added}건 추가`:'최신 상태';
  }catch(e){b.textContent='실패';}
  setTimeout(()=>{b.textContent=old;b.disabled=false;},1800);
}
load();setInterval(load,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = HTML.replace("__LANES__", json.dumps(LANES, ensure_ascii=False))
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/board":
            self._send(200, json.dumps(build_board(), ensure_ascii=False))
        elif self.path == "/api/mentions":
            self._send(200, json.dumps(build_mentions(), ensure_ascii=False))
        elif self.path == "/api/engines":
            self._send(200, json.dumps(engines.availability(), ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/api/refresh":
            self._send(200, json.dumps(refresh_poll()))
            return
        if self.path == "/api/action":
            ok = do_action(data.get("action"), int(data.get("card_id", 0)),
                           data.get("engine", "claude"))
        elif self.path == "/api/mention-action":
            ok = do_mention_action(data.get("action"), int(data.get("mention_id", 0)))
        else:
            self._send(404, "{}")
            return
        self._send(200, json.dumps({"ok": ok}))


def main():
    db.init()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[dashboard] http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
