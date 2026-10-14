"""``lab dashboard``: the database as one self-contained HTML page (no server, no libraries).

Ratings, the pair matrix, bots with their versions, runs, experiments and notes, and the recent
games: each game opens to its seats, the score and army over time (charts) and its fights.
"""

from __future__ import annotations

import html
import json
import time
from collections import defaultdict

from . import bots as registry
from .ratings import fit
from .report import TPS, _net_series, fights, short


def _games(db, limit: int) -> list[dict]:
    out = []
    for g in db.q("SELECT * FROM games ORDER BY finished DESC LIMIT ?", limit):
        info = json.loads(g["info"] or "{}")
        start = info.get("start_tick") or 0
        seats = [dict(s) for s in db.q("SELECT * FROM seats WHERE game_id = ? ORDER BY position", g["id"])]
        series = []
        for s in seats:
            pts = _net_series(db, g["id"], s["position"], 1 - s["position"], s["kind"])
            series.append({"net": [[round((t - start) / TPS, 1), n] for t, n, _ in pts],
                           "army": [[round((t - start) / TPS, 1), r.get("army_value")] for t, _, r in pts if r],
                           "cash": [[round((t - start) / TPS, 1), (r.get("cash") or 0) + (r.get("ore") or 0)] for t, _, r in pts if r]})
        fl = []
        for s in seats:
            if s["kind"] == "ai":
                continue
            evs = [dict(e) for e in db.q("SELECT * FROM events WHERE game_id = ? AND position = ? AND kind IN "
                                         "('unit_lost', 'enemy_killed') ORDER BY tick", g["id"], s["position"])]
            fl += [{"seat": s["position"], "t": round((t - start) / TPS), "text": text} for t, text in fights(evs, start)]
            fl += [{"seat": s["position"], "t": round((e["tick"] - start) / TPS), "text": f"{e['kind']} {e['type']}"}
                   for e in db.q("SELECT * FROM events WHERE game_id = ? AND position = ? AND kind IN "
                                 "('building_lost', 'building_sold') ORDER BY tick", g["id"], s["position"])]
        fl.sort(key=lambda x: x["t"])
        out.append({"id": g["id"], "short": short(g["id"]), "finished": g["finished"], "map": g["map"],
                    "mode": g["time_mode"], "seconds": g["game_seconds"], "termination": g["termination"],
                    "winner": g["winner"], "outcome": g["outcome"], "run": g["run_id"], "label": info.get("label"),
                    "branch_of": short(g["branch_of"]) if g["branch_of"] else None, "branch_label": g["branch_label"],
                    "branch_at": round(((g["branch_tick"] or 0) - start) / TPS) if g["branch_of"] else None,
                    "seats": [{k: s[k] for k in ("bot", "version", "faction", "result", "kills_cost", "deaths_cost", "net",
                                                 "assets_value", "units_killed", "units_lost", "buildings_killed",
                                                 "buildings_lost", "batches", "rejected")} for s in seats],
                    "series": series, "fights": fl[:60]})
    return out


def build(db, limit: int = 200) -> str:
    rows = db.q("SELECT g.id, s.position, s.bot, s.points FROM games g JOIN seats s ON s.game_id = g.id "
                "WHERE g.branch_of IS NULL AND g.outcome != 'no_result' AND g.time_mode = 'virtual'")
    by = defaultdict(dict)
    for r in rows:
        by[r["id"]][r["position"]] = r
    results, pair_score, pair_n = [], defaultdict(float), defaultdict(int)
    for seats in by.values():
        if 0 in seats and 1 in seats and seats[0]["points"] is not None:
            a, b = seats[0]["bot"], seats[1]["bot"]
            results.append((a, b, float(seats[0]["points"])))
            pair_score[(a, b)] += seats[0]["points"]
            pair_score[(b, a)] += seats[1]["points"]
            pair_n[(a, b)] += 1
            pair_n[(b, a)] += 1
    ratings = fit(results) if results else {}
    names = sorted(ratings, key=lambda n: -ratings[n]["rating"])
    table = registry.load()
    bots = [{"name": n, "kind": b.kind, "version": b.version(), "think": b.think, "description": b.description,
             "tags": b.tags, "games": db.one("SELECT COUNT(*) AS n FROM seats WHERE bot = ?", n)["n"]}
            for n, b in table.items()]
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "counts": dict(db.one("SELECT COUNT(*) AS games, SUM(branch_of IS NOT NULL) AS branches, "
                              "SUM(time_mode = 'platform') AS platform FROM games")),
        "ratings": [{"bot": n, **{k: round(v, 1) for k, v in ratings[n].items()}} for n in names],
        "matrix": {"names": names, "cells": [[[pair_score[(a, b)], pair_n[(a, b)]] for b in names] for a in names]},
        "bots": bots,
        "runs": [dict(r) for r in db.q("SELECT id, started, kind, games, status, git_commit, git_dirty, note, experiment, "
                                       "command FROM runs ORDER BY id DESC LIMIT 30")],
        "experiments": [dict(e) | {"notes": [n["text"] for n in db.q("SELECT text FROM notes WHERE experiment = ?", e["name"])]}
                        for e in db.q("SELECT * FROM experiments ORDER BY created DESC")],
        "notes": [dict(n) for n in db.q("SELECT * FROM notes ORDER BY created DESC LIMIT 40")],
        "games": _games(db, limit),
    }
    return PAGE.replace("/*DATA*/null", json.dumps(data, default=str).replace("</", "<\\/"))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Lab</title>
<style>
:root{--bg:#fbfbf9;--fg:#1d1d1b;--muted:#6b6b66;--line:#e2e1dc;--card:#ffffff;--a:#2f6fd6;--b:#d2552f;--good:#1f8a4c;--bad:#c23a2b;--mid:#9a8a2b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe6;--muted:#9b9a94;--line:#2e2e2b;--card:#1e1e1c;--a:#6ea0f0;--b:#f0855f;--good:#4cc27e;--bad:#f06a5a;--mid:#d6c35a}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ecebe6;--muted:#9b9a94;--line:#2e2e2b;--card:#1e1e1c;--a:#6ea0f0;--b:#f0855f;--good:#4cc27e;--bad:#f06a5a;--mid:#d6c35a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}nav{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}nav button{border:1px solid var(--line);background:var(--card);color:var(--fg);padding:6px 12px;border-radius:999px;cursor:pointer}
nav button.on{background:var(--fg);color:var(--bg)}section{display:none}section.on{display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{font-weight:600;color:var(--muted);font-size:12px}
td.num{text-align:right;font-variant-numeric:tabular-nums}tr.game{cursor:pointer}tr.game:hover{background:color-mix(in srgb,var(--fg) 5%,transparent)}
.W{color:var(--good);font-weight:600}.L{color:var(--bad);font-weight:600}.D{color:var(--mid);font-weight:600}
.bar{height:8px;background:var(--a);border-radius:4px}.cell{text-align:center}
.detail td{white-space:normal;background:var(--bg)}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin:8px 0}
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}.chart h3{margin:0 0 4px;font-size:12px;color:var(--muted);font-weight:600}
svg{width:100%;height:150px;display:block}.legend span{margin-right:12px;font-size:12px}.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
input,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}.fights{columns:2 280px;font-size:13px}code{font-size:12px}
</style></head><body><main>
<h1>Bot Lab</h1><div class="muted" id="sub"></div>
<nav id="tabs"></nav>
<section id="ratings"></section><section id="games"></section><section id="bots"></section><section id="runs"></section><section id="notes"></section>
</main><script>
const D=/*DATA*/null;
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const clock=s=>s==null?"-":`${Math.floor(s/60)}:${String(Math.round(s%60)).padStart(2,"0")}`;
const when=t=>t?new Date(t*1000).toLocaleString([], {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}):"-";
const res=r=>({win:"W",loss:"L",draw:"D"}[r]||"-");
$("#sub").textContent=`${D.counts.games} games (${D.counts.branches||0} branches, ${D.counts.platform||0} from the platform) · generated ${D.generated} · lab dashboard to refresh`;
const tabs=[["ratings","Ratings"],["games","Games"],["bots","Bots"],["runs","Runs"],["notes","Experiments & notes"]];
let cur=localStorage.getItem("labtab")||"ratings";
function show(t){cur=t;try{localStorage.setItem("labtab",t)}catch(e){}document.querySelectorAll("section").forEach(s=>s.classList.toggle("on",s.id==t));document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.t==t))}
$("#tabs").innerHTML=tabs.map(([t,n])=>`<button data-t="${t}">${n}</button>`).join("");$("#tabs").onclick=e=>{if(e.target.dataset.t)show(e.target.dataset.t)};
// Ratings
{const r=D.ratings, max=Math.max(...r.map(x=>x.rating),1600), min=Math.min(...r.map(x=>x.rating),1400);
let h=`<p class="muted">Bradley-Terry fit over every lab game from the start (not branches), Elo scale, ± one standard error. Draws count half.</p><div class="scroll"><table><tr><th>#</th><th>bot</th><th>rating</th><th></th><th>±</th><th>games</th><th>score</th></tr>`;
r.forEach((x,i)=>{h+=`<tr><td>${i+1}</td><td>${esc(x.bot)}</td><td class="num">${Math.round(x.rating)}</td><td style="width:40%"><div class="bar" style="width:${Math.max(2,(x.rating-min+40)/(max-min+80)*100)}%"></div></td><td class="num">${Math.round(x.sd)}</td><td class="num">${x.games}</td><td class="num">${Math.round(100*x.score/x.games)}%</td></tr>`});
h+=`</table></div><h2>Pairs</h2><p class="muted">The row bot's score against the column bot (points / games).</p><div class="scroll"><table><tr><th></th>${D.matrix.names.map(n=>`<th>${esc(n)}</th>`).join("")}</tr>`;
D.matrix.names.forEach((a,i)=>{h+=`<tr><th>${esc(a)}</th>`+D.matrix.cells[i].map(([s,n])=>{if(!n)return`<td></td>`;const p=s/n, c=p>.5?"var(--good)":p<.5?"var(--bad)":"var(--mid)";return`<td class="cell" style="background:color-mix(in srgb,${c} ${Math.round(Math.abs(p-.5)*60+8)}%,transparent)">${s}/${n}</td>`}).join("")+`</tr>`});
$("#ratings").innerHTML=h+`</table></div>`;}
// Games
function chart(title, series, names){const all=series.flatMap(s=>s.pts);if(!all.length)return"";const xs=all.map(p=>p[0]), ys=all.map(p=>p[1]??0);
const x0=Math.min(...xs), x1=Math.max(...xs,x0+1), y0=Math.min(0,...ys), y1=Math.max(1,...ys), W=600,H=150,P=4;
const X=x=>P+(x-x0)/(x1-x0)*(W-2*P), Y=y=>H-P-(y-y0)/(y1-y0)*(H-2*P);
let s=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)"/>`;
series.forEach((ser,i)=>{if(ser.pts.length)s+=`<polyline fill="none" stroke="var(--${i?"b":"a"})" stroke-width="2" points="${ser.pts.map(p=>`${X(p[0]).toFixed(1)},${Y(p[1]??0).toFixed(1)}`).join(" ")}"/>`});
return `<div class="chart"><h3>${title} <span class="muted">(${Math.round(y0)} to ${Math.round(y1)})</span></h3>${s}</svg></div>`}
function detail(g){const s=g.seats;let h=`<div class="legend">${s.map((x,i)=>`<span><span class="dot" style="background:var(--${i?"b":"a"})"></span>seat ${i}: ${esc(x.bot)} <code>${esc(x.version)}</code> (${esc(x.faction)})</span>`).join("")}</div>`;
h+=`<div class="scroll"><table><tr><th>seat</th><th>result</th><th>kills $</th><th>losses $</th><th>net</th><th>assets $</th><th>units k/l</th><th>bldgs k/l</th><th>batches</th><th>refused</th></tr>`+s.map((x,i)=>`<tr><td>${i}</td><td class="${res(x.result)}">${x.result}</td><td class="num">${x.kills_cost??"-"}</td><td class="num">${x.deaths_cost??"-"}</td><td class="num">${x.net??"-"}</td><td class="num">${x.assets_value??"-"}</td><td>${x.units_killed??"-"}/${x.units_lost??"-"}</td><td>${x.buildings_killed??"-"}/${x.buildings_lost??"-"}</td><td class="num">${x.batches??"-"}</td><td class="num">${x.rejected??"-"}</td></tr>`).join("")+`</table></div>`;
h+=`<div class="charts">${chart("net score (kills - losses, $)",g.series.map(x=>({pts:x.net})))}${chart("army value ($)",g.series.map(x=>({pts:x.army})))}${chart("cash + ore",g.series.map(x=>({pts:x.cash})))}</div>`;
if(g.fights.length)h+=`<div class="fights">${g.fights.map(f=>`<div><b>${clock(f.t)}</b> seat ${f.seat}: ${esc(f.text)}</div>`).join("")}</div>`;
h+=`<p class="muted">lab show ${g.short} · lab look ${g.short} --at 5:00 · lab branch ${g.short} --at 5:00 --variants ...</p>`;return h}
function games(){const fb=$("#fbot").value, fm=$("#fmap").value, fk=$("#fkind").value;
const rows=D.games.filter(g=>(!fb||g.seats.some(s=>s.bot==fb))&&(!fm||g.map==fm)&&(fk=="all"||(fk=="branch"?g.branch_of:fk=="platform"?g.mode=="platform":!g.branch_of&&g.mode!="platform")));
$("#glist").innerHTML=`<tr><th>game</th><th>finished</th><th>map</th><th>seat 0</th><th></th><th>seat 1</th><th>net 0</th><th>length</th><th>ended by</th><th>run</th><th>note</th></tr>`+rows.map(g=>{const [a,b]=g.seats;return`<tr class="game" data-id="${g.id}"><td><code>${g.short}</code></td><td>${when(g.finished)}</td><td>${esc(g.map)}</td><td>${esc(a?.bot)}</td><td><span class="${res(a?.result)}">${res(a?.result)}</span>-<span class="${res(b?.result)}">${res(b?.result)}</span></td><td>${esc(b?.bot)}</td><td class="num">${a?.net??"-"}</td><td>${clock(g.seconds)}</td><td>${esc(g.termination)}</td><td>${g.run??""}</td><td>${g.branch_of?`branch of ${g.branch_of} @${clock(g.branch_at)} (${esc(g.branch_label)})`:esc(g.label||"")}</td></tr>`}).join("")}
{const bots=[...new Set(D.games.flatMap(g=>g.seats.map(s=>s.bot)))].sort(), maps=[...new Set(D.games.map(g=>g.map))].sort();
$("#games").innerHTML=`<div class="filters"><select id="fbot"><option value="">any bot</option>${bots.map(b=>`<option>${esc(b)}</option>`).join("")}</select><select id="fmap"><option value="">any map</option>${maps.map(m=>`<option>${esc(m)}</option>`).join("")}</select><select id="fkind"><option value="games">lab games</option><option value="branch">branches</option><option value="platform">platform</option><option value="all">all</option></select><span class="muted">the last ${D.games.length} games; click one for its charts and fights</span></div><div class="scroll"><table id="glist"></table></div>`;
["#fbot","#fmap","#fkind"].forEach(s=>$(s).onchange=games);games();
$("#glist").onclick=e=>{const tr=e.target.closest("tr.game");if(!tr)return;const nx=tr.nextElementSibling;if(nx&&nx.classList.contains("detail")){nx.remove();return}
const g=D.games.find(x=>x.id==tr.dataset.id);tr.insertAdjacentHTML("afterend",`<tr class="detail"><td colspan="11">${detail(g)}</td></tr>`)}}
// Bots, runs, notes
$("#bots").innerHTML=`<div class="scroll"><table><tr><th>bot</th><th>kind</th><th>version</th><th>think</th><th>games</th><th>tags</th><th>description</th></tr>${D.bots.map(b=>`<tr><td>${esc(b.name)}</td><td>${b.kind}</td><td><code>${esc(b.version.split("@")[1])}</code></td><td>${b.kind=="ai"?"":esc(b.think)}</td><td class="num">${b.games}</td><td>${esc(b.tags.join(", "))}</td><td style="white-space:normal">${esc(b.description)}</td></tr>`).join("")}</table></div><p class="muted">lab bots add NAME --kind ff --knobs '{...}' · versions change with the settings and the code</p>`;
$("#runs").innerHTML=`<div class="scroll"><table><tr><th>run</th><th>started</th><th>kind</th><th>games</th><th>status</th><th>commit</th><th>note</th><th>experiment</th><th>command</th></tr>${D.runs.map(r=>`<tr><td>${r.id}</td><td>${when(r.started)}</td><td>${r.kind}</td><td class="num">${r.games??""}</td><td>${r.status}</td><td><code>${esc(r.git_commit)}${r.git_dirty?"+":""}</code></td><td>${esc(r.note)}</td><td>${esc(r.experiment)}</td><td><code>${esc(r.command)}</code></td></tr>`).join("")}</table></div>`;
$("#notes").innerHTML=(D.experiments.length?D.experiments.map(e=>`<h2>${esc(e.name)} <span class="muted">(${e.status})</span></h2><p><b>Hypothesis:</b> ${esc(e.hypothesis)}</p><p><b>Conclusion:</b> ${esc(e.conclusion||"-")}</p>${e.notes.map(n=>`<p class="muted">· ${esc(n)}</p>`).join("")}`).join(""):`<p class="muted">No experiments yet: lab experiment new NAME --hypothesis "..."</p>`)
+`<h2>Recent notes</h2>`+(D.notes.map(n=>`<p>${when(n.created)} <b>${esc(n.author)}</b>${n.game_id?` on <code>${esc(n.game_id.slice(4,12))}</code>`:""}: ${esc(n.text)}</p>`).join("")||`<p class="muted">none</p>`);
show(tabs.some(t=>t[0]==cur)?cur:"ratings");
</script></body></html>
"""


def write(db, path, limit: int = 200) -> None:
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(build(db, limit))
