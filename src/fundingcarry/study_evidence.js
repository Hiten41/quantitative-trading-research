"use strict";
const catalog = JSON.parse(document.getElementById("catalog").textContent);
const select = document.getElementById("dataset");
const query = document.getElementById("query");
const count = document.getElementById("count");
const dialog = document.getElementById("record");
const head = document.querySelector(".data-card thead"), body = document.querySelector(".data-card tbody");
const pageSize = 25;
const fmt = new Intl.NumberFormat("en-US",{maximumFractionDigits:4});
const windows = {Primary:"Oct 2023–Jan 2024 benchmark",Confirmation:"Jan–Apr 2024 benchmark",negative:"Sep 2022 · negative",mixed:"Jun 2022 · mixed",development:"Jun 2023 · development",validation:"Jul 2024 · validation",holdout_1:"Mar 2025 · holdout",holdout_2:"Oct 2025 · holdout"};
const policies = {A:"A · Fixed hedge",B:"B · Funding-aware",C:"C · Risk-controlled"};
const labels = {net:"Net PnL (USDT)",net_pnl:"Net PnL (USDT)",funding_net:"Net funding (USDT)",total_cost:"Total costs (USDT)",drawdown:"Drawdown (USDT)",time:"Time (UTC)",utc:"UTC",qty:"Quantity (ETH)",sha256:"SHA-256",path:"File",rate:"Funding rate",mark:"Settlement mark",source:"Source",cashflow:"Cash flow (USDT)",last_signal:"Last signal (UTC)",positive_pct:"Positive (%)",negative_pct:"Negative (%)",equity:"Equity (USDT)"};
const groups = new Map();
for(const [key,entry] of Object.entries(catalog)){
  if(!groups.has(entry.group)){
    const group=document.createElement("optgroup");group.label=entry.group;groups.set(entry.group,group);select.add(group);
  }
  groups.get(entry.group).append(new Option(entry.title,key));
}
const filters = ["window","policy","scenario"].map(field=>({field,el:document.getElementById(field+"-filter")}));
let rows=[],matched=[],fields=[],columns=[],pageNumber=0,request=0;
const human = s => s.replaceAll("_"," ").replace(/^./,c=>c.toUpperCase());
function showValue(field,value,detail=false){
  if(value===undefined || value===null || value==="")return "—";
  if(field==="window")return windows[value]||String(value);
  if(field==="policy")return policies[value]||String(value);
  if(field==="scenario")return human(String(value));
  if(["time","time_ms","last_signal","arrival_ms"].includes(field) && Number.isFinite(Number(value)))
    return new Date(Number(value)).toISOString().replace("T"," ").replace("Z","");
  if(!detail && typeof value!=="boolean" && ["net","net_pnl","total_cost","funding_net","drawdown","equity","spot_cash","futures_wallet"].includes(field))
    return Number(value).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
  return String(value);
}
function element(tag,text){
  const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;
}
function inspect(row){
  const dl=dialog.querySelector("dl");dl.replaceChildren();
  fields.forEach((key,i)=>{
    const pair=element("div"),dd=element("dd"),val=showValue(key,row[i],true);
    if(/^https?:\/\//.test(val)){
      const a=element("a",val);a.href=val;dd.append(a);
    }else{dd.textContent=val;}
    pair.append(element("dt",labels[key]||human(key)),dd);dl.append(pair);
  });
  dialog.showModal();
}
function paint(){
  const pages=Math.max(1,Math.ceil(matched.length/pageSize));
  pageNumber=Math.min(pageNumber,pages-1);
  const start=pageNumber*pageSize;
  const visible=matched.slice(start,start+pageSize);
  body.replaceChildren();
  for(const row of visible){
    const tr=element("tr");
    for(const field of columns){
      const val=row[fields.indexOf(field)];
      const cell=element("td",showValue(field,val));cell.title=showValue(field,val,true);
      if(["net","net_pnl","cashflow"].includes(field))cell.className=Number(val)<0?"loss":Number(val)>0?"gain":"";
      tr.append(cell);
    }
    const td=element("td"),button=element("button","Inspect");button.type="button";button.className="record-button";
    button.onclick=()=>inspect(row);td.append(button);tr.append(td);body.append(tr);
  }
  if(!visible.length){const cell=body.insertRow().insertCell();cell.colSpan=columns.length+1;cell.textContent="No matching records. Change the filters or search text.";}
  count.textContent=fmt.format(matched.length)+" of "+fmt.format(rows.length)+" records";
  document.getElementById("page-label").textContent="Page "+(pageNumber+1)+" of "+fmt.format(pages);
  document.getElementById("prev").disabled=!pageNumber;
  document.getElementById("next").disabled=pageNumber+1>=pages;
}
function applyFilters(){
  const selections=filters.filter(f=>f.el.value).map(f=>[fields.indexOf(f.field),f.el.value]);
  const term=query.value.trim().toLowerCase();
  matched=rows.filter(row=>selections.every(([i,val])=>String(row[i])===val) &&
    (!term || row.some((v,i)=>String(v??"").toLowerCase().includes(term) || showValue(fields[i],v,true).toLowerCase().includes(term))));
  pageNumber=0;paint();
}
async function loadDataset(){
  const token=++request;
  const key=Object.hasOwn(catalog,location.hash.slice(1))?location.hash.slice(1):"metrics";
  const entry=catalog[key];select.value=key;
  delete document.body.dataset.view;
  count.textContent="Loading records…";head.replaceChildren();body.replaceChildren();query.value="";
  for(const filter of filters){filter.el.replaceChildren();filter.el.parentElement.hidden=true;}
  document.getElementById("dataset-title").textContent=entry.title;
  const download=document.getElementById("download");download.href=entry.file;download.textContent="Download "+entry.file.split("/").pop();
  try{
    const bytes=Uint8Array.from(atob(entry.payload),ch=>ch.charCodeAt(0));
    const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    const decoded=await new Response(stream).json();
    if(token!==request)return;
    rows=decoded;fields=entry.fields;columns=entry.visible;
    const tr=head.insertRow();
    for(const key of [...columns,"record"]) tr.append(element("th",labels[key]||human(key)));
    for(const {field,el} of filters){
      const index=fields.indexOf(field);
      if(index<0)continue;
      el.parentElement.hidden=false;el.add(new Option("All "+(field==="policy"?"policies":field==="window"?"windows":"scenarios"),""));
      for(const v of [...new Set(rows.map(r=>r[index]))].sort())el.add(new Option(showValue(field,v),v));
    }
    applyFilters();document.body.dataset.view=key;
  }catch(err){
    if(token!==request)return;
    rows=[];matched=[];count.textContent="This browser could not open the table. Use the original file download.";console.error(err);
  }
}
select.onchange=()=>{location.hash=select.value;};
query.oninput=applyFilters;
for(const {el} of filters)el.onchange=applyFilters;
document.getElementById("prev").onclick=()=>{pageNumber--;paint();};
document.getElementById("next").onclick=()=>{pageNumber++;paint();};
addEventListener("hashchange",loadDataset);loadDataset();
