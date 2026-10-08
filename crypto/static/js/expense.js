/* 支出管理：只保留当前页面草稿，所有金额保存与预览以服务端整数分为准。 */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const state = {settings:null, categories:[], tags:[], templates:[], dashboard:null, date:'', tab:'overview',
        page:1, records:new Map(), charts:{}, dirty:new Set(), edit:null, requestId:null, pending:null, saving:false, private:false, refresh:0, preview:0};
    const fmt = value => value == null ? '未设置' : `¥${String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ',')}`;
    const money = value => `<span class="ex-money">${esc(fmt(value))}</span>`;
    const minor = value => { const n = BigInt(value || 0); const a = n < 0n ? -n : n; return `${n < 0n ? '-' : ''}${a / 100n}.${String(a % 100n).padStart(2,'0')}`; };
    const intMoney = value => { if (!/^\d{1,8}(\.\d{1,2})?$/.test(value)) throw Error('金额须为最多两位小数的正数'); const [a,b=''] = value.split('.'); return BigInt(a)*100n + BigInt(b.padEnd(2,'0')); };
    const auditTime = value => new Intl.DateTimeFormat('zh-CN',{timeZone:state.settings.timezone,dateStyle:'short',timeStyle:'medium',hour12:false}).format(new Date(value));
    const dateAdd = (value,n) => { const d = new Date(value+'T00:00:00Z'); d.setUTCDate(d.getUTCDate()+n); return d.toISOString().slice(0,10); };
    const uuid = () => { const b = new Uint8Array(16); crypto.getRandomValues(b); return Array.from(b,x=>x.toString(16).padStart(2,'0')).join(''); };
    const fields = form => Object.fromEntries(new FormData(form));
    const selectedTags = root => Array.from(root.querySelectorAll('input[type=checkbox]:checked'), e=>e.value);
    const catName = id => { const c = state.categories.find(x=>x.id===id); const p=state.categories.find(x=>x.id===c?.parent_id); return c ? `${p ? p.name+' / ' : ''}${c.name}` : '未分类'; };
    const kindName = value => ({expense:'支出',refund:'回款',movement:'资金划转'}[value] || value);
    const button = (action,id,label) => `<button type="button" data-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
    const table = (heads, rows) => rows.length ? `<div class="ex-table-wrap"><table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>` : '<div class="ex-empty">暂无数据</div>';
    const row = cells => `<tr>${cells.map(c=>`<td>${c}</td>`).join('')}</tr>`;
    const tagChips = ids => ids.map(id=>`<span class="ex-chip">${esc(state.tags.find(t=>t.id===id)?.name || id)}</span>`).join('');
    const on = (id,event,fn) => $(id).addEventListener(event, e=>{ try { Promise.resolve(fn(e)).catch(showError); } catch(error) { showError(error); } });
    function showError(error) { $('error').textContent=error.message || String(error); $('error').hidden=false; }
    function notice(text) { $('message').textContent=''; const span=document.createElement('span'); span.textContent=text; if(text.includes('¥')) span.className='ex-money'; $('message').appendChild(span); $('message').hidden=false; }
    function clearError() { $('error').hidden=true; }
    async function request(path, method='GET', body) {
        const response = await fetch('/expense/api'+path, {method, credentials:'same-origin', cache:'no-store',
            headers:{'Accept':'application/json', ...(method !== 'GET' ? {'Content-Type':'application/json','X-Expense-Request':'1'} : {})},
            ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
        let result;
        try { result=await response.json(); } catch (_) { throw Error('响应无法解析，请保留草稿并重试原请求'); }
        if (!response.ok || !result.success) {
            const error=new Error(result.error?.message || result.message || '请求失败');
            error.status=response.status; error.details=result.error?.details; throw error;
        }
        return result.data;
    }
    const query = args => '?'+new URLSearchParams(Object.entries(args).filter(([,v])=>v!=='' && v!=null));
    function confirmText(title,text) {
        return new Promise(resolve=>MDialog.show({title:esc(title), message:`<div class="ex-dialog ${state.private?'ex-private':''}"><span class="${text.includes('¥')?'ex-money':''}">${esc(text)}</span></div>`, type:'warning', showCancel:true,
            onOk:()=>resolve(true), onCancel:()=>resolve(false)}));
    }
    function modalForm(title, html, save) {
        let busy=false;
        const overlay=MDialog.show({title:esc(title), width:'740px', message:`<div class="ex-dialog ${state.private?'ex-private':''}"><form class="ex-modal-form">${html}<div class="ex-error" role="alert" hidden></div><button class="primary" type="submit">保存确认</button></form></div>`,
            buttons:[{text:'取消',type:'cancel'}]});
        const form=overlay.querySelector('form');
        form.addEventListener('submit',async e=>{
            e.preventDefault(); if(busy) return;
            busy=true; const submit=form.querySelector('[type=submit]'); submit.disabled=true;
            try { await save(form); MDialog.close(overlay); notice('已保存'); await refresh().catch(showError); }
            catch(error) { const target=form.querySelector('.ex-error'); target.textContent=error.message; target.hidden=false; }
            finally { busy=false; submit.disabled=false; }
        });
        return form;
    }
    const input = (label,name,value='',type='text',extra='') => `<label>${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
    const check = (label,name,value=false) => `<label class="ex-check"><input type="checkbox" name="${name}" ${value?'checked':''}>${esc(label)}</label>`;
    function categoryOptions(selected='', all=false, roots=false) {
        const categories=state.categories.filter(c=>(all || (c.is_active && !state.categories.some(p=>p.id===c.parent_id&&!p.is_active)) || c.id===selected) && (!roots || (!c.parent_id && !c.is_system)));
        if(!all&&!roots) {
            const recent=state.recent_categories||[];
            const rank=c=>recent.includes(c.id)?recent.indexOf(c.id):recent.length;
            categories.sort((a,b)=>rank(a)-rank(b));
        }
        return '<option value="">'+(all?'全部 / 不选择':'未分类')+'</option>'+categories.map(c=>`<option value="${esc(c.id)}" ${c.id===selected?'selected':''}>${esc(catName(c.id))}${c.is_active?'':'（停用）'}</option>`).join('');
    }
    const categoryField = selected => `<label>主分类<select name="category_id">${categoryOptions(selected)}</select></label>`;
    function tagBoxes(ids=[],all=false) {
        return state.tags.filter(t=>all || t.is_active || ids.includes(t.id)).map(t=>`<label><input type="checkbox" value="${esc(t.id)}" ${ids.includes(t.id)?'checked':''}>${esc(t.name)}${t.is_active?'':'（停用）'}</label>`).join('');
    }
    function putForm(form, values) {
        for(const [key,value] of Object.entries(values)) {
            const field=form.elements.namedItem(key); if(!field) continue;
            if(field.type==='checkbox') field.checked=Boolean(value); else field.value=value ?? '';
        }
    }
    async function bootstrap() {
        const data=await request('/bootstrap'); Object.assign(state,data);
        $('setup-panel').hidden=data.settings.initialized;
        $('workspace').hidden=!data.settings.initialized;
        $('new-record').disabled=!data.settings.initialized;
        if(!data.settings.initialized) {
            putForm($('setup-form'),{tracking_start_date:data.settings.today}); return false;
        }
        if(!state.date) state.date=data.settings.today;
        const recordCategory=$('record-category').value, filterCategory=$('filter-category').value;
        $('record-category').innerHTML=categoryOptions(recordCategory);
        $('filter-category').innerHTML=categoryOptions(filterCategory,true);
        const recordTags=selectedTags($('record-tags')), filterTags=selectedTags($('filter-tags'));
        $('record-tags').innerHTML=tagBoxes(recordTags); $('filter-tags').innerHTML=tagBoxes(filterTags,true);
        const quick=$('quick-select').value;
        $('quick-select').innerHTML='<option value="">选择模板预填</option>'+state.settings.preferences.quick_entries.map((q,i)=>`<option value="${i}">${esc(q.name)}</option>`).join('');
        $('quick-select').value=quick;
        $('payment-methods').innerHTML=state.settings.preferences.payment_methods.map(p=>`<option value="${esc(p)}"></option>`).join('');
        $('record-form').elements.business_date.max=data.settings.today;
        return true;
    }
    async function refresh() {
        const token=++state.refresh; clearError();
        if(!await bootstrap() || token!==state.refresh) return;
        const [dashboard,templates,recent]=await Promise.all([request('/dashboard'+query({date:state.date})),request('/bill-templates'),request('/records'+query({date:state.date,page_size:8}))]);
        if(token!==state.refresh) return;
        state.dashboard=dashboard; state.templates=templates; state.recentRecords=recent.records;
        state.records.clear();
        $('period-label').textContent=`${dashboard.start} — ${dashboard.end}`; $('period-date').value=state.date;
        renderDashboard(); renderDictionaries(); renderSettings(); renderBudget(); renderBills();
        $('recent-records').innerHTML=recordTable(recent.records,dashboard.daily);
        const billSelection=$('record-bill').value;
        $('record-bill').innerHTML='<option value="">不关联账单</option>'+dashboard.bills.filter(b=>b.status==='open'||b.id===billSelection).map(b=>`<option value="${b.id}">${esc(b.details.name)} · ${b.due_date}</option>`).join('');
        if(billSelection&&!Array.from($('record-bill').options).some(o=>o.value===billSelection)) {
            const option=document.createElement('option'); option.value=billSelection; option.textContent='保留原账单关联'; $('record-bill').appendChild(option);
        }
        $('record-bill').value=billSelection;
        if(state.pending) $('record-form').querySelectorAll('input,select,textarea').forEach(e=>e.disabled=true);
        if(state.tab==='records') await loadRecords();
    }
    function progress(status) { return `<div class="ex-progress ${esc(status.level)}"><span style="width:${Math.min(100,Math.max(0,status.percent||0))}%"></span></div>`; }
    function renderDashboard() {
        const d=state.dashboard, s=d.summary, prefs=state.settings.preferences;
        $('coverage').textContent=`基于已录入记录 · 已核对 ${d.checked_days}/${d.elapsed_days} 天 · 未分类 ${d.unclassified_count} 笔`+(state.settings.tracking_start_date>d.start?' · 本周期启用前的记录尚未补齐':'');
        const cards=[['本周期总预算',d.budget,'快照不随未来设置改变'],['本期净支出',s.net,`流出 ${fmt(s.gross)} / 回款 ${fmt(s.refunds)}`],
            ['预算剩余',d.remaining,d.remaining?.startsWith('-')?'已实际超支':'不等于银行卡余额'],['扣除待付后可安排',d.available,d.current?`待付 ${fmt(d.outstanding)} · 日均 ${fmt(d.daily_allowance)}`:'历史周期不展示实时可安排额度']];
        $('metrics').innerHTML=cards.map((c,i)=>`<div class="ex-metric ${i===3?'featured':''}"><div class="caption">${c[0]}</div><strong>${money(c[1])}</strong><small class="${i===1||i===3?'ex-money':''}">${esc(c[2])}</small></div>`).join('');
        const alerts=[];
        if(s.net_minor<0) alerts.push('本期回款高于支出；负净额不代表需要把额度花完。');
        if(d.available?.startsWith('-')) alerts.push(`扣除待付账单后存在资金安排缺口 ${fmt(d.available.slice(1))}。`);
        if(d.fixed_over_budget) alerts.push('固定安排超过预算，非账单周预算池为零。');
        if(d.pace_fast) alerts.push('非账单支出节奏偏快，请结合待付安排和记录完整性核对。');
        const near=d.bills.filter(b=>b.status==='open' && b.due_date<=dateAdd(d.today,prefs.remind_days));
        if(near.length) alerts.push(`${near.length} 项账单即将到期或逾期，尚未确认支付。`);
        const time=new Intl.DateTimeFormat('en-GB',{timeZone:state.settings.timezone,hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date());
        if(prefs.check_enabled && d.current && time>=prefs.check_time && d.daily.some(x=>x.date===d.today&&!x.confirmed)) alerts.push('今天尚未核对记录，请确认是否已经记全。');
        const week=d.weeks.find(w=>w.current);
        $('risk').innerHTML=`<div class="ex-section-head"><strong>${esc(d.budget_status.label)}${d.budget_status.percent==null?'':` · ${d.budget_status.percent}%`}</strong><span class="muted">周期时间已过 ${d.time_percent}%</span></div>${progress(d.budget_status)}${week?`<p class="ex-money">本周期内本周：${week.start}—${dateAdd(week.end,-1)} · ${fmt(week.actual)} / ${fmt(week.budget)} · ${esc(week.status.label)}</p>`:''}${alerts.map(a=>`<p class="ex-money">${esc(a)}</p>`).join('')}${d.needs_open?button('open-period','','准备本周期预算快照（不记支出）'):''}`;
        const comparison=d.comparison;
        $('review').innerHTML=[`最大分类：${esc(d.categories[0]?.name || '暂无')}`,`最大单笔：${money(d.largest?.amount)}`,
            `≤ ¥50 小额支出累计：${money(d.small_expenses.gross)}`,`期末粗略预测：${d.forecast==null?'记录尚不完整或不足 7 天':money(d.forecast)}`,
            `上期同进度：${comparison.previous_count && comparison.previous!=='0.00'?money(comparison.previous):'无可比基期'}`,
            `本期 / 上期日均：${money(comparison.current_daily)} / ${money(comparison.previous_daily)}`].map(x=>`<div>${x}</div>`).join('');
        $('category-summary').innerHTML=table(['分类','支出流出','实际回款','净额','查看'],d.categories.map(c=>row([esc(c.name),money(minor(c.gross_minor)),money(minor(c.gross_minor-c.net_minor)),money(minor(c.net_minor)),button('filter-category',c.id,'明细')])));
        $('tag-ranking').innerHTML=table(['标签','支出流出','净支出','笔数','查看'],d.tags.map(t=>row([esc(t.name),money(minor(t.gross_minor)),money(minor(t.net_minor)),t.count,button('filter-tag',t.id,'查看')])));
        $('daily-checks').innerHTML=d.daily.filter(x=>!x.future).map(x=>`<button type="button" class="${x.confirmed?'confirmed':''}" data-action="check-day" data-id="${x.date}">${x.date.slice(5)}<br>${x.confirmed?(x.count?'已记全':'已确认零支出'):(x.count?'待核对':'无记录 · 待核对')}</button>`).join('');
        renderCharts();
    }
    function chart(id, options, click) {
        if(!window.echarts) { $(id).textContent='图表组件未加载，仍可查看明细和金额汇总。'; return; }
        const c=state.charts[id] || (state.charts[id]=echarts.init($(id)));
        c.setOption({animation:false, color:['#3a7564','#b98267','#7e9fb0'], tooltip:{trigger:'axis',renderMode:'richText'}, ...options},true);
        c.off('click'); if(click) c.on('click',click); c.resize();
    }
    function dailyChart(id,data) {
        const days=data.daily;
        chart(id,{legend:{data:['支出流出','实际回款','7日支出均线']},grid:{left:60,right:25,top:45,bottom:40},
            xAxis:{type:'category',data:days.map(x=>x.date),axisLabel:{formatter:(v,i)=>v.slice(5)+(!days[i].confirmed&&!days[i].future?' ?':'')}},yAxis:{type:'value',name:'元'},
            series:[{name:'支出流出',type:'bar',data:days.map(x=>x.future?null:x.gross_minor/100)},
                {name:'实际回款',type:'bar',data:days.map(x=>x.future?null:-x.refunds_minor/100)},
                {name:'7日支出均线',type:'line',symbol:'none',data:days.map(x=>x.future?null:x.average_minor/100)}]},p=>filterDate(days[p.dataIndex]?.date));
    }
    function renderCharts() {
        const d=state.dashboard, rules=d.period?.rules;
        const pool=rules&&d.period.budget_minor!=null?Math.max(d.period.budget_minor-rules.fixed_minor,0):null;
        let reference=0;
        const referenceValues=d.daily.map((x,i)=>{
            if(pool==null) return null;
            reference+=Math.floor(pool/d.daily.length)+(i<pool%d.daily.length?1:0);
            reference+=(rules.bill_baseline||[]).filter(b=>b.due_date===x.date).reduce((n,b)=>n+b.amount_minor,0);
            return reference/100;
        });
        chart('cumulative-chart',{legend:{data:['累计净支出','预算上限','参考节奏']},grid:{left:60,right:20,top:50,bottom:35},
            xAxis:{type:'category',data:d.daily.map(x=>x.date),axisLabel:{formatter:v=>v.slice(5)}},yAxis:{type:'value',name:'元'},series:[
                {name:'累计净支出',type:'line',data:d.daily.map(x=>x.cumulative_minor==null?null:x.cumulative_minor/100),symbolSize:4},
                {name:'预算上限',type:'line',data:d.daily.map(()=>d.budget==null?null:Number(d.budget)),symbol:'none',lineStyle:{type:'dashed'}},
                {name:'参考节奏',type:'line',data:referenceValues,symbol:'none',lineStyle:{type:'dotted'}}]},p=>filterDate(d.daily[p.dataIndex]?.date));
        categoryChart(); refreshDailyChart().catch(showError);
    }
    let last30=false, dailyChartToken=0;
    async function refreshDailyChart() {
        const token=++dailyChartToken, recent=last30;
        const data=recent?await request('/charts'+query({start:dateAdd(state.settings.today,-29),end:state.settings.today})):state.dashboard;
        if(token!==dailyChartToken) return;
        dailyChart('daily-chart',data);
        $('daily-title').textContent=recent?'近 30 天 · 每日支出与回款':'本周期 · 每日支出与回款';
        $('daily-range').textContent=recent?'本周期':'近 30 天';
    }
    function categoryChart(parent=null,expanded=false) {
        const d=state.dashboard;
        let values=(parent?d.subcategories.filter(c=>c.parent_id===parent):d.categories).filter(c=>c.gross_minor>0);
        if(!expanded&&values.length>6) values=[...values.slice(0,5),{id:'__more',name:'其他（展开）',gross_minor:values.slice(5).reduce((n,c)=>n+c.gross_minor,0)}];
        $('category-title').textContent=(parent?catName(parent):'分类占比')+' · 未扣回款'; $('category-back').hidden=!parent&&!expanded;
        chart('category-chart',{tooltip:{trigger:'item',renderMode:'richText'},graphic:values.length?[]:[{type:'text',left:'center',top:'middle',style:{text:'暂无支出流出',fill:'#98a198'}}],series:[{type:'pie',radius:['45%','69%'],avoidLabelOverlap:true,label:{formatter:'{b}'},data:values.map(c=>({name:c.name,value:c.gross_minor/100,id:c.id}))}]},p=>{
            if(p.data.id==='__more') return categoryChart(parent,true);
            if(!parent&&d.subcategories.some(c=>c.parent_id===p.data.id)) return categoryChart(p.data.id);
            $('filter-category').value=p.data.id; state.page=1; switchTab('records');
        });
    }
    function recordTable(records,daily=[]) {
        records.forEach(r=>state.records.set(r.id,r));
        const rows=[]; let last='';
        for(const r of records) {
            if(last!==r.business_date) {
                last=r.business_date; const day=daily.find(d=>d.date===last);
                rows.push(`<tr class="ex-day-row"><td colspan="11">${esc(last)}${day?` · 所选范围当日流出 ${money(minor(day.gross_minor))} / 回款 ${money(minor(day.refunds_minor))} / 净额 ${money(minor(day.net_minor))} · ${day.count} 笔 · ${day.confirmed?'已核对':'待核对'}`:' · 此处仅展示当日部分记录'}</td></tr>`);
            }
            const operations=button('detail',r.id,'详情')+(r.deleted_at?button('restore',r.id,'恢复'):button('edit',r.id,'编辑')+(r.kind==='expense'?button('refund',r.id,'登记回款'):'')+button('void',r.id,'作废'));
            const titleCell=`<span class="ex-record-title${r.title?'':' empty'}">${esc(r.title||'未填写标题')}</span>`;
            rows.push(row([esc(r.business_date),money(r.amount),titleCell,esc(kindName(r.kind)),esc(catName(r.category_id)),tagChips(r.tag_ids),esc(r.merchant),esc(r.payment_method),esc(r.note),r.bill_occurrence_id?'账单关联':'—',operations]));
        }
        return table(['日期','金额','标题','类型','分类','标签','商户','支付方式','备注','固定计划','操作'],rows);
    }
    function filterArgs() {
        const args=fields($('filter-form')); args.tags=selectedTags($('filter-tags')).join(',');
        if(!args.start&&!args.end) args.date=state.date;
        return {...args,page:state.page,page_size:$('page-size').value};
    }
    async function loadRecords() {
        const args=filterArgs(), token=JSON.stringify(args); state.filterToken=token;
        const [listing,analytics]=await Promise.all([request('/records'+query(args)),request('/charts'+query(args))]);
        if(token!==state.filterToken) return;
        state.records.clear(); (state.recentRecords||[]).forEach(r=>state.records.set(r.id,r));
        $('records-table').innerHTML=recordTable(listing.records,analytics.daily);
        $('filter-summary').innerHTML=`筛选结果 · ${listing.total} 笔 · 净额 ${money(listing.summary.net)} · 本页小计 ${money(listing.page_net)}`;
        const max=Math.max(1,Math.ceil(listing.total/listing.page_size));
        $('page-label').textContent=`第 ${listing.page} / ${max} 页`; $('previous-page').disabled=state.page<=1; $('next-page').disabled=state.page>=max;
        const conditions=[args.category_id?catName(args.category_id):'',args.kind?kindName(args.kind):'',args.keyword?`关键词：${args.keyword}`:'',args.tags?'标签已筛选':'',args.trash==='1'?'回收站':''].filter(Boolean);
        $('filtered-title').textContent=`${analytics.start}—${analytics.end} · ${conditions.join(' / ')||'全部记录'}（不改变全周期总览）`;
        dailyChart('filtered-chart',analytics);
    }
    function switchTab(tab) {
        state.tab=tab;
        document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==tab);
        document.querySelectorAll('[data-tab]').forEach(el=>{ const active=el.dataset.tab===tab; el.classList.toggle('active',active); el.setAttribute('aria-selected',String(active)); });
        Object.values(state.charts).forEach(c=>c.resize());
        if(tab==='records') loadRecords().catch(showError);
    }
    function filterDate(date) { if(!date) return; putForm($('filter-form'),{start:date,end:date}); state.page=1; switchTab('records'); }
    function renderDictionaries() {
        $('categories-table').innerHTML=table(['分类','状态','排序','操作'],state.categories.map(c=>row([esc(catName(c.id)),c.is_active?'启用':'停用',c.sort_order,c.is_system?'系统兜底':button('edit-category',c.id,'编辑')+button('delete-category',c.id,'删除 / 停用')])));
        $('tags-table').innerHTML=table(['标签','状态','常用','排序','操作'],state.tags.map(t=>row([esc(t.name),t.is_active?'启用':'停用',t.is_favorite?'是':'否',t.sort_order,button('edit-tag',t.id,'编辑')+button('delete-tag',t.id,'删除 / 停用')])));
        $('quick-table').innerHTML=table(['名称','预填金额','分类','操作'],state.settings.preferences.quick_entries.map((q,i)=>row([esc(q.name),money(q.amount),esc(catName(q.category_id)),button('edit-quick',i,'编辑')+button('delete-quick',i,'删除')])));
    }
    function renderSettings() {
        if(state.dirty.has('settings-form')) return;
        const form=$('settings-form'), prefs=state.settings.preferences;
        putForm(form,{...prefs,...state.settings,budget:minor(prefs.budget_minor),payment_methods:prefs.payment_methods.join('\n')});
        if(prefs.budget_minor==null) form.elements.budget.value='';
        form.dataset.version=state.settings.version;
        const pending=state.settings.rules.find(r=>r.effective_from>state.settings.today);
        form.elements.transition_budget.value=pending?.transition_budget_minor==null?'':minor(pending.transition_budget_minor);
        $('future-category-budgets').innerHTML=state.categories.map(c=>{
            const value=(prefs.category_budgets||[]).find(x=>x.category_id===c.id)?.budget_minor;
            return `<label>${esc(catName(c.id))}<input data-category="${c.id}" value="${value==null?'':minor(value)}" placeholder="未设置" class="ex-money"></label>`;
        }).join('');
        transitionPreview();
    }
    function transitionPreview() {
        const form=$('settings-form'), day=Number(form.elements.cycle_day.value), d=state.dashboard;
        if(!d||day<1||day>31) return;
        const end=state.settings.rules.find(r=>r.effective_from>state.settings.today)?.effective_from || (d.current?dateAdd(d.end,1):null);
        if(!end) { $('transition-preview').textContent='请回到当前周期预览新规则生效日期。'; return; }
        const nextAnchor=(year,month)=>new Date(Date.UTC(year,month,Math.min(day,new Date(Date.UTC(year,month+1,0)).getUTCDate()))).toISOString().slice(0,10);
        const date=new Date(end+'T00:00:00Z'); let next=nextAnchor(date.getUTCFullYear(),date.getUTCMonth());
        if(next<=end) next=nextAnchor(date.getUTCFullYear(),date.getUTCMonth()+1);
        $('transition-preview').textContent=`下一规则从 ${end} 生效；首段 ${end}—${dateAdd(next,-1)}。若更改起始日产生过渡段，使用上方明确填写的过渡预算（空表示未设置）。`;
    }
    function renderBudget() {
        if(state.dirty.has('budget-form')) return;
        const d=state.dashboard, p=d.period, form=$('budget-form'); form.hidden=!p;
        $('budget-history').disabled=!p;
        $('budget-empty').textContent=p?'':'尚无预算快照，请在总览点击准备本周期。';
        if(!p) return;
        putForm(form,{...p.rules,budget:p.budget,reason:''}); form.dataset.version=p.version; form.dataset.id=p.id;
        $('week-pool').innerHTML=`计划基准 ${money(minor(p.rules.fixed_minor))} · 未分配机动额度 ${money(d.unallocated_week)} · 全部周段均属于本周期`;
        $('week-budgets').innerHTML=table(['周段','净支出','分配金额（元）','预算状态','上周相同日段'],d.weeks.map((w,i)=>row([`${w.start}—${dateAdd(w.end,-1)}${w.current?'（本周）':''}`,money(w.actual),`<input class="ex-money" data-week="${i}" aria-label="周段 ${w.start} 预算" value="${esc(w.budget??'')}" ${p.rules.week_mode==='auto'?'disabled':''}>`,esc(w.status.label),w.previous==null?'无可比基期':money(w.previous)+` · ${w.comparison_days} 天`])));
        $('category-budgets').innerHTML=state.categories.map(c=>{
            const item=d.category_budgets.find(x=>x.category_id===c.id);
            return `<label>${esc(catName(c.id))}${item?` · ${esc(item.status.label)} · 已用 ${money(item.actual)}${progress(item.status)}`:''}<input data-category="${c.id}" value="${esc(item?.budget??'')}" placeholder="未设置" class="ex-money"></label>`;
        }).join('');
    }
    function budgetDraft() {
        const form=$('budget-form'), d=state.dashboard, p=d?.period;
        if(!p) return;
        const inputs=Array.from(form.querySelectorAll('[data-week]')), auto=form.elements.week_mode.value==='auto';
        inputs.forEach(i=>i.disabled=auto);
        try {
            const budget=form.elements.budget.value.trim(), fixed=BigInt(p.rules.fixed_minor);
            const pool=budget===''?null:(intMoney(budget)>fixed?intMoney(budget)-fixed:0n);
            const days=BigInt(d.daily.length); let offset=0n;
            if(auto) inputs.forEach((input,index)=>{
                const week=d.weeks[index], length=BigInt(Math.round((new Date(week.end)-new Date(week.start))/86400000));
                let assigned=0n;
                if(pool!=null) for(let day=0n;day<length;day++) assigned+=pool/days+(offset+day<pool%days?1n:0n);
                input.value=pool==null?'':minor(assigned); offset+=length;
            });
            const total=pool==null?null:inputs.reduce((sum,i)=>sum+intMoney(i.value),0n);
            $('week-pool').innerHTML=`草稿预览（尚未保存）：计划基准 ${money(minor(fixed))} · 周预算池 ${money(pool==null?null:minor(pool))} · 未分配 ${money(pool==null?null:minor(pool-total))}`;
        } catch(error) { $('week-pool').textContent='草稿预览：'+error.message; }
    }
    function renderBills() {
        const d=state.dashboard;
        $('bills-table').innerHTML=table(['到期日','名称','预计 / 已付','剩余预留','状态','操作'],d.bills.map(b=>row([b.due_date,esc(b.details.name),money(b.expected)+' / '+money(b.paid),money(b.remaining),b.status==='open'?(b.due_date<d.today?'逾期未确认':'待支付'):b.status==='settled'?(b.paid!==b.expected&&BigInt(b.paid.replace('.',''))>BigInt(b.expected.replace('.',''))?'已结清 · 超出预估':'已结清'):'已跳过',
            b.status==='open'?button('pay-bill',b.id,'登记支付')+button('link-bill',b.id,'关联已有记录')+button('close-bill',b.id,'本次结清')+button('skip-bill',b.id,'跳过')+button('edit-bill',b.id,'改预计'):button('reopen-bill',b.id,'重新开放')])));
        $('templates-table').innerHTML=table(['计划','预计金额','重复','首次到期','状态','操作'],state.templates.map(t=>row([esc(t.name),money(t.amount),({once:'一次性',monthly:'每月',yearly:'每年'})[t.rule.frequency],t.rule.first_date,t.is_active?'启用':'停用',button('edit-template',t.id,'修改未来计划')+(t.is_active?button('delete-template',t.id,'停用'):'')])));
    }
    async function editDictionary(type,id) {
        const isCategory=type==='categories', item=(isCategory?state.categories:state.tags).find(x=>x.id===id);
        const html=`<div class="ex-form">${input('名称','name',item?.name,'text','required maxlength="32"')}${input('颜色','color',item?.color||'#3a7564','color')}${input('排序','sort_order',item?.sort_order||0,'number','min="0" max="10000"')}${isCategory?`<label>上级分类<select name="parent_id" ${item?'disabled':''}>${categoryOptions(item?.parent_id,true,true)}</select></label>`:check('常用标签','is_favorite',item?.is_favorite)}${check('启用','is_active',item?.is_active??true)}</div>`;
        modalForm((item?'编辑':'新建')+(isCategory?'分类':'标签'),html,async form=>{
            const body={...fields(form),is_active:form.elements.is_active.checked,...(!isCategory?{is_favorite:form.elements.is_favorite.checked}:{})};
            if(item) body.version=item.version;
            await request('/'+type+(item?'/'+id:''),item?'PATCH':'POST',body);
        });
    }
    function editQuick(index=null) {
        const entries=state.settings.preferences.quick_entries, item=index==null?null:entries[index], version=state.settings.version;
        modalForm(item?'修改常用模板':'新建常用模板',`<div class="ex-form">${input('模板名称','name',item?.name,'text','required maxlength="32"')}${input('预填金额（可空）','amount',item?.amount)}${categoryField(item?.category_id)}<label>备注<textarea name="note" maxlength="1000">${esc(item?.note)}</textarea></label><fieldset class="ex-wide"><legend>标签</legend><div class="ex-tags">${tagBoxes(item?.tag_ids)}</div></fieldset></div>`,async form=>{
            const next=entries.slice(), value={...fields(form),tag_ids:selectedTags(form.querySelector('.ex-tags'))};
            if(index==null) next.push(value); else next[index]=value;
            await request('/settings','PATCH',{version,quick_entries:next});
        });
    }
    function editTemplate(id=null) {
        const t=state.templates.find(t=>t.id===id);
        modalForm(t?'修改未来账单计划':'新建账单计划',`<p class="muted">已有实例不被覆盖。编辑模板时，首次到期日必须不早于当前周期结束。</p><div class="ex-form">${input('计划名称','name',t?.name,'text','required maxlength="128"')}${input('预计金额（元）','amount',t?.amount,'text','required')}${categoryField(t?.category_id)}${input('首次到期日期','first_date',t?dateAdd(state.dashboard.end,1):state.settings.today,'date','required')}<label>重复规则<select name="frequency">${['once','monthly','yearly'].map((f,i)=>`<option value="${f}" ${t?.rule.frequency===f?'selected':''}>${['一次性','每月','每年'][i]}</option>`).join('')}</select></label><fieldset class="ex-wide"><legend>标签</legend><div class="ex-tags">${tagBoxes(t?.tag_ids)}</div></fieldset></div>${check('确认本期及未来已准备周期的计划变化时同步重算周预算（保留调整历史）','confirm_reallocate')}`,async form=>{
            const body={...fields(form),tag_ids:selectedTags(form.querySelector('.ex-tags')),confirm_reallocate:form.elements.confirm_reallocate.checked};
            if(t) body.version=t.version;
            await request('/bill-templates'+(t?'/'+id:''),t?'PATCH':'POST',body);
        });
    }
    async function openRecord(record=null, refund=false, bill=null) {
        if(state.pending) throw Error('上次保存结果尚待确认，请先使用原请求重试，避免重复记账。');
        if(state.dirty.has('record-form')&&!await confirmText('保留还是放弃草稿？','打开另一笔会放弃当前未保存内容，确认继续？')) return;
        const form=$('record-form'); form.reset(); state.edit=refund?null:record; state.requestId=uuid(); state.dirty.delete('record-form');
        $('record-category').innerHTML=categoryOptions(record?.category_id||bill?.details.category_id);
        $('record-tags').innerHTML=tagBoxes(record?.tag_ids||bill?.details.tag_ids||[]);
        if(record?.bill_occurrence_id&&!Array.from($('record-bill').options).some(o=>o.value===record.bill_occurrence_id)) {
            const option=document.createElement('option'); option.value=record.bill_occurrence_id; option.textContent='保留原账单关联'; $('record-bill').appendChild(option);
        }
        putForm(form,{business_date:state.settings.today,kind:'expense',...(record?{...record,kind:refund?'refund':record.kind,amount:refund?'':record.amount,business_date:refund?state.settings.today:record.business_date,original_record_id:refund?record.id:record.original_record_id}:{})});
        if(bill) putForm(form,{amount:bill.remaining,category_id:bill.details.category_id,bill_occurrence_id:bill.id,title:bill.details.name,note:bill.details.name});
        $('record-title').textContent=refund?'登记实际回款':record?'编辑记录':bill?'登记账单支付':'记一笔';
        $('record-panel').hidden=false; $('record-advanced').open=Boolean(record||bill); $('record-save-state').textContent='';
        $('record-amount').focus(); $('record-panel').scrollIntoView({block:'start',behavior:'smooth'}); schedulePreview();
    }
    function recordPayload() {
        const form=$('record-form'); return {...fields(form),tag_ids:selectedTags($('record-tags')),settle_bill:form.elements.settle_bill.checked,
            ...(state.edit?{version:state.edit.version}:{client_request_id:state.requestId || (state.requestId=uuid())})};
    }
    let previewTimer;
    function schedulePreview() {
        clearTimeout(previewTimer); const token=++state.preview;
        previewTimer=setTimeout(async()=>{
            if(!$('record-amount').value) { $('record-preview').textContent='输入金额后显示服务端只读预算预览。'; return; }
            try {
                const body=recordPayload(), args={amount:body.amount,kind:body.kind,business_date:body.business_date,
                    bill_occurrence_id:body.bill_occurrence_id,original_record_id:body.original_record_id,settle_bill:body.settle_bill?'1':'0',id:state.edit?.id};
                const result=await request('/preview'+query(args)); if(token!==state.preview) return;
                $('record-preview').innerHTML=result.periods.map(p=>`${p.start}—${p.end}：净支出 ${money(p.before)} → ${money(p.after)}；预算剩余 ${money(p.remaining)}；扣待付后 ${money(p.available)}`).join('<br>')+(result.linked_refund_dates.length?`<br>修改分类或账单归属将同步影响关联回款日期：${result.linked_refund_dates.map(esc).join('、')}`:'');
            } catch(error) { if(token===state.preview) $('record-preview').textContent=error.message; }
        },260);
    }
    async function saveRecord(event) {
        event.preventDefault(); if(state.saving) return; clearError();
        const body=state.pending || recordPayload(), editing=state.edit;
        if(body.kind==='movement'&&!state.pending&&!await confirmText('确认资金划转','这笔记录不占支出预算。确认属于内部划转或已记消费后的还款？')) return;
        state.saving=true; const buttons=$('record-form').querySelectorAll('button'); buttons.forEach(b=>b.disabled=true);
        const oldLevel=state.dashboard.budget_status.level;
        let committed=false;
        try {
            let result;
            try { result=await request('/records'+(editing?'/'+editing.id:''),editing?'PATCH':'POST',body); }
            catch(error) {
                if(error.details?.duplicate_id&&await confirmText('疑似重复记录',error.message+'。仍然保存为另一笔真实消费？')) {
                    body.confirm_duplicate=true; result=await request('/records','POST',body);
                } else throw error;
            }
            committed=true;
            state.pending=null; state.dirty.delete('record-form'); state.edit=null; state.requestId=uuid();
            $('record-form').querySelectorAll('input,select,textarea').forEach(e=>e.disabled=false);
            const next=result.dashboard;
            notice(`已保存 ${kindName(result.record.kind)} ${fmt(result.record.amount)}${next?`；本期净支出 ${fmt(next.summary.net)}，预算剩余 ${fmt(next.remaining)}，可安排 ${fmt(next.available)}`:''}${result.statistics_pending?'；统计待刷新':''}`);
            const ranks=['unset','normal','warning','critical','exhausted','over'];
            if(next&&ranks.indexOf(next.budget_status.level)>Math.max(1,ranks.indexOf(oldLevel))) {
                const key='expense-risk:'+next.start+':'+next.budget_status.level;
                try { if(!sessionStorage.getItem(key)) { sessionStorage.setItem(key,'1'); MDialog.show({title:'预算风险提高',message:esc(next.budget_status.label)+' · '+esc(fmt(next.remaining)),type:'warning'}); } } catch(_) { /* 禁用浏览器存储时仍保留页面风险横幅。 */ }
            }
            if(event.submitter?.value==='continue') {
                putForm($('record-form'),{amount:'',title:'',note:'',merchant:'',original_record_id:'',unlinked_refund_reason:'',bill_occurrence_id:'',kind:'expense',settle_bill:false});
                $('record-title').textContent='继续记一笔'; $('record-amount').focus();
            } else { $('record-panel').hidden=true; $('record-form').reset(); }
            $('record-save-state').textContent='';
            await refresh().catch(error=>{ notice('已保存，统计待刷新'); showError(error); });
        } catch(error) {
            if(committed) { notice('已保存，页面更新失败，请刷新查看，勿重复新增。'); showError(error); return; }
            if(!error.status||error.status>=500) {
                if(!editing) {
                    state.pending=body;
                    $('record-form').querySelectorAll('input,select,textarea').forEach(e=>e.disabled=true);
                    $('record-save-state').textContent='结果未确认：草稿已锁定；点击保存以原标识安全重试。请勿刷新页面。';
                } else {
                    $('record-save-state').textContent='编辑结果未确认：请刷新列表并从详情核对当前版本；旧版本重试不会覆盖已保存内容。';
                }
            }
            showError(error);
        } finally { state.saving=false; buttons.forEach(b=>b.disabled=false); }
    }
    async function recordDetail(id) {
        const r=await request('/records/'+id);
        const history=r.history.map(h=>`<details><summary>${esc(auditTime(h.created_at))} · ${esc(h.action)} · ${esc(h.reason)}</summary><pre class="ex-money">${esc(h.before_json)}</pre><pre class="ex-money">${esc(h.after_json)}</pre></details>`).join('');
        MDialog.show({title:'记录详情与变更历史',width:'820px',message:`<div class="ex-dialog ${state.private?'ex-private':''}"><p class="ex-record-title${r.title?'':' empty'}">标题：${esc(r.title||'未填写标题')}</p><p>${esc(r.business_date)} · ${esc(kindName(r.kind))} ${money(r.amount)} · ${esc(catName(r.category_id))}</p><p>${esc(r.merchant)} · ${esc(r.note)}</p><p>记录 ID：${esc(r.id)} · 当前版本 ${r.version}</p><p>累计有效回款 ${money(r.refund_total)}</p>${r.refund_records.map(x=>`<p>${esc(x.business_date)} · ${money(x.amount)} · ${x.deleted_at?'已作废':'有效'}</p>`).join('')}<h3>审计时间：${esc(state.settings.timezone)}（原始快照为 UTC）</h3>${history}</div>`});
    }
    async function mutateRecord(action,id) {
        const r=state.records.get(id), restoring=action==='restore';
        if(!await confirmText(restoring?'恢复记录':'作废记录',restoring?'恢复后重新计入统计并使该日核对失效。':'作废会改变预算、使该日核对失效；关联账单将重新核对待付状态。可在回收站恢复。')) return;
        const result=await request('/records/'+id+(restoring?'/restore':''),restoring?'POST':'DELETE',{version:r.version});
        notice(restoring?'记录已恢复':'记录已作废，可立即撤销或到回收站恢复。');
        if(!restoring) { const b=document.createElement('button'); b.textContent='立即撤销'; b.onclick=()=>request('/records/'+id+'/restore','POST',{version:result.record.version}).then(refresh).then(()=>notice('已撤销作废')).catch(showError); $('message').appendChild(b); }
        await refresh();
    }
    async function billAction(action,id) {
        const b=state.dashboard.bills.find(b=>b.id===id);
        if(action==='pay') return openRecord(null,false,b);
        let content=`<p>${esc(b.details.name)} · 到期 ${b.due_date} · 预计 ${money(b.expected)} / 已付 ${money(b.paid)}</p>`;
        if(action==='link') content+=`<div class="ex-form">${input('已有支出记录 ID','record_id','','text','required')}${input('该记录当前版本（详情可查看）','record_version','1','number','min="1" required')}</div>`;
        else if(action==='edit') content+=`<div class="ex-form">${input('本次名称','name',b.details.name,'text','required')}${input('本次预计金额','amount',b.expected,'text','required')}</div>`;
        else content+=`<p>${({close:'确认本次已结清，释放剩余预留，不生成支出。',skip:'只跳过本次，释放预留，不删除已支付记录。',reopen:'重新开放待付状态，不创建支付记录。'})[action]}</p>`;
        content+=check('确认影响本期及未来已准备周期的周预算时重新分配并保留调整历史','confirm_reallocate');
        modalForm('处理账单计划',content,async form=>{
            await request('/bills/'+id+'/'+(action==='link'?'link-record':action),'POST',{...fields(form),version:b.version,confirm_reallocate:form.elements.confirm_reallocate.checked});
        });
    }
    const actions={
        'open-period':async()=>{ await request('/periods/open','POST',{date:state.date}); await refresh(); },
        'detail':recordDetail,'edit':id=>openRecord(state.records.get(id)), 'refund':id=>openRecord(state.records.get(id),true),
        'void':id=>mutateRecord('void',id),'restore':id=>mutateRecord('restore',id),
        'edit-category':id=>editDictionary('categories',id),'edit-tag':id=>editDictionary('tags',id),
        'edit-quick':id=>editQuick(Number(id)),'edit-template':editTemplate,
        'filter-category':id=>{ $('filter-category').value=id; state.page=1; switchTab('records'); },
        'filter-tag':id=>{ $('filter-tags').innerHTML=tagBoxes([id],true); state.page=1; switchTab('records'); },
        'check-day':async date=>{ const day=state.dashboard.daily.find(d=>d.date===date); if(await confirmText('每日核对',day.confirmed?`取消 ${date} 的核对确认？`:`确认 ${date} ${day.count?'已经全部记好':'没有支出'}？`)) { await request('/daily-checks','POST',{business_date:date,confirmed:!day.confirmed}); await refresh(); } }
    };
    for(const type of ['category','tag']) actions['delete-'+type]=async id=>{
        const collection=type==='category'?'categories':'tags', item=state[collection].find(x=>x.id===id);
        if(await confirmText('删除或停用',`确认处理“${item.name}”？有引用时只停用，历史关联保留。`)) {
            const result=await request('/'+collection+'/'+id,'DELETE',{version:item.version}); notice(result.message||'已删除'); await refresh();
        }
    };
    actions['delete-quick']=async index=>{ if(await confirmText('删除模板','只删除预填模板，不影响已记记录。')) { const entries=state.settings.preferences.quick_entries.filter((_,i)=>i!==Number(index)); await request('/settings','PATCH',{version:state.settings.version,quick_entries:entries}); await refresh(); } };
    actions['delete-template']=async id=>{ const t=state.templates.find(x=>x.id===id); if(await confirmText('停用未来计划','已生成的待付实例保留，需要逐次跳过或结清。')) { await request('/bill-templates/'+id,'DELETE',{version:t.version}); await refresh(); } };
    for(const action of ['pay','link','close','skip','reopen','edit']) actions[action+'-bill']=id=>billAction(action,id);
    $('expense-page').addEventListener('click',e=>{ const b=e.target.closest('[data-action]'); if(b&&actions[b.dataset.action]) Promise.resolve(actions[b.dataset.action](b.dataset.id)).catch(showError); });
    document.querySelectorAll('[data-tab],[data-go]').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab||b.dataset.go)));
    for(const id of ['record-form','budget-form','settings-form','setup-form']) {
        $(id).addEventListener('input',()=>state.dirty.add(id)); $(id).addEventListener('change',()=>state.dirty.add(id));
    }
    on('setup-form','submit',async e=>{ e.preventDefault(); const b=e.submitter; b.disabled=true; try { await request('/setup','POST',fields(e.target)); state.dirty.delete('setup-form'); await refresh(); await openRecord(); } finally { b.disabled=false; } });
    on('new-record','click',()=>openRecord());
    on('cancel-record','click',async()=>{ if(state.pending) throw Error('结果未确认，请先重试原保存请求。'); if(!state.dirty.has('record-form')||await confirmText('放弃草稿？','未保存内容将丢失，确认收起？')) { $('record-panel').hidden=true; state.dirty.delete('record-form'); } });
    on('record-form','submit',saveRecord); on('record-form','input',schedulePreview); on('record-form','change',schedulePreview);
    on('quick-select','change',()=>{ const q=state.settings.preferences.quick_entries[$('quick-select').value]; if(!q) return; putForm($('record-form'),q); $('record-tags').innerHTML=tagBoxes(q.tag_ids); state.dirty.add('record-form'); schedulePreview(); });
    on('quick-new-tag','click',()=>editDictionary('tags')); on('new-tag','click',()=>editDictionary('tags')); on('new-category','click',()=>editDictionary('categories'));
    on('new-template','click',()=>editTemplate()); on('new-quick','click',()=>editQuick());
    on('filter-form','submit',e=>{ e.preventDefault(); state.page=1; return loadRecords(); });
    on('clear-filter','click',()=>{ $('filter-form').reset(); $('filter-tags').innerHTML=tagBoxes([],true); state.page=1; return loadRecords(); });
    on('previous-page','click',()=>{ state.page--; return loadRecords(); }); on('next-page','click',()=>{ state.page++; return loadRecords(); }); on('page-size','change',()=>{state.page=1;return loadRecords();});
    on('refresh','click',refresh);
    async function selectPeriod(date) {
        if(state.dirty.has('budget-form')&&!await confirmText('未保存预算','切换周期将放弃本周期预算草稿，确认继续？')) return;
        state.dirty.delete('budget-form'); state.date=date; state.page=1; await refresh();
    }
    on('previous-period','click',()=>selectPeriod(dateAdd(state.dashboard.start,-1)));
    on('next-period','click',()=>selectPeriod(dateAdd(state.dashboard.end,1)));
    on('current-period','click',()=>selectPeriod(state.settings.today)); on('period-date','change',()=>selectPeriod($('period-date').value));
    on('category-back','click',()=>categoryChart());
    on('daily-range','click',()=>{ last30=!last30; return refreshDailyChart(); });
    on('privacy','click',()=>{ state.private=!state.private; $('expense-page').classList.toggle('ex-private',state.private); $('privacy').textContent=state.private?'显示金额':'隐藏金额'; });
    on('export','click',async()=>{
        const response=await fetch('/expense/api/export'+query(filterArgs()),{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store'});
        if(!response.ok) throw Error('导出失败，请确认登录状态与筛选条件');
        const url=URL.createObjectURL(await response.blob()), a=document.createElement('a'); a.href=url; a.download='expenses.csv'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),10000);
    });
    on('budget-form','change',budgetDraft); on('budget-form','input',budgetDraft);
    on('budget-form','submit',async e=>{
        e.preventDefault(); const form=e.target, body={...fields(form),version:Number(form.dataset.version)};
        body.category_budgets=Array.from($('category-budgets').querySelectorAll('input')).filter(i=>i.value.trim()).map(i=>({category_id:i.dataset.category,budget:i.value.trim()}));
        if(body.week_mode==='manual') body.weeks=Array.from(form.querySelectorAll('[data-week]'),i=>({budget:i.value}));
        const weekPreview=Array.from(form.querySelectorAll('[data-week]'),(input,i)=>`${state.dashboard.weeks[i].start}：${fmt(state.dashboard.weeks[i].budget)} → ${fmt(input.value||null)}`).join('；');
        if(!await confirmText('确认预算调整',`调整 ${state.dashboard.start}—${state.dashboard.end}，总预算 ${fmt(body.budget||null)}，周分配方式 ${body.week_mode==='auto'?'自动':'手动'}。${weekPreview}。月、周、分类预警将一起更新；原分配保留审计历史。`)) return;
        const button=e.submitter; button.disabled=true;
        try { await request('/periods/'+form.dataset.id+'/budget','PATCH',body); state.dirty.delete('budget-form'); notice('本周期预算已调整'); await refresh(); } finally { button.disabled=false; }
    });
    on('budget-history','click',async()=>{ const p=await request('/periods/'+state.dashboard.period.id); MDialog.show({title:'预算调整历史 · '+esc(state.settings.timezone),width:'820px',message:`<div class="ex-dialog ${state.private?'ex-private':''}">${p.history.map(h=>`<details><summary>${esc(auditTime(h.created_at))} · ${esc(h.action)} · ${esc(h.reason)}</summary><pre class="ex-money">${esc(h.before_json)}</pre><pre class="ex-money">${esc(h.after_json)}</pre></details>`).join('')}</div>`}); });
    on('settings-form','input',transitionPreview);
    on('settings-form','submit',async e=>{
        e.preventDefault(); const form=e.target, body={...fields(form),version:Number(form.dataset.version),check_enabled:form.elements.check_enabled.checked,payment_methods:form.elements.payment_methods.value.split('\n').map(s=>s.trim()).filter(Boolean)};
        body.category_budgets=Array.from($('future-category-budgets').querySelectorAll('input')).filter(i=>i.value.trim()).map(i=>({category_id:i.dataset.category,budget:i.value.trim()}));
        if(!await confirmText('确认设置与未来规则',$('transition-preview').textContent+' 历史快照和业务日期不会改变。')) return;
        const b=e.submitter; b.disabled=true; try { await request('/settings','PATCH',body); state.dirty.delete('settings-form'); notice('设置已保存；当前周期预算不变'); await refresh(); } finally { b.disabled=false; }
    });
    window.addEventListener('beforeunload',e=>{ if(state.dirty.size||state.pending) {e.preventDefault();e.returnValue='';} });
    window.addEventListener('resize',()=>Object.values(state.charts).forEach(c=>c.resize()));
    let tickBusy=false;
    async function tick() {
        if(document.hidden||tickBusy||state.saving||!state.settings?.initialized) return;
        tickBusy=true;
        try {
            const latest=await request('/settings');
            if(latest.today!==state.settings.today&&state.date===state.settings.today) {
                if(state.dirty.has('budget-form')) notice('账本日期已改变；预算草稿未保存，暂保留原周期。保存或放弃草稿后可点击“本期”切换。');
                else { state.date=latest.today; notice('账本日期已改变，已切换今天；如进入新周期，请明确准备预算快照。未提交草稿日期保持不变。'); }
            }
            await refresh();
        } catch(error) { showError(error); } finally { tickBusy=false; }
    }
    setInterval(tick,60000); document.addEventListener('visibilitychange',()=>{if(!document.hidden) tick();});
    refresh().catch(showError);
})();
