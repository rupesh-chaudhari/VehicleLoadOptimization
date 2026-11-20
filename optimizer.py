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

CONFIG = {"stable": True, "level_mm": None, "_level": None, "level_reserve": 0.04}


def _floor(t) -> float:
    """Floor length the planners may fill: stable mode keeps a small reserve so the layout can level the load."""
    return t.length * (1 - CONFIG["level_reserve"]) if CONFIG["stable"] else t.length   # stable (transit-safe) loading: one orientation per line, full-width rows, level height

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
        T = CONFIG.get("_level") if CONFIG["stable"] else None
        if T and layers:                                        # level load: no stack above the common level (+ tolerance)
            layers = max(1, min(layers, int((T + LEVEL_TOL) // H)))
        pr = r.get("Priority")
        ml = r.get("Must-load min (cases)")
        ln = Line(i, cat, qty, int(pr) if pd.notna(pr) else 99, int(ml) if pd.notna(ml) else 0,
                  float(n["Case weight (kg)"]), L, W, H, int(n["Stacking layers"]))
        ln.drop = str(r.get("Drop")) if pd.notna(r.get("Drop", None)) else ""
        sq = pd.to_numeric(r.get("Stop sequence", None), errors="coerce")
        ln.stop = int(sq) if pd.notna(sq) else 0
        ln.orients = [Orientation("Lengthwise", L, W, int(truck.width // W), layers),
                      Orientation("Turned 90°", W, L, int(truck.width // L), layers)]
        if CONFIG["stable"]:
            keep = _stable_orientation(ln.orients, truck.width)
            for k, o in enumerate(ln.orients):
                if k != keep:
                    o.lanes = 0
        lines.append(ln)
    return lines


def level_height(order: pd.DataFrame, norms: pd.DataFrame) -> float | None:
    """Common load height for stable loading: CONFIG['level_mm'] if set, else the case-weighted median of the
    lines' maximum stack heights (layers norm x case height). Lines that cannot reach it are tiered or flagged."""
    if CONFIG.get("level_mm"):
        return float(CONFIG["level_mm"])
    nm = norms.set_index("Category"); hs = []
    for _, r in order.iterrows():
        c, q = r.get("Category"), pd.to_numeric(r.get("Order qty (cases)"), errors="coerce")
        if isinstance(c, str) and c in nm.index and pd.notna(q) and q > 0:
            hs.append((float(nm.loc[c, "Stacking layers"]) * float(nm.loc[c, "Case height (mm)"]), float(q)))
    if not hs:
        return None
    hs.sort(); half = sum(q for _, q in hs) / 2; acc = 0
    for h, q in hs:
        acc += q
        if acc >= half:
            return h
    return hs[-1][0]


def _set_level(order, norms):
    CONFIG["_level"] = level_height(order, norms) if CONFIG["stable"] else None


def _stable_orientation(orients, width) -> int:
    """One orientation per category: densest, unless the other is within 3% and fills the width better."""
    dens = [o.cpr / o.depth if o.depth and o.cpr else 0 for o in orients]
    gap = [width - o.lanes * o.lane_w if o.lanes else width for o in orients]
    best = int(np.argmax(dens)); other = 1 - best
    if dens[other] >= 0.97 * dens[best] and gap[other] < gap[best]:
        best = other
    return best


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
    A.append(row); lo.append(-np.inf); hi.append(_floor(truck))
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
    if CONFIG["stable"]:
        return build_stacks_stable(lines, truck, sol, by_stop)
    return build_stacks_cube(lines, truck, sol, by_stop)


def _layout_rows(n_cases, lanes, layers, layers_max):
    """Full-width rows: returns (rows, stack layer list per row, note, floor_gap)."""
    per_row = lanes * layers
    full, r = divmod(n_cases, per_row)
    rows = [[layers] * lanes for _ in range(full)]
    note, gap = "", False
    if r:
        if full >= 1 and r <= lanes and layers + 1 <= layers_max:     # small leftover: one more layer on the centre stacks
            for k in _centre_first(lanes)[:r]:
                rows[-1][k] += 1
            note = f"Top-up: {r} extra cases as one more layer on the centre stacks of the last row"
        elif full >= 1 and r < per_row / 2:                         # spread last full row + leftover evenly over 2 rows
            tot = per_row + r
            base, extra = divmod(tot, 2 * lanes)
            two = [base] * (2 * lanes)
            for k in sorted(range(2 * lanes), key=lambda q: (abs((q % lanes) - (lanes - 1) / 2), q))[:extra]:
                two[k] += 1
            rows[-1] = two[:lanes]; rows.append(two[lanes:])
            note = f"Last 2 rows: {tot} cases spread evenly over all {lanes} lanes ({base}{'–' + str(base + 1) if extra else ''} high) - no floor gaps"
        elif r >= lanes:                                             # last row across the full width, part layer on top
            base, extra = divmod(r, lanes)
            last = [base] * lanes
            for k in _centre_first(lanes)[:extra]:
                last[k] += 1
            rows.append(last)
            note = f"Last row: {r} cases across all {lanes} lanes ({base}{'–' + str(base + 1) if extra else ''} high, part layer on top)"
        else:                                                        # whole line smaller than one layer
            last = [0] * lanes
            for k in _centre_first(lanes)[:r]:
                last[k] = 1
            rows.append(last); gap = True
            note = f"Only {r} cases (less than one layer) - fill the gap with dunnage, or round the order to {lanes} cases"
    return rows, note, gap


def _centre_first(n):
    mid = (n - 1) / 2
    return sorted(range(n), key=lambda k: (abs(k - mid), k))


def _level_rows(x, lanes, H, layers_max, T):
    """Rows for one line at target level height T: the row count whose even spread is closest to T
    (never above the stacking norm). Returns (rows, avg height)."""
    r_min = -(-x // (lanes * layers_max))
    r_max = max(r_min, -(-x // lanes))                        # at least ~1 case per stack
    best = min(range(r_min, r_max + 1), key=lambda r: (abs(x / (r * lanes) * H - T), r))
    return best, x / (best * lanes) * H


def _spread(x, rows, lanes):
    """Spread x cases evenly over rows x lanes stacks: every stack base or base+1 high.
    The +1 stacks fill whole rows from the cab side first, so each row is level across the width."""
    n = rows * lanes
    base, extra = divmod(x, n)
    grid = [[base] * lanes for _ in range(rows)]
    full, part = divmod(extra, lanes)
    for r in range(full):
        grid[r] = [base + 1] * lanes
    if part:
        for k in _centre_first(lanes)[:part]:
            grid[full][k] += 1
    return grid, base, extra


LEVEL_TOL = 150   # mm: allowed height difference across the load (about one case)


def _solo_opts(x, o, H):
    """Row options for a line on its own: (rows, low stack height, high stack height, length)."""
    if x < o.lanes:
        return [(1, None, None, o.depth)]
    r_min = -(-x // (o.lanes * o.layers)); r_max = max(r_min, x // o.lanes)
    out = []
    for r in range(r_min, r_max + 1):
        base, extra = divmod(x, r * o.lanes)
        out.append((r, base * H, (base + (1 if extra else 0)) * H, r * o.depth))
    return out


def _solo_seg(ln, o, x, rows, stop):
    return dict(stop=stop, kind="solo", parts=[dict(ln=ln, o=o, x=x, rows=rows, z0=0.0)], length=rows * o.depth)


def _seg_heights(seg):
    if seg["kind"] == "solo":
        p = seg["parts"][0]
        if p["x"] < p["o"].lanes:
            return []
        base, extra = divmod(p["x"], p["rows"] * p["o"].lanes)
        return [base * p["ln"].H] + ([(base + 1) * p["ln"].H] if extra else [])
    b, t = seg["parts"]
    base, extra = divmod(t["x"], t["rows"] * t["o"].lanes)
    return [t["z0"] + base * t["ln"].H] + ([t["z0"] + (base + 1) * t["ln"].H] if extra else [])


def _score(segs, truck):
    length = sum(g["length"] for g in segs)
    if length > truck.length + 1e-6:
        return None
    hs = [h for g in segs for h in _seg_heights(g)]
    return ((max(hs) - min(hs)) if hs else 0) + 0.05 * (truck.length - length)


def _plan_window(items, truck, stopf, fixed_h=(), fixed_len=0.0):
    """Lines on their own, in the narrowest common height window that fits the body length
    (fixed_h / fixed_len: heights and floor length of tiered columns already placed)."""
    opts = [_solo_opts(x, o, ln.H) for ln, x, o in items]
    L = truck.length - fixed_len + 1e-6
    fmin, fmax = (min(fixed_h), max(fixed_h)) if fixed_h else (float("inf"), 0.0)
    los = sorted({p[1] for op in opts for p in op if p[1] is not None and p[1] <= fmin} | ({fmin} if fixed_h else set()))
    his = sorted({p[2] for op in opts for p in op if p[2] is not None and p[2] >= fmax} | ({fmax} if fixed_h else set()))
    best = None
    for lo in los:
        for hi in his:
            if hi < lo or (best and hi - lo >= best[0]):
                continue
            pick = []
            for op in opts:
                ok = [p for p in op if p[1] is None or (p[1] >= lo - 1e-6 and p[2] <= hi + 1e-6)]
                if not ok:
                    break
                pick.append(min(ok, key=lambda p: p[3]))
            else:
                if sum(p[3] for p in pick) <= L:
                    best = (hi - lo, pick, lo, hi)
    if best is None:
        rows = [op[0][0] for op in opts]
    else:
        _, pick, lo, hi = best
        rows = [p[0] for p in pick]
        used = sum(p[3] for p in pick); changed = True
        while changed:                                          # spare floor: lower the tallest lines inside the window
            changed = False
            for k in sorted(range(len(items)), key=lambda k: -(pick[k][2] or 0)):
                op = [p for p in opts[k] if p[0] == rows[k] + 1 and p[1] is not None and p[1] >= lo - 1e-6 and p[2] <= hi + 1e-6]
                if op and used + op[0][3] - pick[k][3] <= L:
                    used += op[0][3] - pick[k][3]; pick[k] = op[0]; rows[k] += 1; changed = True
    return [_solo_seg(ln, o, x, r, stopf(ln)) for (ln, x, o), r in zip(items, rows)]


def _can_stand(x, ln, o, lo, T):
    """True if x cases of a line can stand on their own inside [lo, T] (0 cases is fine)."""
    if x == 0:
        return True
    return any(p[1] is not None and p[1] >= lo - ln.H - 1e-6 and p[2] <= T + 1e-6 for p in _solo_opts(x, o, ln.H))


def _tier_options(B, U, xb, xu, T, lo, Hmax, short_is_top=True):
    """Two-tier column inside one stop: base line B (flat: every stack exactly l_b high) with line U on a tier sheet
    on top. Yields (score, l_b, l_u, rows_b, n_b, rows_u, n_u). Leftovers of both lines must be able to stand at
    the level on their own (or be zero)."""
    (lb, ob), (lu, ou) = B, U
    ab, au = ob.lane_w * ob.depth, ou.lane_w * ou.depth
    for l_b in range(1, ob.layers + 1):
        cap = (lb.norm_layers - l_b) * lb.weight / ab               # extra kg per mm2 the base case may carry (stacking norm)
        z0 = l_b * lb.H
        rows_b_max = xb // (ob.lanes * l_b)
        if rows_b_max < 1:
            continue
        for l_u in range(ou.layers, 0, -1):
            top = z0 + l_u * lu.H
            if l_u * lu.weight / au > cap + 1e-12 or top > min(T, Hmax) + 1e-6 or z0 + (l_u - 1) * lu.H < lo - 1e-6 - lu.H:
                continue
            for rows_b in range(1, rows_b_max + 1):
                n_b = rows_b * ob.lanes * l_b
                rows_u = int(rows_b * ob.depth // ou.depth)               # top tier rows that sit fully on the base
                if rows_u < 1:
                    continue
                if rows_b * ob.depth - rows_u * ou.depth > 0.5 * max(ou.depth, ob.depth):  # uncovered strip too long
                    continue
                n_u = min(xu, rows_u * ou.lanes * l_u)
                if n_u < rows_u * ou.lanes:                                 # top tier must cover the base (>= 1 case per stack)
                    continue
                base_u, extra = divmod(n_u, rows_u * ou.lanes)
                h_lo, h_hi = z0 + base_u * lu.H, z0 + (base_u + (1 if extra else 0)) * lu.H
                if h_lo < lo - 1e-6 or h_hi > T + 1e-6:
                    continue
                lb_left, lu_left = xb - n_b, xu - n_u
                bad = (not _can_stand(lb_left, lb, ob, lo, T)) + (not _can_stand(lu_left, lu, ou, lo, T))
                used_short = n_u if short_is_top else n_b
                yield ((bad, round(abs(T - h_hi) / 60), -used_short, rows_b * ob.depth), l_b, l_u, rows_b, int(n_b), rows_u, int(n_u))


def _plan_tiered(items, truck, stopf, T, tol=LEVEL_TOL):
    """Level height T for the whole load. A line that cannot reach T on its own (low stacking norm) is combined
    with another line of the SAME stop as a two-tier column (tier sheet between), within the crush limit of the
    base case - either on top of it or as its flat base. Stops are never mixed."""
    lo = T - tol
    avail = {k: x for k, (ln, x, o) in enumerate(items)}
    short = [k for k, (ln, x, o) in enumerate(items) if x >= o.lanes and o.layers * ln.H < lo]
    segs, used = [], set()
    for t in sorted(short, key=lambda k: -items[k][1]):
      lt, _, ot = items[t]
      for _once in range(1):                                    # one two-tier column per short line (simple for loaders)
        best = None
        for u, (lu, _, ou) in enumerate(items):
            if u == t or u in short or avail[u] <= 0 or stopf(lu) != stopf(lt):
                continue
            for top_short in (True, False):
                B, U = ((lu, ou), (lt, ot)) if top_short else ((lt, ot), (lu, ou))
                xb, xu = (avail[u], avail[t]) if top_short else (avail[t], avail[u])
                for opt in _tier_options(B, U, xb, xu, T, lo, truck.height, top_short):
                    if best is None or opt[0] < best[0][0]:
                        best = (opt, u, top_short)
        if best is None or best[0][0][0] > 1:
            break
        (sc, l_b, l_u, rows_b, n_b, rows_u, n_u), u, top_short = best
        bk, uk = (u, t) if top_short else (t, u)
        (lb, _, ob), (lu, _, ou) = items[bk], items[uk]
        avail[bk] -= n_b; avail[uk] -= n_u; used.add(u)
        segs.append(dict(stop=stopf(lt), kind="tier", length=rows_b * ob.depth, parts=[
            dict(ln=lb, o=ob, x=n_b, rows=rows_b, z0=0.0, fixed=l_b),
            dict(ln=lu, o=ou, x=n_u, rows=rows_u, z0=l_b * lb.H)]))
    rest = [(ln, avail[k], o) for k, (ln, x, o) in enumerate(items) if avail[k] > 0]
    fixed_h = [h for g in segs for h in _seg_heights(g)]
    fixed_len = sum(g["length"] for g in segs)
    return segs + (_plan_window(rest, truck, stopf, fixed_h, fixed_len) if rest else [])


def build_stacks_stable(lines, truck, sol, by_stop=False):
    """Transit-safe LEVEL layout: one load height cab to doors, full-width rows, one orientation per line, cases spread
    evenly (stacks differ by at most one case), stacks centred, a separator between stops. Low-norm lines (e.g. cans)
    are tiered on a lower base of a line of the same stop so the column reaches the common level."""
    items = []
    for i, ln in enumerate(lines):
        x = int(sol["x"][i])
        o = next((o for o in ln.orients if o.cpr > 0), None)
        if x > 0 and o is not None:
            items.append((ln, x, o))
    if not items:
        return pd.DataFrame(), pd.DataFrame()
    stopf = (lambda ln: ln.stop) if by_stop else (lambda ln: 0)
    best = _plan_window(items, truck, stopf)
    best_sc = _score(best, truck)
    if best_sc is None:
        best_sc = float("inf")
    cands = sorted({p[2] for ln, x, o in items for p in _solo_opts(x, o, ln.H) if p[2]})
    for T in cands:
        segs = _plan_tiered(items, truck, stopf, T)
        sc = _score(segs, truck)
        if sc is not None and sc < best_sc - 100:             # tier only when it clearly levels the load
            best, best_sc = segs, sc
    segs = sorted(best, key=lambda g: (-g["stop"], -max(_seg_heights(g) or [0]), -g["parts"][0]["ln"].weight))

    blocks, stacks = [], []
    x0 = 0.0
    for pos, g in enumerate(segs):
        nxt = segs[pos + 1] if pos + 1 < len(segs) else None
        sep = bool(by_stop and nxt is not None and nxt["stop"] != g["stop"])
        for tier, p in enumerate(g["parts"]):
            ln, o, x, rows, z0 = p["ln"], p["o"], p["x"], p["rows"], p["z0"]
            gap = x < o.lanes
            if gap:
                grid = [[1 if q in _centre_first(o.lanes)[:x] else 0 for q in range(o.lanes)]]
                note = f"Only {x} cases (less than one layer) - fill the gap with dunnage, or round the order to {o.lanes} cases"
            else:
                grid, base, extra = _spread(x, rows, o.lanes)
                if p.get("fixed"):
                    note = (f"BASE TIER: all {rows * o.lanes} stacks exactly {base} high (flat top) - lay a tier sheet "
                            f"(corrugated / ply) over it, then load {g['parts'][1]['ln'].category} on top")
                elif tier == 1:
                    note = (f"TOP TIER on the tier sheet over {g['parts'][0]['ln'].category}: "
                            + (f"all stacks {base} high" if not extra else f"{extra} stacks {base + 1} high (cab side first), rest {base} high"))
                else:
                    note = (f"All {rows * o.lanes} stacks {base} high" if not extra else
                            f"{extra} stacks {base + 1} high (cab side first, full rows), {rows * o.lanes - extra} stacks {base} high")
            y_off = (truck.width - o.lanes * o.lane_w) / 2
            for r, row in enumerate(grid):
                for lane, nl in enumerate(row):
                    if nl:
                        stacks.append(dict(Stop=g["stop"], Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, x=x0 + r * o.depth,
                                           y=y_off + lane * o.lane_w, dx=o.depth, dy=o.lane_w, layers=nl, h=ln.H, z0=z0,
                                           z=z0 + nl * ln.H, cases=nl, kg=nl * ln.weight, tier=tier))
            if sep and tier == len(g["parts"]) - 1:
                note += " · then fix SEPARATOR (corrugated/ply sheet + load bar) before the next stop"
            L_ = max(max(r_) for r_ in grid)
            blocks.append(dict(Stop=g["stop"], Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, Orientation=o.name, Cases=x,
                               Rows=rows, Lanes=o.lanes, Layers=L_, Tier=tier,
                               **{"Cases per row": o.lanes * L_, "Row depth (mm)": o.depth, "Stack height (mm)": round(z0 + L_ * ln.H),
                                  "Side gap each side (mm)": round(y_off), "From (mm)": x0, "To (mm)": x0 + rows * o.depth,
                                  "Note": note, "Floor gap": gap, "Separator after": sep and tier == len(g["parts"]) - 1}))
        x0 += g["length"]
    b = pd.DataFrame(blocks)
    return b, pd.DataFrame(stacks)


def stability_report(plan) -> dict:
    """Transit-stability checks for a laid-out truck."""
    t, b, s = plan["truck"], plan["blocks"], plan["stacks"]
    if b is None or b.empty:
        return {}
    used = b["To (mm)"].max(); void = t.length - used
    full = height_profile(plan)
    side = b["Side gap each side (mm)"].max() if "Side gap each side (mm)" in b else 0
    kg = s["kg"] if "kg" in s else s["cases"]
    cg = float(((s["x"] + s["dx"] / 2) * kg).sum() / kg.sum()) / t.length if kg.sum() else 0
    gaps = int(b["Floor gap"].sum()) if "Floor gap" in b else 0
    rep = dict(height_min=float(full.min()), height_max=float(full.max()), void_mm=float(void), side_gap_mm=float(side), cg_pct=cg,
               floor_gaps=gaps, used_mm=float(used))
    acts = []
    if void > 300:
        acts.append(f"{void / 1000:.1f} m empty at the doors - fit a load-lock bar or dunnage bags against the last row.")
    if side > 75:
        acts.append(f"Side gap up to {side:.0f} mm each side - stacks centred; fill with dunnage / corrugated spacers.")
    if gaps:
        acts.append(f"{gaps} row(s) cannot fill the width - use dunnage or round those lines to full layers (see notes).")
    if rep["height_max"] - rep["height_min"] > 250:
        acts.append(f"Load height varies {rep['height_min']:.0f}–{rep['height_max']:.0f} mm (case heights / stacking norms differ) - "
                    "stretch-wrap the taller block and put a separator sheet at the step.")
    if not 0.35 <= cg <= 0.65:
        acts.append(f"Centre of gravity at {cg:.0%} of the body length - move heavy blocks towards the middle for axle balance.")
    if by_stop_used(b):
        acts.append("Multi-drop: fix a separator (corrugated/ply sheet + load bar) between stops as marked; after each drop, "
                    "move the load bar up to the next stop's face.")
    rep["actions"] = acts
    return rep


def height_profile(plan) -> pd.Series:
    """Load height at the middle of every floor row (top of the column, including any top tier)."""
    b, s = plan["blocks"], plan["stacks"]
    gap_lines = set(b.loc[b["Floor gap"].astype(bool), "Line"]) if "Floor gap" in b else set()
    s = s[~s["Line"].isin(gap_lines)] if len(gap_lines) < s["Line"].nunique() else s
    floor = s[s.get("z0", 0) == 0] if "z0" in s else s
    mids = sorted({round(x + dx / 2) for x, dx in zip(floor["x"], floor["dx"])})
    out = {}
    for m in mids:
        cov = s[(s["x"] <= m) & (s["x"] + s["dx"] >= m)]
        if not cov.empty:
            out[m] = float(cov["z"].max())
    return pd.Series(out, dtype=float)


def by_stop_used(b) -> bool:
    return "Stop" in b and b["Stop"].nunique() > 1 and b["Stop"].max() > 0


def build_stacks_cube(lines, truck, sol, by_stop: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Max-cube layout (no stability rules): blocks from the cab to the doors.
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
            rem_ = cases % o.cpr
            blocks.append(dict(Stop=stop(ln), Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, Orientation=o.name, Cases=cases, Rows=rows,
                               Lanes=o.lanes, Layers=o.layers, **{"Cases per row": o.cpr, "Row depth (mm)": o.depth,
                                                                   "Stack height (mm)": round(o.layers * ln.H),
                                                                   "Side gap each side (mm)": round(truck.width - o.lanes * o.lane_w),
                                                                   "From (mm)": x0, "To (mm)": x0 + rows * o.depth,
                                                                   "Note": f"Last row: {rem_} cases in {-(-rem_ // o.layers)} stacks" if rem_ else "",
                                                                   "Floor gap": bool(rem_ and rem_ < o.cpr - o.layers)}))
            rem = cases
            for r in range(rows):
                for lane in range(o.lanes):
                    if rem <= 0:
                        break
                    nlay = min(o.layers, rem); rem -= nlay
                    stacks.append(dict(Stop=stop(ln), Drop=ln.drop, Line=ln.idx + 1, Category=ln.category, x=x0 + r * o.depth, y=lane * o.lane_w,
                                       dx=o.depth, dy=o.lane_w, layers=nlay, h=ln.H, z=nlay * ln.H, cases=nlay, kg=nlay * ln.weight))
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
    """One truck: optimise with the chosen objective. Several trucks: estimate the count by filling trucks in
    turn, then re-assign lines across that many trucks to avoid small split pieces (stable, easy to load)."""
    _set_level(order, norms)
    seq = _plan_sequential(order, norms, truck, mode, max_trucks, by_stop)
    if max_trucks <= 1 or len(seq) <= 1:
        return seq
    asg = assign_to_trucks(order, norms, [truck] * len(seq), by_stop)
    if asg["short"]:
        asg2 = assign_to_trucks(order, norms, [truck] * min(max_trucks, len(seq) + 1), by_stop)
        if not asg2["short"]:
            asg = asg2
    return asg["plans"] if sum(p["kpi"]["cases"] for p in asg["plans"]) >= sum(p["kpi"]["cases"] for p in seq) else seq


def _plan_sequential(order, norms, truck, mode, max_trucks=20, by_stop=False):
    """Fill trucks of the same type one after another."""
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
    keep, CONFIG["_level"] = CONFIG.get("_level"), None          # single category: no common level cap
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
    CONFIG["_level"] = keep
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
    _set_level(order, norms)
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
        r[NV(t)] = -_floor(tl[t]) * derate; w[NV(t)] = -tl[t].payload_kg * derate
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
    fleet = [tl[t] for t in range(T) for _ in range(v[NV(t)])]
    fleet.sort(key=lambda t: -t.payload_kg)
    asg = assign_to_trucks(order, norms, fleet, by_stop, time_limit=8)
    plans = asg["plans"]
    plans.sort(key=lambda p: -p["truck"].payload_kg)
    planned = sum(p["kpi"]["cases"] for p in plans)
    return {"ok": True, "plans": plans, "planned": planned, "ordered": sum(ln.qty for ln in L0), "message": str(res.message)}


def assign_to_trucks(order: pd.DataFrame, norms: pd.DataFrame, trucks: list, by_stop: bool = False, time_limit: int = 15) -> dict:
    """Assign order lines to specific trucks so that loads are stable and easy to handle:
    every case placed (or reported short), each line on as few trucks as possible, any split piece at least
    one full-width row, and each drop kept on one truck where possible. Each truck is then laid out exactly."""
    src = order.reset_index(drop=True)
    per = [build_lines(src, norms, t) for t in trucks]
    L0 = per[0]; n, m = len(L0), len(trucks)
    if n == 0 or m == 0:
        return {"plans": [], "short": {}}
    drops = sorted({ln.drop for ln in L0 if ln.drop})
    dk = {d: k for k, d in enumerate(drops)}; nd = len(drops)
    X = lambda i, j: i * m + j
    R = lambda i, j: n * m + i * m + j
    Y = lambda i, j: 2 * n * m + i * m + j
    U = lambda i: 3 * n * m + i
    D = lambda k, j: 3 * n * m + n + k * m + j
    nv = 3 * n * m + n + nd * m
    cpr = lambda i, j: max(o.cpr for o in per[j][i].orients)
    dep = lambda i, j: next((o.depth for o in per[j][i].orients if o.cpr == cpr(i, j)), 0)
    A, lo, hi = [], [], []
    def row(): return np.zeros(nv)
    for i, ln in enumerate(L0):
        r = row(); r[U(i)] = 1
        for j in range(m):
            r[X(i, j)] = 1
        A.append(r); lo.append(ln.qty); hi.append(ln.qty)
        for j in range(m):
            c_ = cpr(i, j)
            r = row(); r[X(i, j)] = 1; r[R(i, j)] = -c_; A.append(r); lo.append(-np.inf); hi.append(0)      # rows
            r = row(); r[X(i, j)] = 1; r[Y(i, j)] = -ln.qty; A.append(r); lo.append(-np.inf); hi.append(0)   # used flag
            mp = min(ln.qty, c_) if c_ else 0                                                               # min piece = one full row
            r = row(); r[X(i, j)] = 1; r[Y(i, j)] = -mp; A.append(r); lo.append(0); hi.append(np.inf)
            if ln.drop:
                r = row(); r[Y(i, j)] = 1; r[D(dk[ln.drop], j)] = -1; A.append(r); lo.append(-np.inf); hi.append(0)
    for j, t in enumerate(trucks):
        r = row(); w = row()
        for i, ln in enumerate(L0):
            r[R(i, j)] = dep(i, j); w[X(i, j)] = ln.weight
        A += [r, w]; lo += [-np.inf, -np.inf]; hi += [_floor(t), t.payload_kg]
    lb = np.zeros(nv); ub = np.full(nv, np.inf)
    for i, ln in enumerate(L0):
        for j in range(m):
            c_ = cpr(i, j)
            ub[X(i, j)] = ln.qty if c_ else 0
            ub[R(i, j)] = math.ceil(ln.qty / c_) if c_ else 0
            ub[Y(i, j)] = 1
    for k in range(nd):
        for j in range(m):
            ub[D(k, j)] = 1
    c = np.zeros(nv)
    for i in range(n):
        c[U(i)] = 1000
        for j in range(m):
            c[Y(i, j)] = 10; c[R(i, j)] = 0.001
    for k in range(nd):
        for j in range(m):
            c[D(k, j)] = 6
    res = milp(c, constraints=LinearConstraint(np.array(A), lo, hi), integrality=np.ones(nv), bounds=Bounds(lb, ub),
               options={"time_limit": time_limit, "mip_rel_gap": 0.01})
    if res.x is None:
        return {"plans": [], "short": {ln.category: ln.qty for ln in L0}}
    v = np.round(res.x).astype(int)
    plans = []
    for j, t in enumerate(trucks):
        sub = src.copy(); sub["Order qty (cases)"] = 0; sub["Must-load min (cases)"] = None
        for i, ln in enumerate(L0):
            sub.loc[ln.idx, "Order qty (cases)"] = v[X(i, j)]
        sub = sub[sub["Order qty (cases)"] > 0]
        if len(sub):
            p = _make_plan(sub, norms, t, by_stop)
            if p:
                plans.append(p)
    short = {L0[i].category: int(v[U(i)]) for i in range(n) if v[U(i)] > 0}
    return {"plans": plans, "short": short}


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


# ----------------------------------------------------------------- full-layer rounding (decision aid)
def layer_rounding(plans: list) -> pd.DataFrame:
    """For every block on every truck: how many cases to ADD to complete the part layer on top, or to REMOVE
    to drop it, so the block is perfectly level (all stacks the same height). One row per block."""
    rows = []
    for n, p in enumerate(plans, start=1):
        b, t, k = p["blocks"], p["truck"], p["kpi"]
        if b is None or b.empty:
            continue
        spare = t.payload_kg - k["weight_kg"]
        info = {ln.category: ln for ln in p["lines"]}
        for _, r in b.iterrows():
            if str(r.get("Note", "")).startswith("BASE TIER"):
                continue                                         # flat by design
            ln = info.get(r["Category"])
            if ln is None:
                continue
            lanes, nrows, x = int(r["Lanes"]), int(r["Rows"]), int(r["Cases"])
            layer = lanes * nrows                                # one full layer over this block's floor
            base, extra = divmod(x, layer)
            add = (layer - extra) if extra else 0
            rem = extra
            z0 = float(r["Stack height (mm)"]) - int(r["Layers"]) * ln.H
            add_ok = add == 0 or (add * ln.weight <= spare + 1e-6 and base + 1 <= ln.norm_layers)
            if not extra:
                sug = "Keep (full layers)"
            elif base == 0 or (extra > layer / 2 and add_ok):
                sug = "Add" if add_ok else "Remove"
            else:
                sug = "Remove" if base > 0 else "Keep"
            rows.append({"Truck": f"Truck {n}", "Vehicle": t.name, "Stop": int(r["Stop"]) or None, "Drop": r["Drop"] or "",
                         "Category": r["Category"], "Planned cases": x, "Cases per full layer": layer,
                         "Full layers": base, "Part-layer cases": extra,
                         "Add for full layer (cases)": add, "Add weight (kg)": round(add * ln.weight),
                         "Height if added (mm)": round(z0 + (base + 1) * ln.H) if extra else round(z0 + base * ln.H),
                         "Remove for full layer (cases)": rem,
                         "Height if removed (mm)": round(z0 + base * ln.H),
                         "Add fits truck?": "Yes" if add_ok else f"No - {spare:,.0f} kg spare",
                         "Suggested": sug})
    return pd.DataFrame(rows)


def apply_adjustments(order: pd.DataFrame, adj: dict) -> pd.DataFrame:
    """adj: {(drop, category): +/- cases}. Returns a copy of the order with the quantities changed."""
    if not adj:
        return order
    o = order.copy()
    q = pd.to_numeric(o["Order qty (cases)"], errors="coerce").fillna(0)
    drops = o["Drop"].fillna("").astype(str) if "Drop" in o else pd.Series("", index=o.index)
    for (drop, cat), d in adj.items():
        idx = o.index[(drops == str(drop)) & (o["Category"] == cat)]
        if len(idx):
            q.loc[idx[0]] = max(0, q.loc[idx[0]] + d)
    o["Order qty (cases)"] = q.astype(int)
    return o
