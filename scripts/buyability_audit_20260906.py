#!/usr/bin/env python3
"""One-off research screen, NOT an order generator or final recommendation.
Uses every archived row; ignores old model scores, analyst counts and final20.
Missing source fields stay missing. Quotes and prices require explicit dates.
No schedule, no brokerage access, no credentials written to outputs.
"""
import csv, json, math, re, time, hashlib, statistics, urllib.parse, urllib.request
from pathlib import Path
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'results/2026H1/universe_four_model_scores.csv'
OUT=ROOT/'results/buyability_20260906'
OUT.mkdir(parents=True,exist_ok=True)
ASOF='2026-09-04'
FINANCE=('银行','保险','证券','多元金融')
CYCLICAL=('煤炭','油气','石油','工业金属','小金属','贵金属','化学原料','化学制品','化学纤维','钢铁','水泥','玻璃','养殖','航运','水上运输','造纸','农产品','饲料')

def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError): return None

def div(a,b):
    return a/b if a is not None and b is not None and abs(b)>1e-9 else None

def grow(a,b):
    return (a/b-1)*100 if a is not None and b is not None and b>1e6 else None

def fetch(url,encoding='utf-8',attempts=2):
    error=''
    for i in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://gu.qq.com/'})
            with urllib.request.urlopen(req,timeout=16) as f: return f.read().decode(encoding,errors='replace')
        except Exception as e:
            error=str(e)
            if i+1<attempts: time.sleep(0.4)
    raise RuntimeError(error)

def savejson(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')

def savecsv(name,rows):
    if not rows: return
    cols=list(dict.fromkeys(k for r in rows for k in r))
    with (OUT/name).open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rows)

def symbol(code):
    if code.startswith(('6','5')):return 'sh'+code
    if code.startswith(('0','3')):return 'sz'+code
    return 'bj'+code

def quote_batch(codes):
    syms=','.join(symbol(c) for c in codes)
    text=fetch('https://qt.gtimg.cn/q='+syms,encoding='gb18030')
    out={}
    for m in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"',text):
        parts=m.group(2).split('~')
        if len(parts)<47:continue
        c=parts[2]
        stamp=parts[30]
        if len(c)!=6 or not stamp[:8].isdigit():continue
        date=f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}'
        out[c]={'quote_name':parts[1],'price':num(parts[3]),'previous_close':num(parts[4]),'open':num(parts[5]),'day_return_pct':num(parts[32]),'high':num(parts[33]),'low':num(parts[34]),'turnover_cny':(num(parts[37]) or 0)*10000,'turnover_pct':num(parts[38]),'vendor_pe':num(parts[39]),'float_cap_100m':num(parts[44]),'market_cap_100m':num(parts[45]),'pb':num(parts[46]),'quote_date':date,'quote_timestamp':stamp,'quote_source':'https://qt.gtimg.cn/q='+m.group(1)}
    return out

def history(row):
    c=row['code'];sym=symbol(c)
    url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+urllib.parse.urlencode({'param':f'{sym},day,2026-01-01,{ASOF},160,qfq'})
    try:
        obj=json.loads(fetch(url));d=obj.get('data',{}).get(sym,{})
        raw=d.get('qfqday') or d.get('day') or []
        raw=[x for x in raw if x[0]<=ASOF]
        if len(raw)<30: raise RuntimeError(f'only {len(raw)} historical rows')
        closes=[num(x[2]) for x in raw];vol=[num(x[5]) for x in raw]
        if not all(x is not None and x>0 for x in closes):raise RuntimeError('invalid close')
        last=closes[-1]
        result={'history_date':raw[-1][0],'history_source':url,'history_rows':len(raw),'hist_close':last,'ma5':statistics.mean(closes[-5:]),'ma20':statistics.mean(closes[-20:]),'ma60':statistics.mean(closes[-60:]) if len(closes)>=60 else None,'return5_pct':(last/closes[-6]-1)*100,'return20_pct':(last/closes[-21]-1)*100,'return60_pct':(last/closes[-61]-1)*100 if len(closes)>=61 else None,'volume_to20':div(vol[-1],statistics.mean(vol[-20:])) if all(x is not None for x in vol[-20:]) else None,'high20_close':max(closes[-20:]),'low10_close':min(closes[-10:]),'history_error':''}
        result['quote_history_match']=bool(row.get('price') and abs(last/row['price']-1)<0.015)
        return c,result,raw
    except Exception as e:return c,{'history_error':str(e),'quote_history_match':False},[]

def notices(code):
    params={'sr':-1,'page_size':60,'page_index':1,'ann_type':'A','client_source':'web','stock_list':code,'begin_time':'2026-08-25','end_time':'2026-09-06'}
    url='https://np-anotice-stock.eastmoney.com/api/security/ann?'+urllib.parse.urlencode(params)
    try:
        p=json.loads(fetch(url));items=p.get('data',{}).get('list',[])
        clean=[]
        for a in items:
            dt=str(a.get('notice_date',''))[:10]
            if not '2026-08-25'<=dt<='2026-09-06':continue
            clean.append({'date':dt,'title':a.get('title',''),'art_code':a.get('art_code',''),'url':f'https://data.eastmoney.com/notices/detail/{code}/{a.get("art_code", "")}.html'})
        return code,clean,''
    except Exception as e:return code,[],str(e)

def main():
    started=datetime.now(timezone.utc).isoformat()
    raw=list(csv.DictReader(SOURCE.open(encoding='utf-8-sig')))
    codes=[r['security_code'].zfill(6) for r in raw]
    if len(raw)!=5550 or len(set(codes))!=5550:raise RuntimeError('archived universe is not exactly 5550 unique securities')
    quotes={};quote_errors=[]
    batches=[codes[i:i+50] for i in range(0,len(codes),50)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs={pool.submit(quote_batch,b):b for b in batches}
        for done,f in enumerate(as_completed(futs),1):
            try:quotes.update(f.result())
            except Exception as e:quote_errors.append({'codes':futs[f],'error':str(e)})
            if done%20==0:print('quote batches',done,len(batches),'rows',len(quotes),flush=True)
    rows=[]
    for r in raw:
        code=r['security_code'].zfill(6);name=r.get('name') or r.get('name_h1_2026') or ''
        n=lambda k:num(r.get(k))
        rev=n('revenue_h1_2026');pr=n('net_profit_h1_2026');ded=n('is_deduct_profit_stmt_cur');ded0=n('is_deduct_profit_stmt_prev');ocf=n('cf_netcash_operate_cur')
        ttms=[n('net_profit_fy_2025'),n('net_profit_h1_2025'),pr]
        ttm=ttms[0]-ttms[1]+ttms[2] if all(v is not None for v in ttms) else None
        q2=[pr,n('net_profit_q1_2026'),n('net_profit_h1_2025'),n('net_profit_q1_2025')]
        q2cur=q2[0]-q2[1] if all(v is not None for v in q2[:2]) else None
        q2prev=q2[2]-q2[3] if all(v is not None for v in q2[2:]) else None
        x={'code':code,'name':name,'industry':r.get('industry',''),'h1_revenue_100m':div(rev,1e8),'h1_revenue_growth_pct':grow(rev,n('revenue_h1_2025')),'h1_profit_100m':div(pr,1e8),'h1_deduct_100m':div(ded,1e8),'h1_deduct_growth_pct':grow(ded,ded0),'h1_ocf_100m':div(ocf,1e8),'cash_profit_ratio':div(ocf,pr) if pr and pr>0 else None,'deduct_to_reported':div(ded,pr) if pr and pr>0 else None,'q2_profit_growth_pct':grow(q2cur,q2prev),'q2_profit_100m':div(q2cur,1e8),'q2_prior_loss':bool(q2prev is not None and q2prev<=0),'h1_roe_pct':n('roe_h1_2026'),'gross_margin_change_pp':n('gross_margin_delta'),'debt_ratio':div(n('bs_total_liabilities_cur'),n('bs_total_assets_cur')),'ar_growth_pct':grow(n('bs_accounts_receivable_cur'),n('bs_accounts_receivable_prev')),'inventory_growth_pct':grow(n('bs_inventory_cur'),n('bs_inventory_prev')),'ttm_profit_100m':div(ttm,1e8),'report_date':r.get('announcement_date',''),'old_score_ignored':True}
        x.update(quotes.get(code,{}))
        x['pe_ttm_recomputed']=div(x.get('market_cap_100m'),x.get('ttm_profit_100m')) if ttm and ttm>0 else None
        x['finance_separate_model']=any(s in x['industry'] for s in FINANCE)
        x['cyclical_industry_proxy']=any(s in x['industry'] for s in CYCLICAL)
        issues=[]
        if re.search(r'ST|退',name,re.I):issues.append('risk_warning_name')
        if x['finance_separate_model']:issues.append('financial_requires_separate_model')
        if x.get('quote_date')!=ASOF:issues.append('missing_or_stale_quote')
        if not x.get('price') or x.get('turnover_cny',0)<2e7:issues.append('low_or_missing_liquidity')
        if ded is None:issues.append('missing_deduct_profit')
        if ded is not None and ded<=3e7:issues.append('deduct_profit_below30m')
        if pr is None or pr<=0:issues.append('loss_or_missing_profit')
        if x['cash_profit_ratio'] is None or x['cash_profit_ratio']<0.8:issues.append('cash_conversion_below0.8_or_missing')
        if x['deduct_to_reported'] is None or not 0.75<=x['deduct_to_reported']<=1.5:issues.append('large_nonrecurring_gap_or_missing')
        if x['debt_ratio'] is None or x['debt_ratio']>0.7:issues.append('high_or_missing_leverage')
        if x['pe_ttm_recomputed'] is None or not 0<x['pe_ttm_recomputed']<=45:issues.append('high_or_invalid_ttm_pe')
        growth=x['h1_revenue_growth_pct'];dg=x['h1_deduct_growth_pct']
        if growth is None or growth<5:issues.append('revenue_growth_below5_or_missing')
        if dg is None or dg<15:issues.append('deduct_growth_below15_or_lowbase')
        if growth is not None:
            if x['ar_growth_pct'] is not None and x['ar_growth_pct']>growth+30:issues.append('receivables_outrun_sales')
            if x['inventory_growth_pct'] is not None and x['inventory_growth_pct']>growth+30:issues.append('inventory_outruns_sales')
        if abs(x.get('day_return_pct') or 0)>8:issues.append('large_last_day_move')
        x['screen_issues']=';'.join(issues);x['financial_screen_pass']=not issues
        rows.append(x)
    eligible=[r for r in rows if r['financial_screen_pass']]
    def pct(vals,val):
        if val is None:return 0
        return sum(v<=val for v in vals)/len(vals) if vals else 0
    fields={'h1_deduct_growth_pct':20,'h1_revenue_growth_pct':15,'cash_profit_ratio':20,'h1_roe_pct':15,'gross_margin_change_pp':10,'q2_profit_growth_pct':10,'pe_ttm_recomputed':10}
    for x in eligible:
        score=0
        for col,w in fields.items():
            vals=[r[col] for r in eligible if r.get(col) is not None]
            v=pct(vals,x.get(col))
            if col=='pe_ttm_recomputed':v=1-v
            score+=w*v
        x['audit_rank_score']=round(score,2)
    eligible.sort(key=lambda x:x['audit_rank_score'],reverse=True)
    for i,r in enumerate(eligible,1):r['screen_rank']=i
    # Review capacity constraint, not evidence of investment superiority.
    selected=[];counts=Counter()
    for r in eligible:
        if counts[r['industry']]>=6:continue
        counts[r['industry']]+=1;selected.append(r)
        if len(selected)>=80:break
    histrows=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        fs={pool.submit(history,x):x for x in selected}
        for f in as_completed(fs):
            c,d,h=f.result();fs[f].update(d)
            histrows.extend({'code':c,'date':a[0],'open':a[1],'close':a[2],'high':a[3],'low':a[4],'volume':a[5]} for a in h)
    ann={};ann_errors={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(notices,x['code']) for x in selected]):
            c,a,e=f.result();ann[c]=a
            if e:ann_errors[c]=e
    for x in selected:
        x['announcement_titles_count']=len(ann.get(x['code'],[]))
        x['announcement_fulltext_reviewed']=False
        x['announcement_error']=ann_errors.get(x['code'],'')
        x['price_setup']='unverified'
        if x.get('history_date')==ASOF and x.get('quote_history_match'):
            if x['return5_pct']>12 or x['return20_pct']>30 or x['price']>1.12*x['ma20']:x['price_setup']='extended_do_not_chase'
            elif x['price']<x['ma20']:x['price_setup']='below_ma20_no_short_term_confirmation'
            elif x.get('ma60') and x['ma20']<x['ma60']:x['price_setup']='recovery_needs_confirmation'
            else:x['price_setup']='not_extended_trend_research_only'
        x['tomorrow_buy_approved']=False
    # Full output covers every row, including exclusions; final recommendation remains human work.
    savecsv('universe_audit.csv',rows)
    savecsv('financial_pass.csv',eligible)
    savecsv('review80.csv',selected)
    savecsv('review80_daily_history.csv',histrows)
    savejson('recent_announcements.json',ann)
    savejson('quote_errors.json',quote_errors)
    cols=['screen_rank','code','name','industry','price','quote_date','market_cap_100m','pe_ttm_recomputed','h1_revenue_growth_pct','h1_deduct_growth_pct','cash_profit_ratio','h1_roe_pct','q2_profit_growth_pct','return5_pct','return20_pct','ma5','ma20','price_setup']
    lines=['# 2026-09-07 buyability research screen','', '> Machine screen only. No final buy recommendations. Archived fundamentals are not independently validated by this run.','', '|'+ '|'.join(cols)+'|','|'+'|'.join('---' for _ in cols)+'|']
    for x in selected:
        vals=[]
        for c in cols:
            v=x.get(c)
            vals.append('' if v is None else f'{v:.2f}' if isinstance(v,float) else str(v))
        lines.append('|'+ '|'.join(vals)+'|')
    (OUT/'review80.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    issue_counts=Counter(i for r in rows for i in r['screen_issues'].split(';') if i)
    manifest={'started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'archive_count':len(rows),'unique_codes':len(set(codes)),'quote_retrieved':len(quotes),'quote_date_counts':dict(Counter(q.get('quote_date') for q in quotes.values())),'quotes_asof_count':sum(q.get('quote_date')==ASOF for q in quotes.values()),'financial_pass_count':len(eligible),'review_count':len(selected),'price_setup_counts':dict(Counter(x['price_setup'] for x in selected)),'quote_batch_failures':len(quote_errors),'announcement_errors':len(ann_errors),'exclusion_counts':dict(issue_counts),'missing_fields':{f:sum(r.get(f) is None for r in rows) for f in ['h1_deduct_growth_pct','q2_profit_growth_pct','cash_profit_ratio','debt_ratio','pe_ttm_recomputed']},'all_5550_filing_read':False,'fresh_financial_statement_download':False,'final_buy_recommendations':0,'limitations':['Archived input reused and requires official-filing spot checks.','Financial institutions excluded from generic industrial screen, not judged unattractive.','Recovery and speculative events without current recurring earnings are not covered.','No analyst-count or prior final20 score used.','Technical filters are heuristics, not backtested probabilities.','Only recent announcement TITLES retrieved, not equivalent to full-text review.','No order placed; screen is not a buy list.'],'checksums':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file()}}
    savejson('manifest.json',manifest)
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
