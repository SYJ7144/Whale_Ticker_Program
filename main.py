import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import yfinance as yf
from yahooquery import Screener
import FinanceDataReader as fdr
import pandas as pd
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from supabase import create_client, Client
import os
import time
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("🚨 보안 경고: SUPABASE_URL 또는 SUPABASE_KEY가 설정되지 않았습니다!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GLOBAL_CACHE = {"ALL": {}, "US": {}, "KR": {}}

def load_cache_from_db():
    global GLOBAL_CACHE
    try:
        res = supabase.table("whale_cache").select("*").execute()
        temp_cache = {"ALL": {}, "US": {}, "KR": {}}
        
        if res.data:
            for item in res.data:
                m = item['market']
                s = item['sort_by']
                if s not in temp_cache[m]:
                    temp_cache[m][s] = {}
                
                tv_symbol = item['ticker']
                display_ticker = tv_symbol.split(':')[-1] if ':' in tv_symbol else tv_symbol

                temp_cache[m][s][item['rank_num']] = {
                    'id': item['rank_num'],
                    'name': item['name'],
                    'ticker': display_ticker,
                    'tv_symbol': tv_symbol,
                    'price': item['price'],
                    'change': item['change'],
                    'change_val': item.get('change_val', 0.0),
                    'volume': item.get('volume', 0.0),
                    'amount': item.get('amount', 0.0),
                    'score': item['score'],
                    'tags': item['tags'],
                    'ev1': item['ev1'], 'ev2': item['ev2'], 'ev3': item['ev3'], 'ev4': item['ev4'],
                    'desc1': item.get('desc1', ''), 'desc2': item.get('desc2', ''),
                    'desc3': item.get('desc3', ''), 'desc4': item.get('desc4', ''),
                }
            
        new_cache = {"ALL": {}, "US": {}, "KR": {}}
        for m in temp_cache:
            for s in temp_cache[m]:
                sorted_items = [temp_cache[m][s][k] for k in sorted(temp_cache[m][s].keys())]
                new_cache[m][s] = sorted_items
                    
        GLOBAL_CACHE = new_cache
        print(f"[{datetime.now()}] ⚡ RAM 캐시 업데이트 완료!")
    except Exception as e:
        print(f"RAM 캐시 로드 에러: {e}")

def fetch_universe():
    us_tickers = []
    kr_info = {}
    try:
        s = Screener()
        data = s.get_screeners(['most_actives', 'day_gainers', 'growth_technology_stocks'])
        for key, val in data.items():
            if isinstance(val, dict) and 'quotes' in val:
                for q in val['quotes']:
                    symbol = q.get('symbol')
                    if symbol and '-' not in symbol and '=' not in symbol and '^' not in symbol:
                        us_tickers.append(symbol)
    except Exception:
        pass

    try:
        df_krx = fdr.StockListing('KRX')
        if 'Marcap' in df_krx.columns:
            df_krx = df_krx.sort_values(by='Marcap', ascending=False)
        top_kr = df_krx.head(50)
        for _, row in top_kr.iterrows():
            code = str(row['Code']).zfill(6)
            name = row['Name']
            kr_info[code] = name
    except Exception:
        pass

    us_tickers = list(set(us_tickers)) or ['NVDA', 'AAPL', 'TSLA', 'MSFT']
    if not kr_info:
        kr_info = {'005930': '삼성전자', '000660': 'SK하이닉스'}
        
    return us_tickers, kr_info

def compute_kr_signals(kr_info):
    end = datetime.now()
    start = end - timedelta(days=130)
    results = []

    for code, name in kr_info.items():
        time.sleep(0.1)
        try:
            df = fdr.DataReader(code, start, end)
            if df is None or len(df) < 25:
                continue
            df = df[df['Volume'] > 0].copy()
            if len(df) < 25:
                continue

            if 'Amount' in df.columns:
                df['거래대금'] = df['Amount']
            else:
                df['거래대금'] = df['Close'] * df['Volume']

            today = df.iloc[-1]
            current_price = float(today['Close'])
            prev_price = float(df['Close'].iloc[-2])
            change_pct = ((current_price - prev_price) / prev_price) * 100

            amount_today = float(today['거래대금'])
            hist_amount = df['거래대금'].iloc[:-1]
            avg20_amount = float(hist_amount.tail(20).mean()) if len(hist_amount) >= 5 else amount_today
            tvr = (amount_today / avg20_amount * 100) if avg20_amount > 0 else 100.0

            avg5_amount = float(hist_amount.tail(5).mean()) if len(hist_amount) >= 5 else avg20_amount
            avg60_amount = float(hist_amount.tail(60).mean()) if len(hist_amount) >= 20 else avg20_amount
            dry_up = avg60_amount > 0 and (avg5_amount / avg60_amount) < 0.6
            breakout = dry_up and avg5_amount > 0 and amount_today > avg5_amount * 3

            high, low, close = float(today['High']), float(today['Low']), float(today['Close'])
            clv = (close - low) / (high - low) if high > low else 0.5
            box_low20 = float(df['Low'].iloc[-21:-1].min()) if len(df) >= 21 else low
            near_support = low <= box_low20 * 1.03
            support_pattern = near_support and clv >= 0.65

            ev1 = int(12 + min(13, (tvr / 300) * 13)) 
            ev2 = int(15 + (10 if change_pct > 2 else 5)) 
            ev3 = int(6 + (6 if support_pattern else 0) + (6 if breakout else 0)) 
            ev4 = int(8 if change_pct > 3 else 4) 

            raw_score = ev1 + ev2 + ev3 + ev4
            score = int(min(88, raw_score)) 

            tag = "[수급 유입 집중]" if score >= 75 else ("[모멘텀 관찰]" if score >= 60 else "[관망]")

            results.append({
                'ticker': f"KRX:{code}", 'is_kr': True, 'full_ticker': code, 'name': name,
                'price': f"₩{int(current_price):,}", 'change': f"{'+' if change_pct > 0 else ''}{change_pct:.2f}%",
                'change_val': change_pct, 'volume': float(today['Volume']), 'amount': amount_today, 'score': score,
                'tags': [[tag]], 'ev1': ev1, 'ev2': ev2, 'ev3': ev3, 'ev4': ev4,
                'desc1': f"거래대금 20일 평균 대비 {tvr:.0f}%",
                'desc2': "가격 변동성과 거래량 패턴 기반 수급 분석",
                'desc3': "거래량 건조 후 첫 대량거래 포착" if breakout else ("저점 부근 밑꼬리 지지" if support_pattern else "특이 패턴 미포착"),
                'desc4': "단기 추세 및 모멘텀 지속성 반영",
            })
        except Exception:
            continue
    return results

def compute_us_signals(tickers):
    results = []
    if not tickers: return results
    try:
        data = yf.download(tickers, period="3mo", interval="1d", group_by="ticker", progress=False, threads=True)
    except Exception:
        return results

    for ticker in tickers:
        try:
            df = data[ticker].dropna() if len(tickers) > 1 else data.dropna()
            if len(df) < 25: continue

            current_price = float(df['Close'].iloc[-1])
            prev_price = float(df['Close'].iloc[-2])
            change_pct = ((current_price - prev_price) / prev_price) * 100

            amount = df['Close'] * df['Volume']
            amount_today = float(amount.iloc[-1])
            avg20_amount = float(amount.iloc[-21:-1].mean())
            tvr = (amount_today / avg20_amount * 100) if avg20_amount > 0 else 100.0

            last5 = df.iloc[-5:]
            up_days = int((last5['Close'].diff().dropna() > 0).sum())
            vol_confirm = float(last5['Volume'].mean()) > float(df['Volume'].iloc[-25:-5].mean())
            momentum_proxy = (up_days / 4) * (1.0 if vol_confirm else 0.5)

            channel_high20 = float(df['High'].iloc[-21:-1].max())
            channel_breakout = current_price > channel_high20

            try: real_name = yf.Ticker(ticker).info.get('shortName', ticker)
            except Exception: real_name = ticker

            ev1 = int(12 + min(13, (tvr / 300) * 13)) 
            ev2 = int(15 + min(15, momentum_proxy * 15))
            ev3 = int(6 + (6 if change_pct > 0 else 0) + (6 if tvr > 150 else 0))
            ev4 = int(4 + (8 if channel_breakout else 0))
            
            raw_score = ev1 + ev2 + ev3 + ev4
            score = int(min(88, raw_score)) 

            results.append({
                'ticker': ticker, 'is_kr': False, 'full_ticker': ticker, 'name': real_name,
                'price': f"${current_price:,.2f}", 'change': f"{'+' if change_pct > 0 else ''}{change_pct:.2f}%",
                'change_val': change_pct, 'volume': float(df['Volume'].iloc[-1]), 'amount': amount_today, 'score': score,
                'tags': [["[모멘텀 감지]"] if score >= 65 else ["[관망]"]],
                'ev1': ev1, 'ev2': ev2, 'ev3': ev3, 'ev4': ev4,
                'desc1': f"거래대금 20일 평균 대비 {tvr:.0f}%", 'desc2': "거래량 및 가격 추세 기반 모멘텀 분석",
                'desc3': "5일 연속 상승 + 거래량 동반" if up_days >= 3 else "단기 모멘텀 미미", 'desc4': "20일 신고가 돌파" if channel_breakout else "채널 내 등락",
            })
        except Exception:
            continue
    return results

def background_sync_job():
    print(f"[{datetime.now()}] 🔄 세력 분석 DB 갱신 시작...")
    us_tickers, kr_info = fetch_universe()
    raw_data = compute_us_signals(us_tickers) + compute_kr_signals(kr_info)
    if not raw_data: return

    # 4가지 풀(Pool) 카테고리
    sort_types = ["amount", "volume", "surge", "drop"]
    markets = ["ALL", "US", "KR"]

    for market_key in markets:
        if market_key == "KR": filtered_raw = [x for x in raw_data if x.get('is_kr', False)]
        elif market_key == "US": filtered_raw = [x for x in raw_data if not x.get('is_kr', False)]
        else: filtered_raw = raw_data

        target_data = filtered_raw if filtered_raw else raw_data

        for sort_by in sort_types:
            # 1단계: 각 풀(카테고리)의 특성에 맞춰 상위 후보군(예: 50개)을 먼저 추려냅니다.
            if sort_by == "amount":
                pool = sorted(target_data, key=lambda x: x['amount'], reverse=True)[:50]
            elif sort_by == "volume":
                pool = sorted(target_data, key=lambda x: x['volume'], reverse=True)[:50]
            elif sort_by == "surge":
                surge_items = [x for x in target_data if x['change_val'] > 0]
                pool = sorted(surge_items if surge_items else target_data, key=lambda x: x['change_val'], reverse=True)[:50]
            elif sort_by == "drop":
                drop_items = [x for x in target_data if x['change_val'] < 0]
                pool = sorted(drop_items if drop_items else target_data, key=lambda x: x['change_val'], reverse=False)[:50]
            else:
                pool = target_data

            # 2단계: 추려낸 풀 안에서 무조건 '세력점수(score)'가 높은 순(내림차순)으로 최종 TOP 20을 확정합니다!
            top_results = sorted(pool, key=lambda x: x['score'], reverse=True)[:20]

            try:
                supabase.table("whale_cache").delete().eq("market", market_key).eq("sort_by", sort_by).execute()
                for idx, item in enumerate(top_results):
                    payload = {
                        "market": market_key, "sort_by": sort_by, "rank_num": idx + 1,
                        "ticker": item['ticker'], "name": item['name'], "price": item['price'],
                        "change": item['change'], "change_val": item['change_val'], "volume": item['volume'],
                        "amount": item['amount'], "score": item['score'], "tags": item['tags'],
                        "ev1": item['ev1'], "ev2": item['ev2'], "ev3": item['ev3'], "ev4": item['ev4'],
                        "desc1": item['desc1'], "desc2": item['desc2'], "desc3": item['desc3'], "desc4": item['desc4'],
                        "updated_at": datetime.now().isoformat()
                    }
                    supabase.table("whale_cache").insert(payload).execute()
            except Exception as db_err:
                print(f"-> DB 에러 ({market_key} / {sort_by}): {db_err}")

    print(f"[{datetime.now()}] ✅ 수파베이스 갱신 완료!")
    load_cache_from_db() 

try:
    res = supabase.table("whale_cache").select("id").limit(1).execute()
    if not res.data: background_sync_job()
    else: load_cache_from_db()
except Exception: pass

scheduler = BackgroundScheduler()
scheduler.add_job(background_sync_job, CronTrigger(minute='*/10'))
scheduler.start()

# 💡 [핵심 API 로직] 각 풀별로 필터링한 뒤, 최종 출력은 무조건 '세력점수 높은 순'으로 내림차순 정렬합니다.
@app.get("/api/stocks")
def get_whale_stocks(market: str = Query("ALL"), sort_by: str = Query("amount")):
    market_data = GLOBAL_CACHE.get(market, {})
    if not market_data:
        market_data = GLOBAL_CACHE.get("ALL", {})

    all_items = []
    seen = set()
    for s_type, items in market_data.items():
        for item in items:
            if item['ticker'] not in seen:
                seen.add(item['ticker'])
                all_items.append(item)

    if not all_items:
        return []

    # 1단계: 탭별 카테고리(풀) 필터링 및 상위 후보군 추출
    if sort_by == "amount":
        pool = sorted(all_items, key=lambda x: float(x.get('amount', 0)), reverse=True)[:50]
    elif sort_by == "volume":
        pool = sorted(all_items, key=lambda x: float(x.get('volume', 0)), reverse=True)[:50]
    elif sort_by == "surge":
        surge_items = [x for x in all_items if float(x.get('change_val', 0)) > 0]
        pool = sorted(surge_items if surge_items else all_items, key=lambda x: float(x.get('change_val', 0)), reverse=True)[:50]
    elif sort_by == "drop":
        drop_items = [x for x in all_items if float(x.get('change_val', 0)) < 0]
        pool = sorted(drop_items if drop_items else all_items, key=lambda x: float(x.get('change_val', 0)), reverse=False)[:50]
    else:
        pool = all_items

    # 2단계: 어떤 탭이든 상관없이 무조건 '세력점수(score) 내림차순(높은 순)'으로 정렬하여 상위 20개 반환!
    top_20 = sorted(pool, key=lambda x: float(x.get('score', 0)), reverse=True)[:20]

    for idx, item in enumerate(top_20):
        item['id'] = idx + 1

    return top_20

@app.get("/api/chart")
def get_chart_data(ticker: str):
    try:
        clean_ticker = ticker.split(":")[-1] if ":" in ticker else ticker
        df = pd.DataFrame()

        if ticker.startswith("KRX:") or clean_ticker.isdigit():
            for suffix in [".KS", ".KQ"]:
                try:
                    temp_df = yf.download(clean_ticker + suffix, period="6mo", interval="1d", progress=False)
                    if not temp_df.empty:
                        df = temp_df
                        break
                except Exception:
                    continue
        else:
            df = yf.download(clean_ticker, period="6mo", interval="1d", progress=False)

        if df.empty:
            return []

        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        chart_data = []
        for _, row in df.iterrows():
            date_col = 'Date' if 'Date' in row else 'index'
            if date_col in row and pd.notna(row[date_col]):
                date_val = pd.to_datetime(row[date_col]).strftime("%Y-%m-%d")
                chart_data.append({
                    "time": date_val,
                    "open": float(row['Open']),
                    "high": float(row['High']),
                    "low": float(row['Low']),
                    "close": float(row['Close']),
                    "volume": float(row['Volume'])
                })
        return chart_data
    except Exception as e:
        print(f"차트 데이터 조회 에러: {e}")
        return []