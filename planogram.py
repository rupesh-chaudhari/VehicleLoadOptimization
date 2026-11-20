"""Plotly planogram views of a load plan."""
import plotly.graph_objects as go
import pandas as pd

PALETTE = ["#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD", "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF"]


def colour(line: int) -> str:
    return PALETTE[(int(line) - 1) % len(PALETTE)]


def _groups(stacks: pd.DataFrame, by: str, names: dict | None = None):
    """Yield (key, label, frame) for colouring by order line or by milk-run stop."""
    for key, g in stacks.groupby(by):
        if by == "Stop":
            label = f"Stop {key}: {(names or {}).get(g.Drop.iloc[0], g.Drop.iloc[0])}"
        else:
            label = f"{key}. {g.Category.iloc[0]}"
        yield key, label, g


def _void(fig, stacks, L, top):
    if stacks.empty:
        return
    used = float((stacks["x"] + stacks["dx"]).max())
    if L - used > 100:
        fig.add_shape(type="rect", x0=used, x1=L, y0=0, y1=top, fillcolor="rgba(150,150,150,0.25)", line=dict(color="#999", dash="dot"))
        fig.add_shape(type="line", x0=used + 30, x1=used + 30, y0=0, y1=top, line=dict(color="#C00000", width=4))
        fig.add_annotation(x=(used + L) / 2, y=top / 2, text=f"Void {(L - used) / 1000:.2f} m<br>load-lock bar /<br>dunnage bag" if L - used > 600
                           else "Load-lock<br>bar", showarrow=False, font=dict(size=9, color="#C00000"))


def _separators(fig, blocks, top):
    if blocks is None or blocks.empty or "Separator after" not in blocks:
        return
    for _, b in blocks[blocks["Separator after"].astype(bool)].iterrows():
        x = b["To (mm)"]
        fig.add_shape(type="line", x0=x, x1=x, y0=-40, y1=top + 40, line=dict(color="#000", width=3, dash="dash"))
        fig.add_annotation(x=x, y=top + 120, text="separator", showarrow=False, font=dict(size=9))


def _frame(fig, L, W_or_H, title, ylab):
    fig.add_shape(type="rect", x0=0, y0=0, x1=L, y1=W_or_H, line=dict(color="#333", width=2))
    fig.update_layout(title=title, height=330, margin=dict(l=40, r=20, t=50, b=40), plot_bgcolor="white",
                      showlegend=True, legend=dict(orientation="h", y=-0.25))
    fig.update_xaxes(title="Length from cab (mm)  ◄ FRONT · REAR ►", range=[-100, L + 100], showgrid=False)
    fig.update_yaxes(title=ylab, range=[-80, W_or_H * 1.08], scaleanchor="x", scaleratio=1, showgrid=False)


def top_view(stacks: pd.DataFrame, blocks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    tier = stacks["tier"] if "tier" in stacks else pd.Series(0, index=stacks.index)
    for line, label, g in _groups(stacks[tier == 0], by, names):
        xs, ys, text = [], [], []
        for _, s in g.iterrows():
            xs += [s.x, s.x + s.dx, s.x + s.dx, s.x, s.x, None]
            ys += [s.y, s.y, s.y + s.dy, s.y + s.dy, s.y, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", mode="lines", line=dict(color="white", width=1),
                                 fillcolor=colour(line), name=label, hoverinfo="name"))
    for line, label, g in _groups(stacks[tier == 1], by, names):      # top tier: hatched, dark outline
        xs, ys = [], []
        for _, s in g.iterrows():
            xs += [s.x, s.x + s.dx, s.x + s.dx, s.x, s.x, None]
            ys += [s.y, s.y, s.y + s.dy, s.y + s.dy, s.y, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", mode="lines", line=dict(color="#222", width=1),
                                 fillcolor=colour(line), fillpattern=dict(shape="/", fgcolor="rgba(0,0,0,0.35)"),
                                 name=f"{label} (top tier)", hoverinfo="name", showlegend=False))
    for _, b in blocks.iterrows():
        t_ = int(b.get("Tier", 0) or 0)
        tiered = "Tier" in blocks and ((blocks["From (mm)"] == b["From (mm)"]) & (blocks["Tier"] == 1)).any()
        k = b[by]; mid = (b["From (mm)"] + b["To (mm)"]) / 2
        width_mm = b["To (mm)"] - b["From (mm)"]
        tag = f"S{k}" if by == "Stop" else f"{k}"
        if t_ == 0:
            fig.add_annotation(x=mid, y=truck.width + 70, showarrow=False, text=f"<b>{tag}</b>", font=dict(size=11, color=colour(k)))
        if width_mm > truck.length * 0.07 or t_ == 1:
            yy = truck.width * (0.75 if t_ == 1 else 0.25) if tiered else truck.width / 2
            head = "▲ TOP: " if t_ == 1 else ("BASE: " if tiered else "")
            fig.add_annotation(x=mid, y=yy, showarrow=False, bgcolor="rgba(255,255,255,0.9)", borderpad=2,
                               text=f"<b>{head}{b.Category[:16]}</b><br>{b.Lanes}×{b.Layers} · {b.Rows} rows<br>{b.Cases} cs", font=dict(size=9, color="#222"))
    _void(fig, stacks, truck.length, truck.width)
    _separators(fig, blocks, truck.width)
    _frame(fig, truck.length, truck.width, "Top view - each tile is one stack of cases (stacks centred across the width)", "Width (mm)")
    return fig


def side_view(stacks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None, blocks: pd.DataFrame | None = None) -> go.Figure:
    fig = go.Figure()
    # tallest stack per row position (what you see from the side)
    stacks = stacks.copy()
    if "z0" not in stacks:
        stacks["z0"] = 0.0
    side = stacks.sort_values("z").groupby(["x", "Line", "z0"], as_index=False).last()
    for line, label, g in _groups(side, by, names):
        xs, ys = [], []
        for _, s in g.iterrows():
            for k in range(int(s.layers)):
                y0 = s.z0 + k * s.h
                xs += [s.x, s.x + s.dx, s.x + s.dx, s.x, s.x, None]; ys += [y0, y0, y0 + s.h, y0 + s.h, y0, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", mode="lines", line=dict(color="white", width=1),
                                 fillcolor=colour(line), name=label, hoverinfo="name"))
    _void(fig, stacks, truck.length, truck.height)
    _separators(fig, blocks, truck.height)
    _frame(fig, truck.length, truck.height, "Side view - each band is one layer", "Height (mm)")
    return fig


def view_3d(stacks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    faces = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    for line, label, g in _groups(stacks, by, names):
        X, Y, Z, I, J, K = [], [], [], [], [], []
        ex, ey, ez = [], [], []
        for _, s in g.iterrows():
            b = len(X); x0, x1, y0, y1, z1 = s.x, s.x + s.dx, s.y, s.y + s.dy, s.z
            zb = float(s.get("z0", 0) or 0)
            X += [x0, x1, x1, x0, x0, x1, x1, x0]; Y += [y0, y0, y1, y1, y0, y0, y1, y1]; Z += [zb, zb, zb, zb, z1, z1, z1, z1]
            for f in faces:
                I.append(b + f[0]); J.append(b + f[1]); K.append(b + f[2])
            for (a, c) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
                ex.append(a); ey.append(c); ez.append(z1)
            ex.append(None); ey.append(None); ez.append(None)
        fig.add_trace(go.Mesh3d(x=X, y=Y, z=Z, i=I, j=J, k=K, color=colour(line), opacity=1, flatshading=True,
                                name=label, showlegend=True, hoverinfo="name"))
        fig.add_trace(go.Scatter3d(x=ex, y=ey, z=ez, mode="lines", line=dict(color="white", width=2), showlegend=False, hoverinfo="skip"))
    L, W, H = truck.length, truck.width, truck.height
    bx = [0, L, L, 0, 0, None, 0, L, L, 0, 0, None, 0, 0, None, L, L, None, L, L, None, 0, 0]
    by = [0, 0, W, W, 0, None, 0, 0, W, W, 0, None, 0, 0, None, 0, 0, None, W, W, None, W, W]
    bz = [0, 0, 0, 0, 0, None, H, H, H, H, H, None, 0, H, None, 0, H, None, 0, H, None, 0, H]
    fig.add_trace(go.Scatter3d(x=bx, y=by, z=bz, mode="lines", line=dict(color="#555", width=3), name="Truck body"))
    fig.update_layout(height=520, margin=dict(l=0, r=0, t=30, b=0), legend=dict(orientation="h"),
                      scene=dict(aspectmode="data", xaxis_title="Length (mm)", yaxis_title="Width (mm)", zaxis_title="Height (mm)",
                                 camera=dict(eye=dict(x=-1.3, y=-1.6, z=0.9))))
    return fig

