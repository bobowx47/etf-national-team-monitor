# -*- coding: utf-8 -*-
"""
放量异动微信提醒 - 监控脚本
监控三类对象：大盘指数 / 持仓个股 / 主力强势股
放量(量比达标)时通过 Server酱 推送微信
用法：
  python3 monitor.py         正常模式（会推送、更新状态）
  python3 monitor.py --test  测试模式（只打印判断，不推送、不改状态）
"""
import urllib.request
import urllib.parse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TEST = '--test' in sys.argv
SENDKEY = os.environ.get('SERVERCHAN_KEY', '')

# ==================== 配置区（可按需调整） ====================
# 大盘指数：代码 -> 名称
INDICES = {
    'sh000001': '上证指数',
    'sh000300': '沪深300',
    'sz399006': '创业板指',
}
# 自选股：用户已在旧版卖出的4只股票全部移除。
# 现在用户自己关注的个股在网页「我的自选股」模块里添加，由网页端盯盘并推送。
# 本脚本只负责：大盘指数 + 主力强势股（这两类不依赖用户手动维护）。
HOLDINGS = {}
# 放量阈值（量比：当前成交量 / 近5日同期均量）
IDX_RATIO_UP = 1.5      # 大盘放量上涨阈值
IDX_RATIO_DOWN = 1.5    # 大盘放量下跌阈值
STK_RATIO_UP = 2.0      # 个股放量上涨阈值
STK_RATIO_DOWN = 2.0    # 个股放量下跌阈值
IDX_PCT_MIN = 0.3       # 大盘涨跌幅度最小(%)
STK_PCT_MIN = 2.0       # 个股涨跌幅度最小(%)
COOL_MIN = 60           # 同一标的冷却时间(分钟)
RATIO_STEP = 1.15       # 量比再放大15%可突破冷却提前推
# 去重状态存到用户主目录，确保任何执行环境都能读写、跨运行保持
STATE_FILE = os.path.expanduser('~/.etf_monitor_state.json')

BJ = timezone(timedelta(hours=8))


def now_bj():
    return datetime.now(BJ)


def in_session():
    """判断当前是否A股交易时段（北京时间 工作日 9:25-11:31 / 12:59-15:02）"""
    n = now_bj()
    if n.weekday() >= 5:
        return False, '周末休市'
    t = n.hour * 60 + n.minute
    if 9 * 60 + 25 <= t <= 11 * 60 + 31:
        return True, '上午交易时段'
    if 12 * 60 + 59 <= t <= 15 * 60 + 2:
        return True, '下午交易时段'
    return False, '非交易时段'


def get_quotes(codes):
    """批量抓腾讯实时行情，返回 {纯代码: 数据} """
    if not codes:
        return {}
    url = 'https://qt.gtimg.cn/q=' + ','.join(codes)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    raw = urllib.request.urlopen(req, timeout=20).read().decode('gbk', 'ignore')
    out = {}
    for line in raw.split(';'):
        if '~' not in line:
            continue
        f = line.split('~')
        if len(f) < 50:
            continue
        try:
            out[f[2]] = {
                'name': f[1],
                'price': float(f[3]),
                'pct': float(f[32]) if f[32] else 0.0,
                'ratio': float(f[49]) if f[49] else 0.0,
                'amount': float(f[37]) / 1e4 if f[37] else 0.0,  # 成交额(亿)
                'turn': float(f[38]) if len(f) > 38 and f[38] else 0.0,  # 换手率
            }
        except (ValueError, IndexError):
            continue
    return out


def get_strong_stocks(n=12):
    """抓东财主力资金净流入前N只，返回腾讯格式代码列表"""
    today = now_bj().strftime('%Y-%m-%d')
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?'
           'reportName=RPT_DMSK_TS_STOCKNEW&columns=SECURITY_CODE,SECURITY_NAME_ABBR,PRIME_INFLOW,CHANGE_RATE'
           f'&pageSize={n}&sortColumns=PRIME_INFLOW&sortTypes=-1&filter=(TRADE_DATE%3D%27{today}%27)')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                   'Referer': 'https://data.eastmoney.com/'})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        rows = d['result']['data']
        codes = []
        for r in rows:
            c = r['SECURITY_CODE']
            codes.append(('sh' if c[0] == '6' else 'sz') + c)
        return codes, rows
    except Exception as e:
        print('强势股抓取失败:', e)
        return [], []


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    if TEST:
        return
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def should_push(key, ratio, state):
    """冷却判断：距上次推送>60分钟，或量比较上次放大15%以上"""
    old = state.get(key)
    if not old:
        return True
    try:
        last_ts = datetime.fromisoformat(old['ts'])
        mins = (now_bj() - last_ts).total_seconds() / 60
        last_ratio = float(old.get('ratio', 0))
    except Exception:
        return True
    if mins >= COOL_MIN:
        return True
    if ratio >= last_ratio * RATIO_STEP:
        return True
    return False


def push_wechat(title, desp):
    if TEST:
        return True
    if not SENDKEY:
        print('未配置 SERVERCHAN_KEY，跳过推送')
        return False
    url = f'https://sctapi.ftqq.com/{SENDKEY}.send'
    data = urllib.parse.urlencode({'title': title, 'desp': desp}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        resp = urllib.request.urlopen(req, timeout=20).read().decode()
        r = json.loads(resp)
        ok = r.get('code') == 0
        print('推送结果:', '成功' if ok else resp[:200])
        return ok
    except Exception as e:
        print('推送失败:', e)
        return False


def main():
    ok, reason = in_session()
    print(f'当前(北京): {now_bj().strftime("%Y-%m-%d %H:%M")} | {reason}')
    if not ok and not TEST:
        print('非交易时段，跳过本次监控')
        return

    state = load_state()
    msgs = []  # (key, 标题, 正文)

    # ---------- 1. 大盘 ----------
    idx_codes = list(INDICES.keys())
    q = get_quotes(idx_codes)
    for code, name in INDICES.items():
        d = q.get(code[2:])
        if not d:
            continue
        ratio, pct = d['ratio'], d['pct']
        if ratio >= IDX_RATIO_UP and pct >= IDX_PCT_MIN:
            if should_push(code, ratio, state):
                state[code] = {'ts': now_bj().isoformat(), 'ratio': ratio}
                msgs.append((code, f'📈 大盘放量上攻 · {name}',
                    f'### {name} 放量异动\n\n'
                    f'- 现价：**{d["price"]:.2f}**  涨跌：**<font color="#ff4d5e">+{pct:.2f}%</font>**\n'
                    f'- 量比：**{ratio:.2f}**（成交量是近5日同期的{ratio:.1f}倍）\n'
                    f'- 成交额：{d["amount"]:.0f}亿\n\n'
                    f'> 大盘放量上涨，通常说明资金在进场。**可考虑适当加仓**，但请结合趋势判断，别一次性满仓。\n\n'
                    f'*{now_bj().strftime("%H:%M")} 自动监控推送 · 仅供参考，不构成投资建议*'))
        elif ratio >= IDX_RATIO_DOWN and pct <= -IDX_PCT_MIN:
            if should_push(code, ratio, state):
                state[code] = {'ts': now_bj().isoformat(), 'ratio': ratio}
                msgs.append((code, f'📉 大盘放量下挫 · {name}',
                    f'### {name} 放量下跌\n\n'
                    f'- 现价：**{d["price"]:.2f}**  涨跌：**<font color="#22c55e">{pct:.2f}%</font>**\n'
                    f'- 量比：**{ratio:.2f}**  成交额：{d["amount"]:.0f}亿\n\n'
                    f'> 放量下跌说明抛压较重，**注意控制仓位、别急着抄底**，等企稳信号。\n\n'
                    f'*{now_bj().strftime("%H:%M")} 自动监控推送 · 仅供参考，不构成投资建议*'))

    # ---------- 2. 持仓个股 ----------
    hold_codes = list(HOLDINGS.keys())
    q2 = get_quotes(hold_codes)
    for code, info in HOLDINGS.items():
        name, cost, shares = info
        d = q2.get(code[2:])
        if not d:
            continue
        ratio, pct = d['ratio'], d['pct']
        # 成本盈亏
        pnl = ''
        if cost:
            diff = (d['price'] - cost) / cost * 100
            color = '#ff4d5e' if diff >= 0 else '#22c55e'
            pnl = f'- 持仓成本：{cost:.2f}  浮盈亏：**<font color="{color}">{diff:+.1f}%</font>**\n'
        if ratio >= STK_RATIO_UP and pct >= STK_PCT_MIN:
            if should_push('h_' + code, ratio, state):
                state['h_' + code] = {'ts': now_bj().isoformat(), 'ratio': ratio}
                msgs.append(('h_' + code, f'🚀 持仓放量走强 · {name}',
                    f'### {name} 放量异动\n\n'
                    f'- 现价：**{d["price"]:.2f}**  涨跌：**<font color="#ff4d5e">+{pct:.2f}%</font>**\n'
                    f'- 量比：**{ratio:.2f}**  换手：{d["turn"]:.1f}%  成交额：{d["amount"]:.1f}亿\n' + pnl + '\n'
                    f'> 你的持仓放量走强。**可关注但别追高**，放量不必然持续，注意冲高回落风险。\n\n'
                    f'*{now_bj().strftime("%H:%M")} 自动监控推送 · 仅供参考，不构成投资建议*'))
        elif ratio >= STK_RATIO_DOWN and pct <= -STK_PCT_MIN:
            if should_push('h_' + code, ratio, state):
                state['h_' + code] = {'ts': now_bj().isoformat(), 'ratio': ratio}
                msgs.append(('h_' + code, f'⚠️ 持仓放量下挫 · {name}',
                    f'### {name} 放量下跌\n\n'
                    f'- 现价：**{d["price"]:.2f}**  涨跌：**<font color="#22c55e">{pct:.2f}%</font>**\n'
                    f'- 量比：**{ratio:.2f}**  成交额：{d["amount"]:.1f}亿\n' + pnl + '\n'
                    f'> 持仓放量下跌，抛压明显。**留意止损位**，别盲目补仓摊薄。\n\n'
                    f'*{now_bj().strftime("%H:%M")} 自动监控推送 · 仅供参考，不构成投资建议*'))

    # ---------- 3. 主力强势股 ----------
    strong_codes, strong_rows = get_strong_stocks(12)
    if strong_codes:
        q3 = get_quotes(strong_codes)
        name_map = {('sh' if r['SECURITY_CODE'][0] == '6' else 'sz') + r['SECURITY_CODE']:
                    r['SECURITY_NAME_ABBR'] for r in strong_rows}
        for code in strong_codes:
            d = q3.get(code[2:])
            if not d:
                continue
            ratio, pct = d['ratio'], d['pct']
            nm = name_map.get(code, d['name'])
            if ratio >= STK_RATIO_UP and pct >= STK_PCT_MIN:
                if should_push('s_' + code, ratio, state):
                    state['s_' + code] = {'ts': now_bj().isoformat(), 'ratio': ratio}
                    msgs.append(('s_' + code, f'🔥 强势股放量 · {nm}',
                        f'### {nm} 放量异动（主力抢筹股）\n\n'
                        f'- 现价：**{d["price"]:.2f}**  涨跌：**<font color="#ff4d5e">+{pct:.2f}%</font>**\n'
                        f'- 量比：**{ratio:.2f}**  换手：{d["turn"]:.1f}%\n\n'
                        f'> 该股在今日主力净流入榜内且此刻放量。**仅作观察信号**，强势股风险高，追高需谨慎、务必设止损。\n\n'
                        f'*{now_bj().strftime("%H:%M")} 自动监控推送 · 仅供参考，不构成投资建议*'))

    # ---------- 汇总推送 ----------
    print(f'本次触发 {len(msgs)} 条提醒')
    if msgs:
        for key, title, desp in msgs:
            print(f'  → {title}')
            push_wechat(title, desp)
        save_state(state)
        print('状态已更新')
    else:
        print('无放量异动，本次不推送')


if __name__ == '__main__':
    main()
