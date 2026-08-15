"""One-off read-only pull of REAL-account history deals (manual trading) for DEB-20260815 §十八 follow-up.

Read-only: history_deal_list_query only. No orders, no writes to broker.
Env: sources the same env the systemd unit uses (.env.us) — done by caller shell.
Output: data/manual_live_history_2026-08-15.json
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
OUT = REPO / "data" / "manual_live_history_2026-08-15.json"


def main():
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm, RET_OK

    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
    if not pwd_md5:
        print("FATAL: FUTU_UNLOCK_PWD_MD5 not in env (source .env.us first)")
        return 2

    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port, is_encrypt=False)
    try:
        ret, data = ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
        if ret != RET_OK:
            print("FATAL unlock:", data)
            return 2

        # accounts
        ret, acc = ctx.get_acc_list()
        if ret != RET_OK:
            print("FATAL acc_list:", acc)
            return 2
        us_acc = None
        for _, row in acc.iterrows():
            if str(row.get("trd_env")) == "REAL" and "US" in str(row.get("trdmarket_list", "")):
                us_acc = str(row["acc_id"])
                break
        print("REAL US account:", us_acc)

        deals = []
        end = datetime(2026, 8, 15)
        start_account = datetime(2026, 1, 1)
        cur = start_account
        while cur < end:
            chunk_end = min(cur + timedelta(days=30), end)
            ret, df = ctx.history_deal_list_query(
                start=cur.strftime("%Y-%m-%d"), end=chunk_end.strftime("%Y-%m-%d"),
                trd_env=TrdEnv.REAL, acc_id=us_acc or 0,
            )
            if ret == RET_OK and df is not None and len(df) > 0:
                for rec in df.to_dict("records"):
                    rec["_chunk"] = cur.strftime("%Y-%m-%d")
                    deals.append(rec)
            cur = chunk_end
        # dedup by deal_id
        seen = {}
        for d in deals:
            seen[str(d.get("deal_id"))] = d
        deals = list(seen.values())
        for d in deals:
            for k, v in list(d.items()):
                if not isinstance(v, (str, int, float, bool, type(None))):
                    d[k] = str(v)
        OUT.write_text(json.dumps({"account": us_acc, "pulled_at": datetime.now().isoformat(),
                                   "n_deals": len(deals), "deals": deals}, ensure_ascii=False, indent=1, default=str))
        print("deals:", len(deals), "->", OUT)
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
