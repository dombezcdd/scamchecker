const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const $ = (id) => document.getElementById(id);
const colors = {TON:"#168fdb",USDT:"#20a987",BTC:"#e99b32",ETH:"#6877d8",BNB:"#d7a72c",SOL:"#8b72e8"};
const names = {TON:"Toncoin",USDT:"Tether",BTC:"Bitcoin",ETH:"Ethereum",BNB:"BNB",SOL:"Solana"};
let state = {balances:{}, rates:{}};
const headers = {"Content-Type":"application/json","Authorization":"tma " + (tg?.initData || "")};
function toast(msg){const el=$("toast");el.textContent=msg;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),2600)}
function fmt(n){return Number(n||0).toLocaleString("ru-RU",{maximumFractionDigits:8})}
async function api(url, options={}) {
  const res = await fetch(url,{...options,headers:{...headers,...options.headers}});
  const data = await res.json().catch(()=>({detail:"Ошибка сервера"}));
  if(!res.ok) throw new Error(data.detail || "Ошибка запроса");
  return data;
}
function renderBalances(){
  const entries=Object.entries(state.balances);
  $("assets").innerHTML=entries.map(([a,v],i)=>`<div class="asset" style="animation-delay:${i*45}ms">
    <div class="asset-left"><div class="coin" style="background:${colors[a]||"#477"}">${a[0]}</div>
    <div><div class="coin-name">${names[a]||a}</div><div class="coin-code">${a} · DEMO</div></div></div>
    <div><div class="asset-amount">${fmt(v)}</div><div class="asset-unit">${a} DEMO</div></div></div>`).join("");
  const total=Object.values(state.balances).reduce((s,n)=>s+Number(n),0);
  $("total").textContent=fmt(total)+" units";
}
function setupSelects(){
  const assets=Object.keys(state.balances);
  for(const id of ["fromAsset","toAsset"]) $(id).innerHTML=assets.map(a=>`<option value="${a}">${a}</option>`).join("");
  if(assets.length>1) $("toAsset").value=assets[1];
  updateRate();
}
function updateRate(){
  const a=$("fromAsset").value,b=$("toAsset").value;
  const rate=state.rates[a]?.[b];
  $("rate").textContent=rate?`1 ${a} DEMO = ${rate} ${b} DEMO · условный курс`:"Для пары нет курса";
}
async function loadHistory(){
  const data=await api("/api/history");
  $("history").innerHTML=data.items.length?data.items.map(x=>`<div class="history-item">
    <div>${x.kind.replaceAll("_"," ")}<small>${x.asset} · ${x.created_at}</small></div>
    <div class="${x.amount>=0?"positive":"negative"}">${x.amount>0?"+":""}${fmt(x.amount)} ${x.asset}</div>
  </div>`).join(""):`<div class="empty">Пока нет операций</div>`;
}
async function load(){
  try{
    if(!tg?.initData) throw new Error("Откройте Mini App через Telegram, чтобы авторизоваться.");
    const data=await api("/api/me");
    state.balances=data.balances;
    $("notice").textContent=data.notice;
    $("avatar").textContent=(data.user.first_name||"W").slice(0,1).toUpperCase();
    const rates=await api("/api/rates");state.rates=rates.rates;
    renderBalances();setupSelects();await loadHistory();
  }catch(e){$("notice").textContent=e.message;toast(e.message)}
}
$("refresh").onclick=load;
$("showExchange").onclick=()=>{$("exchange").classList.toggle("hidden");$("exchange").scrollIntoView({behavior:"smooth",block:"center"})};
$("fromAsset").onchange=updateRate;$("toAsset").onchange=updateRate;
$("exchangeBtn").onclick=async()=>{
  try{
    const result=await api("/api/exchange",{method:"POST",body:JSON.stringify({
      from_asset:$("fromAsset").value,to_asset:$("toAsset").value,amount:Number($("amount").value)
    })});
    toast(`Обмен выполнен: получено ${fmt(result.received)} ${$("toAsset").value} DEMO`);
    $("amount").value="";await load();
  }catch(e){toast(e.message)}
};
load();
