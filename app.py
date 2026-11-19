"""Truck Load Planner - Streamlit app.

Purpose: optimise vehicle loading, recommend the right vehicle mix (high utilisation - not
always the biggest or the smallest truck) and give loaders a clear planogram + loading sheet.

Run:  streamlit run app.py
Inputs: Excel workbook with sheets Order, Category norms, Truck master, Settings
(the bundled load_planner_inputs.xlsx is used until you upload your own).
"""
import io
import math
from pathlib import Path

import pandas as pd
import streamlit as st

from optimizer import (MODES, usable_truck, plan_trucks, single_category_max, recommend_fleet, top_up_hint, build_lines)
from planogram import top_view, side_view, view_3d, colour
from loader import loader_html, loading_steps

st.set_page_config(page_title="Truck Load Planner", page_icon="🚚", layout="wide")
HERE = Path(__file__).parent
TEMPLATE = HERE / "load_planner_inputs.xlsx"
SHEETS = {  # sheet: (required columns, optional columns)
    "Order": (["Category", "Order qty (cases)"], ["Drop", "Stop sequence", "Priority", "Must-load min (cases)"]),
    "Category norms": (["Category", "Case weight (kg)", "Stacking layers", "Case length (mm)", "Case width (mm)", "Case height (mm)"], []),
    "Truck master": (["Truck type", "Body type", "Internal length (mm)", "Internal width (mm)", "Usable stack height (mm)", "Capacity (MT)"],
                     ["Available for planning (Y/N)"]),
}
ORDER_COLS = ["Drop", "Stop sequence", "Category", "Order qty (cases)", "Priority", "Must-load min (cases)"]


@st.cache_data
def read_inputs(data: bytes):
    xl = pd.ExcelFile(io.BytesIO(data))
    out, problems = {}, []
    for sheet, (req, opt) in SHEETS.items():
        if sheet not in xl.sheet_names:
            problems.append(f"Missing sheet '{sheet}'"); continue
        df = xl.parse(sheet)
        df.columns = [str(c).strip() for c in df.columns]
        miss = [c for c in req if c not in df.columns]
        if miss:
            problems.append(f"Sheet '{sheet}' is missing columns: {', '.join(miss)}"); continue
        for c in opt:
            if c not in df.columns:
                df[c] = None
        df = df[req + opt].dropna(subset=req[:2]).reset_index(drop=True)
        out[sheet] = df[ORDER_COLS] if sheet == "Order" else df
    settings = {}
    if "Settings" in xl.sheet_names:
        s = xl.parse("Settings")
        settings = dict(zip(s.iloc[:, 0].astype(str), s.iloc[:, 1]))
    return out, settings, problems


@st.cache_data(show_spinner=False)
def cached_recommend(order, norms, trucks, clear, penalty, by_stop, target):
    return recommend_fleet(order, norms, trucks, clear, penalty, by_stop, min_util=target)


@st.cache_data(show_spinner=False)
def cached_plan(order, norms, trow, clear, mode, max_trucks, by_stop):
    return plan_trucks(order, norms, usable_truck(trow, *clear), mode, max_trucks, by_stop)


def setting(key, default):
    v = settings.get(key, default)
    return default if pd.isna(v) else v


def fmt_lines(df):
    cols = [c for c in df.columns if c not in ("Volume (m³)",)]
    df = df[cols]
    if df["Drop"].replace("", None).isna().all():
        df = df.drop(columns=["Drop", "Stop"])
    return df.style.format({"% loaded": "{:.0%}", "Length (mm)": "{:,.0f}", "Weight (kg)": "{:,.0f}",
                            "Loaded cases": "{:,}", "Not loaded": "{:,}", "Order qty": "{:,}"}) \
        .apply(lambda r: [f"background-color:{colour(int(r['Line']))};color:white" if c == "Line" else "" for c in r.index], axis=1)


def render_plan(p, key, title, by="Line", target=0.85):
    """Metrics, line table, planogram, 3D and loader sheet for one truck."""
    t, k = p["truck"], p["kpi"]
    c = st.columns(5)
    c[0].metric("Cases", f"{k['cases']:,}")
    c[1].metric("Payload", f"{k['weight_kg'] / 1000:.2f} MT", f"{k['payload_util']:.0%} of {t.payload_kg / 1000:g} T", delta_color="off")
    c[2].metric("Floor length", f"{k['length_mm'] / 1000:.2f} m", f"{k['floor_util']:.0%} used", delta_color="off")
    c[3].metric("Vehicle utilisation", f"{k['utilisation']:.0%}", "on target" if k["utilisation"] >= target else "below target",
                delta_color="normal" if k["utilisation"] >= target else "inverse",
                help="Higher of payload % and floor-length %: how full the truck is on its binding limit.")
    c[4].metric("Filled by", "Weight" if k["payload_util"] >= k["floor_util"] else "Floor space")
    if k["utilisation"] < target:
        st.info(f"Below the {target:.0%} target. {top_up_hint(p, None)}.")
    v1, v2, v3, v4 = st.tabs(["Planogram", "Loading steps", "3D view", "Order lines"])
    with v1:
        st.plotly_chart(top_view(p["stacks"], p["blocks"], t, by), width="stretch", key=f"top{key}")
        st.plotly_chart(side_view(p["stacks"], t, by), width="stretch", key=f"side{key}")
    with v2:
        steps = loading_steps(p["blocks"]).drop(columns="_key")
        st.dataframe(steps, width="stretch", hide_index=True)
        st.caption("Start at the cab end and work towards the doors.")
    with v3:
        st.plotly_chart(view_3d(p["stacks"], t, by), width="stretch", key=f"3d{key}")
    with v4:
        st.dataframe(fmt_lines(p["table"]), width="stretch", hide_index=True)
    st.download_button("🖨 Loader sheet (printable HTML)", loader_html(p, title, by), f"{title.replace(' ', '_')}_loading_sheet.html",
                       "text/html", key=f"ls{key}")


def excel_report(plans, label):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame([{"Truck no.": n + 1, "Vehicle": p["truck"].name, "Cases": p["kpi"]["cases"], "Weight (kg)": round(p["kpi"]["weight_kg"]),
                       "Payload %": round(p["kpi"]["payload_util"], 3), "Floor %": round(p["kpi"]["floor_util"], 3),
                       "Utilisation": round(p["kpi"]["utilisation"], 3)} for n, p in enumerate(plans)]).to_excel(w, sheet_name="Summary", index=False)
        for n, p in enumerate(plans):
            loading_steps(p["blocks"]).drop(columns="_key").to_excel(w, sheet_name=f"Truck {n + 1} loading steps", index=False)
            p["table"].to_excel(w, sheet_name=f"Truck {n + 1} lines", index=False)
    return buf.getvalue()


# ================================================================ sidebar
st.sidebar.title("🚚 Load Planner")
up = st.sidebar.file_uploader("Input workbook (.xlsx)", type="xlsx", help="Sheets: Order, Category norms, Truck master, Settings")
raw = up.getvalue() if up else TEMPLATE.read_bytes()
st.sidebar.download_button("Download input template", TEMPLATE.read_bytes(), "load_planner_inputs.xlsx")
data, settings, problems = read_inputs(raw)
if problems:
    st.error("Input workbook problems:\n\n- " + "\n- ".join(problems)); st.stop()
if not up:
    st.sidebar.caption("Using the bundled template. Upload your own workbook to replace it.")
with st.sidebar.expander("Loading clearances (mm)"):
    cl = st.number_input("Length", 0, 500, int(setting("Length clearance (mm)", 50)))
    cw = st.number_input("Width", 0, 300, int(setting("Width clearance (mm)", 20)))
    ch = st.number_input("Height", 0, 500, int(setting("Height clearance (mm)", 50)))
clear = (cl, cw, ch)
target = st.sidebar.slider("Target vehicle utilisation", 0.5, 1.0, float(setting("Target vehicle utilisation (%)", 85)) / 100, 0.05, format="%.2f")

# ================================================================ main
st.title("Truck Load Planner")
st.caption("Optimises how each truck is loaded, recommends the vehicle mix with the best utilisation, and produces a clear planogram and loading sheet for the loaders.")

order_box = st.container()          # order table shows above the tabs
tab_rec, tab_one, tab_matrix, tab_inputs = st.tabs(["Recommended vehicles", "Load a chosen truck", "Loadability matrix", "Category norms & trucks"])

with tab_inputs:   # runs first so edits apply everywhere
    st.write("Edit for this session (changes are not saved to your workbook).")
    norms = st.data_editor(data["Category norms"], num_rows="dynamic", width="stretch", key="norms")
    trucks = st.data_editor(data["Truck master"], num_rows="dynamic", width="stretch", key="trucks")
    st.caption("Case dimensions in the template are estimates - replace them with actual case sizes. "
               "Set 'Available for planning' to N to keep a vehicle out of recommendations.")

order_box.subheader("Order")
order = order_box.data_editor(
    data["Order"], num_rows="dynamic", width="stretch", key="order",
    column_config={"Drop": st.column_config.TextColumn(help="Optional: customer / delivery point for multi-drop trucks"),
                   "Stop sequence": st.column_config.NumberColumn(min_value=1, step=1, help="Optional: 1 = unloaded first (loaded last, at the doors)"),
                   "Category": st.column_config.SelectboxColumn(options=norms["Category"].tolist(), required=True),
                   "Order qty (cases)": st.column_config.NumberColumn(min_value=0, step=1),
                   "Priority": st.column_config.NumberColumn(min_value=1, step=1, help="1 = load first (Load a chosen truck → Priority order)"),
                   "Must-load min (cases)": st.column_config.NumberColumn(min_value=0, step=1, help="Optional minimum on the first truck")})
multi_drop = pd.to_numeric(order["Stop sequence"], errors="coerce").notna().any()
by_stop = order_box.checkbox("Multi-drop: load by stop sequence (last stop at the cab, first stop at the doors)", value=bool(multi_drop),
                      disabled=not multi_drop, help="Uses the Stop sequence column")
colour_by = "Stop" if by_stop else "Line"

# ---------------------------------------------------------------- recommended vehicles
with tab_rec:
    avail = trucks[trucks["Available for planning (Y/N)"].fillna("Y").astype(str).str.upper().str.startswith("Y")]
    c1, c2 = st.columns([1, 2])
    bodies = sorted(avail["Body type"].astype(str).unique())
    body_sel = c1.multiselect("Body types", bodies, default=[b for b in bodies if b == setting("Default truck type", bodies[0])] or bodies)
    pool = avail[avail["Body type"].isin(body_sel)]
    names_sel = c2.multiselect("Vehicles the planner may use", pool["Truck type"].tolist(), default=pool["Truck type"].tolist())
    pool = pool[pool["Truck type"].isin(names_sel)]
    penalty = st.slider("Vehicle choice", 0.0, 15.0, float(setting("Vehicle count penalty (T-equivalent)", 4)), 0.5,
                        help="Left: tightest fit (may use more, smaller trucks). Right: fewer, bigger trucks. "
                             "The planner minimises deployed capacity + this penalty per vehicle, so it never defaults to the biggest or the smallest truck.")
    st.caption("◄ tighter fit, more small trucks · · · fewer, bigger trucks ►")
    if pool.empty:
        st.warning("Select at least one vehicle.")
    else:
        with st.spinner("Choosing vehicles and optimising each load..."):
            rec = cached_recommend(order, norms, pool, clear, penalty, by_stop, target)
        if not rec.get("ok"):
            st.error(rec.get("message"))
        else:
            plans = rec["plans"]
            ks = [p["kpi"] for p in plans]
            cap = sum(p["truck"].payload_kg for p in plans) / 1000
            c = st.columns(5)
            c[0].metric("Vehicles", len(plans))
            c[1].metric("Cases planned", f"{rec['planned']:,}", f"of {rec['ordered']:,}", delta_color="off")
            c[2].metric("Load", f"{sum(k['weight_kg'] for k in ks) / 1000:.1f} MT", f"on {cap:g} T deployed", delta_color="off")
            c[3].metric("Average utilisation", f"{sum(k['utilisation'] for k in ks) / len(ks):.0%}")
            c[4].metric("Lowest utilisation", f"{min(k['utilisation'] for k in ks):.0%}")
            fleet = pd.DataFrame([{"Truck": f"Truck {n + 1}", "Vehicle": p["truck"].name, "Cases": p["kpi"]["cases"],
                                   "Weight (kg)": round(p["kpi"]["weight_kg"]), "Payload %": p["kpi"]["payload_util"], "Floor %": p["kpi"]["floor_util"],
                                   "Utilisation": p["kpi"]["utilisation"],
                                   "Status": "✓ on target" if p["kpi"]["utilisation"] >= target else "▲ " + top_up_hint(p, None)}
                                  for n, p in enumerate(plans)])
            st.dataframe(fleet.style.format({"Payload %": "{:.0%}", "Floor %": "{:.0%}", "Utilisation": "{:.0%}", "Cases": "{:,}", "Weight (kg)": "{:,}"})
                         .bar(subset=["Utilisation"], vmin=0, vmax=1, color="#9BC2E6"), width="stretch", hide_index=True)
            with st.expander("Why this mix? Compare with using a single vehicle type"):
                L0 = None; rows = []
                for _, tr in pool.iterrows():
                    t = usable_truck(tr, *clear); ls = build_lines(order, norms, t)
                    if not ls or any(not ln.fits for ln in ls):
                        continue
                    wt = sum(l.qty * l.weight for l in ls)
                    fl = sum(math.ceil(l.qty / max(o.cpr for o in l.orients)) * min(o.depth for o in l.orients if o.cpr) for l in ls)
                    need = max(wt / t.payload_kg, fl / t.length); nveh = max(1, math.ceil(need - 1e-9))
                    rows.append({"Vehicle": t.name, "Trucks needed (approx.)": nveh, "Deployed capacity (T)": nveh * t.payload_kg / 1000,
                                 "Average utilisation (approx.)": need / nveh})
                alt = pd.DataFrame(rows).sort_values("Deployed capacity (T)")
                st.dataframe(alt.style.format({"Average utilisation (approx.)": "{:.0%}", "Deployed capacity (T)": "{:g}"}), width="stretch", hide_index=True)
                st.caption(f"Recommended mix deploys {cap:g} T in {len(plans)} vehicles.")
            tabs = st.tabs([f"Truck {n + 1} · {p['truck'].name.split(' (')[0]}" for n, p in enumerate(plans)])
            for n, (p, tb) in enumerate(zip(plans, tabs)):
                with tb:
                    render_plan(p, f"r{n}", f"Truck {n + 1}", colour_by, target)
            st.download_button("⬇ Download vehicle plan (Excel)", excel_report(plans, "rec"), "vehicle_plan.xlsx", type="primary")

# ---------------------------------------------------------------- chosen truck
with tab_one:
    c1, c2, c3, c4 = st.columns(4)
    bodies_all = list(dict.fromkeys(trucks["Body type"].astype(str)))
    dbody = str(setting("Default truck type", bodies_all[0]))
    body = c1.selectbox("Truck type", bodies_all, index=bodies_all.index(dbody) if dbody in bodies_all else 0)
    caps = sorted(trucks.loc[trucks["Body type"] == body, "Capacity (MT)"].tolist())
    dcap = setting("Default capacity (MT)", caps[0])
    capsel = c2.selectbox("Capacity (MT)", caps, index=caps.index(dcap) if dcap in caps else 0, format_func=lambda x: f"{x:g}")
    mode = c3.selectbox("Objective", MODES, help="Maximise cases · Priority order (priority 1 first) · Proportional (same % of every line) · Maximise payload (kg)")
    multi = c4.checkbox("Split the whole order over several trucks of this type")
    trow = trucks[(trucks["Body type"] == body) & (trucks["Capacity (MT)"] == capsel)].iloc[0]
    t = usable_truck(trow, *clear)
    st.caption(f"**{t.name}** · usable {t.length:,.0f} × {t.width:,.0f} × {t.height:,.0f} mm · payload {t.payload_kg:,.0f} kg")
    with st.spinner("Optimising..."):
        plans1 = cached_plan(order, norms, trow, clear, mode, 20 if multi else 1, by_stop)
    if not plans1:
        st.warning("Nothing could be loaded. Check the order, must-load minimums and that cases fit this truck.")
    else:
        tot = sum(p["kpi"]["cases"] for p in plans1)
        ordered = int(pd.to_numeric(order["Order qty (cases)"], errors="coerce").fillna(0).sum())
        st.success(f"{len(plans1)} × {t.name} · {tot:,} of {ordered:,} cases loaded" if multi else f"{tot:,} of {ordered:,} cases fit in one {t.name}")
        tabs1 = st.tabs([f"Truck {n + 1}" for n in range(len(plans1))]) if multi else [st.container()]
        for n, (p, tb) in enumerate(zip(plans1, tabs1)):
            with tb:
                render_plan(p, f"c{n}", f"Truck {n + 1}", colour_by, target)
        st.download_button("⬇ Download load plan (Excel)", excel_report(plans1, "one"), "load_plan.xlsx")

# ---------------------------------------------------------------- matrix
with tab_matrix:
    st.write("Maximum cases of a single category per truck (lower of floor-space and weight limits, stacking norms applied).")
    m = single_category_max(norms, trucks, clear)
    st.dataframe(m.style.format("{:,}").background_gradient(axis=None, cmap="Blues"), width="stretch")
