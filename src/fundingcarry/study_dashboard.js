"use strict";

const study = JSON.parse(document.getElementById("study").textContent);
const policies = ["A", "B", "C"];
const policyNames = ["Fixed hedge", "Funding-aware", "Risk-controlled"];
const colors = ["#69d6c3", "#ffc179", "#a6baff", "#ff9eab"];
const windowNames = {
  mixed: "Mixed funding · June 2022", negative: "Negative funding · September 2022",
  development: "Development · June 2023", Primary: "Benchmark · Oct 2023–Jan 2024",
  Confirmation: "Benchmark · Jan–Apr 2024", validation: "Validation · July 2024",
  holdout_1: "Holdout · March 2025", holdout_2: "Holdout · October 2025"
};
const scenarios = {
  base: "Base case", double_fees: "Fees ×2", triple_fees: "Fees ×3",
  double_slip: "Slippage ×2", triple_slip: "Slippage ×3", half_receipts: "50% of receipts",
  quarter_receipts: "25% of receipts", higher_margin: "Higher margin", reduced_capital: "Less capital",
  delayed_hedge: "Delayed hedge", expensive_exit: "Costlier exit", lower_mark: "Lower mark",
  upper_mark: "Upper mark", leverage_2: "2× leverage", leverage_3: "3× leverage",
  leverage_5: "5× leverage", reduced_collateral: "Less collateral", delayed_transfer: "Late transfer",
  delayed_transfer_5x: "Late transfer, 5×", exact_initial_1x: "Exact 1× initial",
  exact_initial_2x: "Exact 2× initial", exact_initial_3x: "Exact 3× initial", exact_initial_5x: "Exact 5× initial"
};
const windows = Object.keys(study.windows).sort((a,b) => study.windows[a].start.localeCompare(study.windows[b].start));
const baseRows = study.metrics.filter(r => r.scenario === "base");
const select = document.getElementById("win");
const narrow = matchMedia("(max-width:600px)");
const options = {responsive:true, displaylogo:false, modeBarButtonsToRemove:["lasso2d","select2d"]};
const format = new Intl.NumberFormat("en-US", {minimumFractionDigits:2, maximumFractionDigits:2});
const signed = n => (n < 0 ? "−" : n > 0 ? "+" : "") + format.format(Math.abs(n));
const date = t => new Date(t).toISOString();
const utc = t => date(t).slice(0,16).replace("T"," ");
const scenarioName = s => scenarios[s] || s.replaceAll("_"," ");
const bar = (x,y,name) => ({x,y,name,type:"bar"});
const line = (x,y,name) => ({x,y,name,type:"scatter",mode:"lines"});
function node(tag,text) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  return item;
}
function note(id,message) {
  const el = document.getElementById("plot"+id).parentNode.querySelector(".chart-note");
  el.hidden = !message;
  el.textContent = message;
}
function byPolicy(rows,p) { return rows.find(r=>r.policy===p); }
function fromURL() {
  const url = new URL(location.href);
  const w = (url.protocol === "file:" ? new URLSearchParams(url.hash.slice(1)) : url.searchParams).get("window");
  return windows.includes(w) ? w : "holdout_1";
}
for (const w of windows) select.add(new Option(windowNames[w],w));
select.value = fromURL();
const holdouts = baseRows.filter(r => r.window.startsWith("holdout"));
const fixedNet = holdouts.filter(r=>r.policy==="A").reduce((sum,r)=>sum+r.net,0);
document.getElementById("holdout").textContent = "Across the two 2025 holdouts, the fixed hedge returned " +
  signed(fixedNet) + " USDT on separate 10,000 USDT allocations." +
  (holdouts.filter(r=>r.policy!=="A").every(r=>r.holding_hours===0) ? " Both adaptive policies stayed in cash." : "");

function results(w,base) {
  const range = study.windows[w];
  document.getElementById("dates").textContent = utc(range.start)+" to "+utc(range.end)+" UTC (end exclusive)";
  const cards = policies.map((p,i)=>{
    const row = byPolicy(base,p);
    const card = node("div"); card.className="result";
    const value = node("strong",signed(row.net)+" USDT");
    value.className = row.net<0?"loss":row.net>0?"gain":"";
    card.append(node("span",p+" · "+policyNames[i]),value,
      node("small",row.holding_hours===0?"Stayed in cash":"Net PnL after costs"));
    return card;
  });
  document.getElementById("summary").replaceChildren(...cards);
}

// Native toggle buttons expose trace names and visibility to both keyboard and pointer users.
function legend(container,plot,traces) {
  container.replaceChildren();
  if (traces.length<2) return;
  for (const [i,t] of traces.entries()) {
    const button = node("button",t.name);
    button.type="button"; button.className="trace-key";
    button.style.setProperty("--trace", t.marker?.color || t.line?.color || colors[i%colors.length]);
    button.setAttribute("aria-pressed","true");
    button.setAttribute("aria-controls",plot.id);
    button.addEventListener("click",()=>{
      const visible = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed",String(visible));
      Plotly.restyle(plot,{visible},[i]);
    });
    container.append(button);
  }
}
function draw(i,traces,unit,xType,extra={}) {
  const el=document.getElementById("plot"+i);
  const {xaxis={}, yaxis={}, ...rest}=extra;
  // Plotly mutates nested layout objects. Every panel and every window gets fresh axes.
  const layout={
    autosize:true,showlegend:false,colorway:[...colors],hovermode:"closest",
    paper_bgcolor:"#131f30",plot_bgcolor:"#131f30",font:{color:"#d8e3ef",family:"Segoe UI, sans-serif"},
    margin:{l:64,r:16,t:12,b:48},
    xaxis:{type:xType,autorange:true,gridcolor:"#29394d",...xaxis},
    yaxis:{autorange:true,gridcolor:"#29394d",zerolinecolor:"#506178",automargin:true,title:{text:unit,standoff:8},...yaxis},
    ...rest
  };
  legend(el.parentNode.querySelector(".chart-controls"),el,traces);
  return Plotly.newPlot(el,traces,layout,options);
}
function curves(series,key,scale=1) {
  return policies.map((p,i)=>{
    const c=series[p].curves;
    const t=line(c.time.map(date),c[key].map(v=>v*scale),p);
    t.line={color:colors[i],width:[2,4,2][i],dash:i===2?"dot":"solid"};
    return t;
  });
}
function riskPanel(w,series) {
  const plot=document.getElementById("plot11");
  const section=plot.parentNode;
  section.querySelectorAll(".risk-options,details").forEach(el=>el.remove());
  const events=study.risks.filter(r=>r.window===w);
  const riskSelect=node("select"); riskSelect.id="risk-filter";
  riskSelect.add(new Option("All stress scenarios",""));
  for (const s of new Set(events.map(e=>e.scenario))) riskSelect.add(new Option(scenarioName(s),s));
  const label=node("label","Stress scenario"); label.className="risk-options"; label.append(riskSelect);
  if(events.length) section.insertBefore(label,plot);
  const details=node("details"); section.append(details);
  function update() {
    const filtered=events.filter(e=>!riskSelect.value||e.scenario===riskSelect.value);
    const groups=new Map();
    for(const event of filtered) {
      const key=event.policy+"@"+event.time;
      if (!groups.has(key)) groups.set(key,[]);
      groups.get(key).push(event);
    }
    const marks=policies.map((p,i)=>{
      const f=series[p].fills;
      return {type:"scatter",mode:"markers",x:f.time.map(date),y:f.time.map(()=>p),name:p+" fills",
        text:f.venue.map((v,j)=>v+" "+f.side[j]),marker:{color:colors[i],size:8}};
    });
    if(groups.size) {
      const batches=Array.from(groups.values());
      marks.push({type:"scatter",mode:"markers+text",name:"Stress events",
        x:batches.map(g=>date(g[0].time)),y:batches.map(g=>g[0].policy),
        text:batches.map(g=>g.length>1?"×"+g.length:""),textposition:"top center",
        customdata:batches.map(g=>g.map(e=>scenarioName(e.scenario)+" · "+e.event).join("<br>")),
        hovertemplate:"%{x}<br>Policy %{y}<br>%{customdata}<extra></extra>",
        marker:{color:colors[3],symbol:"diamond-open",size:12}});
    }
    note(11,filtered.length ? filtered.length+" stress events at "+groups.size+" policy/time positions. Labels show coincident counts." : "No stress events in this window.");
    details.replaceChildren(); details.hidden=!filtered.length;
    if(filtered.length) {
      const table=node("table"); const head=table.createTHead().insertRow();
      for(const field of ["UTC","Policy","Scenario","Event"]) head.append(node("th",field));
      const body=table.createTBody();
      for(const e of [...filtered].sort((a,b)=>a.time-b.time)) {
        const tr=body.insertRow();
        for(const value of [utc(e.time),e.policy,scenarioName(e.scenario),e.event]) tr.insertCell().textContent=value;
      }
      const wrap=node("div");wrap.className="event-list";wrap.append(table);
      details.append(node("summary","Inspect "+filtered.length+" stress events"),wrap);
    }
    return draw(11,marks,"Policy","date",{yaxis:{type:"category",categoryorder:"array",categoryarray:[...policies]}});
  }
  riskSelect.addEventListener("change",update);
  return update();
}
async function render(w) {
  const series=study.series[w], base=baseRows.filter(r=>r.window===w);
  const local=study.metrics.filter(r=>r.window===w);
  results(w,base);
  const jobs=[];
  const equal=series.B.curves.time.length===series.C.curves.time.length &&
    series.B.curves.time.every((t,i)=>t===series.C.curves.time[i] && series.B.curves.equity[i]===series.C.curves.equity[i]);
  note(0,equal?"B and C have identical equity. C is dotted over B. Use the buttons to inspect either policy.":"");
  jobs.push(draw(0,curves(series,"equity"),"Equity · USDT","date",{yaxis:{tickformat:",.2f"}}));
  const flows=policies.map(p=>{
    const f=series[p].settlements;
    return {...bar(f.time.map(date),f.cashflow,p),
      text:f.source.map((v,i)=>v+" · mark "+f.mark[i]+" · gap "+f.gap_ms[i]+" ms · short "+f.short[i]+" ETH"),
      hovertemplate:"%{x}<br>%{y:.5f} USDT<br>%{text}<extra>%{fullData.name}</extra>"};
  });
  jobs.push(draw(1,flows,"Payment · USDT","date",{barmode:"group"}));
  jobs.push(draw(2,[line(series.A.curves.time.map(date),series.A.curves.basis_bps,"Perpetual premium")],"Basis points","date"));
  jobs.push(draw(3,curves(series,"funding"),"Funding · USDT","date"));
  const windowTicks=windows.map(key=>windowNames[key].split(" · ")[1]);
  jobs.push(draw(4,policies.map(p=>bar(windows,windows.map(k=>baseRows.find(r=>r.policy===p&&r.window===k).net),p)),
    "Net PnL · USDT","category",{barmode:"group",xaxis:{tickmode:"array",tickvals:[...windows],ticktext:windowTicks,tickangle:-35,automargin:true}}));
  jobs.push(draw(5,["funding_received","funding_paid","total_cost"].map((k,i)=>bar(policies,base.map(r=>r[k]),["Received","Paid","Costs"][i])),
    "USDT","category",{barmode:"group"}));
  note(6,"Maximum minute-close drawdown · "+base.map(r=>r.policy+" "+format.format(r.drawdown)).join(" / ")+" USDT. The curves include each policy’s worst minute.");
  jobs.push(draw(6,curves(series,"drawdown"),"Drawdown · USDT","date"));
  jobs.push(draw(7,curves(series,"margin",100),"Maintenance / equity · %","date"));
  let streak=0;const funding=series.A.settlements;
  jobs.push(draw(8,[bar(funding.time.map(date),funding.rate.map(v=>streak=v<0?streak+1:0),"Negative run")],"Consecutive events","date"));
  const cases=[...new Set(local.map(r=>r.scenario))];
  const z=cases.map(s=>policies.map(p=>local.find(r=>r.scenario===s&&r.policy===p).net));
  const bound=Math.max(...z.flat().map(Math.abs),0.01);
  jobs.push(draw(9,[{type:"heatmap",x:[...policies],y:cases.map(scenarioName),z,zmin:-bound,zmax:bound,
    colorscale:[[0,"#853f4c"],[0.5,"#25384d"],[1,"#226859"]],showscale:false,
    texttemplate:"%{z:.2f}",textfont:{color:"#fff",size:narrow.matches?11:13},
    hovertemplate:"%{y} · Policy %{x}<br>%{z:+.2f} USDT<extra></extra>"}],
    "","category",{margin:{l:narrow.matches?110:145,r:6,t:34,b:8},xaxis:{side:"top"},yaxis:{type:"category",autorange:"reversed",automargin:false}}));
  note(9,"Net PnL in USDT per scenario. Red cells show losses; green cells show gains.");
  jobs.push(draw(10,[bar(policies,base.map(r=>r.funding_received),"Actual receipts"),
    bar(policies,base.map(r=>r.breakeven_gross_receipts),"Break-even receipts")],"USDT","category",{barmode:"group"}));
  jobs.push(riskPanel(w,series));
  await Promise.all(jobs);
  document.body.dataset.loaded=w;
}

let rendering=Promise.resolve();
function refresh() {
  const w=select.value;
  delete document.body.dataset.loaded;
  rendering=rendering.then(()=>render(w));
}
select.addEventListener("change",()=>{
  const url=new URL(location.href);
  if(url.protocol==="file:") url.hash="window="+encodeURIComponent(select.value);
  else url.searchParams.set("window",select.value);
  history.pushState(null,"",url);
  refresh();
});
window.addEventListener("popstate",()=>{select.value=fromURL();refresh();});
narrow.addEventListener("change",refresh);
refresh();
