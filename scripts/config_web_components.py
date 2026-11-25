"""配置页的自定义 Select 组件与异步按钮状态。

原生 <select> 在深色主题下无法统一，弹出层由系统绘制。这里保留原生 select
作为唯一数据源（现有代码继续读 .value），只把视觉层换成自绘 listbox：
关闭时不占 DOM 顺序、支持键盘、双主题、移动端可用。
"""
from __future__ import annotations

from typing import Any


CSS = r"""
.arsel{position:relative;min-width:0}
.arsel-native{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.arsel-trigger{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;min-height:39px;padding:9px 11px;border:1px solid var(--line);border-radius:7px;background:#0b1421;color:var(--ink);font:inherit;font-size:13px;text-align:left;cursor:pointer;transition:border-color .18s,box-shadow .18s,background .18s}
.arsel-trigger:hover{border-color:var(--line-strong)}
.arsel-trigger:focus-visible{outline:0;border-color:var(--cyan);box-shadow:0 0 0 3px rgba(88,166,255,.12)}
.arsel-trigger[aria-expanded="true"]{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(88,166,255,.12)}
.arsel-trigger[aria-disabled="true"]{opacity:.5;cursor:not-allowed}
.arsel-trigger>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arsel-trigger svg{width:14px;height:14px;flex:none;stroke-width:1.9;color:var(--muted);transition:transform .18s,color .18s}
.arsel-trigger[aria-expanded="true"] svg{transform:rotate(180deg);color:var(--cyan)}
.arsel-pop{position:absolute;z-index:30;left:0;right:0;top:calc(100% + 6px);max-height:min(288px,52vh);overflow:auto;padding:5px;border:1px solid var(--line-strong);border-radius:8px;background:#132033;box-shadow:0 18px 44px rgba(0,0,0,.44);animation:arselIn .16s ease both}
.arsel-pop[hidden]{display:none}
.arsel-group{padding:7px 9px 4px;color:var(--dim);font:10px/1.4 var(--display);letter-spacing:.05em}
.arsel-opt{display:flex;align-items:center;justify-content:space-between;gap:9px;padding:8px 9px;border-radius:6px;color:var(--ink);font-size:13px;line-height:1.4;cursor:pointer;overflow-wrap:anywhere}
.arsel-opt:hover,.arsel-opt.act{background:var(--accent-soft);color:var(--cyan-bright)}
.arsel-opt.on{background:rgba(88,166,255,.14);color:var(--cyan-bright);font-weight:600}
.arsel-opt.off{opacity:.42;cursor:not-allowed}
.arsel-opt.off:hover{background:transparent;color:var(--ink)}
.arsel-opt svg{width:13px;height:13px;flex:none;stroke-width:2.2;color:var(--teal)}
@keyframes arselIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
html[data-theme="light"] .arsel-trigger{border-color:var(--line);background:#fff;color:#1f2328}
html[data-theme="light"] .arsel-trigger:hover{border-color:var(--line-strong)}
html[data-theme="light"] .arsel-trigger:focus-visible,html[data-theme="light"] .arsel-trigger[aria-expanded="true"]{border-color:#0969da;box-shadow:0 0 0 3px rgba(9,105,218,.15)}
html[data-theme="light"] .arsel-pop{border-color:#d0d7de;background:#fff;box-shadow:0 12px 32px rgba(31,35,40,.16)}
html[data-theme="light"] .arsel-opt:hover,html[data-theme="light"] .arsel-opt.act{background:#f0f6ff;color:#0550ae}
html[data-theme="light"] .arsel-opt.on{background:#ddf4ff;color:#0550ae}
@media(max-width:460px){.arsel-pop{max-height:min(240px,46vh)}}

.btn[aria-busy="true"]{pointer-events:none;opacity:.72}
.btn .btn-spin{width:13px;height:13px;flex:none;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:arselSpin .7s linear infinite}
@keyframes arselSpin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){
  .arsel-pop{animation:none}
  .btn .btn-spin{animation-duration:1.6s}
}
"""


JAVASCRIPT = r"""
const ARSEL=new WeakMap();
let arselSeq=0;
function arselVisibleOptions(select){
  return [...select.options].filter(o=>!(o.parentElement&&o.parentElement.tagName==='OPTGROUP'&&o.parentElement.disabled));
}
function arselSelected(select){
  const options=arselVisibleOptions(select);
  return options.find(o=>o.value===select.value)||options[0]||null;
}
function arselRender(select){
  const state=ARSEL.get(select);
  if(!state)return;
  const options=arselVisibleOptions(select);
  const current=arselSelected(select);
  state.label.textContent=current?current.textContent:(select.dataset.placeholder||'未选择');
  state.trigger.disabled=!!select.disabled;
  state.trigger.setAttribute('aria-disabled',select.disabled?'true':'false');
  let html='';
  let group=null;
  const flat=[];
  [...select.options].forEach(option=>{
    const parent=option.parentElement;
    const groupName=parent&&parent.tagName==='OPTGROUP'?(parent.label||''):'';
    if(groupName&&groupName!==group){
      group=groupName;
      html+=`<div class="arsel-group" role="presentation">${esc(groupName)}</div>`;
    }else if(!groupName&&group!==''){
      group='';
    }
    const disabled=option.disabled||(parent&&parent.tagName==='OPTGROUP'&&parent.disabled);
    const on=option.value===select.value;
    const id=`${state.id}-opt-${flat.length}`;
    flat.push({value:option.value,disabled:!!disabled,id});
    html+=`<div class="arsel-opt${on?' on':''}${disabled?' off':''}" role="option" id="${id}" data-value="${esc(option.value)}" aria-selected="${on}"${disabled?' aria-disabled="true"':''}><span>${esc(option.textContent||'')}</span>${on?'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>':''}</div>`;
  });
  state.pop.innerHTML=html||'<div class="arsel-opt off"><span>暂无可选项</span></div>';
  state.options=flat;
  if(state.open)arselHighlight(select,Math.max(0,flat.findIndex(item=>!item.disabled&&item.value===select.value)));
}
function arselHighlight(select,index){
  const state=ARSEL.get(select);
  if(!state)return;
  const nodes=[...state.pop.querySelectorAll('.arsel-opt[role="option"]')];
  if(!nodes.length)return;
  const bounded=Math.max(0,Math.min(index,nodes.length-1));
  state.active=bounded;
  nodes.forEach((node,position)=>{
    const on=position===bounded;
    node.classList.toggle('act',on);
    if(on)state.trigger.setAttribute('aria-activedescendant',node.id);
  });
  const node=nodes[bounded];
  if(node&&node.scrollIntoView)node.scrollIntoView({block:'nearest'});
}
function arselOpen(select){
  const state=ARSEL.get(select);
  if(!state||state.open||select.disabled)return;
  state.open=true;
  state.pop.hidden=false;
  state.trigger.setAttribute('aria-expanded','true');
  arselRender(select);
  const start=state.options.findIndex(item=>!item.disabled&&item.value===select.value);
  arselHighlight(select,start<0?0:start);
}
function arselClose(select,refocus){
  const state=ARSEL.get(select);
  if(!state||!state.open)return;
  state.open=false;
  state.pop.hidden=true;
  state.trigger.setAttribute('aria-expanded','false');
  state.trigger.removeAttribute('aria-activedescendant');
  if(refocus)state.trigger.focus();
}
function arselChoose(select,value){
  const option=[...select.options].find(item=>item.value===value);
  if(!option||option.disabled)return;
  const changed=select.value!==value;
  select.value=value;
  arselClose(select,true);
  arselRender(select);
  if(changed)select.dispatchEvent(new Event('change',{bubbles:true}));
}
function arselMove(select,step){
  const state=ARSEL.get(select);
  if(!state)return;
  const total=state.options.length;
  if(!total)return;
  let index=typeof state.active==='number'?state.active:0;
  for(let guard=0;guard<total;guard+=1){
    index=(index+step+total)%total;
    if(!state.options[index].disabled){arselHighlight(select,index);return}
  }
}
function arselEnhance(select){
  if(!select||select.dataset.arsel==='on')return;
  select.dataset.arsel='on';
  const id=`arsel-${(arselSeq+=1)}`;
  const wrap=document.createElement('div');
  wrap.className='arsel';
  select.parentNode.insertBefore(wrap,select);
  wrap.appendChild(select);
  select.classList.add('arsel-native');
  select.setAttribute('tabindex','-1');
  select.setAttribute('aria-hidden','true');
  const trigger=document.createElement('button');
  trigger.type='button';
  trigger.className='arsel-trigger';
  trigger.setAttribute('role','combobox');
  trigger.setAttribute('aria-haspopup','listbox');
  trigger.setAttribute('aria-expanded','false');
  trigger.setAttribute('aria-controls',`${id}-list`);
  trigger.innerHTML='<span></span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  const pop=document.createElement('div');
  pop.className='arsel-pop';
  pop.id=`${id}-list`;
  pop.setAttribute('role','listbox');
  pop.hidden=true;
  wrap.appendChild(trigger);
  wrap.appendChild(pop);
  ARSEL.set(select,{id,wrap,trigger,pop,label:trigger.querySelector('span'),open:false,active:0,options:[]});
  trigger.addEventListener('click',()=>{
    const state=ARSEL.get(select);
    if(state.open)arselClose(select,true);else arselOpen(select);
  });
  trigger.addEventListener('keydown',event=>{
    const state=ARSEL.get(select);
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();
      if(!state.open)arselOpen(select);
      else arselMove(select,event.key==='ArrowDown'?1:-1);
      return;
    }
    if(event.key==='Enter'||event.key===' '){
      event.preventDefault();
      if(!state.open){arselOpen(select);return}
      const node=state.options[state.active];
      if(node&&!node.disabled)arselChoose(select,node.value);
      return;
    }
    if(event.key==='Escape'&&state.open){event.preventDefault();arselClose(select,true);return}
    if(event.key==='Home'&&state.open){event.preventDefault();arselHighlight(select,0);return}
    if(event.key==='End'&&state.open){event.preventDefault();arselHighlight(select,state.options.length-1);return}
    if(event.key==='Tab')arselClose(select,false);
  });
  pop.addEventListener('click',event=>{
    const node=event.target.closest('.arsel-opt[role="option"]');
    if(!node)return;
    if(node.getAttribute('aria-disabled')==='true')return;
    arselChoose(select,node.dataset.value);
  });
  pop.addEventListener('mousemove',event=>{
    const node=event.target.closest('.arsel-opt[role="option"]');
    if(!node||node.getAttribute('aria-disabled')==='true')return;
    const state=ARSEL.get(select);
    const index=state.options.findIndex(item=>item.id===node.id);
    if(index>=0&&index!==state.active)arselHighlight(select,index);
  });
  select.addEventListener('change',()=>arselRender(select));
  arselRender(select);
}
function arselEnhanceAll(root){
  (root||document).querySelectorAll('select:not([data-arsel="on"])').forEach(arselEnhance);
}
document.addEventListener('click',event=>{
  if(event.target.closest('.arsel'))return;
  document.querySelectorAll('select[data-arsel="on"]').forEach(select=>arselClose(select,false));
});
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  document.querySelectorAll('select[data-arsel="on"]').forEach(select=>arselClose(select,false));
});
function withBusy(button,task,label){
  const node=typeof button==='string'?$(button):button;
  if(!node)return Promise.resolve().then(task);
  const original=node.innerHTML;
  const text=label||'处理中…';
  node.setAttribute('aria-busy','true');
  node.disabled=true;
  node.innerHTML=`<span class="btn-spin" aria-hidden="true"></span><span>${esc(text)}</span>`;
  const restore=()=>{
    node.removeAttribute('aria-busy');
    node.disabled=false;
    node.innerHTML=original;
    if(node.dataset.arselRestore==='1'){node.dataset.arselRestore='0'}
  };
  return Promise.resolve().then(task).finally(restore);
}
"""


def inject(page: str, replace_once: Any) -> str:
    """Attach the component CSS and JS, then enhance every existing select."""
    page = replace_once(page, "</head>", f"<style>{CSS}</style></head>")
    page = replace_once(
        page,
        "initTheme();render();",
        JAVASCRIPT + "\ninitTheme();render();arselEnhanceAll();new MutationObserver(()=>arselEnhanceAll()).observe(document.body,{childList:true,subtree:true});",
    )
    return page
