"""Truck load optimiser: how many cases of each order line fit a truck.

Model (mixed-integer programme, solved with SciPy/HiGHS):
  - Each order line is loaded as its own block across the full truck width.
  - A block is made of rows; a row = lanes across the width x stacking layers.
  - Rows may be laid lengthwise (case length along the truck) or turned 90 deg;
    the solver may use both orientations for one line.
  - Constraints: floor length, payload (kg), stacking layers (norm, capped by
    truck height), order qty, optional must-load minimum per line.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from scipy.optimize import milp, LinearConstraint, Bounds

MODES = ["Maximise cases", "Priority order", "Proportional (fair share)", "Maximise payload (kg)"]


@dataclass
class Truck:
    name: str
    body: str
    length: float      # usable, after clearances (mm)
    width: float
    height: float
    payload_kg: float

    @property
    def volume_m3(self) -> float:
        return self.length * self.width * self.height / 1e9


@dataclass
class Orientation:
    name: str          # "Lengthwise" / "Turned 90°"
    depth: float       # row depth along the truck (mm)
    lane_w: float      # width of one lane across the truck (mm)
    lanes: int
    layers: int

    @property
    def cpr(self) -> int:   # cases per row
        return self.lanes * self.layers


@dataclass
class Line:
    idx: int
    category: str
    qty: int
    priority: int
    must_load: int
    weight: float
    L: float
    W: float
    H: float
    norm_layers: int
    orients: list = field(default_factory=list)
    drop: str = ""
    stop: int = 0

    @property
    def fits(self) -> bool:
        return any(o.cpr > 0 for o in self.orients)


def usable_truck(row: pd.Series, clear_l: float, clear_w: float, clear_h: float) -> Truck:
    return Truck(str(row["Truck type"]), str(row["Body type"]),
                 float(row["Internal length (mm)"]) - clear_l,
                 float(row["Internal width (mm)"]) - clear_w,
                 float(row["Usable stack height (mm)"]) - clear_h,
                 float(row["Capacity (MT)"]) * 1000)


def build_lines(order: pd.DataFrame, norms: pd.DataFrame, truck: Truck) -> list[Line]:
    nm = norms.set_index("Category")
    lines = []
    for i, r in order.reset_index(drop=True).iterrows():
        cat = r.get("Category")
        qty = int(r.get("Order qty (cases)") or 0) if pd.notna(r.get("Order qty (cases)")) else 0
        if not isinstance(cat, str) or cat not in nm.index or qty <= 0:
            continue
        n = nm.loc[cat]
        L, W, H = float(n["Case length (mm)"]), float(n["Case width (mm)"]), float(n["Case height (mm)"])
        layers = max(0, min(int(truck.height // H), int(n["Stacking layers"])))
        pr = r.get("Priority")
        ml = r.get("Must-load min (cases)")
        ln = Line(i, cat, qty, int(pr) if pd.notna(pr) else 99, int(ml) if pd.notna(ml) else 0,
                  float(n["Case weight (kg)"]), L, W, H, int(n["Stacking layers"]))
        ln.drop = str(r.get("Drop")) if pd.notna(r.get("Drop", None)) else ""
        sq = pd.to_numeric(r.get("Stop sequence", None), errors="coerce")
        ln.stop = int(sq) if pd.notna(sq) else 0
        ln.orients = [Orientation("Lengthwise", L, W, int(truck.width // W), layers),
                      Orientation("Turned 90°", W, L, int(truck.width // L), layers)]
        lines.append(ln)
    return lines


def _solve(lines, truck, c, extra_A=None, extra_lb=None, extra_ub=None, lb_x=None, time_limit=20):
    """Variables: x_i (cases), rA_i, rB_i (rows per orientation). Minimise c @ v."""
    n = len(lines)
    nv = 3 * n
    A, lo, hi = [], [], []
    for i, ln in enumerate(lines):          # x_i <= cprA*rA + cprB*rB
        row = np.zeros(nv); row[i] = 1
        row[n + i] = -ln.orients[0].cpr; row[2 * n + i] = -ln.orients[1].cpr
        A.append(row); lo.append(-np.inf); hi.append(0)
    row = np.zeros(nv)                      # floor length
    for i, ln in enumerate(lines):
        row[n + i] = ln.orients[0].depth; row[2 * n + i] = ln.orients[1].depth
    A.append(row); lo.append(-np.inf); hi.append(truck.length)
    row = np.zeros(nv)                      # payload
    for i, ln in enumerate(lines):
        row[i] = ln.weight
    A.append(row); lo.append(-np.inf); hi.append(truck.payload_kg)
    if extra_A is not None:
        for r_, l_, u_ in zip(extra_A, extra_lb, extra_ub):
            A.append(r_); lo.append(l_); hi.append(u_)
    lb = np.zeros(nv); ub = np.full(nv, np.inf)
    for i, ln in enumerate(lines):
        ub[i] = ln.qty if ln.fits else 0
        lb[i] = min(ln.must_load, ln.qty) if ln.fits else 0
        if lb_x is not None:
            lb[i] = max(lb[i], min(lb_x[i], ub[i]))
        for k, o in enumerate(ln.orients):
            ub[(k + 1) * n + i] = 0 if o.cpr == 0 else math.ceil(ln.qty / o.cpr)
    res = milp(c, constraints=LinearConstraint(np.array(A), lo, hi), integrality=np.ones(nv),
               bounds=Bounds(lb, ub), options={"time_limit": time_limit, "mip_rel_gap": 0})
    if res.x is None:
        return None, res.message
    return np.round(res.x).astype(int), res.message


def optimise(lines: list[Line], truck: Truck, mode: str) -> dict:
    n = len(lines)
    if n == 0:
        return {"ok": False, "message": "No valid order lines."}
    nv = 3 * n
    tie = np.zeros(nv)                                   # tie-break: least floor length
    for i, ln in enumerate(lines):
        tie[n + i] = ln.orients[0].depth * 1e-6; tie[2 * n + i] = ln.orients[1].depth * 1e-6
    xs = np.zeros(nv); xs[:n] = 1
    v = None; msg = ""
    if mode == "Maximise cases":
        v, msg = _solve(lines, truck, -xs + tie)
    elif mode == "Maximise payload (kg)":
        c = np.zeros(nv); c[:n] = -np.array([ln.weight for ln in lines]) / 10
        v, msg = _solve(lines, truck, c + tie)
    elif mode == "Priority order":                       # lexicographic by priority level
        eA, eL, eU = [], [], []
        for p in sorted({ln.priority for ln in lines}):
            mask = np.array([1.0 if ln.priority == p else 0.0 for ln in lines])
            c = np.zeros(nv); c[:n] = -mask
            v, msg = _solve(lines, truck, c + tie, eA, eL, eU)
            if v is None:
                break
            row = np.zeros(nv); row[:n] = mask
            eA.append(row); eL.append(float(mask @ v[:n])); eU.append(np.inf)
        if v is not None:
            v, msg = _solve(lines, truck, -xs + tie, eA, eL, eU)
    elif mode == "Proportional (fair share)":
        qty = np.array([ln.qty if ln.fits else 0 for ln in lines], float)
        feas = lambda f: _solve(lines, truck, tie, lb_x=np.floor(f * qty).astype(int), time_limit=5)[0] is not None
        if feas(1.0):
            f = 1.0
        else:                                            # bisection on fill fraction f
            lo_, hi_ = 0.0, 1.0
            for _ in range(16):
                mid = (lo_ + hi_) / 2
                lo_, hi_ = (mid, hi_) if feas(mid) else (lo_, mid)
            f = lo_
        v, msg = _solve(lines, truck, -xs + tie, lb_x=np.floor(f * qty).astype(int))
        msg = f"fair share {f:.1%}; " + str(msg)
    if v is None:
        return {"ok": False, "message": "No feasible load - check must-load minimums against truck capacity. " + str(msg)}
    return {"ok": True, "x": v[:n], "rA": v[n:2 * n], "rB": v[2 * n:], "message": str(msg)}


def build_stacks(lines, truck, sol, by_stop: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Blocks from the cab to the doors; returns (blocks, stacks).
    Default: heaviest case first (nearest the cab). by_stop=True (multi-drop): last stop loaded first, first stop at the doors."""
    stop = lambda ln: ln.stop if by_stop else 0
    order = sorted(range(len(lines)), key=lambda i: (-stop(lines[i]), -lines[i].weight))
    blocks, stacks = [], []
    x0 = 0.0
    for i in order:
        ln = lines[i]; left = int(sol["x"][i])
        for k, o in enumerate(ln.orients):
            rows_avail = int(sol["rA"][i] if k == 0 else sol["rB"][i])
            if left <= 0 or rows_avail <= 0 or o.cpr == 0:
                continue
            cases = min(left, rows_avail * o.cpr); left -= cases
            rows = math.ceil(cases / o.cpr)
            blocks.append(dict(Stop=stop(ln), Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, Orientation=o.name, Cases=cases, Rows=rows,
                               Lanes=o.lanes, Layers=o.layers, **{"Cases per row": o.cpr, "Row depth (mm)": o.depth,
                                                                   "From (mm)": x0, "To (mm)": x0 + rows * o.depth}))
            rem = cases
            for r in range(rows):
                for lane in range(o.lanes):
                    if rem <= 0:
                        break
                    nlay = min(o.layers, rem); rem -= nlay
                    stacks.append(dict(Stop=stop(ln), Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, x=x0 + r * o.depth, y=lane * o.lane_w,
                                       dx=o.depth, dy=o.lane_w, layers=nlay, h=ln.H, z=nlay * ln.H, cases=nlay))
            x0 += rows * o.depth
    return pd.DataFrame(blocks), pd.DataFrame(stacks)


def summarise(lines, truck, sol) -> tuple[pd.DataFrame, dict]:
    rows = []
    for i, ln in enumerate(lines):
        x = int(sol["x"][i]); rA, rB = int(sol["rA"][i]), int(sol["rB"][i])
        oA, oB = ln.orients
        usedA = min(x, rA * oA.cpr) if oA.cpr else 0
        usedB = x - usedA
        rowsA = math.ceil(usedA / oA.cpr) if oA.cpr and usedA else 0
        rowsB = math.ceil(usedB / oB.cpr) if oB.cpr and usedB else 0
        length = rowsA * oA.depth + rowsB * oB.depth
        orient = " + ".join(n for n, u in (("Lengthwise", usedA), ("Turned 90°", usedB)) if u) or "-"
        rows.append({"Line": ln.idx + 1, "Drop": ln.drop, "Stop": ln.stop, "Category": ln.category, "Order qty": ln.qty, "Priority": ln.priority,
                     "Loaded cases": x, "Not loaded": ln.qty - x, "% loaded": x / ln.qty if ln.qty else 0,
                     "Orientation": orient if ln.fits else "Does not fit", "Layers": ln.orients[0].layers,
                     "Rows": rowsA + rowsB, "Length (mm)": length, "Weight (kg)": x * ln.weight,
                     "Volume (m³)": x * ln.L * ln.W * ln.H / 1e9})
    df = pd.DataFrame(rows)
    tot_len = df["Length (mm)"].sum(); tot_wt = df["Weight (kg)"].sum()
    k = {"cases": int(df["Loaded cases"].sum()), "ordered": int(df["Order qty"].sum()),
         "left": int(df["Not loaded"].sum()), "weight_kg": tot_wt,
         "payload_util": tot_wt / truck.payload_kg, "length_mm": tot_len, "floor_util": tot_len / truck.length,
         "volume_util": df["Volume (m³)"].sum() / truck.volume_m3}
    k["utilisation"] = max(k["payload_util"], k["floor_util"])
    k["limited_by"] = ("Full order" if k["left"] == 0 else
                       "Floor space" if k["floor_util"] >= k["payload_util"] else "Payload")
    return df, k


def plan_trucks(order, norms, truck, mode, max_trucks=20, by_stop=False):
    """Split the whole order over successive trucks of the same type."""
    remaining = order.copy()
    plans = []
    for t in range(max_trucks):
        lines = build_lines(remaining, norms, truck)
        if not lines or all(not ln.fits for ln in lines):
            break
        for ln in lines:            # must-load applies to first truck only
            if t > 0:
                ln.must_load = 0
        sol = optimise(lines, truck, mode)
        if not sol["ok"] or sol["x"].sum() == 0:
            break
        df, k = summarise(lines, truck, sol)
        blocks, stacks = build_stacks(lines, truck, sol, by_stop)
        plans.append(dict(truck=truck, lines=lines, sol=sol, table=df, kpi=k, blocks=blocks, stacks=stacks))
        loaded = {ln.idx: int(sol["x"][i]) for i, ln in enumerate(lines)}
        remaining = remaining.reset_index(drop=True).copy()
        for idx, x in loaded.items():
            remaining.loc[idx, "Order qty (cases)"] = int(remaining.loc[idx, "Order qty (cases)"]) - x
            remaining.loc[idx, "Must-load min (cases)"] = 0
        if remaining["Order qty (cases)"].fillna(0).sum() <= 0:
            break
    return plans


def single_category_max(norms: pd.DataFrame, trucks: pd.DataFrame, clear) -> pd.DataFrame:
    """Loadability matrix: max cases of one category per truck (by volume & weight)."""
    out = {}
    for _, t in trucks.iterrows():
        tr = usable_truck(t, *clear)
        col = {}
        for _, n in norms.iterrows():
            o = pd.DataFrame([{"Category": n["Category"], "Order qty (cases)": 10 ** 6, "Priority": 1}])
            ln = build_lines(o, norms, tr)[0]
            by_vol = max(int(tr.length // oo.depth) * oo.cpr for oo in ln.orients)
            by_wt = int(tr.payload_kg // ln.weight)
            col[n["Category"]] = min(by_vol, by_wt)
        out[tr.name] = col
    return pd.DataFrame(out)


# ----------------------------------------------------------------- vehicle recommendation
def plan_to_order(plan, min_load: bool = False) -> pd.DataFrame:
    rows = []
    for i, ln in enumerate(plan["lines"]):
        x = int(plan["sol"]["x"][i])
        if x > 0:
            rows.append({"Drop": ln.drop or None, "Stop sequence": ln.stop or None, "Category": ln.category, "Order qty (cases)": x,
                         "Priority": ln.priority, "Must-load min (cases)": x if min_load else None})
    return pd.DataFrame(rows, columns=["Drop", "Stop sequence", "Category", "Order qty (cases)", "Priority", "Must-load min (cases)"])


def _make_plan(order, norms, truck, by_stop):
    lines = build_lines(order, norms, truck)
    sol = optimise(lines, truck, "Maximise cases")
    if not sol["ok"]:
        return None
    df, k = summarise(lines, truck, sol)
    blocks, stacks = build_stacks(lines, truck, sol, by_stop)
    return dict(truck=truck, lines=lines, sol=sol, table=df, kpi=k, blocks=blocks, stacks=stacks)


def consolidate(plans, norms, trucks_df, clear, by_stop=False, min_util=0.85, rounds=6):
    """Empty poorly-used trucks into spare space on the others; else move them to a better-sized truck."""
    for _ in range(rounds):
        weak = sorted([p for p in plans if p["kpi"]["utilisation"] < min_util], key=lambda p: p["kpi"]["utilisation"])
        if not weak or len(plans) < 2:
            break
        p = weak[0]
        left = plan_to_order(p)
        others = sorted([q for q in plans if q is not p], key=lambda q: q["kpi"]["utilisation"])
        new_others = []
        for q in others:
            if left["Order qty (cases)"].sum() <= 0:
                new_others.append(q); continue
            combined = pd.concat([plan_to_order(q, min_load=True), left], ignore_index=True)
            nq = _make_plan(combined, norms, q["truck"], by_stop)
            if nq is None:
                new_others.append(q); continue
            got = {}
            for i, ln in enumerate(nq["lines"]):
                got[ln.idx] = int(nq["sol"]["x"][i])
            base = len(combined) - len(left)
            for j in range(len(left)):
                taken = got.get(base + j, 0)
                left.loc[left.index[j], "Order qty (cases)"] -= taken
            new_others.append(nq)
        left = left[left["Order qty (cases)"] > 0]
        if left.empty:
            plans = new_others
            continue
        single = best_single_truck(left, norms, trucks_df, clear, by_stop)
        if single is not None and single["kpi"]["utilisation"] > p["kpi"]["utilisation"] + 0.01:
            plans = new_others + [single]
        else:
            break
    return plans


def recommend_fleet(order: pd.DataFrame, norms: pd.DataFrame, trucks_df: pd.DataFrame, clear, vehicle_penalty_t: float = 4.0,
                    by_stop: bool = False, time_limit: int = 8, min_util: float = 0.85) -> dict:
    """Runs the fleet model with small capacity safety margins and keeps the best realised plan."""
    best = None
    for derate in (1.0, 0.98, 0.96, 0.93):
        r = _recommend_once(order, norms, trucks_df, clear, vehicle_penalty_t, by_stop, time_limit, derate, min_util)
        if not r["ok"]:
            if best is None:
                best = r
            continue
        r["score"] = sum(p["truck"].payload_kg / 1000 + vehicle_penalty_t for p in r["plans"]) + (0 if r["planned"] == r["ordered"] else 1e6)
        if best is None or not best.get("ok") or r["score"] < best["score"] - 1e-6:
            best = r
        if all(p["kpi"]["utilisation"] >= min_util for p in r["plans"][:-1]) and min(p["kpi"]["utilisation"] for p in r["plans"]) >= 0.5:
            break
    return best


def _recommend_once(order, norms, trucks_df, clear, vehicle_penalty_t, by_stop, time_limit, derate, min_util) -> dict:
    """Choose the truck mix that carries the whole order with the least deployed capacity.

    Objective: minimise  sum_t n_t * (capacity_t in tonnes + vehicle_penalty_t).
      - capacity term  -> right-sizing: no oversized trucks (higher utilisation)
      - penalty term   -> discourages many very small trucks (fewer vehicles)
    Aggregated MILP per truck type (floor length and payload x number of trucks), then each
    type's share is split into individual trucks with the exact single-truck optimiser.
    """
    trucks_df = trucks_df.reset_index(drop=True)
    tl = [usable_truck(r, *clear) for _, r in trucks_df.iterrows()]
    per_t = [build_lines(order, norms, t) for t in tl]
    if not tl or not per_t[0]:
        return {"ok": False, "message": "No valid order lines or trucks."}
    L0 = per_t[0]; n, T = len(L0), len(tl)
    nofit = [ln.category for i, ln in enumerate(L0) if not any(per_t[t][i].fits for t in range(T))]
    if nofit:
        return {"ok": False, "message": "These categories do not fit any allowed truck: " + ", ".join(nofit)}
    X = lambda i, t: i * T + t
    RA = lambda i, t: n * T + i * T + t
    RB = lambda i, t: 2 * n * T + i * T + t
    NV = lambda t: 3 * n * T + t
    nv = 3 * n * T + T
    A, lo, hi = [], [], []
    for i in range(n):                                   # every case loaded
        r = np.zeros(nv)
        for t in range(T):
            r[X(i, t)] = 1
        A.append(r); lo.append(L0[i].qty); hi.append(L0[i].qty)
    for i in range(n):                                   # cases <= rows x cases/row
        for t in range(T):
            o = per_t[t][i].orients
            r = np.zeros(nv); r[X(i, t)] = 1; r[RA(i, t)] = -o[0].cpr; r[RB(i, t)] = -o[1].cpr
            A.append(r); lo.append(-np.inf); hi.append(0)
    for t in range(T):                                   # floor + payload per truck type
        r = np.zeros(nv); w = np.zeros(nv)
        for i in range(n):
            o = per_t[t][i].orients
            r[RA(i, t)] = o[0].depth; r[RB(i, t)] = o[1].depth; w[X(i, t)] = L0[i].weight
        r[NV(t)] = -tl[t].length * derate; w[NV(t)] = -tl[t].payload_kg * derate
        A += [r, w]; lo += [-np.inf, -np.inf]; hi += [0, 0]
    lb = np.zeros(nv); ub = np.full(nv, np.inf)
    tot_w = sum(ln.qty * ln.weight for ln in L0)
    for t in range(T):
        for i in range(n):
            o = per_t[t][i].orients
            if not per_t[t][i].fits:
                ub[X(i, t)] = 0
            for k, idx in ((0, RA(i, t)), (1, RB(i, t))):
                ub[idx] = 0 if o[k].cpr == 0 else math.ceil(L0[i].qty / o[k].cpr)
        ub[NV(t)] = 2 + math.ceil(max(tot_w / tl[t].payload_kg, sum(math.ceil(L0[i].qty / max(1, max(o.cpr for o in per_t[t][i].orients)))
                                                                     * min(o.depth for o in per_t[t][i].orients) for i in range(n)) / tl[t].length))
    c = np.zeros(nv)
    for t in range(T):
        c[NV(t)] = tl[t].payload_kg / 1000 + vehicle_penalty_t
    res = milp(c, constraints=LinearConstraint(np.array(A), lo, hi), integrality=np.ones(nv), bounds=Bounds(lb, ub),
               options={"time_limit": time_limit, "mip_rel_gap": 0.002})
    if res.x is None:
        return {"ok": False, "message": str(res.message)}
    v = np.round(res.x).astype(int)
    plans = []
    src = order.reset_index(drop=True)
    for t in range(T):
        if v[NV(t)] == 0:
            continue
        sub = src.copy()
        sub["Order qty (cases)"] = 0
        for i, ln in enumerate(L0):
            sub.loc[ln.idx, "Order qty (cases)"] = v[X(i, t)]
        sub["Must-load min (cases)"] = None
        sub = sub[sub["Order qty (cases)"] > 0]
        ps = plan_trucks(sub, norms, tl[t], "Maximise cases", max_trucks=v[NV(t)] + 3, by_stop=by_stop)
        plans += ps
    plans = consolidate(plans, norms, trucks_df, clear, by_stop, min_util)
    plans.sort(key=lambda p: -p["truck"].payload_kg)
    planned = sum(p["kpi"]["cases"] for p in plans)
    return {"ok": True, "plans": plans, "planned": planned, "ordered": sum(ln.qty for ln in L0), "message": str(res.message)}


def best_single_truck(order, norms, trucks_df, clear, by_stop=False):
    """Smallest-capacity truck (best utilisation) that carries the whole order in one vehicle."""
    best = None
    for _, r in trucks_df.sort_values("Capacity (MT)").iterrows():
        t = usable_truck(r, *clear)
        lines = build_lines(order, norms, t)
        if not lines or any(not ln.fits for ln in lines):
            continue
        sol = optimise(lines, t, "Maximise cases")
        if sol["ok"] and int(sol["x"].sum()) == sum(ln.qty for ln in lines):
            df, k = summarise(lines, t, sol)
            if best is None or k["utilisation"] > best["kpi"]["utilisation"] + 1e-9:
                blocks, stacks = build_stacks(lines, t, sol, by_stop)
                best = dict(truck=t, lines=lines, sol=sol, table=df, kpi=k, blocks=blocks, stacks=stacks)
    return best


def top_up_hint(plan, norms) -> str:
    """How many more cases of each loaded category would still fit (floor + payload)."""
    t, k = plan["truck"], plan["kpi"]
    spare_len, spare_kg = t.length - k["length_mm"], t.payload_kg - k["weight_kg"]
    hints = []
    for ln in plan["lines"]:
        o = max(ln.orients, key=lambda o: o.cpr / o.depth if o.depth else 0)
        if o.cpr == 0:
            continue
        add = min(int(spare_len // o.depth) * o.cpr, int(spare_kg // ln.weight))
        if add > 0:
            hints.append(f"{add} × {ln.category}")
    return ("Room for about " + ", or ".join(hints[:3])) if hints else "Truck is full"
