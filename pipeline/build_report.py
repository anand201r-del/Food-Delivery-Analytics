"""
Builds the public report (docs/index.html) from the data in data/*.csv.gz.

  python pipeline/build_report.py

It aggregates the orders into a compact "cube" (per city x month: revenue, orders,
hours, cuisines, delivery times, coupons, payments ...), plus customer segments,
cohorts, RFM groups and leaderboards, then injects that JSON into
pipeline/report_template.html. The page itself does all filtering in the browser.
"""
import json
import os
from datetime import date

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ["cities", "zones", "customers", "restaurants", "menu_items", "delivery_partners", "coupons",
          "orders", "order_items", "payments", "ratings"]


def load_tuples():
    """Tables as lists of row tuples (the shape the aggregation code below expects)."""
    d = {}
    for t in TABLES:
        df = pd.read_csv(os.path.join(ROOT, "data", f"{t}.csv.gz"))
        if t == "coupons":
            df["valid_from"] = pd.to_datetime(df.valid_from).dt.date
            df["valid_to"] = pd.to_datetime(df.valid_to).dt.date
            df["max_discount"] = df.max_discount.astype(object).where(df.max_discount.notna(), None)
        if t == "orders":
            df["coupon_id"] = df.coupon_id.astype(object).where(df.coupon_id.notna(), None)
            df["delivered_time"] = df.delivered_time.astype(object).where(df.delivered_time.notna(), None)
        d[t] = list(df.itertuples(index=False, name=None))
    return d


# ---------------------------------------------------------------- 1. customer blocks per city / state
def city_blocks(d):
    O=pd.DataFrame(d["orders"],columns="order_id customer_id restaurant_id partner_id coupon_id order_time status item_total discount delivery_fee final promised delivered".split())
    R=pd.DataFrame(d["restaurants"],columns="restaurant_id name zone_id cuisine veg avg_rating tot open close onb act".split())
    Z=pd.DataFrame(d["zones"],columns="zone_id city_id zone lat lng".split()); C=pd.DataFrame(d["cities"],columns="city_id city state".split())
    CU=pd.DataFrame(d["customers"],columns="customer_id name email phone zone_id signup act".split())
    CP=pd.DataFrame(d["coupons"],columns="coupon_id code typ val cap mn vf vt act".split())
    RT=pd.DataFrame(d["ratings"],columns="rating_id order_id customer_id restaurant_id food deliv review rated".split())
    DP=pd.DataFrame(d["delivery_partners"],columns="partner_id n p z vehicle lat lng av j".split())
    O["order_time"]=pd.to_datetime(O.order_time); O["delivered"]=pd.to_datetime(O.delivered)
    ZC=Z.merge(C)[["zone_id","city"]]
    R=R.merge(ZC); CU=CU.merge(ZC)
    O=O.merge(R[["restaurant_id","city","cuisine","name"]],on="restaurant_id").merge(DP[["partner_id","vehicle"]],on="partner_id")
    O["mins"]=(O.delivered-O.order_time).dt.total_seconds()/60; O["late"]=O.mins>O.promised
    O["month"]=O.order_time.dt.strftime("%Y-%m"); O["hour"]=O.order_time.dt.hour; O["dow"]=O.order_time.dt.dayofweek
    months=sorted(O.month.unique()); END=O.order_time.max()
    RT=RT.merge(O[["order_id","mins","promised","city"]])
    RT["lb"]=RT.mins-RT.promised
    cuisines=list(O.groupby("cuisine").final.sum().sort_values(ascending=False).index)
    def block(o, cu, rt):
        D=o[o.status=="DELIVERED"]
        k=dict(orders=len(o),delivered=len(D),gmv=round(D.final.sum()),aov=round(D.final.mean(),2),cancel=round(100*(o.status=="CANCELLED").mean(),2),
               late=round(100*D.late.mean(),1),mins=round(D.mins.mean(),1),rating=round(rt.food.mean(),2),ratings=len(rt),customers=int(o.customer_id.nunique()),discount=round(o.discount.sum()))
        m=D.groupby("month").agg(r=("final","sum"),n=("order_id","size")).reindex(months,fill_value=0)
        days=o.order_time.dt.date.nunique()
        heat=o.groupby(["dow","hour"]).size().unstack(fill_value=0).reindex(index=range(7),columns=range(24),fill_value=0)
        # avg per day-of-week day count
        dd=o.groupby(o.order_time.dt.date).size(); dd.index=pd.to_datetime(dd.index); dow_avg=dd.groupby(dd.index.dayofweek).mean().reindex(range(7)).round(1)
        cu_rev=D.groupby("cuisine").final.sum().reindex(cuisines,fill_value=0)
        cu_n=D.groupby("cuisine").size().reindex(cuisines,fill_value=0)
        lh=D.groupby("hour").late.mean().reindex(range(24)).mul(100).round(1)
        b=pd.cut(rt.lb,[-1e9,0,10,20,1e9],labels=[0,1,2,3])
        rl=rt.groupby(b,observed=False).deliv.agg(["mean","size"])
        cnt=o.groupby("customer_id").size(); last=o.groupby("customer_id").order_time.max(); gap=(END-last).dt.days
        seg=dict(active=int((gap<=30).sum()),risk=int(((gap>30)&(gap<=90)).sum()),churned=int((gap>90).sum()),never=int(len(cu)-len(cnt)),
                 one_time=int((cnt==1).sum()),repeat=round(100*(cnt>=2).mean(),1),ordering=len(cnt))
        sp=D.groupby("customer_id").final.sum().sort_values(ascending=False).values; q=np.array_split(sp,4); quart=[round(100*x.sum()/sp.sum(),1) for x in q]
        fo=o.groupby("customer_id").order_time.min().dt.to_period("M"); oo=o.assign(c=o.customer_id.map(fo),p=o.order_time.dt.to_period("M")); oo["k"]=(oo.p-oo.c).apply(lambda z:z.n)
        act=oo.drop_duplicates(["customer_id","k"]); coh=act.groupby(["c","k"]).size().unstack(fill_value=0)
        cohort=[[str(i), int(coh.loc[i,0])]+[round(100*coh.loc[i,j]/coh.loc[i,0],1) if j in coh.columns and coh.loc[i,0]>0 and (i+j)<=pd.Period(END,"M") else None for j in (1,2,3)] for i in coh.index]
        veh=D.groupby("vehicle").agg(m=("mins","mean"),l=("late","mean"),n=("order_id","size"))
        cpn=D[D.coupon_id.notna()].groupby("coupon_id").agg(n=("order_id","size"),disc=("discount","sum"),rev=("final","sum"))
        cpn=cpn.join(CP.set_index("coupon_id")["code"])
        top=D.groupby(["restaurant_id","name","city","cuisine"]).final.sum().sort_values(ascending=False).head(5)
        return dict(k=k, m=[round(x) for x in m.r], mn=[int(x) for x in m.n], heat=heat.values.tolist(), dow=dow_avg.tolist(),
            cu=[round(x) for x in cu_rev], cun=[int(x) for x in cu_n], lh=[None if pd.isna(x) else float(x) for x in lh],
            rl=[[None if pd.isna(r["mean"]) else round(r["mean"],2), int(r["size"])] for _,r in rl.iterrows()],
            seg=seg, quart=quart, cohort=cohort,
            veh={v:[round(r.m,1),round(100*r.l,1),int(r.n)] for v,r in veh.iterrows()},
            cpn=[[r.code,int(r.n),round(r.disc),round(r.rev)] for _,r in cpn.iterrows()],
            top=[[k[1],k[2],k[3],round(v)] for k,v in top.items()])
    out={"months":months,"cuisines":cuisines,"cities":{}}
    out["cities"]["All"]=block(O,CU,RT)
    for c in O.groupby("city").final.sum().sort_values(ascending=False).index:
        out["cities"][c]=block(O[O.city==c],CU[CU.city==c],RT[RT.city==c])
    ST=C.set_index("city")["state"]
    O["state"]=O.city.map(ST); CU["state"]=CU.city.map(ST); RT["state"]=RT.city.map(ST)
    for stt in O.groupby("state").final.sum().sort_values(ascending=False).index:
        out["cities"]["st:"+stt]=block(O[O.state==stt],CU[CU.state==stt],RT[RT.state==stt])
    # coupon lift (all)
    lift=[]
    for _,r in CP.iterrows():
        days=(r.vt-r.vf).days+1
        if days>=120: continue
        vf=pd.Timestamp(r.vf); vt=pd.Timestamp(r.vt)+pd.Timedelta(days=1)
        dur=((O.order_time>=vf)&(O.order_time<vt)).sum(); bef=((O.order_time>=vf-pd.Timedelta(days=days))&(O.order_time<vf)).sum()
        cost=O[(O.coupon_id==r.coupon_id)&(O.status=="DELIVERED")].discount.sum()
        lift.append([r.code,str(r.vf),str(r.vt),days,round(bef/days,1),round(dur/days,1),round(100*(dur-bef)/bef,1),int(dur-bef),round(cost)])
    out["lift"]=lift
    return out


# ---------------------------------------------------------------- 2. the city x month cube
def build_cube(d, dash):
    O=pd.DataFrame(d["orders"],columns="order_id customer_id restaurant_id partner_id coupon_id order_time status item_total discount delivery_fee final promised delivered".split())
    R=pd.DataFrame(d["restaurants"],columns="restaurant_id name zone_id cuisine veg avg_rating tot open close onb act".split())
    Z=pd.DataFrame(d["zones"],columns="zone_id city_id zone lat lng".split()); C=pd.DataFrame(d["cities"],columns="city_id city state".split())
    CP=pd.DataFrame(d["coupons"],columns="coupon_id code typ val cap mn vf vt act".split())
    RT=pd.DataFrame(d["ratings"],columns="rating_id order_id customer_id restaurant_id food deliv review rated".split())
    DP=pd.DataFrame(d["delivery_partners"],columns="partner_id pname p z vehicle lat lng av j".split())
    P=pd.DataFrame(d["payments"],columns="pid order_id method amount pstatus paid".split())
    O["order_time"]=pd.to_datetime(O.order_time); O["delivered"]=pd.to_datetime(O.delivered)
    ZC=Z.merge(C)[["zone_id","city"]]; R=R.merge(ZC)
    O=O.merge(R[["restaurant_id","city","cuisine"]],on="restaurant_id").merge(DP[["partner_id","vehicle","pname"]],on="partner_id").merge(P[["order_id","method"]],on="order_id")
    O["mins"]=(O.delivered-O.order_time).dt.total_seconds()/60; O["late"]=(O.mins>O.promised)&(O.status=="DELIVERED")
    O["dl"]=O.status=="DELIVERED"; O["rev"]=np.where(O.dl,O.final,0.0)
    O["month"]=O.order_time.dt.strftime("%Y-%m"); O["hour"]=O.order_time.dt.hour; O["dow"]=O.order_time.dt.dayofweek
    months=sorted(O.month.unique()); MI={m:i for i,m in enumerate(months)}
    cities=list(O.groupby("city").rev.sum().sort_values(ascending=False).index)
    cuisines=list(O.groupby("cuisine").rev.sum().sort_values(ascending=False).index)
    vehicles=["Bike","Scooter","EV","Bicycle"]; codes=list(CP.code); methods=["UPI","CARD","WALLET","NETBANKING","COD"]
    RT=RT.merge(O[["order_id","mins","promised","city","month"]]); RT["b"]=pd.cut(RT.mins-RT.promised,[-1e9,0,10,20,1e9],labels=[0,1,2,3]).astype(int)
    O=O.merge(CP[["coupon_id","code"]],on="coupon_id",how="left")
    nm=len(months)
    def arr(g, idx, cols, val, fill=0):
        """pivot to [month][col] lists"""
        t=g.pivot_table(index=idx,columns=cols,values=val,aggfunc="sum",fill_value=fill) if val else None
        return t
    cube={}
    for c in cities:
        o=O[O.city==c]; rt=RT[RT.city==c]
        g=o.groupby("month")
        base=pd.DataFrame(index=months)
        base["orders"]=g.size(); base["del"]=g.dl.sum(); base["rev"]=g.rev.sum().round(); base["late"]=g.late.sum()
        base["mins"]=o[o.dl].groupby("month").mins.sum().round(1); base["disc"]=g.discount.sum().round()
        base["rs"]=rt.groupby("month").food.sum(); base["rn"]=rt.groupby("month").size()
        base=base.fillna(0)
        def piv(df, col, keys, val=None, agg="size"):
            if val is None: t=df.groupby(["month",col]).size().unstack(fill_value=0)
            else: t=df.groupby(["month",col])[val].agg(agg).unstack(fill_value=0)
            t=t.reindex(index=months,columns=keys,fill_value=0)
            return [[round(float(x),1) for x in row] for row in t.values]
        hours=piv(o,"hour",list(range(24)))
        od=o[o.dl]
        lateH=piv(od,"hour",list(range(24)),"late","sum"); delH=piv(od,"hour",list(range(24)))
        o2=o.assign(dh=o.dow*24+o.hour); heat=piv(o2,"dh",list(range(168)))
        cu=piv(od,"cuisine",cuisines,"final","sum"); cun=piv(od,"cuisine",cuisines)
        rls=piv(rt,"b",[0,1,2,3],"deliv","sum"); rln=piv(rt,"b",[0,1,2,3])
        vn=piv(od,"vehicle",vehicles); vl=piv(od,"vehicle",vehicles,"late","sum"); vm=piv(od,"vehicle",vehicles,"mins","sum")
        oc=od[od.code.notna()]
        cpn=piv(oc,"code",codes); cpd=piv(oc,"code",codes,"discount","sum"); cpr=piv(oc,"code",codes,"final","sum")
        pay=piv(o,"method",methods)
        cube[c]=dict(o=[int(x) for x in base.orders],d=[int(x) for x in base["del"]],r=[int(x) for x in base.rev],l=[int(x) for x in base.late],
            mn=[float(x) for x in base.mins],ds=[int(x) for x in base.disc],rs=[int(x) for x in base.rs],rn=[int(x) for x in base.rn],
            h=[[int(x) for x in r] for r in hours],lh=[[int(x) for x in r] for r in lateH],dh=[[int(x) for x in r] for r in delH],
            hm=[[int(x) for x in r] for r in heat],cu=[[int(x) for x in r] for r in cu],cn=[[int(x) for x in r] for r in cun],
            rls=[[int(x) for x in r] for r in rls],rln=[[int(x) for x in r] for r in rln],
            vn=[[int(x) for x in r] for r in vn],vl=[[int(x) for x in r] for r in vl],vm=vm,
            cpn=[[int(x) for x in r] for r in cpn],cpd=[[int(x) for x in r] for r in cpd],cpr=[[int(x) for x in r] for r in cpr],
            pay=[[int(x) for x in r] for r in pay])
    # restaurants: monthly revenue
    od=O[O.dl]
    rr=od.groupby(["restaurant_id","month"]).final.sum().unstack(fill_value=0).reindex(columns=months,fill_value=0)
    rrat=RT.groupby("restaurant_id" if "restaurant_id" in RT else "order_id").size()
    rt_full=pd.DataFrame(d["ratings"],columns="rating_id order_id customer_id restaurant_id food deliv review rated".split())
    ravg=rt_full.groupby("restaurant_id").food.agg(["mean","size"])
    rests=[]
    for _,r in R.iterrows():
        rv=rr.loc[r.restaurant_id].round().astype(int).tolist() if r.restaurant_id in rr.index else [0]*nm
        rests.append([r["name"],r.city,cuisines.index(r.cuisine),round(float(ravg.loc[r.restaurant_id,"mean"]),2) if r.restaurant_id in ravg.index else 0,int(ravg.loc[r.restaurant_id,"size"]) if r.restaurant_id in ravg.index else 0,rv])
    # partners leaderboard (full range) per city
    pp=od.groupby(["partner_id","pname","vehicle","city"]).agg(n=("order_id","size"),m=("mins","mean"),l=("late","mean")).reset_index()
    pp=pp[pp.n>=25]
    partners={}
    ST=C.set_index("city")["state"]; pp["state"]=pp.city.map(ST)
    states=list(O.assign(state=O.city.map(ST)).groupby("state").rev.sum().sort_values(ascending=False).index)
    for c in cities+["All"]+["st:"+x for x in states]:
        q=pp if c=="All" else pp[pp.state==c[3:]] if c.startswith("st:") else pp[pp.city==c]
        q=q.sort_values("m")
        partners[c]=dict(fast=[[r.pname,r.vehicle,r.city,int(r.n),round(r.m,1),round(100*r.l,1)] for r in q.head(6).itertuples()],
                         slow=[[r.pname,r.vehicle,r.city,int(r.n),round(r.m,1),round(100*r.l,1)] for r in q.tail(6).iloc[::-1].itertuples()],
                         count=int(len(q)))
    old=dash
    cust={c:{k:old["cities"][c][k] for k in ("seg","quart","cohort")} for c in old["cities"]}
    out=dict(states={x:[c for c in cities if ST[c]==x] for x in states},cityState={c:ST[c] for c in cities},months=months,cities=cities,cuisines=cuisines,vehicles=vehicles,codes=codes,methods=methods,cube=cube,rest=rests,partners=partners,cust=cust,lift=old["lift"])
    return out


# ---------------------------------------------------------------- 3. delivery-time bins + RFM segments
def add_rfm(d, cube):
    O=pd.DataFrame(d["orders"],columns="order_id customer_id restaurant_id partner_id coupon_id order_time status item_total discount delivery_fee final promised delivered".split())
    R=pd.DataFrame(d["restaurants"],columns="restaurant_id name zone_id cuisine veg avg_rating tot open close onb act".split())
    Z=pd.DataFrame(d["zones"],columns="zone_id city_id zone lat lng".split()); C=pd.DataFrame(d["cities"],columns="city_id city state".split())
    O["order_time"]=pd.to_datetime(O.order_time); O["delivered"]=pd.to_datetime(O.delivered)
    O=O.merge(R[["restaurant_id","zone_id"]]).merge(Z[["zone_id","city_id"]]).merge(C[["city_id","city"]])
    O["month"]=O.order_time.dt.strftime("%Y-%m"); months=cube["months"]
    D=O[O.status=="DELIVERED"].copy(); D["mins"]=(D.delivered-D.order_time).dt.total_seconds()/60
    # delivery-time histogram: 5-minute bins 10-15 ... 55-60, 60+
    edges=list(range(10,65,5)); labels=[f"{a}–{a+5}" for a in edges[:-1]]+["60+"]
    D["bin"]=np.clip(((D.mins-10)//5).astype(int),0,len(labels)-1)
    for c in cube["cities"]:
        t=D[D.city==c].groupby(["month","bin"]).size().unstack(fill_value=0).reindex(index=months,columns=range(len(labels)),fill_value=0)
        cube["cube"][c]["dt"]=[[int(x) for x in r] for r in t.values]
    cube["dtBins"]=labels
    # RFM (full history, as of the last order date)
    END=O.order_time.max()
    g=O.groupby("customer_id").agg(city=("city","first"),last=("order_time","max"),f=("order_id","size"))
    g["m"]=D.groupby("customer_id").final.sum(); g["m"]=g.m.fillna(0)
    g["rec"]=(END-g["last"]).dt.days
    g["R"]=pd.qcut(g.rec.rank(method="first"),5,labels=[5,4,3,2,1]).astype(int)
    g["F"]=pd.qcut(g.f.rank(method="first"),5,labels=[1,2,3,4,5]).astype(int)
    def seg(r):
        if r.R>=4 and r.F>=4: return "Champions"
        if r.F>=4 and r.R>=2: return "Loyal"
        if r.R>=4 and r.F>=2: return "Potential loyalists"
        if r.R>=4: return "New customers"
        if r.R<=2 and r.F>=3: return "At risk"
        if r.R<=2: return "Hibernating"
        return "Needs attention"
    g["seg"]=g.apply(seg,axis=1)
    order=["Champions","Loyal","Potential loyalists","New customers","Needs attention","At risk","Hibernating"]
    rfm={}
    CS=cube["cityState"]; g["state"]=g.city.map(CS)
    for c in ["All"]+cube["cities"]+["st:"+x for x in cube["states"]]:
        q=g if c=="All" else g[g.state==c[3:]] if c.startswith("st:") else g[g.city==c]
        tot=q.m.sum()
        rfm[c]=[[s,int((q.seg==s).sum()),round(100*q[q.seg==s].m.sum()/tot,1),round(q[q.seg==s].rec.mean(),0) if (q.seg==s).any() else None,
                 round(q[q.seg==s].f.mean(),1) if (q.seg==s).any() else None, round(q[q.seg==s].m.mean()) if (q.seg==s).any() else None] for s in order]
    cube["rfm"]=rfm
    return cube


# ---------------------------------------------------------------- 4. as-of date, latest day, page
def add_meta(d, cube):
    O = pd.DataFrame(d["orders"], columns="order_id customer_id restaurant_id partner_id coupon_id order_time status item_total discount delivery_fee final promised delivered".split())
    O["order_time"] = pd.to_datetime(O.order_time)
    as_of = O.order_time.max().date()
    month_days = pd.Period(as_of, "M").days_in_month
    cube["asOf"] = as_of.isoformat()
    cube["nOrders"] = int(len(O))
    cube["mtd"] = None if as_of.day == month_days else {"days": as_of.day, "monthDays": int(month_days)}
    log_path = os.path.join(ROOT, "data", "daily_log.json")
    log = json.load(open(log_path)) if os.path.exists(log_path) else []
    last = O[O.order_time.dt.date == as_of]
    dl = last[last.status == "DELIVERED"]
    lt = (pd.to_datetime(dl.delivered) - dl.order_time).dt.total_seconds() / 60 > dl.promised
    cube["latest"] = {"date": as_of.isoformat(), "orders": int(len(last)), "revenue": round(float(dl.final.sum())),
                      "late": round(100 * float(lt.mean()), 1) if len(dl) else 0,
                      "rain": next((x["rain_cities"] for x in reversed(log) if x["date"] == as_of.isoformat()), None),
                      "daily": [[x["date"], x["orders"], x["revenue"], len(x["rain_cities"])] for x in log[-30:]]}
    return cube


def indian(n):
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + parts + [tail]) if head else ",".join(parts + [tail])


def main():
    d = load_tuples()
    dash = city_blocks(d)
    cube = build_cube(d, dash)
    cube = add_rfm(d, cube)
    cube = add_meta(d, cube)
    tpl = open(os.path.join(ROOT, "pipeline", "report_template.html"), encoding="utf-8").read()
    html = tpl.replace("1,00,000 orders", indian(cube["nOrders"]) + " orders")
    html = html.replace("__DATA__", json.dumps(cube, separators=(",", ":")))
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    open(os.path.join(ROOT, "docs", "index.html"), "w", encoding="utf-8").write(html)
    print(f"docs/index.html built: data to {cube['asOf']}, {cube['nOrders']:,} orders, {len(html) // 1024} KB")


if __name__ == "__main__":
    main()
